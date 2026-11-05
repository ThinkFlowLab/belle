"""Reactions, members and pins on Matrix, as the host's events."""

import time

import pytest
from peer import USER, Peer, Room

from belle_agent.platforms.base import InboundEvent, InboundStop
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.settings import MatrixSettings

ROOM = "!lounge:syn.example"
BOB = "@bob:syn.example"


@pytest.fixture
def world():
    peer = Peer()
    peer.rooms[ROOM] = Room(encrypted=True)
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p",
                              typing_indicator=False, bot_user_ids=frozenset({"@bot:syn.example"}))
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    platform._helper, platform.user_id = peer, USER
    got: list = []

    async def receive(record):
        got.append(record)
        return f"took:{len(got)}"

    platform._on_inbound = receive
    platform.got, platform.peer = got, peer
    return platform


async def deliver(platform, event_type: str, content: dict, *, sender: str = BOB, **fields) -> str:
    event_id = f"$e{time.monotonic_ns()}"
    room_event = {"type": event_type, "sender": sender, "event_id": event_id, "origin_server_ts": int(time.time() * 1000),
                  "content": content, **fields}
    platform.last = event_id
    return await platform.handle({"event": "timeline", "seq": 1, "room": ROOM, "encrypted": False, "direct": False, "room_event": room_event})


def annotation(target: str, key: str) -> dict:
    return {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}}


async def test_a_reaction_and_its_removal_are_events_about_the_message_in_its_thread(world):
    world.peer.events["$reply"] = {"type": "m.room.message", "sender": BOB, "event_id": "$reply",
                                   "content": {"body": "x", "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}}
    await deliver(world, "m.reaction", annotation("$reply", "\N{PARTY POPPER}"))
    reaction = world.last
    await deliver(world, "m.room.redaction", {}, redacts=reaction)

    added, removed = world.got
    assert isinstance(added, InboundEvent) and (added.kind, added.actor, added.anchor, added.anchor_thread) == (
        "reaction", BOB, "$reply", "$root")
    assert added.event_id == reaction and added.chat_type == "private"
    assert added.record == {"family": "reaction", "event_type": "reaction_added", "key": "\N{PARTY POPPER}",
                            "message_id": "$reply", "author_user_id": BOB, "chat_id": ROOM}
    assert (removed.record["event_type"], removed.anchor) == ("reaction_removed", "$reply")


async def test_a_redaction_of_no_reaction_seen_and_what_the_bot_did_are_no_events(world):
    assert await deliver(world, "m.room.redaction", {}, redacts="$unknown") == "ignored:redaction-of-no-reaction-seen"
    assert await deliver(world, "m.reaction", annotation("$m", "\N{EYES}"), sender=USER) == "ignored:done-by-this-bot"
    assert world.got == []


async def test_the_stop_reaction_is_a_stop_and_no_event(world):
    await deliver(world, "m.reaction", annotation("$m", "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}"))
    assert [type(record) for record in world.got] == [InboundStop]


async def test_a_change_of_members_names_who_did_it_and_whom_it_is_about_and_a_profile_change_is_none(world):
    await deliver(world, "m.room.member", {"membership": "leave"}, state_key="@carol:syn.example",
                  unsigned={"prev_content": {"membership": "join"}})
    assert await deliver(world, "m.room.member", {"membership": "join", "displayname": "Bobby"}, state_key=BOB,
                         unsigned={"prev_content": {"membership": "join"}}) == "ignored:profile-change"

    (kicked,) = world.got
    assert (kicked.kind, kicked.actor, kicked.anchor) == ("member", BOB, "")
    assert kicked.record["membership"] == "leave" and kicked.record["previous_membership"] == "join"
    assert kicked.record["user_id"] == "@carol:syn.example"


async def test_a_pin_names_each_message_added_or_removed_and_who_pinned(world):
    await deliver(world, "m.room.pinned_events", {"pinned": ["$a", "$b"]}, state_key="",
                  unsigned={"prev_content": {"pinned": ["$a", "$c"]}})

    added, removed = world.got
    assert (added.kind, added.actor, added.anchor, added.record["event_type"]) == ("pin", BOB, "$b", "pin_added")
    assert (removed.anchor, removed.record["event_type"]) == ("$c", "pin_removed")
    assert added.event_id == f"{world.last}:$b"


async def test_a_pin_that_changes_no_message_is_handed_over_naming_none(world):
    await deliver(world, "m.room.pinned_events", {"pinned": ["$a"]}, state_key="", unsigned={"prev_content": {"pinned": ["$a"]}})
    (pin,) = world.got
    assert pin.anchor == "" and pin.kind == "pin"


async def test_an_actor_named_as_a_bot_is_marked_so(world):
    await deliver(world, "m.reaction", annotation("$m", "\N{EYES}"), sender="@bot:syn.example")
    assert world.got[0].actor_is_bot


async def test_a_turn_is_told_the_event_that_started_it_and_where_to_answer(world):
    await deliver(world, "m.reaction", annotation("$m", "\N{EYES}"))
    text = world.render_events({"events": []}, world.got[0])
    assert "[matrix_event]" in text and f"[chat_id: {ROOM}, message_id: $m] [trigger: event]" in text
    assert "[matrix_events]" in text and "reply_to_message_id" in text
