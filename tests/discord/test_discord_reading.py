"""Discord read tools check both readers and describe incomplete results."""

import json

import pytest
from discord_transport import TOKEN, FakeRest

from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.platforms.discord.targets import CONNECT, READ_HISTORY, VIEW

GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
PRIVATE = "1000000000000000301"
MESSAGE = "1000000000000000800"
BOT = "1000000000000000116"
USER = "1000000000000000400"
FILE = "1000000000000000900"
NEW = "1000000000000000401"


@pytest.fixture
def read_setup():
    rest = FakeRest()
    platform = DiscordPlatform(DiscordSettings(token=TOKEN, bot="east"), client=DiscordClient(TOKEN, transport=rest.transport()))
    platform.bot_user_id = BOT
    metadata = {"discord_bot": "east", "discord_channel_id": CHANNEL,
                "discord_guild_id": GUILD, "discord_user_id": USER}
    tool = platform.toolkits(lambda: metadata)[1]
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
        "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY)}]}))
    for user in (BOT, USER):
        rest.answer("GET", f"/guilds/{GUILD}/members/{user}",
                    (200, {"user": {"id": user}, "roles": []}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER)]))
    rest.answer("GET", f"/guilds/{GUILD}/channels", (200, [{"id": CHANNEL, "type": 0, "guild_id": GUILD}]))
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": []}))
    return tool, rest, metadata


def _message(text="hello", channel=CHANNEL, **extra):
    return {"id": MESSAGE, "channel_id": channel, "author": {"id": USER},
            "content": text, "timestamp": "2026-10-21T00:00:00Z", **extra}


async def test_one_and_paged_message_read_reports_coverage(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message()))
    result = json.loads(await tool.read_discord_conversation(message_id=MESSAGE))
    assert result["messages"][0]["content"] == "hello"
    assert result["messages"][0]["url"].endswith(f"/{CHANNEL}/{MESSAGE}")
    assert result["coverage"]["status"] == "single_message"
    rest.answer("GET", f"/channels/{CHANNEL}/messages", (200, [_message()]))
    result = json.loads(await tool.read_discord_conversation(limit=1))
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["order"] == "newest_first"
    assert result["coverage"]["next_before_message_id"] == MESSAGE
    assert result["coverage"]["older_boundary_exhausted"] is False


async def test_voice_channel_read_needs_view_history_and_connect(read_setup):
    tool, rest, metadata = read_setup
    voice = "1000000000000000333"
    metadata["discord_channel_id"] = voice
    rest.answer("GET", f"/channels/{voice}", (200, {"id": voice, "type": 2, "guild_id": GUILD}))
    rest.answer("GET", f"/channels/{voice}/messages/{MESSAGE}", (200, _message(channel=voice)))

    denied = json.loads(await tool.read_discord_conversation(chat_id=voice, message_id=MESSAGE))
    assert denied["error"] == "requester_cannot_read_target"

    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
        "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY | (1 << 20))}]}))
    result = json.loads(await tool.read_discord_conversation(chat_id=voice, message_id=MESSAGE))

    assert result["messages"][0]["chat_id"] == voice
    assert not rest.calls("GET", f"/channels/{voice}/messages")


@pytest.mark.parametrize("source_voice", [False, True])
@pytest.mark.parametrize("target_voice", [False, True])
async def test_cross_channel_voice_reads_accept_equal_audiences(read_setup, source_voice, target_voice):
    tool, rest, _ = read_setup
    for channel, voice in ((CHANNEL, source_voice), (PRIVATE, target_voice)):
        rest.answer("GET", f"/channels/{channel}", (200, {
            "id": channel, "type": 2 if voice else 0, "guild_id": GUILD}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
        "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY | CONNECT)}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER)]))
    rest.answer("GET", f"/channels/{PRIVATE}/messages/{MESSAGE}", (200, _message(channel=PRIVATE)))
    result = json.loads(await tool.read_discord_conversation(chat_id=PRIVATE, message_id=MESSAGE))
    assert result["messages"][0]["chat_id"] == PRIVATE


async def test_cross_channel_voice_read_excludes_members_without_connect(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 2,
        "guild_id": GUILD, "permission_overwrites": [
            {"id": NEW, "type": 1, "deny": str(CONNECT), "allow": "0"}]}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
        "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY | CONNECT)}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)]))
    result = json.loads(await tool.read_discord_conversation(chat_id=PRIVATE, message_id=MESSAGE))
    assert result["error"] == "read_would_disclose_to_other_readers"
    assert not rest.calls("GET", f"/channels/{PRIVATE}/messages/{MESSAGE}")


async def test_tool_host_lists_and_routes_reads(read_setup):
    from belle_agent.backend import ToolCall
    from belle_agent.tools.host import BoundTurn, ToolHost

    tool, rest, metadata = read_setup
    platform = tool.platform
    host = ToolHost([])
    host.serve("discord", platform.toolkits(lambda: metadata))
    host.bind("conversation", BoundTurn("turn", platform="discord", metadata=metadata))
    assert {item["name"] for item in host.definitions("conversation")} >= {
        "post_message", "read_discord_conversation", "search_discord_guild", "find_by_name",
        "download_discord_attachment"}
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message()))
    result = json.loads(await host.call(ToolCall("conversation", "read_discord_conversation", {"message_id": MESSAGE})))
    assert result["messages"][0]["content"] == "hello"


async def test_read_refuses_missing_history_and_wrong_bot(read_setup):
    tool, rest, metadata = read_setup
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
        "roles": [{"id": GUILD, "permissions": str(VIEW)}]}))
    assert json.loads(await tool.read_discord_conversation())["error"] == "requester_cannot_read_target"
    assert not rest.calls("GET", f"/channels/{CHANNEL}/messages")
    metadata["discord_bot"] = "west"
    assert json.loads(await tool.read_discord_conversation())["error"] == "discord_bot_mismatch"


@pytest.mark.parametrize("user,label", [(USER, "requester"), (BOT, "bot")])
async def test_read_still_requires_history_for_requester_and_bot(read_setup, user, label):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {
        "id": CHANNEL, "type": 0, "guild_id": GUILD, "permission_overwrites": [
            {"id": user, "type": 1, "deny": str(READ_HISTORY), "allow": "0"}]}))

    result = json.loads(await tool.read_discord_conversation(message_id=MESSAGE))

    assert result["error"] == f"{label}_cannot_read_target"
    assert not rest.calls("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}")


async def test_private_thread_needs_exact_membership(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 12,
        "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{PRIVATE}/thread-members/{USER}", (404, {}))
    assert json.loads(await tool.read_discord_conversation(chat_id=PRIVATE))["error"] == "requester_cannot_read_target"
    assert not rest.calls("GET", f"/channels/{PRIVATE}/messages")


async def test_cross_channel_read_refuses_disclosure_to_source_readers(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 0,
        "guild_id": GUILD, "permission_overwrites": [
            {"id": NEW, "type": 1, "deny": str(VIEW), "allow": "0"}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)]))
    result = json.loads(await tool.read_discord_conversation(chat_id=PRIVATE))
    assert result["error"] == "read_would_disclose_to_other_readers"
    assert not rest.calls("GET", f"/channels/{PRIVATE}/messages")


@pytest.mark.parametrize("same_channel", [False, True])
async def test_hosted_history_read_excludes_source_viewers_without_target_history(read_setup, same_channel):
    from belle_agent.backend import ToolCall
    from belle_agent.tools.host import BoundTurn, ToolHost

    tool, rest, metadata = read_setup
    target = CHANNEL if same_channel else PRIVATE
    rest.answer("GET", f"/channels/{target}", (200, {"id": target, "type": 0,
        "guild_id": GUILD, "permission_overwrites": [
            {"id": NEW, "type": 1, "deny": str(READ_HISTORY), "allow": "0"}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)]))
    rest.answer("GET", f"/channels/{target}/messages/{MESSAGE}", (200, _message(
        "Historical content unavailable to one source viewer", channel=target)))
    host = ToolHost([])
    host.serve("discord", tool.platform.toolkits(lambda: metadata))
    host.bind("conversation", BoundTurn("turn", platform="discord", metadata=metadata))

    result = json.loads(await host.call(ToolCall("conversation", "read_discord_conversation", {
        "chat_id": target, "message_id": MESSAGE,
    })))

    assert result["error"] == "read_would_disclose_to_other_readers"
    assert not rest.calls("GET", f"/channels/{target}/messages/{MESSAGE}")


async def test_cross_channel_read_requires_audience_evidence(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (403, {}))
    assert json.loads(await tool.read_discord_conversation(chat_id=PRIVATE))["error"] == "read_audience_evidence_unavailable"


async def test_search_indexing_and_private_hits_are_not_returned(read_setup):
    tool, rest, _ = read_setup
    path = f"/guilds/{GUILD}/messages/search"
    rest.answer("GET", path, (202, {"code": 110000, "retry_after": 2}))
    indexed = json.loads(await tool.search_discord_guild({"content": "plan"}))
    assert indexed["coverage"]["status"] == "indexing" and indexed["coverage"]["retry_after_seconds"] == 2
    rest.answer("GET", path, (200, {"messages": [[_message("secret", PRIVATE)]], "total_results": 2}))
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 12,
        "guild_id": GUILD, "parent_id": CHANNEL}))
    rest.answer("GET", f"/channels/{PRIVATE}/thread-members/{USER}", (404, {}))
    result = json.loads(await tool.search_discord_guild({"content": "plan"}))
    assert result["messages"] == []
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["readable_source_audience_only"] is True
    assert "withheld_messages" not in result["coverage"]
    assert "reported_total_results" not in result["coverage"]
    assert PRIVATE not in json.dumps(result)


async def test_search_checks_every_returned_channel_and_names_cursor(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/messages/search",
                (200, {"messages": [[_message()]], "total_results": 26}))
    result = json.loads(await tool.search_discord_guild({"content": "hello"}))
    assert result["messages"][0]["message_id"] == MESSAGE
    assert result["coverage"]["next_offset"] == 25
    invalid = json.loads(await tool.search_discord_guild({"channel_ids": ["not-an-id"]}))
    assert invalid["error"] == "filters_invalid"


async def test_search_counts_hits_within_channel_groups(read_setup):
    tool, rest, _ = read_setup
    messages = [_message(f"hello {index}", id=str(int(MESSAGE) + index)) for index in range(10)]
    rest.answer("GET", f"/guilds/{GUILD}/messages/search",
                (200, {"messages": [messages], "total_results": len(messages)}))

    result = json.loads(await tool.search_discord_guild({"content": "hello", "limit": 25}))

    assert len(result["messages"]) == 10
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["next_offset"] == 25
    assert result["coverage"]["complete"] is False


async def test_name_discovery_only_returns_visible_channels(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/channels", (200, [
        {"id": CHANNEL, "type": 0, "guild_id": GUILD, "name": "planning"},
        {"id": PRIVATE, "type": 0, "guild_id": GUILD, "name": "planning-secret"}]))
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": []}))
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 0, "guild_id": GUILD,
        "permission_overwrites": [{"type": 1, "id": USER, "deny": str(VIEW), "allow": "0"}]}))
    result = json.loads(await tool.find_by_name("planning", kind="channel"))
    assert [item["chat_id"] for item in result["matches"]] == [CHANNEL]
    assert result["coverage"]["kinds"]["channel"]["status"] == "searched"


async def test_attachment_download_refetches_parent_and_returns_text(read_setup):
    tool, rest, _ = read_setup
    url = "https://cdn.discordapp.com/attachments/fresh.txt"
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message(attachments=[
        {"id": FILE, "filename": "fresh.txt", "size": 5, "content_type": "text/plain", "url": url}])))
    rest.answer("GET", "/attachments/fresh.txt", (200, b"fresh"))
    result = json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))
    assert result["content"] == "fresh" and result["attachment_id"] == FILE
    assert rest.calls("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}")


async def test_attachment_rejects_wrong_id_and_oversize_before_cdn(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message(attachments=[])))
    assert json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))["error"] == "attachment_not_in_message"


async def test_expired_attachment_url_is_a_refusal(read_setup):
    tool, rest, _ = read_setup
    url = "https://cdn.discordapp.com/attachments/expired.txt"
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message(attachments=[
        {"id": FILE, "filename": "expired.txt", "size": 5, "url": url}])))
    rest.answer("GET", "/attachments/expired.txt", (403, {"code": 0}))
    assert json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))["error"] == "forbidden"


async def test_message_content_disabled_reports_missing_guild_content(read_setup):
    tool, rest, _ = read_setup
    tool.platform._settings = DiscordSettings(token=TOKEN, bot="east", message_content=False)
    assert "search_discord_guild" not in {card["name"] for card in tool.get_tool_definitions()}
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message("")))
    result = json.loads(await tool.read_discord_conversation(message_id=MESSAGE))
    assert result["coverage"]["status"] == "content_unavailable"


@pytest.mark.parametrize("cursor_field", ["before_message_id", "after_message_id", "around_message_id"])
async def test_history_accepts_synthetic_cursors_and_orders_directionally(read_setup, cursor_field):
    tool, rest, _ = read_setup
    later = str(int(MESSAGE) + 1)
    rest.answer("GET", f"/channels/{CHANNEL}/messages", (200, [_message(), _message(id=later)]))
    result = json.loads(await tool.read_discord_conversation(**{cursor_field: "0"}, limit=2))
    assert [item["message_id"] for item in result["messages"]] == [later, MESSAGE]
    coverage = result["coverage"]
    if cursor_field != "before_message_id":
        assert coverage["next_after_message_id"] == later
    if cursor_field != "after_message_id":
        assert coverage["next_before_message_id"] == MESSAGE
    if cursor_field == "around_message_id":
        assert coverage["older_boundary_exhausted"] is None
        assert coverage["newer_boundary_exhausted"] is None


@pytest.mark.parametrize("value", ["-1", "18446744073709551616", "", 0, "1.0"])
async def test_history_rejects_invalid_cursor_before_network(read_setup, value):
    tool, rest, _ = read_setup
    result = json.loads(await tool.read_discord_conversation(after_message_id=value))
    assert result["error"] == "cursor_invalid"
    assert not rest.calls("GET", f"/channels/{CHANNEL}")


async def test_history_defaults_to_thread_and_projects_components_poll_and_forward(read_setup):
    tool, rest, metadata = read_setup
    metadata["discord_thread_id"] = PRIVATE
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 11,
        "guild_id": GUILD, "parent_id": CHANNEL}))
    extra = {"components": [{"type": 1}], "poll": {"question": {"text": "Tea?"}},
             "sticker_items": [{"id": FILE}], "message_snapshots": [{"message": {"content": "copied"}}],
             "type": 0, "edited_timestamp": None, "mentions": [{"id": BOT}]}
    rest.answer("GET", f"/channels/{PRIVATE}/messages/{MESSAGE}", (200, _message("", channel=PRIVATE, **extra)))
    result = json.loads(await tool.read_discord_conversation(message_id=MESSAGE))
    assert result["chat_id"] == PRIVATE
    assert all(result["messages"][0][key] == value for key, value in extra.items())
    assert result["messages"][0]["projection"]["raw_message"] is False
    assert result["coverage"]["status"] == "single_message"


async def test_history_full_retained_history_pages_more_than_100_messages(read_setup):
    tool, rest, _ = read_setup
    path = f"/channels/{CHANNEL}/messages"
    rest.answer("GET", path, (200, [_message(id=str(int(MESSAGE) + index)) for index in range(100)]),
                (200, [_message(id=str(int(MESSAGE) - 1))]))
    first = json.loads(await tool.read_discord_conversation(limit=100))
    second = json.loads(await tool.read_discord_conversation(
        before_message_id=first["coverage"]["next_before_message_id"], limit=100))
    assert len(first["messages"]) + len(second["messages"]) == 101
    assert second["coverage"]["older_boundary_exhausted"] is True


async def test_attachment_available_without_message_content_intent(read_setup):
    tool, rest, _ = read_setup
    tool.platform._settings = DiscordSettings(token=TOKEN, bot="east", message_content=False)
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message("", attachments=[
        {"id": FILE, "filename": "own.txt", "size": 3, "url": "https://cdn.discordapp.com/attachments/own.txt"}])))
    rest.answer("GET", "/attachments/own.txt", (200, b"own"))
    result = json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))
    assert result["content"] == "own"


async def test_attachment_withheld_and_belle_size_limit(read_setup):
    tool, rest, _ = read_setup
    tool.platform._settings = DiscordSettings(token=TOKEN, bot="east", message_content=False, file_download_max_bytes=3)
    path = f"/channels/{CHANNEL}/messages/{MESSAGE}"
    rest.answer("GET", path, (200, _message("", attachments=[])))
    assert json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))["error"] == "content_unavailable"
    rest.answer("GET", path, (200, _message("", attachments=[{"id": FILE, "size": 4}])))
    result = json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))
    assert result["error"] == "file_over_size_limit"
    assert "file_download_max_bytes is 3" in result["detail"]


async def test_search_schema_matches_runtime_filters(read_setup):
    tool, _, _ = read_setup
    card = next(card for card in tool.get_tool_definitions() if card["name"] == "search_discord_guild")
    fields = card["input_params"]["properties"]["filters"]["properties"]
    assert fields["limit"]["maximum"] == 25
    assert fields["offset"]["maximum"] == 9975
    assert fields["channel_ids"]["maxItems"] == 500
    assert "-poll" in fields["has"]["items"]["enum"]
    assert fields["min_id"]["pattern"] == "^[0-9]{17,20}$"
    assert fields["channel_ids"]["items"]["pattern"] == fields["min_id"]["pattern"]
    assert json.loads(await tool.search_discord_guild({"has": ["nonsense"]}))["error"] == "filters_invalid"


async def test_search_underfull_page_still_continues_and_accepts_file_filters(read_setup):
    tool, rest, _ = read_setup
    path = f"/guilds/{GUILD}/messages/search"
    rest.answer("GET", path, (200, {"messages": [], "total_results": 0}),
                (200, {"messages": [[_message()]], "total_results": 1}))
    filters = {"has": ["file"], "attachment_extensions": ["txt"]}
    first = json.loads(await tool.search_discord_guild(filters))
    assert first["coverage"]["next_offset"] == 25
    second = json.loads(await tool.search_discord_guild({**filters, "offset": first["coverage"]["next_offset"]}))
    assert second["messages"][0]["message_id"] == MESSAGE
    assert second["coverage"]["offset"] == 25


async def test_search_guild_index_includes_archived_hits_without_channel_preflight(read_setup):
    tool, rest, metadata = read_setup
    metadata["discord_thread_id"] = PRIVATE
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 11,
        "guild_id": GUILD, "parent_id": CHANNEL, "thread_metadata": {"archived": True}}))
    rest.answer("GET", f"/guilds/{GUILD}/messages/search", (200, {
        "messages": [[_message(channel=PRIVATE)]], "total_results": 1}))
    result = json.loads(await tool.search_discord_guild({"offset": 9975}))
    assert result["messages"][0]["chat_id"] == PRIVATE
    assert result["coverage"]["offset_limit_reached"] is True
    assert result["coverage"]["next_offset"] is None
    assert not rest.calls("GET", f"/guilds/{GUILD}/channels")
    assert not rest.calls("GET", f"/guilds/{GUILD}/threads/active")


async def test_search_only_discloses_context_for_safe_hits(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/messages/search", (200, {
        "messages": [[_message()]], "total_results": 1,
        "threads": [{"id": CHANNEL, "name": "safe"}, {"id": PRIVATE, "name": "secret"}],
        "members": [{"id": CHANNEL, "user_id": BOT}, {"id": PRIVATE, "user_id": BOT}]}))
    result = json.loads(await tool.search_discord_guild({}))
    assert result["threads"] == [{"id": CHANNEL, "name": "safe"}]
    assert result["members"] == [{"id": CHANNEL, "user_id": BOT}]


async def test_attachment_refreshes_expired_signed_url_on_next_call(read_setup):
    tool, rest, _ = read_setup
    path = f"/channels/{CHANNEL}/messages/{MESSAGE}"
    rest.answer("GET", path,
        (200, _message(attachments=[{"id": FILE, "size": 3, "filename": "fresh.txt",
                                    "url": "https://cdn.discordapp.com/attachments/old.txt"}])),
        (200, _message(attachments=[{"id": FILE, "size": 3, "filename": "fresh.txt",
                                    "url": "https://cdn.discordapp.com/attachments/new.txt"}])))
    rest.answer("GET", "/attachments/old.txt", (403, {}))
    rest.answer("GET", "/attachments/new.txt", (200, b"new"))
    assert json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))["error"] == "forbidden"
    assert json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))["content"] == "new"
    assert len(rest.calls("GET", path)) == 2


async def test_attachment_enforces_actual_response_byte_limit(read_setup):
    tool, rest, _ = read_setup
    tool.platform._settings = DiscordSettings(token=TOKEN, bot="east", file_download_max_bytes=3)
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message(attachments=[
        {"id": FILE, "size": 3, "filename": "large.txt", "url": "https://cdn.discordapp.com/attachments/large.txt"}])))
    rest.answer("GET", "/attachments/large.txt", (200, b"large"))
    result = json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))
    assert result["error"] == "file_over_size_limit"
    assert "file_download_max_bytes (3 bytes)" in result["detail"]


async def test_attachment_at_configured_byte_limit_is_read_whole(read_setup):
    tool, rest, _ = read_setup
    tool.platform._settings = DiscordSettings(token=TOKEN, bot="east", file_download_max_bytes=3)
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, _message(attachments=[
        {"id": FILE, "size": 3, "filename": "exact.txt", "url": "https://cdn.discordapp.com/attachments/exact.txt"}])))
    rest.answer("GET", "/attachments/exact.txt", (200, b"yes"))

    result = json.loads(await tool.download_discord_attachment(CHANNEL, MESSAGE, FILE))

    assert result["content"] == "yes"


async def test_search_excludes_denied_channels_before_request(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/channels", (200, [{"id": PRIVATE, "type": 0}]))
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {"id": PRIVATE, "type": 0,
        "guild_id": GUILD, "permission_overwrites": [{"id": USER, "type": 1, "deny": str(VIEW), "allow": "0"}]}))
    result = json.loads(await tool.search_discord_guild({"channel_ids": [PRIVATE]}))
    assert result["error"] == "requester_cannot_read_target"
    assert not rest.calls("GET", f"/guilds/{GUILD}/messages/search")


async def test_search_permission_race_omits_hidden_ids_and_context(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{PRIVATE}", (403, {}))
    rest.answer("GET", f"/guilds/{GUILD}/messages/search", (200, {
        "messages": [[_message("secret", channel=PRIVATE)]], "total_results": 123,
        "threads": [{"id": PRIVATE, "name": "secret"}],
        "members": [{"id": PRIVATE, "user_id": BOT}]}))
    result = json.loads(await tool.search_discord_guild({}))
    assert result["messages"] == result["threads"] == result["members"] == []
    assert PRIVATE not in json.dumps(result)
    assert "123" not in json.dumps(result)
