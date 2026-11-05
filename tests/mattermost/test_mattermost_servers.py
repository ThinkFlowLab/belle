"""Several Mattermost servers in one process, on fake servers that use the same ids on purpose.

``platforms.mattermost.servers`` gives one connector per server: a WebSocket,
a REST client and an action listener of its own. What the host keeps apart
for any platform is in ``tests/conformance/test_conformance_workspaces.py``.
These checks hold the Mattermost side: which server each call was made on.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from transport import FakeRest, posted

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import ModelTurn, worlds
from mattermost_kit import MattermostServersKit
from scripted_backend import ScriptedBackend

from belle_agent.click_notices import WITHDRAWN_QUESTION_NOTE
from belle_agent.config import BelleConfigError
from belle_agent.platforms import installations
from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.installations import InstallationConnecting, InstallationUnknown
from belle_agent.platforms.mattermost import commands
from belle_agent.platforms.mattermost.servers import SERVER_UNKNOWN, MattermostServers

LOGGER = "belle_agent.platforms.mattermost.servers"
TEAM, CHANNEL, OTHER, ONLY_ON_ONE = "t" * 26, "c" * 26, "d" * 26, "e" * 26
ALICE, MESSAGE = "a" * 26, "m" * 26
(ONE, _, ONE_URL), (TWO, _, TWO_URL) = MattermostServersKit.SERVERS

SETTINGS = """
platforms:
  mattermost:
    acknowledgement_emoji: eyes
scopes:
  - match: {channel: mattermost}
    delivery: {mode: [all]}
"""
ASK_TO_REACT = SETTINGS + "  - match: {channel: mattermost}\n    permissions: {tools: {react_to_message: ask}}\n"


@pytest.fixture
def kit(monkeypatch) -> MattermostServersKit:
    return MattermostServersKit(monkeypatch)


@pytest_asyncio.fixture
async def two(kit, tmp_path, monkeypatch):
    """A builder of a host on two servers. It returns the world, the fake servers, and the connector of each."""
    # These checks deliver each server's events themselves.
    monkeypatch.setattr(MattermostServersKit, "attach", lambda self, host: host.platform)
    async with worlds(ScriptedBackend(), tmp_path, kit) as build:

        async def start(settings: str = SETTINGS):
            world = await build(settings)
            first, second = world.host.platform.members.values()
            for rest in kit.rests.values():
                rest.channels.update({
                    CHANNEL: {"id": CHANNEL, "type": "O", "team_id": TEAM, "name": "lounge"},
                    OTHER: {"id": OTHER, "type": "O", "team_id": TEAM, "name": "other"},
                })
                rest.members.update({CHANNEL: [ALICE, rest.bot], OTHER: [ALICE, rest.bot]})
            kit.rests[ONE_URL].channels[ONLY_ON_ONE] = {"id": ONLY_ON_ONE, "type": "O", "team_id": TEAM, "name": "only"}
            kit.rests[ONE_URL].members[ONLY_ON_ONE] = [ALICE, kit.rests[ONE_URL].bot]
            return world, (first, kit.rests[ONE_URL]), (second, kit.rests[TWO_URL])

        yield start


def _message(text: str, post_id: str = MESSAGE, **fields) -> dict:
    return posted(post_id, channel=CHANNEL, user=ALICE, message=text, team=TEAM, **fields)


def _posts(rest: FakeRest) -> list[tuple[str, str]]:
    return [(post["channel_id"], post["message"]) for post in rest.posts.values() if post["user_id"] == rest.bot]


def _paths(rest: FakeRest) -> list[str]:
    return [f"{request.method} {request.path}" for request in rest.requests]


# ── connections ──────────────────────────────────────────────────────────


async def test_each_server_has_a_websocket_a_client_and_a_listener_of_its_own(two, kit):
    world, (first, one), (second, other) = await two()

    assert isinstance(world.host.platform, MattermostServers)
    assert world.host.platform.direct_chat_types == frozenset({"D"})
    assert world.host.platform.command_prefix(f"{ONE}:{TEAM}") == "/belle"
    assert (first.server, second.server) == (ONE, TWO) and first.bot_user_id != second.bot_user_id
    assert {request.headers["authorization"] for request in one.requests} == {f"Bearer {kit.token(ONE)}"}
    assert {request.headers["authorization"] for request in other.requests} == {f"Bearer {kit.token(TWO)}"}
    assert [opened[1] for opened in kit.feeders[ONE_URL].opened] == [kit.token(ONE)]
    assert first.actions.port != second.actions.port

    await world.host.stop()
    assert kit.feeders[ONE_URL].closed and kit.feeders[TWO_URL].closed


async def test_place_checks_resolve_dynamic_teams_on_the_named_server(two):
    world, (_first, _one), (_second, other) = await two()
    platform = world.host.platform
    assert platform.workspaces is None
    assert (await platform.check_place(CHANNEL, "", workspace=f"{ONE}:{TEAM}")).member
    other.members[CHANNEL].remove(other.bot)
    assert not (await platform.check_place(CHANNEL, "", workspace=f"{TWO}:{TEAM}")).member
    assert (await platform.check_place(ONLY_ON_ONE, "", workspace=f"{ONE}:{TEAM}")).member
    assert not (await platform.check_place(ONLY_ON_ONE, "", workspace=f"{TWO}:{TEAM}")).exists
    await world.host.stop()


# ── the same ids on two servers ──────────────────────────────────────────


async def test_the_same_channel_user_and_post_ids_on_two_servers_are_two_conversations(two):
    world, (first, one), (second, other) = await two()
    world.kit.play(ModelTurn(final="one answered", hold=True), ModelTurn(final="two answered"))

    assert (await first.handle(_message("from one"))).startswith("dispatched:")
    await world.kit.holding()
    # The same channel, sender, team and post id on the other server: a turn of its own, not a duplicate.
    assert (await second.handle(_message("from two"))).startswith("dispatched:")
    await world.handed(2)
    assert world.requests[0].conversation != world.requests[1].conversation
    assert [request.metadata["mattermost_server"] for request in world.requests] == [ONE, TWO]
    # Control on the first server: the same post again is a duplicate.
    assert await first.handle(_message("from one")) == "ignored:already-handled (dedupe)"

    world.kit.release()
    await world.host.idle()

    assert _posts(one) == [(CHANNEL, "one answered")] and _posts(other) == [(CHANNEL, "two answered")]
    # The acknowledgement is added and removed on the server of its message.
    for rest in (one, other):
        assert [request.body["post_id"] for request in rest.calls("POST", "/reactions")] == [MESSAGE]
        assert len(rest.calls("DELETE", "/users/")) == 1


async def test_a_record_names_the_server_in_its_workspace(two):
    _world, (first, _one), (second, _other) = await two()
    seen = []

    async def keep(record):
        seen.append(record)
        return "kept"

    first._on_inbound = second._on_inbound = keep
    await first.handle(_message("in a team"))
    await second.handle(posted("n" * 26, channel=CHANNEL, user=ALICE, channel_type="D", team=""))

    assert [record.workspace for record in seen] == [f"{ONE}:{TEAM}", f"{TWO}:direct"]


# ── tools ────────────────────────────────────────────────────────────────


async def test_a_tool_call_acts_on_the_server_of_its_turn_and_on_no_other(two):
    world, (_first, one), (second, other) = await two()
    calls = (("react_to_message", {"emoji": "tada"}), ("post_message", {"text": "over there", "chat_id": OTHER}))
    world.kit.play(ModelTurn(final="done", calls=calls))
    started = len(one.requests)

    await second.handle(_message("post it over there"))
    await world.host.idle()

    assert json.loads(world.kit.results[1])["ok"] is True
    assert (OTHER, "over there") in _posts(other)
    assert "tada" in [request.body["emoji_name"] for request in other.calls("POST", "/reactions")]
    # The first server has the same channel ids, and was asked nothing.
    assert len(one.requests) == started


async def test_an_id_of_another_server_is_looked_up_on_the_turn_s_server_and_refused(two):
    world, (_first, one), (second, other) = await two()
    world.kit.play(ModelTurn(final="done", calls=(("post_message", {"text": "across", "chat_id": ONLY_ON_ONE}),)))
    started = len(one.requests)

    await second.handle(_message("post it on the other server"))
    await world.host.idle()

    refused = json.loads(world.kit.results[0])
    assert (refused["ok"], refused["error"]) == (False, "not_found")
    assert f"GET /channels/{ONLY_ON_ONE}" in _paths(other)
    assert ("across" not in [text for _, text in _posts(other)]) and len(one.requests) == started


async def test_a_tool_call_of_a_server_that_is_not_served_is_refused(two):
    world, (_first, one), (_second, other) = await two()
    metadata = {"mattermost_server": "three", "mattermost_channel_id": CHANNEL, "mattermost_user_id": ALICE}
    (tools,) = world.host.platform.toolkits(lambda: metadata)
    started = len(one.requests) + len(other.requests)

    refused = json.loads(await tools.post_message(text="anywhere"))

    assert refused["error"] == SERVER_UNKNOWN and "three" in refused["detail"]
    assert await tools.widening_question("post_message", {"chat_id": OTHER}) is None
    assert len(one.requests) + len(other.requests) == started


async def test_an_action_that_names_no_served_server_is_not_sent_anywhere(two):
    world, (_first, one), (_second, other) = await two()
    platform = world.host.platform

    for workspace in ("", TEAM, "three:" + TEAM):
        with pytest.raises(InstallationUnknown, match="nothing is sent"):
            await platform.post(CHANNEL, "", "to whom?", workspace=workspace)
    unknown = InboundMessage(platform="mattermost", workspace=TEAM, conversation=CHANNEL, message_id=MESSAGE, sender=ALICE, text="x")
    with pytest.raises(InstallationUnknown):
        await platform.mark(unknown, "received")
    assert _posts(one) == _posts(other) == []

    # Control: named, it goes out on that server alone.
    await platform.post(CHANNEL, "", "to two", workspace=f"{TWO}:{TEAM}")
    assert (_posts(one), _posts(other)) == ([], [(CHANNEL, "to two")])


# ── clicks and the slash command ─────────────────────────────────────────


async def _question(rest: FakeRest) -> str:
    """The id of the first post with buttons on a server, once there is one."""
    for _ in range(1000):
        if found := [key for key, post in rest.posts.items() if post["props"].get("attachments")]:
            return found[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no question was posted")


async def _click(member, rest: FakeRest, post_id: str, index: int = 0) -> dict:
    """Send a click as a server does: to the listener of the button's address, over HTTP."""
    context = rest.posts[post_id]["props"]["attachments"][0]["actions"][index]["integration"]["context"]
    body = {"user_id": ALICE, "user_name": "alice", "post_id": post_id, "channel_id": CHANNEL, "context": context}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{member.actions.port}") as listener:
        return (await listener.post("/action", json=body)).json()


async def test_a_click_reaches_the_listener_of_its_server_and_no_other(two):
    world, (first, one), (second, other) = await two(ASK_TO_REACT)
    world.kit.play(ModelTurn(final="done", calls=(("react_to_message", {"emoji": "tada"}),)))

    await second.handle(_message("react"))
    question = await _question(other)
    button = other.posts[question]["props"]["attachments"][0]["actions"][0]["integration"]
    assert button["url"] == "http://belle-two.invalid:18400/action"

    # The same button, sent to the first server's listener: its secret is another listener's.
    elsewhere = await _click(first, other, question)
    assert elsewhere == {"ephemeral_text": WITHDRAWN_QUESTION_NOTE} and "tada" not in [request.body["emoji_name"] for request in other.calls("POST", "/reactions")]

    answered = await _click(second, other, question)
    await world.host.idle()
    assert "update" in answered and "ephemeral_text" not in answered
    assert "tada" in [request.body["emoji_name"] for request in other.calls("POST", "/reactions")]
    assert "tada" not in [request.body["emoji_name"] for request in one.calls("POST", "/reactions")]


async def test_each_server_s_slash_command_takes_that_server_s_token_only(two, kit):
    _world, (first, _one), (second, _other) = await two()
    form = {"user_id": ALICE, "channel_id": CHANNEL, "command": "/belle", "text": "help"}

    async def say(member, token: str) -> str:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{member.actions.port}") as listener:
            answer = await listener.post("/command", data=form, headers={"Authorization": f"Token {token}"})
        return answer.json()["text"]

    assert await say(second, f"command-token-{ONE}") == commands.TOKEN_REFUSED
    assert (await say(second, f"command-token-{TWO}")).startswith("**Belle commands**")
    assert (await say(first, f"command-token-{ONE}")).startswith("**Belle commands**")


# ── start and failure ────────────────────────────────────────────────────


async def test_a_server_that_refuses_its_token_is_named_and_the_others_are_served(two, kit, caplog):
    kit.rests[TWO_URL].routes[("GET", "/users/me")] = (401, {"id": "api.context.session_expired.app_error"})

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        world, (_first, _one), (_second, _other) = await two()

    assert any(
        f"server {TWO} is NOT SERVED: Mattermost did not accept the bot token of {kit.token_env(TWO)}: token_rejected" in record.getMessage()
        for record in caplog.records
    )
    assert any(record.getMessage().startswith(f"mattermost serves 1 of 2 servers: {ONE}. Not served") for record in caplog.records)
    assert kit.token(TWO) not in caplog.text
    with pytest.raises(InstallationUnknown):
        await world.host.platform.post(CHANNEL, "", "x", workspace=f"{TWO}:{TEAM}")


async def test_the_start_fails_when_no_server_can_be_served(two, kit):
    for rest in kit.rests.values():
        rest.routes[("GET", "/users/me")] = (401, {"id": "api.context.session_expired.app_error"})

    with pytest.raises(BelleConfigError, match="no server of platforms.mattermost.servers can be served") as raised:
        await two()
    assert f"{ONE}: " in str(raised.value) and f"{TWO}: " in str(raised.value)


@pytest.fixture
def quick_retries(monkeypatch):
    monkeypatch.setattr(installations, "RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(installations, "RETRY_MAX_SECONDS", 0.01)


async def _until(condition) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


async def test_a_server_that_does_not_answer_is_connected_in_the_background(two, kit, caplog, quick_retries):
    kit.rests[TWO_URL].unreachable = True

    with caplog.at_level(logging.INFO, logger=LOGGER):
        world, _one, (_second, other) = await two()
        platform = world.host.platform

        assert set(platform.serving) == {ONE} and platform.connecting == {TWO}
        assert any(
            f"server {TWO} is NOT CONNECTED: Mattermost did not answer the bot's identity check: unreachable" in line
            for line in _messages(caplog)
        )
        assert any(line.startswith(f"mattermost serves 1 of 2 servers: {ONE}. Connecting: {TWO}") for line in _messages(caplog))
        # Until it connects, an action on that server is refused, and says why.
        with pytest.raises(InstallationConnecting, match=f"server {TWO} has not connected yet"):
            await platform.post(CHANNEL, "", "too early", workspace=f"{TWO}:{TEAM}")
        await _until(lambda: any(f"server {TWO} is still not connected" in line for line in _messages(caplog)))

        other.unreachable = False
        await _until(lambda: TWO in platform.serving)

    assert not platform.connecting
    assert any(f"server {TWO} is served now" in line for line in _messages(caplog))
    await platform.post(CHANNEL, "", "now", workspace=f"{TWO}:{TEAM}")
    assert _posts(other) == [(CHANNEL, "now")]


async def test_the_start_fails_when_no_server_answers(two, kit, quick_retries):
    for rest in kit.rests.values():
        rest.unreachable = True

    with pytest.raises(BelleConfigError, match="no server of platforms.mattermost.servers can be served") as raised:
        await two()
    assert f"{ONE}: Mattermost did not answer" in str(raised.value) and f"{TWO}: Mattermost did not answer" in str(raised.value)


async def test_a_server_that_refuses_its_token_once_it_answers_is_not_served(two, kit, caplog, quick_retries):
    kit.rests[TWO_URL].unreachable = True

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        world, _one, (_second, other) = await two()
        platform = world.host.platform
        other.routes[("GET", "/users/me")] = (401, {"id": "api.context.session_expired.app_error"})
        other.unreachable = False
        await _until(lambda: not platform.connecting)

    assert set(platform.serving) == {ONE}
    assert any(
        f"server {TWO} is NOT SERVED: Mattermost did not accept the bot token of {kit.token_env(TWO)}: token_rejected" in line
        for line in _messages(caplog)
    )
    with pytest.raises(InstallationUnknown) as refused:
        await platform.post(CHANNEL, "", "x", workspace=f"{TWO}:{TEAM}")
    assert not isinstance(refused.value, InstallationConnecting)


async def test_a_stop_ends_the_attempts_to_connect(two, kit, quick_retries):
    kit.rests[TWO_URL].unreachable = True
    world, _one, (_second, other) = await two()
    platform = world.host.platform

    await platform.stop()
    other.unreachable = False
    await asyncio.sleep(0.05)

    assert not platform.connecting and not platform.serving
    # A request the fake records is one that reached it: none was made after the stop.
    assert not other.requests


async def test_two_entries_of_one_bot_on_one_server_are_not_both_served(two, kit, caplog):
    kit.rests[TWO_URL].bot = kit.rests[ONE_URL].bot

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await two()

    assert any(f"server {TWO} is NOT SERVED: its bot is the bot of server {ONE}" in record.getMessage() for record in caplog.records)


async def test_two_entries_with_two_bots_on_one_server_are_not_both_served(two, kit, caplog):
    for rest in kit.rests.values():
        rest.client_config["DiagnosticId"] = "i" * 26

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await two()

    assert any(
        f"server {TWO} is NOT SERVED: its server has the installation id of server {ONE}" in record.getMessage()
        for record in caplog.records
    )
