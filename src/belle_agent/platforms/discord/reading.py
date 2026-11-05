"""Read Discord messages, directory entries, and attachments for one turn."""

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.attachments import FileTextOverLimit, file_content
from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import ID_ARGUMENTS, ID_FORMS, SNOWFLAKE_RE, checks_discord_ids, message_url
from belle_agent.platforms.discord.targets import (
    CONNECT,
    MANAGE_THREADS,
    READ_HISTORY,
    THREAD_TYPES,
    VIEW,
    TargetResolver,
    permissions,
)
from belle_agent.tools.host import refusal
from belle_agent.tools.inputs import checks_ids

_GUILD_MESSAGE_SEARCH = "search_discord_guild"
_READ = "read_discord_conversation"
_FIND = "find_by_name"
_DOWNLOAD = "download_discord_attachment"
_SEARCH_FIELDS = {
    "content": "content", "channel_ids": "channel_id", "author_ids": "author_id",
    "author_types": "author_type", "mentioned_user_ids": "mentions",
    "mentioned_role_ids": "mentions_role_id", "replied_to_message_ids": "replied_to_message_id",
    "replied_to_user_ids": "replied_to_user_id", "has": "has", "embed_types": "embed_type",
    "embed_providers": "embed_provider", "link_hostnames": "link_hostname",
    "attachment_filenames": "attachment_filename", "attachment_extensions": "attachment_extension",
    "min_id": "min_id", "max_id": "max_id", "slop": "slop", "pinned": "pinned",
    "mention_everyone": "mention_everyone", "sort_by": "sort_by", "sort_order": "sort_order",
    "include_nsfw": "include_nsfw", "limit": "limit", "offset": "offset",
}
_SEARCH_IDS = {"channel_ids", "author_ids", "mentioned_user_ids", "mentioned_role_ids",
               "replied_to_message_ids", "replied_to_user_ids", "min_id", "max_id"}
_SEARCH_ARRAYS = {
    "channel_ids", "author_ids", "author_types", "mentioned_user_ids", "mentioned_role_ids",
    "replied_to_message_ids", "replied_to_user_ids", "has", "embed_types", "embed_providers",
    "link_hostnames", "attachment_filenames", "attachment_extensions",
}
_SEARCH_BOOLS = {"pinned", "mention_everyone", "include_nsfw"}
_SEARCH_INTS = {"slop": (0, 100), "limit": (1, 25), "offset": (0, 9975)}
_HIDDEN = {"requester_cannot_read_target", "bot_cannot_read_target", "requester_cannot_access_target",
           "read_would_disclose_to_other_readers", "read_audience_evidence_unavailable"}
_SEARCH_ARRAY_LIMITS = {"channel_ids": 500, "author_ids": 100, "mentioned_user_ids": 100,
                        "mentioned_role_ids": 100, "replied_to_message_ids": 100,
                        "replied_to_user_ids": 100, "embed_providers": 100,
                        "link_hostnames": 100, "attachment_filenames": 100,
                        "attachment_extensions": 100}


def _card(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description,
            "input_params": {"type": "object", "properties": properties, "required": required or []}}


_ID = {"type": "string", "description": "Discord Snowflake ID."}
_NUMBER = {"type": "integer"}
_CURSOR = {"type": "string", "pattern": "^[0-9]{1,20}$",
           "description": "Unsigned 64-bit decimal pagination value; 0 and synthetic Snowflakes are allowed."}
_checks_read_ids = checks_ids(
    {key: value for key, value in ID_ARGUMENTS.items()
     if key not in {"before_message_id", "after_message_id", "around_message_id"}},
    ID_FORMS, "Discord", refusal)
_SEARCH_ENUMS = {
    "author_types": ["user", "bot", "webhook", "-user", "-bot", "-webhook"],
    "has": ["image", "sound", "video", "file", "sticker", "embed", "link", "poll", "snapshot",
            "-image", "-sound", "-video", "-file", "-sticker", "-embed", "-link", "-poll", "-snapshot"],
    "embed_types": ["image", "video", "gif", "sound", "article"],
    "sort_by": ["timestamp", "relevance"], "sort_order": ["asc", "desc"],
}
_SEARCH_TEXT_LIMITS = {"content": 1024, "embed_providers": 256, "link_hostnames": 256,
                       "attachment_filenames": 1024, "attachment_extensions": 256}


def _search_schema(name: str) -> dict[str, Any]:
    if name in _SEARCH_INTS:
        low, high = _SEARCH_INTS[name]
        return {"type": "integer", "minimum": low, "maximum": high}
    if name in _SEARCH_BOOLS:
        return {"type": "boolean"}
    item: dict[str, Any] = {"type": "string", "minLength": 1}
    if name in _SEARCH_IDS:
        item.update(pattern=SNOWFLAKE_RE.pattern)
    if name in _SEARCH_ENUMS:
        item.update(enum=_SEARCH_ENUMS[name])
    if name in _SEARCH_TEXT_LIMITS:
        item.update(maxLength=_SEARCH_TEXT_LIMITS[name])
    if name in _SEARCH_ARRAYS:
        schema: dict[str, Any] = {"type": "array", "items": item, "minItems": 1}
        if name in _SEARCH_ARRAY_LIMITS:
            schema["maxItems"] = _SEARCH_ARRAY_LIMITS[name]
        return schema
    return item


class DiscordReadingToolkit:
    """Read only through the bot and requester of the current Discord turn."""

    def __init__(self, platform: Any, metadata_provider: Callable[[], Mapping[str, Any]]) -> None:
        self.platform = platform
        self.metadata_provider = metadata_provider

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        cards = [
            _card(_READ, "Read one Discord message or one newest-first page. A thread has its own chat_id."
                  " Defaults to the active thread. Coverage gives directional continuation cursors.", {
                      "chat_id": _ID, "message_id": _ID, "before_message_id": _CURSOR,
                      "after_message_id": _CURSOR, "around_message_id": _CURSOR,
                      "limit": {**_NUMBER, "minimum": 1, "maximum": 100, "description": "Discord page size, 1–100; default 50."},
                  }),
            _card(_FIND, "Find named guild channels, active and archived threads/forum posts, tags, members, roles, emoji and stickers."
                  " Member names use Discord prefix search. Follow coverage.next_cursor; coverage names denied or unavailable sources.", {
                      "query": {"type": "string"},
                      "kind": {"type": "string", "enum": ["channel", "thread", "tag", "member", "role", "emoji", "sticker"],
                               "description": "Set this when the resource type is known. Omitting it searches every kind."},
                      "guild_id": _ID, "limit": {"type": "integer", "minimum": 1, "default": 100},
                      "cursor": {"type": "string", "description": "Opaque continuation from coverage.next_cursor for the same query and kind."},
                  }, ["query"]),
            _card(_DOWNLOAD, "Download an attachment from a message the requester can read. Give its exact"
                  " message, channel and attachment IDs; no arbitrary URL is accepted."
                  " A larger file is refused whole; the operator can raise platforms.discord.file_download_max_bytes.", {
                      "chat_id": _ID, "message_id": _ID, "attachment_id": _ID,
                  }, ["chat_id", "message_id", "attachment_id"]),
        ]
        if self.platform._settings.message_content:
            cards.append(_card(_GUILD_MESSAGE_SEARCH, "Search Discord guild messages using structured API filters."
                               " Each result is checked against the requester's current channel access."
                               " Partial coverage means more pages may exist; indexing is separate. Use next_offset to continue.", {
                                   "guild_id": _ID,
                                   "filters": {"type": "object", "properties": {
                                       key: _search_schema(key) for key in _SEARCH_FIELDS
                                   }, "additionalProperties": False},
                               }, ["filters"]))
        return cards

    def _turn(self) -> tuple[str, str, str]:
        metadata = self.metadata_provider()
        bot = str(metadata.get("discord_bot") or "")
        if bot != self.platform.bot:
            raise DiscordError("discord_bot_mismatch", "GET", "/channels")
        requester = str(metadata.get("discord_user_id") or "")
        if not requester:
            raise DiscordError("requester_required", "GET", "/channels")
        return str(metadata.get("discord_thread_id") or metadata.get("discord_channel_id") or ""), requester, str(metadata.get("discord_guild_id") or "")

    async def _guild_members(self, guild_id: str, requester: str) -> tuple[Mapping[str, Any], dict[str, Mapping[str, Any]]]:
        client = self.platform._client
        guild = await client.mapping("GET", f"/guilds/{guild_id}")
        if str(guild.get("id")) != guild_id:
            raise DiscordError("target_mismatch", "GET", f"/guilds/{guild_id}")
        members: dict[str, Mapping[str, Any]] = {}
        for user in (requester, self.platform.bot_user_id):
            if not user:
                raise DiscordError("bot_identity_unknown", "GET", f"/guilds/{guild_id}")
            member = await client.mapping("GET", f"/guilds/{guild_id}/members/{user}")
            if not isinstance(member.get("user"), Mapping) or str(member["user"].get("id")) != user:
                raise DiscordError("target_mismatch", "GET", f"/guilds/{guild_id}/members/{user}")
            members[user] = member
        return guild, members

    async def _readable(self, chat_id: str, requester: str, *, history_audience: bool = True) -> str:
        client = self.platform._client
        path = f"/channels/{chat_id}"
        channel = await client.mapping("GET", path)
        if str(channel.get("id")) != chat_id:
            raise DiscordError("target_mismatch", "GET", path)
        kind = channel.get("type")
        if kind == 1:
            readers = {str(item.get("id")) for item in channel.get("recipients", ()) if isinstance(item, Mapping)}
            if readers != {requester} or not self.platform.bot_user_id:
                raise DiscordError("requester_cannot_access_target", "GET", path)
            await self._safe_for_source(chat_id, requester, history_audience=history_audience)
            return ""
        if kind not in (0, 5, 15, 16, 2, *THREAD_TYPES):
            raise DiscordError("unsupported_read_target", "GET", path)
        guild_id = str(channel.get("guild_id") or "")
        if not guild_id:
            raise DiscordError("bad_response", "GET", path)
        guild, members = await self._guild_members(guild_id, requester)
        parent_id = str(channel.get("parent_id") or "") if kind in THREAD_TYPES else ""
        permission_channel = await client.mapping("GET", f"/channels/{parent_id}") if parent_id else channel
        if parent_id and (str(permission_channel.get("id")) != parent_id
                          or str(permission_channel.get("guild_id")) != guild_id):
            raise DiscordError("target_mismatch", "GET", f"/channels/{parent_id}")
        for user, label in ((requester, "requester"), (self.platform.bot_user_id, "bot")):
            member = members[user]
            try:
                granted = permissions(guild, permission_channel, member)
            except (KeyError, TypeError, ValueError):
                raise DiscordError("bad_response", "GET", f"/guilds/{guild_id}/members/{user}") from None
            needed = VIEW | READ_HISTORY | (CONNECT if kind == 2 else 0)
            if granted & needed != needed:
                raise DiscordError(f"{label}_cannot_read_target", "GET", path)
            if kind == 12 and not granted & MANAGE_THREADS:
                try:
                    found = await client.mapping("GET", f"/channels/{chat_id}/thread-members/{user}")
                except DiscordError as exc:
                    if exc.status == 404:
                        raise DiscordError(f"{label}_cannot_read_target", "GET", path) from None
                    raise
                if str(found.get("user_id")) != user:
                    raise DiscordError("target_mismatch", "GET", path)
        await self._safe_for_source(chat_id, requester, history_audience=history_audience)
        return guild_id

    async def _safe_for_source(self, target: str, requester: str, *, history_audience: bool = True) -> None:
        metadata = self.metadata_provider()
        source = str(metadata.get("discord_thread_id") or metadata.get("discord_channel_id") or "")
        if not source:
            raise DiscordError("source_conversation_required", "GET", f"/channels/{target}")
        if metadata.get("discord_channel_type") == "dm" or not history_audience and source == target:
            return
        resolver = TargetResolver(self.platform)
        try:
            source_target = await resolver.resolve(source, requester, posting=False)
            target_audience = source_target if source == target else await resolver.resolve(target, requester, posting=False)
        except DiscordError as exc:
            raise DiscordError("read_audience_evidence_unavailable", "GET", f"/channels/{target}") from exc
        target_readers = target_audience.history_readers if history_audience else target_audience.readers
        if not source_target.readers <= target_readers:
            raise DiscordError("read_would_disclose_to_other_readers", "GET", f"/channels/{target}")

    def _message(self, message: Mapping[str, Any], chat_id: str, guild_id: str) -> dict[str, Any]:
        if str(message.get("channel_id")) != chat_id or not SNOWFLAKE_RE.fullmatch(str(message.get("id") or "")):
            raise DiscordError("target_mismatch", "GET", f"/channels/{chat_id}/messages")
        ident = str(message["id"])
        author = message.get("author")
        reference = message.get("message_reference")
        result = {
            "message_id": ident, "chat_id": chat_id,
            "author_id": str(author.get("id") or "") if isinstance(author, Mapping) else "",
            "guild_id": guild_id or None, "author": author,
            "timestamp": message.get("timestamp"), "content": message.get("content", ""),
            "embeds": message.get("embeds", []),
            "attachments": [{**{key: value for key, value in item.items()
                                  if key not in {"id", "size", "content_type", "url", "proxy_url"}},
                             "attachment_id": str(item.get("id") or ""),
                             "filename": item.get("filename"), "size_bytes": item.get("size"),
                             "mimetype": item.get("content_type")}
                            for item in message.get("attachments", ()) if isinstance(item, Mapping)],
            "reply_to_message_id": str(reference.get("message_id") or "") if isinstance(reference, Mapping) else "",
            "url": message_url(guild_id or "@me", chat_id, ident),
        }
        projected = ("edited_timestamp", "type", "components", "poll", "sticker_items", "stickers",
                     "message_reference", "message_snapshots", "mentions", "mention_roles", "mention_channels",
                     "mention_everyone", "reactions", "flags", "webhook_id", "application_id")
        for field in projected:
            if field in message:
                result[field] = message[field]
        result["projection"] = {
            "raw_message": False,
            "unavailable_fields": [field for field in (*projected, "content", "embeds", "attachments", "author", "timestamp")
                                   if field not in message],
            "omitted_fields": sorted(set(message) - set(projected) - {
                "id", "channel_id", "guild_id", "author", "timestamp", "content", "embeds", "attachments"}),
            "omitted_attachment_fields": ["url", "proxy_url"],
        }
        result["content_availability"] = (
            "may_be_withheld" if guild_id and not self.platform._settings.message_content
            and not any(message.get(field) for field in ("content", "embeds", "attachments", "components", "poll"))
            else "returned_fields")
        return result

    async def _one(self, chat_id: str, message_id: str, requester: str) -> tuple[dict[str, Any], Mapping[str, Any], str]:
        guild_id = await self._readable(chat_id, requester)
        path = f"/channels/{chat_id}/messages/{message_id}"
        message = await self.platform._client.mapping("GET", path)
        if str(message.get("id")) != message_id:
            raise DiscordError("target_mismatch", "GET", path)
        return self._message(message, chat_id, guild_id), message, guild_id

    @_checks_read_ids
    async def read_discord_conversation(self, chat_id: str | None = None, message_id: str | None = None,
                                        before_message_id: str | None = None, after_message_id: str | None = None,
                                        around_message_id: str | None = None, limit: int = 50) -> str:
        """Read one message or a page after checking both readers."""
        for field, value in (("before_message_id", before_message_id), ("after_message_id", after_message_id),
                             ("around_message_id", around_message_id)):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value)
                                      or int(value) > (1 << 64) - 1):
                return refusal("cursor_invalid", f"{field} must be an unsigned 64-bit decimal string, including 0")
        positions = [value for value in (message_id, before_message_id, after_message_id, around_message_id) if value is not None]
        if len(positions) > 1:
            return refusal("message_selectors_conflict", "use only one of message_id, before_message_id, after_message_id, around_message_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            return refusal("limit_invalid", "limit must be an integer from 1 to 100")
        try:
            origin, requester, _ = self._turn()
            target = chat_id or origin
            if not target:
                raise DiscordError("trusted_conversation_context_required", "GET", "/channels")
            if message_id:
                record, _, guild_id = await self._one(target, message_id, requester)
                content_unavailable = record["content_availability"] == "may_be_withheld"
                return json.dumps({"ok": True, "chat_id": target, "messages": [record],
                                   "coverage": {"status": "content_unavailable" if content_unavailable else "single_message",
                                                "scope": "exact_message", "message_id": message_id,
                                                "unrecoverable": ["deleted_messages", "older_edit_revisions"]}}, ensure_ascii=False)
            guild_id = await self._readable(target, requester)
            params: dict[str, Any] = {"limit": limit}
            for key, value in (("before", before_message_id), ("after", after_message_id), ("around", around_message_id)):
                if value:
                    params[key] = value
            path = f"/channels/{target}/messages"
            page = await self.platform._client.request("GET", path, params=params)
            if not isinstance(page, list) or any(not isinstance(item, Mapping) for item in page):
                raise DiscordError("bad_response", "GET", path)
            records = sorted((self._message(item, target, guild_id) for item in page),
                             key=lambda item: int(item["message_id"]), reverse=True)
            mode = "around" if around_message_id is not None else "after" if after_message_id is not None else "before"
            full = len(page) == limit
            coverage: dict[str, Any] = {
                "status": "partial" if full or mode == "around" else "complete",
                "scope": "centered_page" if mode == "around" else f"{mode}_page",
                "order": "newest_first", "older_boundary_exhausted": None,
                "newer_boundary_exhausted": None,
                "next_before_message_id": None, "next_after_message_id": None,
                "unrecoverable": ["deleted_messages", "older_edit_revisions"],
            }
            if records:
                if mode in ("before", "around") and (full or mode == "around"):
                    coverage["next_before_message_id"] = records[-1]["message_id"]
                if mode in ("after", "around") and (full or mode == "around"):
                    coverage["next_after_message_id"] = records[0]["message_id"]
            if mode != "around":
                coverage["older_boundary_exhausted" if mode == "before" else "newer_boundary_exhausted"] = not full
            if any(record["content_availability"] == "may_be_withheld" for record in records):
                coverage.update(status="content_unavailable", reason="Discord may withhold content without Message Content intent")
            return json.dumps({"ok": True, "chat_id": target, "messages": records, "coverage": coverage}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def download_discord_attachment(self, chat_id: str, message_id: str, attachment_id: str) -> Any:
        """Download the signed URL from the current parent message, never from the caller."""
        try:
            _, requester, _ = self._turn()
            record, message, _ = await self._one(chat_id, message_id, requester)
            attachment = next((item for item in message.get("attachments", ())
                               if isinstance(item, Mapping) and str(item.get("id")) == attachment_id), None)
            if attachment is None:
                if record["content_availability"] == "may_be_withheld":
                    return refusal("content_unavailable", "attachment_id is absent from the current response; Discord may withhold content without Message Content intent")
                return refusal("attachment_not_in_message", "attachment_id is not on this message")
            size = attachment.get("size")
            if type(size) is not int or size < 0:
                raise DiscordError("bad_response", "GET", f"/channels/{chat_id}/messages/{message_id}")
            max_bytes = self.platform._settings.file_download_max_bytes
            if size > max_bytes:
                return refusal("file_over_size_limit", f"attachment is {size} bytes; platforms.discord.file_download_max_bytes is {max_bytes}")
            try:
                data = await self.platform._client.download(str(attachment.get("url") or ""), max_bytes)
            except DiscordError as exc:
                if exc.code == "too_large":
                    return refusal("file_over_size_limit", f"attachment response exceeds platforms.discord.file_download_max_bytes ({max_bytes} bytes)")
                raise
            name = str(attachment.get("filename") or attachment_id)
            info = {"ok": True, "chat_id": chat_id, "message_id": message_id,
                    "attachment_id": attachment_id, "name": name,
                    "mimetype": str(attachment.get("content_type") or "application/octet-stream"),
                    "size_bytes": len(data), "message_url": record["url"]}
            return file_content(info, data, name=name, max_chars=max_bytes)
        except FileTextOverLimit as exc:
            return refusal("file_text_over_limit", f"file has {exc.chars} characters; platforms.discord.file_download_max_bytes is {self.platform._settings.file_download_max_bytes}")
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def find_by_name(self, query: str, kind: str | None = None, guild_id: str | None = None,
                           limit: int = 100, cursor: str | None = None) -> str:
        """Find visible named resources in one verified guild."""
        from belle_agent.platforms.discord.discovery import find_by_name

        return await find_by_name(self, query, kind, guild_id, limit, cursor)

    @checks_discord_ids
    async def search_discord_guild(self, filters: Mapping[str, Any], guild_id: str | None = None) -> str:
        """Search one guild and filter every returned message by current reader access."""
        if not isinstance(filters, Mapping):
            return refusal("filters_invalid", "filters must be an object")
        unknown = set(filters) - _SEARCH_FIELDS.keys()
        if unknown:
            return refusal("filters_invalid", f"unknown filters: {', '.join(map(str, sorted(unknown)))}")
        params: list[tuple[str, Any]] = []
        for name, value in filters.items():
            if name in _SEARCH_INTS:
                low, high = _SEARCH_INTS[name]
                if type(value) is not int or not low <= value <= high:
                    return refusal("filters_invalid", f"filters.{name} must be an integer from {low} to {high}")
            elif name in _SEARCH_BOOLS:
                if type(value) is not bool:
                    return refusal("filters_invalid", f"filters.{name} must be true or false")
            elif name in _SEARCH_ARRAYS:
                if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
                    return refusal("filters_invalid", f"filters.{name} must be a nonempty list of strings")
                maximum = _SEARCH_ARRAY_LIMITS.get(name)
                if maximum and len(value) > maximum:
                    return refusal("filters_invalid", f"filters.{name} accepts at most {maximum} values in Discord")
            elif not isinstance(value, str) or not value:
                return refusal("filters_invalid", f"filters.{name} must be a nonempty string")
            if name == "content" and len(value) > 1024:
                return refusal("filters_invalid", "filters.content accepts at most 1024 characters in Discord")
            maximum_chars = _SEARCH_TEXT_LIMITS.get(name)
            if maximum_chars and any(len(item) > maximum_chars for item in (value if name in _SEARCH_ARRAYS else [value])):
                return refusal("filters_invalid", f"filters.{name} contains a value over Discord's {maximum_chars}-character limit")
            values = value if name in _SEARCH_ARRAYS else [value]
            for item in values:
                if name in _SEARCH_ENUMS and item not in _SEARCH_ENUMS[name]:
                    return refusal("filters_invalid", f"filters.{name} expects one of {', '.join(_SEARCH_ENUMS[name])}")
                if name in _SEARCH_IDS and not SNOWFLAKE_RE.fullmatch(item):
                    return refusal("filters_invalid", f"filters.{name} must contain Discord Snowflake IDs")
                params.append((_SEARCH_FIELDS[name], str(item).lower() if isinstance(item, bool) else item))
        if filters.get("sort_by") not in (None, "timestamp", "relevance"):
            return refusal("filters_invalid", "filters.sort_by must be timestamp or relevance")
        if filters.get("sort_order") not in (None, "asc", "desc"):
            return refusal("filters_invalid", "filters.sort_order must be asc or desc")
        if not self.platform._settings.message_content:
            return refusal("content_unavailable", "Discord Message Content intent is disabled")
        try:
            _, requester, origin_guild = self._turn()
            target_guild = guild_id or origin_guild
            if not target_guild:
                raise DiscordError("guild_required", "GET", "/guilds")
            await self._guild_members(target_guild, requester)
            path = f"/guilds/{target_guild}/messages/search"
            # An explicit target must be readable before searching it. Guild-wide search can
            # return archived threads absent from channel listings; check each hit below.
            for channel in filters.get("channel_ids", []):
                if await self._readable(channel, requester) != target_guild:
                    raise DiscordError("target_mismatch", "GET", f"/channels/{channel}")
            offset = filters.get("offset", 0)
            scope = {
                "guild_id": target_guild, "channel_ids": filters.get("channel_ids"),
                "messages": "Discord guild index; output filtered by requester, bot and source audience",
                "archived_threads": "included when indexed and readable",
                "dm_search": "unavailable", "resource_names": "use find_by_name",
            }
            result = await self.platform._client.mapping("GET", path, params=params)
            if result.get("code") == 110000:
                return json.dumps({"ok": True, "messages": [], "coverage": {
                    "status": "indexing", "retry_after_seconds": result.get("retry_after"),
                    "retry_after": result.get("retry_after"), "scope": scope, "offset": offset}})
            groups = result.get("messages")
            if not isinstance(groups, list) or any(not isinstance(group, list) for group in groups):
                raise DiscordError("bad_response", "GET", path)
            found: list[dict[str, Any]] = []
            access: dict[str, str | None] = {}
            for group in groups:
                for message in group:
                    if not isinstance(message, Mapping):
                        raise DiscordError("bad_response", "GET", path)
                    channel = str(message.get("channel_id") or "")
                    if not SNOWFLAKE_RE.fullmatch(channel):
                        raise DiscordError("bad_response", "GET", path)
                    if channel not in access:
                        try:
                            access[channel] = await self._readable(channel, requester)
                        except DiscordError as exc:
                            if exc.code in _HIDDEN or exc.status in (403, 404):
                                access[channel] = None
                            else:
                                raise
                    if access[channel] is None:
                        continue
                    elif access[channel] != target_guild:
                        raise DiscordError("target_mismatch", "GET", path)
                    else:
                        found.append(self._message(message, channel, target_guild))
            page_size = int(filters.get("limit", 25))
            next_offset = offset + page_size if offset + page_size <= 9975 else None

            context_threads, context_members = result.get("threads", []), result.get("members", [])
            if (not isinstance(context_threads, list) or not isinstance(context_members, list)
                    or any(not isinstance(item, Mapping) for item in [*context_threads, *context_members])):
                raise DiscordError("bad_response", "GET", path)
            safe_channels = {record["chat_id"] for record in found}
            threads = [{key: thread[key] for key in (
                "id", "guild_id", "parent_id", "type", "name", "thread_metadata", "applied_tags") if key in thread}
                for thread in context_threads if str(thread.get("id")) in safe_channels]
            members = [{key: member[key] for key in ("id", "user_id", "join_timestamp", "flags") if key in member}
                for member in context_members if str(member.get("id")) in safe_channels
                and str(member.get("user_id")) in (requester, self.platform.bot_user_id)]
            return json.dumps({"ok": True, "messages": found, "threads": threads, "members": members,
                               "coverage": {
                "status": "partial", "scope": scope, "offset": offset,
                "next_offset": next_offset,
                "more_possible": True, "complete": False,
                "readable_source_audience_only": True,
                "indexing": bool(result.get("doing_deep_historical_index")),
                "page_size_limit": 25, "offset_limit": 9975,
                "offset_limit_reached": next_offset is None,
                "note": "Partial coverage means more pages may exist, not incomplete indexing. Check indexing separately. Continue with filters.offset=next_offset; at the offset ceiling narrow filters with min_id/max_id or channel_ids",
            }}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))
