"""Text carried into Slack message and event turns."""

import json
from collections.abc import Mapping
from typing import Any

from belle_agent.host import events as host_events
from belle_agent.platforms.slack.scoped_config import (
    TRIGGER_ALL,
    TRIGGER_EVENT,
    TRIGGER_HAS_FILE,
    TRIGGER_MENTION,
    TRIGGER_REPLY,
    TRIGGER_URL,
)

# Use the scope trigger names in labels. DMs have no channel trigger.
_LABELLED_TRIGGERS: frozenset[str] = frozenset(
    {
        TRIGGER_MENTION,
        TRIGGER_REPLY,
        TRIGGER_ALL,
        TRIGGER_URL,
        TRIGGER_HAS_FILE,
        TRIGGER_EVENT,
    }
)

# A URL or file trigger needs a label even without a prompt.
_SELF_EXPLAINING_TRIGGERS: frozenset[str] = frozenset(
    {TRIGGER_MENTION, TRIGGER_REPLY, TRIGGER_ALL}
)

# Brackets distinguish connector metadata from the user's text.
_TRIGGER_LABEL_FORMAT = "[trigger: {name}]"


def trigger_label(trigger: str, *, has_prompt: bool) -> str:
    """Name the trigger when a prompt needs it or the message cannot explain it.

    ``has_prompt`` tracks the resolved prompt even when a caller omits repeated text.
    """
    if trigger not in _LABELLED_TRIGGERS:
        return ""
    if not has_prompt and trigger in _SELF_EXPLAINING_TRIGGERS:
        return ""
    return _TRIGGER_LABEL_FORMAT.format(name=trigger)


# Use Slack's ts and thread_ts names so the marker matches tool arguments.
# The turn envelope already names the conversation.
_LOCATION_LABEL_FORMAT = "[ts: {ts}]"
_LOCATION_LABEL_THREAD_FORMAT = "[ts: {ts}, thread_ts: {thread_ts}]"


def location_label(ts: str, thread_ts: str) -> str:
    """Name the message position using the event timestamp, never a fallback."""
    message_ts = str(ts or "").strip()
    if not message_ts:
        return ""
    root_ts = str(thread_ts or "").strip()
    if root_ts:
        return _LOCATION_LABEL_THREAD_FORMAT.format(ts=message_ts, thread_ts=root_ts)
    return _LOCATION_LABEL_FORMAT.format(ts=message_ts)


# Buffered events state facts without directing the model.
_INBOUND_EVENTS_LABEL = "[slack_events]"


def inbound_events_block(payload: Mapping[str, Any] | None) -> str:
    """Render a buffered event set as JSON with Slack field names."""
    if not payload:
        return ""
    return f"{_INBOUND_EVENTS_LABEL}\n{json.dumps(payload, ensure_ascii=False)}"


# Distinguish the event that started this turn from buffered context.
_EVENT_TURN_LABEL = "[slack_event]"


def event_turn_block(record: Mapping[str, Any] | None) -> str:
    """Render the event that started this turn."""
    if not record:
        return ""
    return f"{_EVENT_TURN_LABEL}\n{json.dumps(record, ensure_ascii=False)}"


# The core's words, for an event's turn on every platform.
EVENT_TURN_RANKING = host_events.EVENT_TURN_RANKING

# A reply to an anchored event stays in its thread.
EVENT_TURN_DESTINATION_THREAD = (
    " If you do answer, your reply is posted in the thread named above, which"
    " is the only place this turn can post anything."
)

EVENT_TURN_DESTINATION_ROOT = host_events.EVENT_TURN_DESTINATION_ROOT


def event_turn_instruction(*, anchored_on_a_message: bool) -> str:
    """Combine the event rule with its routed destination."""
    return EVENT_TURN_RANKING + (
        EVENT_TURN_DESTINATION_THREAD
        if anchored_on_a_message
        else EVENT_TURN_DESTINATION_ROOT
    )


def message_turn_text(
    text: str, *, message_ts: str, thread_ts: str, trigger: str,
    prompt: str, buffered_events: str, silence_instruction: str,
) -> str:
    """Append the connector's facts and policy after a user's message."""
    envelope = location_label(message_ts, thread_ts)
    label = trigger_label(trigger, has_prompt=bool(prompt))
    if label:
        envelope = f"{envelope} {label}".strip()
    appended = [
        part for part in (
            envelope, buffered_events, prompt if prompt and prompt not in text else "",
            silence_instruction,
        ) if part
    ]
    return "\n\n".join([text, *appended]) if appended else text


def event_turn_text(
    record: Mapping[str, Any], *, anchor_ts: str, anchor_thread_ts: str,
    anchored: bool, buffered_events: str, prompt: str,
    silence_instruction: str, destination: str = "",
) -> str:
    """Render an event turn with the same envelope as a message turn.

    ``destination`` says where an answer goes, for a caller that does not place it as the default sentence says."""
    return "\n\n".join(
        part for part in (
            event_turn_block(record),
            f"{location_label(anchor_ts, anchor_thread_ts)}"
            f" {trigger_label(TRIGGER_EVENT, has_prompt=True)}".strip(),
            buffered_events,
            EVENT_TURN_RANKING + destination if destination else event_turn_instruction(anchored_on_a_message=anchored),
            prompt,
            silence_instruction,
        ) if part
    )


def append_turn_advice(text: str, advice: str, *, suffixes: tuple[str, ...]) -> str:
    """Place routing advice before a final silence instruction, if present."""
    for fragment in suffixes:
        suffix = f"\n\n{fragment}"
        if text.endswith(suffix):
            return text[:-len(suffix)] + f"\n\n{advice}" + suffix
    return f"{text}\n\n{advice}"
