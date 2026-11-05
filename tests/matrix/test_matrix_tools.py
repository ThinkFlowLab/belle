"""The Matrix tools on the peer helper: post, react and read, the audience rule, and the decision gate's inputs."""

import json

import pytest
from peer import USER, Peer, Room

from belle_agent.message_decisions import KIND_PRIVATE
from belle_agent.platforms.base import TRIGGER_MENTION, InboundMessage
from belle_agent.platforms.matrix.connector import UNDECRYPTABLE_ENTRY, MatrixPlatform
from belle_agent.platforms.matrix.settings import MatrixSettings
from belle_agent.platforms.matrix.toolkit import METADATA_MESSAGE, METADATA_ROOM, METADATA_USER

LOUNGE = "!lounge:syn.example"
TOWN = "!town:syn.example"
OTHER = "!other:syn.example"
ALICE, BOB, CAROL = "@alice:syn.example", "@bob:syn.example", "@carol:syn.example"


def joined(*users: str) -> list[dict]:
    return [{"user_id": USER, "display_name": "belle", "membership": "join"},
            *({"user_id": user, "display_name": user[1:].split(":")[0], "membership": "join"} for user in users)]


def test_matrix_registers_each_tool_once(turn):
    names = [card["name"] for toolkit in turn.platform.toolkits(dict) for card in toolkit.get_tool_definitions()]
    assert len(names) == len(set(names))


@pytest.fixture
def peer() -> Peer:
    found = Peer()
    found.rooms.update({LOUNGE: Room(), TOWN: Room(encrypted=False, public=True, name="town"), OTHER: Room()})
    found.members.update({LOUNGE: joined(ALICE, BOB), TOWN: joined(ALICE, BOB, CAROL), OTHER: joined(ALICE)})
    return found


@pytest.fixture
def turn(peer):
    """A toolkit bound to bob's message in the lounge, and the metadata it reads."""
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p")
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    platform._helper, platform.user_id = peer, USER
    started = peer.add(LOUNGE, BOB, {"msgtype": "m.text", "body": "hello"})
    metadata = {METADATA_ROOM: LOUNGE, METADATA_MESSAGE: started, METADATA_USER: BOB}
    (toolkit,) = platform.toolkits(lambda: metadata)

    async def call(name: str, **arguments) -> dict:
        return json.loads(await getattr(toolkit, name)(**arguments))

    call.toolkit, call.metadata, call.platform, call.started = toolkit, metadata, platform, started
    return call


async def test_a_post_lands_in_the_turns_room_with_its_id_and_a_permalink(turn, peer):
    posted = await turn("post_message", text="**hi**")
    assert posted["ok"] and posted["chat_id"] == LOUNGE
    (sent,) = peer.sent()
    assert sent["content"]["msgtype"] == "m.notice" and sent["content"]["formatted_body"] == "<p><strong>hi</strong></p>"
    assert posted["permalink"].startswith("https://matrix.to/#/%21lounge%3Asyn.example/%24peer")


async def test_post_text_is_kept_and_non_string_text_is_refused(turn, peer):
    assert (await turn("post_message", text=7))["error"] == "text_invalid"
    assert peer.sent() == []
    assert (await turn("post_message", text="  exact text  "))["ok"] is True
    assert peer.sent()[0]["content"]["body"] == "  exact text  "


async def test_a_reply_to_a_message_in_a_thread_posts_under_the_threads_root(turn, peer):
    reply = peer.add(LOUNGE, ALICE, {"body": "in it", "m.relates_to": {"rel_type": "m.thread", "event_id": turn.started}})
    await turn("post_message", text="answer", reply_to_message_id=reply)
    assert peer.sent()[0]["content"]["m.relates_to"] == {"rel_type": "m.thread", "event_id": turn.started}


async def test_a_post_to_a_room_whose_readers_read_this_one_needs_no_question(turn, peer):
    assert await turn.toolkit.widening_question("post_message", {"text": "x", "chat_id": OTHER}) is None
    assert (await turn("post_message", text="x", chat_id=OTHER))["chat_id"] == OTHER


async def test_a_post_that_reaches_new_readers_is_asked_about_or_refused_without_a_requester(turn, peer):
    question = await turn.toolkit.widening_question("post_message", {"text": "x", "chat_id": TOWN})
    assert f"would go to town ({TOWN}), which is a public room. Anybody who can join the room can read it" in question
    assert "workspace" not in question and "channel" not in question
    turn.metadata[METADATA_USER] = ""
    refused = await turn("post_message", text="x", chat_id=TOWN)
    assert refused["error"] == "write_target_widens_audience" and peer.sent() == []


async def test_a_post_to_a_person_uses_or_makes_their_direct_room(turn, peer):
    posted = await turn("post_message", text="psst", chat_id=ALICE)
    assert posted["chat_id"] == peer.dms[ALICE] and peer.sent()[0]["room"] == peer.dms[ALICE]
    question = await turn.toolkit.widening_question("post_message", {"text": "x", "chat_id": CAROL})
    assert "privately to @carol:syn.example" in question and CAROL not in peer.dms


@pytest.mark.parametrize(("arguments", "code"), [
    ({"text": "  "}, "nothing_to_post"),
    ({"text": "x", "chat_id": "#lounge:syn.example"}, "chat_id_malformed"),
    ({"text": "x", "reply_to_message_id": "abc"}, "message_id_malformed"),
    ({"text": "x", "reply_to_message_id": "$missing"}, "message_not_found"),
])
async def test_a_post_that_does_not_fit_is_refused_by_name(turn, peer, arguments, code):
    assert (await turn("post_message", **arguments))["error"] == code
    assert peer.sent() == []


async def test_a_post_in_a_room_the_bot_did_not_join_is_refused(turn, peer):
    peer.joined = {LOUNGE, OTHER}
    turn.metadata[METADATA_USER] = ""
    peer.members.pop(TOWN)
    refused = await turn("post_message", text="x", chat_id=TOWN)
    assert refused["error"] == "chat_not_joined" and f"not a member of {TOWN}," in refused["detail"]
    assert refused["chat_id"] == TOWN


async def test_a_reaction_lands_once_on_the_turns_message_and_comes_off_again(turn, peer):
    first = await turn("react_to_message", emoji=":+1:")
    again = await turn("react_to_message", emoji="\N{THUMBS UP SIGN}\N{VARIATION SELECTOR-16}")
    assert first["ok"] and again["ok"] and first["message_id"] == turn.started
    (reaction,) = peer.sent("m.reaction")
    assert reaction["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": turn.started, "key": "\N{THUMBS UP SIGN}"}
    removed = await turn("react_to_message", emoji="+1", remove=True)
    assert removed["removed"] and [r["op"] for r in peer.requests].count("redact") == 1
    assert (await turn("react_to_message", emoji="+1", remove=True))["ok"]
    assert [r["op"] for r in peer.requests].count("redact") == 1


async def test_a_reaction_on_another_rooms_message_or_with_an_unknown_name_is_refused(turn, peer):
    elsewhere = peer.add(OTHER, ALICE, {"body": "there"})
    assert (await turn("react_to_message", emoji="eyes", message_id=elsewhere))["error"] == "message_not_found"
    refused = await turn("react_to_message", emoji="not_an_emoji")
    assert refused["error"] == "emoji_not_recognised" and "GLYPH_SHORTCODES" in refused["detail"]
    assert peer.sent("m.reaction") == []


async def test_a_read_returns_messages_in_order_counts_what_it_cannot_decrypt_and_applies_only_own_edits(turn, peer):
    peer.timeline[LOUNGE] = []
    peer.add(LOUNGE, ALICE, {}, event_type="m.room.create")
    secret = peer.add(LOUNGE, CAROL, {}, undecryptable=True)
    mine = peer.add(LOUNGE, ALICE, {"msgtype": "m.text", "body": "first"})
    theirs = peer.add(LOUNGE, BOB, {"msgtype": "m.text", "body": "second"})
    peer.add(LOUNGE, ALICE, {"body": "* first!", "m.new_content": {"body": "first!"}, "m.relates_to": {"rel_type": "m.replace", "event_id": mine}})
    peer.add(LOUNGE, ALICE, {"body": "* hacked", "m.new_content": {"body": "hacked"}, "m.relates_to": {"rel_type": "m.replace", "event_id": theirs}})
    peer.add(LOUNGE, ALICE, {"m.relates_to": {"rel_type": "m.annotation", "event_id": mine, "key": "x"}}, event_type="m.reaction")
    read = await turn("read_conversation")
    assert [m["message_id"] for m in read["messages"]] == [secret, mine, theirs]
    assert read["messages"][0]["undecryptable"] and read["messages"][0]["text"] == ""
    assert read["messages"][1]["text"] == "first!" and "edited_iso_utc" in read["messages"][1]
    assert read["messages"][2]["text"] == "second" and read["messages"][2]["author_name"] == "bob"
    assert read["coverage"]["undecryptable"] == 1 and read["coverage"]["edits_by_others_ignored"] == 1
    assert read["coverage"]["status"] == "complete" and "inaccessible_before_iso_utc" not in read["coverage"]
    assert read["encrypted"] is True and read["chat_type"] == "private"


async def test_a_read_pages_back_to_the_start_of_the_room(turn, peer):
    peer.timeline[LOUNGE] = []
    sent = [peer.add(LOUNGE, ALICE, {"msgtype": "m.text", "body": f"m{n}"}) for n in range(250)]
    seen, before = [], None
    for _ in range(10):
        read = await turn("read_conversation", max_messages=100, **({"before_message_id": before} if before else {}))
        seen = [m["message_id"] for m in read["messages"]] + seen
        if read["coverage"]["status"] == "complete":
            break
        before = read["coverage"]["next_before_message_id"]
    assert seen == sent
    # The history starts after the room's first event: the server keeps older events from the bot.
    assert "inaccessible_before_iso_utc" in read["coverage"]


async def test_a_thread_message_whose_bundled_edit_cannot_be_decrypted_says_so(turn, peer):
    reply = peer.add(LOUNGE, ALICE, {"body": "first", "m.relates_to": {"rel_type": "m.thread", "event_id": turn.started}})
    plain = peer.add(LOUNGE, ALICE, {"body": "old", "m.relates_to": {"rel_type": "m.thread", "event_id": turn.started}})
    peer.events[reply]["unsigned"] = {"m.relations": {"m.replace": {"type": "m.room.encrypted", "sender": ALICE, "content": {}}}}
    peer.events[plain]["unsigned"] = {"m.relations": {"m.replace": {"type": "m.room.message", "sender": ALICE,
                                                                    "content": {"m.new_content": {"body": "new"}}}}}
    read = await turn("read_conversation", message_id=turn.started)
    texts = {m["message_id"]: (m["text"], m.get("edit_not_decrypted", False)) for m in read["messages"]}
    assert texts[reply] == ("first", True) and texts[plain] == ("new", False)
    assert read["coverage"]["edits_not_decrypted"] == 1


async def test_a_read_after_an_instant_stops_there(turn, peer):
    peer.timeline[LOUNGE] = []
    peer.add(LOUNGE, ALICE, {"body": "old"}, at=1_000_000)
    new = peer.add(LOUNGE, ALICE, {"body": "new"}, at=2_000_000)
    read = await turn("read_conversation", after_iso_utc="1970-01-13T00:00:00Z")
    assert [m["message_id"] for m in read["messages"]] == [new] and read["coverage"]["status"] == "complete"


async def test_a_thread_is_read_with_its_root_first(turn, peer):
    replies = [peer.add(LOUNGE, ALICE, {"body": f"r{n}", "m.relates_to": {"rel_type": "m.thread", "event_id": turn.started}})
               for n in range(3)]
    read = await turn("read_conversation", message_id=replies[1])
    assert [m["message_id"] for m in read["messages"]] == [turn.started, *replies]
    assert {m["root_message_id"] for m in read["messages"][1:]} == {turn.started}
    older = await turn("read_conversation", message_id=turn.started, before_message_id=replies[1])
    assert [m["message_id"] for m in older["messages"]] == [turn.started, replies[0]]


async def test_a_thread_applies_the_time_bound_to_root_replies_and_undecryptable_events(turn, peer):
    peer.events[turn.started]["origin_server_ts"] = 1_000_000_000
    relation = {"rel_type": "m.thread", "event_id": turn.started}
    peer.add(LOUNGE, ALICE, {"body": "old", "m.relates_to": relation}, at=1_000_001)
    recent = peer.add(LOUNGE, ALICE, {"body": "recent", "m.relates_to": relation}, at=1_000_002)
    hidden = peer.add(LOUNGE, ALICE, {"body": "hidden", "m.relates_to": relation}, at=1_000_003)
    peer.undecryptable.add(hidden)
    bound = "1970-01-12T13:46:41Z"

    read = await turn("read_conversation", message_id=turn.started, after_iso_utc=bound)
    before = await turn("read_conversation", message_id=turn.started, after_iso_utc=bound,
                        before_message_id=recent)
    empty = await turn("read_conversation", message_id=turn.started, after_iso_utc="1970-01-12T13:46:43Z")

    assert [message["message_id"] for message in read["messages"]] == [recent, hidden]
    assert read["coverage"]["undecryptable"] == 1
    assert read["coverage"]["after_iso_utc"] == bound
    assert before["messages"] == [] and before["coverage"]["status"] == "complete"
    assert empty["messages"] == [] and empty["coverage"]["status"] == "complete"


async def test_thread_time_bound_does_not_assume_timestamps_follow_relation_order(turn, peer):
    peer.events[turn.started]["origin_server_ts"] = 1_000_000_000
    relation = {"rel_type": "m.thread", "event_id": turn.started}
    earlier = peer.add(LOUNGE, ALICE, {"body": "newer timestamp", "m.relates_to": relation}, at=1_000_010)
    peer.add(LOUNGE, ALICE, {"body": "older timestamp", "m.relates_to": relation}, at=1_000_000)
    latest = peer.add(LOUNGE, ALICE, {"body": "latest", "m.relates_to": relation}, at=1_000_011)

    read = await turn("read_conversation", message_id=turn.started, after_iso_utc="1970-01-12T13:46:41Z")

    assert [message["message_id"] for message in read["messages"]] == [earlier, latest]


async def test_a_read_of_a_room_that_reaches_people_beyond_this_one_is_refused(turn, peer):
    peer.members[OTHER] = joined(ALICE, BOB)
    peer.members[LOUNGE] = joined(ALICE, BOB, CAROL)
    refused = await turn("read_conversation", chat_id=OTHER)
    assert refused["error"] == "source_members_not_in_target" and refused["messages"] == []


async def test_a_read_of_a_room_the_bot_did_not_join_is_refused(turn, peer):
    peer.joined = {LOUNGE}
    assert (await turn("read_conversation", chat_id=OTHER))["error"] == "chat_not_joined"


@pytest.mark.parametrize(("arguments", "code"), [
    ({"after_iso_utc": "yesterday"}, "after_iso_utc_must_be_an_iso_instant"),
    ({"max_messages": 0}, "max_messages_invalid"),
    ({"before_message_id": "$nowhere"}, "message_not_found"),
    ({"chat_id": "@dave:syn.example"}, "direct_room_not_found"),
])
async def test_a_read_that_does_not_fit_is_refused_by_name(turn, arguments, code):
    assert (await turn("read_conversation", **arguments))["error"] == code


async def test_a_call_with_no_room_of_its_turn_is_refused(turn):
    turn.metadata.clear()
    for name, arguments in (("post_message", {"text": "x"}), ("react_to_message", {"emoji": "eyes"}), ("read_conversation", {})):
        assert (await turn(name, **arguments))["error"] == "trusted_conversation_context_required"


async def test_the_decision_gate_reads_recent_messages_and_the_facts_of_a_message(turn, peer):
    peer.timeline[LOUNGE] = []
    peer.add(LOUNGE, ALICE, {"body": "one"})
    peer.add(LOUNGE, CAROL, {}, undecryptable=True)
    last = peer.add(LOUNGE, BOB, {"body": "two"})
    recent = await turn.platform.recent_messages(LOUNGE, 10, last)
    assert [(e["author"], e["text"]) for e in recent] == [(ALICE, "one"), (CAROL, UNDECRYPTABLE_ENTRY)]
    inbound = InboundMessage(platform="matrix", workspace="default", conversation=LOUNGE, message_id=last, sender=BOB,
                             text="two", chat_type="private", trigger=TRIGGER_MENTION)
    facts = turn.platform.decision_facts(inbound)
    assert facts["conversation_kind"] == KIND_PRIVATE and facts["last_message"]["author"] == BOB
    assert turn.platform.request_metadata(inbound)[METADATA_ROOM] == LOUNGE


# ── edits, removals and files ────────────────────────────────────────────


async def test_the_bot_edits_and_removes_its_own_message(turn, peer):
    mine = (await turn("post_message", text="first"))["message_id"]
    edited = await turn("edit_message", message_id=mine, text="**second**")
    assert edited["ok"] and edited["message_id"] == mine
    (edit,) = peer.sent()[1:]
    content = edit["content"]
    assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": mine}
    assert content["m.new_content"]["body"] == "**second**" and content["m.new_content"]["msgtype"] == "m.notice"
    assert (await turn("delete_message", message_id=mine))["deleted"] is True
    assert peer.requests[-1]["op"] == "redact" and peer.requests[-1]["event_id"] == mine


@pytest.mark.parametrize("tool", ["edit_message", "delete_message"])
async def test_a_change_of_another_persons_message_is_refused_and_nothing_is_sent(turn, peer, tool):
    arguments = {"text": "rewritten"} if tool == "edit_message" else {}
    refused = await turn(tool, message_id=turn.started, **arguments)
    assert refused["error"] == "message_not_own" and BOB in refused["detail"] and USER in refused["detail"]
    assert peer.sent() == [] and not any(request["op"] == "redact" for request in peer.requests)


async def test_an_edit_longer_than_half_an_event_is_refused_with_the_limit(turn, peer):
    mine = (await turn("post_message", text="short"))["message_id"]
    refused = await turn("edit_message", message_id=mine, text="word " * 6000)
    assert refused["error"] == "edit_text_too_long" and "22000 bytes" in refused["detail"] and len(peer.sent()) == 1


async def test_a_file_sent_in_an_encrypted_room_is_uploaded_encrypted_and_reads_back(turn, peer):
    sent = await turn("send_file", filename="notes.txt", content="# Notes", comment="the notes")
    assert sent["ok"] and sent["encrypted"] is True and sent["mimetype"] == "text/plain"
    (upload,) = [request for request in peer.requests if request["op"] == "upload"]
    assert upload["room"] == LOUNGE and upload["mime"] == "text/plain"
    message = peer.events[sent["message_id"]]["content"]
    assert "file" in message and "url" not in message and message["filename"] == "notes.txt" and message["body"] == "the notes"
    read = await turn("download_file", file_id=sent["file_id"])
    assert read["content"] == "# Notes"


async def test_an_empty_matrix_file_is_uploaded_and_reads_back_empty(turn, peer):
    sent = await turn("send_file", filename="empty.txt", content="")

    assert sent["ok"] and sent["size_bytes"] == 0
    assert peer.media[peer.events[sent["message_id"]]["content"]["file"]["url"]] == b""
    read = await turn("download_file", file_id=sent["file_id"])
    assert read["content"] == ""


async def test_exact_matrix_event_and_context_keep_event_type_and_visible_messages(turn, peer):
    first = peer.add(LOUNGE, ALICE, {"msgtype": "m.text", "body": "first"})
    target = peer.add(LOUNGE, ALICE, {"msgtype": "m.text", "body": "target"})
    last = peer.add(LOUNGE, ALICE, {"msgtype": "m.text", "body": "last"})

    found = await turn("read_conversation", event_id=target, context_messages=4)

    assert found["event"]["type"] == "m.room.message"
    assert {first, target, last} <= {message["message_id"] for message in found["messages"]}
    assert found["coverage"]["context_returned"] >= 2
    assert (await turn("read_conversation", event_id=target, message_id=first))["error"] == "conflicting_read_selector"


async def test_exact_state_event_is_not_presented_as_a_message(turn, peer):
    event_id = peer.add(LOUNGE, ALICE, {"name": "New room name"}, event_type="m.room.name")

    found = await turn("read_conversation", event_id=event_id)

    assert found["event"]["type"] == "m.room.name"
    assert found["event"]["content"] == {"name": "New room name"}
    assert found["messages"] == []


async def test_a_file_for_new_readers_is_asked_about_and_refused_without_a_requester(turn, peer):
    assert await turn.toolkit.widening_question("send_file", {"content": "x", "chat_id": TOWN})
    assert await turn.toolkit.widening_question("edit_message", {"message_id": "$x", "text": "x", "chat_id": TOWN})
    sent = await turn("send_file", filename="a.txt", content="x", chat_id=TOWN)
    assert sent["encrypted"] is False and "url" in peer.events[sent["message_id"]]["content"]
    turn.metadata[METADATA_USER] = ""
    refused = await turn("send_file", filename="a.txt", content="x", chat_id=TOWN)
    assert refused["error"] == "write_target_widens_audience"


async def test_a_file_given_in_no_form_or_two_is_refused(turn, peer):
    assert (await turn("send_file", filename="a.txt"))["error"] == "file_source_required"
    assert (await turn("send_file", content="x"))["error"] == "filename_required"
    assert not any(request["op"] == "upload" for request in peer.requests)


async def test_matrix_media_fields_and_encrypted_thumbnail_reach_the_event(turn, peer):
    import base64

    sent = await turn(
        "send_file", filename="clip.mp4", content_base64=base64.b64encode(b"video").decode(),
        mimetype="video/mp4", width=640, height=360, duration_ms=1200,
        thumbnail_content_base64=base64.b64encode(b"preview").decode(),
        thumbnail_mimetype="image/png", thumbnail_width=160, thumbnail_height=90,
    )
    content = peer.events[sent["message_id"]]["content"]

    assert sent["ok"] and content["msgtype"] == "m.video"
    assert content["info"] == {
        "mimetype": "video/mp4", "size": 5, "w": 640, "h": 360, "duration": 1200,
        "thumbnail_file": content["info"]["thumbnail_file"],
        "thumbnail_info": {"mimetype": "image/png", "size": 7, "w": 160, "h": 90},
    }
    assert content["info"]["thumbnail_file"]["url"] in peer.media


async def test_invalid_matrix_media_fields_are_refused_before_upload(turn, peer):
    for options, code in [
        ({"mimetype": "video"}, "mimetype_invalid"),
        ({"width": 0}, "width_invalid"),
        ({"duration_ms": -1}, "duration_ms_invalid"),
        ({"thumbnail_content_base64": "%%%", "thumbnail_mimetype": "image/png"}, "thumbnail_base64_invalid"),
        ({"thumbnail_width": 10}, "thumbnail_required"),
    ]:
        assert (await turn("send_file", filename="clip.mp4", content="data", **options))["error"] == code
    assert not any(request["op"] == "upload" for request in peer.requests)


@pytest.mark.parametrize("threaded", [False, True])
async def test_server_size_fallback_returns_every_event_in_order(turn, peer, monkeypatch, threaded):
    from belle_agent.platforms.matrix.helper import HelperError

    original = turn.platform.send_event
    attempts = 0

    async def send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HelperError("M_TOO_LARGE", "server event limit")
        return await original(*args, **kwargs)

    monkeypatch.setattr(turn.platform, "send_event", send)
    arguments = {"reply_to_message_id": turn.started} if threaded else {}
    result = await turn("post_message", text="words " * 30, **arguments)
    events = [e for e in peer.events.values() if e["sender"] == USER]
    ids = [e["event_id"] for e in events]
    assert result["ok"] and len(ids) >= 2
    assert result["part_message_ids"] == ids
    assert result["message_id"] == ids[0] and result["parts"] == len(ids)
    assert result["thread_id"] == (turn.started if threaded else "")
    assert events[0]["content"].get("m.relates_to", {}) == (
        {"rel_type": "m.thread", "event_id": turn.started} if threaded else {}
    )
    for event in events[1:]:
        assert event["content"]["m.relates_to"]["event_id"] == (turn.started if threaded else ids[0])


async def test_native_reply_and_mentions_keep_each_part_out_of_a_thread(turn, peer, monkeypatch):
    from belle_agent.platforms.matrix import render

    monkeypatch.setitem(render.MESSAGE_BUDGET, True, 100)
    result = await turn(
        "post_message", text="hello " * 30, reply_to_message_id=turn.started,
        reply_mode="reply", message_type="m.text", mention_user_ids=[USER], mention_room=True,
    )
    events = [event for event in peer.events.values() if event["sender"] == USER]

    assert result["ok"] and result["thread_id"] == "" and result["reply_to_message_id"] == turn.started
    assert len(events) > 1
    assert all(event["content"]["msgtype"] == "m.text" for event in events)
    assert all(event["content"]["m.relates_to"] == {"m.in_reply_to": {"event_id": turn.started}} for event in events)
    assert events[0]["content"]["m.mentions"] == {"user_ids": [USER], "room": True}
    assert all(event["content"]["m.mentions"] == {} for event in events[1:])


async def test_invalid_matrix_post_options_are_refused_before_sending(turn, peer):
    for options, code in [
        ({"reply_mode": "maybe"}, "reply_mode_invalid"),
        ({"message_type": "m.image"}, "message_type_invalid"),
        ({"mention_user_ids": ["alice"]}, "mention_user_ids_invalid"),
        ({"mention_room": "yes"}, "mention_room_invalid"),
    ]:
        assert (await turn("post_message", text="hello", **options))["error"] == code
    assert not [event for event in peer.events.values() if event["sender"] == USER]


@pytest.mark.parametrize("threaded", [False, True])
@pytest.mark.parametrize("recursive", [False, True])
@pytest.mark.parametrize("failure", ["helper", "connection"])
async def test_later_send_failure_retains_all_visible_ids(turn, peer, monkeypatch, recursive, failure, threaded):
    from belle_agent.platforms.matrix import render
    from belle_agent.platforms.matrix.helper import HelperError

    original = turn.platform.send_event
    attempts = 0
    if not recursive:
        monkeypatch.setitem(render.MESSAGE_BUDGET, True, 100)

    async def send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if recursive and attempts == 1:
            raise HelperError("M_TOO_LARGE", "server event limit")
        if attempts == (3 if recursive else 2):
            if failure == "connection":
                raise ConnectionError("helper socket closed")
            raise HelperError("M_FORBIDDEN", "send denied")
        return await original(*args, **kwargs)

    monkeypatch.setattr(turn.platform, "send_event", send)
    arguments = {"reply_to_message_id": turn.started} if threaded else {}
    result = await turn("post_message", text="words " * 30, **arguments)
    ids = [e["event_id"] for e in peer.events.values() if e["sender"] == USER]
    assert not result["ok"] and len(ids) == 1
    assert result["part_message_ids"] == ids and result["message_id"] == ids[0]
    assert result["parts"] == 1 and result["thread_id"] == (turn.started if threaded else "")
    assert "retrying the whole post can duplicate" in result["detail"]
    assert result["error"] == ("helper_restarted" if failure == "connection" else "forbidden")


@pytest.mark.parametrize("event_id", [None, "", 17, "not-an-event", "$bad id"])
async def test_invalid_send_response_never_invents_an_event_id(turn, peer, monkeypatch, event_id):
    original = peer.request

    async def request(op, **fields):
        if op == "send":
            return {"event_id": event_id}
        return await original(op, **fields)

    monkeypatch.setattr(peer, "request", request)
    result = await turn("post_message", text="hello")
    assert not result["ok"] and result["error"] == "helper_invalid_response"
    assert "message_id" not in result and "part_message_ids" not in result


@pytest.mark.parametrize("connection", [False, True])
async def test_send_failure_before_any_event_has_no_message_id(turn, monkeypatch, connection):
    from belle_agent.platforms.matrix.helper import HelperError

    async def send(*args, **kwargs):
        if connection:
            raise ConnectionError("helper socket closed")
        raise HelperError("M_FORBIDDEN", "send denied")

    monkeypatch.setattr(turn.platform, "send_event", send)
    result = await turn("post_message", text="hello")
    assert not result["ok"]
    assert "message_id" not in result and "part_message_ids" not in result
