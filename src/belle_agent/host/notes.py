"""What a conversation's next turn is told, kept in a file so that a restart does not lose it."""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Notes that go into the next turn's system additions: what became of its held calls.
SYSTEM_ADDITIONS = "system_additions"
#: Notes that go into the next turn's message: which turns a person stopped.
MESSAGE = "message"
# Each kind keeps notes for this many conversations.
MAX_NOTED_SESSIONS = 64
MAX_NOTE_CHARS = 600
_VERSION = 1


class TurnNotes:
    """Notes for the next turn of each conversation, by conversation key. The next turn takes them once.

    With a ``path``, the notes are read from that file at the start and written to it after each change.
    Without one, or where the file cannot be written, they are kept in memory only.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._kinds: dict[str, dict[str, list[str]]] = {SYSTEM_ADDITIONS: {}, MESSAGE: {}}
        self._unwritten = False
        if path is not None:
            self._read(path)

    def add(self, kind: str, key: str, text: str) -> None:
        """Keep ``text`` for the next turn of the conversation ``key``. The oldest conversation goes first."""
        self._keep(kind, key, text)
        self._write()

    def take(self, key: str) -> dict[str, list[str]]:
        """Remove and return the notes of the conversation ``key``, by kind."""
        taken = {kind: notes.pop(key, []) for kind, notes in self._kinds.items()}
        if any(taken.values()):
            self._write()
        return taken

    def _keep(self, kind: str, key: str, text: str) -> None:
        notes = self._kinds[kind]
        while key not in notes and len(notes) >= MAX_NOTED_SESSIONS:
            del notes[next(iter(notes))]
        notes.setdefault(key, []).append(text[:MAX_NOTE_CHARS])

    def _read(self, path: Path) -> None:
        try:
            stored: Any = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("turn notes file %s is unreadable, so no note from before the start is kept: %s", path, exc)
            return
        if not isinstance(stored, dict) or stored.get("version") != _VERSION:
            logger.warning(
                "turn notes file %s is not an object with version %s, so no note from before the start is kept",
                path, _VERSION,
            )
            return
        for kind in self._kinds:
            kept = stored.get(kind, {})
            if not isinstance(kept, dict) or not all(
                isinstance(texts, list) and all(isinstance(text, str) for text in texts) for texts in kept.values()
            ):
                logger.warning(
                    "turn notes file %s: %s is not a map of conversation keys to lists of strings,"
                    " so no note from before the start is kept",
                    path, kind,
                )
                self._kinds = {kind: {} for kind in self._kinds}
                return
            for key, texts in kept.items():
                for text in texts:
                    self._keep(kind, key, text)

    def _write(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            written = self._path.with_suffix(".tmp")
            written.write_text(json.dumps({"version": _VERSION, **self._kinds}, ensure_ascii=False), encoding="utf-8")
            written.replace(self._path)
        except OSError as exc:
            if not self._unwritten:
                logger.warning("turn notes cannot be written to %s, so a restart loses them: %s", self._path, exc)
            self._unwritten = True
            return
        self._unwritten = False
