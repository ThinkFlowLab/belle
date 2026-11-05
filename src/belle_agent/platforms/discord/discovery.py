"""Guild resource discovery with explicit coverage and resumable archive pages."""

import base64
import binascii
import hmac
import json
import secrets
from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE
from belle_agent.platforms.discord.targets import (
    MANAGE_THREADS,
    THREAD_TYPES,
    VIEW,
    TargetResolver,
    permissions,
    visibility_channels,
)
from belle_agent.tools.host import refusal
from belle_agent.tools.lookup import lookup_query, matches_in

KINDS = ("channel", "thread", "tag", "member", "role", "emoji", "sticker")
CHANNEL_TYPES = (0, 2, 4, 5, 13, 14, 15, 16)
# Authenticate claimed search coverage; a process restart invalidates outstanding cursors.
_CURSOR_KEY = secrets.token_bytes(32)


async def _visible(tool: Any, ident: str, guild_id: str, requester: str) -> Mapping[str, Any]:
    """Discovery needs View Channel; message history and Connect are separate capabilities."""
    path = f"/channels/{ident}"
    channel: Mapping[str, Any] = await tool.platform._client.mapping("GET", path)
    if str(channel.get("id")) != ident or str(channel.get("guild_id")) != guild_id:
        raise DiscordError("target_mismatch", "GET", path)
    guild, members = await tool._guild_members(guild_id, requester)
    visible_channels = await visibility_channels(tool.platform._client, channel)
    try:
        for user, label in ((requester, "requester"), (tool.platform.bot_user_id, "bot")):
            if not any(permissions(guild, candidate, members[user]) & VIEW for candidate in visible_channels):
                raise DiscordError(f"{label}_cannot_read_target", "GET", path)
    except (KeyError, TypeError, ValueError):
        raise DiscordError("bad_response", "GET", path) from None
    metadata = tool.metadata_provider()
    source = str(metadata.get("discord_thread_id") or metadata.get("discord_channel_id") or "")
    if not source:
        raise DiscordError("source_conversation_required", "GET", path)
    if source != ident and metadata.get("discord_channel_type") != "dm":
        resolver = TargetResolver(tool.platform)
        try:
            source_readers = (await resolver.resolve(source, requester, posting=False, visibility_only=True)).readers
            target_readers = (await resolver.resolve(ident, requester, posting=False, visibility_only=True)).readers
        except DiscordError as exc:
            raise DiscordError("read_audience_evidence_unavailable", "GET", path) from exc
        if not source_readers <= target_readers:
            raise DiscordError("read_would_disclose_to_other_readers", "GET", path)
    return channel


async def _guild_audience(tool: Any, guild_id: str, requester: str) -> None:
    """Guild directory data may be returned only to guild members or the requester's DM."""
    metadata = tool.metadata_provider()
    source = str(metadata.get("discord_thread_id") or metadata.get("discord_channel_id") or "")
    if not source:
        raise DiscordError("source_conversation_required", "GET", "/guilds")
    channel = await tool.platform._client.mapping("GET", f"/channels/{source}")
    if str(channel.get("id")) != source:
        raise DiscordError("target_mismatch", "GET", f"/channels/{source}")
    if channel.get("type") == 1:
        await tool._readable(source, requester, history_audience=False)
        return
    # All readers of a guild channel belong to that guild, including private-thread readers.
    if str(channel.get("guild_id")) == guild_id:
        if channel.get("type") in THREAD_TYPES:
            await tool._readable(source, requester, history_audience=False)
        else:
            await _visible(tool, source, guild_id, requester)
        return
    try:
        readers = (await TargetResolver(tool.platform).resolve(source, requester, posting=False, visibility_only=True)).readers
    except DiscordError as exc:
        raise DiscordError("read_audience_evidence_unavailable", "GET", f"/guilds/{guild_id}") from exc
    for user in readers:
        try:
            member = await tool.platform._client.mapping("GET", f"/guilds/{guild_id}/members/{user}")
        except DiscordError as exc:
            code = "read_would_disclose_to_other_readers" if exc.status == 404 else "read_audience_evidence_unavailable"
            raise DiscordError(code, "GET", f"/guilds/{guild_id}") from exc
        if not isinstance(member.get("user"), Mapping) or str(member["user"].get("id")) != user:
            raise DiscordError("read_would_disclose_to_other_readers", "GET", f"/guilds/{guild_id}")


def _position(cursor: str | None, binding: list[Any]) -> tuple[int, int, str | None, dict[str, str]]:
    if cursor is None:
        return 0, 0, None, {}
    try:
        if not isinstance(cursor, str):
            raise TypeError
        payload, signature = cursor.rsplit(".", 1)
        expected = hmac.digest(_CURSOR_KEY, payload.encode("ascii"), "sha256").hex()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        value = json.loads(base64.b64decode(payload.encode("ascii"), altchars=b"-_", validate=True))
        if (not isinstance(value, dict) or value.get("binding") != binding
                or type(value.get("source")) is not int or value["source"] < 0
                or type(value.get("offset")) is not int or value["offset"] < 0
                or value.get("before") is not None and not isinstance(value["before"], str)):
            raise ValueError
        prior = value.get("unavailable", {})
        if not isinstance(prior, dict) or any(key not in KINDS or status not in ("permission_denied", "unavailable") for key, status in prior.items()):
            raise ValueError
        return value["source"], value["offset"], value.get("before"), prior
    except (TypeError, ValueError, UnicodeError, binascii.Error):
        raise ValueError("cursor must be an unchanged continuation from this query, kind, guild, bot, requester, source and running process; after a process restart, omit cursor to restart the search") from None


def _cursor(binding: list[Any], source: int, offset: int, before: str | None, unavailable: dict[str, str]) -> str:
    value = {"binding": binding, "source": source, "offset": offset, "before": before, "unavailable": unavailable}
    payload = base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()
    signature = hmac.digest(_CURSOR_KEY, payload.encode("ascii"), "sha256").hex()
    return f"{payload}.{signature}"


def _match(raw: Mapping[str, Any], kind: str, guild_id: str, folded: str, *, parent: str = "") -> dict[str, Any] | None:
    ident = str(raw.get("id") or "")
    if not SNOWFLAKE_RE.fullmatch(ident):
        raise DiscordError("bad_response", "GET", f"/guilds/{guild_id}")
    names = [str(raw.get("name") or "")]
    if kind == "member":
        names = [str(raw.get(key) or "") for key in ("username", "nick")]
    hits = [hit for name in names for hit in matches_in([{"name": name}], folded, None)]
    if folded == ident:
        match_type = "id"
    elif not hits:
        return None
    else:
        match_type = min((hit["match_type"] for hit in hits), key={"exact": 0, "prefix": 1, "contains": 2}.__getitem__)
        if kind == "member" and match_type == "contains":
            return None
    result = {"id": ident, "kind": kind, "guild_id": guild_id, "name": next((name for name in names if name), ""),
              "parent_id": parent or str(raw.get("parent_id") or ""), "match_type": match_type,
              "archived": bool(raw.get("archived", False))}
    if kind in ("channel", "thread"):
        result.update(chat_id=ident, parent_chat_id=result["parent_id"], type=raw.get("type"))
    if kind == "thread":
        result["membership"] = raw.get("membership", {"requester": "unknown", "bot": "unknown"})
    return result


async def find_by_name(tool: Any, query: str, kind: str | None, guild_id: str | None,
                       limit: int, cursor: str | None) -> str:
    """Search one verified guild; cursors resume sources without treating short archive pages as final."""
    from belle_agent.platforms.discord.native import DiscordNativeToolkit
    from belle_agent.platforms.discord.reading import _HIDDEN

    text, folded = lookup_query(query)
    if not folded:
        return refusal("query_required", "query must name a Discord resource")
    if kind is not None and kind not in KINDS:
        return refusal("kind_invalid", "kind must be " + ", ".join(KINDS))
    if type(limit) is not int or limit < 1:
        return refusal("limit_invalid", "limit must be a positive integer")
    try:
        _, requester, origin_guild = tool._turn()
        target_guild = guild_id or origin_guild
        if not target_guild:
            raise DiscordError("guild_required", "GET", "/guilds")
        metadata = tool.metadata_provider()
        binding = [1, text, kind, target_guild, tool.platform.bot, requester,
                   metadata.get("discord_thread_id") or metadata.get("discord_channel_id")]
        try:
            source_index, offset, before, unavailable = _position(cursor, binding)
        except ValueError as exc:
            return refusal("cursor_invalid", str(exc))
        guild, members = await tool._guild_members(target_guild, requester)
        kinds = (kind,) if kind else KINDS
        client = tool.platform._client
        sources: list[tuple[str, str, str]] = []
        channels = []
        if any(value in kinds for value in ("channel", "thread", "tag")):
            channels = await client.request("GET", f"/guilds/{target_guild}/channels")
            if not isinstance(channels, list) or any(not isinstance(item, Mapping) for item in channels):
                raise DiscordError("bad_response", "GET", f"/guilds/{target_guild}/channels")
        if "channel" in kinds:
            sources.append(("channel", "channels", ""))
        if "tag" in kinds:
            sources.extend(("tag", "tags", str(channel.get("id") or "")) for channel in channels if channel.get("type") in (15, 16))
        limited_private = set()
        if "thread" in kinds:
            sources.append(("thread", "active", ""))
            for channel in channels:
                if channel.get("type") in (0, 5, 15, 16):
                    parent = str(channel.get("id") or "")
                    sources.append(("thread", "public_archived", parent))
                    if channel.get("type") == 0:
                        try:
                            managed = all(permissions(guild, channel, members[user]) & MANAGE_THREADS
                                          for user in (requester, tool.platform.bot_user_id))
                        except (KeyError, TypeError, ValueError):
                            raise DiscordError("bad_response", "GET", f"/channels/{parent}") from None
                        if not managed:
                            limited_private.add(parent)
                        sources.append(("thread", "private_archived" if managed else "joined_private_archived", parent))
        sources.extend((value, value, "") for value in kinds if value in ("member", "role", "emoji", "sticker"))
        if cursor is not None and source_index >= len(sources):
            return refusal("cursor_invalid", "cursor source no longer exists; restart without cursor")
        native = DiscordNativeToolkit(tool.platform, lambda: {**tool.metadata_provider(), "discord_guild_id": target_guild})
        found = []
        coverage: dict[str, Any] = {value: {"status": unavailable.get(value, "searched"), "sources": []} for value in kinds}
        next_position = None
        start_position = {"source": source_index, "offset": offset, "before": before}
        while source_index < len(sources):
            resource, endpoint, parent = sources[source_index]
            info: dict[str, Any] = {"source": endpoint, "status": "searched"}
            rows = []
            next_before = None
            try:
                if endpoint == "channels":
                    rows = [raw for raw in channels if raw.get("type") in CHANNEL_TYPES]
                elif endpoint == "tags":
                    visible = await _visible(tool, parent, target_guild, requester)
                    rows = visible.get("available_tags", [])
                elif resource == "thread":
                    if parent:
                        await _visible(tool, parent, target_guild, requester)
                    page = json.loads(await native.list_discord_threads(
                        mode=endpoint, parent_channel_id=parent or None, before=before,
                        **({"limit": 100} if endpoint != "active" else {})))
                    if not page.get("ok"):
                        if page.get("error") == "before_invalid":
                            return refusal("cursor_invalid", page.get("detail", "invalid archive position"))
                        raise DiscordError(str(page.get("error")), "GET", "/threads")
                    rows = [{**raw, "id": raw["thread_id"], "parent_id": raw["parent_channel_id"]} for raw in page["threads"]]
                    next_before = page["coverage"]["next_before"]
                    info["excluded_threads"] = page["coverage"].get("excluded_threads", 0)
                    if endpoint == "joined_private_archived" and parent in limited_private:
                        info.update(status="permission_denied", reason="Full private archives require Manage Threads for requester and bot; searched only bot-joined private archives")
                    if page["coverage"].get("unavailable_threads"):
                        info.update(status="unavailable", reason="read_audience_evidence_unavailable",
                                    unavailable_threads=page["coverage"]["unavailable_threads"])
                else:
                    await _guild_audience(tool, target_guild, requester)
                    path = f"/guilds/{target_guild}/{ {'member': 'members/search', 'role': 'roles', 'emoji': 'emojis', 'sticker': 'stickers'}[resource]}"
                    if resource == "member":
                        if SNOWFLAKE_RE.fullmatch(text):
                            raw = await client.mapping("GET", f"/guilds/{target_guild}/members/{text}")
                            rows = [raw]
                        else:
                            rows = await client.request("GET", path, params={"query": text, "limit": 1000})
                        info.update(match_semantics="username_or_nickname_prefix", discord_wide_directory="unavailable", dm_index="unavailable")
                        if isinstance(rows, list) and len(rows) == 1000:
                            info.update(status="unavailable", reason="Discord member prefix search returns at most 1000 members and has no continuation; narrow query")
                        if not isinstance(rows, list) or any(not isinstance(raw, Mapping) or not isinstance(raw.get("user"), Mapping) for raw in rows):
                            raise DiscordError("bad_response", "GET", path)
                        rows = [{**raw["user"], "nick": raw.get("nick")} for raw in rows]
                    else:
                        rows = await client.request("GET", path)
                if not isinstance(rows, list) or any(not isinstance(raw, Mapping) for raw in rows):
                    raise DiscordError("bad_response", "GET", f"/guilds/{target_guild}")
                if parent:
                    info["parent_channel_id"] = parent
                index = offset
                while index < len(rows):
                    raw = rows[index]
                    index += 1
                    match = _match(raw, resource, target_guild, folded, parent=parent)
                    if match is None:
                        continue
                    if resource == "channel":
                        try:
                            await _visible(tool, match["id"], target_guild, requester)
                        except DiscordError as exc:
                            if exc.code not in _HIDDEN:
                                raise
                            if exc.code == "read_audience_evidence_unavailable":
                                info.update(status="unavailable", reason=exc.code)
                                info["unavailable_resources"] = info.get("unavailable_resources", 0) + 1
                            else:
                                info["excluded_resources"] = info.get("excluded_resources", 0) + 1
                            continue
                    found.append(match)
                    if len(found) == limit:
                        if index < len(rows):
                            next_position = (source_index, index, before)
                        elif next_before:
                            next_position = (source_index, 0, next_before)
                        elif source_index + 1 < len(sources):
                            next_position = (source_index + 1, 0, None)
                        break
                if next_before and not next_position:
                    if next_before == before:
                        raise DiscordError("bad_response", "GET", "/threads")
                    # One remote archive page per source per call bounds work without hiding later pages.
                    next_position = (source_index, 0, next_before)
            except DiscordError as exc:
                if exc.code not in _HIDDEN | {"requester_cannot_list_private_threads", "bot_cannot_list_private_threads", "forbidden", "not_found"}:
                    raise
                info = {"source": endpoint, "status": "unavailable" if exc.code == "read_audience_evidence_unavailable" else "permission_denied", "reason": exc.code}
            coverage[resource]["sources"].append(info)
            if info["status"] != "searched" and coverage[resource]["status"] != "unavailable":
                coverage[resource]["status"] = info["status"]
                unavailable[resource] = info["status"]
            if next_position:
                for pending, _, _ in sources[source_index:]:
                    if coverage[pending]["status"] == "searched":
                        coverage[pending]["status"] = "still_paged"
                break
            source_index += 1
            offset, before = 0, None
        next_cursor = _cursor(binding, *next_position, unavailable) if next_position else None
        status = "partial" if next_cursor or any(item["status"] != "searched" for item in coverage.values()) else "complete"
        return json.dumps({"ok": True, "query": text, "guild_id": target_guild, "matches": found,
                           "coverage": {"status": status, "kinds": coverage, "next_cursor": next_cursor,
                                        "visible_source_audience_only": True, "snapshot": False, "position": start_position}}, ensure_ascii=False)
    except DiscordError as exc:
        return refusal(exc.code, str(exc))
