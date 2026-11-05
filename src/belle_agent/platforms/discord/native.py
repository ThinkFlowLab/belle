"""Discord thread and poll companions for the current conversation."""

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE, checks_discord_ids, discord_id_error
from belle_agent.platforms.discord.reading import _HIDDEN, _card
from belle_agent.platforms.discord.targets import MANAGE_THREADS, SEND_THREADS, THREAD_TYPES, permissions
from belle_agent.platforms.discord.writing import DiscordWritingToolkit
from belle_agent.tools.host import refusal

_ID = {"type": "string", "description": "Discord Snowflake ID."}
CREATE_PUBLIC_THREADS = 1 << 35
CREATE_PRIVATE_THREADS = 1 << 36
_ARCHIVE_DURATIONS = {60, 1440, 4320, 10080}
_ARCHIVED = {
    "public_archived": "public",
    "private_archived": "private",
    "joined_private_archived": "users/@me/threads/archived/private",
}


class DiscordNativeToolkit(DiscordWritingToolkit):
    """Read and manage Discord's native threads and polls."""

    active_turn_only = frozenset({"create_discord_thread", "update_discord_thread",
                                  "change_discord_thread_member", "end_discord_poll"})

    def __init__(self, platform: Any, metadata_provider: Callable[[], Mapping[str, Any]]) -> None:
        super().__init__(platform, metadata_provider)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            _card("list_discord_threads", "List one Discord thread collection. Active threads are guild-wide;"
                  " archived modes use a parent channel. The cursor kind depends on the mode.", {
                      "mode": {"type": "string", "enum": ["active", *_ARCHIVED]},
                      "parent_channel_id": _ID,
                      "before": {"type": "string", "description": "Archive timestamp, or thread ID for joined_private_archived."},
                      "limit": {"type": "integer", "minimum": 2, "maximum": 100,
                                "description": "Archived page size, 2–100; omit for active threads."},
                      "joined_only": {"type": "boolean", "description": "Return only threads the requester joined."},
                  }, []),
            _card("list_discord_thread_members", "Page members of a readable Discord thread."
                  " Requires Discord Server Members access.", {
                      "thread_id": _ID, "after": _ID,
                      "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                  }, ["thread_id"]),
            _card("read_discord_poll", "Read a poll from a message the requester can see."
                  " Optionally page voters for one answer. Missing results mean unknown counts.", {
                      "chat_id": _ID, "message_id": _ID,
                      "answer_id": {"type": "integer", "minimum": 1}, "after": _ID,
                      "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                  }, ["message_id"]),
            _card("end_discord_poll", "End this bot's poll early after a person approves during this turn.",
                  {"chat_id": _ID, "message_id": _ID}, ["message_id"]),
            _card("create_discord_thread", "Create a text or announcement thread. For a forum or media post,"
                  " use post_message with reply_mode=thread and thread.name. Provide kind for a thread"
                  " without a source message.", {
                      "parent_channel_id": _ID, "name": {"type": "string", "minLength": 1, "maxLength": 100},
                      "source_message_id": _ID, "kind": {"type": "string", "enum": ["public", "private"]},
                      "auto_archive_duration": {"type": "integer", "enum": sorted(_ARCHIVE_DURATIONS)},
                      "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
                      "invitable": {"type": "boolean"},
                  }, ["parent_channel_id", "name"]),
            _card("update_discord_thread", "Change supported thread settings. The requester and bot must have"
                  " authority in the source audience.", {
                      "thread_id": _ID, "name": {"type": "string", "minLength": 1, "maxLength": 100},
                      "archived": {"type": "boolean"}, "locked": {"type": "boolean"},
                      "auto_archive_duration": {"type": "integer", "enum": sorted(_ARCHIVE_DURATIONS)},
                      "slowmode_seconds": {"type": "integer", "minimum": 0, "maximum": 21600},
                      "invitable": {"type": "boolean"},
                      "applied_tag_ids": {"type": "array", "items": _ID, "maxItems": 5},
                  }, ["thread_id"]),
            _card("change_discord_thread_member", "Join or leave as the bot, or add or remove a user from"
                  " an active thread. Another user's membership requires approval in this turn.", {
                      "thread_id": _ID, "action": {"type": "string", "enum": ["join", "leave", "add", "remove"]},
                      "user_id": _ID,
                  }, ["thread_id", "action"]),
        ]

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        if tool_name == "end_discord_poll":
            return "End this Discord poll now? People can no longer vote after it ends."
        if tool_name == "change_discord_thread_member" and isinstance(arguments, Mapping) and arguments.get("action") in {"add", "remove"}:
            if arguments.get("action") == "add" and not discord_id_error(dict(arguments)):
                try:
                    thread, requester, granted = await self._thread_for_write(str(arguments["thread_id"]))
                except (DiscordError, KeyError):
                    return None
                if self._invite_requires_manager(thread) and not granted[requester] & MANAGE_THREADS:
                    return None
            return f"{str(arguments['action']).capitalize()} user {arguments.get('user_id')} in Discord thread {arguments.get('thread_id')}?"
        if tool_name == "update_discord_thread" and isinstance(arguments, Mapping) and any(
            key in arguments for key in ("archived", "locked")
        ):
            return f"Change archived or locked state of Discord thread {arguments.get('thread_id')}?"
        return None

    @staticmethod
    def _name(name: Any) -> str | None:
        return None if isinstance(name, str) and 1 <= len(name) <= 100 else "name must be 1 to 100 characters"

    @staticmethod
    def _archive_duration(value: Any) -> str | None:
        return None if value is None or type(value) is int and value in _ARCHIVE_DURATIONS else (
            "auto_archive_duration must be 60, 1440, 4320, or 10080 minutes")

    @staticmethod
    def _slowmode(value: Any) -> str | None:
        return None if value is None or type(value) is int and 0 <= value <= 21600 else (
            "slowmode_seconds must be an integer from 0 to 21600")

    @staticmethod
    def _invite_requires_manager(thread: Mapping[str, Any]) -> bool:
        metadata = thread.get("thread_metadata")
        return thread.get("type") == 12 and isinstance(metadata, Mapping) and metadata.get("invitable") is False

    async def _thread_for_write(self, thread_id: str) -> tuple[Mapping[str, Any], str, dict[str, int]]:
        _, requester, _ = self._turn()
        path = f"/channels/{thread_id}"
        thread = await self.platform._client.mapping("GET", path)
        parent = str(thread.get("parent_id") or "")
        if str(thread.get("id")) != thread_id or thread.get("type") not in THREAD_TYPES or not SNOWFLAKE_RE.fullmatch(parent):
            raise DiscordError("thread_id_not_thread", "GET", path)
        guild_id = str(thread.get("guild_id") or "")
        if not SNOWFLAKE_RE.fullmatch(guild_id):
            raise DiscordError("bad_response", "GET", path)
        await self._readable(thread_id, requester, history_audience=False)
        _, _, newcomers = await self._access(thread_id, write=True)
        if newcomers:
            raise DiscordError("write_target_widens_audience", "PATCH", path)
        guild, members = await self._guild_members(guild_id, requester)
        parent_channel = await self.platform._client.mapping("GET", f"/channels/{parent}")
        if str(parent_channel.get("id")) != parent or str(parent_channel.get("guild_id")) != guild_id:
            raise DiscordError("target_mismatch", "GET", f"/channels/{parent}")
        try:
            granted = {user: permissions(guild, parent_channel, members[user])
                       for user in (requester, self.platform.bot_user_id)}
        except (KeyError, TypeError, ValueError):
            raise DiscordError("bad_response", "GET", path) from None
        return thread, requester, granted

    @checks_discord_ids
    async def create_discord_thread(
        self, parent_channel_id: str, name: str, source_message_id: str | None = None,
        kind: Literal["public", "private"] | None = None, auto_archive_duration: int | None = None,
        slowmode_seconds: int | None = None, invitable: bool | None = None,
    ) -> str:
        if error := self._name(name):
            return refusal("name_invalid", error)
        if error := self._archive_duration(auto_archive_duration):
            return refusal("auto_archive_duration_invalid", error)
        if error := self._slowmode(slowmode_seconds):
            return refusal("slowmode_seconds_invalid", error)
        if source_message_id and (kind is not None or invitable is not None):
            return refusal("source_message_options_invalid", "kind and invitable require a thread without source_message_id")
        if not source_message_id and kind not in ("public", "private"):
            return refusal("kind_required", "kind must be public or private without source_message_id")
        if invitable is not None and (type(invitable) is not bool or kind != "private"):
            return refusal("invitable_invalid", "invitable must be a boolean for a private thread without a source message")
        try:
            _, requester, _ = self._turn()
            path = f"/channels/{parent_channel_id}"
            parent = await self.platform._client.mapping("GET", path)
            guild_id = str(parent.get("guild_id") or "")
            if str(parent.get("id")) != parent_channel_id or not SNOWFLAKE_RE.fullmatch(guild_id) or parent.get("type") not in (0, 5):
                raise DiscordError("parent_channel_required", "GET", path)
            await self._readable(parent_channel_id, requester, history_audience=False)
            capability = CREATE_PRIVATE_THREADS if kind == "private" else CREATE_PUBLIC_THREADS
            _, _, newcomers = await self._access(parent_channel_id, write=True, extra=capability)
            if newcomers:
                raise DiscordError("write_target_widens_audience", "POST", path)
            if source_message_id:
                await self._message_for(parent_channel_id, source_message_id, write=True, extra=capability)
            payload: dict[str, Any] = {"name": name}
            if auto_archive_duration is not None:
                payload["auto_archive_duration"] = auto_archive_duration
            if slowmode_seconds is not None:
                payload["rate_limit_per_user"] = slowmode_seconds
            if source_message_id:
                path = f"/channels/{parent_channel_id}/messages/{source_message_id}/threads"
            else:
                path = f"/channels/{parent_channel_id}/threads"
                payload["type"] = 11 if kind == "public" else 12
                if invitable is not None:
                    payload["invitable"] = invitable
            created = await self.platform._client.mapping("POST", path, json=payload)
            record = self._thread(created, parent=parent_channel_id)
            if str(created.get("guild_id")) != guild_id:
                raise DiscordError("target_mismatch", "POST", path)
            expected_type = (11 if parent.get("type") == 0 else 10) if source_message_id else (11 if kind == "public" else 12)
            if record["type"] != expected_type or source_message_id and record["thread_id"] != source_message_id:
                raise DiscordError("target_mismatch", "POST", path)
            if kind == "private":
                try:
                    await self.platform._client.request("PUT", f"/channels/{record['thread_id']}/thread-members/{requester}")
                except DiscordError as exc:
                    return json.dumps({"ok": False, "error": "requester_not_added", "detail": str(exc),
                                       "created_thread_id": record["thread_id"]})
            return json.dumps({"ok": True, **record})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def update_discord_thread(
        self, thread_id: str, name: str | None = None, archived: bool | None = None,
        locked: bool | None = None, auto_archive_duration: int | None = None,
        slowmode_seconds: int | None = None, invitable: bool | None = None,
        applied_tag_ids: list[str] | None = None,
    ) -> str:
        if name is not None and (error := self._name(name)):
            return refusal("name_invalid", error)
        if error := self._archive_duration(auto_archive_duration):
            return refusal("auto_archive_duration_invalid", error)
        if error := self._slowmode(slowmode_seconds):
            return refusal("slowmode_seconds_invalid", error)
        for field, value in (("archived", archived), ("locked", locked), ("invitable", invitable)):
            if value is not None and type(value) is not bool:
                return refusal(f"{field}_invalid", f"{field} must be a boolean")
        if applied_tag_ids is not None and (not isinstance(applied_tag_ids, list) or len(applied_tag_ids) > 5
                                            or any(not isinstance(tag, str) or not SNOWFLAKE_RE.fullmatch(tag)
                                                   for tag in applied_tag_ids)):
            return refusal("applied_tag_ids_invalid", "applied_tag_ids must be at most five Discord tag IDs")
        payload = {key: value for key, value in {
            "name": name, "archived": archived, "locked": locked,
            "auto_archive_duration": auto_archive_duration, "rate_limit_per_user": slowmode_seconds,
            "invitable": invitable, "applied_tags": applied_tag_ids,
        }.items() if value is not None}
        if not payload:
            return refusal("changes_required", "provide at least one thread setting to update")
        try:
            thread, requester, granted = await self._thread_for_write(thread_id)
            path = f"/channels/{thread_id}"
            owner = str(thread.get("owner_id") or "")
            if invitable is not None and thread.get("type") != 12:
                return refusal("invitable_invalid", "invitable applies only to private threads")
            moderated_tags: set[str] = set()
            if applied_tag_ids is not None:
                parent = await self.platform._client.mapping("GET", f"/channels/{thread['parent_id']}")
                if parent.get("type") not in (15, 16):
                    return refusal("applied_tag_ids_invalid", "applied_tag_ids require a forum or media thread")
                tags = parent.get("available_tags")
                if not isinstance(tags, list):
                    raise DiscordError("bad_response", "GET", f"/channels/{thread['parent_id']}")
                known = {str(tag.get("id")): tag for tag in tags if isinstance(tag, Mapping)}
                if not set(applied_tag_ids) <= known.keys():
                    return refusal("applied_tag_ids_invalid", "applied_tag_ids must name tags on the thread's parent channel")
                moderated_tags = {ident for ident in {*applied_tag_ids, *(thread.get("applied_tags") or [])}
                                  if isinstance(known.get(ident), Mapping) and known[ident].get("moderated") is True}
            metadata = thread.get("thread_metadata")
            if not isinstance(metadata, Mapping):
                raise DiscordError("bad_response", "GET", path)
            if metadata.get("archived") is True and archived is not False:
                return refusal("thread_archived", "an archived thread must be unarchived in the same update")
            for label, user in (("requester", requester), ("bot", self.platform.bot_user_id)):
                can_manage = bool(granted[user] & MANAGE_THREADS)
                owns_thread = owner == user
                if archived is False and metadata.get("locked") is True and not can_manage:
                    return refusal(f"{label}_cannot_unarchive_locked_thread", "unarchiving a locked thread requires MANAGE_THREADS")
                if (any(field in payload for field in ("locked", "rate_limit_per_user")) or moderated_tags) and not can_manage:
                    return refusal(f"{label}_cannot_manage_thread", "locked, slowmode_seconds, and moderated tags require MANAGE_THREADS")
                if applied_tag_ids is not None and not granted[user] & SEND_THREADS:
                    return refusal(f"{label}_cannot_update_tags", "applied_tag_ids require SEND_MESSAGES_IN_THREADS")
                if not (can_manage or owns_thread) and any(field in payload for field in ("name", "archived", "auto_archive_duration", "invitable")):
                    if archived is not False or metadata.get("locked") is True or len(payload) != 1:
                        return refusal(f"{label}_cannot_update_thread", "thread changes require MANAGE_THREADS or thread creator authority")
                    if not granted[user] & SEND_THREADS:
                        return refusal(f"{label}_cannot_unarchive_thread", "unarchiving an unlocked thread requires SEND_MESSAGES_IN_THREADS")
            changed = await self.platform._client.mapping("PATCH", path, json=payload)
            record = self._thread(changed, parent=str(thread["parent_id"]))
            if str(changed.get("guild_id")) != str(thread.get("guild_id")):
                raise DiscordError("target_mismatch", "PATCH", path)
            return json.dumps({"ok": True, **record})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def change_discord_thread_member(
        self, thread_id: str, action: Literal["join", "leave", "add", "remove"], user_id: str | None = None,
    ) -> str:
        if action not in ("join", "leave", "add", "remove"):
            return refusal("action_invalid", "action must be join, leave, add, or remove")
        if (action in ("add", "remove")) != (user_id is not None):
            return refusal("user_id_invalid", "user_id is required for add or remove and must be omitted for join or leave")
        try:
            thread, requester, granted = await self._thread_for_write(thread_id)
            metadata = thread.get("thread_metadata")
            if not isinstance(metadata, Mapping) or metadata.get("archived") is not False:
                return refusal("thread_archived", "membership changes require an active thread")
            if action == "add":
                if self._invite_requires_manager(thread) and not granted[requester] & MANAGE_THREADS:
                    return refusal("requester_cannot_invite_to_private_thread", "thread_metadata.invitable=false requires requester MANAGE_THREADS")
                if not all(granted[user] & SEND_THREADS for user in (requester, self.platform.bot_user_id)):
                    return refusal("cannot_add_thread_member", "requester and bot need SEND_MESSAGES_IN_THREADS")
                member = await self.platform._client.mapping("GET", f"/guilds/{thread['guild_id']}/members/{user_id}")
                if str(member.get("user", {}).get("id")) != user_id:
                    raise DiscordError("target_mismatch", "GET", f"/guilds/{thread['guild_id']}/members/{user_id}")
            elif action == "remove":
                owner = str(thread.get("owner_id") or "")
                for user in (requester, self.platform.bot_user_id):
                    if not (granted[user] & MANAGE_THREADS or thread.get("type") == 12 and owner == user):
                        return refusal("cannot_remove_thread_member", "removing another member needs MANAGE_THREADS or private-thread creator authority")
            path = f"/channels/{thread_id}/thread-members/{user_id}" if user_id else f"/channels/{thread_id}/thread-members/@me"
            await self.platform._client.request("PUT" if action in ("join", "add") else "DELETE", path)
            return json.dumps({"ok": True, "thread_id": thread_id, "action": action,
                               "user_id": user_id or self.platform.bot_user_id})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @staticmethod
    def _archive_cursor(value: str | None, mode: str) -> str | None:
        if value is None:
            return None
        if mode == "joined_private_archived":
            if not isinstance(value, str) or not SNOWFLAKE_RE.fullmatch(value):
                raise ValueError("before must be a Discord thread ID of 17 to 20 digits")
        else:
            try:
                if datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("before must be an ISO8601 timestamp with timezone") from None
        return value

    @staticmethod
    def _thread(raw: Mapping[str, Any], *, parent: str = "") -> dict[str, Any]:
        ident = str(raw.get("id") or "")
        parent_id = str(raw.get("parent_id") or "")
        if not SNOWFLAKE_RE.fullmatch(ident) or not SNOWFLAKE_RE.fullmatch(parent_id) or parent and parent_id != parent:
            raise DiscordError("target_mismatch", "GET", "/threads")
        metadata = raw.get("thread_metadata")
        if not isinstance(metadata, Mapping) or not isinstance(metadata.get("archived"), bool):
            raise DiscordError("bad_response", "GET", "/threads")
        return {
            "thread_id": ident, "parent_channel_id": parent_id, "type": raw.get("type"),
            "name": raw.get("name"), "archived": metadata["archived"],
            "archive_timestamp": metadata.get("archive_timestamp"),
            "last_message_id": raw.get("last_message_id"), "owner_id": raw.get("owner_id"),
        }

    @checks_discord_ids
    async def list_discord_threads(
        self, mode: Literal["active", "public_archived", "private_archived", "joined_private_archived"] = "active",
        parent_channel_id: str | None = None, before: str | None = None,
        limit: int | None = None, joined_only: bool = False,
    ) -> str:
        if mode not in {"active", *_ARCHIVED}:
            return refusal("mode_invalid", "mode must be active, public_archived, private_archived, or joined_private_archived")
        if not isinstance(joined_only, bool):
            return refusal("joined_only_invalid", "joined_only must be a boolean")
        if limit is not None and (type(limit) is not int or limit < 1):
            return refusal("limit_invalid", "limit must be a positive integer")
        if mode == "active" and (before is not None or limit is not None):
            return refusal("active_listing_unpaged", "before and limit apply only to archived threads")
        if mode != "active" and limit is not None and not 2 <= limit <= 100:
            return refusal("limit_invalid", "limit must be an integer from 2 to 100 for archived threads")
        try:
            cursor = self._archive_cursor(before, mode) if mode != "active" else None
        except ValueError as exc:
            return refusal("before_invalid", str(exc))
        try:
            origin, requester, guild_id = self._turn()
            if parent_channel_id:
                parent_record = await self.platform._client.mapping("GET", f"/channels/{parent_channel_id}")
                if str(parent_record.get("id")) != parent_channel_id or parent_record.get("type") not in (0, 5, 15, 16):
                    raise DiscordError("parent_channel_required", "GET", f"/channels/{parent_channel_id}")
                guild_id = str(parent_record.get("guild_id") or "")
            if not guild_id:
                raise DiscordError("guild_context_required", "GET", "/guilds/threads/active")
            if mode == "active":
                if parent_channel_id:
                    await self._readable(parent_channel_id, requester, history_audience=False)
                await self._guild_members(guild_id, requester)
                path = f"/guilds/{guild_id}/threads/active"
                result = await self.platform._client.mapping("GET", path)
                has_more = False
            else:
                parent = parent_channel_id or origin
                channel = await self.platform._client.mapping("GET", f"/channels/{parent}")
                if channel.get("type") in THREAD_TYPES and not parent_channel_id:
                    parent = str(channel.get("parent_id") or "")
                    channel = await self.platform._client.mapping("GET", f"/channels/{parent}")
                if str(channel.get("id")) != parent or channel.get("type") not in (0, 5, 15, 16):
                    raise DiscordError("parent_channel_required", "GET", f"/channels/{parent}")
                guild_id = await self._readable(parent, requester, history_audience=False)
                if mode == "private_archived":
                    guild, members = await self._guild_members(guild_id, requester)
                    try:
                        granted = {user: permissions(guild, channel, members[user])
                                   for user in (requester, self.platform.bot_user_id)}
                    except (KeyError, TypeError, ValueError):
                        raise DiscordError("bad_response", "GET", f"/channels/{parent}") from None
                    if not granted[requester] & MANAGE_THREADS:
                        raise DiscordError("requester_cannot_list_private_threads", "GET", f"/channels/{parent}")
                    if not granted[self.platform.bot_user_id] & MANAGE_THREADS:
                        raise DiscordError("bot_cannot_list_private_threads", "GET", f"/channels/{parent}")
                path = (f"/channels/{parent}/{_ARCHIVED[mode]}" if mode == "joined_private_archived"
                        else f"/channels/{parent}/threads/archived/{_ARCHIVED[mode]}")
                params: dict[str, Any] = {}
                if cursor is not None:
                    params["before"] = cursor
                if limit is not None:
                    params["limit"] = limit
                result = await self.platform._client.mapping("GET", path, params=params)
                has_more = result.get("has_more")
                if not isinstance(has_more, bool):
                    raise DiscordError("bad_response", "GET", path)
            raw_threads = result.get("threads")
            if not isinstance(raw_threads, list) or any(not isinstance(item, Mapping) for item in raw_threads):
                raise DiscordError("bad_response", "GET", path)
            records = []
            excluded = 0
            unavailable = 0
            for raw in raw_threads:
                item = self._thread(raw, parent=parent if mode != "active" else "")
                if str(raw.get("guild_id")) != guild_id or raw.get("type") not in THREAD_TYPES:
                    raise DiscordError("target_mismatch", "GET", path)
                if parent_channel_id and item["parent_channel_id"] != parent_channel_id:
                    continue
                try:
                    await self._readable(item["thread_id"], requester, history_audience=False)
                    membership = {}
                    for user, label in ((requester, "requester"), (self.platform.bot_user_id, "bot")):
                        try:
                            member = await self.platform._client.mapping("GET", f"/channels/{item['thread_id']}/thread-members/{user}")
                        except DiscordError as exc:
                            if exc.status not in (403, 404):
                                raise
                            membership[label] = "not_joined" if exc.status == 404 else "unknown"
                        else:
                            membership[label] = "joined" if str(member.get("user_id")) == user else "unknown"
                    item["membership"] = membership
                    if joined_only and membership["requester"] != "joined":
                        excluded += 1
                        continue
                except DiscordError as exc:
                    if exc.code == "read_audience_evidence_unavailable":
                        unavailable += 1
                        continue
                    if exc.code in _HIDDEN | {"not_found"}:
                        excluded += 1
                        continue
                    raise
                records.append(item)
            if has_more and not raw_threads:
                raise DiscordError("bad_response", "GET", path)
            last = self._thread(raw_threads[-1]) if has_more else None
            next_before = (last["thread_id"] if mode == "joined_private_archived"
                           else last["archive_timestamp"]) if last else None
            if has_more and not next_before:
                raise DiscordError("bad_response", "GET", path)
            return json.dumps({"ok": True, "mode": mode, "threads": records,
                               "coverage": {"status": "partial" if has_more or unavailable else "complete",
                                            "unavailable_threads": unavailable,
                                            "unavailable_reason": "read_audience_evidence_unavailable" if unavailable else None,
                                            "guild_id": guild_id, "parent_channel_id": parent_channel_id or (parent if mode != "active" else None),
                                            "visible_source_audience_only": True, "excluded_threads": excluded,
                                            "next_before": next_before,
                                            "cursor_type": "thread_id" if mode == "joined_private_archived" else "timestamp" if mode != "active" else None}})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def list_discord_thread_members(self, thread_id: str, after: str | None = None, limit: int = 100) -> str:
        if not self.platform._settings.guild_members:
            return refusal("guild_members_intent_required", "list_discord_thread_members requires platforms.discord.guild_members: true")
        if after is not None and not SNOWFLAKE_RE.fullmatch(after):
            return refusal("after_invalid", "after must be a Discord user ID of 17 to 20 digits")
        if type(limit) is not int or not 1 <= limit <= 100:
            return refusal("limit_invalid", "limit must be an integer from 1 to 100")
        try:
            _, requester, _ = self._turn()
            channel = await self.platform._client.mapping("GET", f"/channels/{thread_id}")
            if str(channel.get("id")) != thread_id or channel.get("type") not in THREAD_TYPES:
                raise DiscordError("thread_id_not_thread", "GET", f"/channels/{thread_id}")
            await self._readable(thread_id, requester, history_audience=False)
            path = f"/channels/{thread_id}/thread-members"
            params: dict[str, Any] = {"with_member": "true", "limit": limit}
            if after:
                params["after"] = after
            page = await self.platform._client.request("GET", path, params=params)
            if not isinstance(page, list):
                raise DiscordError("bad_response", "GET", path)
            members = []
            for item in page:
                if not isinstance(item, Mapping) or not SNOWFLAKE_RE.fullmatch(str(item.get("user_id") or "")):
                    raise DiscordError("bad_response", "GET", path)
                members.append({"user_id": str(item["user_id"]), "join_timestamp": item.get("join_timestamp"),
                                "member": item.get("member")})
            return json.dumps({"ok": True, "thread_id": thread_id, "members": members,
                               "coverage": {"status": "partial" if len(page) == limit else "complete",
                                            "next_after": members[-1]["user_id"] if len(page) == limit else None}})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def read_discord_poll(
        self, message_id: str, chat_id: str | None = None, answer_id: int | None = None,
        after: str | None = None, limit: int = 25,
    ) -> str:
        if answer_id is not None and (type(answer_id) is not int or answer_id < 1):
            return refusal("answer_id_invalid", "answer_id must be a positive poll answer integer")
        if answer_id is None and (after is not None or limit != 25):
            return refusal("answer_id_required", "after and limit require answer_id")
        if after is not None and not SNOWFLAKE_RE.fullmatch(after):
            return refusal("after_invalid", "after must be a Discord user ID of 17 to 20 digits")
        if type(limit) is not int or not 1 <= limit <= 100:
            return refusal("limit_invalid", "limit must be an integer from 1 to 100")
        try:
            target = self._target(chat_id)
            _, requester, _ = self._turn()
            _, message, _ = await self._one(target, message_id, requester)
            poll = message.get("poll")
            if not isinstance(poll, Mapping) or not isinstance(poll.get("answers"), list):
                raise DiscordError("message_has_no_poll", "GET", f"/channels/{target}/messages/{message_id}")
            results = poll.get("results")
            if results is not None and not isinstance(results, Mapping):
                raise DiscordError("bad_response", "GET", f"/channels/{target}/messages/{message_id}")
            body: dict[str, Any] = {
                "ok": True, "chat_id": target, "message_id": message_id,
                "poll": {"question": poll.get("question"), "answers": poll["answers"],
                         "expiry": poll.get("expiry"), "allow_multiselect": poll.get("allow_multiselect"),
                         "results": results},
                "counts_status": "unknown" if results is None else "final" if results.get("is_finalized") is True else "approximate",
            }
            if answer_id is not None:
                if answer_id not in {item.get("answer_id") for item in poll["answers"] if isinstance(item, Mapping)}:
                    return refusal("answer_id_not_in_poll", "answer_id must name an answer in this poll")
                path = f"/channels/{target}/polls/{message_id}/answers/{answer_id}"
                params: dict[str, Any] = {"limit": limit}
                if after:
                    params["after"] = after
                page = await self.platform._client.mapping("GET", path, params=params)
                users = page.get("users")
                if not isinstance(users, list) or any(not isinstance(user, Mapping)
                       or not SNOWFLAKE_RE.fullmatch(str(user.get("id") or "")) for user in users):
                    raise DiscordError("bad_response", "GET", path)
                body["voters"] = [{"user_id": str(user["id"]), "username": user.get("username")} for user in users]
                body["coverage"] = {"status": "partial" if len(users) == limit else "complete",
                                    "next_after": str(users[-1]["id"]) if len(users) == limit else None}
            return json.dumps(body)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def end_discord_poll(self, message_id: str, chat_id: str | None = None) -> str:
        try:
            target = self._target(chat_id)
            _, requester, _ = self._turn()
            await self._readable(target, requester)
            message, _, newcomers = await self._message_for(target, message_id, write=True)
            if newcomers:
                raise DiscordError("write_target_widens_audience", "POST", f"/channels/{target}")
            author = message.get("author")
            if not isinstance(author, Mapping) or str(author.get("id")) != self.platform.bot_user_id:
                raise DiscordError("poll_not_owned_by_bot", "POST", f"/channels/{target}/polls/{message_id}")
            poll = message.get("poll")
            if not isinstance(poll, Mapping):
                raise DiscordError("message_has_no_poll", "POST", f"/channels/{target}/polls/{message_id}")
            results = poll.get("results")
            if isinstance(results, Mapping) and results.get("is_finalized") is True:
                raise DiscordError("poll_already_finalized", "POST", f"/channels/{target}/polls/{message_id}")
            path = f"/channels/{target}/polls/{message_id}/expire"
            result = await self.platform._client.mapping("POST", path)
            if str(result.get("id")) != message_id or str(result.get("channel_id")) != target:
                raise DiscordError("target_mismatch", "POST", path)
            return json.dumps({"ok": True, "chat_id": target, "message_id": message_id,
                               "poll": result.get("poll"), "ended": True})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))
