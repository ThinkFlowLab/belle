"""Guild resource reads keep requester and conversation access intact."""

import json

from test_discord_reading import BOT, CHANNEL, GUILD, USER

from belle_agent.platforms.discord.targets import VIEW

pytest_plugins = ["test_discord_reading"]

EVENT = "1000000000000000801"
SOUND = "1000000000000000901"
VOICE = "1000000000000000302"
OTHER = "1000000000000000402"


async def test_resource_cards_are_registered(read_setup) -> None:
    tool, _, _ = read_setup
    names = {card["name"] for card in tool.get_tool_definitions()}
    assert {"list_discord_scheduled_events", "get_discord_scheduled_event", "list_discord_event_users",
            "list_discord_soundboard_sounds", "get_discord_soundboard_sound", "list_discord_default_sounds"} <= names


async def test_event_list_filters_channel_not_visible_to_source(read_setup) -> None:
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, OTHER)]))
    rest.answer("GET", f"/channels/{VOICE}", (200, {"id": VOICE, "type": 2, "guild_id": GUILD,
                "permission_overwrites": [{"id": OTHER, "type": 1, "allow": "0", "deny": str(VIEW)}]}))
    rest.answer("GET", f"/guilds/{GUILD}/scheduled-events", (200, [
        {"id": EVENT, "guild_id": GUILD, "channel_id": VOICE, "entity_type": 2},
        {"id": "1000000000000000802", "guild_id": GUILD, "channel_id": None, "entity_type": 3},
    ]))
    result = json.loads(await tool.list_discord_scheduled_events())
    assert [item["id"] for item in result["events"]] == ["1000000000000000802"]
    assert result["coverage"]["hidden_for_audience"] == 1


async def test_event_detail_and_subscribers_page(read_setup) -> None:
    tool, rest, _ = read_setup
    event = {"id": EVENT, "guild_id": GUILD, "channel_id": CHANNEL, "entity_type": 2}
    rest.answer("GET", f"/guilds/{GUILD}/scheduled-events/{EVENT}", (200, event))
    rest.answer("GET", f"/guilds/{GUILD}/scheduled-events/{EVENT}/users", (200, [
        {"guild_scheduled_event_id": EVENT, "user": {"id": USER}}]))
    result = json.loads(await tool.get_discord_scheduled_event(EVENT))
    assert result["event"] == event
    page = json.loads(await tool.list_discord_event_users(EVENT, limit=1))
    assert page["users"][0]["user"]["id"] == USER
    assert page["coverage"]["next_after"] == USER
    assert json.loads(await tool.list_discord_event_users(EVENT, before=USER, after=BOT))["error"] == "cursor_conflict"


async def test_soundboard_reads_and_missing_grant(read_setup) -> None:
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/soundboard-sounds", (200, {"items": [
        {"sound_id": SOUND, "guild_id": GUILD, "name": "ding"}]}))
    rest.answer("GET", f"/guilds/{GUILD}/soundboard-sounds/{SOUND}", (200, {
        "sound_id": SOUND, "guild_id": GUILD, "name": "ding"}))
    rest.answer("GET", "/soundboard-default-sounds", (200, [{"sound_id": "1", "name": "quack"}]))
    assert json.loads(await tool.list_discord_soundboard_sounds())["sounds"][0]["name"] == "ding"
    assert json.loads(await tool.get_discord_soundboard_sound(SOUND))["sound"]["name"] == "ding"
    assert json.loads(await tool.list_discord_default_sounds())["sounds"][0]["name"] == "quack"
    rest.answer("GET", f"/guilds/{GUILD}/members/{USER}", (403, {"code": 50001, "message": "Missing Access"}))
    denied = json.loads(await tool.list_discord_soundboard_sounds())
    assert not denied["ok"]


async def test_cross_guild_request_from_server_channel_is_refused(read_setup) -> None:
    tool, rest, _ = read_setup
    other_guild = "1000000000000000201"
    denied = json.loads(await tool.list_discord_scheduled_events(guild_id=other_guild))
    assert denied["error"] == "read_would_disclose_to_other_readers"
    assert not rest.calls("GET", f"/guilds/{other_guild}/scheduled-events")


async def test_malformed_resource_id_stops_before_rest(read_setup) -> None:
    tool, rest, _ = read_setup
    result = json.loads(await tool.get_discord_scheduled_event(event_id="bad"))
    assert not result["ok"]
    assert not rest.calls("GET", f"/guilds/{GUILD}/scheduled-events/bad")
