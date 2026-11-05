"""Decide whether a session must defer new work, what one message carries, and what becomes of an event."""

import re
from collections.abc import Collection, Mapping
from typing import Any

from belle_agent.scopes import MID_TURN_QUEUE

#: The ``delivery`` key that says what a conversation does with each kind of event.
KEY_EVENTS = "events"

EVENT_OFF = "off"
EVENT_CONTEXT = "context"
EVENT_TURN = "turn"

EVENT_DISPOSITIONS: tuple[str, ...] = (EVENT_OFF, EVENT_CONTEXT, EVENT_TURN)

MODE_ALL = "all"
TRIGGER_REPLY = "reply"
MODE_URL = "url"
MODE_HAS_FILE = "has_file"

_HTTP_URL_RE = re.compile(r"https?://[^\s<>()|]+", re.IGNORECASE)


def carries_url(text: str) -> bool:
    """Whether a message's text holds a web link, for a conversation that answers messages with one."""
    return _HTTP_URL_RE.search(text) is not None


def selected_message_trigger(
    triggers: Collection[str], text: str, *, reply_to_bot: bool, has_file: bool,
) -> str:
    """Select the first matching content trigger: all, reply, link, then file."""
    if MODE_ALL in triggers:
        return MODE_ALL
    if TRIGGER_REPLY in triggers and reply_to_bot:
        return TRIGGER_REPLY
    if MODE_URL in triggers and carries_url(text):
        return MODE_URL
    if MODE_HAS_FILE in triggers and has_file:
        return MODE_HAS_FILE
    return ""


def disposition_word(value: Any) -> str:
    """Normalize a disposition. YAML 1.1 parses unquoted ``off`` as ``False``."""
    if isinstance(value, bool):
        return EVENT_OFF if value is False else ""
    return str(value or "").strip().lower()


def disposition_holds(word: str, *, may_turn: bool) -> bool:
    """Accept a known disposition only when its destination permits it."""
    return word in EVENT_DISPOSITIONS and (word != EVENT_TURN or may_turn)


def normalize_event_policy(
    raw: Any, *, kinds: Collection[str], turn_kinds: Collection[str],
) -> dict[str, str] | None:
    """Normalize known event kinds and drop dispositions they cannot support."""
    if not isinstance(raw, Mapping):
        return None
    settled: dict[str, str] = {}
    for kind, disposition in raw.items():
        name = str(kind or "").strip().lower()
        word = disposition_word(disposition)
        if name in kinds and disposition_holds(word, may_turn=name in turn_kinds):
            settled[name] = word
    return settled


def event_disposition(policy: Any, kind: str, *, may_turn: bool) -> str:
    """Read what a conversation does with one kind of event. What cannot be read, or held, is ``off``."""
    if not isinstance(policy, Mapping):
        return EVENT_OFF
    words = {str(key or "").strip().lower(): disposition_word(value) for key, value in policy.items()}
    word = words.get(kind, "")
    return word if disposition_holds(word, may_turn=may_turn) else EVENT_OFF


def should_queue_message(
    mid_turn: str, *, turn_running: bool, event_admitting: bool,
    event_turn_active: bool, messages_waiting: bool,
) -> bool:
    """Keep arrivals separate while a turn or event admission owns the session."""
    return (mid_turn == MID_TURN_QUEUE or event_admitting or event_turn_active) and (
        turn_running or event_admitting or messages_waiting
    )


def should_hold_event(
    *, reserved: bool, event_admitting: bool, earlier_event_admitting: bool,
    turn_running: bool, awaiting_answer: bool, messages_waiting: bool,
    events_waiting: bool,
) -> bool:
    """Keep event order without bypassing a turn, question, or reservation."""
    return bool(
        (event_admitting and not reserved)
        or earlier_event_admitting
        or turn_running
        or awaiting_answer
        or (not reserved and (messages_waiting or events_waiting))
    )
