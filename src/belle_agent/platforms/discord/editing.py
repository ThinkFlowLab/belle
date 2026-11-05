"""Turn-bound edits and deletion of posts sent by this Discord bot."""

import hashlib
import json
import weakref
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import checks_discord_ids, discord_id_error
from belle_agent.platforms.discord.rich_post import (
    COMPONENTS_V2,
    SUPPRESS_EMBEDS,
    RichPost,
    RichPostError,
    _components,
    _embeds,
    compose_rich_post,
)
from belle_agent.platforms.discord.writing import DiscordWritingToolkit
from belle_agent.tools.host import active_call, refusal

_ABSENT = object()
_ID = {"type": "string", "description": "Discord Snowflake ID."}


def _fingerprint(message: Mapping[str, Any]) -> str:
    fields = {key: message.get(key) for key in (
        "content", "embeds", "components", "attachments", "flags", "edited_timestamp", "poll"
    )}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


class DiscordEditToolkit(DiscordWritingToolkit):
    """Edit or delete only this bot's posts after an active-turn approval."""

    active_turn_only = frozenset({"edit_message", "delete_message"})

    def __init__(self, platform: Any, metadata_provider: Callable[[], Mapping[str, Any]]) -> None:
        super().__init__(platform, metadata_provider)
        self._approvals: dict[int, tuple[Any, tuple[str, str], dict[str, Any], str,
                                         frozenset[str], str]] = {}

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        target = {"chat_id": _ID, "message_id": _ID}
        attachment = {"type": "object", "description": "Existing {id, description?}, or a new inline file"
                      " {filename, content|content_base64, description?, is_spoiler?}.",
                      "properties": {"id": _ID, "filename": {"type": "string"},
                                     "content": {"type": "string"}, "content_base64": {"type": "string"},
                                     "description": {"type": "string", "maxLength": 1024},
                                     "is_spoiler": {"type": "boolean"}}}
        return [
            {"name": "edit_message", "description": "Edit this bot's post after approval. Omitted attachments stay;"
             " an explicit [] removes them. Content edits do not send mentions. Components V2 cannot revert.",
             "input_params": {"type": "object", "required": ["message_id"], "properties": {
                 **target, "content": {"type": ["string", "null"], "maxLength": 2000},
                 "embeds": {"type": ["array", "null"], "maxItems": 10, "items": {"type": "object"}},
                 "components": {"type": ["array", "null"], "items": {"type": "object"}},
                 "attachments": {"type": "array", "items": attachment},
                 "suppress_embeds": {"type": "boolean"},
             }}},
            {"name": "delete_message", "description": "Delete a post sent by this bot after approval."
             " Approval expires when this turn ends.",
             "input_params": {"type": "object", "required": ["message_id"], "properties": target}},
        ]

    def _own(self, message: Mapping[str, Any], target: str, message_id: str) -> None:
        author = message.get("author")
        if (not isinstance(author, Mapping) or str(author.get("id")) != self.platform.bot_user_id
                or message.get("webhook_id")):
            raise DiscordError("not_this_bot_message", "GET", f"/channels/{target}/messages/{message_id}")

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        if tool_name not in self.active_turn_only or not isinstance(arguments, Mapping):
            return None
        if discord_id_error(dict(arguments)):
            return None
        try:
            target = self._target(arguments.get("chat_id"))
            message_id = arguments.get("message_id")
            if not isinstance(message_id, str):
                return None
            message, readers, _ = await self._message_for(target, message_id, write=True)
            self._own(message, target, message_id)
        except DiscordError:
            return None
        context = active_call()
        if context:
            key, call = context
            identity = id(call)
            self._approvals[identity] = (
                weakref.ref(call, lambda _: self._approvals.pop(identity, None)),
                key, dict(arguments), target, readers, _fingerprint(message),
            )
        action = "Edit" if tool_name == "edit_message" else "Delete"
        return f"{action} this bot's message {message_id} in Discord channel {target}?"

    def _approved(self, target: str, readers: frozenset[str], message: Mapping[str, Any]) -> None:
        context = active_call()
        saved = self._approvals.pop(id(context[1]), None) if context else None
        if not saved or not context:
            raise DiscordError("approval_required", "POST", f"/channels/{target}")
        reference, key, arguments, approved_target, approved_readers, fingerprint = saved
        if (reference() is not context[1] or key != context[0] or arguments != dict(context[1].arguments)
                or approved_target != target or approved_readers != readers or fingerprint != _fingerprint(message)):
            raise DiscordError("approval_stale", "POST", f"/channels/{target}")

    def _attachments(self, current: Any, requested: Any) -> RichPost:
        if not isinstance(current, list) or any(not isinstance(item, Mapping) for item in current):
            raise RichPostError("attachments", "an attachment list from Discord")
        known = {str(item.get("id")): item for item in current}
        if requested is _ABSENT:
            requested = [{"id": str(item["id"])} for item in current]
        if not isinstance(requested, list):
            raise RichPostError("attachments", "a list of retained IDs or new inline files")
        retained = []
        new = []
        seen: set[str] = set()
        for index, entry in enumerate(requested):
            field = f"attachments[{index}]"
            if not isinstance(entry, Mapping):
                raise RichPostError(field, "an attachment object")
            if "id" not in entry:
                new.append(dict(entry))
                continue
            ident = str(entry["id"])
            if ident not in known or set(entry) - {"id", "description"} or ident in seen:
                raise RichPostError(field, "one existing attachment ID, at most once")
            seen.add(ident)
            old = known[ident]
            retained.append({"id": ident, "filename": old["filename"],
                             **({"description": entry["description"]} if "description" in entry
                                else {"description": old["description"]} if "description" in old else {})})
        uploaded = compose_rich_post(attachments=new) if new else RichPost({})
        return RichPost({"attachments": [*retained, *uploaded.payload.get("attachments", [])]}, uploaded.files)

    @checks_discord_ids
    async def edit_message(
        self, message_id: str, chat_id: str | None = None, content: Any = _ABSENT,
        embeds: Any = _ABSENT, components: Any = _ABSENT, attachments: Any = _ABSENT,
        suppress_embeds: Any = _ABSENT,
    ) -> str:
        if all(value is _ABSENT for value in (content, embeds, components, attachments, suppress_embeds)):
            return refusal("nothing_to_edit", "set content, embeds, components, attachments, or suppress_embeds")
        try:
            target = self._target(chat_id)
            message, readers, _ = await self._message_for(target, message_id, write=True)
            self._own(message, target, message_id)
            self._approved(target, readers, message)
            if message.get("poll") is not None:
                raise DiscordError("poll_cannot_be_edited", "PATCH", f"/channels/{target}/messages/{message_id}")
            if not self.platform._settings.message_content and message.get("attachments") is None:
                raise DiscordError("attachment_state_unavailable", "GET", f"/channels/{target}/messages/{message_id}")
            upload = self._attachments(message.get("attachments"), attachments)
            payload: dict[str, Any] = {"attachments": upload.payload["attachments"]}
            if content is not _ABSENT:
                if content is not None and (not isinstance(content, str) or len(content) > 2000):
                    raise RichPostError("content", "null or a string of at most 2000 characters")
                payload["content"] = content
            if embeds is not _ABSENT:
                payload["embeds"] = None if embeds is None else _embeds(embeds)
            v2 = False
            if components is not _ABSENT:
                payload["components"], v2 = (None, False) if components is None else (
                    ([], False) if components == [] else _components(components, set())
                )
            flags = message.get("flags", 0)
            if type(flags) is not int:
                raise DiscordError("bad_response", "GET", f"/channels/{target}/messages/{message_id}")
            if suppress_embeds is not _ABSENT:
                if not isinstance(suppress_embeds, bool):
                    raise RichPostError("suppress_embeds", "a boolean")
                flags = flags & ~SUPPRESS_EMBEDS | (SUPPRESS_EMBEDS if suppress_embeds else 0)
            if v2 and not flags & COMPONENTS_V2:
                if payload.get("content") not in (None, "") or payload.get("embeds") not in (None, []):
                    raise RichPostError("components", "Components V2 without content or embeds")
                payload.update(content=None, embeds=[], sticker_ids=[], poll=None)
                flags |= COMPONENTS_V2
            if flags & COMPONENTS_V2 and (payload.get("content") not in (None, "")
                                         or payload.get("embeds") not in (None, [])):
                raise RichPostError("components", "Components V2 without content or embeds")
            if flags != message.get("flags", 0):
                payload["flags"] = flags
            if content is not _ABSENT or components is not _ABSENT:
                payload["allowed_mentions"] = {"parse": [], "replied_user": False}
            path = f"/channels/{target}/messages/{message_id}"
            response = await self.platform._client.mapping("PATCH", path, **RichPost(payload, upload.files).request_kwargs())
            if str(response.get("id")) != message_id or str(response.get("channel_id")) != target:
                raise DiscordError("target_mismatch", "PATCH", path)
            changed = response.get("attachments")
            ids = [str(item.get("id")) for item in changed] if isinstance(changed, list) else []
            return json.dumps({"ok": True, "chat_id": target, "message_id": message_id,
                               "attachment_status": "confirmed" if isinstance(changed, list) else "unconfirmed",
                               "attachment_ids": ids})
        except RichPostError as exc:
            return refusal("edit_invalid", str(exc))
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def delete_message(self, message_id: str, chat_id: str | None = None) -> str:
        try:
            target = self._target(chat_id)
            message, readers, _ = await self._message_for(target, message_id, write=True)
            self._own(message, target, message_id)
            self._approved(target, readers, message)
            await self.platform._client.request("DELETE", f"/channels/{target}/messages/{message_id}")
            return json.dumps({"ok": True, "chat_id": target, "message_id": message_id, "deleted": True})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))
