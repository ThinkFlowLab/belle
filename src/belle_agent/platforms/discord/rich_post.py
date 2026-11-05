"""Validate Discord message content before a post is sent."""

import json
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from belle_agent.attachments import FileSourceRefused, outgoing_file
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE

COMPONENTS_V2 = 1 << 15
SUPPRESS_EMBEDS = 1 << 2
SUPPRESS_NOTIFICATIONS = 1 << 12
MAX_REQUEST_BYTES = 25 * 1024 * 1024
_HEX = re.compile(r"\A[0-9a-fA-F]{6}\Z")
_SENDABLE = ("content", "embeds", "components", "poll", "sticker_ids", "shared_client_theme", "attachments")


class RichPostError(ValueError):
    def __init__(self, field: str, expected: str) -> None:
        self.field = field
        super().__init__(f"{field}: expected {expected}")


def _object(value: Any, field: str, allowed: set[str], required: AbstractSet[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RichPostError(field, "an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown or missing:
        name = min(unknown or missing)
        raise RichPostError(f"{field}.{name}", "a supported field" if unknown else "a value")
    return dict(value)


def _array(value: Any, field: str, maximum: int | None = None, *, minimum: int = 0) -> list[Any]:
    if not isinstance(value, list) or len(value) < minimum or maximum is not None and len(value) > maximum:
        limit = f" with {minimum} to {maximum} items" if maximum else f" with at least {minimum} items"
        raise RichPostError(field, "a list" + limit)
    return value


def _text(value: Any, field: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise RichPostError(field, f"a {'possibly empty ' if empty else ''}string of at most {limit} characters")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RichPostError(field, "a nonempty string")
    return value


def _integer(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RichPostError(field, f"an integer from {low} to {high}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RichPostError(field, "a boolean")
    return value


def _snowflake(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SNOWFLAKE_RE.fullmatch(value):
        raise RichPostError(field, "a Discord ID of 17 to 20 digits")
    return value


def _embeds(value: Any) -> list[dict[str, Any]]:
    result = []
    total = 0
    for index, raw in enumerate(_array(value, "embeds", 10)):
        field = f"embeds[{index}]"
        item = _object(raw, field, {"title", "description", "url", "timestamp", "color", "footer", "image", "thumbnail", "author", "fields"})
        for name, limit in (("title", 256), ("description", 4096)):
            if name in item:
                total += len(_text(item[name], f"{field}.{name}", limit))
        if "color" in item:
            _integer(item["color"], f"{field}.color", 0, 0xFFFFFF)
        for name, keys in (("footer", {"text", "icon_url"}), ("author", {"name", "url", "icon_url"}),
                           ("image", {"url"}), ("thumbnail", {"url"})):
            if name not in item:
                continue
            nested = _object(item[name], f"{field}.{name}", keys)
            title = "text" if name == "footer" else "name" if name == "author" else "url"
            if title not in nested:
                raise RichPostError(f"{field}.{name}.{title}", "a value")
            if name in ("footer", "author"):
                total += len(_text(nested[title], f"{field}.{name}.{title}", 2048 if name == "footer" else 256))
            else:
                _string(nested[title], f"{field}.{name}.{title}")
        if "fields" in item:
            for number, raw_field in enumerate(_array(item["fields"], f"{field}.fields", 25)):
                path = f"{field}.fields[{number}]"
                entry = _object(raw_field, path, {"name", "value", "inline"}, {"name", "value"})
                for name, limit in (("name", 256), ("value", 1024)):
                    total += len(_text(entry[name], f"{path}.{name}", limit))
                if "inline" in entry:
                    _boolean(entry["inline"], f"{path}.inline")
        for name in ("url", "timestamp"):
            if name in item:
                _string(item[name], f"{field}.{name}")
        result.append(item)
    if total > 6000:
        raise RichPostError("embeds", "at most 6000 characters across all embeds")
    return result


def _components(value: Any, handlers: set[str]) -> tuple[list[dict[str, Any]], bool]:
    components = _array(value, "components", 40, minimum=1)
    v2 = False
    count = 0

    def check(raw: Any, field: str) -> dict[str, Any]:
        nonlocal v2, count
        if not isinstance(raw, Mapping) or "type" not in raw or not all(isinstance(key, str) for key in raw):
            raise RichPostError(field, "a Discord component object with a type")
        item = dict(raw)
        kind = _integer(item["type"], f"{field}.type", 1, 23)
        if kind not in {*range(1, 15), 17} or kind == 4:
            raise RichPostError(f"{field}.type", "a Discord message component type")
        count += 1
        if kind not in {1, 2, 3, 5, 6, 7, 8}:
            v2 = True
        if kind in {2, 3, 5, 6, 7, 8}:
            custom = item.get("custom_id")
            if kind == 2 and item.get("style") in {5, 6}:
                if custom is not None:
                    raise RichPostError(f"{field}.custom_id", "absent on a link or premium button")
            elif not isinstance(custom, str) or custom not in handlers:
                raise RichPostError(f"{field}.custom_id", "an ID with a registered interaction handler")
        if kind == 12:
            for index, raw_media in enumerate(_array(item.get("items"), f"{field}.items", 10, minimum=1)):
                path = f"{field}.items[{index}]"
                media_item = _object(raw_media, path, {"media", "description", "spoiler"}, {"media"})
                media = _object(media_item["media"], f"{path}.media", {"url"}, {"url"})
                _string(media["url"], f"{path}.media.url")
                if "description" in media_item and media_item["description"] is not None:
                    _text(media_item["description"], f"{path}.description", 1024, empty=True)
                if "spoiler" in media_item:
                    _boolean(media_item["spoiler"], f"{path}.spoiler")
        if "components" in item:
            for index, child in enumerate(_array(item["components"], f"{field}.components")):
                check(child, f"{field}.components[{index}]")
        if "accessory" in item:
            check(item["accessory"], f"{field}.accessory")
        return item

    result = [check(item, f"components[{index}]") for index, item in enumerate(components)]
    if count > 40:
        raise RichPostError("components", "at most 40 total components")
    return result, v2


def _poll(value: Any) -> dict[str, Any]:
    poll = _object(value, "poll", {"question", "answers", "duration", "allow_multiselect", "layout_type"}, {"question", "answers"})
    question = _object(poll["question"], "poll.question", {"text"}, {"text"})
    _text(question["text"], "poll.question.text", 300)
    for index, raw in enumerate(_array(poll["answers"], "poll.answers", 10, minimum=1)):
        path = f"poll.answers[{index}]"
        answer = _object(raw, path, {"poll_media"}, {"poll_media"})
        media = _object(answer["poll_media"], f"{path}.poll_media", {"text", "emoji"})
        if "text" in media:
            _text(media["text"], f"{path}.poll_media.text", 55)
        if "emoji" in media:
            emoji = _object(media["emoji"], f"{path}.poll_media.emoji", {"id", "name"})
            if len(emoji) != 1:
                raise RichPostError(f"{path}.poll_media.emoji", "exactly one of id or name")
            if "id" in emoji:
                _snowflake(emoji["id"], f"{path}.poll_media.emoji.id")
            else:
                _text(emoji["name"], f"{path}.poll_media.emoji.name", 32)
        if not media:
            raise RichPostError(f"{path}.poll_media", "text or emoji")
    if "duration" in poll:
        _integer(poll["duration"], "poll.duration", 1, 32 * 24)
    if "allow_multiselect" in poll:
        _boolean(poll["allow_multiselect"], "poll.allow_multiselect")
    if "layout_type" in poll:
        _integer(poll["layout_type"], "poll.layout_type", 1, 1)
    return poll


def _mentions(value: Any, *, allow_role_mentions: bool, allow_everyone_mentions: bool) -> dict[str, Any]:
    item = _object(value, "mentions", {"users", "roles", "everyone", "replied_user"})
    result: dict[str, Any] = {"parse": []}
    for name in ("users", "roles"):
        if name in item:
            ids = item[name]
            if not isinstance(ids, list) or len(ids) > 100:
                raise RichPostError(f"mentions.{name}", "a list of at most 100 Discord IDs")
            result[name] = [_snowflake(id_, f"mentions.{name}[{index}]") for index, id_ in enumerate(ids)]
    if item.get("roles") and not allow_role_mentions:
        raise RichPostError("mentions.roles", "role-mention permission and Belle policy approval")
    if "everyone" in item and _boolean(item["everyone"], "mentions.everyone"):
        if not allow_everyone_mentions:
            raise RichPostError("mentions.everyone", "everyone-mention permission and Belle policy approval")
        result["parse"].append("everyone")
    if "replied_user" in item:
        result["replied_user"] = _boolean(item["replied_user"], "mentions.replied_user")
    return result


def _theme(value: Any) -> dict[str, Any]:
    item = _object(value, "shared_client_theme", {"colors", "gradient_angle", "base_mix", "base_theme"},
                   {"colors", "gradient_angle", "base_mix"})
    for index, color in enumerate(_array(item["colors"], "shared_client_theme.colors", 5, minimum=1)):
        if not isinstance(color, str) or not _HEX.fullmatch(color):
            raise RichPostError(f"shared_client_theme.colors[{index}]", "six hexadecimal digits")
    _integer(item["gradient_angle"], "shared_client_theme.gradient_angle", 0, 360)
    _integer(item["base_mix"], "shared_client_theme.base_mix", 0, 100)
    if "base_theme" in item and item["base_theme"] is not None:
        _integer(item["base_theme"], "shared_client_theme.base_theme", 0, 4)
    return item


@dataclass(frozen=True)
class RichPost:
    payload: dict[str, Any]
    files: tuple[tuple[str, tuple[str, bytes]], ...] = ()

    def request_kwargs(self) -> dict[str, Any]:
        if self.files:
            return {"data": {"payload_json": json.dumps(self.payload)}, "files": list(self.files)}
        return {"json": self.payload}


def compose_rich_post(
    *, text: str | None = None, embeds: Any = None, components: Any = None, poll: Any = None,
    sticker_ids: Any = None, attachments: Any = None, mentions: Any = None,
    suppress_embeds: bool = False, suppress_notifications: bool = False, tts: bool = False,
    shared_client_theme: Any = None, handlers: set[str] | None = None,
    allow_role_mentions: bool = False, allow_everyone_mentions: bool = False, allow_tts: bool = False,
) -> RichPost:
    payload: dict[str, Any] = {}
    files: list[tuple[str, tuple[str, bytes]]] = []
    if text is not None:
        payload["content"] = _text(text, "text", 2000)
    if embeds is not None:
        payload["embeds"] = _embeds(embeds)
    if components is not None:
        payload["components"], v2 = _components(components, handlers or set())
        if v2:
            payload["flags"] = COMPONENTS_V2
    if poll is not None:
        payload["poll"] = _poll(poll)
    if sticker_ids is not None:
        payload["sticker_ids"] = [_snowflake(value, f"sticker_ids[{index}]")
                                  for index, value in enumerate(_array(sticker_ids, "sticker_ids", 3))]
    if shared_client_theme is not None:
        payload["shared_client_theme"] = _theme(shared_client_theme)
    if attachments is not None:
        uploaded = []
        for index, raw in enumerate(_array(attachments, "attachments")):
            field = f"attachments[{index}]"
            entry = _object(raw, field, {"filename", "content", "content_base64", "file_id", "title", "description", "is_spoiler"})
            name = entry.get("filename")
            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                raise RichPostError(f"{field}.filename", "a filename without directory separators")
            try:
                data = outgoing_file(entry.get("content"), entry.get("content_base64"), entry.get("file_id"), name)
            except FileSourceRefused as exc:
                raise RichPostError(field, exc.detail) from None
            if data is None:
                raise RichPostError(f"{field}.file_id", "an inline content or content_base64 source until Discord file reads are available")
            metadata: dict[str, Any] = {"id": index, "filename": name}
            if "title" in entry:
                metadata["title"] = _string(entry["title"], f"{field}.title")
            if "description" in entry:
                metadata["description"] = _text(entry["description"], f"{field}.description", 1024)
            if "is_spoiler" in entry:
                metadata["is_spoiler"] = _boolean(entry["is_spoiler"], f"{field}.is_spoiler")
            uploaded.append(metadata)
            files.append((f"files[{index}]", (name, data)))
        payload["attachments"] = uploaded
    if mentions is not None:
        payload["allowed_mentions"] = _mentions(mentions, allow_role_mentions=allow_role_mentions,
                                                 allow_everyone_mentions=allow_everyone_mentions)
    else:
        payload["allowed_mentions"] = {"parse": ["users"], "replied_user": False}
    flags = payload.get("flags", 0)
    if _boolean(suppress_embeds, "suppress_embeds"):
        flags |= SUPPRESS_EMBEDS
    if _boolean(suppress_notifications, "suppress_notifications"):
        flags |= SUPPRESS_NOTIFICATIONS
    if flags:
        payload["flags"] = flags
    if _boolean(tts, "tts"):
        if not allow_tts:
            raise RichPostError("tts", "SEND_TTS_MESSAGES permission and Belle policy approval")
        payload["tts"] = True
    if not any(payload.get(name) for name in _SENDABLE):
        raise RichPostError("text", "text or another sendable field")
    if flags & COMPONENTS_V2 and any(payload.get(name) for name in ("content", "embeds", "poll", "sticker_ids", "shared_client_theme")):
        raise RichPostError("components", "Components V2 without text, embeds, poll, stickers or shared_client_theme")
    if components is not None and not flags & COMPONENTS_V2 and len(payload["components"]) > 5:
        raise RichPostError("components", "at most five legacy action rows")
    if sum(len(data) for _, (_, data) in files) > MAX_REQUEST_BYTES:
        raise RichPostError("attachments", "a request of at most 25 MiB")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise RichPostError("components", "JSON-compatible Discord component fields") from None
    return RichPost(payload, tuple(files))


def rich_tool_properties() -> dict[str, Any]:
    """Model-facing arguments for Discord's supported Create Message fields."""
    embed = {"type": "object", "additionalProperties": False, "properties": {
        "title": {"type": "string", "maxLength": 256},
        "description": {"type": "string", "maxLength": 4096},
        "url": {"type": "string"}, "timestamp": {"type": "string"},
        "color": {"type": "integer", "minimum": 0, "maximum": 16777215},
        "footer": {"type": "object", "properties": {"text": {"type": "string", "maxLength": 2048}, "icon_url": {"type": "string"}}},
        "image": {"type": "object", "properties": {"url": {"type": "string"}}},
        "thumbnail": {"type": "object", "properties": {"url": {"type": "string"}}},
        "author": {"type": "object", "properties": {"name": {"type": "string", "maxLength": 256}, "url": {"type": "string"}, "icon_url": {"type": "string"}}},
        "fields": {"type": "array", "maxItems": 25, "items": {"type": "object", "required": ["name", "value"], "properties": {
            "name": {"type": "string", "maxLength": 256}, "value": {"type": "string", "maxLength": 1024}, "inline": {"type": "boolean"},
        }}},
    }}
    return {
        "embeds": {"type": "array", "maxItems": 10, "items": embed, "description": "Rich embeds; all embeds together may contain at most 6000 characters."},
        "components": {"type": "array", "items": {"type": "object"}, "description": "Discord message component objects. Interactive custom_id controls require a registered handler; otherwise only display components and Discord-managed link or premium buttons are available. Components V2 cannot accompany text, embeds, polls, stickers or a shared theme."},
        "poll": {"type": "object", "required": ["question", "answers"], "properties": {
            "question": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string", "maxLength": 300}}},
            "answers": {"type": "array", "maxItems": 10, "items": {
                "type": "object", "required": ["poll_media"], "properties": {
                    "poll_media": {"type": "object", "properties": {
                        "text": {"type": "string", "maxLength": 55}, "emoji": {"type": "object"},
                    }},
                },
            }},
            "duration": {"type": "integer", "minimum": 1, "maximum": 768, "description": "Hours; defaults to 24."},
            "allow_multiselect": {"type": "boolean"}, "layout_type": {"type": "integer", "enum": [1]},
        }},
        "sticker_ids": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "attachments": {"type": "array", "items": {"type": "object", "required": ["filename"], "properties": {
            "filename": {"type": "string"}, "content": {"type": "string"}, "content_base64": {"type": "string"},
            "title": {"type": "string"}, "description": {"type": "string", "maxLength": 1024},
            "is_spoiler": {"type": "boolean"},
        }}, "description": "Inline file data, as content or content_base64, and a filename. Never a local path. A caption may be sent in text."},
        "mentions": {"type": "object", "properties": {
            "users": {"type": "array", "maxItems": 100, "items": {"type": "string"}},
            "roles": {"type": "array", "maxItems": 100, "items": {"type": "string"}},
            "everyone": {"type": "boolean"}, "replied_user": {"type": "boolean"},
        }, "description": "Explicit mention recipients. Role and everyone pings require operator permission and Discord permissions."},
        "suppress_embeds": {"type": "boolean"}, "suppress_notifications": {"type": "boolean"},
        "tts": {"type": "boolean", "description": "Text-to-speech; requires operator permission and Discord SEND_TTS_MESSAGES."},
        "shared_client_theme": {"type": "object", "required": ["colors", "gradient_angle", "base_mix"], "properties": {
            "colors": {"type": "array", "maxItems": 5, "items": {"type": "string", "pattern": "^[0-9a-fA-F]{6}$"}},
            "gradient_angle": {"type": "integer", "minimum": 0, "maximum": 360},
            "base_mix": {"type": "integer", "minimum": 0, "maximum": 100},
            "base_theme": {"type": "integer", "minimum": 0, "maximum": 4},
        }},
    }
