"""One check per row of the inbound table: how a timeline event becomes the host's record."""

import time

import pytest
from peer import USER, Peer, Room

from belle_agent.platforms.base import TRIGGER_DM, TRIGGER_MENTION, TRIGGER_NONE, TRIGGER_REPLY
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.settings import MatrixSettings

ROOM = "!lounge:syn.example"
DM = "!dm:syn.example"
BOB = "@bob:syn.example"


def matrix(peer: Peer, **fields) -> MatrixPlatform:
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p", **fields)
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    platform._helper, platform.user_id = peer, USER
    return platform


def timeline(content: dict, *, room: str = ROOM, sender: str = BOB, event_id: str = "$e1", **fields) -> dict:
    return {"event": "timeline", "seq": 1, "room": room, "encrypted": True, "direct": room == DM,
            "room_event": {"type": "m.room.message", "sender": sender, "event_id": event_id,
                           "origin_server_ts": int(time.time() * 1000), "content": content, **fields}}


@pytest.fixture
def peer() -> Peer:
    found = Peer()
    found.rooms[DM] = Room(direct=True, joined_members=2)
    return found


async def test_a_direct_room_with_two_members_is_direct_and_its_answer_stays_at_the_top(peer):
    record = await matrix(peer).inbound(timeline({"msgtype": "m.text", "body": "hi"}, room=DM))
    assert (record.chat_type, record.trigger, record.reply_thread) == ("direct", TRIGGER_DM, "")
    assert (record.conversation, record.message_id, record.event_id, record.sender) == (DM, "$e1", "$e1", BOB)
    assert record.envelope == f"[chat_id: {DM}, message_id: $e1]"


async def test_a_direct_room_that_gained_a_third_member_is_private(peer):
    peer.rooms[DM].joined_members = 3
    record = await matrix(peer).inbound(timeline({"body": "hi"}, room=DM))
    assert (record.chat_type, record.trigger) == ("private", TRIGGER_NONE)


async def test_a_public_join_rule_is_public_and_any_other_private(peer):
    peer.rooms["!town:s"] = Room(encrypted=False, public=True)
    assert (await matrix(peer).inbound(timeline({"body": "x"}, room="!town:s"))).chat_type == "public"
    assert (await matrix(peer).inbound(timeline({"body": "x"}))).chat_type == "private"


async def test_a_mention_in_m_mentions_triggers_and_is_answered_in_the_thread_of_the_message(peer):
    record = await matrix(peer).inbound(timeline({"body": "belle: hi", "m.mentions": {"user_ids": [USER]}}))
    assert (record.trigger, record.reply_thread) == (TRIGGER_MENTION, "$e1")
    assert record.envelope.endswith("[trigger: mention]")


async def test_room_mentions_and_display_names_are_no_mention(peer):
    platform = matrix(peer)
    assert (await platform.inbound(timeline({"body": "@room hi", "m.mentions": {"room": True}}))).trigger == TRIGGER_NONE
    assert (await platform.inbound(timeline({"body": "belle hi"}))).trigger == TRIGGER_NONE


async def test_with_no_m_mentions_a_link_to_the_bot_in_the_html_is_a_mention(peer):
    html = f'<a href="https://matrix.to/#/{USER}">belle</a> hi'
    record = await matrix(peer).inbound(timeline({"body": "belle hi", "format": "org.matrix.custom.html", "formatted_body": html}))
    assert record.trigger == TRIGGER_MENTION
    # With m.mentions present, its list decides, whatever the HTML links.
    linked = {"body": "x", "formatted_body": html, "m.mentions": {}}
    assert (await matrix(peer).inbound(timeline(linked))).trigger == TRIGGER_NONE


async def test_a_reply_to_the_bot_triggers_and_a_reply_to_somebody_else_does_not(peer):
    peer.events["$bot"] = {"type": "m.room.message", "sender": USER, "event_id": "$bot", "content": {"body": "earlier"}}
    peer.events["$bob"] = {"type": "m.room.message", "sender": BOB, "event_id": "$bob", "content": {"body": "earlier"}}
    platform = matrix(peer)
    reply = await platform.inbound(timeline({"body": "and?", "m.relates_to": {"m.in_reply_to": {"event_id": "$bot"}}}))
    assert (reply.trigger, reply.reply_thread, reply.envelope.endswith("[trigger: reply]")) == (TRIGGER_REPLY, "$e1", True)
    other = await platform.inbound(timeline({"body": "and?", "m.relates_to": {"m.in_reply_to": {"event_id": "$bob"}}}, event_id="$e2"))
    assert other.trigger == TRIGGER_NONE


async def test_a_message_in_a_thread_the_bot_started_triggers_and_keeps_its_thread(peer):
    peer.events["$root"] = {"type": "m.room.message", "sender": USER, "event_id": "$root", "content": {"body": "root"}}
    relates = {"rel_type": "m.thread", "event_id": "$root", "is_falling_back": True, "m.in_reply_to": {"event_id": "$x"}}
    record = await matrix(peer).inbound(timeline({"body": "more", "m.relates_to": relates}))
    assert (record.trigger, record.thread, record.reply_thread) == (TRIGGER_REPLY, "$root", "$root")
    # The fallback reply of a thread is not read as a reply: only the thread's root counted.
    assert [r["event_id"] for r in peer.requests if r["op"] == "event"] == ["$root"]


async def test_an_edit_by_its_sender_is_an_edited_message_of_the_original(peer):
    peer.events["$first"] = {"type": "m.room.message", "sender": BOB, "event_id": "$first",
                             "content": {"body": "x", "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}}
    edit = {"body": "* fixed", "m.new_content": {"msgtype": "m.text", "body": "fixed"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$first"}}
    record = await matrix(peer).inbound(timeline(edit, event_id="$edit"))
    assert (record.edited, record.text, record.message_id, record.event_id, record.thread) == (True, "fixed", "$first", "$edit", "$root")


async def test_an_edit_by_another_sender_is_dropped_and_logged(peer, caplog):
    peer.events["$first"] = {"type": "m.room.message", "sender": "@alice:syn.example", "event_id": "$first", "content": {"body": "x"}}
    edit = {"body": "* mine", "m.new_content": {"body": "mine"}, "m.relates_to": {"rel_type": "m.replace", "event_id": "$first"}}
    assert await matrix(peer).inbound(timeline(edit, event_id="$edit")) is None
    assert "matrix dropped an edit of $first by @bob:syn.example" in caplog.text


@pytest.mark.parametrize(("content", "sender", "bot_ids"), [
    ({"msgtype": "m.notice", "body": "x"}, BOB, frozenset()),
    ({"msgtype": "m.text", "body": "x"}, "@relay:syn.example", frozenset({"@relay:syn.example"})),
])
async def test_a_notice_or_a_sender_in_bot_user_ids_is_a_bot(peer, content, sender, bot_ids):
    assert (await matrix(peer, bot_user_ids=bot_ids).inbound(timeline(content, sender=sender))).sender_is_bot


async def test_the_bots_own_message_and_other_event_types_are_no_message(peer):
    platform = matrix(peer)
    assert await platform.inbound(timeline({"body": "mine"}, sender=USER)) is None
    reaction = timeline({"m.relates_to": {"rel_type": "m.annotation", "event_id": "$e", "key": "👍"}})
    reaction["room_event"]["type"] = "m.reaction"
    assert await platform.inbound(reaction) is None
