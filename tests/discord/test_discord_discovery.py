"""Named discovery checks visibility, API semantics, audience and continuations."""

import base64
import json

import pytest
from test_discord_reading import BOT, CHANNEL, GUILD, NEW, PRIVATE, USER

from belle_agent.platforms.discord.targets import READ_HISTORY, VIEW

TAG = "1000000000000000550"
THREAD = "1000000000000000551"


def channels(rest, rows):
    rest.answer("GET", f"/guilds/{GUILD}/channels", (200, rows))
    for row in rows:
        rest.answer("GET", f"/channels/{row['id']}", (200, row))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (USER, BOT)]))


def record(ident=PRIVATE, kind=15, **extra):
    return {"id": ident, "guild_id": GUILD, "type": kind, "name": "planning", **extra}


@pytest.mark.parametrize("channel_type", [0, 2, 4, 5, 13, 14, 15, 16])
async def test_discovery_covers_visible_channel_types_without_history_or_connect(read_setup, channel_type):
    tool, rest, _ = read_setup
    channels(rest, [record(kind=channel_type)])
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
        "roles": [{"id": GUILD, "permissions": str(VIEW)}]}))

    result = json.loads(await tool.find_by_name("plan", kind="channel"))

    assert result["matches"][0] == {"id": PRIVATE, "chat_id": PRIVATE, "kind": "channel", "guild_id": GUILD,
        "name": "planning", "parent_id": "", "parent_chat_id": "", "type": channel_type,
        "match_type": "prefix", "archived": False}
    assert result["coverage"]["status"] == "complete"
    assert not rest.calls("GET", f"/channels/{PRIVATE}/messages")


@pytest.mark.parametrize("other_child", [False, True])
@pytest.mark.parametrize("source_is_child", [False, True])
async def test_category_discovery_uses_child_visibility_for_each_source_reader(read_setup, source_is_child, other_child):
    tool, rest, _ = read_setup
    category = record(kind=4, permission_overwrites=[
        {"type": 0, "id": GUILD, "allow": "0", "deny": str(VIEW)}])
    child = record(THREAD, 0, parent_id=PRIVATE, permission_overwrites=[
        {"type": 0, "id": GUILD, "allow": "0", "deny": str(VIEW)},
        *({"type": 1, "id": user, "allow": str(VIEW), "deny": "0"} for user in (USER, BOT))])
    source = record(CHANNEL, 0, parent_id=PRIVATE if source_is_child else None,
                    permission_overwrites=[] if not source_is_child else child["permission_overwrites"])
    rows = [category, child, source]
    if other_child:
        rows.append(record(TAG, 0, parent_id=PRIVATE, permission_overwrites=[
            {"type": 0, "id": GUILD, "allow": "0", "deny": str(VIEW)},
            {"type": 1, "id": NEW, "allow": str(VIEW), "deny": "0"}]))
    channels(rest, rows)
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (USER, BOT, NEW)]))

    result = json.loads(await tool.find_by_name(PRIVATE, kind="channel"))

    assert result["coverage"]["status"] == "complete"
    assert [item["id"] for item in result["matches"]] == ([PRIVATE] if source_is_child or other_child else [])


@pytest.mark.parametrize("channel_type", [15, 16])
async def test_tag_discovery_returns_parent_and_ambiguous_matches(read_setup, channel_type):
    tool, rest, _ = read_setup
    channels(rest, [record(kind=channel_type, available_tags=[
        {"id": TAG, "name": "Plan"}, {"id": THREAD, "name": "Planning"}])])

    result = json.loads(await tool.find_by_name("plan", kind="tag"))

    assert [item["id"] for item in result["matches"]] == [TAG, THREAD]
    assert all(item["parent_id"] == PRIVATE for item in result["matches"])
    assert [item["match_type"] for item in result["matches"]] == ["exact", "prefix"]


async def test_invisible_or_unsafe_forum_tags_are_not_returned(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(available_tags=[{"id": TAG, "name": "plan"}], permission_overwrites=[
        {"type": 1, "id": USER, "allow": "0", "deny": str(VIEW)}])])

    result = json.loads(await tool.find_by_name("plan", kind="tag"))

    assert result["matches"] == []
    assert result["coverage"]["kinds"]["tag"]["status"] == "permission_denied"
    assert PRIVATE not in json.dumps(result)


async def test_channel_results_page_without_dropping_ambiguity(read_setup):
    tool, rest, metadata = read_setup
    channels(rest, [record(CHANNEL, 0), record(PRIVATE)])

    first = json.loads(await tool.find_by_name("plan", kind="channel", limit=1))
    cursor = first["coverage"]["next_cursor"]
    second = json.loads(await tool.find_by_name("plan", kind="channel", limit=1, cursor=cursor))

    assert [item["id"] for item in first["matches"] + second["matches"]] == [CHANNEL, PRIVATE]
    assert second["coverage"]["next_cursor"] is None
    assert json.loads(await tool.find_by_name("other", kind="channel", cursor=cursor))["error"] == "cursor_invalid"
    metadata["discord_user_id"] = NEW
    assert json.loads(await tool.find_by_name("plan", kind="channel", cursor=cursor))["error"] == "cursor_invalid"


@pytest.mark.parametrize("cursor", ["bad!", "W10=", "bnVsbA==", "e30=", 123])
async def test_cursor_validation_refuses_malformed_positions_before_remote_calls(read_setup, cursor):
    tool, rest, _ = read_setup
    result = json.loads(await tool.find_by_name("plan", cursor=cursor))
    assert result["error"] == "cursor_invalid"
    assert not rest.requests


@pytest.mark.parametrize("resource, endpoint", [("role", "roles"), ("emoji", "emojis"), ("sticker", "stickers")])
async def test_guild_resource_discovery_uses_listing_and_exact_ids(read_setup, resource, endpoint):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/{endpoint}", (200, [{"id": TAG, "name": "planning"}]))

    result = json.loads(await tool.find_by_name(TAG, kind=resource))

    assert result["matches"][0]["match_type"] == "id"
    assert result["matches"][0]["kind"] == resource
    assert result["coverage"]["status"] == "complete"


async def test_member_prefix_search_does_not_claim_substring_matching(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/members/search", (200, [
        {"user": {"id": TAG, "username": "planning"}},
        {"user": {"id": THREAD, "username": "supplyplan"}},
        {"user": {"id": PRIVATE, "username": "alice"}, "nick": "Planner"}]))

    result = json.loads(await tool.find_by_name("plan", kind="member"))

    assert [item["id"] for item in result["matches"]] == [TAG, PRIVATE]
    info = result["coverage"]["kinds"]["member"]["sources"][0]
    assert info["match_semantics"] == "username_or_nickname_prefix"
    assert info["dm_index"] == "unavailable"
    assert not rest.calls("GET", f"/guilds/{GUILD}/members")


async def test_exact_member_id_uses_resource_lookup(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/members/{TAG}", (200, {"user": {"id": TAG, "username": "alice"}}))

    result = json.loads(await tool.find_by_name(TAG, kind="member"))

    assert result["matches"][0]["id"] == TAG
    assert not rest.calls("GET", f"/guilds/{GUILD}/members/search")


async def test_member_api_ceiling_reports_unavailable_continuation(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/members/search", (200, [
        {"user": {"id": str(int(TAG) + index), "username": f"plan{index}"}} for index in range(1000)]))

    result = json.loads(await tool.find_by_name("plan", kind="member", limit=1000))

    assert len(result["matches"]) == 1000
    assert result["coverage"]["kinds"]["member"]["status"] == "unavailable"
    assert result["coverage"]["next_cursor"] is None
    assert "narrow query" in result["coverage"]["kinds"]["member"]["sources"][0]["reason"]


async def test_directory_refuses_source_readers_outside_target_guild(read_setup):
    tool, rest, _ = read_setup
    other_guild = "1000000000000000999"
    rest.answer("GET", f"/guilds/{other_guild}", (200, {"id": other_guild, "owner_id": "owner",
        "roles": [{"id": other_guild, "permissions": str(VIEW)}]}))
    for user in (BOT, USER):
        rest.answer("GET", f"/guilds/{other_guild}/members/{user}", (200, {"user": {"id": user}, "roles": []}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (USER, BOT, NEW)]))
    rest.answer("GET", f"/guilds/{other_guild}/members/{NEW}", (404, {}))

    result = json.loads(await tool.find_by_name("plan", kind="role", guild_id=other_guild))

    assert result["matches"] == []
    assert result["coverage"]["kinds"]["role"]["status"] == "permission_denied"
    assert not rest.calls("GET", f"/guilds/{other_guild}/roles")


async def test_guild_directory_from_thread_keeps_visibility_audience(read_setup):
    tool, rest, metadata = read_setup
    metadata["discord_thread_id"] = THREAD
    rest.answer("GET", f"/channels/{THREAD}", (200, record(THREAD, 11, parent_id=CHANNEL)))
    rest.answer("GET", f"/channels/{CHANNEL}", (200, record(CHANNEL, 0, permission_overwrites=[
        {"id": NEW, "type": 1, "deny": str(READ_HISTORY), "allow": "0"}])))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)]))
    rest.answer("GET", f"/guilds/{GUILD}/roles", (200, [{"id": TAG, "name": "planning"}]))

    result = json.loads(await tool.find_by_name("plan", kind="role"))

    assert [item["id"] for item in result["matches"]] == [TAG]
    assert result["coverage"]["status"] == "complete"


async def test_forum_archived_discovery_reuses_thread_pagination(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record()])
    raw = record(THREAD, 11, parent_id=PRIVATE,
                 thread_metadata={"archived": True, "archive_timestamp": "2026-10-21T00:00:00Z"})
    rest.answer("GET", f"/channels/{THREAD}", (200, raw))
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": []}))
    rest.answer("GET", f"/channels/{PRIVATE}/threads/archived/public", (200, {"threads": [raw], "has_more": True}),
                (200, {"threads": [], "has_more": False}))
    rest.answer("GET", f"/channels/{THREAD}/thread-members/{USER}", (200, {"user_id": USER}))
    rest.answer("GET", f"/channels/{THREAD}/thread-members/{BOT}", (404, {}))

    first = json.loads(await tool.find_by_name("plan", kind="thread"))
    second = json.loads(await tool.find_by_name("plan", kind="thread", cursor=first["coverage"]["next_cursor"]))

    assert first["matches"][0]["archived"] is True
    assert first["matches"][0]["membership"] == {"requester": "joined", "bot": "not_joined"}
    assert first["coverage"]["kinds"]["thread"]["status"] == "still_paged"
    assert second["coverage"]["status"] == "complete"
    assert len(rest.calls("GET", f"/channels/{PRIVATE}/threads/archived/public")) == 2


async def test_private_discovery_uses_joined_fallback_and_reports_gap(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(CHANNEL, 0)])
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": []}))
    rest.answer("GET", f"/channels/{CHANNEL}/threads/archived/public", (200, {"threads": [], "has_more": False}))
    rest.answer("GET", f"/channels/{CHANNEL}/users/@me/threads/archived/private", (200, {"threads": [], "has_more": False}))

    result = json.loads(await tool.find_by_name("plan", kind="thread"))

    assert result["coverage"]["status"] == "partial"
    info = result["coverage"]["kinds"]["thread"]["sources"][-1]
    assert info["source"] == "joined_private_archived"
    assert info["status"] == "permission_denied"
    assert not rest.calls("GET", f"/channels/{CHANNEL}/threads/archived/private")


async def test_all_kinds_are_reported_when_broad_discovery_is_paged(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(CHANNEL, 0)])

    result = json.loads(await tool.find_by_name("plan", limit=1))

    assert set(result["coverage"]["kinds"]) == {"channel", "thread", "tag", "member", "role", "emoji", "sticker"}
    assert result["coverage"]["kinds"]["member"]["status"] == "still_paged"


async def test_member_search_failure_is_permission_denied_not_complete_empty(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/guilds/{GUILD}/members/search", (403, {}))

    result = json.loads(await tool.find_by_name("plan", kind="member"))

    assert result["matches"] == []
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["kinds"]["member"]["status"] == "permission_denied"


async def test_bot_denied_forum_and_source_audience_denied_tags_are_filtered(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(available_tags=[{"id": TAG, "name": "plan"}], permission_overwrites=[
        {"type": 1, "id": BOT, "allow": "0", "deny": str(VIEW)}])])
    denied_bot = json.loads(await tool.find_by_name("plan", kind="tag"))
    assert denied_bot["matches"] == []
    channels(rest, [record(available_tags=[{"id": TAG, "name": "plan"}], permission_overwrites=[
        {"type": 1, "id": NEW, "allow": "0", "deny": str(VIEW)}])])
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (USER, BOT, NEW)]))

    denied_audience = json.loads(await tool.find_by_name("plan", kind="tag"))

    assert denied_audience["matches"] == []
    assert denied_audience["coverage"]["kinds"]["tag"]["sources"][0]["reason"] == "read_would_disclose_to_other_readers"


async def test_full_private_archive_is_used_once_with_both_manage_grants(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(CHANNEL, 0)])
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
        "roles": [{"id": GUILD, "permissions": str(VIEW | (1 << 16) | (1 << 34))}]}))
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": []}))
    rest.answer("GET", f"/channels/{CHANNEL}/threads/archived/public", (200, {"threads": [], "has_more": False}))
    rest.answer("GET", f"/channels/{CHANNEL}/threads/archived/private", (200, {"threads": [], "has_more": False}))

    result = json.loads(await tool.find_by_name("plan", kind="thread"))

    assert result["coverage"]["status"] == "complete"
    assert rest.calls("GET", f"/channels/{CHANNEL}/threads/archived/private")
    assert not rest.calls("GET", f"/channels/{CHANNEL}/users/@me/threads/archived/private")


async def test_continuation_preserves_previous_permission_denials(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(CHANNEL, 0)])
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": []}))
    rest.answer("GET", f"/channels/{CHANNEL}/threads/archived/public", (200, {"threads": [], "has_more": False}))
    rest.answer("GET", f"/channels/{CHANNEL}/users/@me/threads/archived/private", (200, {"threads": [], "has_more": False}))
    rest.answer("GET", f"/guilds/{GUILD}/members/search", (200, []))
    for endpoint in ("roles", "emojis", "stickers"):
        rest.answer("GET", f"/guilds/{GUILD}/{endpoint}", (200, [{"id": TAG, "name": "plan"}]))

    first = json.loads(await tool.find_by_name("plan", limit=2))
    assert first["coverage"]["kinds"]["thread"]["status"] == "permission_denied"
    second = json.loads(await tool.find_by_name("plan", limit=100, cursor=first["coverage"]["next_cursor"]))

    assert second["coverage"]["next_cursor"] is None
    assert second["coverage"]["kinds"]["thread"]["status"] == "permission_denied"
    assert second["coverage"]["status"] == "partial"


@pytest.mark.parametrize("kind", ["channel", "tag"])
async def test_missing_audience_evidence_is_unavailable_not_complete(read_setup, kind):
    tool, rest, _ = read_setup
    channels(rest, [record(available_tags=[{"id": TAG, "name": "plan"}])])
    rest.answer("GET", f"/guilds/{GUILD}/members", (403, {}))

    result = json.loads(await tool.find_by_name("plan", kind=kind))

    assert result["matches"] == []
    assert result["coverage"]["status"] == "partial"
    info = result["coverage"]["kinds"][kind]
    assert info["status"] == "unavailable"
    assert info["sources"][0]["reason"] == "read_audience_evidence_unavailable"
    assert info["sources"][0].get("excluded_resources", 0) == 0


async def test_thread_discovery_preserves_native_unavailable_audience_coverage(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record()])
    raw = record(THREAD, 11, parent_id=PRIVATE, thread_metadata={"archived": False})
    rest.answer("GET", f"/channels/{THREAD}", (200, raw))
    rest.answer("GET", f"/guilds/{GUILD}/threads/active", (200, {"threads": [raw]}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (403, {}))

    result = json.loads(await tool.find_by_name("plan", kind="thread"))

    assert result["matches"] == []
    assert result["coverage"]["status"] == "partial"
    info = result["coverage"]["kinds"]["thread"]
    assert info["status"] == "unavailable"
    active = next(source for source in info["sources"] if source["source"] == "active")
    assert active["status"] == "unavailable"
    assert active["unavailable_threads"] == 1
    assert active["excluded_threads"] == 0


async def test_continuation_preserves_missing_audience_coverage(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(PRIVATE), record(CHANNEL, 0), record(THREAD)])
    rest.answer("GET", f"/guilds/{GUILD}/members", (403, {}))

    first = json.loads(await tool.find_by_name("plan", kind="channel", limit=1))
    assert [match["id"] for match in first["matches"]] == [CHANNEL]
    assert first["coverage"]["kinds"]["channel"]["status"] == "unavailable"
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (USER, BOT)]))
    second = json.loads(await tool.find_by_name("plan", kind="channel", cursor=first["coverage"]["next_cursor"]))

    assert [match["id"] for match in second["matches"]] == [THREAD]
    assert second["coverage"]["next_cursor"] is None
    assert second["coverage"]["status"] == "partial"
    assert second["coverage"]["kinds"]["channel"]["status"] == "unavailable"


@pytest.mark.parametrize("field,value", [
    ("source", 1), ("offset", 999), ("before", "2026-10-21T00:00:00Z"),
    ("unavailable", {"channel": "unavailable"}), ("binding", []),
])
async def test_cursor_rejects_modified_payload_before_remote_calls(read_setup, field, value):
    tool, rest, _ = read_setup
    channels(rest, [record(CHANNEL, 0), record(PRIVATE)])
    first = json.loads(await tool.find_by_name("plan", kind="channel", limit=1))
    payload, signature = first["coverage"]["next_cursor"].rsplit(".", 1)
    position = json.loads(base64.urlsafe_b64decode(payload))
    position[field] = value
    changed = base64.urlsafe_b64encode(json.dumps(position).encode()).decode() + "." + signature
    rest.requests.clear()

    result = json.loads(await tool.find_by_name("plan", kind="channel", cursor=changed))

    assert result["error"] == "cursor_invalid"
    assert not rest.requests


async def test_cursor_rejects_modified_signature_and_unsigned_payload(read_setup):
    tool, rest, _ = read_setup
    channels(rest, [record(CHANNEL, 0), record(PRIVATE)])
    first = json.loads(await tool.find_by_name("plan", kind="channel", limit=1))
    payload, signature = first["coverage"]["next_cursor"].rsplit(".", 1)
    changed_signature = ("0" if signature[0] != "0" else "1") + signature[1:]
    rest.requests.clear()

    for cursor in (payload, payload + "." + changed_signature):
        result = json.loads(await tool.find_by_name("plan", kind="channel", cursor=cursor))
        assert result["error"] == "cursor_invalid"
    assert not rest.requests


async def test_cursor_after_process_key_change_requires_search_restart(read_setup, monkeypatch):
    from belle_agent.platforms.discord import discovery

    tool, rest, _ = read_setup
    channels(rest, [record(CHANNEL, 0), record(PRIVATE)])
    first = json.loads(await tool.find_by_name("plan", kind="channel", limit=1))
    monkeypatch.setattr(discovery, "_CURSOR_KEY", b"different-process-key")
    rest.requests.clear()

    expired = json.loads(await tool.find_by_name("plan", kind="channel", cursor=first["coverage"]["next_cursor"]))

    assert expired["error"] == "cursor_invalid"
    assert "omit cursor" in expired["detail"]
    assert not rest.requests
    restarted = json.loads(await tool.find_by_name("plan", kind="channel"))
    assert [match["id"] for match in restarted["matches"]] == [CHANNEL, PRIVATE]
