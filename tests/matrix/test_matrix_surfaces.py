"""A running turn on Matrix: its reply shown as it is written, its activity card, and a stop by reaction."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from peer import USER, Peer, Room

from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent
from belle_agent.click_notices import STOP_REFUSED_NOTICE
from belle_agent.platforms.base import (
    ENDED_SILENT,
    MARK_DONE,
    MARK_FAILED,
    MARK_STOPPED,
    InboundDrop,
    InboundMessage,
    InboundStop,
)
from belle_agent.platforms.matrix import render, surfaces
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.settings import MatrixSettings

ROOM = "!lounge:syn.example"
TOWN = "!town:syn.example"
MESSAGE = InboundMessage(
    platform="matrix", workspace="default", conversation=ROOM, message_id="$m1", sender="@bob:s", text="hi", reply_thread="$m1"
)


@pytest.fixture(autouse=True)
def no_spacing(monkeypatch):
    monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.0)


@pytest.fixture
def peer() -> Peer:
    found = Peer()
    found.rooms[TOWN] = Room(encrypted=False, public=True)
    return found


def matrix(peer: Peer, **fields) -> MatrixPlatform:
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p",
                              typing_indicator=False, **fields)
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    platform._helper, platform.user_id = peer, USER
    return platform


def shown(peer: Peer, room: str = ROOM) -> list[str]:
    """The text of each message of the room as a client shows it: with its latest edit, and none that was removed."""
    events = peer.timeline.get(room, [])
    edits = {e["content"]["m.relates_to"]["event_id"]: e["content"]["m.new_content"]["body"]
             for e in events if (e["content"].get("m.relates_to") or {}).get("rel_type") == "m.replace"}
    return [edits.get(e["event_id"], e["content"]["body"]) for e in events
            if e["type"] == "m.room.message" and "m.new_content" not in e["content"]]


async def say(platform: MatrixPlatform, *deltas: str, message: InboundMessage = MESSAGE) -> None:
    for delta in deltas:
        await platform.progress(message, TurnEvent(EVENT_TEXT_DELTA, text=delta))
        await asyncio.sleep(0.01)


def tool(kind: str, call_id: str, name: str = "read_conversation", text: str = "") -> TurnEvent:
    return TurnEvent(kind, text=text, data={"call_id": call_id, "name": name})


def test_each_surface_is_shown_only_where_its_setting_turns_it_on(peer):
    assert set(matrix(peer, enable_streaming=True, activity_card=False).shows) == {EVENT_TEXT_DELTA, EVENT_STEP}
    assert set(matrix(peer).shows) == {EVENT_TOOL_CALLED, EVENT_TOOL_RESULT}


async def test_a_streamed_reply_is_one_notice_edited_in_place_that_ends_as_the_final(peer):
    platform = matrix(peer, enable_streaming=True)

    await say(platform, "The tide ", "turns at four.")
    assert await platform.conclude(MESSAGE, MARK_DONE, "The tide turns at four.") is True

    assert shown(peer) == ["The tide turns at four."]
    first = peer.timeline[ROOM][0]["content"]
    assert (first["msgtype"], first["m.relates_to"]) == ("m.notice", {"rel_type": "m.thread", "event_id": "$m1"})


async def test_a_reply_that_is_not_the_final_comes_down_and_the_host_posts_the_final(peer):
    platform = matrix(peer, enable_streaming=True)

    await say(platform, "draft")
    assert await platform.conclude(MESSAGE, MARK_FAILED) is False

    assert shown(peer) == []


async def test_a_stopped_reply_keeps_what_it_wrote_marked_as_stopped(peer):
    platform = matrix(peer, enable_streaming=True)

    await say(platform, "halfway")
    assert await platform.conclude(MESSAGE, MARK_STOPPED) is False

    assert shown(peer) == ["halfway\n\n_Turn stopped_"]


@pytest.mark.parametrize(("room", "encrypted"), [(ROOM, True), (TOWN, False)])
async def test_a_reply_past_the_streamed_budget_goes_on_in_a_new_notice_never_inside_a_code_block(peer, monkeypatch, room, encrypted):
    monkeypatch.setitem(render.STREAM_BUDGET, encrypted, 400)
    monkeypatch.setitem(render.STREAM_BUDGET, not encrypted, 10**6)
    platform = matrix(peer, enable_streaming=True)
    message = InboundMessage(platform="matrix", workspace="default", conversation=room, message_id="$m1", sender="@bob:s", text="hi")
    code = "```\n" + "\n\n".join(f"line {n}" for n in range(10)) + "\n```"
    final = "\n\n".join(["Before the code. " * 8, code, "After the code. " * 8])

    await say(platform, *[final[i : i + 50] for i in range(0, len(final), 50)], message=message)
    assert await platform.conclude(message, MARK_DONE, final) is True

    parts = shown(peer, room)
    assert len(parts) > 1 and all(render.size(part) <= 400 for part in parts)
    assert "\n\n".join(parts).split() == final.split()
    assert all(part.count("```") in (0, 2) for part in parts)


async def test_a_rate_limit_slows_the_edits_and_the_final_still_arrives_whole(peer):
    platform = matrix(peer, enable_streaming=True)
    await say(platform, "first")
    reply = platform.surfaces._replies[(ROOM, "$m1")]
    peer.send_errors = [("M_LIMIT_EXCEEDED", 30)]

    await say(platform, " second")
    assert reply.interval >= 0.03 and shown(peer) == ["first"]

    peer.send_errors = [("M_LIMIT_EXCEEDED", 30), ("M_LIMIT_EXCEEDED", 30)]
    started = time.monotonic()
    assert await platform.conclude(MESSAGE, MARK_DONE, "first second third") is True
    assert time.monotonic() - started >= 0.06
    assert shown(peer) == ["first second third"]


# ── the card ─────────────────────────────────────────────────────────────


async def test_the_card_is_a_notice_in_the_thread_with_a_stop_reaction_and_ends_as_finished(peer):
    platform = matrix(peer, activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)

    await platform.progress(MESSAGE, tool(EVENT_TOOL_CALLED, "c1"))
    await asyncio.sleep(0.05)
    card = peer.timeline[ROOM][0]
    assert card["content"]["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$m1"}
    assert "**Working...**" in card["content"]["body"] and "read_conversation" in card["content"]["body"]
    (stop,) = peer.sent("m.reaction")
    assert stop["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": card["event_id"], "key": "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}"}

    await platform.progress(MESSAGE, tool(EVENT_TOOL_RESULT, "c1", text='{"ok": true}'))
    await platform.conclude(MESSAGE, MARK_DONE, "done")

    (text,) = shown(peer)
    assert text.startswith("**Turn finished**") and "to stop" not in text
    assert [r["event_id"] for r in peer.requests if r["op"] == "redact"] == [next(
        event_id for event_id, event in peer.events.items() if event["type"] == "m.reaction")]


@pytest.mark.parametrize(("state", "title"), [(ENDED_SILENT, "**Turn finished"), (MARK_STOPPED, "**Turn stopped**")])
async def test_the_card_of_a_turn_that_said_nothing_or_was_stopped_stays_in_its_thread_settled(peer, state, title):
    platform = matrix(peer, activity_card_delay_seconds=0)
    await platform.progress(MESSAGE, tool(EVENT_TOOL_CALLED, "c1"))
    await asyncio.sleep(0.05)

    await platform.conclude(MESSAGE, state)

    # A redaction would strip the thread relation, and the stub would show in the room's main timeline.
    (text,) = shown(peer)
    assert text.startswith(title) and "to stop" not in text
    assert [r["op"] for r in peer.requests if r["op"] == "redact"] == ["redact"]  # The stop reaction only.


async def test_a_turn_that_ends_before_the_delay_posts_no_card(peer):
    platform = matrix(peer, activity_card_delay_seconds=5)
    await platform.progress(MESSAGE, tool(EVENT_TOOL_CALLED, "c1"))
    await platform.conclude(MESSAGE, MARK_DONE, "done")
    assert peer.sent() == []


# ── a stop or a removal by reaction (D18) ────────────────────────────────


def turn(message_id: str, sender: str = "@bob:s") -> SimpleNamespace:
    return SimpleNamespace(inbound=SimpleNamespace(message_id=message_id, sender=sender, reply_thread=message_id))


def host(running=(), waiting=()) -> SimpleNamespace:
    return SimpleNamespace(running=lambda workspace, room: list(running), waiting=lambda workspace, room: list(waiting))


def reaction(target: str, key: str = "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}", sender: str = "@carol:s") -> dict:
    return {"event": "timeline", "seq": 1, "room": ROOM, "encrypted": False, "direct": False,
            "room_event": {"type": "m.reaction", "sender": sender, "event_id": f"$r{time.monotonic_ns()}",
                           "origin_server_ts": int(time.time() * 1000),
                           "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}}}}


def receiving(platform: MatrixPlatform, outcome: str) -> list:
    got: list = []

    async def receive(record):
        got.append(record)
        return outcome

    platform._on_inbound = receive
    return got


async def test_a_stop_reaction_on_the_card_stops_the_turn_of_its_message(peer):
    platform = matrix(peer, activity_card_delay_seconds=0)
    platform.bind(host())
    await platform.progress(MESSAGE, tool(EVENT_TOOL_CALLED, "c1"))
    await asyncio.sleep(0.05)
    card = peer.timeline[ROOM][0]["event_id"]
    got = receiving(platform, "stopped session=s")

    assert await platform.handle(reaction(card)) == "stopped session=s"
    assert got == [InboundStop("matrix", "default", ROOM, "$m1", "@carol:s")]


async def test_a_stop_reaction_on_a_running_message_stops_it_and_on_a_waiting_one_removes_it(peer):
    platform = matrix(peer)
    platform.bind(host(running=[turn("$run")], waiting=[turn("$wait")]))
    got = receiving(platform, "stopped")

    await platform.handle(reaction("$run", key="\N{BLACK SQUARE FOR STOP}"))
    await platform.handle(reaction("$wait"))

    assert got == [InboundStop("matrix", "default", ROOM, "$run", "@carol:s"), InboundDrop("matrix", "default", ROOM, "$wait", "@carol:s")]


async def test_another_reaction_stops_nothing_and_a_reaction_of_the_bot_s_is_no_stop(peer):
    platform = matrix(peer)
    platform.bind(host(running=[turn("$run")]))
    got = receiving(platform, "buffered:reaction")

    assert await platform.handle(reaction("$run", key="\N{THUMBS UP SIGN}")) == "buffered:reaction"
    assert await platform.handle(reaction("$run", sender=USER)) == "ignored:done-by-this-bot"
    assert [type(record).__name__ for record in got] == ["InboundEvent"]


async def test_a_refused_stop_is_told_once_in_the_thread_and_notifies_nobody(peer):
    platform = matrix(peer)
    platform.bind(host(running=[turn("$run")]))
    receiving(platform, "refused:clicks.stop=user")

    await platform.handle(reaction("$run"))
    await platform.handle(reaction("$run"))

    (notice,) = peer.sent()
    assert notice["content"]["body"] == f"@carol:s: {STOP_REFUSED_NOTICE}"
    assert notice["content"]["m.mentions"] == {} and notice["content"]["m.relates_to"] == {
        "m.in_reply_to": {"event_id": "$run"}, "rel_type": "m.thread", "event_id": "$run", "is_falling_back": False}
