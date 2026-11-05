"""Translate Matrix reactions, membership changes, and pins into host events.

Redacting an ``m.reaction`` removes it. An ``m.room.member`` state key names
the affected user. Changes to the ``m.room.pinned_events`` list produce one
event per added or removed pin. Belle handles its stop reaction separately.
"""

import json
from collections.abc import Mapping
from typing import Any

from belle_agent.host.events import EVENT_TURN_DESTINATION_ROOT, EVENT_TURN_RANKING
from belle_agent.platforms.base import EventKind, InboundEvent

KIND_REACTION = "reaction"
KIND_MEMBER = "member"
KIND_PIN = "pin"
REACTION = "m.reaction"
REDACTION = "m.room.redaction"
MEMBER = "m.room.member"
PINNED = "m.room.pinned_events"
#: Event kinds and retained context limits.
EVENT_KINDS: Mapping[str, EventKind] = {
    KIND_REACTION: EventKind(held=30, turns=True, anchored=True),
    KIND_MEMBER: EventKind(held=20, turns=True, waits=True),
    KIND_PIN: EventKind(held=10, turns=True, anchored=True, waits=True),
}

REPLY_BY_TOOL = (
    " If you do answer, pass the message_id named above as reply_to_message_id to post_message. A post that"
    " names no message lands at the top level of this room, where everyone in it sees it."
)


def previous(room_event: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a state event's previous content, if available."""
    unsigned = room_event.get("unsigned")
    found = unsigned.get("prev_content") if isinstance(unsigned, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def pin_changes(room_event: Mapping[str, Any]) -> list[tuple[str, bool]]:
    """Each message a pin event added or removed, with whether it was added, in the order of the lists."""

    def pinned(content: Any) -> list[str]:
        found = content.get("pinned") if isinstance(content, Mapping) else None
        return [str(item) for item in found if isinstance(item, str)] if isinstance(found, list) else []

    now, before = pinned(room_event.get("content")), pinned(previous(room_event))
    return [(item, True) for item in now if item not in before] + [(item, False) for item in before if item not in now]


def render_events(held: Mapping[str, Any] | None, started_by: InboundEvent | None) -> str:
    """What a turn is told of events: the one that started it, with where it happened, and those its conversation kept."""
    parts = []
    if started_by is not None:
        parts.append(f"[matrix_event]\n{json.dumps(dict(started_by.record), ensure_ascii=False)}")
        if started_by.anchor:
            root = f", root_message_id: {started_by.anchor_thread}" if started_by.anchor_thread != started_by.anchor else ""
            parts.append(f"[chat_id: {started_by.conversation}, message_id: {started_by.anchor}{root}] [trigger: event]")
        else:
            parts.append(f"[chat_id: {started_by.conversation}] [trigger: event]")
    if held:
        parts.append(f"[matrix_events]\n{json.dumps(dict(held), ensure_ascii=False)}")
    if started_by is not None:
        parts.append(EVENT_TURN_RANKING + (REPLY_BY_TOOL if started_by.anchor else EVENT_TURN_DESTINATION_ROOT))
    return "\n\n".join(parts)
