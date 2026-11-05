"""What Mattermost reports beside messages, as the host's events: reactions, and members who join or leave a channel.

A pin is no event here. The server reports it as an edit of the post, with
nobody named as the one who pinned it, and the host takes no event without
the person who did it.
"""

import json
from collections.abc import Mapping
from typing import Any

from belle_agent.host.events import EVENT_TURN_DESTINATION_ROOT, EVENT_TURN_RANKING
from belle_agent.platforms.base import EventKind, InboundEvent

KIND_REACTION = "reaction"
KIND_MEMBER = "member"
#: The kind of each WebSocket event that the connector hands over.
EVENT_TYPES = {
    "reaction_added": KIND_REACTION,
    "reaction_removed": KIND_REACTION,
    "user_added": KIND_MEMBER,
    "user_removed": KIND_MEMBER,
}
#: Each system post that records a member change, with the event that it stands for and the prop that names the
#: member, or ``None`` where the post's author is the member. A team's own changes are posted in its default
#: channel, whose members are the team's.
MEMBER_POSTS: Mapping[str, tuple[str, str | None]] = {
    "system_add_to_channel": ("user_added", "addedUserId"),
    "system_join_channel": ("user_added", None),
    "system_remove_from_channel": ("user_removed", "removedUserId"),
    "system_leave_channel": ("user_removed", None),
    "system_add_to_team": ("user_added", "addedUserId"),
    "system_join_team": ("user_added", None),
    "system_remove_from_team": ("user_removed", None),
    "system_leave_team": ("user_removed", None),
}
#: As ``delivery.events`` names them, with how many a conversation keeps as context.
EVENT_KINDS: Mapping[str, EventKind] = {
    KIND_REACTION: EventKind(held=30, turns=True, anchored=True),
    KIND_MEMBER: EventKind(held=20, turns=True, waits=True),
}

#: Where the answer of an event's turn goes when the event is about a message.
REPLY_BY_TOOL = (
    " If you do answer, pass the message_id named above as reply_to_message_id to post_message. A post that"
    " names no message lands at the top level of this channel, where everyone in it sees it."
)


def render_events(held: Mapping[str, Any] | None, started_by: InboundEvent | None) -> str:
    """What a turn is told of events: the one that started it, with where it happened, and those its conversation kept."""
    parts = []
    if started_by is not None:
        parts.append(f"[mattermost_event]\n{json.dumps(dict(started_by.record), ensure_ascii=False)}")
        if started_by.anchor:
            root = f", root_message_id: {started_by.anchor_thread}" if started_by.anchor_thread != started_by.anchor else ""
            parts.append(f"[chat_id: {started_by.conversation}, message_id: {started_by.anchor}{root}] [trigger: event]")
        else:
            parts.append(f"[chat_id: {started_by.conversation}] [trigger: event]")
    if held:
        parts.append(f"[mattermost_events]\n{json.dumps(dict(held), ensure_ascii=False)}")
    if started_by is not None:
        parts.append(EVENT_TURN_RANKING + (REPLY_BY_TOOL if started_by.anchor else EVENT_TURN_DESTINATION_ROOT))
    return "\n\n".join(parts)
