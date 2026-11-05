"""Bounded in-memory event context for a conversation."""

import time
from collections.abc import Callable, Mapping
from typing import Any

DROP_OVERFLOW = "overflow"
DROP_EXPIRED = "expired"


class InboundEventBuffer:
    """Buffer events by conversation and family until a turn."""

    def __init__(
        self,
        *,
        limits: Mapping[str, int],
        ttl_seconds: float,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._limits = dict(limits)
        self._ttl_seconds = float(ttl_seconds)
        self._now = now or time.time
        self._held: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = {}
        self._dropped: dict[tuple[str, str], dict[str, int]] = {}

    def record(self, chat_id: str, family: str, record: Mapping[str, Any]) -> None:
        """Keep an event; count and remove the oldest when its family is full."""
        chat_id = str(chat_id or "").strip()
        if not chat_id or family not in self._limits:
            return
        key = (chat_id, family)
        held = self._held.setdefault(key, [])
        now = float(self._now())
        self._expire(key, held, now)
        limit = max(int(self._limits.get(family, 0)), 0)
        if limit <= 0:
            return
        overflow = max(0, len(held) - limit + 1)
        if overflow:
            del held[:overflow]
            self._count_drop(key, DROP_OVERFLOW, overflow)
        held.append((now, dict(record)))

    def fold(self, chat_id: str) -> dict[str, Any] | None:
        """Return and clear events and drop counts in arrival order."""
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return None
        now = float(self._now())
        entries: list[tuple[float, dict[str, Any]]] = []
        dropped: dict[str, dict[str, int]] = {}
        for family in self._limits:
            key = (chat_id, family)
            held = self._held.pop(key, None)
            if held is not None:
                self._expire(key, held, now)
                entries.extend(held)
            counts = self._dropped.pop(key, None)
            if counts:
                dropped[family] = counts
        if not entries and not dropped:
            return None
        entries.sort(key=lambda item: item[0])
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "events": [record for _, record in entries],
        }
        if dropped:
            payload["dropped"] = dropped
        return payload

    def forget(self, chat_id: str = "") -> None:
        """Discard one conversation's state, or all state if no ID is given."""
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            self._held.clear()
            self._dropped.clear()
            return
        for family in self._limits:
            self._held.pop((chat_id, family), None)
            self._dropped.pop((chat_id, family), None)

    def _expire(
        self,
        key: tuple[str, str],
        held: list[tuple[float, dict[str, Any]]],
        now: float,
    ) -> None:
        if self._ttl_seconds <= 0:
            return
        cutoff = now - self._ttl_seconds
        expired = next((index for index, (when, _) in enumerate(held) if when >= cutoff), len(held))
        if expired:
            del held[:expired]
            self._count_drop(key, DROP_EXPIRED, expired)

    def _count_drop(self, key: tuple[str, str], reason: str, count: int = 1) -> None:
        counts = self._dropped.setdefault(key, {})
        counts[reason] = counts.get(reason, 0) + count
