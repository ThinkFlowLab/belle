"""What the decision gate is told of a Slack message, through the connector.

``tests/conformance/test_conformance_decisions.py`` holds when the gate is
asked and what becomes of its answers, on every platform. These checks hold
what is Slack's: the facts of a message as the JiuwenSwarm channel states
them, and the messages before it, read from the conversation's history.
"""

import asyncio
import logging

import pytest
import pytest_asyncio
from conformance_kit import worlds
from decision_standin import KEY, KEY_ENV, DecisionStandIn, logged
from scripted_backend import ScriptedBackend
from slack_kit import SlackKit
from slack_transport import BOT_USER, TEAM

from belle_agent.message_decisions import ADVISORY_PREAMBLE, RECENT_MESSAGE_DEPTH
from belle_agent.platforms.slack import connector
from belle_agent.platforms.slack.decision_questions import bot_identity
from belle_agent.platforms.slack.scope_policy import resolve_bot_name

ROOM = "C0ROOM0001"
DIRECT = "D0DIRECT01"
ASKER = "U0ASKER001"
OTHER = "U0OTHER002"
TS = "1710000005.000100"

AMBIENT = """
scopes:
  - match: {channel: slack}
    delivery: {mode: [all], reply: optional}
"""


@pytest.fixture
def decisions(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    endpoint = DecisionStandIn()
    yield endpoint
    endpoint.close()


@pytest_asyncio.fixture
async def build_world(monkeypatch, tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, SlackKit(monkeypatch)) as build:
        yield build


async def _asked(endpoint: DecisionStandIn, count: int) -> list[dict]:
    async with asyncio.timeout(10):
        while len(endpoint.log) < count:
            await asyncio.sleep(0.01)
    return endpoint.states()


def _settings(endpoint: DecisionStandIn, forward: bool) -> str:
    # The suite's kit writes its settings for ``loopback`` and puts this platform's name in their place.
    return endpoint.config(forward=forward) + AMBIENT.replace("slack", "loopback")


async def test_the_gate_is_told_of_a_message_what_the_jiuwenswarm_channel_tells_it(build_world, decisions):
    world = await build_world(_settings(decisions, forward=True))
    app = world.platform.app
    said = f"<@{BOT_USER}> can you check the deploy?"
    place = {"channel": ROOM, "user": ASKER, "ts": TS, "text": said, "files": [{"id": "F1"}, {"id": "F2"}], "attachments": [{"text": "a card"}]}

    # Slack sends a mention as the message it is, which states the kind of conversation, and as an app_mention.
    await app.deliver({"type": "message", "channel_type": "channel", **place}, event_id="Ev01")
    await app.deliver({"type": "app_mention", **place}, event_id="Ev02")
    await world.host.idle()

    (state,) = decisions.states()
    assert state["channel"] == ROOM
    # The bot by its name and its mention token, the sender as Slack writes a mention of them.
    assert state["bot_name"] == bot_identity(resolve_bot_name(""), BOT_USER)
    # The text as written: the leading mention is what says who the message is for.
    assert state["last_message"] == {
        "author": f"<@{ASKER}>", "text": said, "ts": TS, "thread_ts": "", "thread_position": "root",
        "file_count": 2, "attachment_count": 1,
    }
    assert state["conversation_kind"] == "public_channel"
    assert ADVISORY_PREAMBLE in world.requests[0].text


async def test_a_direct_message_is_named_as_one(build_world, decisions):
    world = await build_world(_settings(decisions, forward=False))

    await world.platform.app.deliver(
        {"type": "message", "channel": DIRECT, "channel_type": "im", "user": ASKER, "ts": TS, "text": "hello"}, event_id="Ev01"
    )
    (state,) = await _asked(decisions, 1)
    await world.host.idle()

    assert state["conversation_kind"] == "one_to_one_dm"


async def test_the_gate_reads_the_messages_before_the_one_it_is_asked_about(build_world, decisions):
    world = await build_world(_settings(decisions, forward=True))
    app, web = world.platform.app, world.platform.web
    # Newest first, as Slack answers. The message asked about, and a later one, are not part of what came before.
    web.history[ROOM] = [
        {"user": OTHER, "text": "later", "ts": "1710000009.000100"},
        {"user": ASKER, "text": "the one asked about", "ts": TS},
        {"user": BOT_USER, "text": "the deploy is green", "ts": "1710000004.000100"},
        {"user": OTHER, "text": "is the deploy green?", "ts": "1710000003.000100", "files": [{"id": "F1"}]},
    ]

    await app.deliver({"type": "message", "channel": ROOM, "channel_type": "channel", "user": ASKER, "ts": TS, "text": "the one asked about"}, event_id="Ev01")
    await world.host.idle()

    assert web.payload("conversations_history") == {"channel": ROOM, "limit": RECENT_MESSAGE_DEPTH, "latest": TS, "inclusive": False}
    (state,) = decisions.states()
    # Oldest first, the bot's own turns among them under its name.
    assert [(entry["author"], entry["text"]) for entry in state["recent_messages"]] == [
        (f"<@{OTHER}>", "is the deploy green?"),
        (resolve_bot_name(""), "the deploy is green"),
    ]
    assert state["recent_messages"][0]["file_count"] == 1


async def test_the_record_of_a_decision_names_the_message_as_slack_does_and_holds_no_text(build_world, decisions, caplog):
    world = await build_world(_settings(decisions, forward=False))

    with caplog.at_level(logging.INFO, logger="belle_agent.message_decisions"):
        await world.platform.app.deliver(
            {"type": "message", "channel": ROOM, "channel_type": "channel", "user": ASKER, "ts": TS, "text": "a private matter"},
            event_id="Ev01",
        )
        (record,) = await logged(caplog)

    assert {key: record[key] for key in ("team", "channel", "ts", "user", "chat_type", "is_dm")} == {
        "team": TEAM, "channel": ROOM, "ts": TS, "user": ASKER, "chat_type": "channel", "is_dm": False,
    }
    assert "private matter" not in caplog.text


async def test_with_no_decision_endpoint_the_connector_keeps_nothing_of_a_message(build_world, decisions):
    world = await build_world(AMBIENT.replace("slack", "loopback"))

    await world.platform.app.deliver(
        {"type": "message", "channel": ROOM, "channel_type": "channel", "user": ASKER, "ts": TS, "text": "hello"}, event_id="Ev01"
    )
    await world.host.idle()

    assert world.host.platform._decision_inputs == {} and decisions.log == []
    assert not [method for method, _ in world.platform.web.calls if method == "conversations_history"]


def test_the_two_decision_settings_are_no_longer_named_as_doing_nothing():
    assert not {"decision_forward", "decision_questions_file"} & connector.UNREAD_SETTINGS
