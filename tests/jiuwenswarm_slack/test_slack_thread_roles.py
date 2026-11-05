"""A Slack thread timestamp answers two questions, so the turn entry holds two.

``reply_thread_ts`` says where this turn's answer is posted. Empty means the
top of the room, which is where a DM outside any thread is answered.

``root_thread_ts`` says which thread the message belongs to. It is the event's
``thread_ts`` for a reply and the message's own ``ts`` otherwise, so it is
never empty. The session id is built from it and both thread-status APIs
require it.

Both are read together wherever a status is drawn. The root names the thread
Slack is given, and the reply target says whether Slack has already drawn it:
a status written on a root Slack has never drawn opens that root. The reader
then sits in a thread the answer does not go into.

These tests pin the pair from the outside: what Slack is asked to draw, and
where the reply is routed, for each of the four conversations this connector
distinguishes.
"""

from belle_agent.platforms.slack import thread_status

import time
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm.slack_routing import slack_session_id
from belle_agent.scopes import MID_TURN_QUEUE
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"
CHANNEL = "C0CHANNEL1"
THINKING = "is thinking…"

pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


class _RecordingSlackClient:
    """Just enough Slack to see the status calls and the posts."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.api_calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000199.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        return {"ts": kwargs.get("ts", "")}

    async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
        self.api_calls.append({"api_method": api_method, **kwargs})
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}


def _status_threads(client: _RecordingSlackClient) -> list[str]:
    return [
        str(call["data"]["thread_ts"])
        for call in client.api_calls
        if call["api_method"] == thread_status._ASSISTANT_SET_STATUS_METHOD
    ]


def _channel(**overrides: Any) -> tuple[SlackChannel, list[Message], Any]:
    overrides.setdefault("allow_from", [ASKER])
    overrides.setdefault("reply_in_thread", True)
    overrides.setdefault("thinking_status", THINKING)
    # The subject is the line assistant.threads.setStatus draws, so the surface
    # is pinned. Under the default word, auto, the shipped wording probes for
    # agents.sessions.setStatus instead.
    overrides.setdefault("session_surface", "assistant")
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **overrides), RobotMessageRouter()
    )
    channel._running = True
    client = _RecordingSlackClient()
    channel._client = client
    dispatched: list[Message] = []
    channel.on_message(dispatched.append)
    return channel, dispatched, client


def _event(
    *, channel_id: str, chat_type: str, ts: str, thread_ts: str = ""
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel_type": chat_type,
        "channel": channel_id,
        "user": ASKER,
        "text": "sweep the repos",
        "ts": ts,
    }
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


async def _post(
    channel: SlackChannel, event: dict[str, Any], event_id: str, *, is_dm: bool
) -> str:
    return await channel._handle_slack_event(
        event,
        {"event_id": event_id, "team_id": TEAM},
        is_dm=is_dm,
        trigger="dm" if is_dm else "mention",
    )


# --------------------------------------------------------------------------
# the DM, where the two values differ
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_top_level_dm_gets_no_status_at_all() -> None:
    """The one conversation that gets no status line.

    Slack opens the thread a status is written on, and the only thread here is
    the message's own root, which Slack has never drawn. A status would move
    the reader into that empty thread while the answer lands at the top of the
    conversation. Slack's own stop button follows the status, so a top-level DM
    has no native stop button either.
    """
    channel, _dispatched, client = _channel()
    ts = "1710000100.000100"

    await _post(channel, _event(channel_id=DM, chat_type="im", ts=ts), "EvA", is_dm=True)

    assert _status_threads(client) == []


@pytest.mark.asyncio
async def test_a_top_level_dm_reply_is_still_posted_at_top_level() -> None:
    """The status thread must not become a reply target.

    A DM is already private, so threading each exchange inside it adds a layer
    nobody asked for. ``slack_thread_ts`` routes the reply, and it stays empty.
    """
    channel, dispatched, _client = _channel()
    ts = "1710000100.000200"

    await _post(channel, _event(channel_id=DM, chat_type="im", ts=ts), "EvB", is_dm=True)

    assert dispatched[0].metadata["slack_thread_ts"] == ""


@pytest.mark.asyncio
async def test_a_top_level_dm_gets_a_status_under_reply_in_thread_all() -> None:
    """``reply_in_thread: all`` threads the answer, so the status comes back.

    The condition is read off this message's own reply target, so it follows
    the setting without reading it. The answer opens the thread, Slack draws
    that thread, and the status goes somewhere the reader can see.
    """
    channel, dispatched, client = _channel(reply_in_thread="all")
    ts = "1710000100.000110"

    await _post(channel, _event(channel_id=DM, chat_type="im", ts=ts), "EvA2", is_dm=True)

    assert _status_threads(client) == [ts]
    assert dispatched[0].metadata["slack_thread_ts"] == ts


@pytest.mark.asyncio
async def test_a_threaded_dm_answers_and_draws_in_the_thread_it_joined() -> None:
    """A DM the sender put in a thread: both values are that thread.

    Neither value is the reply's own ``ts``. The sender chose the thread, so
    the answer goes there and the status is drawn there. Slack has already
    drawn that thread, which is what separates this from the top-level case
    under the same setting.
    """
    channel, dispatched, client = _channel()
    thread = "1710000100.000300"

    await _post(
        channel,
        _event(channel_id=DM, chat_type="im", ts="1710000100.000400", thread_ts=thread),
        "EvC",
        is_dm=True,
    )

    assert _status_threads(client) == [thread]
    assert dispatched[0].metadata["slack_thread_ts"] == thread


# --------------------------------------------------------------------------
# the channel, where the two values agree and nothing may move
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_in_thread", [True, False])
async def test_a_channel_message_is_unchanged_either_way(
    reply_in_thread: bool,
) -> None:
    """``reply_in_thread`` moves the reply. A channel's status stays put.

    With it on, a root message opens a thread and both values are its ``ts``.
    With it off, the reply goes to the channel root and the reply target is
    empty. The status is drawn on the message either way: the rule that skips
    an undrawn root is read for the one-to-one direct message, the one room
    where a reader was moved.
    """
    channel, dispatched, client = _channel(reply_in_thread=reply_in_thread)
    ts = "1710000100.000500"

    await _post(
        channel,
        _event(channel_id=CHANNEL, chat_type="channel", ts=ts),
        "EvD",
        is_dm=False,
    )

    assert _status_threads(client) == [ts]
    assert dispatched[0].metadata["slack_thread_ts"] == (ts if reply_in_thread else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_in_thread", [True, False])
async def test_a_threaded_channel_message_is_answered_in_its_thread(
    reply_in_thread: bool,
) -> None:
    """The setting does not reach a message that is already in a thread.

    That was the point of the first arm of the old two-armed condition, and it
    survives the rewrite into one.
    """
    channel, dispatched, client = _channel(reply_in_thread=reply_in_thread)
    thread = "1710000100.000600"

    await _post(
        channel,
        _event(
            channel_id=CHANNEL,
            chat_type="channel",
            ts="1710000100.000700",
            thread_ts=thread,
        ),
        "EvE",
        is_dm=False,
    )

    assert _status_threads(client) == [thread]
    assert dispatched[0].metadata["slack_thread_ts"] == thread


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_session_id_is_built_from_the_root_and_not_the_reply_target()  -> (
    None
):
    """A channel message with ``reply_in_thread`` off keeps its thread session.

    The reply target is empty here and the root is the message's own ``ts``.
    The session id holds the root, which is what makes the root the identity.
    """
    channel, dispatched, _client = _channel(reply_in_thread=False)
    ts = "1710000100.000800"

    await _post(
        channel,
        _event(channel_id=CHANNEL, chat_type="channel", ts=ts),
        "EvF",
        is_dm=False,
    )

    assert dispatched[0].session_id == slack_session_id(
        team_id=TEAM,
        channel_id=CHANNEL,
        user_id=ASKER,
        root_thread_ts=ts,
        is_dm=False,
    )
    assert dispatched[0].metadata["slack_thread_ts"] == ""


# --------------------------------------------------------------------------
# the entry itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_turn_entry_holds_both_and_neither_is_called_thread_ts() -> None:
    """Nothing may read one field and get whichever meaning it assumed.

    The field name is the whole guard: a consumer has to name the value it
    wants, so a consumer that wanted the other one cannot get it by accident.
    """
    channel, dispatched, _client = _channel()
    ts = "1710000100.000900"

    await _post(channel, _event(channel_id=DM, chat_type="im", ts=ts), "EvG", is_dm=True)

    entry = channel.turn_initiator(str(dispatched[0].session_id))
    assert entry is not None
    assert (entry.reply_thread_ts, entry.root_thread_ts) == ("", ts)
    assert not hasattr(entry, "thread_ts")
    assert not hasattr(slack_connect._SlackQueuedMessage, "thread_ts")


@pytest.mark.asyncio
async def test_slacks_own_stop_event_finds_a_top_level_dm_turn() -> None:
    """A stop event names a thread, so the turn is matched on the root.

    The reply target is empty in a top-level DM and the event's thread never
    is, so matching the reply target would find no turn at all. The match has
    to hold for a button Slack drew before this turn was dispatched as much as
    for one drawn in a thread.
    """
    channel, dispatched, _client = _channel()
    ts = "1710000101.000100"

    await _post(channel, _event(channel_id=DM, chat_type="im", ts=ts), "EvH", is_dm=True)
    candidates = channel._native_stop_candidates(DM, ts)

    assert [session_id for session_id, _entry in candidates] == [
        str(dispatched[0].session_id)
    ]


@pytest.mark.asyncio
async def test_a_queued_dm_keeps_both_values_through_the_drain() -> None:
    """The drain turns a held message into a turn, and hands on all four.

    A queued message is where the two can drift apart unnoticed: it is settled
    on one path and read on another, minutes later.
    """
    channel, dispatched, _client = _channel()
    # Queue rather than replace, so the second message is held.
    channel._mid_turn_mode = lambda *_a, **_kw: MID_TURN_QUEUE  # type: ignore[assignment]
    first = "1710000102.000100"
    second = "1710000102.000200"

    await _post(
        channel, _event(channel_id=DM, chat_type="im", ts=first), "EvI", is_dm=True
    )
    outcome = await _post(
        channel, _event(channel_id=DM, chat_type="im", ts=second), "EvJ", is_dm=True
    )
    assert outcome.startswith("queued:")

    session_id = str(dispatched[0].session_id)
    held = channel._queued_messages[session_id][0]
    assert (held.reply_thread_ts, held.root_thread_ts) == ("", second)

    # End the running turn so the drain starts the held one.
    channel._admission.turn_initiators.pop(session_id, None)
    await channel._drain_queued_messages(session_id)

    entry = channel.turn_initiator(session_id)
    assert entry is not None
    assert (entry.reply_thread_ts, entry.root_thread_ts) == ("", second)
    assert time.monotonic() >= entry.started_at
