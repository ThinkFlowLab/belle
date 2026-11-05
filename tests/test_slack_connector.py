"""The Slack connector, on a Slack that exists only in this process.

Every check runs against ``tests/slack_transport.py``: no connection is
opened, and the tokens are synthetic. What the connector shares with the
JiuwenSwarm channel (the toolkits, the marks, the question presenter) is
checked where it lives. These checks hold the connector to the platform port.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from conformance_kit import ModelTurn, as_file, scripted_second_brain, worlds
from scripted_backend import ScriptedBackend
from slack_kit import SlackKit
from slack_transport import APP_TOKEN, BOT_ID, BOT_TOKEN, BOT_USER, TEAM, FakeSlackWeb, FakeSocket, button, click_body

from belle_agent.backend import APPROVAL, Answer, Question
from belle_agent.click_notices import ANSWER_REFUSED_NOTICE, WITHDRAWN_QUESTION_NOTE
from belle_agent.config import BelleConfigError, load_config
from belle_agent.host import OUTCOME_INTERRUPTED, OUTCOME_POSTED_BY_TOOL, OUTCOME_POSTED_FINAL
from belle_agent.platforms.base import (
    MARK_DONE,
    MARK_INGEST_DECLINED,
    MARK_INGEST_EMPTY,
    MARK_INGEST_FAILED,
    MARK_INGESTED,
    MARK_INGESTING,
    MARK_QUEUED,
    MARK_RECEIVED,
    MARK_REFUSED,
    MARK_STARTED,
    MARK_STOPPED,
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_NONE,
    TRIGGER_REPLY,
    InboundAnswer,
    InboundMessage,
    PlaceCheck,
    PlatformUnreachable,
    create_platform,
)
from belle_agent.platforms.slack import connector
from belle_agent.platforms.slack.file_transfer import FILE_REASON_DOWNLOAD_FAILED, SlackAttachmentError
from belle_agent.platforms.slack.scope_policy import slack_required_scopes
from belle_agent.platforms.slack.socket_mode import SlackAppTokenRefused
from belle_agent.scopes import capabilities as scope_capabilities

ROOM = "C0ROOM0001"
OTHER_ROOM = "C0ROOM0002"
THIRD_ROOM = "C0ROOM0003"
DIRECT = "D0DIRECT01"
ASKER = "U0ASKER001"
BYSTANDER = "U0OTHER002"
TS = "1710000001.000100"
ROOT = "1710000000.000100"

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: slack}
    delivery: {mode: [all]}
"""


def _config(tmp_path, text: str = "platforms: {slack: {enforce_audience: true}}\n") -> dict:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


@pytest.fixture
def slack(monkeypatch) -> SlackKit:
    """The fake transport in place of Socket Mode, and synthetic tokens in the environment."""
    return SlackKit(monkeypatch)


@pytest_asyncio.fixture
async def connected(slack, tmp_path):
    """A connector with no host behind it: what it hands over is kept, and answered as dispatched."""
    registry = scope_capabilities.snapshot_registry()
    started = []

    async def connect(text: str = "platforms: {slack: {enforce_audience: true}}\n", outcome: str = "dispatched:test"):
        platform = create_platform(_config(tmp_path, text), "slack")
        received: list = []

        async def on_inbound(record) -> str:
            received.append(record)
            return outcome

        await platform.start(on_inbound)
        started.append(platform)
        return platform, slack.sockets[-1], received

    yield connect
    for platform in started:
        await platform.stop()
    scope_capabilities.restore_registry(registry)


@pytest_asyncio.fixture
async def build_world(slack, tmp_path):
    """A host on the connector, with the scripted backend behind it."""
    async with worlds(ScriptedBackend(), tmp_path, slack) as build:
        yield build


def _message(text: str = "hello", **fields) -> dict:
    return {"type": "message", "channel": ROOM, "channel_type": "channel", "user": ASKER, "text": text, "ts": TS, **fields}


async def test_schedule_command_uses_the_authenticated_slack_dm(connected, slack):
    platform, socket, _ = await connected()
    calls = []

    class Toolkit:
        async def invoke_at(self, name, arguments, *, at):
            calls.append((name, arguments, at))
            return json.dumps({"ok": True, "jobs": []})

    platform.bind(SimpleNamespace(
        schedule_toolkit=Toolkit(), admits=lambda user: True, serves=lambda channel: True,
        command_unavailable=lambda words: "",
    ))
    result = await socket.app.slash("/belle", "schedule list", user=ASKER, channel=DIRECT)
    assert result.acks == [{}]
    assert "No scheduled jobs" in result.texts[-1]
    name, arguments, at = calls[0]
    assert (name, arguments, at.platform, at.workspace, at.conversation, at.sender, at.chat_type) == (
        "schedule_list", {}, "slack", TEAM, DIRECT, ASKER, "im"
    )



@pytest.mark.parametrize(("text", "arguments"), [("sessions list", {}), ("sessions list c1", {"cursor": "c1"})])
async def test_sessions_list_takes_an_optional_cursor(connected, text, arguments):
    platform, socket, _ = await connected()
    calls = []

    class Toolkit:
        async def invoke_at(self, name, arguments, *, at):
            calls.append((name, arguments))
            return json.dumps({"ok": True, "sessions": [], "next_cursor": ""})

    platform.bind(SimpleNamespace(
        session_toolkit=Toolkit(), admits=lambda user: True, serves=lambda channel: True,
        command_unavailable=lambda words: "",
    ))
    result = await socket.app.slash("/belle", text, user=ASKER, channel=DIRECT)
    assert result.acks == [{}]
    assert "Sessions visible here" in result.texts[-1]
    assert calls == [("session_list", arguments)]


async def test_sessions_show_without_its_session_id_gets_the_usage(connected):
    platform, socket, _ = await connected()
    platform.bind(SimpleNamespace(admits=lambda user: True, serves=lambda channel: True, command_unavailable=lambda words: ""))
    result = await socket.app.slash("/belle", "sessions show", user=ASKER, channel=DIRECT)
    assert result.texts == ["Usage: `/belle sessions show <session id> [cursor]`"]

# ── the configuration ────────────────────────────────────────────────────


def test_the_tokens_fall_back_to_the_environment(slack, tmp_path):
    platform = create_platform(_config(tmp_path), "slack")

    assert platform.name == "slack"
    assert (platform._bot_token, platform._app_token) == (BOT_TOKEN, APP_TOKEN)


def test_config_tokens_take_precedence_independently(slack, tmp_path, monkeypatch):
    monkeypatch.setenv("BELLE_TEST_BOT_TOKEN", "xoxb-config")
    platform = create_platform(_config(tmp_path, "platforms: {slack: {enforce_audience: true, bot_token: '${BELLE_TEST_BOT_TOKEN}'}}\n"), "slack")
    assert (platform._bot_token, platform._app_token) == ("xoxb-config", APP_TOKEN)


@pytest.mark.parametrize(
    ("text", "unset", "message"),
    [
        ("platforms: {slack: {enforce_audience: true, bot_token: '${BELLE_TEST_BOT_TOKEN}'}}\n", "", "Belle config platforms.slack.bot_token does not hold a token"),
        ("platforms: {slack: {enforce_audience: true}}\n", "SLACK_BOT_TOKEN", "environment variable SLACK_BOT_TOKEN, which is not set"),
        ("platforms: {slack: {enforce_audience: true}}\n", "SLACK_APP_TOKEN", "environment variable SLACK_APP_TOKEN, which is not set"),
    ],
)
def test_a_configuration_that_cannot_connect_says_why_and_shows_no_token(slack, tmp_path, monkeypatch, text, unset, message):
    monkeypatch.setenv("BELLE_TEST_BOT_TOKEN", "xapp-wrong-kind")
    if unset:
        monkeypatch.delenv(unset)

    with pytest.raises(BelleConfigError, match=message) as raised:
        create_platform(_config(tmp_path, text), "slack")
    assert BOT_TOKEN not in str(raised.value) and APP_TOKEN not in str(raised.value)
    assert "xapp-wrong-kind" not in str(raised.value)


def test_a_token_in_the_wrong_variable_is_refused_without_being_shown(slack, tmp_path, monkeypatch):
    # The two tokens swapped: the commonest mistake, and one Slack would answer with an error that names neither.
    monkeypatch.setenv("SLACK_BOT_TOKEN", APP_TOKEN)

    with pytest.raises(BelleConfigError, match="SLACK_BOT_TOKEN does not hold a token that starts with xoxb-") as raised:
        create_platform(_config(tmp_path), "slack")
    assert APP_TOKEN not in str(raised.value)


def test_a_setting_of_the_jiuwenswarm_channel_that_does_nothing_here_is_named_at_start(slack, tmp_path, caplog):
    text = "platforms: {slack: {enforce_audience: true, enable_memory: true, optional_reply_mode: sentinel, reply_in_thread: none}}\n"

    with caplog.at_level(logging.WARNING, logger="belle_agent.platforms.slack.connector"):
        create_platform(_config(tmp_path, text), "slack")

    warned = [record.getMessage() for record in caplog.records]
    assert [line.split(" is set")[0] for line in warned] == [
        "Belle config platforms.slack.enable_memory",
        "Belle config platforms.slack.optional_reply_mode",
    ]


def test_the_capabilities_are_slacks(slack, tmp_path, caplog):
    from belle_agent.platforms.slack.scope_capabilities import slack_capabilities
    from belle_agent.scopes import compile_scopes, compose_section, register_channel

    full, mine = slack_capabilities(), create_platform(_config(tmp_path), "slack").capabilities
    registry = scope_capabilities.snapshot_registry()
    register_channel(mine)
    try:
        rules = [{"match": {"channel": "slack", "chat": ROOM}, "delivery": {"mode": ["url"], "events": {"reaction": "context"}}}]
        with caplog.at_level(logging.WARNING):
            scopes = compile_scopes(rules, channels_config={"slack": {}})
    finally:
        scope_capabilities.restore_registry(registry)

    # Every setting Slack declares is acted on here, so the scope loader has nothing to say of one.
    assert (mine.channel, mine.axes, mine.axis_values, mine.attended, mine.sections) == (
        full.channel, full.axes, full.axis_values, full.attended, full.sections,
    )
    assert not any("events" in record.getMessage() for record in caplog.records)
    assert compose_section(scopes, channel="slack", chat=ROOM, section="delivery") == {
        "mode": frozenset({"url"}),
        "events": {"reaction": "context"},
    }


# ── the connection ───────────────────────────────────────────────────────


async def test_start_learns_who_the_bot_is_and_connects_and_stop_disconnects(connected):
    platform, socket, _ = await connected()

    assert socket.connected and not socket.closed
    assert (platform._identity.bot_user_id, platform._identity.bot_id, platform._identity.team_id) == (BOT_USER, BOT_ID, TEAM)

    await platform.stop()
    assert socket.closed
    # Nothing is delivered once stopped.
    assert await socket.app.deliver(_message()) == "dropped:platform-not-started"


async def test_a_token_slack_refuses_stops_the_start_and_is_not_shown(slack, tmp_path, monkeypatch):
    monkeypatch.setattr(connector, "SlackSocket", lambda *a, **k: FakeSocket(*a, web=FakeSlackWeb({"auth_test": "invalid_auth"}), **k))
    platform = create_platform(_config(tmp_path), "slack")

    with pytest.raises(BelleConfigError, match="did not accept the bot token of SLACK_BOT_TOKEN.*invalid_auth") as raised:
        await platform.start(lambda record: None)
    assert BOT_TOKEN not in str(raised.value)


@pytest.mark.parametrize("failure", ["auth_test", "socket"])
async def test_a_slack_that_does_not_answer_fails_the_start_and_leaves_nothing_open(slack, tmp_path, monkeypatch, failure):
    sockets: list[FakeSocket] = []

    class Silent(FakeSocket):
        async def connect(self) -> None:
            raise PlatformUnreachable("the Slack socket did not open within 30 s")

    def socket(*args, **kwargs):
        web = FakeSlackWeb({"auth_test": "service_unavailable"}) if failure == "auth_test" else None
        sockets.append((Silent if failure == "socket" else FakeSocket)(*args, web=web, **kwargs))
        return sockets[-1]

    monkeypatch.setattr(connector, "SlackSocket", socket)
    platform = create_platform(_config(tmp_path), "slack")

    # One workspace alone that does not answer stops the start, so that nothing reachable is never mistaken for served.
    with pytest.raises(PlatformUnreachable, match="did not answer auth.test: service_unavailable|did not open within"):
        await asyncio.wait_for(platform.start(lambda record: None), 5)
    assert sockets[0].closed and not sockets[0].connected


async def test_an_app_token_slack_refuses_stops_the_start_and_is_not_shown(slack, tmp_path, monkeypatch):
    class Refused(FakeSocket):
        async def connect(self) -> None:
            raise SlackAppTokenRefused("invalid_auth")

    monkeypatch.setattr(connector, "SlackSocket", Refused)
    platform = create_platform(_config(tmp_path), "slack")

    with pytest.raises(BelleConfigError, match="did not accept the app token of SLACK_APP_TOKEN.*invalid_auth") as raised:
        await platform.start(lambda record: None)
    assert APP_TOKEN not in str(raised.value)


async def test_an_app_that_lacks_a_scope_it_cannot_serve_without_does_not_start(slack, tmp_path, monkeypatch):
    required = sorted(slack_required_scopes())
    granted = ",".join(required[1:])
    monkeypatch.setattr(connector, "SlackSocket", lambda *a, **k: FakeSocket(*a, web=FakeSlackWeb(scopes=granted), **k))

    with pytest.raises(BelleConfigError, match=f"lacks {required[0]}"):
        await create_platform(_config(tmp_path), "slack").start(lambda record: None)

    # Control: with every required scope granted the connector starts.
    monkeypatch.setattr(connector, "SlackSocket", lambda *a, **k: FakeSocket(*a, web=FakeSlackWeb(scopes=",".join(required)), **k))
    platform = create_platform(_config(tmp_path), "slack")
    await platform.start(lambda record: None)
    await platform.stop()


# ── what arrives ─────────────────────────────────────────────────────────


async def test_a_direct_message_becomes_a_record_with_the_fields_the_port_defines(connected):
    _, socket, received = await connected()

    await socket.app.deliver(
        {"type": "message", "channel": DIRECT, "channel_type": "im", "user": ASKER, "text": " hello ", "ts": TS},
        event_id="Ev0001",
    )

    assert received == [
        InboundMessage(
            platform="slack", workspace=TEAM, conversation=DIRECT, message_id=TS, sender=ASKER, text="hello",
            event_id="Ev0001", thread="", chat_type="im", trigger=TRIGGER_DM,
            # A direct message is answered beside it, and its turn is told the id its tools take.
            reply_thread="", envelope=f"[ts: {TS}]",
        )
    ]


async def test_a_channel_message_is_answered_in_a_thread_unless_the_setting_says_otherwise(connected):
    _, socket, received = await connected()
    _, flat_socket, flat = await connected("platforms: {slack: {enforce_audience: true, reply_in_thread: none}}\n")

    for app in (socket.app, flat_socket.app):
        await app.deliver(_message("at the top level"))
        await app.deliver(_message("inside a thread", ts="1710000002.000100", thread_ts=ROOT))

    top, inside = received
    assert (top.trigger, top.thread, top.reply_thread) == (TRIGGER_NONE, "", TS)
    assert (inside.thread, inside.reply_thread, inside.envelope) == (ROOT, ROOT, f"[ts: 1710000002.000100, thread_ts: {ROOT}]")
    # With threads off, a top-level message is answered at the top level. A reply in a thread stays in it.
    assert [(record.thread, record.reply_thread) for record in flat] == [("", ""), (ROOT, ROOT)]


async def test_a_mention_arrives_once_with_the_mention_taken_off(connected):
    _, socket, received = await connected()
    text = f"<@{BOT_USER}> what is the plan?"

    # Slack sends a mention as the message it is and as an app_mention. The second is the one taken.
    assert await socket.app.deliver(_message(text)) == "ignored:leading-bot-mention-handled-as-app_mention"
    await socket.app.deliver({"type": "app_mention", "channel": ROOM, "user": ASKER, "text": text, "ts": TS})

    (mention,) = received
    assert (mention.trigger, mention.text, mention.chat_type) == (TRIGGER_MENTION, "what is the plan?", "channel")


async def test_a_reply_in_a_thread_the_bot_started_is_a_reply_to_the_bot(connected):
    _, socket, received = await connected()

    await socket.app.deliver(_message("and then?", thread_ts=ROOT, parent_user_id=BOT_USER))
    await socket.app.deliver(_message("and you?", ts="1710000003.000100", thread_ts=ROOT, parent_user_id=BYSTANDER))

    assert [record.trigger for record in received] == [TRIGGER_REPLY, TRIGGER_NONE]


async def test_what_the_bot_and_other_apps_post_and_what_is_not_a_message_starts_nothing(connected):
    _, socket, received = await connected()

    assert await socket.app.deliver(_message(user=BOT_USER)) == "ignored:posted-by-this-bot"
    assert await socket.app.deliver(_message(bot_id="B0ANOTHERAPP")) == "ignored:posted-by-an-app"
    assert await socket.app.deliver(_message(subtype="channel_join")) == "ignored:subtype-not-user-content (channel_join)"
    assert received == []


async def test_a_notice_from_slack_in_a_direct_message_starts_no_turn(connected):
    _, socket, received = await connected()
    notice = {"channel": "D0SLACK001", "channel_type": "im", "user": "USLACK"}

    assert await socket.app.deliver(_message("You have been removed from #general by <@U1>", **notice)) == (
        "ignored:direct-message-from-slack"
    )
    assert received == []
    # Control: a person's direct message is handed over.
    await socket.app.deliver(_message("hello", channel="D0PERSON01", channel_type="im"))
    assert [record.sender for record in received] == [ASKER]


async def test_an_edit_is_handed_over_as_an_edit_and_the_host_decides(connected):
    _, socket, received = await connected()

    await socket.app.deliver(
        {
            "type": "message", "subtype": "message_changed", "channel": ROOM, "channel_type": "channel", "ts": "1710000009.000100",
            "message": {"user": ASKER, "text": "reworded", "ts": TS, "thread_ts": ROOT},
        }
    )

    (edit,) = received
    assert (edit.edited, edit.message_id, edit.sender, edit.thread, edit.text) == (True, TS, ASKER, ROOT, "reworded")


async def test_a_file_is_handed_over_by_reference_and_nothing_is_downloaded(connected, monkeypatch):
    _, socket, received = await connected()

    async def no_download(*args, **kwargs):
        raise AssertionError("a file was downloaded before the host asked for it")

    monkeypatch.setattr(connector, "fetch_attachment_bytes", no_download)

    await socket.app.deliver(_message("the notes", subtype="file_share", files=[{"id": "F0001", "name": "notes.md"}]))

    (shared,) = received
    assert [(entry["name"], entry["uri"]) for entry in shared.files] == [("notes.md", "slack-file:F0001")]


async def test_app_messages_are_admitted_only_where_the_setting_lists_the_app(connected):
    listed = "platforms: {slack: {enforce_audience: true, app_messages: listed, app_messages_from: [A0DEPLOYER]}}\n"
    _, socket, received = await connected(listed)
    posted = {"bot_id": "B0DEPLOYER", "app_id": "A0DEPLOYER"}

    assert await socket.app.deliver(_message("build passed", **posted)) == "dispatched:test"
    assert (await socket.app.deliver(_message("spam", bot_id="B0ANOTHERAPP", app_id="A0OTHER"))).startswith("ignored:app-not-in-")
    assert [record.text for record in received] == ["build passed"]


# ── the metadata and the toolkits ────────────────────────────────────────


async def test_the_request_metadata_is_what_the_slack_toolkits_read(connected):
    platform, socket, received = await connected()
    await socket.app.deliver(_message("inside a thread", thread_ts=ROOT), event_id="Ev0002")

    assert platform.request_metadata(received[0]) == {
        "user_id": ASKER,
        "slack_event_id": "Ev0002",
        "slack_team_id": TEAM,
        "slack_channel_id": ROOM,
        "slack_channel_type": "channel",
        "slack_user_id": ASKER,
        "slack_enforce_audience": True,
        "slack_message_ts": TS,
        "slack_thread_ts": ROOT,
        "message_ts": TS,
    }


def test_the_toolkits_are_the_existing_ones_with_nothing_wrapped_around_them(slack, tmp_path):
    from belle_agent.platforms.slack.post import SlackPostToolkit
    from belle_agent.platforms.slack.reactions import SlackReactionToolkit

    platform = create_platform(_config(tmp_path), "slack")
    kits = platform.toolkits(dict)

    assert [type(kit) for kit in kits[:2]] == [SlackPostToolkit, SlackReactionToolkit]
    assert {type(kit).__module__.rsplit(".", 1)[0] for kit in kits} == {"belle_agent.platforms.slack"}
    names = [definition["name"] for kit in kits for definition in kit.get_tool_definitions()]
    assert len(names) == len(set(names))
    assert platform.posting_tool == "post_message" and names[:4] == ["post_message", "edit_message", "delete_message", "react_to_message"]
    assert {"read_slack_conversation", "find_by_name", "pin_message", "write_bookmark", "read_slack_canvas", "read_slack_list"} <= set(names)
    # Search is on unless the operator switches it off, as on the JiuwenSwarm channel.
    assert "search_slack_workspace" in names
    quiet = create_platform(_config(tmp_path, "platforms: {slack: {enforce_audience: true, search_enabled: false}}\n"), "slack")
    assert "search_slack_workspace" not in [d["name"] for kit in quiet.toolkits(dict) for d in kit.get_tool_definitions()]


async def test_a_turn_posts_and_reacts_through_the_slack_toolkits(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    calls = (("react_to_message", {"emoji": "eyes"}), ("post_message", {"text": "on it", "reply_to_message_id": TS}))
    world.kit.play(ModelTurn(final="done", calls=calls))

    await world.platform.app.deliver(_message("please look"))
    await world.host.idle()

    assert world.platform.web.payload("reactions_add") == {"channel": ROOM, "timestamp": TS, "name": "eyes"}
    posted = world.platform.web.payload("chat_postMessage")
    assert (posted["channel"], posted["thread_ts"], posted["text"]) == (ROOM, TS, "on it")
    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]
    # The turn was told the id it passed back to thread its post.
    assert world.requests[0].text == f"please look\n\n[ts: {TS}]"


async def test_a_post_that_would_reach_new_readers_is_refused_where_nobody_can_be_asked(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    web = world.platform.web
    web.members.update({ROOM: [ASKER, BOT_USER], OTHER_ROOM: [ASKER, BYSTANDER, BOT_USER], THIRD_ROOM: [ASKER, BOT_USER]})
    elsewhere = (("post_message", {"text": "for other eyes", "chat_id": OTHER_ROOM}),)
    nearby = (("post_message", {"text": "for the same eyes", "chat_id": THIRD_ROOM}),)
    world.kit.play(ModelTurn(final="it was refused", calls=elsewhere), ModelTurn(final="done", calls=nearby))

    async def cannot_ask(conversation, thread, question, **where):
        raise connector.SlackDeliveryError("channel_not_found", channel_id=conversation)

    # Where the question can be shown it is asked: tests/test_slack_audience_question.py.
    world.host.platform.ask = cannot_ask
    await world.platform.app.deliver(_message("post it over there"))
    await world.host.idle()

    refused = json.loads(world.kit.results[0])
    assert refused["error"] == "approval_required" and BYSTANDER in refused["detail"]
    # A deny is an absent side effect: nothing reached the other conversation, and the reply owed went where it was asked.
    assert [payload["channel"] for payload in web.payloads("chat_postMessage")] == [ROOM]

    # Control: a conversation whose every reader can already read this one takes the same call.
    world.kit.results.clear()
    await world.platform.app.deliver(_message("post it next door", ts="1710000004.000100"))
    await world.host.idle()
    assert json.loads(world.kit.results[0])["ok"] is True
    # The post next door is not the reply this conversation is owed, so the final follows here.
    assert [(payload["channel"], payload["text"]) for payload in web.payloads("chat_postMessage")][1:] == [
        (THIRD_ROOM, "for the same eyes"), (ROOM, "done"),
    ]


# ── the marks ────────────────────────────────────────────────────────────


def _reactions(web: FakeSlackWeb) -> list[tuple[str, str, str]]:
    return [
        ("-" if method == "reactions_remove" else "+", payload["timestamp"], payload["name"])
        for method, payload in web.calls
        if method.startswith("reactions_")
    ]


async def test_a_message_is_marked_as_the_jiuwenswarm_channel_marks_it(connected):
    platform, socket, received = await connected("platforms: {slack: {enforce_audience: true, session_surface: assistant}}\n")
    await socket.app.deliver(_message("one"))
    await socket.app.deliver(_message("two", ts="1710000005.000100", thread_ts=TS))
    await socket.app.deliver(_message("three", ts="1710000006.000100", user=BYSTANDER))
    first, second, third = received

    for record, state in ((first, MARK_RECEIVED), (second, MARK_QUEUED), (first, MARK_DONE), (second, MARK_STARTED),
                          (second, MARK_STOPPED), (third, MARK_REFUSED)):
        await platform.mark(record, state)

    assert _reactions(socket.web) == [
        ("+", first.message_id, "eyes"),
        ("+", second.message_id, "eyes"),
        ("+", second.message_id, "hourglass_flowing_sand"),
        ("-", first.message_id, "eyes"),
        ("-", second.message_id, "hourglass_flowing_sand"),
        ("-", second.message_id, "eyes"),
        ("+", second.message_id, "black_square_for_stop"),
        ("+", third.message_id, "no_entry_sign"),
    ]
    # The thread says a turn is thinking while one runs, and stops saying so when it ends.
    statuses = [payload["status"] for method, payload in socket.web.calls if method == "assistant.threads.setStatus"]
    assert statuses[0] == "is thinking…" and statuses[-1] == ""


async def test_with_acknowledgements_off_no_message_is_marked(connected):
    platform, socket, received = await connected("platforms: {slack: {enforce_audience: true, acknowledge_mode: 'off'}}\n")
    await socket.app.deliver(_message("one"))

    for state in (MARK_RECEIVED, MARK_DONE, MARK_REFUSED):
        await platform.mark(received[0], state)

    assert _reactions(socket.web) == []


async def test_an_ingest_is_marked_with_the_configured_reactions(connected, caplog):
    text = "platforms: {slack: {enforce_audience: true, brain_react_success: books, brain_react_declined: disabled}}\n"
    with caplog.at_level(logging.WARNING, logger="belle_agent.platforms.slack.connector"):
        platform, socket, received = await connected(text)
    for number in range(4):
        await socket.app.deliver(_message("see this", ts=f"171000001{number}.000100"))
    endings = (MARK_INGESTED, MARK_INGEST_FAILED, MARK_INGEST_DECLINED, MARK_INGEST_EMPTY)

    for record, ending in zip(received, endings, strict=True):
        await platform.mark(record, MARK_INGESTING)
        await platform.mark(record, ending)

    ingested, failed, declined, empty = (record.message_id for record in received)
    assert _reactions(socket.web) == [
        ("+", ingested, "open_book"),
        ("-", ingested, "open_book"),
        ("+", ingested, "books"),
        ("+", failed, "open_book"),
        ("-", failed, "open_book"),
        ("+", failed, "warning"),
        ("+", declined, "open_book"),
        ("-", declined, "open_book"),
        ("+", empty, "open_book"),
        ("-", empty, "open_book"),
    ]
    assert not [record for record in caplog.records if "brain_react" in record.getMessage()]


async def test_a_turn_on_slack_leaves_the_marks_of_its_course(build_world):
    world = await build_world("platforms: {slack: {acknowledge_mode: reaction, session_surface: 'off'}}\n" + ANSWER_EVERYTHING)
    world.kit.play(ModelTurn(final="the answer"))

    await world.platform.app.deliver(_message("a question"))
    await world.host.idle()

    assert _reactions(world.platform.web) == [("+", TS, "eyes"), ("-", TS, "eyes")]
    # The answer is in the thread the connector's setting opens under the message.
    assert [(post.thread, post.text) for post in world.posts(ROOM)] == [(TS, "the answer")]


# ── questions and stops ──────────────────────────────────────────────────


APPROVE_RM = Question(id="perm-1", text="Allow rm to run?", options=("approve", "reject"), kind=APPROVAL, tool="rm")


def _click(socket, payload: dict, ts: str, *, value: str, user: str = ASKER):
    return socket.app.click(click_body(user=user, channel=ROOM, message_ts=ts, payload=payload), button(payload["blocks"], value=value))


async def test_an_approval_is_rendered_with_buttons_and_resolved_from_the_click(connected):
    platform, socket, received = await connected(outcome="answered session=s1")

    shown = await platform.ask(ROOM, ROOT, APPROVE_RM)

    payload = socket.web.payload("chat_postMessage")
    buttons = payload["blocks"][-1]["elements"]
    assert (payload["channel"], payload["thread_ts"], payload["text"]) == (ROOM, ROOT, "Allow rm to run?")
    assert [(b["text"]["text"], b.get("style")) for b in buttons] == [("Approve", "primary"), ("Reject", "danger")]

    assert await _click(socket, payload, shown, value="reject", user=BYSTANDER) == "answered session=s1"

    assert received == [
        InboundAnswer(platform="slack", workspace=TEAM, conversation=ROOM, question_id="perm-1", sender=BYSTANDER,
                      answer=Answer(values=("reject",), approved=False))
    ]
    # The buttons come off, and the message says who answered what.
    closed = socket.web.payload("chat_update")
    assert closed["ts"] == shown and not [block for block in closed["blocks"] if block["type"] == "actions"]
    assert f"Answered by <@{BYSTANDER}>: Reject" in closed["text"]


async def test_a_click_the_host_refuses_tells_the_clicker_and_leaves_the_question_open(connected):
    platform, socket, _ = await connected(outcome="refused:clicks.approve=user:U0LEAD")
    shown = await platform.ask(ROOM, "", APPROVE_RM)
    payload = socket.web.payload("chat_postMessage")

    await _click(socket, payload, shown, value="approve", user=BYSTANDER)

    told = socket.web.payload("chat_postEphemeral")
    assert (told["channel"], told["user"], told["text"]) == (ROOM, BYSTANDER, ANSWER_REFUSED_NOTICE)
    assert socket.web.payloads("chat_update") == []


async def test_a_click_on_a_question_that_no_longer_waits_takes_its_buttons_off(connected):
    platform, socket, _ = await connected(outcome="ignored:question-not-waiting")
    shown = await platform.ask(ROOM, "", APPROVE_RM)

    await _click(socket, socket.web.payload("chat_postMessage"), shown, value="approve")

    assert WITHDRAWN_QUESTION_NOTE in socket.web.payload("chat_update")["text"]


async def test_a_question_with_choices_is_answered_with_the_choice(connected):
    platform, socket, received = await connected(outcome="answered session=s1")
    shown = await platform.ask(ROOM, "", Question(id="form-1", text="Red or blue?", options=("Red", "Blue")))

    await _click(socket, socket.web.payload("chat_postMessage"), shown, value="Blue")

    assert received[0].answer == Answer(values=("Blue",), approved=None)


async def test_slacks_own_stop_button_stops_the_turn_that_runs_in_its_thread(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    world.kit.play(ModelTurn(final="never said", hold=True))
    app = world.platform.app
    stop = {"type": "agent_session_stopped", "channel": ROOM, "thread_ts": TS, "user": ASKER}

    await app.deliver(_message("a long task"))
    await world.kit.holding()

    # The button names the thread. The connector names the message whose turn runs there.
    assert (await app.deliver(stop)).startswith("stopped")
    world.kit.release()
    await world.host.idle()
    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert world.posts() == []
    # Control: with nothing running in the thread there is nothing to stop.
    assert await app.deliver(stop) == "ignored:no-turn-running"


# ── files and the second brain ───────────────────────────────────────────


async def test_a_file_that_cannot_be_read_is_left_out_and_its_sender_is_told(connected, monkeypatch):
    platform, socket, received = await connected()

    async def fetch(url, mimetype, *, bot_token, allowed_hosts):
        if "broken" in url:
            raise SlackAttachmentError("HTTP 500", FILE_REASON_DOWNLOAD_FAILED)
        return bytearray(b"# Notes\n")

    monkeypatch.setattr(connector, "fetch_attachment_bytes", fetch)
    files = [
        {"id": "F0001", "name": "notes.md", "mimetype": "text/markdown", "url_private": "https://files.slack.com/f/notes.md"},
        {"id": "F0002", "name": "broken.pdf", "mimetype": "application/pdf", "url_private": "https://files.slack.com/f/broken.pdf"},
    ]
    await socket.app.deliver(_message("two files", subtype="file_share", files=files))

    loaded = await platform.fetch_files(received[0])

    assert loaded == ({"name": "notes.md", "uri": "data:text/markdown;base64,IyBOb3Rlcwo="},)
    notice = socket.web.payload("chat_postMessage")
    assert (notice["channel"], notice["thread_ts"]) == (ROOM, TS)
    assert "broken.pdf" in notice["text"] and FILE_REASON_DOWNLOAD_FAILED in notice["text"]


async def test_public_in_the_ingest_channels_selects_the_public_channels_of_the_workspace(build_world, tmp_path):
    world = await build_world(
        f"second_brain: {{enabled: true, corpus_dir: {tmp_path / 'corpus'}, ingest: {{channels: [public]}}}}\n",
        second_brain=scripted_second_brain(),
    )
    shared = world.message("see this", conversation=ROOM, files=(as_file("kite-bridle.md"),))

    # The kit delivers a message as one in a public channel. Slack calls a private one a group.
    assert await world.send(shared) == "ingested:second-brain"
    marks = [("+", "open_book"), ("-", "open_book"), ("+", "brain")]
    assert _reactions(world.platform.web) == [(sign, shared.message_id, name) for sign, name in marks]
    private = {"type": "message", "channel": "G0PRIVATE1", "channel_type": "group", "user": ASKER, "text": "see https://example.test/doc", "ts": TS}
    assert await world.platform.app.deliver(private) == "ignored:no-trigger-matched"


# ── what the host posts ──────────────────────────────────────────────────


async def test_a_long_answer_is_posted_in_parts_and_a_notice_is_shown_to_one_person(connected):
    platform, socket, _ = await connected()
    long = "\n\n".join(f"Paragraph {number}. " + "word " * 900 for number in range(12))

    first = await platform.post(ROOM, ROOT, long)
    await platform.post(ROOM, ROOT, "only you", ephemeral_to=ASKER)

    parts = socket.web.payloads("chat_postMessage")
    assert len(parts) > 1 and {part["thread_ts"] for part in parts} == {ROOT}
    assert first == socket.web.sent[0][2]
    assert socket.web.payload("chat_postEphemeral")["user"] == ASKER


async def test_a_required_reply_is_posted_once_through_the_connector(build_world, caplog):
    world = await build_world()
    world.kit.play(ModelTurn(final="hello back"))

    with caplog.at_level(logging.DEBUG):
        outcome = await world.platform.app.deliver(
            {"type": "message", "channel": DIRECT, "channel_type": "im", "user": ASKER, "text": "hello", "ts": TS}
        )
        await world.host.idle()

    assert outcome.startswith("dispatched:dm")
    assert [(post.conversation, post.thread, post.text) for post in world.posts()] == [(DIRECT, "", "hello back")]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    # The log names the event and what became of it, and not what was said.
    line = next(record.getMessage() for record in caplog.records if "slack inbound message" in record.getMessage())
    assert f"channel={DIRECT}" in line and "outcome=dispatched:dm" in line and "hello" not in line
    # Nothing here logs as the JiuwenSwarm channel: the parts shared with it log under their own names, with this connector's tag.
    delivered = next(record for record in caplog.records if "delivered text" in record.getMessage())
    assert (delivered.name, delivered.getMessage().split(" ")[0]) == ("belle_agent.platforms.slack.reply_delivery", "[slack]")
    assert not [r for r in caplog.records if "jiuwenswarm" in r.name or "SlackChannel" in r.getMessage()]



async def test_content_attachment_uses_current_turn_file_gate(slack, tmp_path, monkeypatch):
    platform = create_platform(_config(tmp_path), "slack")
    metadata = {"slack_channel_id": ROOM, "slack_team_id": TEAM, "slack_enforce_audience": True}
    calls = []

    async def fetch(toolkit, file_id):
        calls.append((file_id, toolkit._runtime_metadata()))
        return {"mimetype": "application/pdf"}, b"pdf bytes"

    monkeypatch.setattr(connector.SlackInlineHistoryToolkit, "fetch_slack_file", fetch)
    assert await platform.resolve_content_attachment("F0FILE001", metadata) == {
        "document": "cGRmIGJ5dGVz", "document_mime_type": "application/pdf",
    }
    assert calls == [("F0FILE001", metadata)]


async def test_content_attachment_preserves_file_refusal(slack, tmp_path, monkeypatch):
    platform = create_platform(_config(tmp_path), "slack")

    async def fetch(toolkit, file_id):
        return {"error": "slack_file_audience_denied", "detail": "file is private"}, None

    monkeypatch.setattr(connector.SlackInlineHistoryToolkit, "fetch_slack_file", fetch)
    with pytest.raises(ValueError, match="slack_file_audience_denied: file is private"):
        await platform.resolve_content_attachment("F0FILE001", {"slack_channel_id": ROOM})


@pytest.mark.parametrize("ref", ["https://example.com/file", "/tmp/file", "F0FILE001"])
async def test_content_attachment_requires_authorized_turn_context(slack, tmp_path, ref):
    platform = create_platform(_config(tmp_path), "slack")
    with pytest.raises(ValueError):
        await platform.resolve_content_attachment(ref, {})


async def test_place_checks_use_slack_membership_and_thread(connected, slack):
    platform, socket, _ = await connected()
    web = socket.web
    web.conversations[ROOM] = {"is_member": True}
    web.conversations[OTHER_ROOM] = {"is_member": False}
    web.conversations[THIRD_ROOM] = {"is_member": True, "is_private": False}
    web.history[ROOM] = [{"ts": ROOT, "text": "root"}]

    assert platform.workspaces == frozenset({TEAM})
    assert await platform.check_place(ROOM, ROOT, workspace=TEAM) == PlaceCheck(True, True, chat_type="group")
    assert not (await platform.check_place(ROOM, TS, workspace=TEAM)).exists
    assert await platform.check_place(OTHER_ROOM, "", workspace=TEAM) == PlaceCheck(True, False, chat_type="group")
    assert (await platform.check_place(THIRD_ROOM, "", workspace=TEAM)).chat_type == "channel"
    assert not (await platform.check_place(ROOM, "", workspace="other")).exists


async def test_place_audience_and_direct_message(connected, slack):
    platform, socket, _ = await connected()
    web = socket.web
    web.conversations[ROOM] = {"is_member": True}
    web.conversations[OTHER_ROOM] = {"is_member": True}
    web.conversations[THIRD_ROOM] = {"is_member": True, "is_private": False}
    web.members.update({ROOM: [ASKER, BOT_USER], OTHER_ROOM: [ASKER, BYSTANDER, BOT_USER]})

    assert await platform.can_read(ASKER, ROOM, workspace=TEAM)
    assert not await platform.can_read(BYSTANDER, ROOM, workspace=TEAM)
    assert await platform.newcomers(ROOM, OTHER_ROOM, workspace=TEAM) == (BYSTANDER,)
    assert await platform.newcomers(ROOM, THIRD_ROOM, workspace=TEAM) is None
    assert platform.mention(ASKER) == f"<@{ASKER}>"
    assert await platform.open_direct_message(ASKER, workspace=TEAM) == f"D{ASKER}"
