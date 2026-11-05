"""The host's record of each attached session that a turn runs in, or that waits for its idle expiry.

An attached session is the harness session of a chat place: Belle owns none of it. Its record holds the running turn, so
that a restarted host can recover or stop a turn that outlived the process that started it. With
``attached_sessions.expire_after_idle_hours``, the record also stays after its turn, so that Belle can delete an idle
attached session. A record holds no turn history and no detached session.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Container, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import InboundMessage
from belle_agent.scheduling.clock import iso_utc
from belle_agent.scheduling.records import (
    NO_DELETION,
    SESSION_CONVERSATION_PREFIX,
    Deletion,
    Record,
    RecordInvalid,
    utc_time,
)
from belle_agent.scheduling.store import StoreUnreadable, check_owners, heartbeat, locked, owner_is_live, write_checked

_VERSION = 1


@dataclass(frozen=True)
class RecordedMessage(Record):
    """The message that started a chat turn, with what a restarted host needs to build the turn again."""

    platform: str
    workspace: str
    conversation: str
    message_id: str
    sender: str
    text: str
    event_id: str
    thread: str
    chat_type: str
    trigger: str
    reply_thread: str | None
    envelope: str
    side: str
    skills_required: list[str]
    reply_optional: bool

    _NONEMPTY = ("platform", "conversation", "sender")

    @classmethod
    def of(cls, inbound: InboundMessage, *, reply_optional: bool) -> "RecordedMessage":
        """Record the fields of ``inbound`` that the turn needs."""
        names = [item.name for item in fields(cls) if item.name not in {"skills_required", "reply_optional"}]
        return cls(**{name: getattr(inbound, name) for name in names},
                   skills_required=list(inbound.skills_required), reply_optional=reply_optional)

    def message(self, request_id: str) -> InboundMessage:
        """Build the message again. A message with no event id or message id had ``request_id`` from the clock."""
        values = self.to_json()
        del values["reply_optional"]
        return InboundMessage(**{**values, "event_id": self.event_id or request_id,
                                 "skills_required": tuple(self.skills_required)})


@dataclass(frozen=True)
class RunningTurn(Record):
    """A chat turn that a host runs now."""

    request_id: str
    #: The message that started the turn. None for a turn that an event started.
    inbound: RecordedMessage | None
    started_iso_utc: str
    #: The process that runs the turn. See ``owner_is_live``.
    owner: str
    #: The host has begun to deliver the turn's answer. A restart then delivers nothing again.
    delivering: bool

    _NONEMPTY = ("request_id", "started_iso_utc", "owner")


@dataclass(frozen=True)
class AttachedSession(Record):
    """An attached session and its running turn."""

    key: str
    backend: str
    running: RunningTurn | None
    last_turn_ended_iso_utc: str
    #: The deletion of an idle conversation. See ``Deletion``.
    deletion: Deletion

    _NONEMPTY = ("key",)

    def _check(self, path: str) -> None:
        if self.key.startswith(SESSION_CONVERSATION_PREFIX):
            raise RecordInvalid(f"{path}.key must not start with {SESSION_CONVERSATION_PREFIX}: that key is a detached session's")


@dataclass(frozen=True)
class AttachedSessionSettings:
    """Belle's settings for attached sessions."""

    #: Delete an attached session on the harness this many hours after its last turn ended. None never deletes one.
    expire_after_idle_hours: int | None = None


def attached_session_settings(config: Mapping[str, Any]) -> AttachedSessionSettings:
    """Read and check the optional ``attached_sessions`` section."""
    section = config.get("attached_sessions") or {}
    if not isinstance(section, dict) or set(section) - {"expire_after_idle_hours"}:
        raise BelleConfigError("Belle config attached_sessions must be a mapping with only expire_after_idle_hours")
    value = section.get("expire_after_idle_hours", "disabled")
    if value == "disabled":
        return AttachedSessionSettings()
    if type(value) is not int or value < 1:
        raise BelleConfigError(
            "Belle config attached_sessions.expire_after_idle_hours must be disabled or a whole number of hours, 1 or more. "
            "An unquoted off or no is read as false: write disabled"
        )
    return AttachedSessionSettings(value)


class AttachedSessionStore:
    """``<state_dir>/attached_sessions.json``, under a file lock that every process of the state directory shares."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "attached_sessions.json"
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[dict[str, Any]]:
        """Yield the file's contents, ``owners`` and ``sessions``, and write them if they changed."""
        async with locked(self.path, self._lock):
            data = await asyncio.to_thread(self._read)
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                await asyncio.to_thread(write_checked, self.path, data, self._read)

    def _read(self, source: Path | None = None) -> dict[str, Any]:
        source = source or self.path
        try:
            if not source.exists():
                return {"version": _VERSION, "owners": {}, "sessions": {}}
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise StoreUnreadable(self.path, str(exc)) from exc
        if (not isinstance(data, dict) or set(data) != {"version", "owners", "sessions"}
                or data["version"] != _VERSION or not isinstance(data["owners"], dict)
                or not isinstance(data["sessions"], dict)):
            raise StoreUnreadable(self.path, f"the root must have exactly version {_VERSION} and the objects owners "
                                             "and sessions")
        try:
            check_owners(data["owners"])
            for key, record in data["sessions"].items():
                if AttachedSession.from_json(record, f"sessions.{key}").key != key:
                    raise RecordInvalid(f"sessions.{key}.key must be {key}")
        except RecordInvalid as exc:
            raise StoreUnreadable(self.path, str(exc)) from exc
        return data

    async def beat(self, owner: str, now: datetime) -> None:
        """Record that ``owner`` still runs, so that no other process takes its turns."""
        async with self.transaction() as data:
            heartbeat(data["owners"], owner, now)
            for name, entry in list(data["owners"].items()):
                if utc_time(entry["seen_iso_utc"]) < now - timedelta(minutes=10):
                    del data["owners"][name]

    async def started(self, key: str, backend: str, running: RunningTurn, now: datetime) -> bool:
        """Record the running turn of an attached session.

        Return False, and change nothing, while the record holds a turn that another process runs or that a stopped
        process of ``backend`` left: that turn still needs its record, so that a host recovers it or stops it.
        """
        async with self.transaction() as data:
            heartbeat(data["owners"], running.owner, now)
            stored = data["sessions"].get(key, {})
            held = stored.get("running")
            if held is not None and held["owner"] != running.owner and (
                    stored["backend"] == backend or owner_is_live(data["owners"], held["owner"], running.owner, now)):
                return False
            ended = stored.get("last_turn_ended_iso_utc", "")
            # A turn that runs makes the conversation active again, so no deletion of it waits.
            data["sessions"][key] = AttachedSession(key, backend, running, ended, NO_DELETION).to_json()
        return True

    async def delivering(self, key: str, request_id: str) -> None:
        """Record that the host delivers the answer of an attached session's running turn now."""
        async with self.transaction() as data:
            running = data["sessions"].get(key, {}).get("running")
            if running is not None and running["request_id"] == request_id:
                running["delivering"] = True

    async def take_orphans(self, backend: str, owner: str,
                           now: datetime) -> tuple[list[tuple[str, RunningTurn]], set[str]]:
        """Take each running turn of ``backend`` whose owner is gone, so that one process handles it once.

        Return the taken turns, and the keys whose turn another live process runs.
        """
        taken, elsewhere = [], set()
        async with self.transaction() as data:
            heartbeat(data["owners"], owner, now)
            for key, record in data["sessions"].items():
                running = record["running"]
                if running is None or running["owner"] == owner:
                    continue
                if owner_is_live(data["owners"], running["owner"], owner, now):
                    elsewhere.add(key)
                elif record["backend"] == backend:
                    taken.append((key, RunningTurn.from_json(deepcopy(running), f"sessions.{key}.running")))
                    running["owner"] = owner
        return taken, elsewhere

    async def give_back(self, key: str, running: RunningTurn, owner: str) -> None:
        """Give a taken turn back to its stopped owner, so that a later pass takes it again."""
        async with self.transaction() as data:
            stored = (data["sessions"].get(key) or {}).get("running")
            if stored is not None and stored["request_id"] == running.request_id and stored["owner"] == owner:
                stored["owner"] = running.owner

    async def ended(self, key: str, request_id: str, now: str, *, keep: bool) -> None:
        """Clear the running turn of an attached session. Keep the record only when ``keep`` is true."""
        async with self.transaction() as data:
            record = data["sessions"].get(key)
            if record is None or (record["running"] or {}).get("request_id") != request_id:
                return
            if keep:
                record.update(running=None, last_turn_ended_iso_utc=now)
            else:
                del data["sessions"][key]

    async def claim_idle(self, owner: str, now: datetime, idle_hours: int,
                         skip: Container[str]) -> list[tuple[AttachedSession, str]]:
        """Claim each attached session of no running turn whose last turn ended ``idle_hours`` ago or more.

        Skip each key in ``skip``. Return each claimed record with the deletion state that the claim read.
        """
        claimed = []
        async with self.transaction() as data:
            heartbeat(data["owners"], owner, now)
            for key, record in data["sessions"].items():
                claimer = record["deletion"]["claimed_by"]
                if key in skip or claimer and claimer != owner and owner_is_live(data["owners"], claimer, owner, now):
                    continue
                ended = record["last_turn_ended_iso_utc"]
                if record["running"] is None and ended and now >= utc_time(ended) + timedelta(hours=idle_hours):
                    state = record["deletion"]["state"]
                    record["deletion"].update(state="deleting", claimed_by=owner)
                    claimed.append((AttachedSession.from_json(deepcopy(record), f"sessions.{key}"), state))
        return claimed

    async def release(self, record: AttachedSession, owner: str) -> None:
        """Give up the claim on an attached session that is active again: no deletion of it waits."""
        async with self.transaction() as data:
            stored = data["sessions"].get(record.key)
            if stored is not None and stored["deletion"]["claimed_by"] == owner:
                stored["deletion"] = NO_DELETION.to_json()

    async def settle(self, record: AttachedSession, owner: str, now: datetime, state: str, error: str = "") -> None:
        """End a claimed deletion.

        ``deleted``: the harness deleted the conversation, so the record goes. ``deleting`` or ``blocked``: the record
        stays with ``error``, and the claim lapses.
        """
        async with self.transaction() as data:
            stored = data["sessions"].get(record.key)
            if stored is None or stored["deletion"]["claimed_by"] != owner:
                return
            if state == "deleted":
                del data["sessions"][record.key]
                return
            deletion = stored["deletion"]
            deletion.update(state=state, claimed_by="", attempts=deletion["attempts"] + 1, last_error=error,
                            last_attempt_iso_utc=iso_utc(now))
