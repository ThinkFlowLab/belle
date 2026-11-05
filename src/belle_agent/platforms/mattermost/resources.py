"""Discover Mattermost groups and manage channel bookmarks."""

import json
from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.mattermost.client import MattermostError
from belle_agent.platforms.mattermost.directory import MembersIncomplete
from belle_agent.platforms.mattermost.ids import checks_mattermost_ids
from belle_agent.platforms.mattermost.reading import MattermostReadingToolkit
from belle_agent.platforms.mattermost.toolkit import _Refusal, _refused, _server_refused

_BOOKMARK_ACTIONS = frozenset({"create_mattermost_bookmark", "edit_mattermost_bookmark", "delete_mattermost_bookmark"})


def _card(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description,
            "input_params": {"type": "object", "properties": properties, "required": required or []}}


_CHAT = {"type": "string", "description": "Channel ID; omit for this conversation."}
_ID = {"type": "string", "description": "Mattermost ID."}
_FIELDS = {
    "display_name": {"type": "string", "description": "Bookmark label."},
    "type": {"type": "string", "enum": ["link", "file"]},
    "link_url": {"type": "string", "description": "Required for a link bookmark."},
    "file_id": _ID,
    "image_url": {"type": "string"},
    "emoji": {"type": "string"},
}


class MattermostResourcesToolkit(MattermostReadingToolkit):
    """Turn-bound resource reads and bookmark writes."""

    active_turn_only = _BOOKMARK_ACTIONS

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [*super().get_tool_definitions(),
                _card("list_mattermost_groups", "List one page of groups associated with the current channel or its team. The server may require a license or elevated bot permission; a refusal means unavailable, not empty.", {
                    "scope": {"type": "string", "enum": ["channel", "team"]}, "chat_id": _CHAT,
                    "page": {"type": "integer", "minimum": 0},
                    "per_page": {"type": "integer", "minimum": 1, "maximum": 200},
                    "filter_allow_reference": {"type": "boolean"},
                }),
                _card("list_mattermost_bookmarks", "Read channel bookmarks after checking the requester and source-conversation audience.", {"chat_id": _CHAT}),
                _card("create_mattermost_bookmark", "Create a link or file bookmark in a channel. Requires the requester's bookmark permission and approval during this turn.", {"chat_id": _CHAT, **_FIELDS}, ["display_name", "type"]),
                _card("edit_mattermost_bookmark", "Change fields of a channel bookmark. Requires the requester's bookmark permission and approval during this turn.", {"chat_id": _CHAT, "bookmark_id": _ID, **_FIELDS}, ["bookmark_id"]),
                _card("delete_mattermost_bookmark", "Archive a channel bookmark. Requires the requester's bookmark permission and approval during this turn.", {"chat_id": _CHAT, "bookmark_id": _ID}, ["bookmark_id"]),
                ]

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        if tool_name in _BOOKMARK_ACTIONS and isinstance(arguments, Mapping):
            try:
                origin, _, _ = self._turn()
            except _Refusal:
                return None
            channel = str(arguments.get("chat_id") or origin)
            action = tool_name.removesuffix("_mattermost_bookmark").capitalize()
            bookmark = f" {arguments['bookmark_id']}" if arguments.get("bookmark_id") else ""
            return f"{action} bookmark{bookmark} in Mattermost channel {channel}?"
        return await super().widening_question(tool_name, arguments)

    async def _resource_channel(self, chat_id: str | None) -> tuple[str, str, str, Mapping[str, Any]]:
        origin, requester, _ = self._turn()
        if not requester:
            raise _Refusal("requester_required", "a trusted Mattermost requester is required")
        target = str(chat_id or "").strip() or origin
        if target != origin:
            await self._may_read(origin, target, requester)
        record = await self._directory.channel(target)
        if not record or str(record.get("id") or target) != target:
            raise _Refusal("channel_not_found", f"channel {target} is unavailable")
        return origin, requester, target, record

    async def _requester_can_bookmark(self, requester: str, channel: str, record: Mapping[str, Any], action: str) -> None:
        if requester not in await self._directory.members(channel):
            raise _Refusal("requester_not_channel_member", "the requester is not a member of this channel")
        user = await self._client.request("GET", f"/users/{requester}")
        if not isinstance(user, Mapping):
            raise _Refusal("requester_roles_unknown", "the server returned no requester roles")
        if user.get("is_guest") or "system_guest" in str(user.get("roles") or "").split():
            raise _Refusal("requester_guest", "a guest cannot manage channel bookmarks")
        channel_member = await self._client.request("GET", f"/channels/{channel}/members/{requester}")
        if not isinstance(channel_member, Mapping):
            raise _Refusal("requester_roles_unknown", "the server returned no channel roles")
        roles = set(str(user.get("roles") or "").split()) | set(str(channel_member.get("roles") or "").split())
        team = str(record.get("team_id") or "")
        if team:
            member = await self._client.request("GET", f"/teams/{team}/members/{requester}")
            if not isinstance(member, Mapping):
                raise _Refusal("requester_roles_unknown", "the server returned no team roles")
            roles.update(str(member.get("roles") or "").split())
        if not roles:
            raise _Refusal("requester_roles_unknown", "the server returned no requester roles")
        found = await self._client.request("POST", "/roles/names", json=sorted(roles))
        if not isinstance(found, list) or {str(role.get("name")) for role in found if isinstance(role, Mapping)} != roles:
            raise _Refusal("requester_roles_unknown", "the server did not resolve every requester role")
        kind = "public_channel" if record.get("type") == "O" else "private_channel"
        required = f"{action}_bookmark_{kind}"
        allowed = any(required in (role.get("permissions") or ()) or "manage_system" in (role.get("permissions") or ())
                      for role in found if isinstance(role, Mapping))
        if not allowed:
            raise _Refusal("requester_bookmark_permission_required", f"the requester needs {required}")

    @checks_mattermost_ids
    async def list_mattermost_groups(self, scope: str = "channel", chat_id: str | None = None,
                                     page: int = 0, per_page: int = 60,
                                     filter_allow_reference: bool = False) -> str:
        if scope not in {"channel", "team"}:
            return _refused("scope_invalid", "scope must be channel or team")
        if type(page) is not int or page < 0 or type(per_page) is not int or not 1 <= per_page <= 200:
            return _refused("page_invalid", "page must be a nonnegative integer and per_page must be 1 to 200")
        if type(filter_allow_reference) is not bool:
            return _refused("filter_allow_reference_invalid", "filter_allow_reference must be a boolean")
        try:
            _, requester, channel, record = await self._resource_channel(chat_id)
            team = str(record.get("team_id") or "")
            if scope == "team":
                if not team or requester not in await self._directory.in_team(team, [requester]):
                    raise _Refusal("requester_not_team_member", "the requester is not a member of this channel's team")
                path = f"/teams/{team}/groups"
            else:
                path = f"/channels/{channel}/groups"
            found = await self._client.request("GET", path, params={"page": page, "per_page": per_page,
                                                                    "filter_allow_reference": str(filter_allow_reference).lower()})
            if not isinstance(found, list) or any(not isinstance(group, Mapping) for group in found):
                raise _Refusal("bad_response", "the server returned no group page")
            groups = [{key: group[key] for key in ("id", "name", "display_name", "member_count", "allow_reference") if key in group}
                      for group in found]
            return json.dumps({"ok": True, "scope": scope, "chat_id": channel, "team_id": team,
                               "groups": groups, "coverage": {"page": page, "next_page": page + 1 if len(found) == per_page else None,
                                                              "status": "possible_more" if len(found) == per_page else "complete"}})
        except _Refusal as exc:
            return _refused(exc.code, exc.detail)
        except MembersIncomplete as exc:
            return _refused("conversation_members_incomplete", str(exc))
        except MattermostError as exc:
            return _refused("feature_unavailable", "this Mattermost server does not provide group discovery") if exc.status == 501 else _server_refused(exc)

    @checks_mattermost_ids
    async def list_mattermost_bookmarks(self, chat_id: str | None = None) -> str:
        try:
            _, _, channel, _ = await self._resource_channel(chat_id)
            found = await self._client.request("GET", f"/channels/{channel}/bookmarks")
            if not isinstance(found, list):
                raise _Refusal("bad_response", "the server returned no bookmark list")
            return json.dumps({"ok": True, "chat_id": channel, "bookmarks": found, "coverage": {"status": "complete"}})
        except _Refusal as exc:
            return _refused(exc.code, exc.detail)
        except MembersIncomplete as exc:
            return _refused("conversation_members_incomplete", str(exc))
        except MattermostError as exc:
            return _refused("feature_unavailable", "this Mattermost server does not provide channel bookmarks") if exc.status == 501 else _server_refused(exc)

    async def _bookmark_fields(self, origin: str, requester: str, target: str, *,
                               display_name: str | None, type: str | None, link_url: str | None,
                               file_id: str | None, image_url: str | None, emoji: str | None,
                               existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = {"display_name": display_name, "type": type, "link_url": link_url,
                  "file_id": file_id, "image_url": image_url, "emoji": emoji}
        fields = {name: value for name, value in values.items() if value is not None}
        if not fields:
            raise _Refusal("bookmark_fields_required", "provide at least one bookmark field")
        for name in ("display_name", "link_url", "file_id", "image_url", "emoji"):
            if name in fields and (not isinstance(fields[name], str) or not fields[name].strip()):
                raise _Refusal(f"{name}_invalid", f"{name} must be a nonempty string")
        if type is not None and type not in {"link", "file"}:
            raise _Refusal("type_invalid", "type must be link or file")
        current_type = existing.get("type") if existing is not None else None
        effective_type = type or current_type
        changing_type = existing is None or type is not None and type != current_type
        if changing_type and (effective_type == "link" and not link_url or effective_type == "file" and not file_id):
            raise _Refusal("bookmark_source_required", "link bookmarks need link_url; file bookmarks need file_id")
        if effective_type == "link" and file_id or effective_type == "file" and link_url:
            raise _Refusal("bookmark_source_conflict", "link bookmarks use link_url; file bookmarks use file_id")
        if file_id:
            info = await self._client.request("GET", f"/files/{file_id}/info")
            source = str(info.get("channel_id") or "") if isinstance(info, Mapping) else ""
            if not source:
                raise _Refusal("file_channel_unknown", "the file has no known source channel")
            if source != origin:
                await self._may_read(origin, source, requester)
            newcomers, _ = await self._directory.newcomers(source, target)
            if newcomers != ():
                raise _Refusal("file_would_disclose", "the target channel has readers who cannot read the source file")
        return fields

    async def _bookmark_write(self, action: str, chat_id: str | None, bookmark_id: str | None,
                              display_name: str | None = None, type: str | None = None,
                              link_url: str | None = None, file_id: str | None = None,
                              image_url: str | None = None, emoji: str | None = None) -> str:
        try:
            origin, requester, channel, record = await self._resource_channel(chat_id)
            await self._requester_can_bookmark(requester, channel, record, action)
            path = f"/channels/{channel}/bookmarks"
            if action == "add":
                if not display_name or not type:
                    raise _Refusal("bookmark_fields_required", "create requires display_name and type")
                fields = await self._bookmark_fields(origin, requester, channel, display_name=display_name, type=type,
                                                     link_url=link_url, file_id=file_id, image_url=image_url, emoji=emoji)
                found = await self._client.request("POST", path, json=fields)
            else:
                if not bookmark_id:
                    raise _Refusal("bookmark_id_required", "bookmark_id is required")
                current = await self._client.request("GET", path)
                if not isinstance(current, list) or not any(isinstance(item, Mapping) and item.get("id") == bookmark_id for item in current):
                    raise _Refusal("bookmark_not_found", "bookmark_id does not belong to this channel")
                existing = next(item for item in current if isinstance(item, Mapping) and item.get("id") == bookmark_id)
                path += f"/{bookmark_id}"
                if action == "edit":
                    fields = await self._bookmark_fields(origin, requester, channel, display_name=display_name, type=type,
                                                         link_url=link_url, file_id=file_id, image_url=image_url, emoji=emoji,
                                                         existing=existing)
                    found = await self._client.request("PATCH", path, json=fields)
                else:
                    found = await self._client.request("DELETE", path)
            if not isinstance(found, Mapping):
                raise _Refusal("bad_response", "the server returned no bookmark record; the write may have succeeded")
            return json.dumps({"ok": True, "chat_id": channel, "bookmark": found})
        except _Refusal as exc:
            return _refused(exc.code, exc.detail)
        except MembersIncomplete as exc:
            return _refused("conversation_members_incomplete", str(exc))
        except MattermostError as exc:
            return _refused("feature_unavailable", "this Mattermost server does not provide channel bookmarks") if exc.status == 501 else _server_refused(exc)

    @checks_mattermost_ids
    async def create_mattermost_bookmark(self, display_name: str, type: str, chat_id: str | None = None,
                                         link_url: str | None = None, file_id: str | None = None,
                                         image_url: str | None = None, emoji: str | None = None) -> str:
        return await self._bookmark_write("add", chat_id, None, display_name, type, link_url, file_id, image_url, emoji)

    @checks_mattermost_ids
    async def edit_mattermost_bookmark(self, bookmark_id: str, chat_id: str | None = None,
                                       display_name: str | None = None, type: str | None = None,
                                       link_url: str | None = None, file_id: str | None = None,
                                       image_url: str | None = None, emoji: str | None = None) -> str:
        return await self._bookmark_write("edit", chat_id, bookmark_id, display_name, type, link_url, file_id, image_url, emoji)

    @checks_mattermost_ids
    async def delete_mattermost_bookmark(self, bookmark_id: str, chat_id: str | None = None) -> str:
        return await self._bookmark_write("delete", chat_id, bookmark_id)
