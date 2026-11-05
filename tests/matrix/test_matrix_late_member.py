"""A requester reads only what Matrix let them read: in an encrypted room, nothing from before they joined."""

# ruff: noqa: F811 -- the fixtures of the tools' checks, taken as arguments.

import pytest
from peer import Room
from test_matrix_tools import ALICE, BOB, CAROL, LOUNGE, TOWN, joined, peer, turn  # noqa: F401

from belle_agent.platforms.matrix.reading import MembershipHistoryLimit, visible_since
from belle_agent.platforms.matrix.toolkit import METADATA_USER
from belle_agent.tools.inputs import iso_utc

PINS = "m.room.pinned_events"


def membership(peer, room: str, user: str, at: float, state: str = "join", *, earlier: str = "", was: str = "") -> str:
    """Put a membership event of ``user`` in the room's timeline, make it the room's state, and return its id."""
    event_id = peer.add(room, user, {"membership": state}, event_type="m.room.member", at=at)
    event = peer.events[event_id]
    event["state_key"] = user
    if earlier:
        event["unsigned"] = {"replaces_state": earlier, "prev_content": {"membership": was}}
    peer.state[(room, "m.room.member", user)] = event
    return event_id


async def test_membership_history_budget_refuses_instead_of_hiding_readable_messages(peer, turn):
    earlier = ""
    for at in range(100, 121):
        earlier = membership(peer, LOUNGE, CAROL, at, earlier=earlier, was="join" if earlier else "")
    with pytest.raises(MembershipHistoryLimit):
        await visible_since(peer.request, LOUNGE, CAROL, encrypted=True, max_events=20)
    assert await visible_since(peer.request, LOUNGE, CAROL, encrypted=True, max_events=21) == 100
    turn.metadata[METADATA_USER] = CAROL
    refused = await turn("read_conversation")
    assert refused["error"] == "membership_history_budget_exceeded"
    assert "platforms.matrix.membership_history_max_events" in refused["detail"]


async def test_search_membership_history_budget_returns_actionable_refusal(peer, turn):
    peer.state[(TOWN, "m.room.history_visibility", "")] = {"content": {"history_visibility": "joined"}}
    earlier = ""
    for at in range(100, 121):
        earlier = membership(peer, TOWN, BOB, at, earlier=earlier, was="join" if earlier else "")
    peer.add(TOWN, ALICE, {"msgtype": "m.text", "body": "zebracorn"}, at=200)

    refused = await turn("search_messages", query="zebracorn")

    assert refused["ok"] is False
    assert refused["error"] == "membership_history_budget_exceeded"
    assert "platforms.matrix.membership_history_max_events" in refused["detail"]
    assert refused["messages"] == []


@pytest.fixture
def late(peer, turn):
    """carol joined the encrypted lounge at 500, changed her name at 700, and asks. Two messages before, two after."""
    peer.timeline[LOUNGE] = []
    peer.members[LOUNGE] = joined(ALICE, BOB, CAROL)
    peer.add(LOUNGE, ALICE, {}, event_type="m.room.create", at=10)
    before = [peer.add(LOUNGE, BOB, {"msgtype": "m.text", "body": f"before {n}"}, at=100 + n) for n in range(2)]
    joined_at = membership(peer, LOUNGE, CAROL, 500, earlier=membership(peer, LOUNGE, CAROL, 400, "leave"), was="leave")
    after = [peer.add(LOUNGE, BOB, {"msgtype": "m.text", "body": "after 0"}, at=600)]
    membership(peer, LOUNGE, CAROL, 700, earlier=joined_at, was="join")
    after.append(peer.add(LOUNGE, BOB, {"msgtype": "m.text", "body": "after 1"}, at=800))
    turn.metadata[METADATA_USER] = CAROL
    return before, after


async def test_a_late_member_reads_only_what_came_after_their_join_and_is_told_the_rest_was_withheld(turn, late):
    _, after = late
    read = await turn("read_conversation")
    assert [m["message_id"] for m in read["messages"]] == after
    coverage = read["coverage"]
    assert coverage["before_requester_joined"] == "withheld" and coverage["requester_joined_iso_utc"] == iso_utc(500)
    assert "inaccessible_before_iso_utc" not in coverage


async def test_a_member_since_the_start_reads_every_message(turn, late, peer):
    before, after = late
    turn.metadata[METADATA_USER] = BOB
    read = await turn("read_conversation")
    assert [m["message_id"] for m in read["messages"]] == before + after
    assert "before_requester_joined" not in read["coverage"]


async def test_a_thread_whose_root_is_older_than_the_join_withholds_the_root(turn, late, peer):
    before, _ = late
    reply = peer.add(LOUNGE, BOB, {"body": "later", "m.relates_to": {"rel_type": "m.thread", "event_id": before[0]}}, at=900)
    read = await turn("read_conversation", message_id=reply)
    assert [m["message_id"] for m in read["messages"]] == [reply] and read["coverage"]["before_requester_joined"] == "withheld"


async def test_a_file_from_before_the_join_is_refused(turn, late, peer):
    early = peer.add(LOUNGE, BOB, {"msgtype": "m.file", "body": "a.txt", "file": {"url": "mxc://syn.example/a"}}, at=300)
    peer.media["mxc://syn.example/a"] = b"secret"
    assert (await turn("download_file", file_id=early))["error"] == "message_before_requester_joined"
    assert not [r for r in peer.requests if r["op"] == "download"]


async def test_the_pins_from_before_the_join_are_left_out_and_counted(turn, late, peer):
    before, after = late
    peer.state[(LOUNGE, PINS, "")] = {"type": PINS, "sender": ALICE, "origin_server_ts": 900, "content": {"pinned": [before[0], after[0]]}}
    pins = await turn("read_matrix_pinned_messages")
    assert [m["message_id"] for m in pins["pinned_messages"]] == [after[0]]
    coverage = pins["coverage"]
    assert coverage["pins_before_requester_joined"] == 1 and coverage["status"] == "partial"
    assert "not_readable_message_ids" not in coverage


async def test_a_member_who_is_not_joined_reads_nothing_of_an_encrypted_room(turn, late, peer):
    membership(peer, LOUNGE, CAROL, 950, "leave")
    read = await turn("read_conversation")
    assert read["messages"] == [] and read["coverage"]["requester_joined_iso_utc"] is None


@pytest.mark.parametrize(("visibility", "shown"), [("shared", 3), ("joined", 1), ("invited", 2)])
async def test_a_plain_room_bounds_a_search_by_its_history_visibility(turn, peer, visibility, shown):
    peer.rooms[TOWN] = Room(encrypted=False, public=False, name="town")
    peer.state[(TOWN, "m.room.history_visibility", "")] = {"type": "m.room.history_visibility", "content": {"history_visibility": visibility}}
    peer.add(TOWN, BOB, {"msgtype": "m.text", "body": "zebracorn early"}, at=100)
    invite = membership(peer, TOWN, CAROL, 200, "invite")
    membership(peer, TOWN, CAROL, 300, earlier=invite, was="invite")
    peer.add(TOWN, BOB, {"msgtype": "m.text", "body": "zebracorn late"}, at=400)
    peer.add(TOWN, BOB, {"msgtype": "m.text", "body": "zebracorn invited"}, at=250)
    turn.metadata[METADATA_USER] = CAROL
    found = await turn("search_messages", query="zebracorn")
    assert len(found["messages"]) == shown
    withheld = found["coverage"].get("results_before_requester_joined", 0)
    assert withheld == 3 - len(found["messages"])
    assert (withheld > 0) == ("results_before_requester_joined" in found["coverage"]["partial_reasons"])


async def test_a_match_in_a_room_name_from_before_the_join_is_no_withheld_message(turn, peer):
    peer.rooms[TOWN] = Room(encrypted=False, public=False, name="town zebracorn")
    peer.state[(TOWN, "m.room.history_visibility", "")] = {"type": "m.room.history_visibility", "content": {"history_visibility": "joined"}}
    peer.add(TOWN, BOB, {"name": "zebracorn", "body": "zebracorn"}, event_type="m.room.name", at=100)
    membership(peer, TOWN, CAROL, 300)
    turn.metadata[METADATA_USER] = CAROL
    found = await turn("search_messages", query="zebracorn")
    assert found["messages"] == [] and "results_before_requester_joined" not in found["coverage"]
