"""Policy and bounded context buffer for non-message Slack events."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from belle_agent.admission import policy as admission_policy
from belle_agent.admission.policy import (
    EVENT_CONTEXT,
    EVENT_DISPOSITIONS,
    EVENT_OFF,
    EVENT_TURN,
    KEY_EVENTS,
    disposition_word,
)

from .tool_common import _iso_utc as iso_utc
from .tool_common import _timestamp as _float_or_none

EVENT_FAMILY_REACTION = "reaction"
EVENT_FAMILY_PIN = "pin"
EVENT_FAMILY_MEMBER = "member"
EVENT_FAMILY_APP_HOME = "app_home"

EVENT_FAMILIES: tuple[str, ...] = (
    EVENT_FAMILY_REACTION,
    EVENT_FAMILY_PIN,
    EVENT_FAMILY_MEMBER,
    EVENT_FAMILY_APP_HOME,
)

EVENT_TURN_FAMILIES: frozenset[str] = frozenset({EVENT_FAMILY_REACTION, EVENT_FAMILY_PIN, EVENT_FAMILY_MEMBER})

EVENT_TURN_WITHHELD: Mapping[str, str] = MappingProxyType(
    {
        EVENT_FAMILY_APP_HOME: (
            "Opening App Home has no message to answer. No family splits on a"
            " payload field. The tab can be Messages or Home, but neither"
            " defines a reply destination."
        )
    }
)

TURN_WITHHELD_UNDECIDED = (
    "a turn woken by an event has to answer somewhere, and nothing says where a turn woken by one of these would post"
)


def turn_withheld_reason(family: str) -> str:
    """Return the reason this family cannot start a turn."""
    return EVENT_TURN_WITHHELD.get(family) or TURN_WITHHELD_UNDECIDED


EVENT_ANCHORED_FAMILIES: frozenset[str] = frozenset({EVENT_FAMILY_REACTION, EVENT_FAMILY_PIN})


def family_anchors_on_a_message(family: str) -> bool:
    """Return whether this family answers in a message's thread."""
    return family in EVENT_ANCHORED_FAMILIES


EVENT_HELD_FAMILIES = frozenset({EVENT_FAMILY_MEMBER, EVENT_FAMILY_PIN})


def family_waits_its_turn(family: str) -> bool:
    return family in EVENT_HELD_FAMILIES


EVENT_DISPOSITION_DEFAULT = EVENT_OFF

EVENT_TYPE_FAMILIES: Mapping[str, str] = MappingProxyType(
    {
        "reaction_added": EVENT_FAMILY_REACTION,
        "reaction_removed": EVENT_FAMILY_REACTION,
        "pin_added": EVENT_FAMILY_PIN,
        "pin_removed": EVENT_FAMILY_PIN,
        "member_joined_channel": EVENT_FAMILY_MEMBER,
        "member_left_channel": EVENT_FAMILY_MEMBER,
        "app_home_opened": EVENT_FAMILY_APP_HOME,
    }
)

# Per-family caps prevent reactions from evicting membership events.
EVENT_BUFFER_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        EVENT_FAMILY_REACTION: 30,
        EVENT_FAMILY_PIN: 10,
        EVENT_FAMILY_MEMBER: 20,
        EVENT_FAMILY_APP_HOME: 5,
    }
)

EVENT_BUFFER_TTL_SECONDS = 3600.0


def normalize_event_policy(raw: Any) -> dict[str, str] | None:
    """Normalize known family dispositions; return ``None`` for non-mappings."""
    return admission_policy.normalize_event_policy(raw, kinds=EVENT_FAMILIES, turn_kinds=EVENT_TURN_FAMILIES)


def disposition_holds(family: str, word: str) -> bool:
    """Return whether this family accepts the disposition."""
    return admission_policy.disposition_holds(word, may_turn=family in EVENT_TURN_FAMILIES)


def event_disposition(policy: Mapping[str, str] | None, family: str) -> str:
    """Return the family's disposition, defaulting to ``off`` if invalid."""
    if not isinstance(policy, Mapping):
        return EVENT_DISPOSITION_DEFAULT
    word = disposition_word(policy.get(family))
    return word if disposition_holds(family, word) else EVENT_DISPOSITION_DEFAULT


def event_family(event: Mapping[str, Any] | None) -> str | None:
    """Return the event family, or ``None`` for an unknown event type."""
    if not isinstance(event, Mapping):
        return None
    return EVENT_TYPE_FAMILIES.get(str(event.get("type") or "").strip())


def event_actor_id(event: Mapping[str, Any] | None) -> str:
    """Return the actor's ``user`` ID, never the affected item's ``item_user``."""
    if not isinstance(event, Mapping):
        return ""
    return str(event.get("user") or "").strip()


def event_chat_id(event: Mapping[str, Any] | None) -> str:
    """Return the event's conversation ID from Slack's three field shapes."""
    if not isinstance(event, Mapping):
        return ""
    item = event.get("item")
    if isinstance(item, Mapping):
        from_item = str(item.get("channel") or "").strip()
        if from_item:
            return from_item
    return str(event.get("channel_id") or "").strip() or str(event.get("channel") or "").strip()


def render_event(event: Mapping[str, Any], *, family: str) -> dict[str, Any]:
    """Render event fields without API calls or App Home view content."""
    record: dict[str, Any] = {
        "family": family,
        "event_type": str(event.get("type") or "").strip(),
        "event_ts_iso_utc": iso_utc(_float_or_none(event.get("event_ts"))),
    }
    actor = event_actor_id(event)
    if actor:
        record["author_user_id"] = actor
        record["author_name"] = actor
    if event.get("bot_id") or event.get("bot_profile"):
        record["is_author_bot"] = True

    if family == EVENT_FAMILY_APP_HOME:
        tab = str(event.get("tab") or "").strip()
        if tab:
            record["tab"] = tab

    if family == EVENT_FAMILY_REACTION:
        emoji = str(event.get("reaction") or "").strip()
        if emoji:
            record["emoji_name"] = emoji

    item_ts = _item_ts(event, family=family)
    if item_ts:
        record["ts"] = item_ts
        record["ts_iso_utc"] = iso_utc(_float_or_none(item_ts))
    return record


def event_anchor(event: Mapping[str, Any] | None, *, family: str) -> tuple[str, str]:
    """Return the affected message's ``(ts, thread_ts)``."""
    if not isinstance(event, Mapping):
        return "", ""
    ts = _item_ts(event, family=family)
    if not ts:
        return "", ""
    return ts, _item_thread_ts(event) or ts


def _item_thread_ts(event: Mapping[str, Any]) -> str:
    """Return the affected message's thread if Slack supplies one."""
    item = event.get("item")
    if not isinstance(item, Mapping):
        return ""
    message = item.get("message")
    if isinstance(message, Mapping):
        return str(message.get("thread_ts") or "").strip()
    return str(item.get("thread_ts") or "").strip()


def _item_ts(event: Mapping[str, Any], *, family: str) -> str:
    """Return the affected message's ts, if this family has a message anchor."""
    if family not in EVENT_ANCHORED_FAMILIES:
        return ""
    item = event.get("item")
    if not isinstance(item, Mapping):
        return ""
    direct = str(item.get("ts") or "").strip()
    if direct:
        return direct
    message = item.get("message")
    if isinstance(message, Mapping):
        return str(message.get("ts") or "").strip()
    return ""




__all__ = [
    "EVENT_ANCHORED_FAMILIES",
    "EVENT_BUFFER_LIMITS",
    "EVENT_BUFFER_TTL_SECONDS",
    "EVENT_CONTEXT",
    "EVENT_DISPOSITIONS",
    "EVENT_DISPOSITION_DEFAULT",
    "EVENT_FAMILIES",
    "EVENT_FAMILY_APP_HOME",
    "EVENT_FAMILY_MEMBER",
    "EVENT_FAMILY_PIN",
    "EVENT_FAMILY_REACTION",
    "EVENT_HELD_FAMILIES",
    "EVENT_OFF",
    "EVENT_TURN",
    "EVENT_TURN_FAMILIES",
    "EVENT_TURN_WITHHELD",
    "EVENT_TYPE_FAMILIES",
    "KEY_EVENTS",
    "TURN_WITHHELD_UNDECIDED",
    "disposition_holds",
    "disposition_word",
    "event_actor_id",
    "event_anchor",
    "event_chat_id",
    "event_disposition",
    "event_family",
    "family_anchors_on_a_message",
    "family_waits_its_turn",
    "iso_utc",
    "normalize_event_policy",
    "render_event",
    "turn_withheld_reason",
]
