"""The stop button: Slack's second gesture, and who is allowed to use it.

Slack has one ambient action -- posting a message -- and the gateway already
reads that as "cancel the running turn". So "stop, this is wrong" and "here is
one more detail" arrive identically and are resolved identically, in favour of
the first, and there is nowhere to refuse either of them. A message cannot be
gated on who sent it without also gating the conversation.

The button is the second gesture. It means only "stop", and because it is a
click rather than a message it holds a principal the connector can check. What
it checks is two clauses:

* whoever started a turn may stop it -- an invariant floor, not a rule anybody
  wrote and not one any rule can take away; and
* failing that, the allow list, which is what layer 0 already gates.

The floor needs a *recorded* starter and an identified clicker. Where either is
missing the floor does not arise and the allow list decides alone -- which is a
different thing from a refusal, and the tests below hold that distinction: a
connector that cannot say who started a turn has not said that nobody may stop
it.

Nothing here changes the ungated path. An ordinary message into a running
session still cancels it with no check at all, and in a shared channel thread
that is still a second person's message cancelling the first person's turn.
"""

from belle_agent.platforms.slack import thread_status

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest
from belle_agent.platforms.slack.admission import SlackMessageOrigin, SlackTurnInitiator
from belle_agent.platforms.slack import streaming as slack_streaming
from belle_agent.platforms.slack import activity as slack_activity
from belle_agent.scopes import MID_TURN_CANCEL, MID_TURN_QUEUE

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_stream_turn
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.platforms.slack import reply_delivery
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)
from slack_log_capture import captured_logs

ASKER = "U0ASKER001"
BYSTANDER = "U0OTHER002"
OUTSIDER = "U0OUTSIDE3"
TEAM = "T0TESTTEAM"
ROOM = "C0CHANNEL1"
THREAD = "1710000001.000100"
SESSION = f"slack_{TEAM}_{ROOM}_{THREAD}"
REQUEST_ID = "req-7f3a"


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


class _RecordingSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        # Both of the above in the order they happened. A card that is deleted
        # and then posted again puts the newer message in ``posts`` while
        # ``updates`` still ends with the older one, so "the last thing written"
        # cannot be read off either list alone.
        self.writes: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.api_calls: list[tuple[str, dict[str, Any]]] = []
        self.ephemerals: list[dict[str, Any]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.ts_counter = 0

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        self.deleted.append((kwargs.get("channel", ""), kwargs.get("ts", "")))
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        self.api_calls.append((method, kwargs.get("data") or {}))
        return {"ok": True}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        self.writes.append(kwargs)
        self.ts_counter += 1
        return {"ts": f"1780000000.{self.ts_counter:06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        self.writes.append(kwargs)
        return {"ts": kwargs["ts"]}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, str]:
        self.ephemerals.append(kwargs)
        return {"ok": "true"}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.added.append(
            (kwargs.get("channel", ""), kwargs.get("timestamp", ""), kwargs.get("name", ""))
        )
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed.append(
            (kwargs.get("channel", ""), kwargs.get("timestamp", ""), kwargs.get("name", ""))
        )
        return {"ok": True}


class _Ack:
    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _channel(
    *, mid_turn: str = MID_TURN_CANCEL, **config: Any
) -> tuple[SlackChannel, _RecordingSlackClient, list[Message]]:
    config.setdefault("activity_card_delay_seconds", 0.0)
    config.setdefault("activity_card_min_edit_seconds", 0.0)
    client = _RecordingSlackClient()
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id=ROOM, **config),
        RobotMessageRouter(),
    )
    # Set, not inherited. Everything in this file about a message superseding a
    # running turn needs the conversation to be cancelling, and cancel stopped
    # being what an unconfigured conversation does when the default became
    # queue. Patched at the seam rather than written as a scope so that these
    # tests keep testing the cancel path and not the settling cascade, which
    # test_slack_scopes covers.
    channel._mid_turn_mode = lambda *_a, **_kw: mid_turn  # type: ignore[assignment]
    channel._client = client
    channel._running = True
    dispatched: list[Message] = []
    channel.on_message(dispatched.append)
    return channel, client, dispatched


def _event(
    event_type: EventType,
    payload: dict[str, Any],
    *,
    request_id: str = REQUEST_ID,
    session_id: str = SESSION,
) -> Message:
    return Message(
        id=request_id,
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_type.value, **payload},
        event_type=event_type,
        metadata={"slack_channel_id": ROOM},
    )


def _tool_call(call_id: str = "call-1", **kw: Any) -> Message:
    """One ordinary tool call, which is what opens a turn's activity record."""
    return _event(
        EventType.CHAT_TOOL_CALL,
        {
            "tool_call": {
                "name": "bash_tool",
                "arguments": {"command": "ls"},
                "tool_call_id": call_id,
            }
        },
        **kw,
    )


def _final(**kw: Any) -> Message:
    return _event(EventType.CHAT_FINAL, {"content": "Done."}, **kw)


async def _settle() -> None:
    """Let the delayed-post task run. The delay is zero in these tests."""
    for _ in range(4):
        await asyncio.sleep(0)


async def _card(channel: SlackChannel, client: _RecordingSlackClient) -> None:
    """Put a card on screen for a turn that is running."""
    await channel.send(_tool_call())
    await _settle()
    assert client.posts, "the card should have been posted"


def _last_write(client: _RecordingSlackClient) -> dict[str, Any]:
    return client.writes[-1]


def _stop_button(call: dict[str, Any]) -> dict[str, Any] | None:
    for block in call.get("blocks") or []:
        if block.get("type") != "actions":
            continue
        for element in block.get("elements") or []:
            if element.get("action_id") == slack_connect._STOP_ACTION_ID:
                return element
    return None


def _click(
    *,
    user_id: str,
    value: str | None = None,
    channel_id: str = ROOM,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A ``block_actions`` payload for the stop button, as Slack sends one."""
    if value is None:
        value = json.dumps(
            {
                "session_id": SESSION,
                "request_id": REQUEST_ID,
                "channel_id": channel_id,
            },
            separators=(",", ":"),
        )
    action = {
        "type": "button",
        "action_id": slack_connect._STOP_ACTION_ID,
        "value": value,
        "text": {"type": "plain_text", "text": "Stop"},
    }
    body: dict[str, Any] = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "channel": {"id": channel_id},
        "container": {
            "type": "message",
            "channel_id": channel_id,
            "message_ts": "1780000000.000001",
        },
        "message": {"ts": "1780000000.000001", "blocks": []},
        "actions": [action],
    }
    if user_id:
        body["user"] = {"id": user_id}
    return body, action


def _cancels(dispatched: list[Message]) -> list[Message]:
    return [msg for msg in dispatched if msg.req_method == ReqMethod.CHAT_CANCEL]


# ── The gesture ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_running_turn_gets_a_stop_button() -> None:
    """The card is the per-turn surface, so the control lives on it."""
    channel, client, _dispatched = _channel()

    await _card(channel, client)

    button = _stop_button(client.posts[0])
    assert button is not None
    assert button["text"]["text"] == "Stop"
    # Red, because it destroys work.
    assert button["style"] == "danger"


@pytest.mark.asyncio
async def test_the_button_names_the_session_it_would_cancel() -> None:
    channel, client, _dispatched = _channel()

    await _card(channel, client)

    button = _stop_button(client.posts[0])
    assert button is not None
    assert json.loads(button["value"]) == {
        "session_id": SESSION,
        "request_id": REQUEST_ID,
        "channel_id": ROOM,
    }


@pytest.mark.asyncio
async def test_the_button_goes_when_the_turn_ends() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    assert _stop_button(client.posts[0]) is not None

    await channel.send(_final())
    await _settle()

    assert client.updates, "the card is rewritten when the turn ends"
    assert _stop_button(_last_write(client)) is None


@pytest.mark.asyncio
async def test_a_card_carrying_a_control_is_not_treated_as_chrome() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    key = (REQUEST_ID, ROOM)
    record = channel._activity_records[key]
    assert channel._activity_block_kind(record) == (
        slack_connect.slack_blocks.BLOCK_KIND_INTERACTIVE
    )

    record.closed = True
    assert channel._activity_block_kind(record) == (
        slack_connect.slack_blocks.BLOCK_KIND_CHROME
    )


# ── The floor: whoever started a turn may stop it ────────────────────────────


@pytest.mark.asyncio
async def test_the_starter_may_stop_their_own_turn() -> None:
    channel, client, dispatched = _channel(allow_from=[BYSTANDER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    (cancel,) = _cancels(dispatched)
    assert cancel.session_id == SESSION
    assert cancel.params["intent"] == "cancel"


@pytest.mark.asyncio
async def test_a_stop_reaches_the_runtime_as_chat_interrupt() -> None:
    """The same request Web, CLI, TUI and ACP send; only this half was missing."""
    channel, client, dispatched = _channel()
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    (cancel,) = _cancels(dispatched)
    assert cancel.req_method.value == "chat.interrupt"
    assert cancel.type == "req"
    # Answered once, not streamed.
    assert cancel.is_stream is False
    assert cancel.user_id == ASKER
    assert cancel.chat_id == ROOM
    assert cancel.metadata["slack_channel_id"] == ROOM
    assert cancel.metadata["slack_team_id"] == TEAM


@pytest.mark.asyncio
async def test_the_starter_is_permitted_even_after_the_card_says_nothing() -> None:
    channel, client, dispatched = _channel(allow_from=[BYSTANDER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")
    channel._activity_records[(REQUEST_ID, ROOM)].tool_runs.clear()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_someone_else_is_refused_where_the_allow_list_refuses_them() -> None:
    """The floor is the starter's alone. Everyone else is layer 0's business."""
    channel, client, dispatched = _channel(allow_from=[ASKER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []
    # Told to the person who clicked, and to nobody else: for everyone else in
    # the channel nothing happened.
    (note,) = client.ephemerals
    assert note["user"] == OUTSIDER
    assert note["text"] == slack_connect._STOP_REFUSED_NOTICE
    # And the button is still there, because the turn is still running.
    assert _stop_button(_last_write(client)) is not None


# ── The allowance needs a recorded starter ──────────────────────────────────


@pytest.mark.asyncio
async def test_with_no_recorded_initiator_the_allow_list_decides_alone() -> None:
    channel, client, dispatched = _channel(allow_from=[BYSTANDER])
    await _card(channel, client)
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)
    assert _cancels(dispatched) == []

    body, action = _click(user_id=BYSTANDER)
    await channel._handle_stop_action(_Ack(), body, action)
    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_no_recorded_initiator_is_not_read_as_a_refusal() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1
    assert client.ephemerals[-1]["text"] == slack_connect._STOP_ACCEPTED_NOTICE


@pytest.mark.asyncio
async def test_an_aged_out_entry_does_not_deny_the_person_it_named() -> None:
    channel, client, dispatched = _channel(allow_from=[ASKER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")
    entry = channel._admission.turn_initiators[SESSION]
    entry.started_at -= slack_connect._TURN_INITIATOR_TIMEOUT_SECONDS + 1
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_an_unnamed_clicker_cannot_be_the_starter() -> None:
    channel, client, dispatched = _channel(allow_from=[ASKER])
    await _card(channel, client)
    channel._admission.turn_initiators[SESSION] = SlackTurnInitiator(
        user_id="", request_id=REQUEST_ID, is_dm=False, chat_type="channel"
    )
    assert channel.turn_initiator(SESSION) is not None

    body, action = _click(user_id="")
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


@pytest.mark.asyncio
async def test_an_unnamed_clicker_is_refused_by_the_allow_list_not_skipped() -> None:
    channel, client, dispatched = _channel(allow_from=[ASKER, BYSTANDER])
    await _card(channel, client)

    body, action = _click(user_id="")
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []
    assert channel._activity_records[(REQUEST_ID, ROOM)].stop_requested is False


# ── One stop per turn ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_second_click_stops_nothing_more() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1
    assert client.ephemerals[-1]["text"] == slack_connect._STOP_STALE_NOTICE


@pytest.mark.asyncio
async def test_the_button_goes_with_the_card_once_a_stop_is_dispatched() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert client.deleted, "the card goes, and its button with it"
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_stop_on_a_turn_that_has_ended_is_refused() -> None:
    """A card whose rewrite never landed still must not stop the next turn."""
    channel, client, dispatched = _channel()
    await _card(channel, client)
    await channel.send(_final())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []
    assert client.ephemerals[-1]["text"] == slack_connect._STOP_STALE_NOTICE


@pytest.mark.asyncio
async def test_a_stop_naming_a_turn_this_process_never_tracked_is_refused() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)
    channel._activity_records.clear()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


# ── Payloads this connector did not post ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_button_value_that_does_not_decode_stops_nothing() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)

    for value in ("", "not json", "[]", json.dumps({"session_id": SESSION})):
        body, action = _click(user_id=ASKER, value=value)
        await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


@pytest.mark.asyncio
async def test_a_stopped_channel_acts_on_nothing() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)
    channel._running = False

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


# ── Availability ─────────────────────────────────────────────────────────────


@pytest.fixture
def logged(monkeypatch) -> dict[str, list[str]]:
    """Everything this module logs, by level.

    The connector's logger does not propagate, so ``caplog`` sees nothing; the
    idiom the other Slack tests use is to record the calls themselves.
    """
    recorded: dict[str, list[str]] = {"info": [], "warning": []}

    def record(level: str):
        def sink(message: str, *args: Any, **kwargs: Any) -> None:
            recorded[level].append(message % args if args else message)

        return sink

    monkeypatch.setattr(slack_connect.logger, "info", record("info"))
    monkeypatch.setattr(slack_connect.logger, "warning", record("warning"))
    monkeypatch.setattr(slack_activity.logger, "info", record("info"))
    monkeypatch.setattr(slack_activity.logger, "warning", record("warning"))
    return recorded


def test_startup_says_the_stop_button_is_inactive_when_the_card_is_off(
    logged: dict[str, list[str]],
) -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, activity_card=False), RobotMessageRouter()
    )

    channel._log_stop_button_availability()

    (line,) = logged["warning"]
    assert "stop button inactive" in line
    assert "activity_card" in line


def test_startup_says_the_stop_button_is_active_when_the_card_is_on(
    logged: dict[str, list[str]],
) -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, activity_card=True), RobotMessageRouter()
    )

    channel._log_stop_button_availability()

    assert logged["warning"] == []
    assert any("stop button active" in line for line in logged["info"])


# ── A turn that calls no tool ────────────────────────────────────────────────
#
# The card used to open on the turn's first tool call, so a turn that only wrote
# had no button, and Slack's own stop on the status line works only for an app
# with the agent surface. A turn owed a reply now gets its card when it starts.

DM = "D0DIRECT01"
DM_TS = "1710000005.000100"


async def _direct_message(channel: SlackChannel, dispatched: list[Message]) -> Message:
    await channel._handle_message_event(
        {"type": "message", "channel_type": "im", "channel": DM, "user": ASKER, "text": "think about it", "ts": DM_TS},
        {"event_id": "Ev-dm", "team_id": TEAM},
    )
    await _settle()
    (request,) = [msg for msg in dispatched if msg.req_method == ReqMethod.CHAT_SEND]
    return request


@pytest.mark.asyncio
async def test_a_turn_that_calls_no_tool_gets_a_card_with_only_its_stop_and_the_card_goes_at_the_end() -> None:
    channel, client, dispatched = _channel(allow_from=[ASKER])
    request = await _direct_message(channel, dispatched)

    (card,) = client.posts
    assert [block["type"] for block in card["blocks"]] == ["section", "actions"]
    button = _stop_button(card)
    assert button is not None
    assert json.loads(button["value"]) == {"session_id": request.session_id, "request_id": request.id, "channel_id": DM}

    await channel.send(Message(
        id=request.id, type="event", channel_id="slack", session_id=request.session_id, params={},
        timestamp=time.time(), ok=True, payload={"event_type": EventType.CHAT_FINAL.value, "content": "Done."},
        event_type=EventType.CHAT_FINAL, metadata={"slack_channel_id": DM},
    ))
    await _settle()

    assert client.deleted == [(DM, "1780000000.000001")]


@pytest.mark.asyncio
async def test_the_stop_on_a_card_of_a_turn_that_called_no_tool_stops_it() -> None:
    channel, client, dispatched = _channel(allow_from=[ASKER])
    request = await _direct_message(channel, dispatched)
    (card,) = client.posts
    button = _stop_button(card)
    assert button is not None

    body, action = _click(user_id=ASKER, value=button["value"], channel_id=DM)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    (cancel,) = _cancels(dispatched)
    assert cancel.session_id == request.session_id


@pytest.mark.asyncio
async def test_a_turn_whose_reply_is_optional_gets_no_card_before_it_calls_a_tool() -> None:
    channel, client, _dispatched = _channel()
    origin = SlackMessageOrigin(channel_id=ROOM, message_ts=THREAD, reply_thread_ts=THREAD, root_thread_ts=THREAD)
    channel._remember_turn_initiator(
        SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel", origin=origin,
        request_metadata={slack_connect.REPLY_OPTIONAL_KEY: True},
    )

    await channel._begin_activity_card(SESSION)
    await _settle()

    assert client.posts == []
    # Control: the same turn owed a reply gets its card.
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel", origin=origin)
    await channel._begin_activity_card(SESSION)
    await _settle()
    assert [block["type"] for block in client.posts[0]["blocks"]] == ["section", "actions"]


# ── The card a stop leaves behind, which is none ─────────────────────────────
#
# Every other ending on the card arrives as an event. A stop arrives as nothing:
# the cancel reaches ``_cancel_agent_work_for_session``, which cancels the
# gateway's own ``process_stream`` task, and that coroutine catches its
# ``CancelledError`` and returns without publishing a terminal. Its ``finally``
# emits a ``chat.processing_status``, which settles nothing, and the
# ``chat.interrupt_result`` that follows renders to no text. So the connector
# hears nothing further about the turn at all.
#
# That is the first thing these tests hold: the card is settled on the strength
# of the click, and no test below sends a terminal event before asserting that
# it was. A card that waits for one says "Working..." until somebody scrolls
# past it.
#
# The second is what settling it means. The card is deleted, and it is the third
# reason a card is deleted rather than a fourth wording it can be rewritten
# with. The reader already knows: the message that started the turn carries the
# stopped reaction, a streamed reply is marked ``_STREAM_STOPPED_TAIL``, and the
# person who pressed the button was told by an ephemeral. A card whose whole job
# is to report a turn that is running has nothing to add about one that is not.
#
# A failed turn is the exception and is the one card a stop cannot take down.
# The card holds the only copy of the harness error, because the message that
# would have reported it is suppressed on the strength of the card showing it.


def _plan(call: dict[str, Any]) -> dict[str, Any] | None:
    for block in call.get("blocks") or []:
        if block.get("type") == "plan":
            return block
    return None


def _title(client: _RecordingSlackClient) -> str:
    """The title of the last card written, whatever was written after it.

    The last write is not the last card. A turn's own reply is posted after the
    card it settles, and a card that was deleted and posted again leaves an
    older rewrite behind it in the fake's log, so the card has to be looked for
    rather than assumed to be on the end.
    """
    for call in reversed(client.writes):
        plan = _plan(call)
        if plan is not None:
            return str(plan["title"])
    raise AssertionError("no card was written")


def _tool_result(call_id: str = "call-1", **kw: Any) -> Message:
    """The ordinary tool result, so a run is not left in flight."""
    return _event(
        EventType.CHAT_TOOL_RESULT,
        {"tool_name": "bash_tool", "tool_call_id": call_id, "result": "ok"},
        **kw,
    )


@pytest.mark.asyncio
async def test_a_stopped_turn_settles_its_card_with_no_terminal_event() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()
    written_before_the_stop = len(client.updates)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True
    assert client.deleted, "the card must be taken down by the stop itself"
    assert len(client.updates) == written_before_the_stop, "taken down, not rewritten"


@pytest.mark.asyncio
async def test_a_stopped_turn_leaves_no_card_at_all() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()
    posted_ts = channel._activity_records[(REQUEST_ID, ROOM)].message_ts
    assert posted_ts
    written_before_the_stop = len(client.updates)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert client.deleted == [(ROOM, posted_ts)]
    assert len(client.updates) == written_before_the_stop


@pytest.mark.asyncio
async def test_a_stopped_card_is_deleted_for_that_reason_by_name(
    logged: dict[str, list[str]],
) -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert client.deleted
    assert any(
        "activity card deleted (stopped)" in line for line in logged["info"]
    )


def test_the_stopped_mark_on_a_reply_is_ascii() -> None:
    """One non-ASCII character in a text element has taken a message down."""
    slack_connect._STREAM_STOPPED_TAIL.encode("ascii")


def test_no_card_title_says_a_turn_was_stopped() -> None:
    record = slack_connect._SlackActivityRecord(
        request_id=REQUEST_ID, channel_id=ROOM, thread_ts=THREAD, session_id=SESSION
    )
    record.settle(stopped=True)

    assert record.stopped is True
    assert slack_connect.SlackChannel._activity_title(record).startswith(
        slack_connect._ACTIVITY_TITLE_DONE
    )


@pytest.mark.asyncio
async def test_the_stopped_title_names_nobody() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=BYSTANDER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    written = json.dumps(_last_write(client))
    assert ASKER not in written
    assert BYSTANDER not in written
    # The one place the person who acted is named is addressed to them alone.
    assert client.ephemerals[-1]["user"] == BYSTANDER


@pytest.mark.asyncio
async def test_a_run_still_in_flight_at_the_stop_did_not_fail() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert client.deleted
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.tool_totals() == {slack_activity._RUN_UNREPORTED: 1}
    assert record.tool_totals().get(slack_connect._RUN_FAILED, 0) == 0


@pytest.mark.asyncio
async def test_a_late_terminal_after_a_stop_posts_no_second_card() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    posted = len([post for post in client.posts if post.get("blocks")])
    written = len(client.updates)
    assert len(client.deleted) == 1

    await channel.send(_final())
    await _settle()

    assert len(client.updates) == written
    # The answer itself is still posted. What must not come back is a card: the
    # stop deleted one, and a terminal that put another in its place would be
    # reporting a turn nobody is waiting on.
    assert len([post for post in client.posts if post.get("blocks")]) == posted
    assert len(client.deleted) == 1
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True


@pytest.mark.asyncio
async def test_settling_a_stopped_card_twice_deletes_once() -> None:
    """Idempotent from this side too, not only from the terminal's."""
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    assert len(client.deleted) == 1

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    await channel._settle_stopped_card(record)

    assert len(client.deleted) == 1
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_request_that_resumes_after_its_stop_is_not_still_stopped() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    assert len(client.deleted) == 1

    await channel.send(_tool_call(call_id="call-2"))
    await _settle()
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert record.stopped is False
    assert record.stop_requested is True
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING
    assert _stop_button(_last_write(client)) is None

    await channel.send(_tool_result(call_id="call-2"))
    await channel.send(_final())
    await _settle()

    assert _title(client).startswith(slack_connect._ACTIVITY_TITLE_DONE)


@pytest.mark.asyncio
async def test_a_stopped_card_carries_no_harness_error() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.failed is False
    assert record.harness_error == ""
    plan = _plan(_last_write(client))
    assert plan is not None
    titles = [str(card.get("title") or "") for card in plan["tasks"]]
    assert slack_connect._ERROR_CARD_TITLE not in titles


# ── The stream a stop leaves open ────────────────────────────────────────────
#
# The card was one half. The other is the message the reply was being written
# into. ``_close_stream`` is reached from ``send()`` and from nowhere else, so
# the same missing terminal that left the card captioned "Working..." leaves
# the streamed message in streaming state, holding whatever the turn had
# written when the cancel landed.
#
# Nothing else calls ``chat.stopStream``, so the stream stays open until the
# process restarts. These tests send no terminal event either, and assert on
# the stop call the click itself makes.


class _StreamingStopClient(_RecordingSlackClient):
    """The recording client, plus the three streaming methods."""

    def __init__(self, *, stop_error: Exception | None = None) -> None:
        super().__init__()
        self.starts: list[dict[str, Any]] = []
        self.appends: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self._stop_error = stop_error

    async def chat_startStream(self, **kwargs: Any) -> dict[str, str]:
        self.starts.append(kwargs)
        return {"ts": "1780000000.777777"}

    async def chat_appendStream(self, **kwargs: Any) -> dict[str, str]:
        self.appends.append(kwargs)
        return {"ok": "true"}

    async def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
        if self._stop_error is not None:
            raise self._stop_error
        self.stops.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}


@pytest.fixture
def _no_stream_debounce(monkeypatch) -> None:
    """Flush every append on the next loop pass rather than a second later."""
    monkeypatch.setattr(slack_stream_turn, "_STREAM_APPEND_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_streaming, "_STREAM_APPEND_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(slack_stream_turn, "_STREAM_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_streaming, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 0.0)


def _streaming_channel(
    **config: Any,
) -> tuple[SlackChannel, _StreamingStopClient, list[Message]]:
    """A channel that streams, with the recipient a channel stream needs."""
    client = _StreamingStopClient(**config.pop("client", {}))
    channel, _old, dispatched = _channel(enable_streaming=True, **config)
    channel._client = client
    channel._remember_stream_recipient(SESSION, ASKER, TEAM)
    return channel, client, dispatched


def _delta(text: str, **kw: Any) -> Message:
    """One fragment of the reply, in the thread a stream has to live in."""
    msg = _event(EventType.CHAT_DELTA, {"content": text}, **kw)
    msg.metadata = {"slack_channel_id": ROOM, "slack_thread_ts": THREAD}
    return msg


def _threaded_tool_call(**kw: Any) -> Message:
    msg = _tool_call(**kw)
    msg.metadata = {"slack_channel_id": ROOM, "slack_thread_ts": THREAD}
    return msg


def _threaded_final(**kw: Any) -> Message:
    """The terminal, addressed to the same thread the stream was opened in.

    ``_close_stream`` keys on the thread as well as the request, so a terminal
    that resolved to a different delivery would not find the stream at all --
    which is why the stop path matches on the request and the channel and
    leaves the thread out of it.
    """
    msg = _final(**kw)
    msg.metadata = {"slack_channel_id": ROOM, "slack_thread_ts": THREAD}
    return msg


async def _streaming_card(
    channel: SlackChannel, client: _StreamingStopClient
) -> None:
    """A card on screen and a stream open, which is the production shape."""
    await channel.send(_threaded_tool_call())
    await _settle()
    await channel.send(_delta("The answer begins here.\n"))
    await _settle()
    assert client.starts, "the reply should have opened a stream"


@pytest.mark.asyncio
async def test_a_stop_closes_the_open_stream_with_no_terminal_event(
    _no_stream_debounce,
) -> None:
    channel, client, dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    assert client.stops == []

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert len(client.stops) == 1
    assert client.stops[0]["channel"] == ROOM
    assert client.stops[0]["ts"] == "1780000000.777777"


@pytest.mark.asyncio
async def test_a_stopped_stream_leaves_no_entry_behind(
    _no_stream_debounce,
) -> None:
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    assert channel._streams, "the stream should be tracked while it is open"

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert channel._streams == {}


@pytest.mark.asyncio
async def test_the_partial_text_stays_and_is_marked_as_stopped(
    _no_stream_debounce,
) -> None:
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    streamed = "".join(
        str(chunk.get("text") or "")
        for call in [*client.starts, *client.appends]
        for chunk in call.get("chunks") or []
    )
    assert "The answer begins here." in streamed

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    (stop,) = client.stops
    tail = "".join(
        str(chunk.get("text") or "")
        for chunk in stop.get("chunks") or []
        if chunk.get("type") == "markdown_text"
    )
    assert tail == slack_connect._STREAM_STOPPED_TAIL
    assert slack_connect._ACTIVITY_TITLE_STOPPED in tail
    # Nothing was re-sent: the mark is additive, so the streamed prefix is
    # nowhere in what the close holds.
    assert "The answer begins here." not in tail


def test_the_streamed_mark_says_what_the_card_says() -> None:
    assert slack_connect._ACTIVITY_TITLE_STOPPED in slack_connect._STREAM_STOPPED_TAIL
    slack_connect._STREAM_STOPPED_TAIL.encode("ascii")
    assert ASKER not in slack_connect._STREAM_STOPPED_TAIL
    assert "stopped by" not in slack_connect._STREAM_STOPPED_TAIL.lower()


@pytest.mark.asyncio
async def test_a_terminal_arriving_after_the_stop_does_not_close_it_twice(
    _no_stream_debounce,
) -> None:
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    assert len(client.stops) == 1

    await channel.send(_threaded_final())
    await _settle()

    assert len(client.stops) == 1, "the close must not be spent twice"


@pytest.mark.asyncio
async def test_a_turn_that_ended_on_its_own_keeps_its_own_ending(
    _no_stream_debounce,
) -> None:
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)

    await channel.send(_threaded_final())
    await _settle()
    closes_by_the_turn = len(client.stops)
    assert closes_by_the_turn == 1

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(client.stops) == closes_by_the_turn
    assert channel._streams == {}


@pytest.mark.asyncio
async def test_a_close_refused_at_the_stop_is_reported_and_not_raised(
    _no_stream_debounce, logged: dict[str, list[str]]
) -> None:
    channel, client, dispatched = _streaming_channel(
        client={"stop_error": RuntimeError("message_not_in_streaming_state")}
    )
    await _streaming_card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert client.stops == []
    assert channel._streams == {}
    assert any("streaming stopped with the turn" in line for line in logged["info"])


@pytest.mark.asyncio
async def test_a_stop_with_no_stream_open_closes_nothing(
    _no_stream_debounce,
) -> None:
    channel, client, dispatched = _streaming_channel()
    await channel.send(_threaded_tool_call())
    await _settle()
    assert client.starts == []

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert client.stops == []


@pytest.mark.asyncio
async def test_a_resumed_turns_stream_is_found_under_its_own_id(
    _no_stream_debounce,
) -> None:
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)

    resumed = "req-7f3a-resumed"
    channel._remember_activity_alias(resumed, REQUEST_ID)
    await channel.send(_delta(" And carries on.\n", request_id=resumed))
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    keys = channel._stream_keys_for(record)
    assert sorted(key[0] for key in keys) == sorted([REQUEST_ID, resumed])

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(client.stops) == 2
    assert channel._streams == {}


# ── Releasing the session ────────────────────────────────────────────────────
#
# The third consumer of the terminal event a stop never sends. The card was the
# first and the open stream the second; this one is the initiator record, and
# unlike those two it is not a display defect. ``turn_initiator`` is the
# liveness signal the inbound path reads, so an entry left standing after a stop
# tells every later message that a turn is running -- and under
# ``mid_turn: queue`` every later message is therefore held, behind a turn that
# ended minutes ago, waiting for an event that is never coming.


def _queueing_channel(
    **config: Any,
) -> tuple[SlackChannel, _RecordingSlackClient, list[Message]]:
    """A channel that holds mid-turn messages instead of cancelling the turn.

    ``_mid_turn_mode`` is patched rather than configured for the reason the
    mid-turn tests patch it: what chooses between the three modes is a
    per-conversation setting, and it is not this file's subject.
    """
    channel, client, dispatched = _channel(**config)
    channel._mid_turn_mode = (  # type: ignore[assignment]
        lambda *_a, **_kw: MID_TURN_QUEUE
    )
    return channel, client, dispatched


def _room_message(*, ts: str, user: str = ASKER, text: str = "and one more thing") -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": ROOM,
        "user": user,
        "text": text,
        "ts": ts,
        "thread_ts": THREAD,
    }


async def _inbound(channel: SlackChannel, ts: str, event_id: str, **kw: Any) -> str:
    return await channel._handle_slack_event(
        _room_message(ts=ts, **kw),
        {"event_id": event_id, "team_id": TEAM},
        is_dm=False,
        trigger="mention",
    )


async def _running_turn(
    channel: SlackChannel, client: _RecordingSlackClient
) -> None:
    """A turn with a card on screen and an initiator entry, as a real one has."""
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")


def _sends(dispatched: list[Message]) -> list[Message]:
    return [msg for msg in dispatched if msg.req_method == ReqMethod.CHAT_SEND]


@pytest.mark.asyncio
async def test_a_message_after_a_stop_is_dispatched_rather_than_queued() -> None:
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    # The entry is what the inbound path reads, and it is gone.
    assert channel.turn_initiator(SESSION) is None

    outcome = await _inbound(channel, "1780000100.000100", "Ev-after-stop")

    assert outcome.startswith("dispatched:")
    assert channel._queued_messages == {}
    assert len(_sends(dispatched)) == 1


@pytest.mark.asyncio
async def test_a_stop_releases_the_message_already_waiting_behind_it() -> None:
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    held = await _inbound(channel, "1780000050.000100", "Ev-held")
    assert held.startswith("queued:")
    assert channel._queued_messages[SESSION]
    assert client.added, "the held message should have been marked"

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    # Dispatched by the stop itself, with no further message needed to shake it
    # loose.
    assert len(_sends(dispatched)) == 1
    assert channel._queued_messages == {}
    assert client.removed, "the waiting mark should have come off"


@pytest.mark.asyncio
async def test_a_stop_drains_one_held_message_and_not_the_queue() -> None:
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    assert (await _inbound(channel, "1780000050.000100", "Ev-1")).startswith("queued:")
    assert (await _inbound(channel, "1780000051.000100", "Ev-2")).startswith("queued:")
    assert len(channel._queued_messages[SESSION]) == 2

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_sends(dispatched)) == 1
    assert len(channel._queued_messages[SESSION]) == 1
    # The one that left is the one that arrived first: waiting must not reorder
    # a conversation.
    # startswith, because the envelope marker the connector appends trails it.
    assert _sends(dispatched)[0].params["query"].startswith("and one more thing")


@pytest.mark.asyncio
async def test_a_stop_does_not_retire_a_newer_turns_initiator() -> None:
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)
    # The session has moved on to somebody else's turn.
    channel._remember_turn_initiator(SESSION, BYSTANDER, "req-newer", is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    entry = channel.turn_initiator(SESSION)
    assert entry is not None
    assert entry.request_id == "req-newer"
    assert entry.user_id == BYSTANDER

    # And the drain reads the same entry, so nothing is let out in front of it.
    assert (await _inbound(channel, "1780000100.000100", "Ev-after")).startswith(
        "queued:"
    )
    assert len(_sends(dispatched)) == 0


@pytest.mark.asyncio
async def test_a_resumed_turns_initiator_is_found_through_the_alias() -> None:
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    resumed = "req-7f3a-resumed"
    channel._remember_activity_alias(resumed, REQUEST_ID)
    channel._remember_turn_initiator(SESSION, ASKER, resumed, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert channel.turn_initiator(SESSION) is None


@pytest.mark.asyncio
async def test_a_terminal_arriving_after_a_stop_changes_nothing() -> None:
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    assert channel.turn_initiator(SESSION) is None

    await channel.send(_final())
    await _settle()

    assert channel.turn_initiator(SESSION) is None
    assert channel._queued_messages == {}
    assert len(_sends(dispatched)) == 0


@pytest.mark.asyncio
async def test_a_stop_says_it_released_the_session(
    logged: dict[str, list[str]],
) -> None:
    channel, client, _dispatched = _queueing_channel()
    await _running_turn(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert any(
        "turn initiator retired by a stop" in line and REQUEST_ID in line
        for line in logged["info"]
    )


@pytest.mark.asyncio
async def test_a_stop_with_nothing_recorded_releases_nothing() -> None:
    channel, client, dispatched = _queueing_channel(allow_from=[ASKER])
    await _card(channel, client)
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert channel.turn_initiator(SESSION) is None
    assert channel._queued_messages == {}


# ── The other trigger: a message that cancels the turn in front of it ────────
#
# The button is not the only way a turn ends without saying so. Slack's ambient
# gesture is posting a message, and the gateway reads that as "cancel the
# running turn and run this one instead" -- which is the default, what every
# conversation that writes no ``mid_turn`` does, and how almost every cancelled
# turn on a busy channel actually ends. That cancel is the same cancel the
# button dispatches and it reports the same nothing, but it never goes near
# ``_handle_stop_action``, so none of the endings written there were written
# for it. Four mentions in quick succession opened three streams and closed
# one, and left three cards saying "Working..." about turns that had finished.


@pytest.mark.asyncio
async def test_the_next_message_ends_the_turn_it_cancels() -> None:
    channel, client, _dispatched = _channel()
    await _running_turn(channel, client)
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING

    assert (await _inbound(channel, "1780000100.000100", "Ev-second")).startswith(
        "dispatched:"
    )
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True
    # And it loses its card for the same reason a pressed stop does. Supersede
    # shares that path deliberately rather than by accident: it reaches
    # ``_settle_stopped_card``, which is where the discard is decided, so the
    # card a cancelled turn leaves behind is no card at all.
    assert len(client.deleted) == 1
    # The tool that was still running when the cancel landed is counted as
    # having reported nothing, never as having failed -- the same reading the
    # button's stop gets.
    assert record.tool_totals().get(slack_connect._RUN_FAILED, 0) == 0


@pytest.mark.asyncio
async def test_the_next_message_closes_the_stream_it_cancels(
    _no_stream_debounce,
) -> None:
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")
    assert client.stops == []

    await _inbound(channel, "1780000100.000100", "Ev-second")
    await _settle()

    assert len(client.stops) == 1
    assert client.stops[0]["ts"] == "1780000000.777777"
    assert channel._streams == {}


@pytest.mark.asyncio
async def test_a_message_into_an_idle_session_ends_nothing() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    # No initiator entry: as far as this connector can say, nothing is running.
    assert channel.turn_initiator(SESSION) is None
    before = len(client.updates)

    await _inbound(channel, "1780000100.000100", "Ev-first")
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert len(client.updates) == before


@pytest.mark.asyncio
async def test_a_steered_message_does_not_end_the_turn_it_joins() -> None:
    channel, client, _dispatched = _channel()
    await _running_turn(channel, client)
    channel._mid_turn_mode = (  # type: ignore[assignment]
        lambda *_a, **_kw: slack_connect.MID_TURN_STEER
    )

    assert (await _inbound(channel, "1780000100.000100", "Ev-steer")).startswith(
        "steered:"
    )
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING


@pytest.mark.asyncio
async def test_a_held_message_does_not_end_the_turn_it_waits_for() -> None:
    """Under ``queue`` the message is not sent, so nothing is cancelled."""
    channel, client, _dispatched = _queueing_channel()
    await _running_turn(channel, client)

    assert (await _inbound(channel, "1780000100.000100", "Ev-held")).startswith(
        "queued:"
    )
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING


@pytest.mark.asyncio
async def test_a_resumed_turns_card_is_found_when_the_next_message_cancels_it() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    resumed = "req-7f3a-resumed"
    channel._remember_activity_alias(resumed, REQUEST_ID)
    channel._remember_turn_initiator(SESSION, ASKER, resumed, is_dm=False, chat_type="channel")

    await _inbound(channel, "1780000100.000100", "Ev-second")
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """Everything the connector's own logger emits, taken off it directly.

    Not ``caplog``, whose handler sits on the root logger: whether anything
    reaches it depends on whether this package's loggers propagate, which is a
    property of whatever configured logging first rather than of the code under
    test.
    """
    collected: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = collected.append  # type: ignore[method-assign]
    # A notice is posted by the reply delivery, which logs under its own name.
    loggers = [logging.getLogger(slack_connect.__name__), reply_delivery.logger]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        for logger, level in zip(loggers, levels):
            logger.removeHandler(handler)
            logger.setLevel(level)


@pytest.mark.asyncio
async def test_the_clicker_being_told_is_recorded_as_its_own_notice(records) -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    told = [r for r in records if "what became of their message" in r.getMessage()]
    assert len(told) == 1
    assert told[0].levelno == logging.INFO
    assert ASKER in told[0].getMessage()
    assert "stop-accepted" in told[0].getMessage()
    assert slack_connect._STOP_ACCEPTED_NOTICE not in told[0].getMessage()


# ── Slack's own stop button ──────────────────────────────────────────────────
#
# The third stop path and the only one that is an event. Slack draws a stop
# button of its own on a thread whose agent session is ``processing``, and a
# click on it arrives as ``agent_session_stopped``. It names a conversation, a
# thread and a person, and nothing else: no request id, because Slack knows of
# none.
#
# Two duties, and Slack states both. Stop the work for that channel and thread,
# and move the session off ``processing`` -- Slack does not move it, and a
# session left there stays there for an hour.
#
# It is not registered through the event-family machinery. That machinery is for
# events describing what somebody did to a message, each with a disposition an
# operator sets per conversation and every one of them shipping off. This is a
# command aimed at this app, and a disposition between the button and the turn
# would mean the button did nothing in every default deployment.


def _native_stop(
    *,
    user_id: str = ASKER,
    channel_id: str = ROOM,
    thread_ts: str = THREAD,
    streaming_message_ts: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The event Slack sends for a click on its own stop button."""
    event: dict[str, Any] = {
        "type": "agent_session_stopped",
        "channel": channel_id,
        "thread_ts": thread_ts,
        "user": user_id,
        "event_ts": "1783536983.783769",
    }
    if streaming_message_ts is not None:
        event["streaming_message_ts"] = streaming_message_ts
    return event, {"team_id": TEAM, "event": event}


async def _native_stop_target(
    channel: SlackChannel, client: _RecordingSlackClient
) -> None:
    """A running turn with a card, reachable by conversation and thread."""
    await _card(channel, client)
    _native_stop_turn(channel)


def _native_stop_turn(channel: SlackChannel, request_id: str = REQUEST_ID) -> None:
    """A running turn reachable by conversation and thread, with no card."""
    channel._remember_turn_initiator(
        SESSION,
        ASKER,
        request_id,
        is_dm=False,
        chat_type="channel",
        origin=SlackMessageOrigin(
            channel_id=ROOM,
            message_ts="1780000100.000100",
            reply_thread_ts=THREAD,
            root_thread_ts=THREAD,
        ),
    )


@pytest.mark.asyncio
async def test_slacks_own_stop_button_stops_the_turn_and_drops_the_card() -> None:
    channel, client, dispatched = _channel(session_surface="agents")
    await _native_stop_target(channel, client)

    event, body = _native_stop(streaming_message_ts=["1782234987.693923"])
    outcome = await channel._route_agent_session_stopped(event, body)
    await _settle()

    assert outcome == "stopped:native-button-1"
    assert len(_cancels(dispatched)) == 1
    assert len(client.deleted) == 1
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.stopped is True
    assert record.closed is True


@pytest.mark.asyncio
async def test_slacks_own_stop_button_stops_a_turn_that_has_no_card() -> None:
    channel, _client, dispatched = _channel(session_surface="agents")
    _native_stop_turn(channel)

    event, body = _native_stop()
    outcome = await channel._route_agent_session_stopped(event, body)
    await _settle()

    assert outcome == "stopped:native-button-1"
    assert len(_cancels(dispatched)) == 1
    assert channel.turn_initiator(SESSION) is None
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.stopped is True
    assert record.closed is True


@pytest.mark.asyncio
async def test_stops_of_turns_with_no_card_keep_a_bounded_number_of_records() -> None:
    channel, _client, _dispatched = _channel(session_surface="agents")
    stops = slack_activity._MAX_CLOSED_ACTIVITY_RECORDS + 8

    for index in range(stops):
        _native_stop_turn(channel, request_id=f"req-{index}")
        event, body = _native_stop()
        assert await channel._route_agent_session_stopped(event, body) == (
            "stopped:native-button-1"
        )
        await _settle()

    assert len(channel._activity_records) <= slack_activity._MAX_CLOSED_ACTIVITY_RECORDS


def _route_fails_once(channel: SlackChannel) -> None:
    """Make the next routed request raise, as a bus that is down does."""
    route = channel._route_request

    async def fail_once(_req: Message) -> None:
        channel._route_request = route  # type: ignore[method-assign]
        raise RuntimeError("bus down")

    channel._route_request = fail_once  # type: ignore[method-assign]


def _raised(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_a_card_stop_that_fails_to_send_can_be_clicked_again() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")
    _route_fails_once(channel)

    body, action = _click(user_id=ASKER)
    with captured_logs(slack_connect.logger.name) as records:
        await channel._handle_stop_action(_Ack(), body, action)
    assert any("a stop click raised" in line for line in _raised(records))
    assert channel.turn_initiator(SESSION) is not None

    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert channel.turn_initiator(SESSION) is None
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.stopped is True
    assert record.closed is True


@pytest.mark.asyncio
async def test_a_native_stop_that_fails_to_send_can_be_pressed_again() -> None:
    channel, client, dispatched = _channel(session_surface="agents")
    await _native_stop_target(channel, client)
    _route_fails_once(channel)

    event, body = _native_stop()
    with captured_logs(slack_connect.logger.name) as records:
        await channel._handle_agent_session_stopped(event, body)
    assert any("raised" in line for line in _raised(records))
    assert channel.turn_initiator(SESSION) is not None

    outcome = await channel._route_agent_session_stopped(event, body)
    await _settle()

    assert outcome == "stopped:native-button-1"
    assert len(_cancels(dispatched)) == 1
    assert channel.turn_initiator(SESSION) is None
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.stopped is True
    assert record.closed is True


@pytest.mark.asyncio
async def test_a_failed_stop_leaves_no_record_once_a_turn_with_the_card_off_ends() -> None:
    channel, _client, dispatched = _channel(session_surface="agents", activity_card=False)
    _native_stop_turn(channel)
    _route_fails_once(channel)

    event, body = _native_stop()
    with pytest.raises(RuntimeError):
        await channel._route_agent_session_stopped(event, body)
    await channel.send(_final())
    await _settle()

    assert not _cancels(dispatched)
    assert (REQUEST_ID, ROOM) not in channel._activity_records


@pytest.mark.asyncio
async def test_slacks_own_stop_button_moves_the_session_off_processing() -> None:
    channel, client, _dispatched = _channel(session_surface="agents")
    await _native_stop_target(channel, client)

    event, body = _native_stop()
    await channel._route_agent_session_stopped(event, body)
    await _settle()

    statuses = [
        data
        for method, data in client.api_calls
        if method == thread_status._AGENTS_SET_STATUS_METHOD
    ]
    assert statuses[-1]["status"] == thread_status.AGENTS_STATUS_ACTIVE
    assert statuses[-1]["channel_id"] == ROOM
    assert statuses[-1]["thread_ts"] == THREAD


@pytest.mark.asyncio
async def test_a_native_stop_on_a_thread_with_no_turn_still_writes_the_status(
) -> None:
    channel, client, dispatched = _channel(session_surface="agents")
    await _card(channel, client)

    event, body = _native_stop()
    outcome = await channel._route_agent_session_stopped(event, body)
    await _settle()

    assert outcome == "ignored:no-running-turn"
    assert _cancels(dispatched) == []
    assert client.deleted == []
    statuses = [
        data
        for method, data in client.api_calls
        if method == thread_status._AGENTS_SET_STATUS_METHOD
    ]
    assert [data["status"] for data in statuses] == [
        thread_status.AGENTS_STATUS_ACTIVE
    ]


@pytest.mark.asyncio
async def test_a_native_stop_names_its_streamed_replies_as_a_list() -> None:
    channel = _channel()[0]

    assert channel._streaming_message_ts(
        {"streaming_message_ts": ["1782234987.693923", "1782234987.693999"]}
    ) == ("1782234987.693923", "1782234987.693999")
    # A payload that narrows to one id later must not become 18 of them.
    assert channel._streaming_message_ts(
        {"streaming_message_ts": "1782234987.693923"}
    ) == ("1782234987.693923",)
    assert channel._streaming_message_ts({}) == ()
    assert channel._streaming_message_ts({"streaming_message_ts": None}) == ()


@pytest.mark.asyncio
async def test_a_native_stop_reports_the_streams_slack_named(records) -> None:
    channel, client, _dispatched = _channel(session_surface="agents")
    await _native_stop_target(channel, client)

    event, body = _native_stop(
        streaming_message_ts=["1782234987.693923", "1782234987.693999"]
    )
    await channel._route_agent_session_stopped(event, body)
    await _settle()

    lines = [record.getMessage() for record in records]
    assert any(
        "Slack's own button" in line
        and "1782234987.693923,1782234987.693999" in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_a_turn_that_ends_normally_keeps_its_card() -> None:
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()

    await channel.send(_final())
    await _settle()

    assert client.deleted == []
    assert _title(client).startswith(slack_connect._ACTIVITY_TITLE_DONE)
