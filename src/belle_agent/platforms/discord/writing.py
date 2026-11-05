"""Discord message changes by the current bot, within the current turn's audience."""

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE, checks_discord_ids
from belle_agent.platforms.discord.reading import DiscordReadingToolkit
from belle_agent.platforms.discord.targets import READ_HISTORY, VIEW, TargetResolver, permissions
from belle_agent.tools.host import refusal

ADD_REACTIONS = 1 << 6
PIN_MESSAGES = 1 << 51
MANAGE_THREADS = 1 << 34
PINNED_THREAD = 1 << 1
_CUSTOM_EMOJI = re.compile(r"\A[^:\s]{1,32}:[0-9]{17,20}\Z")
_ID = {"type": "string", "description": "Discord Snowflake ID."}


def _card(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description,
            "input_params": {"type": "object", "properties": properties, "required": required}}


def _emoji(value: Any) -> str:
    if not isinstance(value, str) or not value or ":" in value and not _CUSTOM_EMOJI.fullmatch(value):
        raise ValueError("emoji must be Unicode or a custom name:Discord-ID")
    return urllib.parse.quote(value, safe="")


def _has_reaction(message: Mapping[str, Any], emoji: str) -> bool:
    name, _, ident = emoji.partition(":")
    return any(isinstance(item, Mapping) and isinstance(item.get("emoji"), Mapping)
               and item["emoji"].get("name") == name
               and (not ident or str(item["emoji"].get("id")) == ident)
               for item in message.get("reactions", ()))


class DiscordWritingToolkit(DiscordReadingToolkit):
    """Message companions for the current Discord installation and requester."""

    def __init__(self, platform: Any, metadata_provider: Callable[[], Mapping[str, Any]]) -> None:
        super().__init__(platform, metadata_provider)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        target = {"chat_id": _ID, "message_id": _ID}
        return [
            _card("react_to_message", "Add or remove this bot's reaction to a readable Discord message.",
                  {**target, "emoji": {"type": "string", "description": "Unicode emoji or custom name:ID."},
                   "remove": {"type": "boolean", "default": False}}, ["emoji"]),
            _card("read_discord_reactions", "Page users who reacted to a readable message with one emoji."
                  " Coverage names the next user ID when more may remain.",
                  {**target, "emoji": {"type": "string"}, "type": {"type": "integer", "enum": [0, 1]},
                   "after": _ID, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                  ["message_id", "emoji"]),
            _card("read_pinned_messages", "Page pinned messages in a readable channel, newest pin first.",
                  {"chat_id": _ID, "before": {"type": "string", "description": "ISO8601 pin timestamp from a prior page."},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, []),
            _card("pin_message", "Pin or unpin a verified message if the requester and bot can pin there.",
                  {**target, "remove": {"type": "boolean", "default": False}}, ["message_id"]),
            _card(
                "set_discord_thread_pinned",
                "Pin or unpin a forum or media post thread when the requester and bot can manage threads there.",
                {"chat_id": _ID, "pinned": {"type": "boolean", "description": "True pins the thread; false unpins it."}},
                ["pinned"],
            ),
        ]

    async def _access(self, chat_id: str, *, write: bool, extra: int = 0) -> tuple[str, frozenset[str], frozenset[str]]:
        source, requester, _ = self._turn()
        if not source:
            raise DiscordError("source_conversation_required", "GET", f"/channels/{chat_id}")
        resolver = TargetResolver(self.platform)
        metadata = self.metadata_provider()
        origin = str(metadata.get("discord_thread_id") or source)
        target = await resolver.resolve(chat_id, requester, posting=False)
        source_target = target if origin == chat_id else await resolver.resolve(origin, requester, posting=False)
        newcomers = target.readers - source_target.readers if write else frozenset()
        if not write and not source_target.readers <= target.history_readers:
            raise DiscordError("read_would_disclose_to_other_readers", "GET", f"/channels/{chat_id}")
        if target.guild:
            guild, members = await self._guild_members(target.guild, requester)
            channel = await self.platform._client.mapping("GET", f"/channels/{target.parent or chat_id}")
            if str(channel.get("id")) != (target.parent or chat_id):
                raise DiscordError("target_mismatch", "GET", f"/channels/{target.parent or chat_id}")
            for user, label in ((requester, "requester"), (self.platform.bot_user_id, "bot")):
                try:
                    granted = permissions(guild, channel, members[user])
                except (KeyError, TypeError, ValueError):
                    raise DiscordError("bad_response", "GET", f"/guilds/{target.guild}/members/{user}") from None
                needed = VIEW | READ_HISTORY | extra
                if granted & needed != needed:
                    raise DiscordError(f"{label}_cannot_{'write' if write else 'read'}_target", "GET", f"/channels/{chat_id}")
        elif target.readers != frozenset({requester, self.platform.bot_user_id}):
            raise DiscordError("requester_cannot_access_target", "GET", f"/channels/{chat_id}")
        return target.guild, target.readers, newcomers

    async def _message_for(self, chat_id: str, message_id: str, *, write: bool, extra: int = 0) -> tuple[Mapping[str, Any], frozenset[str], frozenset[str]]:
        _, readers, newcomers = await self._access(chat_id, write=write, extra=extra)
        path = f"/channels/{chat_id}/messages/{message_id}"
        message = await self.platform._client.mapping("GET", path)
        if str(message.get("id")) != message_id or str(message.get("channel_id")) != chat_id:
            raise DiscordError("target_mismatch", "GET", path)
        return message, readers, newcomers

    def _target(self, chat_id: str | None) -> str:
        source, _, _ = self._turn()
        target = chat_id or str(self.metadata_provider().get("discord_thread_id") or source)
        if not target:
            raise DiscordError("trusted_conversation_context_required", "GET", "/channels")
        return target

    @checks_discord_ids
    async def read_discord_reactions(self, message_id: str, emoji: str, chat_id: str | None = None,
                                     type: int = 0, after: str | None = None, limit: int = 25) -> str:
        if type not in (0, 1) or isinstance(type, bool):
            return refusal("type_invalid", "type must be 0 (normal) or 1 (burst)")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            return refusal("limit_invalid", "limit must be an integer from 1 to 100")
        if after is not None and (not isinstance(after, str) or not SNOWFLAKE_RE.fullmatch(after)):
            return refusal("after_invalid", "after must be a Discord user ID of 17 to 20 digits")
        try:
            encoded = _emoji(emoji)
            target = self._target(chat_id)
            await self._message_for(target, message_id, write=False)
            path = f"/channels/{target}/messages/{message_id}/reactions/{encoded}"
            params: dict[str, int | str] = {"type": type, "limit": limit}
            if after:
                params["after"] = after
            page = await self.platform._client.request("GET", path, params=params)
            if not isinstance(page, list) or any(not isinstance(user, Mapping) or not SNOWFLAKE_RE.fullmatch(str(user.get("id") or "")) for user in page):
                raise DiscordError("bad_response", "GET", path)
            users = [{"user_id": str(user["id"]), "username": user.get("username")} for user in page]
            return json.dumps({"ok": True, "chat_id": target, "message_id": message_id, "emoji": emoji,
                               "type": type, "users": users, "coverage": {"status": "partial" if len(page) == limit else "complete",
                               "next_after": users[-1]["user_id"] if len(page) == limit else None}})
        except ValueError as exc:
            return refusal("emoji_invalid", str(exc))
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def read_pinned_messages(self, chat_id: str | None = None, before: str | None = None, limit: int = 50) -> str:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            return refusal("limit_invalid", "limit must be an integer from 1 to 50")
        if before is not None:
            try:
                parsed = datetime.fromisoformat(before)
                if parsed.tzinfo is None:
                    raise ValueError("timezone required")
            except (AttributeError, ValueError):
                return refusal("before_invalid", "before must be an ISO8601 timestamp")
        try:
            target = self._target(chat_id)
            await self._access(target, write=False)
            path = f"/channels/{target}/messages/pins"
            params: dict[str, Any] = {"limit": limit}
            if before:
                params["before"] = before
            result = await self.platform._client.mapping("GET", path, params=params)
            items = result.get("items")
            if not isinstance(items, list) or not isinstance(result.get("has_more"), bool):
                raise DiscordError("bad_response", "GET", path)
            pins = []
            for item in items:
                if not isinstance(item, Mapping) or not isinstance(item.get("message"), Mapping):
                    raise DiscordError("bad_response", "GET", path)
                msg = item["message"]
                if str(msg.get("channel_id")) != target or not SNOWFLAKE_RE.fullmatch(str(msg.get("id") or "")):
                    raise DiscordError("target_mismatch", "GET", path)
                if not isinstance(item.get("pinned_at"), str):
                    raise DiscordError("bad_response", "GET", path)
                pins.append({"message_id": str(msg["id"]), "pinned_at": item["pinned_at"],
                             "content": msg.get("content", ""), "author_id": str(msg.get("author", {}).get("id") or "")})
            if result["has_more"] and not pins:
                raise DiscordError("bad_response", "GET", path)
            return json.dumps({"ok": True, "chat_id": target, "pins": pins,
                               "coverage": {"status": "partial" if result["has_more"] else "complete",
                                            "next_before": pins[-1]["pinned_at"] if pins and result["has_more"] else None}})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def react_to_message(self, emoji: str, message_id: str | None = None, chat_id: str | None = None,
                               remove: bool = False) -> str:
        if not isinstance(remove, bool):
            return refusal("remove_invalid", "remove must be a boolean")
        try:
            encoded = _emoji(emoji)
            target = self._target(chat_id)
            message_id = message_id or str(self.metadata_provider().get("discord_message_id") or "")
            if not SNOWFLAKE_RE.fullmatch(message_id):
                return refusal("message_id_required", "message_id or an initiating message is required")
            message, _, newcomers = await self._message_for(target, message_id, write=True)
            if newcomers:
                raise DiscordError("write_target_widens_audience", "PUT", f"/channels/{target}")
            existing = _has_reaction(message, emoji)
            if not remove and not existing:
                await self._access(target, write=True, extra=ADD_REACTIONS)
            path = f"/channels/{target}/messages/{message_id}/reactions/{encoded}/@me"
            await self.platform._client.request("DELETE" if remove else "PUT", path)
            return json.dumps({"ok": True, "chat_id": target, "message_id": message_id, "emoji": emoji,
                               "reacted": not remove})
        except ValueError as exc:
            return refusal("emoji_invalid", str(exc))
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def pin_message(self, message_id: str, chat_id: str | None = None, remove: bool = False) -> str:
        if not isinstance(remove, bool):
            return refusal("remove_invalid", "remove must be a boolean")
        try:
            target = self._target(chat_id)
            _, _, newcomers = await self._message_for(target, message_id, write=True, extra=PIN_MESSAGES)
            if newcomers:
                raise DiscordError("write_target_widens_audience", "PUT", f"/channels/{target}")
            path = f"/channels/{target}/messages/pins/{message_id}"
            await self.platform._client.request("DELETE" if remove else "PUT", path)
            return json.dumps({"ok": True, "chat_id": target, "message_id": message_id, "pinned": not remove})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def set_discord_thread_pinned(self, pinned: bool, chat_id: str | None = None) -> str:
        if not isinstance(pinned, bool):
            return refusal("pinned_invalid", "pinned must be a boolean")
        try:
            target = self._target(chat_id)
            _, _, newcomers = await self._access(target, write=True, extra=MANAGE_THREADS)
            if newcomers:
                raise DiscordError("write_target_widens_audience", "PATCH", f"/channels/{target}")
            thread = await self.platform._client.mapping("GET", f"/channels/{target}")
            parent_id = str(thread.get("parent_id") or "")
            if str(thread.get("id") or "") != target or thread.get("type") not in (11, 12) or not parent_id:
                raise DiscordError("forum_or_media_thread_required", "GET", f"/channels/{target}")
            parent = await self.platform._client.mapping("GET", f"/channels/{parent_id}")
            if str(parent.get("id") or "") != parent_id or parent.get("type") not in (15, 16):
                raise DiscordError("forum_or_media_thread_required", "GET", f"/channels/{parent_id}")
            flags = thread.get("flags", 0)
            if type(flags) is not int or flags < 0:
                raise DiscordError("bad_response", "GET", f"/channels/{target}")
            requested = flags | PINNED_THREAD if pinned else flags & ~PINNED_THREAD
            if requested != flags:
                changed = await self.platform._client.mapping("PATCH", f"/channels/{target}", json={"flags": requested})
                if str(changed.get("id") or "") != target or changed.get("flags", 0) != requested:
                    raise DiscordError("pin_state_unconfirmed", "PATCH", f"/channels/{target}")
            return json.dumps({"ok": True, "chat_id": target, "pinned": pinned})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))
