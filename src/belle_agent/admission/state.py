"""Bounded queues and turn tracking behind admission decisions."""

import time
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from belle_agent.admission.policy import should_hold_event, should_queue_message


@dataclass
class AdmissionState:
    """Own waiting messages and event admission state for one connector.

    Each queue is keyed by a conversation key. Entries are the connector's own
    records. A turn has ``request_id`` and ``started_at``, a waiting message
    ``queued_at``, and a pending event ``arrival_seq`` and ``queued_at``.
    """

    queued_messages: dict[str, list[Any]] = field(default_factory=dict)
    pending_events: dict[str, list[Any]] = field(default_factory=dict)
    admitting_events: dict[str, dict[int, Any]] = field(default_factory=dict)
    event_admissions: set[str] = field(default_factory=set)
    turn_initiators: dict[str, Any] = field(default_factory=dict)
    event_turn_sessions: dict[str, str] = field(default_factory=dict)
    arrival_seq: int = 0
    #: Names the platform in warnings.
    label: str = "Conversation"

    def remember_turn(
        self, key: str, entry: Any, *, cap: int,
        timeout: float, warn: Callable[..., Any],
    ) -> None:
        cutoff = time.monotonic() - timeout
        for stale in [stale for stale, value in self.turn_initiators.items() if value.started_at < cutoff]:
            del self.turn_initiators[stale]
        while len(self.turn_initiators) >= cap:
            oldest = min(self.turn_initiators, key=lambda key: self.turn_initiators[key].started_at)
            warn("%s dropped the oldest of too many tracked turn initiators: %s", self.label, oldest)
            del self.turn_initiators[oldest]
        for tracked, request_id in list(self.event_turn_sessions.items()):
            current = self.turn_initiators.get(tracked)
            if current is None or current.request_id != request_id:
                del self.event_turn_sessions[tracked]
        self.turn_initiators[key] = entry

    def forget_turn(self, key: str, request_id: str) -> None:
        if not key or not request_id:
            return
        entry = self.turn_initiators.get(key)
        if entry is not None and entry.request_id == request_id:
            del self.turn_initiators[key]
            self.event_turn_sessions.pop(key, None)

    def turn_initiator(self, key: str, *, timeout: float) -> Any | None:
        if not key:
            return None
        entry = self.turn_initiators.get(key)
        if entry is not None and entry.started_at < time.monotonic() - timeout:
            del self.turn_initiators[key]
            self.event_turn_sessions.pop(key, None)
            return None
        return entry

    def queue_message(
        self, key: str, entry: Any, *, max_per_session: int, warn: Callable[..., Any]
    ) -> bool:
        """Preserve older promises when the conversation's queue is full."""
        queue = self.queued_messages.setdefault(key, [])
        if len(queue) >= max_per_session:
            warn(
                "%s refused a queued message: conversation %s already holds %s,"
                " which is the cap", self.label, key, len(queue),
            )
            return False
        queue.append(entry)
        return True

    def prune_messages(
        self, *, cutoff: float, max_sessions: int, warn: Callable[..., Any]
    ) -> None:
        """Expire waiting messages and evict the oldest full-session queue."""
        for key, queue in list(self.queued_messages.items()):
            fresh = [entry for entry in queue if entry.queued_at >= cutoff]
            if len(fresh) != len(queue):
                warn(
                    "%s dropped %s queued message(s) for conversation %s: the turn"
                    " they were waiting on never reported that it ended",
                    self.label, len(queue) - len(fresh), key,
                )
            if fresh:
                self.queued_messages[key] = fresh
            else:
                del self.queued_messages[key]
        while len(self.queued_messages) >= max_sessions:
            oldest = min(
                self.queued_messages,
                key=lambda key: self.queued_messages[key][0].queued_at,
            )
            warn(
                "%s dropped the queued messages of the oldest of too many"
                " waiting conversations: %s", self.label, oldest,
            )
            del self.queued_messages[oldest]

    def take_message(self, key: str) -> Any | None:
        queue = self.queued_messages.get(key)
        if not queue:
            return None
        entry = queue.pop(0)
        if not queue:
            del self.queued_messages[key]
        return entry

    def prune_events(self, *, cutoff: float) -> list[Any]:
        """Expire pending events and return them for loss logging."""
        expired: list[Any] = []
        for key, queue in list(self.pending_events.items()):
            fresh = [entry for entry in queue if entry.queued_at >= cutoff]
            expired.extend(entry for entry in queue if entry.queued_at < cutoff)
            if fresh:
                self.pending_events[key] = fresh
            else:
                del self.pending_events[key]
        return expired

    def pending_count(self, key: str = "") -> int:
        """Count unique queued and in-flight events."""
        sessions = (
            (key,) if key
            else self.pending_events.keys() | self.admitting_events.keys()
        )
        entries: set[int] = set()
        for current in sessions:
            entries.update(map(id, self.pending_events.get(current, ())))
            entries.update(map(id, self.admitting_events.get(current, {}).values()))
        return len(entries)

    def hold_event(
        self, key: str, entry: Any, *, max_per_session: int, max_total: int
    ) -> bool:
        """Hold a unique event within both caps, ordered by arrival sequence."""
        queue = self.pending_events.setdefault(key, [])
        new_entry = not (
            self.admitting_events.get(key, {}).get(entry.arrival_seq)
            is entry
            or any(waiting is entry for waiting in queue)
        )
        if (
            self.pending_count(key) + new_entry > max_per_session
            or self.pending_count() + new_entry > max_total
        ):
            if not queue:
                del self.pending_events[key]
            return False
        positions = [waiting.arrival_seq for waiting in queue]
        queue.insert(bisect_right(positions, entry.arrival_seq), entry)
        return True

    def next_arrival(self) -> int:
        self.arrival_seq += 1
        return self.arrival_seq

    def track_admitting(self, key: str, entry: Any) -> None:
        arrivals = self.admitting_events.setdefault(key, {})
        arrivals[entry.arrival_seq] = entry

    def can_reserve(self, key: str, *, turn_running: bool,
                    awaiting_answer: bool) -> bool:
        return not (
            key in self.event_admissions or turn_running
            or self.queued_messages.get(key)
            or self.pending_events.get(key)
            or self.admitting_events.get(key)
            or awaiting_answer
        )

    def must_queue_message(self, key: str, *, mid_turn: str,
                           turn_running: bool, event_turn_active: bool) -> bool:
        return should_queue_message(
            mid_turn,
            turn_running=turn_running,
            event_admitting=key in self.event_admissions,
            event_turn_active=event_turn_active,
            messages_waiting=bool(self.queued_messages.get(key)),
        )

    def should_hold(self, key: str, arrival_seq: int, *,
                    reserved: bool, turn_running: bool,
                    awaiting_answer: bool) -> bool:
        return should_hold_event(
            reserved=reserved,
            event_admitting=key in self.event_admissions,
            earlier_event_admitting=any(
                earlier < arrival_seq
                for earlier in self.admitting_events.get(key, ())
            ),
            turn_running=turn_running,
            awaiting_answer=awaiting_answer,
            messages_waiting=bool(self.queued_messages.get(key)),
            events_waiting=bool(self.pending_events.get(key)),
        )

    def take_pending_event(self, key: str) -> Any | None:
        """Reserve and remove the oldest ready event, preserving arrival order."""
        queue = self.pending_events.get(key)
        if not queue or key in self.event_admissions:
            return None
        if any(
            earlier < queue[0].arrival_seq
            for earlier in self.admitting_events.get(key, ())
        ):
            return None
        self.event_admissions.add(key)
        entry = queue.pop(0)
        if not queue:
            del self.pending_events[key]
        return entry

    def finish_admission(
        self, key: str, arrival_seq: int, reserved: bool
    ) -> None:
        if arrival_seq:
            outstanding = self.admitting_events.get(key)
            if outstanding is not None:
                outstanding.pop(arrival_seq, None)
                if not outstanding:
                    del self.admitting_events[key]
        if reserved:
            self.event_admissions.discard(key)

    def clear(self) -> None:
        """Drop waiting work when the connector stops; retain arrival order."""
        self.queued_messages.clear()
        self.pending_events.clear()
        self.admitting_events.clear()
        self.event_admissions.clear()
        self.turn_initiators.clear()
        self.event_turn_sessions.clear()
