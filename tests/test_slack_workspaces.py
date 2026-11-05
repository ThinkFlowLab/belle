"""Several Slack workspaces in one process, on a fake transport that models two.

``slack.workspaces`` gives one connector per entry: a Socket Mode connection
and a web client of its own. The two workspaces here use the same channel
id, the same user id and the same message ids on purpose. What the host
keeps apart for any platform is in
``tests/conformance/test_conformance_workspaces.py``. These checks hold the
Slack side: which client each call was made with.
"""

import asyncio
import json
import logging

import pytest
import pytest_asyncio
from conformance_kit import ModelTurn, worlds
from scripted_backend import ScriptedBackend, ScriptedTurn
from slack_kit import SlackKit
from slack_transport import (
    APP_TOKEN,
    BOT_TOKEN,
    BOT_USER,
    SECOND_APP_TOKEN,
    SECOND_BOT_TOKEN,
    SECOND_TEAM,
    TEAM,
    FakeSlackWeb,
    FakeSocket,
    button,
    click_body,
)
from test_slack_connector import ASKER, OTHER_ROOM, ROOM, TS, _config, _message
from test_slack_turn_surfaces import clock  # noqa: F401

from belle_agent.backend import VERDICT_DENY, ToolCall
from belle_agent.config import BelleConfigError
from belle_agent.host import OUTCOME_POSTED_FINAL
from belle_agent.platforms import installations
from belle_agent.platforms.base import InboundMessage, PlatformUnreachable, create_platform
from belle_agent.platforms.installations import InstallationUnknown
from belle_agent.platforms.slack import connector
from belle_agent.platforms.slack.socket_mode import SlackAppTokenRefused
from belle_agent.platforms.slack.workspaces import SlackWorkspaces
from belle_agent.tools.host import APPROVE

LOGGER = "belle_agent.platforms.slack.workspaces"

TWO = """
platforms:
  slack:
    enforce_audience: true
    acknowledge_mode: reaction
    workspaces:
      - {bot_token: "${SLACK_BOT_TOKEN}", app_token: "${SLACK_APP_TOKEN}"}
      - {bot_token: "${SLACK_BOT_TOKEN_TWO}", app_token: "${SLACK_APP_TOKEN_TWO}"}
scopes:
  - match: {channel: slack}
    delivery: {mode: [all]}
"""
ASK_TO_REACT = TWO + "  - match: {channel: slack}\n    permissions: {tools: {react_to_message: ask}}\n"


@pytest.fixture
def slack(monkeypatch) -> SlackKit:
    """The fake transport, with the tokens of two Slack apps in the environment."""
    kit = SlackKit(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN_TWO", SECOND_BOT_TOKEN)
    monkeypatch.setenv("SLACK_APP_TOKEN_TWO", SECOND_APP_TOKEN)
    return kit


@pytest_asyncio.fixture
async def build(slack, tmp_path, monkeypatch):
    """A builder of a host on the workspaces a configuration lists, with the scripted backend behind it."""
    # The suite's driver speaks for one workspace. These checks deliver through each fake app themselves.
    monkeypatch.setattr(SlackKit, "attach", lambda self, host: host.platform)
    async with worlds(ScriptedBackend(), tmp_path, slack) as build_world:
        yield build_world


@pytest_asyncio.fixture
async def two(build, slack):
    """A builder of a host on two workspaces. It returns the world and the fake Slack of each workspace."""

    async def start(settings: str = TWO):
        world = await build(settings)
        first, second = slack.sockets[-2:]
        assert (first.web.team, second.web.team) == (TEAM, SECOND_TEAM)
        return world, first, second

    return start


def _sent(socket: FakeSocket) -> list[tuple[str, str]]:
    return [(payload["channel"], payload["text"]) for method, payload, _ in socket.web.sent if method == "chat_postMessage"]


def _reactions(socket: FakeSocket) -> list[str]:
    return [payload["name"] for payload in socket.web.payloads("reactions_add")]


def _methods(socket: FakeSocket) -> set[str]:
    return {method for method, _ in socket.web.calls}


# ── connections ──────────────────────────────────────────────────────────


async def test_each_workspace_has_a_connection_and_a_client_of_its_own(two):
    world, first, second = await two()

    assert isinstance(world.host.platform, SlackWorkspaces)
    assert (first.tokens, second.tokens) == ((BOT_TOKEN, APP_TOKEN), (SECOND_BOT_TOKEN, SECOND_APP_TOKEN))
    assert first.connected and second.connected and first.web is not second.web
    assert _methods(first) == _methods(second) == {"auth_test"}

    await world.host.stop()
    assert first.closed and second.closed


async def test_place_checks_use_only_the_named_workspace(two):
    world, first, second = await two()
    platform = world.host.platform
    first.web.conversations[ROOM] = {"is_member": True}
    second.web.conversations[ROOM] = {"is_member": False}

    assert platform.workspaces == frozenset({TEAM, SECOND_TEAM})
    assert (await platform.check_place(ROOM, "", workspace=TEAM)).member
    assert not (await platform.check_place(ROOM, "", workspace=SECOND_TEAM)).member
    await world.host.stop()


async def test_a_disabled_entry_is_not_connected(build, slack):
    await build(TWO.replace('app_token: "${SLACK_APP_TOKEN}"}', 'app_token: "${SLACK_APP_TOKEN}", enabled: false}'))

    assert [socket.tokens[0] for socket in slack.sockets] == [SECOND_BOT_TOKEN]


# ── the same ids in two workspaces ───────────────────────────────────────


async def test_the_same_channel_user_and_message_ids_in_two_workspaces_are_two_conversations(two):
    world, first, second = await two()
    world.kit.play(ModelTurn(final="one answered", hold=True), ModelTurn(final="two answered"))

    assert (await first.app.deliver(_message("from one"), event_id="Ev1")).startswith("dispatched:")
    await world.kit.holding()
    # The same channel, user and ts through the other app: a turn of its own, not a duplicate and not queued.
    assert (await second.app.deliver(_message("from two"), event_id="Ev2")).startswith("dispatched:")
    await world.handed(2)
    one, two_ = world.requests
    assert one.conversation != two_.conversation
    assert (one.metadata["slack_team_id"], two_.metadata["slack_team_id"]) == (TEAM, SECOND_TEAM)
    # Controls in the first workspace: its message again is a duplicate, and a new one waits behind its turn.
    assert await first.app.deliver(_message("from one"), event_id="Ev3") == "ignored:already-handled (dedupe)"
    assert (await first.app.deliver(_message("more", ts="1710000002.000100", thread_ts=TS), event_id="Ev4")).startswith("queued:")
    # The tools of the second workspace's session closed with its turn, while the first still runs.
    await world.turn_ended(two_.request_id)
    assert (await world.host.tools.authorize(ToolCall(conversation=two_.conversation, name="post_message"))).action == VERDICT_DENY

    world.kit.release()
    await world.host.idle()

    # Each answer went out through the client of the workspace its message came from, and through no other.
    assert _sent(first) == [(ROOM, "one answered"), (ROOM, "ok")]
    assert _sent(second) == [(ROOM, "two answered")]
    # The acknowledgement is added and removed in its own workspace.
    assert [payload["timestamp"] for payload in second.web.payloads("reactions_add")] == [TS]
    assert [payload["timestamp"] for payload in second.web.payloads("reactions_remove")] == [TS]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL] * 3


async def test_a_tool_call_acts_in_the_workspace_of_its_turn_and_in_no_other(two):
    world, first, second = await two()
    for socket in (first, second):
        socket.web.members.update({ROOM: [ASKER, BOT_USER], OTHER_ROOM: [ASKER, BOT_USER]})
    calls = (("react_to_message", {"emoji": "tada"}), ("post_message", {"text": "over there", "chat_id": OTHER_ROOM}))
    world.kit.play(ModelTurn(final="done", calls=calls))

    await second.app.deliver(_message("post it over there"), event_id="Ev1")
    await world.host.idle()

    assert json.loads(world.kit.results[1])["ok"] is True
    assert (OTHER_ROOM, "over there") in _sent(second)
    assert "tada" in _reactions(second)
    # The other workspace has the same channel ids, and was asked nothing: not a post, not a membership, not a mark.
    assert _methods(first) == {"auth_test"}


async def test_a_tool_call_of_a_workspace_that_is_not_served_is_refused(two):
    world, first, second = await two()
    metadata = {"slack_team_id": "W9", "slack_channel_id": ROOM, "slack_user_id": ASKER, "slack_enforce_audience": True}
    post = next(kit for kit in world.host.platform.toolkits(lambda: metadata) if hasattr(kit, "post_message"))

    refused = json.loads(await post.post_message(text="anywhere"))

    assert refused["error"] == "slack_workspace_unknown"
    assert await post.widening_question("post_message", {"chat_id": OTHER_ROOM}) is None
    assert _sent(first) == _sent(second) == []


async def test_a_question_and_a_stop_of_one_workspace_are_not_taken_from_the_other(two):
    world, first, second = await two(ASK_TO_REACT)
    world.kit.play(ModelTurn(final="done", calls=(("react_to_message", {"emoji": "tada"}),)))

    await first.app.deliver(_message("react"), event_id="Ev1")
    await world.kit.holding()
    question = next(payload for payload in first.web.payloads("chat_postMessage") if payload.get("blocks"))
    ts = next(sent_ts for _, payload, sent_ts in first.web.sent if payload is question)
    pressed = button(question["blocks"], value=APPROVE)
    assert second.web.payloads("chat_postMessage") == []

    # The same button and the same thread, through the other workspace's app.
    stale = click_body(user=ASKER, channel=ROOM, message_ts=ts, payload=question, team=SECOND_TEAM)
    assert await second.app.click(stale, pressed) == "ignored:question-not-waiting"
    stop = {"type": "agent_session_stopped", "channel": ROOM, "thread_ts": TS, "user": ASKER}
    assert await second.app.deliver(stop) == "ignored:no-turn-running"
    assert "tada" not in _reactions(first) and world.outcomes() == []

    # Control: through the workspace that asked, the click releases the call.
    here = click_body(user=ASKER, channel=ROOM, message_ts=ts, payload=question, team=TEAM)
    assert (await first.app.click(here, pressed)).startswith("answered")
    await world.host.idle()
    assert "tada" in _reactions(first) and _reactions(second) == []


async def test_a_file_is_fetched_with_the_token_of_its_workspace(two, monkeypatch):
    world, _first, second = await two()
    tokens: list[str] = []

    async def fetch(url: str, mimetype: str, *, bot_token: str, allowed_hosts) -> bytearray:
        tokens.append(bot_token)
        return bytearray(b"# notes\n")

    monkeypatch.setattr(connector, "fetch_attachment_bytes", fetch)
    file = {"id": "F0001", "name": "notes.md", "mimetype": "text/markdown", "url_private": "https://files.slack.com/files-pri/F0001/notes.md"}

    await second.app.deliver(_message("read this", subtype="file_share", files=[file]), event_id="Ev1")
    await world.host.idle()

    assert tokens == [SECOND_BOT_TOKEN]
    assert [entry["name"] for entry in world.requests[0].files] == ["notes.md"]


async def test_a_reply_as_it_is_written_and_an_activity_card_stay_in_the_workspace_of_their_turn(two, clock):  # noqa: F811
    world, first, second = await two(TWO.replace("acknowledge_mode: reaction", "enable_streaming: true\n    activity_card: true"))
    calls = (("react_to_message", {"emoji": "anchor"}),)
    world.kit.play(ScriptedTurn(final="It is noon.\nThe ferry has left.\n", calls=calls, hold_before_final=True))

    await second.app.deliver(_message("what time is it"), event_id="Ev1")
    await world.kit.holding()
    await clock.advance(5)
    world.kit.release()
    await world.host.idle()

    # The card was posted and settled, and the reply was streamed, through the second workspace's client.
    assert {"chat_postMessage", "chat_update", "chat_startStream", "chat_stopStream"} <= _methods(second)
    card, reply = second.web.screen.values()
    assert card["blocks"][0]["title"] == "Turn finished" and reply["text"].strip() == "It is noon.\nThe ferry has left."
    assert second.web.payload("chat_startStream")["recipient_team_id"] == SECOND_TEAM
    # The first workspace has the same channel and thread, and saw none of it.
    assert _methods(first) == {"auth_test"} and first.web.screen == {}


async def test_an_action_that_names_no_served_workspace_is_not_sent_anywhere(two):
    world, first, second = await two()
    platform = world.host.platform

    for workspace in ("", "W9"):
        with pytest.raises(InstallationUnknown, match="nothing is sent"):
            await platform.post(ROOM, "", "to whom?", workspace=workspace)
    unknown = InboundMessage(platform="slack", workspace="W9", conversation=ROOM, message_id=TS, sender=ASKER, text="x")
    with pytest.raises(InstallationUnknown):
        await platform.mark(unknown, "received")

    assert _sent(first) == _sent(second) == []
    # Control: named, it goes out through that workspace's client alone.
    await platform.post(ROOM, "", "to two", workspace=SECOND_TEAM)
    assert (_sent(first), _sent(second)) == ([], [(ROOM, "to two")])


# ── configuration, start and failure ─────────────────────────────────────


def _platform(tmp_path, text: str = TWO):
    return create_platform(_config(tmp_path, text.lstrip()), "slack")


def test_a_token_written_in_an_entry_is_refused_and_not_shown(slack, tmp_path):
    literal = TWO.replace('"${SLACK_BOT_TOKEN_TWO}"', "xoxb-99-written-in-the-file")

    with pytest.raises(BelleConfigError, match=r"platforms.slack.workspaces\[1\].bot_token must be an environment reference") as raised:
        _platform(tmp_path, literal)
    assert "xoxb-99" not in str(raised.value)


def test_a_token_of_the_wrong_kind_is_not_logged(slack, tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_TWO", "xapp-wrong-kind")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        platform = _platform(tmp_path, TWO)

    assert len(list(platform.members.values())) == 1
    assert "slack.workspaces[1] is NOT SERVED: its bot_token does not hold a token" in caplog.text
    assert "xapp-wrong-kind" not in caplog.text


def test_one_app_listed_twice_is_refused_with_what_it_would_cost(slack, tmp_path):
    twice = TWO.replace("_TWO}", "}")

    with pytest.raises(BelleConfigError, match=r"platforms.slack.workspaces\[1\].bot_token is the token of platforms.slack.workspaces\[0\]") as raised:
        _platform(tmp_path, twice)
    assert "each receive part of its events" in str(raised.value) and "xoxb-" not in str(raised.value)


def test_the_list_takes_precedence_over_the_single_pair(slack, tmp_path, monkeypatch):
    # The single pair is in the environment and names another app. With a list, it is not read.
    monkeypatch.setenv("SLACK_BOT_TOKEN_ONE", BOT_TOKEN)
    monkeypatch.setenv("SLACK_APP_TOKEN_ONE", APP_TOKEN)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-00-not-in-the-list")
    listed = TWO.replace("${SLACK_BOT_TOKEN}", "${SLACK_BOT_TOKEN_ONE}").replace("${SLACK_APP_TOKEN}", "${SLACK_APP_TOKEN_ONE}")

    platform = _platform(tmp_path, listed)

    assert [member._bot_token for member in list(platform.members.values())] == [BOT_TOKEN, SECOND_BOT_TOKEN]


def _sockets_with(slack, monkeypatch, webs: dict[str, FakeSlackWeb]) -> None:
    """Give the connection of one bot token a Slack that answers as ``webs`` says."""

    def socket(bot_token: str, app_token: str, register, **_) -> FakeSocket:
        slack.sockets.append(FakeSocket(bot_token, app_token, register, web=webs.get(bot_token)))
        return slack.sockets[-1]

    monkeypatch.setattr(connector, "SlackSocket", socket)


def _errors(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


async def _still_answers(world, socket: FakeSocket) -> None:
    world.kit.play(ModelTurn(final="still here"))
    await socket.app.deliver(_message("anyone?"), event_id="Ev-check")
    await world.host.idle()
    assert _sent(socket) == [(ROOM, "still here")]


async def test_an_entry_whose_variable_is_not_set_is_named_and_the_others_are_served(build, slack, monkeypatch, caplog):
    monkeypatch.delenv("SLACK_APP_TOKEN_TWO")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        world = await build(TWO)

    assert [socket.tokens[0] for socket in slack.sockets] == [BOT_TOKEN]
    assert any("slack.workspaces[1] is NOT SERVED: its app_token is empty" in line for line in _errors(caplog))
    await _still_answers(world, slack.sockets[0])


async def test_a_workspace_slack_refuses_is_named_at_start_and_the_others_are_served(build, slack, monkeypatch, caplog):
    _sockets_with(slack, monkeypatch, {SECOND_BOT_TOKEN: FakeSlackWeb(fail={"auth_test": "invalid_auth"}, team=SECOND_TEAM)})

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        world = await build(TWO)

    first, second = slack.sockets
    errors = _errors(caplog)
    assert any(
        "platforms.slack.workspaces[1] is NOT SERVED: Slack did not accept the bot token of platforms.slack.workspaces[1].bot_token" in line
        and "invalid_auth" in line
        for line in errors
    )
    assert any(line.startswith("slack serves 1 of 2 workspaces: platforms.slack.workspaces[0]. Not served: platforms.slack.workspaces[1]") for line in errors)
    assert "xoxb-" not in "".join(errors) and "xapp-" not in "".join(errors)
    # The connection that could not be served is closed, and the other one answers.
    assert second.closed and not second.connected and first.connected
    await _still_answers(world, first)


async def test_the_start_fails_when_no_workspace_can_be_served(build, slack, monkeypatch):
    refusing = {
        BOT_TOKEN: FakeSlackWeb(fail={"auth_test": "invalid_auth"}),
        SECOND_BOT_TOKEN: FakeSlackWeb(fail={"auth_test": "account_inactive"}, team=SECOND_TEAM),
    }
    _sockets_with(slack, monkeypatch, refusing)

    with pytest.raises(BelleConfigError, match="no workspace of slack.workspaces can be served") as raised:
        await build(TWO)

    assert "slack.workspaces[0]" in str(raised.value) and "slack.workspaces[1]" in str(raised.value)
    assert all(socket.closed for socket in slack.sockets)


async def test_a_workspace_whose_app_token_slack_refuses_does_not_hold_up_the_start(build, slack, monkeypatch, caplog):
    class Refused(FakeSocket):
        """A connection whose app token Slack refuses when it is asked for the connection URL."""

        async def connect(self) -> None:
            raise SlackAppTokenRefused("invalid_auth")

    def socket(bot_token: str, app_token: str, register, **_) -> FakeSocket:
        kind = Refused if bot_token == SECOND_BOT_TOKEN else FakeSocket
        slack.sockets.append(kind(bot_token, app_token, register))
        return slack.sockets[-1]

    monkeypatch.setattr(connector, "SlackSocket", socket)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        world = await asyncio.wait_for(build(TWO), 10)

    first, second = slack.sockets
    errors = _errors(caplog)
    assert any(
        "platforms.slack.workspaces[1] is NOT SERVED: Slack did not accept the app token of"
        " platforms.slack.workspaces[1].app_token: apps.connections.open answered invalid_auth" in line
        for line in errors
    )
    assert any(line.startswith("slack serves 1 of 2 workspaces: platforms.slack.workspaces[0]. Not served: platforms.slack.workspaces[1]") for line in errors)
    assert "xoxb-" not in "".join(errors) and "xapp-" not in "".join(errors)
    assert second.closed and first.connected
    await _still_answers(world, first)


@pytest.fixture
def quick_retries(monkeypatch):
    monkeypatch.setattr(installations, "RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(installations, "RETRY_MAX_SECONDS", 0.01)


class Unanswered(FakeSocket):
    """A connection whose socket does not open while its Slack does not answer."""

    answering = False

    async def connect(self) -> None:
        if not Unanswered.answering:
            raise PlatformUnreachable("the Slack socket did not open within 30 s")
        await super().connect()


async def _until(condition) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


@pytest.mark.parametrize("failure", ["auth_test", "socket"])
async def test_a_workspace_slack_does_not_answer_is_served_once_it_answers(build, slack, monkeypatch, caplog, quick_retries, failure):
    silent = {"auth_test": "service_unavailable"}
    web = FakeSlackWeb(fail=lambda method, _: silent.get(method), team=SECOND_TEAM)
    monkeypatch.setattr(Unanswered, "answering", failure != "socket")
    if failure == "socket":
        silent.clear()

    def socket(bot_token: str, app_token: str, register, **_) -> FakeSocket:
        if bot_token == SECOND_BOT_TOKEN:
            slack.sockets.append(Unanswered(bot_token, app_token, register, web=web))
        else:
            slack.sockets.append(FakeSocket(bot_token, app_token, register))
        return slack.sockets[-1]

    monkeypatch.setattr(connector, "SlackSocket", socket)
    caplog.set_level(logging.INFO, logger=LOGGER)

    world = await asyncio.wait_for(build(TWO), 10)
    platform = world.host.platform
    first = slack.sockets[0]

    # The start finished with the first workspace served, and the second is tried again in the background.
    assert any("platforms.slack.workspaces[1] is NOT CONNECTED" in line for line in _errors(caplog))
    assert platform.workspaces == frozenset({TEAM}) and platform.connecting == {"platforms.slack.workspaces[1]"}
    await _until(lambda: len(slack.sockets) > 3)
    # Each attempt that failed left nothing open.
    assert all(attempt.closed and not attempt.connected for attempt in slack.sockets[1:-1])
    with pytest.raises(InstallationUnknown, match="nothing is sent"):
        await platform.post(ROOM, "", "too early", workspace=SECOND_TEAM)
    await _still_answers(world, first)

    silent.clear()
    monkeypatch.setattr(Unanswered, "answering", True)
    await _until(lambda: SECOND_TEAM in platform.workspaces)
    second = slack.sockets[-1]
    assert second.connected and not platform.connecting
    assert any("platforms.slack.workspaces[1] is served now" in record.getMessage() for record in caplog.records)
    world.kit.play(ModelTurn(final="here at last"))
    await second.app.deliver(_message("and you?"), event_id="Ev-late")
    await world.host.idle()
    assert _sent(second) == [(ROOM, "here at last")]


async def test_the_start_fails_when_no_workspace_answers(build, slack, monkeypatch):
    silent = {
        BOT_TOKEN: FakeSlackWeb(fail={"auth_test": "service_unavailable"}),
        SECOND_BOT_TOKEN: FakeSlackWeb(fail={"auth_test": "ratelimited"}, team=SECOND_TEAM),
    }
    _sockets_with(slack, monkeypatch, silent)

    with pytest.raises(BelleConfigError, match="no workspace of slack.workspaces can be served") as raised:
        await asyncio.wait_for(build(TWO), 10)

    assert "did not answer auth.test: service_unavailable" in str(raised.value)
    assert "did not answer auth.test: ratelimited" in str(raised.value)
    assert all(socket.closed for socket in slack.sockets) and len(slack.sockets) == 2


REACTION_EVENTS = TWO + "  - match: {channel: slack}\n    delivery: {reply: optional, events: {reaction: turn}}\n"
#: The bot user of the second app of this Belle.
SECOND_BOT_USER = "U0SECONDBOT"
#: The bot user of an app this Belle does not serve, in a channel both workspaces share.
OTHER_APP_BOT = "U0OTHERAPPBOT"


def _reaction_by(user: str, kind: str = "reaction_added") -> dict:
    return {"type": kind, "user": user, "reaction": "eyes",
            "item": {"type": "message", "channel": ROOM, "ts": TS}, "event_ts": "1710000001.000200"}


def _users_info(socket: FakeSocket) -> list[str]:
    return [payload["user"] for method, payload in socket.web.calls if method == "users_info"]


async def test_the_other_apps_marks_in_a_shared_channel_start_no_event_turn(build, slack, monkeypatch):
    _sockets_with(slack, monkeypatch, {SECOND_BOT_TOKEN: FakeSlackWeb(team=SECOND_TEAM, bot_user=SECOND_BOT_USER)})
    world = await build(REACTION_EVENTS)
    first, second = slack.sockets

    # Each app's 👀 on a person's message, and its removal, as the other app receives them in a Slack Connect channel.
    outcomes = [
        await second.app.deliver(_reaction_by(BOT_USER), event_id="Ev-first-adds"),
        await second.app.deliver(_reaction_by(BOT_USER, "reaction_removed"), event_id="Ev-first-removes"),
        await first.app.deliver(_reaction_by(SECOND_BOT_USER), event_id="Ev-second-adds"),
    ]

    assert outcomes == ["ignored:done-by-an-app"] * 3
    # Their bot users are known from auth.test, so nobody is asked.
    assert _users_info(first) == _users_info(second) == []
    await world.host.stop()


async def test_another_apps_reaction_in_a_shared_channel_starts_no_event_turn(two):
    world, first, second = await two(REACTION_EVENTS)
    second.web.bots.add(OTHER_APP_BOT)

    outcome = await second.app.deliver(_reaction_by(OTHER_APP_BOT), event_id="Ev-other-app")

    assert outcome == "ignored:done-by-an-app", outcome
    assert _users_info(second) == [OTHER_APP_BOT] and _users_info(first) == []
    await world.host.stop()


async def test_a_persons_reaction_in_a_shared_channel_still_starts_its_event_turn(two):
    world, first, second = await two(REACTION_EVENTS)
    world.kit.play(ModelTurn(final="noted"))

    outcome = await second.app.deliver(_reaction_by(ASKER), event_id="Ev-person")
    await world.host.idle()

    assert outcome.startswith("dispatched:event:reaction"), outcome
    await world.host.stop()


async def test_two_apps_installed_in_one_workspace_are_not_both_served(build, slack, monkeypatch, caplog):
    _sockets_with(slack, monkeypatch, {SECOND_BOT_TOKEN: FakeSlackWeb(team=TEAM)})

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        world = await build(TWO)

    first, second = slack.sockets
    assert any("slack.workspaces[1] is NOT SERVED" in line and "slack.workspaces[0] already serves" in line for line in _errors(caplog))
    assert second.closed
    await _still_answers(world, first)
