"""Build a forum or media post from a validated first message."""

import json
from collections.abc import Mapping
from typing import Any, NoReturn

from belle_agent.platforms.discord.client import DiscordClient, DiscordError
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE

FORUM_TYPES = frozenset({15, 16})
REQUIRE_TAG = 1 << 4
ARCHIVE_MINUTES = frozenset({60, 1440, 4320, 10080})
MESSAGE_FIELDS = frozenset({
    "content", "embeds", "allowed_mentions", "components", "sticker_ids", "attachments", "flags",
})
SENDABLE_FIELDS = frozenset({"content", "embeds", "components", "sticker_ids"})
MESSAGE_FLAGS = (1 << 2) | (1 << 12)


def _invalid(field: str, expected: str) -> NoReturn:
    raise DiscordError(f"{field}_expected_{expected}", "POST", "/channels/threads")


def forum_thread_payload(
    channel: Mapping[str, Any], options: Mapping[str, Any] | None, message: Mapping[str, Any], *, has_files: bool = False,
) -> dict[str, Any]:
    """Validate the forum/media-specific shape before creating its thread."""
    if channel.get("type") not in FORUM_TYPES:
        _invalid("chat_id", "forum_or_media_channel")
    if not isinstance(options, Mapping):
        _invalid("thread", "object")
    unknown = set(options) - {"name", "auto_archive_minutes", "rate_limit_per_user", "applied_tag_ids"}
    if unknown:
        _invalid(f"thread.{min(unknown)}", "supported_field")
    name = options.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        _invalid("thread.name", "1_to_100_character_name")
    payload: dict[str, Any] = {"name": name}
    if "auto_archive_minutes" in options:
        minutes = options["auto_archive_minutes"]
        if type(minutes) is not int or minutes not in ARCHIVE_MINUTES:
            _invalid("thread.auto_archive_minutes", "60_1440_4320_or_10080")
        payload["auto_archive_duration"] = minutes
    if "rate_limit_per_user" in options:
        rate = options["rate_limit_per_user"]
        if rate is not None and (type(rate) is not int or not 0 <= rate <= 21600):
            _invalid("thread.rate_limit_per_user", "null_or_integer_0_to_21600")
        payload["rate_limit_per_user"] = rate
    tags = options.get("applied_tag_ids", [])
    if not isinstance(tags, list) or len(tags) > 5:
        _invalid("thread.applied_tag_ids", "list_of_at_most_5_Discord_IDs")
    available = channel.get("available_tags")
    available_ids = {str(tag.get("id")) for tag in available if isinstance(tag, Mapping)} if isinstance(available, list) else set()
    for index, tag in enumerate(tags):
        if not isinstance(tag, str) or not SNOWFLAKE_RE.fullmatch(tag):
            _invalid(f"thread.applied_tag_ids[{index}]", "Discord_ID_of_17_to_20_digits")
        if tag not in available_ids:
            _invalid(f"thread.applied_tag_ids[{index}]", "tag_available_in_target_channel")
    if len(set(tags)) != len(tags):
        _invalid("thread.applied_tag_ids", "distinct_tags")
    if not tags and isinstance(channel.get("flags"), int) and channel["flags"] & REQUIRE_TAG:
        _invalid("thread.applied_tag_ids", "at_least_one_tag_required_by_channel")
    if tags:
        payload["applied_tags"] = tags

    if not isinstance(message, Mapping):
        _invalid("message", "object")
    unknown_message = set(message) - MESSAGE_FIELDS
    if unknown_message:
        _invalid(f"message.{min(unknown_message)}", "supported_forum_message_field")
    if not any(message.get(key) for key in SENDABLE_FIELDS) and not has_files:
        _invalid("message", "content_embeds_components_stickers_or_files")
    if "flags" in message and (type(message["flags"]) is not int or message["flags"] & ~MESSAGE_FLAGS):
        _invalid("message.flags", "suppress_embeds_or_suppress_notifications")
    payload["message"] = dict(message)
    return payload


def forum_request_kwargs(
    channel: Mapping[str, Any], options: Mapping[str, Any] | None, message: Mapping[str, Any],
    files: tuple[tuple[str, tuple[str, bytes]], ...] = (),
) -> dict[str, Any]:
    """Nest the initial message and keep its uploads in Discord's multipart form."""
    payload = forum_thread_payload(channel, options, message, has_files=bool(files))
    if files:
        return {"data": {"payload_json": json.dumps(payload)}, "files": list(files)}
    return {"json": payload}


async def create_forum_thread(
    client: DiscordClient, channel: Mapping[str, Any], options: Mapping[str, Any] | None,
    message: Mapping[str, Any], files: tuple[tuple[str, tuple[str, bytes]], ...] = (),
) -> tuple[str, str, Mapping[str, Any]]:
    """Create a post and return its thread ID and confirmed first message."""
    channel_id = str(channel.get("id") or "")
    if not SNOWFLAKE_RE.fullmatch(channel_id):
        _invalid("chat_id", "Discord_ID_of_17_to_20_digits")
    path = f"/channels/{channel_id}/threads"
    request = forum_request_kwargs(channel, options, message, files)
    try:
        created = await client.mapping("POST", path, **request)
    except DiscordError as exc:
        if exc.code in {"unreachable", "bad_response"}:
            raise DiscordError("delivery_uncertain_do_not_retry", "POST", path) from None
        raise
    thread_id = str(created.get("id") or "")
    first = created.get("message")
    message_id = str(first.get("id") or "") if isinstance(first, Mapping) else ""
    if (not SNOWFLAKE_RE.fullmatch(thread_id) or created.get("type") != 11
            or str(created.get("parent_id") or "") != channel_id
            or not isinstance(first, Mapping)
            or not SNOWFLAKE_RE.fullmatch(message_id)
            or str(first.get("channel_id") or "") != thread_id):
        raise DiscordError("delivery_uncertain_do_not_retry", "POST", path)
    return thread_id, message_id, first
