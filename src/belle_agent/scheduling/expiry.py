"""When a session expires, and the deletion of an expired session. One rule for every session kind."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol

from belle_agent.scheduling.clock import Clock, iso_utc
from belle_agent.scheduling.records import DetachedSession, owes_delivery, utc_time
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore, Snapshot, drop_history, heartbeat, owner_is_live, run_due

logger = logging.getLogger(__name__)


def held(session: dict[str, Any], jobs: dict[str, dict[str, Any]]) -> bool:
    """Whether something still needs the session: a run that waits or runs, a post that Belle owes, or its shared job."""
    job = jobs.get(session["job_id"])
    return (
        any(run["state"] in {"pending", "running"} or owes_delivery(run) for run in session["runs"])
        or job is not None and job["shared_session_id"] == session["session_id"]
    )


def _ended(session: dict[str, Any]) -> datetime:
    ends = [utc_time(run["ended_iso_utc"]) for run in session["runs"] if run["ended_iso_utc"]]
    return max(ends, default=utc_time(session["created_iso_utc"]))


def expires_iso_utc(session: dict[str, Any], snapshot: Snapshot, settings: SchedulingSettings) -> str | None:
    """Return when the session expires, or None while it is held.

    An unheld session expires ``session_retention_hours`` after its latest run ended. It expires when its latest
    run ended if its job was deleted without history, or if its fresh job has ``kept_runs_per_job`` newer ended
    sessions.
    """
    if held(session, snapshot.jobs):
        return None
    ended = _ended(session)
    stub = snapshot.deleted.get(session["job_id"])
    if stub is not None and not stub["history_kept"]:
        return iso_utc(ended)
    if session["job_mode"] == "fresh":
        newer = [
            other for other in snapshot.job_sessions(session["job_id"])
            if other is not session and not held(other, snapshot.jobs)
            and (_ended(other), other["session_id"]) > (ended, session["session_id"])
        ]
        if len(newer) >= settings.kept_runs_per_job:
            return iso_utc(ended)
    return iso_utc(ended + timedelta(hours=settings.session_retention_hours))


def expired(expires: str | None, now: datetime) -> bool:
    """Whether a session with this expiry time has expired at ``now``."""
    return expires is not None and now >= utc_time(expires)


class Blocked(Exception):
    """The running backend cannot delete a session's conversation. The text says why."""


class Deletions:
    """Delete claimed sessions on the harness, for detached and attached sessions alike.

    The backend of a process does not change, so a session that it cannot delete stays ``blocked``. This process
    claims such a session once and logs it once, when it becomes blocked.
    """

    def __init__(self, kind: str) -> None:
        #: The kind of session, for the log.
        self.kind = kind
        #: The keys of the sessions that this process found blocked.
        self.blocked: set[str] = set()

    async def delete(self, key: str, claimed: str, delete: Callable[[], Awaitable[None]],
                     settle: Callable[[str, str], Awaitable[None]]) -> None:
        """Delete a session on the harness, then settle its record as ``deleted``, ``deleting`` or ``blocked``.

        ``claimed`` is the deletion state that the claim read.
        """
        try:
            await delete()
        except Blocked as exc:
            self.blocked.add(key)
            if claimed != "blocked":
                logger.warning("%s %s stays in the store: %s", self.kind, key, exc)
            await settle("blocked", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a failed deletion keeps the record for the next pass
            logger.warning("could not delete %s %s on the harness: %s", self.kind, key, exc)
            await settle("deleting", f"{type(exc).__name__}: {exc}")
            return
        logger.info("deleted %s %s on the harness", self.kind, key)
        await settle("deleted", "")


class Deleter(Protocol):
    """Delete the harness conversation that a session owns."""

    async def delete_owned_conversation(self, session: DetachedSession) -> None: ...


class Expiry:
    """Delete expired sessions: first the conversation on the harness, then the record.

    A store transaction calls no harness. A pass claims each expired session under the store lock, deletes its
    conversation outside the lock, and then deletes the record under the lock. A failed deletion keeps the record
    and its claim lapses, so the next pass tries again. A dead owner's claim lapses with its heartbeat. See
    :class:`Deletions` for a session that the backend cannot delete.
    """

    def __init__(self, store: JobStore, deleter: Deleter, clock: Clock, settings: SchedulingSettings, owner: str) -> None:
        self.store = store
        self.deleter = deleter
        self.clock = clock
        self.settings = settings
        self.owner = owner
        self._pass = asyncio.Lock()
        self._deletions = Deletions("detached session")

    async def tick(self) -> None:
        """Run one deletion pass. One pass runs at a time in each process."""
        async with self._pass:
            for session, state in await self._claim():
                await self._deletions.delete(session.session_id, state,
                                             partial(self.deleter.delete_owned_conversation, session),
                                             partial(self._settle, session))

    async def _claim(self) -> list[tuple[DetachedSession, str]]:
        """Claim each expired session. Return it with the deletion state that the claim read."""
        now = self.clock.now()
        claimed = []
        async with self.store.transaction() as snapshot:
            # A claim holds while its owner is live.
            heartbeat(snapshot.owners, self.owner, now)
            for stored in snapshot.sessions.values():
                deletion = stored["deletion"]
                claimer = deletion["claimed_by"]
                if (stored["session_id"] in self._deletions.blocked
                        or claimer and claimer != self.owner and owner_is_live(snapshot.owners, claimer, self.owner, now)):
                    continue
                if not expired(expires_iso_utc(stored, snapshot, self.settings), now):
                    continue
                claimed.append((DetachedSession.from_json(deepcopy(stored), f"sessions.{stored['session_id']}"),
                                deletion["state"]))
                deletion.update(state="deleting", claimed_by=self.owner)
        return claimed

    async def _settle(self, session: DetachedSession, state: str, error: str) -> None:
        """End a claimed deletion. ``deleted`` deletes the record. Another state keeps it with ``error``."""
        async with self.store.transaction() as snapshot:
            stored = snapshot.sessions.get(session.session_id)
            if stored is None or stored["deletion"]["claimed_by"] != self.owner:
                return
            if state == "deleted":
                del snapshot.sessions[session.session_id]
                drop_history(snapshot.job_record(session.job_id), map(run_due, stored["runs"]))
                return
            deletion = stored["deletion"]
            deletion.update(state=state, claimed_by="", attempts=deletion["attempts"] + 1, last_error=error,
                            last_attempt_iso_utc=iso_utc(self.clock.now()))

