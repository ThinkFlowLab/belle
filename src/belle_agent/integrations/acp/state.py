"""What the ACP backend keeps across restarts: each conversation's session, and the requests it admitted.

A request is recorded when it is admitted, before anything is sent to the agent, and again with its terminal
event. Its absence is thus proof that nothing was submitted, as long as no entry was ever dropped. The file keeps
the last ``remembered_requests`` requests, each with its final text unless its turn withheld the final. It holds
no token, no binding and no prompt text.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from belle_agent.backend import EVENT_FAILED, EVENT_INTERRUPTED, EVENT_SUCCEEDED, TurnEvent

logger = logging.getLogger(__name__)


class State:
    """One JSON file, written whole and atomically, readable by its owner only."""

    def __init__(self, path: Path, window: int) -> None:
        self._path = path
        #: How many requests the file keeps. The oldest goes first.
        self._window = window
        self._sessions: dict[str, str] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        #: A request may be missing because it was dropped, so a missing one proves nothing.
        self.dropped = False

    def load(self) -> None:
        """Read the file. A missing one is an empty record. An unreadable one proves nothing."""
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise TypeError("state must be an object")
            sessions, requests = stored.get("sessions"), stored.get("requests")
            if not isinstance(sessions, dict) or not all(isinstance(value, str) for value in sessions.values()):
                raise TypeError("state.sessions must map conversation IDs to session IDs")
            if not isinstance(requests, dict) or not all(_valid_entry(value) for value in requests.values()):
                raise TypeError("state.requests must map request IDs to request records")
            if not isinstance(stored.get("dropped"), bool):
                raise TypeError("state.dropped must be a boolean")
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("acp: %s is unreadable, so no earlier session or request is known: %s", self._path, exc)
            self.dropped = True
            return
        self._sessions = sessions
        self._requests = requests
        self.dropped = stored.get("dropped")

    def session(self, conversation: str) -> str | None:
        """The conversation's ACP session id, if it has one."""
        return self._sessions.get(conversation)

    def set_session(self, conversation: str, session: str) -> None:
        """Record the conversation's ACP session id."""
        self._sessions[conversation] = session
        self._write()

    def drop_session(self, conversation: str) -> None:
        """Forget the conversation's ACP session id. A failed write is logged."""
        self._sessions.pop(conversation, None)
        try:
            self._write()
        except OSError as exc:
            logger.warning(
                "acp: cannot remove the session of conversation %s from %s: %s", conversation, self._path, exc
            )

    def request(self, key: str) -> dict[str, Any] | None:
        """The entry of a request, if it was admitted and not dropped."""
        return self._requests.get(key)

    def admit(self, key: str) -> None:
        """Record a request before it is submitted. An ``OSError`` means it must not be submitted."""
        self._requests[key] = {}
        while len(self._requests) > self._window:
            del self._requests[next(iter(self._requests))]
            self.dropped = True
        try:
            self._write()
        except OSError:
            del self._requests[key]
            raise

    def end(self, key: str, event: TurnEvent) -> None:
        """Record the terminal event of a request. A failed write is logged."""
        if key not in self._requests:
            return
        self._requests[key] = {"terminal": {"kind": event.kind, "text": event.text, "data": dict(event.data)}}
        try:
            self._write()
        except OSError as exc:
            logger.warning("acp: cannot record the end of request %s in %s: %s", key, self._path, exc)

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        written = self._path.with_suffix(".tmp")
        data = json.dumps({"sessions": self._sessions, "requests": self._requests, "dropped": self.dropped})
        descriptor = os.open(written, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        written.replace(self._path)
        # A missing request counts as never sent, so the replace must survive a crash too.
        directory = os.open(self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _valid_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if "terminal" not in entry:
        return True
    ended = entry["terminal"]
    return (
        isinstance(ended, dict)
        and ended.get("kind") in (EVENT_FAILED, EVENT_INTERRUPTED, EVENT_SUCCEEDED)
        and isinstance(ended.get("text", ""), str)
        and isinstance(ended.get("data", {}), dict)
    )


def terminal(entry: dict[str, Any]) -> TurnEvent | None:
    """The terminal event a request entry recorded, if it ended."""
    ended = entry.get("terminal")
    return TurnEvent(ended["kind"], text=ended.get("text", ""), data=ended.get("data") or {}) if ended else None
