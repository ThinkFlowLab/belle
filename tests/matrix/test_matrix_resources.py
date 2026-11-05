"""Alias, hierarchy and state reads keep their access boundaries."""

from test_matrix_tools import LOUNGE, OTHER, TOWN

pytest_plugins = ["test_matrix_tools"]


async def test_alias_resolution_does_not_join_or_claim_read_access(turn, peer) -> None:
    peer.joined = {LOUNGE, TOWN, OTHER}
    peer.aliases["#elsewhere:syn.example"] = "!elsewhere:syn.example"
    result = await turn("resolve_matrix_alias", alias="#elsewhere:syn.example")
    assert result == {"ok": True, "alias": "#elsewhere:syn.example", "room_id": "!elsewhere:syn.example",
                      "servers": ["syn.example"], "joined": False, "read_access_checked": False}
    assert not any(request["op"] == "join" for request in peer.requests)


async def test_space_hierarchy_pages_and_hides_private_children(turn, peer) -> None:
    peer.hierarchy[LOUNGE] = [
        {"room_id": TOWN, "name": "Town", "join_rule": "public"},
        {"room_id": OTHER, "name": "Private", "join_rule": "invite"},
        {"room_id": "!outside:syn.example", "name": "Outside", "join_rule": "public"},
    ]
    first = await turn("list_matrix_space", limit=2)
    assert [room["room_id"] for room in first["rooms"]] == [TOWN]
    assert first["coverage"] == {"status": "partial", "hidden_for_audience": 1, "next_token": "2"}
    second = await turn("list_matrix_space", limit=2, next_token="2")
    assert [room["room_id"] for room in second["rooms"]] == ["!outside:syn.example"]
    assert second["coverage"]["next_token"] is None


async def test_state_read_returns_event_without_write(turn, peer) -> None:
    event = {"type": "m.room.topic", "state_key": "", "content": {"topic": "Town"}}
    peer.state[(LOUNGE, "m.room.topic", "")] = event
    result = await turn("read_matrix_state", event_type="m.room.topic")
    assert result["event"] == event
    assert not any(request["op"] == "state_put" for request in peer.requests)


async def test_state_read_refuses_foreign_private_room(turn, peer) -> None:
    result = await turn("read_matrix_state", event_type="m.room.topic", chat_id=OTHER)
    assert not result["ok"]
    assert not any(request["op"] == "state_get" and request["room"] == OTHER for request in peer.requests)


async def test_resource_cards_match_tools(turn) -> None:
    names = {card["name"] for card in turn.toolkit.get_tool_definitions()}
    assert {"resolve_matrix_alias", "list_matrix_space", "read_matrix_state"} <= names
