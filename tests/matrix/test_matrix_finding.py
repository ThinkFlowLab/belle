"""The Matrix tools that find things, on the peer helper: search, find by name, pin and read pins, each with its coverage."""

# ruff: noqa: F811 -- the fixtures of the posting tools' checks, taken as arguments.

import pytest
from peer import Room
from test_matrix_tools import ALICE, BOB, CAROL, LOUNGE, OTHER, TOWN, joined, peer, turn  # noqa: F401

PINS = "m.room.pinned_events"
OUTSIDE = "!outside:syn.example"


@pytest.fixture
def rooms(peer):
    """town is public and plain, lounge encrypted, other private and plain with carol, who is not in the lounge."""
    peer.rooms[TOWN].name = "town"
    peer.rooms[LOUNGE].name = "lounge"
    peer.rooms[OTHER] = Room(encrypted=False)
    peer.members[OTHER] = joined(CAROL)
    peer.public_directory.extend([
        {"room_id": TOWN, "name": "town", "num_joined_members": 4},
        {"room_id": OUTSIDE, "name": "outside plaza", "canonical_alias": "#outside:syn.example", "num_joined_members": 9},
    ])
    return peer


async def test_a_search_finds_the_plain_rooms_and_names_each_room_it_could_not_search_with_its_reason(turn, rooms):
    rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "the zebracorn is here"}, at=100)
    rooms.add(LOUNGE, BOB, {"msgtype": "m.text", "body": "a secret zebracorn"}, at=200)
    found = await turn("search_messages", query="zebracorn")
    (message,) = found["messages"]
    assert message["text"] == "the zebracorn is here" and message["chat_id"] == TOWN and message["chat_name"] == "town"
    assert message["chat_type"] == "public" and message["permalink"].startswith("https://matrix.to/#/%21town")
    coverage = found["coverage"]
    assert coverage["channels"] == "partial" and coverage["older_matches"] == "none"
    assert set(coverage["partial_reasons"]) == {"encrypted_channels_not_searched", "channels_not_joined"}
    assert {"chat_id": LOUNGE, "chat_name": "lounge", "reason": "encrypted"} in coverage["channels_not_searched"]
    assert {"chat_id": OUTSIDE, "chat_name": "outside plaza", "reason": "not_joined"} in coverage["channels_not_searched"]
    assert {"chat_id": TOWN, "chat_name": "town"} in coverage["channels_searched"]


async def test_search_coverage_budgets_can_reach_later_public_rooms(turn, rooms):
    rooms.public_directory.extend(
        {"room_id": f"!public{index}:syn.example", "name": f"public {index}"}
        for index in range(1000)
    )
    limited = await turn("search_messages", query="zebracorn", max_public_pages=1, max_unsearched_rooms=1)
    assert "channel_list_incomplete" in limited["coverage"]["partial_reasons"]
    assert "max_public_pages" in limited["coverage"]["channel_list_note"]
    assert "max_unsearched_rooms" in limited["coverage"]["unsearched_list_note"]

    complete = await turn("search_messages", query="zebracorn", max_public_pages=11, max_unsearched_rooms=1002)
    assert "channel_list_incomplete" not in complete["coverage"]["partial_reasons"]
    assert len(complete["coverage"]["channels_not_searched"]) == 1002


async def test_invalid_search_coverage_budget_is_refused(turn, rooms):
    assert (await turn("search_messages", query="zebracorn", max_public_pages=0))["error"] == "invalid_max_public_pages"
    assert (await turn("search_messages", query="zebracorn", max_unsearched_rooms=0))["error"] == "invalid_max_unsearched_rooms"


async def test_a_match_in_a_room_this_conversation_cannot_read_is_withheld_and_its_room_is_not_named(turn, rooms):
    rooms.add(OTHER, CAROL, {"msgtype": "m.text", "body": "zebracorn for carol"})
    found = await turn("search_messages", query="zebracorn")
    coverage = found["coverage"]
    assert found["messages"] == [] and coverage["results_withheld"] == 1 and "results_withheld" in coverage["partial_reasons"]
    assert OTHER not in str(coverage) and coverage["channels_unnamed"] == 1


async def test_a_search_with_more_matches_than_one_page_names_the_token_of_the_next(turn, rooms):
    for n in range(60):
        rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": f"zebracorn {n}"}, at=1000 + n)
    first = await turn("search_messages", query="zebracorn")
    assert len(first["messages"]) == 50 and first["messages"][0]["text"] == "zebracorn 59"
    assert first["coverage"]["older_matches"] == "more_available" and first["coverage"]["server_match_count"] == 60
    rest = await turn("search_messages", query="zebracorn", page_token=first["coverage"]["next_page_token"])
    assert [m["text"] for m in rest["messages"]][-1] == "zebracorn 0" and rest["coverage"]["older_matches"] == "none"


async def test_search_selectors_and_state_results_preserve_resource_kind(turn, rooms):
    rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "zebracorn message"}, at=100)
    rooms.add(TOWN, ALICE, {"name": "zebracorn hall"}, event_type="m.room.name", at=200)
    rooms.add(OTHER, CAROL, {"msgtype": "m.text", "body": "zebracorn elsewhere"}, at=300)

    found = await turn("search_messages", query="zebracorn", keys=["content.name"],
                       room_ids=[TOWN], event_types=["m.room.name"])

    assert found["messages"] == []
    assert [(item["kind"], item["type"], item["chat_id"]) for item in found["state_matches"]] == [
        ("room_state", "m.room.name", TOWN),
    ]
    assert found["coverage"]["results_returned"] == 1
    assert found["coverage"]["state_matches_returned"] == 1
    request = next(request for request in reversed(rooms.requests) if request["op"] == "search")
    assert request["keys"] == ["content.name"] and request["rooms"] == [TOWN]
    assert request["types"] == ["m.room.name"]


async def test_search_page_token_is_bound_to_selectors(turn, rooms):
    for number in range(60):
        rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": f"zebracorn {number}"}, at=number + 100)
    first = await turn("search_messages", query="zebracorn", room_ids=[TOWN])

    refused = await turn("search_messages", query="zebracorn", room_ids=[OTHER],
                         page_token=first["coverage"]["next_page_token"])

    assert refused["error"] == "invalid_page_token"


@pytest.mark.parametrize("tool", ["search_messages", "find_by_name"])
async def test_non_utf8_page_token_is_refused(turn, rooms, tool):
    kwargs = {"query": "town", "page_token": "/w=="}
    if tool == "find_by_name":
        kwargs["kind"] = "chat"
    assert (await turn(tool, **kwargs))["error"] == "invalid_page_token"


@pytest.mark.parametrize(("selectors", "texts"), [
    ({"sender_ids": [ALICE]}, ["zebracorn alice"]),
    ({"exclude_sender_ids": [ALICE]}, ["zebracorn bob"]),
    ({"room_ids": [TOWN]}, ["zebracorn bob", "zebracorn alice"]),
    ({"exclude_room_ids": [TOWN]}, []),
    ({"exclude_event_types": ["m.room.message"]}, []),
])
async def test_search_filters_reach_the_helper_and_change_matches(turn, rooms, selectors, texts):
    rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "zebracorn alice"}, at=100)
    rooms.add(TOWN, BOB, {"msgtype": "m.text", "body": "zebracorn bob"}, at=200)

    found = await turn("search_messages", query="zebracorn", **selectors)

    assert [item["text"] for item in found["messages"]] == texts
    request = next(request for request in reversed(rooms.requests) if request["op"] == "search")
    assert request[{"sender_ids": "senders", "exclude_sender_ids": "not_senders", "room_ids": "rooms",
                    "exclude_room_ids": "not_rooms", "exclude_event_types": "not_types"}[next(iter(selectors))]] == next(iter(selectors.values()))


async def test_search_context_and_order_options_reach_the_helper(turn, rooms):
    rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "before"}, at=100)
    rooms.add(TOWN, BOB, {"msgtype": "m.text", "body": "zebracorn"}, at=200)
    rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "after"}, at=300)

    found = await turn("search_messages", query="zebracorn", order_by="rank",
                       context_before=1, context_after=1, include_profiles=True)

    assert [item["text"] for item in found["messages"][0]["context"]["events_before"]] == ["before"]
    assert [item["text"] for item in found["messages"][0]["context"]["events_after"]] == ["after"]
    request = next(request for request in reversed(rooms.requests) if request["op"] == "search")
    assert (request["order_by"], request["before_limit"], request["after_limit"], request["include_profile"]) == (
        "rank", 1, 1, True,
    )


async def test_search_context_omits_profiles_of_messages_before_requester_join(turn, rooms):
    rooms.state[(TOWN, "m.room.history_visibility", "")] = {"content": {"history_visibility": "joined"}}
    rooms.state[(TOWN, "m.room.member", BOB)] = {
        "origin_server_ts": 150_000, "content": {"membership": "join"},
    }
    rooms.add(TOWN, CAROL, {"msgtype": "m.text", "body": "earlier"}, at=100)
    rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "zebracorn"}, at=200)
    rooms.add(TOWN, BOB, {"msgtype": "m.text", "body": "later"}, at=300)

    found = await turn("search_messages", query="zebracorn", room_ids=[TOWN],
                       context_before=1, context_after=1, include_profiles=True)

    context = found["messages"][0]["context"]
    assert context["events_before"] == []
    assert [event["text"] for event in context["events_after"]] == ["later"]
    assert set(context["profile_info"]) == {ALICE, BOB}


async def test_invalid_search_selector_is_refused_before_the_helper(turn, rooms):
    found = await turn("search_messages", query="zebracorn", keys=["content.not_a_key"])
    assert found["error"] == "invalid_search_key"
    assert not any(request["op"] == "search" for request in rooms.requests)


async def test_a_match_in_an_edit_is_shown_as_the_message_it_edits_and_says_so(turn, rooms):
    original = rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "first words"}, at=100)
    edit = {"msgtype": "m.text", "body": "* zebracorn words", "m.new_content": {"msgtype": "m.text", "body": "zebracorn words"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": original}}
    edit_id = rooms.add(TOWN, ALICE, edit, at=200)
    (message,) = (await turn("search_messages", query="zebracorn"))["messages"]
    assert message["message_id"] == original and message["matched_in_edit_message_id"] == edit_id


async def test_an_empty_query_is_refused(turn, rooms):
    assert (await turn("search_messages", query="  "))["error"] == "query_required"


async def test_find_by_name_finds_people_in_the_directory_and_rooms_joined_or_public(turn, rooms):
    rooms.directory.extend([{"user_id": ALICE, "display_name": "Alice Liddell"}, {"user_id": "@malice:syn.example"}])
    found = await turn("find_by_name", query="@alice:syn.example")
    assert [m["id"] for m in found["matches"]] == [ALICE, "@malice:syn.example"] and found["matches"][0]["matched"] == "id"
    chats = await turn("find_by_name", query="outside", kind="chat")
    (match,) = chats["matches"]
    assert match["id"] == OUTSIDE and match["is_member"] is False and match["alias"] == "#outside:syn.example"
    (town,) = (await turn("find_by_name", query="#town", kind="chat"))["matches"]
    assert town["is_member"] is True and town["is_private"] is False and town["match_type"] == "exact"
    assert "chat_scope_note" in chats["coverage"] and chats["coverage"]["kinds_searched"] == ["chat"]


async def test_a_directory_the_server_cut_short_is_reported_as_searched_in_part(turn, rooms):
    rooms.directory.extend({"user_id": f"@ann{n}:syn.example"} for n in range(60))
    found = await turn("find_by_name", query="ann", kind="person")
    assert found["coverage"]["scanned"]["person"]["directory_complete"] is False
    assert "directory_scan_incomplete" in found["coverage"]["partial_reasons"]


async def test_user_directory_limit_can_reach_a_match_after_the_default_limit(turn, rooms):
    rooms.directory.extend({"user_id": f"@ann{n:02}:syn.example"} for n in range(60))
    found = await turn("find_by_name", query="ann", kind="person", directory_limit=60)
    assert len(found["matches"]) == 60
    assert found["coverage"]["scanned"]["person"]["directory_complete"] is True
    assert (await turn("find_by_name", query="ann", kind="person"))["coverage"]["person_limit_note"]


async def test_public_room_pages_continue_without_skipping_or_repeating(turn, rooms):
    rooms.public_directory.extend(
        {"room_id": f"!plaza{n:02}:syn.example", "name": f"plaza {n:02}"} for n in range(55)
    )
    seen = []
    token = None
    for _ in range(6):
        args = {"page_token": token} if token else {}
        result = await turn("find_by_name", query="plaza", kind="chat", directory_limit=10, **args)
        seen.extend(match["id"] for match in result["matches"])
        token = result["coverage"].get("next_page_token")
        if not token:
            break
    assert len(seen) == 56 and len(set(seen)) == 56
    assert token is None


async def test_room_directory_cursor_binds_query_and_server(turn, rooms):
    rooms.public_directory.extend(
        {"room_id": f"!plaza{n}:syn.example", "name": f"plaza {n}"} for n in range(3)
    )
    first = await turn("find_by_name", query="plaza", kind="chat", directory_limit=1,
                       directory_server="remote.example")
    token = first["coverage"]["next_page_token"]
    assert (await turn("find_by_name", query="other", kind="chat", directory_limit=1,
                       directory_server="remote.example", page_token=token))["error"] == "invalid_page_token"
    assert (await turn("find_by_name", query="plaza", kind="chat", directory_limit=1,
                       directory_server="syn.example", page_token=token))["error"] == "invalid_page_token"
    await turn("find_by_name", query="plaza", kind="chat", directory_limit=1,
               directory_server="remote.example", page_token=token)
    assert any(request.get("server") == "remote.example" for request in rooms.requests if request["op"] == "public_rooms")


async def test_unavailable_remote_directory_is_not_an_empty_result(turn, rooms):
    result = await turn("find_by_name", query="plaza", kind="chat", directory_server="missing.example")
    assert result["error"] == "not_found"


async def test_find_by_name_refuses_a_kind_matrix_does_not_have(turn, rooms):
    refused = await turn("find_by_name", query="party", kind="emoji")
    assert refused["error"] == "unknown_kind" and "person, chat" in refused["detail"]


async def test_a_pin_is_added_once_and_comes_off_again(turn, rooms):
    assert (await turn("pin_message", message_id=turn.started))["pinned"] is True
    assert (await turn("pin_message", message_id=turn.started))["ok"]
    assert rooms.state[(LOUNGE, PINS, "")]["content"] == {"pinned": [turn.started]}
    assert [r["op"] for r in rooms.requests].count("state_put") == 1
    assert (await turn("pin_message", message_id=turn.started, remove=True))["pinned"] is False
    assert rooms.state[(LOUNGE, PINS, "")]["content"] == {"pinned": []}


async def test_a_pin_the_room_does_not_permit_is_refused_with_both_power_levels(turn, rooms):
    rooms.state_forbidden.add(LOUNGE)
    rooms.state[(LOUNGE, "m.room.power_levels", "")] = {"content": {"users": {BOB: 100}, "state_default": 50}}
    refused = await turn("pin_message", message_id=turn.started)
    assert refused["error"] == "pin_not_permitted"
    assert refused["required_power_level"] == 50 and refused["bot_power_level"] == 0 and "power level 50" in refused["detail"]


async def test_a_pin_of_a_message_of_another_room_is_refused(turn, rooms):
    elsewhere = rooms.add(TOWN, ALICE, {"msgtype": "m.text", "body": "there"})
    assert (await turn("pin_message", message_id=elsewhere))["error"] == "message_not_found"
    assert (await turn("pin_message", message_id="nope"))["error"] == "message_id_malformed"


async def test_the_pins_are_listed_newest_first_with_what_could_not_be_read(turn, rooms):
    old = rooms.add(LOUNGE, ALICE, {"msgtype": "m.text", "body": "old"}, at=100)
    new = rooms.add(LOUNGE, BOB, {"msgtype": "m.text", "body": "new"}, at=300)
    secret = rooms.add(LOUNGE, CAROL, {}, undecryptable=True, at=200)
    rooms.state[(LOUNGE, PINS, "")] = {"type": PINS, "sender": ALICE, "origin_server_ts": 400_000,
                                       "content": {"pinned": [old, "$gone", new, secret]}}
    found = await turn("read_matrix_pinned_messages")
    assert [m["message_id"] for m in found["pinned_messages"]] == [new, secret, old]
    assert found["pinned_messages"][1]["undecryptable"] is True
    coverage = found["coverage"]
    assert coverage["status"] == "partial" and coverage["undecryptable"] == 1 and coverage["not_readable_message_ids"] == ["$gone"]
    assert coverage["pins_changed_by_user_id"] == ALICE and coverage["pinned_count"] == 4


async def test_a_room_with_no_pins_lists_none_and_is_complete(turn, rooms):
    found = await turn("read_matrix_pinned_messages", chat_id=TOWN)
    assert found["pinned_messages"] == [] and found["coverage"]["status"] == "complete" and found["chat_name"] == "town"


async def test_the_pins_of_a_room_this_conversation_cannot_read_are_refused(turn, rooms):
    refused = await turn("read_matrix_pinned_messages", chat_id=OTHER)
    assert refused["ok"] is False and refused["pinned_messages"] == []
    assert not any(r["op"] == "state_get" and r["room"] == OTHER for r in rooms.requests)


def test_the_toolkit_serves_each_finding_card_once(turn):
    names = [card["name"] for card in turn.toolkit.get_tool_definitions()]
    for name in ("search_messages", "find_by_name", "pin_message", "read_matrix_pinned_messages"):
        assert names.count(name) == 1
