"""Read guild events and sounds through the current Discord requester."""

import json
from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE, checks_discord_ids
from belle_agent.platforms.discord.reading import DiscordReadingToolkit, _card
from belle_agent.platforms.discord.targets import Target, TargetResolver
from belle_agent.tools.host import refusal

_ID = {"type": "string", "pattern": SNOWFLAKE_RE.pattern, "description": "Discord Snowflake ID."}


class DiscordResourcesToolkit(DiscordReadingToolkit):
    """Read guild-wide resources without disclosing private channel events."""

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [*super().get_tool_definitions(),
                _card("list_discord_scheduled_events", "List guild scheduled events visible to this requester's conversation. Events in private channels are omitted when this conversation has a wider audience.", {
                    "guild_id": _ID, "with_user_count": {"type": "boolean"},
                }),
                _card("get_discord_scheduled_event", "Read one guild scheduled event if its channel is visible to the requester, bot and source conversation.", {
                    "guild_id": _ID, "event_id": _ID, "with_user_count": {"type": "boolean"},
                }, ["event_id"]),
                _card("list_discord_event_users", "Read one page of subscribers to a visible guild event. Use next_after or before to continue.", {
                    "guild_id": _ID, "event_id": _ID,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "before": _ID, "after": _ID, "with_member": {"type": "boolean"},
                }, ["event_id"]),
                _card("list_discord_soundboard_sounds", "List the guild's soundboard sounds when the requester and bot are guild members.", {"guild_id": _ID}),
                _card("get_discord_soundboard_sound", "Read one soundboard sound from a guild the requester and bot belong to.", {
                    "guild_id": _ID, "sound_id": _ID,
                }, ["sound_id"]),
                _card("list_discord_default_sounds", "List Discord's default soundboard sounds, available to all users.", {}),
                ]

    async def _guild_context(self, guild_id: str | None) -> tuple[str, str, Target]:
        source, requester, origin_guild = self._turn()
        target_guild = guild_id or origin_guild
        if not target_guild:
            raise DiscordError("guild_required", "GET", "/guilds")
        source_target = await TargetResolver(self.platform).resolve(source, requester, posting=False, visibility_only=True)
        if source_target.guild and source_target.guild != target_guild:
            raise DiscordError("read_would_disclose_to_other_readers", "GET", f"/guilds/{target_guild}")
        await self._guild_members(target_guild, requester)
        return target_guild, requester, source_target

    async def _visible_event(self, event: Mapping[str, Any], guild_id: str, requester: str,
                             source: Target) -> bool:
        if not SNOWFLAKE_RE.fullmatch(str(event.get("id") or "")) or str(event.get("guild_id")) != guild_id:
            raise DiscordError("target_mismatch", "GET", f"/guilds/{guild_id}/scheduled-events")
        if "channel_id" not in event or event.get("entity_type") not in (1, 2, 3):
            raise DiscordError("bad_response", "GET", f"/guilds/{guild_id}/scheduled-events")
        channel_id = event["channel_id"]
        if channel_id is None:
            if event["entity_type"] != 3:
                raise DiscordError("bad_response", "GET", f"/guilds/{guild_id}/scheduled-events")
            return True
        if event["entity_type"] == 3:
            raise DiscordError("bad_response", "GET", f"/guilds/{guild_id}/scheduled-events")
        if not SNOWFLAKE_RE.fullmatch(str(channel_id)):
            raise DiscordError("bad_response", "GET", f"/guilds/{guild_id}/scheduled-events")
        try:
            channel = await TargetResolver(self.platform).resolve(str(channel_id), requester, posting=False, visibility_only=True)
        except DiscordError as exc:
            if exc.status in (403, 404) or exc.code in {
                "requester_cannot_access_target", "bot_cannot_post_target", "unsupported_post_target"
            }:
                return False
            raise
        return channel.guild == guild_id and source.readers <= channel.readers

    @checks_discord_ids
    async def list_discord_scheduled_events(self, guild_id: str | None = None,
                                            with_user_count: bool = False) -> str:
        if type(with_user_count) is not bool:
            return refusal("with_user_count_invalid", "with_user_count must be true or false")
        try:
            guild, requester, source = await self._guild_context(guild_id)
            path = f"/guilds/{guild}/scheduled-events"
            events = await self.platform._client.request("GET", path, params={"with_user_count": str(with_user_count).lower()})
            if not isinstance(events, list) or any(not isinstance(item, Mapping) for item in events):
                raise DiscordError("bad_response", "GET", path)
            visible = [item for item in events if await self._visible_event(item, guild, requester, source)]
            return json.dumps({"ok": True, "guild_id": guild, "events": visible,
                               "coverage": {"status": "complete", "hidden_for_audience": len(events) - len(visible)}}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def get_discord_scheduled_event(self, event_id: str, guild_id: str | None = None,
                                          with_user_count: bool = False) -> str:
        if type(with_user_count) is not bool:
            return refusal("with_user_count_invalid", "with_user_count must be true or false")
        try:
            guild, requester, source = await self._guild_context(guild_id)
            path = f"/guilds/{guild}/scheduled-events/{event_id}"
            event = await self.platform._client.mapping("GET", path, params={"with_user_count": str(with_user_count).lower()})
            if str(event.get("id")) != event_id:
                raise DiscordError("target_mismatch", "GET", path)
            if not await self._visible_event(event, guild, requester, source):
                return refusal("requester_cannot_read_target", "the event's channel is outside this conversation's audience")
            return json.dumps({"ok": True, "guild_id": guild, "event": event}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def list_discord_event_users(self, event_id: str, guild_id: str | None = None,
                                       limit: int = 100, before: str | None = None,
                                       after: str | None = None, with_member: bool = False) -> str:
        if type(limit) is not int or not 1 <= limit <= 100:
            return refusal("limit_invalid", "limit must be an integer from 1 to 100")
        if before and after:
            return refusal("cursor_conflict", "provide before or after, not both")
        for name, value in (("before", before), ("after", after)):
            if value is not None and (not isinstance(value, str) or not SNOWFLAKE_RE.fullmatch(value)):
                return refusal(f"{name}_invalid", f"{name} must be a Discord user ID of 17 to 20 digits")
        if type(with_member) is not bool:
            return refusal("with_member_invalid", "with_member must be true or false")
        try:
            guild, requester, source = await self._guild_context(guild_id)
            event_path = f"/guilds/{guild}/scheduled-events/{event_id}"
            event = await self.platform._client.mapping("GET", event_path)
            if str(event.get("id")) != event_id or not await self._visible_event(event, guild, requester, source):
                return refusal("requester_cannot_read_target", "the event is outside this conversation's audience")
            path = f"{event_path}/users"
            params: dict[str, Any] = {"limit": limit, "with_member": str(with_member).lower()}
            if before:
                params["before"] = before
            if after:
                params["after"] = after
            users = await self.platform._client.request("GET", path, params=params)
            if not isinstance(users, list) or any(not isinstance(item, Mapping) or not isinstance(item.get("user"), Mapping)
                                                  or not SNOWFLAKE_RE.fullmatch(str(item["user"].get("id") or "")) for item in users):
                raise DiscordError("bad_response", "GET", path)
            return json.dumps({"ok": True, "guild_id": guild, "event_id": event_id, "users": users,
                               "coverage": {"status": "partial" if len(users) == limit else "complete",
                                            "next_after": str(users[-1]["user"]["id"]) if len(users) == limit else None}}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def list_discord_soundboard_sounds(self, guild_id: str | None = None) -> str:
        try:
            guild, _, _ = await self._guild_context(guild_id)
            path = f"/guilds/{guild}/soundboard-sounds"
            response = await self.platform._client.mapping("GET", path)
            items = response.get("items")
            if not isinstance(items, list) or any(not isinstance(item, Mapping) or item.get("guild_id", guild) != guild
                                                   for item in items):
                raise DiscordError("bad_response", "GET", path)
            return json.dumps({"ok": True, "guild_id": guild, "sounds": items, "coverage": {"status": "complete"}}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def get_discord_soundboard_sound(self, sound_id: str, guild_id: str | None = None) -> str:
        try:
            guild, _, _ = await self._guild_context(guild_id)
            path = f"/guilds/{guild}/soundboard-sounds/{sound_id}"
            sound = await self.platform._client.mapping("GET", path)
            if str(sound.get("sound_id")) != sound_id or str(sound.get("guild_id", guild)) != guild:
                raise DiscordError("target_mismatch", "GET", path)
            return json.dumps({"ok": True, "guild_id": guild, "sound": sound}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    async def list_discord_default_sounds(self) -> str:
        try:
            source, requester, _ = self._turn()
            await TargetResolver(self.platform).resolve(source, requester, posting=False, visibility_only=True)
            path = "/soundboard-default-sounds"
            sounds = await self.platform._client.request("GET", path)
            if not isinstance(sounds, list) or any(not isinstance(item, Mapping) for item in sounds):
                raise DiscordError("bad_response", "GET", path)
            return json.dumps({"ok": True, "sounds": sounds, "coverage": {"status": "complete"}}, ensure_ascii=False)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))
