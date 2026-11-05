"""Persist a bounded window of inbound event IDs across reconnects."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import portalocker

logger = logging.getLogger(__name__)

# Keep the file small enough to rewrite after each message.
DEFAULT_MAX_ENTRIES = 1024
_FILE_LOCK_TIMEOUT_SEC = 10.0
_SCHEMA_VERSION = 1


class EventDedupStore:
    """Remember handled event IDs across restarts and processes."""

    #: Names the platform in log lines.
    label = "Event"

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        file_lock_timeout: float = _FILE_LOCK_TIMEOUT_SEC,
    ) -> None:
        self._path = path
        self._max_entries = max(1, int(max_entries))
        self._file_lock_timeout = float(file_lock_timeout)
        self._lock = asyncio.Lock()
        # Keep deduplicating in memory if the file becomes unavailable.
        self._fallback: list[str] = []
        self._degraded = False

    @property
    def path(self) -> Path:
        return self._path

    async def remember(self, key: str) -> bool:
        """Return whether ``key`` was new, using memory if persistence fails."""
        if not key:
            return True
        async with self._lock:
            try:
                return await asyncio.to_thread(self._remember_under_file_lock, key)
            except Exception as exc:  # noqa: BLE001 - portalocker/OS errors vary
                if not self._degraded:
                    logger.warning(
                        "%s dedup store unavailable (%s); de-duplicating in "
                        "memory only, so a restart may re-answer a retried event",
                        self.label,
                        exc,
                    )
                    self._degraded = True
                return self._remember_in_memory(key)

    async def holds(self, key: str) -> bool:
        """Return whether ``key`` was remembered, without remembering it."""
        if not key:
            return False
        async with self._lock:
            try:
                seen = await asyncio.to_thread(self._read_unlocked)
                return key in seen or (self._degraded and key in self._fallback)
            except Exception:  # noqa: BLE001 - as in ``remember``, memory stands in for the file
                return key in self._fallback

    def _keep_newest(self, events: list[str]) -> None:
        """Drop the oldest entries so only ``max_entries`` ids remain."""
        del events[: max(0, len(events) - self._max_entries)]

    def _remember_in_memory(self, key: str) -> bool:
        if key in self._fallback:
            return False
        self._fallback.append(key)
        self._keep_newest(self._fallback)
        return True

    def _remember_under_file_lock(self, key: str) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), timeout=self._file_lock_timeout):
            stored = self._read_unlocked()
            # Preserve events accepted in memory while persistence was unavailable.
            seen = list(dict.fromkeys([*stored, *self._fallback])) if self._degraded else stored.copy()
            is_new = key not in seen
            if is_new:
                seen.append(key)
            self._keep_newest(seen)
            if seen != stored:
                self._write_unlocked(seen)
            self._fallback = list(seen)
            self._degraded = False
            return is_new

    def _read_unlocked(self) -> list[str]:
        """Read the stored window. Malformed JSON resets it; filesystem errors use the memory fallback."""
        try:
            if not self._path.exists():
                return []
            raw = self._path.read_text(encoding="utf-8")
            if not raw.strip():
                return []
            data: Any = json.loads(raw)
            if not isinstance(data, dict):
                return []
            events = data.get("events")
            if not isinstance(events, list):
                return []
            return [str(item) for item in events if isinstance(item, (str, int))]
        except OSError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s dedup file %s is unreadable (%s); starting a fresh window",
                self.label,
                self._path,
                exc,
            )
            return []

    def _write_unlocked(self, events: list[str]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = json.dumps(
            {"version": _SCHEMA_VERSION, "events": events},
            ensure_ascii=False,
        )
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self._path)
