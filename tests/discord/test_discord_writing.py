"""Discord message companions check the acting bot, requester, audience and route."""

import json

import pytest
from discord_transport import TOKEN, FakeRest

from belle_agent.backend import ToolCall
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.platforms.discord.targets import READ_HISTORY, SEND, VIEW
from belle_agent.platforms.discord.writing import ADD_REACTIONS, MANAGE_THREADS, PIN_MESSAGES
from belle_agent.tools.host import BoundTurn, ToolHost

GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
OTHER = "1000000000000000301"
MESSAGE = "1000000000000000800"
BOT = "1000000000000000116"
USER = "1000000000000000400"
NEW = "1000000000000000401"
FORUM = "1000000000000000302"
THREAD = "1000000000000000303"


@pytest.fixture
def setup():
    rest = FakeRest()
    platform = DiscordPlatform(DiscordSettings(token=TOKEN, bot="east"), client=DiscordClient(TOKEN, transport=rest.transport()))
    platform.bot_user_id = BOT
    metadata = {"discord_bot": "east", "discord_channel_id": CHANNEL, "discord_user_id": USER,
                "discord_message_id": MESSAGE, "discord_guild_id": GUILD}
    tool = platform.toolkits(lambda: metadata)[2]
    bits = VIEW | READ_HISTORY | SEND | ADD_REACTIONS | PIN_MESSAGES | MANAGE_THREADS
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
                "roles": [{"id": GUILD, "permissions": str(bits)}]}))
    for member in (BOT, USER):
        rest.answer("GET", f"/guilds/{GUILD}/members/{member}", (200, {"user": {"id": member}, "roles": []}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": member}, "roles": []} for member in (BOT, USER, NEW)]))
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {
        "id": MESSAGE, "channel_id": CHANNEL, "author": {"id": USER},
        "reactions": [{"emoji": {"name": "🔥", "id": None}}]}))
    return tool, rest, metadata


def _result(value):
    return json.loads(value)


async def test_companion_tools_are_registered_and_edit_delete_are_not_yet_exposed(setup):
    tool, _, _ = setup
    names = {card["name"] for card in tool.get_tool_definitions()}
    assert names == {"react_to_message", "read_discord_reactions", "read_pinned_messages", "pin_message", "set_discord_thread_pinned"}
    assert names <= {card["name"] for card in tool.platform.toolkits(tool.metadata_provider)[2].get_tool_definitions()}


async def test_reaction_uses_existing_emoji_and_encodes_it(setup):
    tool, rest, _ = setup
    result = _result(await tool.react_to_message("🔥"))
    assert result["ok"] and result["reacted"]
    assert rest.calls("PUT", f"/channels/{CHANNEL}/messages/{MESSAGE}/reactions/🔥/@me")
    result = _result(await tool.react_to_message("🔥", remove=True))
    assert result["ok"] and not result["reacted"]
    assert rest.calls("DELETE", f"/channels/{CHANNEL}/messages/{MESSAGE}/reactions/🔥/@me")


async def test_tool_host_routes_reaction_to_discord_writing_tool(setup):
    tool, rest, metadata = setup
    host = ToolHost([])
    host.serve("discord", tool.platform.toolkits(tool.metadata_provider))
    host.bind("conversation", BoundTurn("turn", platform="discord", metadata=metadata))

    result = _result(await host.call(ToolCall("conversation", "react_to_message", {"emoji": "✅"})))

    assert result["reacted"]
    assert rest.calls("PUT", f"/channels/{CHANNEL}/messages/{MESSAGE}/reactions/✅/@me")


async def test_new_reaction_checks_requester_and_bot_permission(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
                "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY)}]}))
    result = _result(await tool.react_to_message("✅"))
    assert result["error"] == "requester_cannot_write_target"
    assert not rest.calls("PUT", f"/channels/{CHANNEL}/messages/{MESSAGE}/reactions/✅/@me")


async def test_reaction_read_pages_users_and_checks_message(setup):
    tool, rest, _ = setup
    path = f"/channels/{CHANNEL}/messages/{MESSAGE}/reactions/🔥"
    rest.answer("GET", path, (200, [{"id": USER, "username": "alice"}]))
    result = _result(await tool.read_discord_reactions(MESSAGE, "🔥", limit=1))
    assert result["users"] == [{"user_id": USER, "username": "alice"}]
    assert result["coverage"] == {"status": "partial", "next_after": USER}
    assert _result(await tool.read_discord_reactions(MESSAGE, "🔥", after="bad"))["error"] == "after_invalid"


async def test_pins_use_current_endpoint_and_exact_message(setup):
    tool, rest, _ = setup
    path = f"/channels/{CHANNEL}/messages/pins"
    rest.answer("GET", path, (200, {"items": [{"pinned_at": "2026-10-21T00:00:00Z",
                "message": {"id": MESSAGE, "channel_id": CHANNEL, "author": {"id": USER}, "content": "hello"}}],
                "has_more": True}))
    page = _result(await tool.read_pinned_messages(limit=1))
    assert page["pins"][0]["message_id"] == MESSAGE
    assert page["coverage"] == {"status": "partial", "next_before": "2026-10-21T00:00:00Z"}
    assert _result(await tool.pin_message(MESSAGE))["pinned"]
    assert rest.calls("PUT", f"/channels/{CHANNEL}/messages/pins/{MESSAGE}")
    assert _result(await tool.pin_message(MESSAGE, remove=True))["pinned"] is False
    assert rest.calls("DELETE", f"/channels/{CHANNEL}/messages/pins/{MESSAGE}")


@pytest.mark.parametrize("same_channel", [False, True])
async def test_pins_refuse_source_viewers_without_target_history(setup, same_channel):
    tool, rest, _ = setup
    target = CHANNEL if same_channel else OTHER
    rest.answer("GET", f"/channels/{target}", (200, {
        "id": target, "type": 0, "guild_id": GUILD, "permission_overwrites": [
            {"id": NEW, "type": 1, "deny": str(READ_HISTORY), "allow": "0"}]}))

    result = _result(await tool.read_pinned_messages(chat_id=target))

    assert result["error"] == "read_would_disclose_to_other_readers"
    assert not rest.calls("GET", f"/channels/{target}/messages/pins")


async def test_wrong_bot_and_foreign_message_never_write(setup):
    tool, rest, metadata = setup
    metadata["discord_bot"] = "west"
    assert _result(await tool.pin_message(MESSAGE))["error"] == "discord_bot_mismatch"
    metadata["discord_bot"] = "east"
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": OTHER}))
    assert _result(await tool.pin_message(MESSAGE))["error"] == "target_mismatch"
    assert not rest.calls("PUT", f"/channels/{CHANNEL}/messages/pins/{MESSAGE}")


async def test_cross_audience_write_refused_without_late_approval(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD,
                "permission_overwrites": [{"id": NEW, "type": 1, "deny": str(VIEW), "allow": "0"}]}))
    rest.answer("GET", f"/channels/{OTHER}", (200, {"id": OTHER, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/channels/{OTHER}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": OTHER}))
    result = _result(await tool.pin_message(MESSAGE, chat_id=OTHER))
    assert result["error"] == "write_target_widens_audience"
    assert not rest.calls("PUT", f"/channels/{OTHER}/messages/pins/{MESSAGE}")


async def test_forum_thread_pin_preserves_other_flags_and_checks_result(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/channels/{FORUM}", (200, {"id": FORUM, "type": 15, "guild_id": GUILD}))
    rest.answer("GET", f"/channels/{THREAD}", (200, {
        "id": THREAD, "type": 11, "guild_id": GUILD, "parent_id": FORUM, "flags": 16,
    }))
    rest.answer("PATCH", f"/channels/{THREAD}", (200, {"id": THREAD, "flags": 18}))

    result = _result(await tool.set_discord_thread_pinned(True, chat_id=THREAD))

    assert result == {"ok": True, "chat_id": THREAD, "pinned": True}
    assert rest.calls("PATCH", f"/channels/{THREAD}")[0].body == {"flags": 18}


async def test_forum_thread_pin_requires_requester_manage_threads(setup):
    tool, rest, _ = setup
    rest.answer("GET", f"/channels/{FORUM}", (200, {"id": FORUM, "type": 15, "guild_id": GUILD}))
    rest.answer("GET", f"/channels/{THREAD}", (200, {
        "id": THREAD, "type": 11, "guild_id": GUILD, "parent_id": FORUM, "flags": 0,
    }))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
                "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY | PIN_MESSAGES)}]}))

    result = _result(await tool.set_discord_thread_pinned(True, chat_id=THREAD))

    assert result["error"] == "requester_cannot_write_target"
    assert not rest.calls("PATCH", f"/channels/{THREAD}")


async def test_forward_message_uses_native_reference_after_source_check(setup):
    tool, rest, metadata = setup
    post = tool.platform.toolkits(lambda: metadata)[0]
    rest.answer("POST", f"/channels/{CHANNEL}/messages", (200, {
        "id": THREAD, "channel_id": CHANNEL,
    }))

    result = _result(await post.forward_discord_message(CHANNEL, MESSAGE, CHANNEL))

    assert result["ok"] and result["message_id"] == THREAD
    body = rest.calls("POST", f"/channels/{CHANNEL}/messages")[0].body
    assert body["message_reference"] == {"type": 1, "channel_id": CHANNEL, "message_id": MESSAGE}


async def test_crosspost_requires_current_turn_confirmation(setup):
    tool, rest, metadata = setup
    post = tool.platform.toolkits(lambda: metadata)[0]
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 5, "guild_id": GUILD}))
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {
        "id": MESSAGE, "channel_id": CHANNEL, "author": {"id": BOT},
    }))
    path = f"/channels/{CHANNEL}/messages/{MESSAGE}/crosspost"
    rest.answer("POST", path, (200, {"id": MESSAGE, "channel_id": CHANNEL}))

    direct = _result(await post.crosspost_discord_message(MESSAGE))
    host = ToolHost([post])
    turn = BoundTurn("turn", platform="discord", metadata=metadata)
    host.bind("conversation", turn)
    call = ToolCall("conversation", "crosspost_discord_message", {"message_id": MESSAGE}, call_id="crosspost")
    pending = await host.authorize(call)
    confirmed = _result(await host.run_confirmed(call, turn))

    assert direct["error"] == "crosspost_confirmation_required"
    assert pending.action == "ask"
    assert confirmed["ok"] and confirmed["crossposted"]
    assert len(rest.calls("POST", path)) == 1
