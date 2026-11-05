"""Resolve Matrix aliases and read visible space and room state."""

import json
import re
from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.matrix.finding import MatrixFindingToolkit
from belle_agent.platforms.matrix.helper import HelperError
from belle_agent.platforms.matrix.ids import ROOM_RE, checks_matrix_ids
from belle_agent.platforms.matrix.toolkit import _helper_refused, _Refusal, _refused

_ALIAS = re.compile(r"\A#[^\s:]+:\S+\Z")


def _card(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": description,
            "input_params": {"type": "object", "properties": properties, "required": required or []}}


class MatrixResourcesToolkit(MatrixFindingToolkit):
    """Matrix resource reads with the same source audience check as conversation reads."""

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [*super().get_tool_definitions(),
                _card("resolve_matrix_alias", "Resolve a public Matrix room alias. The result reports whether Belle joined the room; resolution does not join it or prove the requester can read it.", {
                    "alias": {"type": "string", "description": "Full room alias, such as #team:example.org."},
                }, ["alias"]),
                _card("list_matrix_space", "Read one page of the visible hierarchy of a joined Matrix space. Private children are shown only when the requester and source conversation may read them. Continue with next_token.", {
                    "space_id": {"type": "string", "description": "Space room ID; omit for this room."},
                    "next_token": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "max_depth": {"type": "integer", "minimum": 0},
                    "suggested_only": {"type": "boolean"},
                }),
                _card("read_matrix_state", "Read one current state event of a joined Matrix room. Another room must pass the source-conversation audience check. This tool does not change state.", {
                    "event_type": {"type": "string", "description": "Matrix state event type, such as m.room.topic."},
                    "state_key": {"type": "string", "description": "State key; defaults to the empty key."},
                    "chat_id": {"type": "string", "description": "Room ID; omit for this room."},
                }, ["event_type"]),
                ]

    async def resolve_matrix_alias(self, alias: str) -> str:
        try:
            self._turn()
        except _Refusal as exc:
            return _refused(exc.code, exc.detail)
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            return _refused("alias_invalid", "alias must be a full Matrix room alias such as #team:example.org")
        try:
            found = await self._account.call("alias_resolve", alias=alias)
            return json.dumps({"ok": True, "alias": alias, "room_id": found["room"],
                               "servers": found["servers"], "joined": found["joined"],
                               "read_access_checked": False})
        except HelperError as exc:
            return _helper_refused(exc, alias)

    @checks_matrix_ids
    async def list_matrix_space(self, space_id: str | None = None, next_token: str | None = None,
                                limit: int = 50, max_depth: int | None = None,
                                suggested_only: bool = False) -> str:
        if type(limit) is not int or not 1 <= limit <= 2**32 - 1:
            return _refused("limit_invalid", "limit must be an integer from 1 to the helper's 32-bit limit")
        if max_depth is not None and (type(max_depth) is not int or not 0 <= max_depth <= 2**32 - 1):
            return _refused("max_depth_invalid", "max_depth must be a nonnegative integer within the helper's 32-bit limit")
        if type(suggested_only) is not bool:
            return _refused("suggested_only_invalid", "suggested_only must be a boolean")
        if next_token is not None and (not isinstance(next_token, str) or not next_token):
            return _refused("next_token_invalid", "next_token must be a nonempty server token")
        try:
            origin, requester, _ = self._turn()
            target = space_id or origin
            if not ROOM_RE.fullmatch(target):
                raise _Refusal("space_id_invalid", "space_id must be a Matrix room ID beginning with !")
            if target != origin:
                await self._may_read(origin, target, requester)
            fields: dict[str, Any] = {"room": target, "limit": limit, "suggested_only": suggested_only}
            if next_token:
                fields["from"] = next_token
            if max_depth is not None:
                fields["max_depth"] = max_depth
            found = await self._account.call("hierarchy", **fields)
            visible: list[Mapping[str, Any]] = []
            hidden = 0
            for room in found["rooms"]:
                room_id = str(room.get("room_id") or "")
                if not ROOM_RE.fullmatch(room_id):
                    raise _Refusal("hierarchy_bad_response", "the server returned a hierarchy entry without a room ID")
                public = room.get("world_readable") is True or room.get("join_rule") == "public"
                if public or await self._readable(origin, room_id, requester):
                    visible.append(room)
                else:
                    hidden += 1
            return json.dumps({"ok": True, "space_id": target, "rooms": visible,
                               "coverage": {"status": "partial" if hidden or found.get("next") else "complete",
                                            "hidden_for_audience": hidden, "next_token": found.get("next")}})
        except _Refusal as exc:
            return _refused(exc.code, exc.detail)
        except HelperError as exc:
            return _helper_refused(exc, space_id or "")

    @checks_matrix_ids
    async def read_matrix_state(self, event_type: str, state_key: str = "", chat_id: str | None = None) -> str:
        if not isinstance(event_type, str) or not event_type or any(char.isspace() for char in event_type):
            return _refused("event_type_invalid", "event_type must be a nonempty Matrix event type without whitespace")
        if not isinstance(state_key, str):
            return _refused("state_key_invalid", "state_key must be a string")
        try:
            origin, requester, _ = self._turn()
            target = chat_id or origin
            if not ROOM_RE.fullmatch(target):
                raise _Refusal("chat_id_invalid", "chat_id must be a Matrix room ID beginning with !")
            if target != origin:
                await self._may_read(origin, target, requester)
            found = await self._account.call("state_get", room=target, type=event_type, state_key=state_key)
            return json.dumps({"ok": True, "chat_id": target, "event_type": event_type,
                               "state_key": state_key, "event": found.get("event")})
        except _Refusal as exc:
            return _refused(exc.code, exc.detail)
        except HelperError as exc:
            return _helper_refused(exc, chat_id or "")
