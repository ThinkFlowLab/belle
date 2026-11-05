"""What the platform sends, and how it keeps itself running: posts, reactions, typing, invites, notices, restarts."""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from peer import USER, Peer, Room

from belle_agent.platforms.base import (
    MARK_DONE,
    MARK_RECEIVED,
    InboundAnswer,
    InboundDrop,
    InboundEvent,
    InboundMessage,
    InboundStop,
)
from belle_agent.platforms.matrix import connector, render
from belle_agent.platforms.matrix.connector import UNDECRYPTABLE_NOTICE, MatrixPlatform
from belle_agent.platforms.matrix.settings import MatrixSettings

ROOM = "!lounge:syn.example"
TOWN = "!town:syn.example"
DM = "!dm:syn.example"
FAKE = str(Path(__file__).parent / "fake_helper.py")


def matrix(peer: Peer, **fields) -> MatrixPlatform:
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p", **fields)
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    platform._helper, platform.user_id = peer, USER
    return platform


@pytest.fixture
def peer() -> Peer:
    found = Peer()
    found.rooms.update({DM: Room(direct=True, joined_members=2), TOWN: Room(encrypted=False, public=True)})
    return found


def message(room: str = ROOM) -> InboundMessage:
    return InboundMessage(platform="matrix", workspace="default", conversation=room, message_id="$m1", sender="@bob:s", text="hi")


@pytest.mark.parametrize(("room", "budget"), [(ROOM, 44_000), (TOWN, 60_000)])
async def test_a_long_answer_is_split_by_the_budget_of_its_room_and_nothing_is_lost(peer, room, budget):
    paragraphs = [f"Paragraph {number}: " + "word " * 900 for number in range(40)]
    await matrix(peer).post(room, "$root", "\n\n".join(paragraphs))
    sent = [request["content"] for request in peer.sent()]
    assert len(sent) > 1 and all(render.size(content["body"]) <= budget for content in sent)
    assert "\n\n".join(content["body"] for content in sent) == "\n\n".join(paragraph.strip() for paragraph in paragraphs)
    assert {content["m.relates_to"]["event_id"] for content in sent} == {"$root"}
    assert all(content["msgtype"] == "m.notice" and content["format"] == "org.matrix.custom.html" for content in sent)


async def test_a_part_the_server_refuses_as_too_large_is_split_again(peer):
    peer.max_bytes = 3_000
    text = "\n\n".join("line " * 100 for _ in range(10))
    first = await matrix(peer).post(ROOM, "", text)
    sent = [event["content"]["body"] for event in peer.events.values()]
    assert first == next(iter(peer.events)) and len(sent) > 1 and len(peer.sent()) > len(sent)
    assert " ".join(" ".join(sent).split()) == " ".join(text.split())


async def test_a_rate_limited_send_waits_and_keeps_its_transaction_id(peer):
    peer.send_errors = [("M_LIMIT_EXCEEDED", 20)]
    await matrix(peer).post(ROOM, "", "hello")
    first, second = peer.sent()
    assert first["txn_id"] == second["txn_id"]


async def test_another_refusal_is_raised_with_its_code(peer):
    peer.send_errors = [("M_FORBIDDEN", None)]
    with pytest.raises(Exception, match="M_FORBIDDEN"):
        await matrix(peer).post(ROOM, "", "hello")


async def test_marks_add_and_remove_the_configured_reactions(peer):
    platform = matrix(peer, typing_indicator=False)
    await platform.mark(message(), MARK_RECEIVED)
    [reaction] = peer.sent("m.reaction")
    assert reaction["content"] == {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$m1", "key": "\N{EYES}"}}
    await platform.mark(message(), MARK_DONE)
    reaction_id = next(event_id for event_id, event in peer.events.items() if event["type"] == "m.reaction")
    assert [r["event_id"] for r in peer.requests if r["op"] == "redact"] == [reaction_id]


async def test_a_reaction_key_matches_with_and_without_u_fe0f(peer):
    platform = matrix(peer)
    await platform.react(ROOM, "$m1", "\N{THUMBS UP SIGN}")
    await platform.react(ROOM, "$m1", "\N{THUMBS UP SIGN}️")
    assert len(peer.sent("m.reaction")) == 1
    await platform.react(ROOM, "$m1", "+1", remove=True)
    assert [r["op"] for r in peer.requests] == ["send", "redact"]


async def test_a_reaction_name_outside_the_table_is_refused_naming_it(peer):
    with pytest.raises(ValueError, match="GLYPH_SHORTCODES in belle_agent/emoji.py"):
        await matrix(peer).react(ROOM, "$m1", "not_an_emoji")


async def test_the_bot_types_in_a_room_while_a_turn_runs_and_stops_after(peer, monkeypatch):
    monkeypatch.setattr(connector, "TYPING_REPEAT_SECONDS", 0.01)
    platform = matrix(peer, emoji={})
    await platform.mark(message(), MARK_RECEIVED)
    await asyncio.sleep(0.05)
    await platform.mark(message(), MARK_DONE)
    await asyncio.sleep(0.01)
    typing = [(r["typing"], r.get("timeout_ms")) for r in peer.requests if r["op"] == "typing"]
    assert len(typing) >= 2 and set(typing[:-1]) == {(True, 30_000)} and typing[-1] == (False, None)


@pytest.mark.parametrize(("room", "direct", "told"), [(DM, True, 1), (ROOM, False, 0)])
async def test_an_undecryptable_message_is_logged_and_told_once_per_session_in_a_direct_room(peer, caplog, room, direct, told):
    platform = matrix(peer)
    utd = {"event": "utd", "room": room, "event_id": "$x", "sender": "@bob:s", "session_id": "S1", "direct": direct}
    await platform.handle(utd)
    await platform.handle({**utd, "event_id": "$y"})
    assert [r["content"]["body"] for r in peer.sent()] == [UNDECRYPTABLE_NOTICE] * told
    assert caplog.text.count("matrix cannot decrypt") == 2 and "session=S1" in caplog.text


@pytest.mark.parametrize(("inviter", "op"), [("@alice:syn.example", "join"), ("@mallory:evil.example", "leave")])
async def test_an_invite_is_joined_where_allow_from_names_the_inviter_and_declined_otherwise(peer, inviter, op):
    platform = matrix(peer, allow_from=frozenset({"@alice:syn.example"}))
    await platform.handle({"event": "invite", "seq": 1, "room": "!new:s", "inviter": inviter})
    assert [r["op"] for r in peer.requests] == [op]


async def test_with_an_empty_allow_from_every_invite_is_joined(peer):
    await matrix(peer).handle({"event": "invite", "seq": 1, "room": "!new:s", "inviter": "@anyone:s"})
    assert [r["op"] for r in peer.requests] == ["join"]


async def test_a_message_older_than_the_catch_up_window_reaches_the_host_as_late_and_the_skip_is_summarized(peer, caplog):
    caplog.set_level(logging.INFO)
    handed = []

    async def host(record):
        handed.append((record.text, record.late))
        return "admitted"

    platform = matrix(peer, catch_up_seconds=300)
    await platform.start(host)
    now = int(time.time() * 1000)
    for text, age in (("old", 600_000), ("older", 900_000), ("fresh", 1_000)):
        peer.emit({"event": "timeline", "seq": 1, "room": DM, "encrypted": True, "direct": True, "room_event": {
            "type": "m.room.message", "sender": "@bob:s", "event_id": f"${text}", "origin_server_ts": now - age,
            "content": {"msgtype": "m.text", "body": text}}})
    for _ in range(100):
        if len(handed) == 3 and "matrix gave no turn to 2 message(s) older than 300 s" in caplog.text:
            break
        await asyncio.sleep(0.01)
    await platform.stop()
    assert handed == [("old", True), ("older", True), ("fresh", False)]
    assert "matrix gave no turn to 2 message(s) older than 300 s" in caplog.text


def _said(peer: Peer, seq: int, text: str, sent: float) -> None:
    peer.emit({"event": "timeline", "seq": seq, "room": DM, "encrypted": True, "direct": True, "room_event": {
        "type": "m.room.message", "sender": "@bob:s", "event_id": f"${text}", "origin_server_ts": int(sent * 1000),
        "content": {"msgtype": "m.text", "body": text}}})


async def _until(check: Callable[[], bool], seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition did not hold in time")


async def test_a_message_that_waits_in_belles_own_queue_past_the_catch_up_window_still_gets_a_turn(peer: Peer) -> None:
    handed: list[str] = []

    async def host(record: InboundMessage | InboundAnswer | InboundStop | InboundDrop | InboundEvent) -> str:
        assert isinstance(record, InboundMessage)
        handed.append(record.text + (" (late)" if record.late else ""))
        if record.text == "first":
            await asyncio.sleep(1.3)  # A mark that waits out the server's rate limit holds up the next event.
        return "admitted"

    platform = matrix(peer, catch_up_seconds=1)
    await platform.start(host)
    now = time.time()
    _said(peer, 1, "first", now)
    _said(peer, 2, "second", now)
    await _until(lambda: len(handed) == 2)
    await platform.stop()
    assert handed == ["first", "second"]


async def test_a_message_the_helper_held_back_while_belle_was_busy_is_on_time_and_a_late_one_late(
    peer: Peer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    handed: list[str] = []

    async def host(record: InboundMessage | InboundAnswer | InboundStop | InboundDrop | InboundEvent) -> str:
        assert isinstance(record, InboundMessage)
        handed.append(record.text + (" (late)" if record.late else ""))
        if record.text == "first":
            await asyncio.sleep(1.3)
        return "admitted"

    platform = matrix(peer, catch_up_seconds=1)
    await platform.start(host)
    busy = time.time()
    _said(peer, 1, "first", busy)
    # The helper syncs again only once Belle has acknowledged the batch, so "held" reaches Belle 1.3 s after it was sent.
    await _until(lambda: {"op": "ack", "seq": 1} in peer.requests)
    _said(peer, 2, "held", busy)
    await _until(lambda: len(handed) == 2)
    await _until(lambda: {"op": "ack", "seq": 2} in peer.requests)
    # Belle is idle now: a message that reaches it more than catch_up_seconds after it was sent came late from outside.
    idle = time.time()
    await asyncio.sleep(1.3)
    _said(peer, 3, "late", idle)
    await _until(lambda: "matrix gave no turn to 1 message(s) older than 1 s" in caplog.text)
    await platform.stop()
    assert handed == ["first", "held", "late (late)"]


async def test_a_helper_that_exits_is_started_again_and_its_old_events_are_not_acknowledged(peer, monkeypatch, caplog):
    monkeypatch.setattr(connector, "RESTART_MAX_DELAY_SECONDS", 0.01)
    platform = matrix(peer)
    platform._helper = None
    await platform.start(lambda record: asyncio.sleep(0, "admitted"))
    peer.exited.set_result(9)
    for _ in range(100):
        if "matrix connected" in caplog.text and caplog.text.count("matrix connected") == 2:
            break
        await asyncio.sleep(0.01)
    assert "matrix helper exited with code 9" in caplog.text
    assert [r["op"] for r in peer.requests].count("start") == 2
    await platform.stop()


async def test_a_helper_that_keeps_exiting_leaves_matrix_down(monkeypatch, caplog):
    monkeypatch.setattr(connector, "RESTART_MAX_DELAY_SECONDS", 0.01)
    monkeypatch.setenv("FAKE_HELPER_MODE", "die")
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p",
                              recovery_key="k", helper_path=FAKE)
    platform = MatrixPlatform(settings)
    await platform.start(lambda record: asyncio.sleep(0, "admitted"))
    supervisor = platform._tasks[1]
    await asyncio.wait_for(supervisor, 20)
    await platform.stop()
    assert caplog.text.count("its pending requests failed") == connector.RESTART_LIMIT
    assert "matrix stays down: the helper exited 5 times within 300 s. The other platforms go on" in caplog.text
