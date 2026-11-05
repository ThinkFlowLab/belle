"""Transactional JSON store shared by scheduler processes."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import portalocker

from belle_agent.scheduling.clock import Clock, SystemClock, iso_utc
from belle_agent.scheduling.records import (
    ENDED_STATES,
    DeletedJob,
    DetachedSession,
    Job,
    RecordInvalid,
    owes_delivery,
    utc_time,
)

_VERSION = 4
#: An owner that wrote no heartbeat for this long is dead.
OWNER_TIMEOUT = timedelta(seconds=90)


class StoreUnreadable(Exception):
    """The existing store cannot be read safely and must not be replaced."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class StoreBusy(Exception):
    """The store lock was held for more than 10 seconds."""


@dataclass
class Snapshot:
    """Mutable store contents held under one cross-process transaction lock."""

    owners: dict[str, dict[str, Any]]
    jobs: dict[str, dict[str, Any]]
    deleted: dict[str, dict[str, Any]]
    sessions: dict[str, dict[str, Any]]

    def json(self) -> dict[str, Any]:
        """Return the versioned file shape."""
        return {"version": _VERSION, "owners": self.owners, "jobs": self.jobs, "deleted": self.deleted,
                "sessions": self.sessions}

    def job_sessions(self, job_id: str) -> Iterator[dict[str, Any]]:
        """Yield the sessions of one job."""
        return (session for session in self.sessions.values() if session["job_id"] == job_id)

    def job_record(self, job_id: str) -> dict[str, Any] | None:
        """Return the job, or the stub of the deleted job, that owns a history."""
        return self.jobs.get(job_id) or self.deleted.get(job_id)

    def find_run(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Return the session and the run of a run id."""
        for session in self.sessions.values():
            for run in session["runs"]:
                if run["run_id"] == run_id:
                    return session, run
        return None


def run_due(run: dict[str, Any]) -> str:
    """Return when a stored run fell due, or when someone asked for it."""
    return str(run["clock"]["due_iso_utc"] if run["clock"] else run["request"]["requested_iso_utc"])


def drop_history(record: dict[str, Any] | None, dues: Iterable[str]) -> None:
    """Record in a job or a deleted job that its history no longer holds the rows of ``dues``."""
    if record is None:
        return
    times = [*dues, *filter(None, [record["dropped_through_iso_utc"]])]
    if times:
        record["dropped_through_iso_utc"] = max(times, key=utc_time)


def heartbeat(owners: dict[str, dict[str, Any]], owner: str, now: datetime) -> None:
    """Record in ``owners`` that ``owner`` runs in this process now."""
    owners[owner] = {"pid": os.getpid(), "host": socket.gethostname(), "seen_iso_utc": iso_utc(now)}


def owner_is_live(owners: dict[str, dict[str, Any]], owner: str, me: str, now: datetime) -> bool:
    """Whether another process that shares the store still runs as ``owner``.

    A live owner wrote a heartbeat less than 90 s ago. On this host its process must also exist and differ from
    this one: a restarted container can give the new process the pid of the old one. Each process has one owner.
    """
    if owner == me:
        return True
    entry = owners.get(owner)
    if entry is None or utc_time(entry["seen_iso_utc"]) < now - OWNER_TIMEOUT:
        return False
    if entry["host"] != socket.gethostname():
        return True
    if entry["pid"] == os.getpid():
        return False
    try:
        os.kill(entry["pid"], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def check_owners(owners: dict[str, Any]) -> None:
    """Check the ``owners`` heartbeats of a store. Raise :class:`RecordInvalid` with the path and the form."""
    for owner, value in owners.items():
        if (not isinstance(value, dict) or set(value) != {"pid", "host", "seen_iso_utc"}
                or type(value["pid"]) is not int or not isinstance(value["host"], str)
                or not isinstance(value["seen_iso_utc"], str)):
            raise RecordInvalid(f"owners.{owner} must have exactly the integer pid and the strings host and seen_iso_utc")
        try:
            utc_time(value["seen_iso_utc"])
        except ValueError as exc:
            raise RecordInvalid(f"owners.{owner}.seen_iso_utc must be a UTC time: {exc}") from None


def _check(data: dict[str, Any]) -> None:
    """Read each record with its type, then check the rules between records."""
    check_owners(data["owners"])
    jobs = {key: Job.from_json(value, f"jobs.{key}") for key, value in data["jobs"].items()}
    deleted = {key: DeletedJob.from_json(value, f"deleted.{key}") for key, value in data["deleted"].items()}
    sessions = {key: DetachedSession.from_json(value, f"sessions.{key}") for key, value in data["sessions"].items()}
    for key, job in jobs.items():
        if job.job_id != key:
            raise RecordInvalid(f"jobs.{key}.job_id must be {key}")
        if key in deleted:
            raise RecordInvalid(f"jobs.{key} must not also be in deleted")
        if job.shared_session_id:
            shared = sessions.get(job.shared_session_id)
            if shared is None or shared.job_id != key or shared.job_mode != "shared":
                raise RecordInvalid(f"jobs.{key}.shared_session_id must name a session of job_mode shared of this job")
    claims: set[str] = set()
    runs: set[str] = set()
    shared_sessions: set[str] = set()
    for key, session in sessions.items():
        path = f"sessions.{key}"
        if session.session_id != key:
            raise RecordInvalid(f"{path}.session_id must be {key}")
        if session.job_id and session.job_id not in jobs and session.job_id not in deleted:
            raise RecordInvalid(f"{path}.job_id must name a job or a deleted job")
        if session.job_mode == "shared" and session.deletion.state == "none":
            if session.job_id in shared_sessions:
                raise RecordInvalid(f"{path} is a second shared session of job {session.job_id}")
            shared_sessions.add(session.job_id)
        for index, run in enumerate(session.runs):
            if run.run_id in runs:
                raise RecordInvalid(f"{path}.runs[{index}].run_id must be unique in the store")
            runs.add(run.run_id)
            if run.clock is not None:
                if run.clock.claim_key in claims:
                    raise RecordInvalid(f"{path}.runs[{index}].clock.claim_key must be unique in the store")
                claims.add(run.clock.claim_key)
    owners: list[tuple[str, str, Job | DeletedJob]] = [
        *(("jobs", key, job) for key, job in jobs.items()), *(("deleted", key, stub) for key, stub in deleted.items())
    ]
    for name, key, record in owners:
        for index, unrun in enumerate(record.unrun):
            if unrun.claim_key in claims:
                raise RecordInvalid(f"{name}.{key}.unrun[{index}].claim_key must be unique in the store")
            claims.add(unrun.claim_key)


@asynccontextmanager
async def locked(path: Path, lock: asyncio.Lock) -> AsyncIterator[None]:
    """Hold ``lock`` in this process and the lock file beside ``path`` across processes."""
    async with lock:
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        file_lock = portalocker.Lock(str(path.with_suffix(".json.lock")), timeout=10)
        acquiring: asyncio.Future[Any] = asyncio.ensure_future(asyncio.to_thread(lambda: file_lock.acquire()))

        def release_late(done: asyncio.Future[Any]) -> None:
            if not done.cancelled() and done.exception() is None:
                file_lock.release()

        try:
            await asyncio.shield(acquiring)
        except portalocker.exceptions.LockException as exc:
            raise StoreBusy(f"the lock of {path.name} was held for more than 10 s") from exc
        except asyncio.CancelledError:
            # The thread can still take the lock after the cancellation. It must not keep it.
            acquiring.add_done_callback(release_late)
            raise
        try:
            yield
        finally:
            await asyncio.to_thread(file_lock.release)


def write_checked(path: Path, data: dict[str, Any], read: Callable[[Path], Any]) -> None:
    """Replace ``path`` with ``data`` once ``read`` accepts the new file. A refused file replaces nothing."""
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        read(temporary)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class JobStore:
    """Write all job changes under a file lock and an in-process lock."""

    def __init__(self, state_dir: Path, *, kept_runs_per_job: int = 20, clock: Clock | None = None) -> None:
        self.path = state_dir / "sessions.json"
        self._kept_runs_per_job = kept_runs_per_job
        self._clock = clock or SystemClock()
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """The in-process lock. Code that must not run inside a transaction checks it."""
        return self._lock

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Snapshot]:
        """Yield a mutable snapshot and commit only if its contents changed."""
        async with locked(self.path, self._lock):
            snapshot = await asyncio.to_thread(self._read_unlocked)
            before = json.dumps(snapshot.json(), ensure_ascii=False, sort_keys=True)
            yield snapshot
            self._maintain(snapshot)
            if json.dumps(snapshot.json(), ensure_ascii=False, sort_keys=True) != before:
                self._bump_revisions(snapshot, json.loads(before)["jobs"])
                await asyncio.to_thread(self._write_unlocked, snapshot)

    def _read_unlocked(self, source: Path | None = None) -> Snapshot:
        source = source or self.path
        try:
            if not source.exists():
                return Snapshot({}, {}, {}, {})
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise StoreUnreadable(self.path, str(exc)) from exc
        if not isinstance(data, dict):
            raise StoreUnreadable(self.path, "root must be an object")
        version = data.get("version")
        if type(version) is not int or version < 1:
            raise StoreUnreadable(self.path, f"version must be {_VERSION}")
        if version > _VERSION:
            raise StoreUnreadable(self.path, f"written by a newer Belle: version {version}, this Belle reads version {_VERSION}")
        if version < _VERSION:
            raise StoreUnreadable(self.path, (
                f"version {version} holds records of the earlier session model, which this Belle does not read. "
                "Stop Belle and delete the file (and its .bak copies) to start an empty store."
            ))
        if set(data) != {"version", "owners", "jobs", "deleted", "sessions"} or any(
            not isinstance(data[key], dict) for key in ("owners", "jobs", "deleted", "sessions")
        ):
            raise StoreUnreadable(self.path, "the root must have exactly version and the objects owners, jobs, deleted and sessions")
        try:
            _check(data)
        except RecordInvalid as exc:
            raise StoreUnreadable(self.path, str(exc)) from exc
        return Snapshot(data["owners"], data["jobs"], data["deleted"], data["sessions"])

    def _maintain(self, snapshot: Snapshot) -> None:
        """Bound the history of each job. Delete no session: a session goes with its conversation, by expiry."""
        now = self._clock.now()
        kept = self._kept_runs_per_job
        for owner, entry in list(snapshot.owners.items()):
            if utc_time(entry["seen_iso_utc"]) < now - timedelta(minutes=10):
                del snapshot.owners[owner]
        for job_id, stub in list(snapshot.deleted.items()):
            if next(snapshot.job_sessions(job_id), None) is None:
                del snapshot.deleted[job_id]
        for record in (*snapshot.jobs.values(), *snapshot.deleted.values()):
            drop_history(record, [unrun["due_iso_utc"] for unrun in record["unrun"][:-kept]])
            del record["unrun"][:-kept]
        for session in snapshot.sessions.values():
            if session["job_mode"] != "shared":
                continue
            ended = [run for run in session["runs"] if run["state"] in ENDED_STATES and not owes_delivery(run)]
            drop_history(snapshot.job_record(session["job_id"]), map(run_due, ended[:-kept]))
            dropped = {run["run_id"] for run in ended[:-kept]}
            session["runs"] = [run for run in session["runs"] if run["run_id"] not in dropped]

    def _bump_revisions(self, snapshot: Snapshot, old_jobs: dict[str, Any]) -> None:
        for job_id, job in snapshot.jobs.items():
            previous = old_jobs.get(job_id)
            if previous is not None and job != previous:
                job["revision"] = previous["revision"] + 1

    def _write_unlocked(self, snapshot: Snapshot) -> None:
        write_checked(self.path, snapshot.json(), self._read_unlocked)
