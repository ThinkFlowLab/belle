"""Claim due jobs once and start the runs of every session within the configured run slots."""

import asyncio
import logging
from collections.abc import Collection
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from belle_agent.backend import (
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_SUCCEEDED,
    RECOVERY_NOT_SUBMITTED,
    RECOVERY_RUNNING,
    Recovery,
)
from belle_agent.control.protocol import Component, ScheduleRow, WorkRow
from belle_agent.scheduling.clock import Clock
from belle_agent.scheduling.clock import iso_utc as _iso
from belle_agent.scheduling.expiry import Expiry, expired, expires_iso_utc
from belle_agent.scheduling.records import (
    ENDED_STATES,
    NO_DELETION,
    ClockStart,
    DetachedSession,
    Job,
    RequestStart,
    SessionRun,
    UnrunDue,
    fingerprint,
    new_run,
    new_session_id,
    owes_delivery,
)
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore, Snapshot, StoreBusy, StoreUnreadable, heartbeat, owner_is_live

logger = logging.getLogger(__name__)
_POLL = timedelta(seconds=5)
_HEARTBEAT = timedelta(seconds=30)
_ERROR_LOG = timedelta(minutes=10)
#: How often a scheduler deletes expired sessions.
EXPIRY_INTERVAL = timedelta(minutes=5)
_SUCCEEDED = frozenset({"posted-final", "posted-by-tool", "withheld", "stored-final"})
#: The outcome of a run that recovery ended, by the kind of its terminal event.
RECOVERED_OUTCOMES = {EVENT_SUCCEEDED: "stored-final", EVENT_FAILED: "failed", EVENT_INTERRUPTED: "interrupted"}


class Recurrence(Protocol):
    """Return the first due UTC instant strictly after an instant."""

    def next_after(self, when: dict[str, Any], after: datetime) -> datetime | None: ...


class RunStarter(Protocol):
    """Start, stop, recover and report session runs in the host."""

    async def start_run(self, job: Job | None, session: DetachedSession, run: SessionRun) -> None: ...

    async def stop_run(self, session: DetachedSession, run: SessionRun) -> bool: ...

    async def recover_run(self, session: DetachedSession, run: SessionRun) -> Recovery | None: ...

    async def report_run(self, session_id: str, run_id: str) -> None: ...

    async def can_continue_session(self, session: DetachedSession) -> bool: ...

    async def delete_owned_conversation(self, session: DetachedSession) -> None: ...


@dataclass(frozen=True)
class RunEnd:
    """The result that the host reports when a run ends."""

    outcome: str
    final: str
    posted: list[dict[str, str]]
    error: str
    turn: dict[str, Any]
    ended_iso_utc: str


@dataclass(frozen=True)
class RunnerStats:
    """Counts for the host's status line."""

    enabled_jobs: int = 0
    total_jobs: int = 0
    running_runs: int = 0
    pending_runs: int = 0
    #: Expired sessions whose conversation deletion waits for a retry, and sessions this backend cannot delete.
    deleting_sessions: int = 0
    blocked_sessions: int = 0


@dataclass
class PlannedJob:
    """The revision and next due instant of one job."""

    revision: int
    next_due: datetime | None


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _latest(session: dict[str, Any]) -> dict[str, Any] | None:
    runs = session["runs"]
    return runs[-1] if runs else None


def _end_state(outcome: str, stop_requested: bool) -> str:
    if outcome in _SUCCEEDED:
        return "succeeded"
    if outcome == "interrupted":
        return "cancelled" if stop_requested else "interrupted"
    return "failed"


def cancel_pending(run: dict[str, Any], now: datetime, *, notify: bool) -> None:
    """Cancel a pending run. It owes its notice only with ``notify``.

    A person who cancels through a tool or a command reads the run in the answer, so the run owes no notice. An
    operator's action has no answer in the requester's conversation, so the run keeps its notice.
    """
    run.update(state="cancelled", outcome="interrupted", ended_iso_utc=_iso(now), prompt="")
    if not notify and run["report"]["state"] == "pending":
        run["report"]["state"] = "none"


@dataclass
class Ending:
    """The runs that a cancellation ended, in order, and those of them that must stop, as (session id, run id)."""

    ended: list[tuple[str, str]] = field(default_factory=list)
    stopping: list[tuple[str, str]] = field(default_factory=list)

    def end(self, session: dict[str, Any], now: datetime, *, notify: bool) -> None:
        """Cancel each pending run of a session, and mark each running one ``stop_requested``."""
        for run in session["runs"]:
            if run["state"] == "pending":
                cancel_pending(run, now, notify=notify)
            elif run["state"] == "running":
                run["stop_requested"] = True
                self.stopping.append((session["session_id"], run["run_id"]))
            else:
                continue
            self.ended.append((session["session_id"], run["run_id"]))

    @property
    def cancelled(self) -> list[tuple[str, str]]:
        return [ended for ended in self.ended if ended not in self.stopping]


def delete_job_records(snapshot: Snapshot, job_id: str, *, keep_history: bool, now: datetime, notify: bool) -> Ending:
    """Delete one job in a transaction, and return the runs that it ended or must stop.

    A pending run is cancelled. A running run gets ``stop_requested``, and its session keeps the job's id, so the
    run's end has a record to land in. The job leaves a stub while a session names it.
    """
    job = snapshot.jobs.pop(job_id)
    ending = Ending()
    for session in snapshot.job_sessions(job_id):
        ending.end(session, now, notify=notify)
    snapshot.deleted[job_id] = {
        "origin": job["origin"], "deliver_to": job["deliver_to"], "created_by_user_id": job["created_by_user_id"],
        "history_kept": keep_history, "unrun": job["unrun"],
        "dropped_through_iso_utc": job["dropped_through_iso_utc"], "backend": job["backend"],
    }
    return ending


class Runner:
    """Keep a per-job plan, claim due runs under the shared store lock, and run every session's runs."""

    def __init__(
        self,
        store: JobStore,
        settings: SchedulingSettings,
        starter: RunStarter,
        clock: Clock,
        recurrence: Recurrence,
        *,
        owner: str,
    ) -> None:
        self.store = store
        self.settings = settings
        self.starter = starter
        self.clock = clock
        self.recurrence = recurrence
        self.owner = owner
        self._plan: dict[str, PlannedJob] = {}
        #: The run ids that hold a run slot.
        self._active: set[str] = set()
        self._slots = asyncio.Semaphore(settings.max_concurrent_runs)
        self._gate = asyncio.Lock()
        self._wake = asyncio.Event()
        self._dirty = True
        self._task: asyncio.Task[None] | None = None
        self._stamp: tuple[int, int, int] | None = None
        self._checked_at: datetime | None = None
        self._heartbeat_at: datetime | None = None
        self._last_tick: datetime | None = None
        self._last_error_log: datetime | None = None
        self._stats = RunnerStats()
        self._blocked = False
        self._status: Component = self._unavailable_status("not_sampled")
        self._pending_work: list[WorkRow] = []
        self.expiry = Expiry(store, starter, clock, settings, owner)
        self._expired_at: datetime | None = None

    async def start(self) -> None:
        """Recover dead owners' runs, claim due runs, delete expired sessions, then monitor future due times."""
        if self._task is not None:
            return
        await self._recover_runs()
        await self._tick()
        await self._expire()
        self._task = asyncio.create_task(self._loop(), name="belle-scheduler")
        self._task.add_done_callback(_loop_ended)

    async def _expire(self) -> None:
        self._expired_at = self.clock.now()
        try:
            await self.expiry.tick()
        except (StoreUnreadable, StoreBusy) as exc:
            self._unreadable(exc)

    async def _recover_runs(self) -> None:
        """Take every run of a dead owner, at start and with each heartbeat.

        A pending run waits for a slot again. The host inspects each running run without sending its prompt again.
        Belle posts what a dead owner still owed for an ended run: its notice, or the final that recovery stored. An
        owner on another host stays live until its heartbeat is 90 s old, so its runs wait for a later pass.
        """
        now = self.clock.now()
        candidates: list[tuple[str, DetachedSession, SessionRun]] = []
        reports: list[tuple[str, str]] = []
        async with self.store.transaction() as snapshot:
            heartbeat(snapshot.owners, self.owner, now)
            for session in snapshot.sessions.values():
                for run in session["runs"]:
                    if owner_is_live(snapshot.owners, run["owner"], self.owner, now):
                        continue
                    if run["state"] in ENDED_STATES:
                        if owes_delivery(run):
                            reports.append((session["session_id"], run["run_id"]))
                        continue
                    if run["state"] == "running":
                        candidates.append((run["owner"], DetachedSession.from_json(deepcopy(session), "session"),
                                           SessionRun.from_json(deepcopy(run), "run")))
                    run["owner"] = self.owner
        self._heartbeat_at = now
        self._dirty = True
        for previous, recovering, started in candidates:
            try:
                recovered = await self.starter.recover_run(recovering, started)
            except Exception:
                logger.exception("could not inspect run %s of session %s after restart; the next pass tries again",
                                 started.run_id, recovering.session_id)
                await self._give_back(started.run_id, previous)
                continue
            try:
                await self._recovered(recovering, started, recovered)
            except Exception:  # One run's failure leaves the other runs to recover.
                logger.exception("could not end recovered run %s of session %s", started.run_id, recovering.session_id)
        for session_id, run_id in reports:
            await self._report(session_id, run_id)

    async def _recovered(self, session: DetachedSession, run: SessionRun, recovered: Recovery | None) -> None:
        """Store what the inspection of a recovered run found: no recovery, a prompt never sent, or its end."""
        if recovered is None:
            await self._end_and_report(session.session_id, run.run_id, RunEnd(
                "interrupted", "", [], "backend cannot recover a started turn; final outcome unknown",
                run.turn, _iso(self.clock.now()),
            ))
        elif recovered.state == RECOVERY_NOT_SUBMITTED:
            async with self.store.transaction() as snapshot:
                found = snapshot.find_run(run.run_id)
                if found is not None and found[1]["state"] == "running" and found[1]["owner"] == self.owner:
                    found[1].update(state="pending", started_iso_utc="")
        elif recovered.state != RECOVERY_RUNNING:
            terminal = recovered.terminal
            assert terminal is not None
            await self._end_and_report(session.session_id, run.run_id, RunEnd(
                RECOVERED_OUTCOMES[terminal.kind], terminal.text, [],
                terminal.text if terminal.kind == EVENT_FAILED else "", run.turn, _iso(self.clock.now()),
            ))

    async def _report(self, session_id: str, run_id: str) -> None:
        """Deliver what a run owes. A failed delivery is logged, and the other runs still get theirs."""
        try:
            await self.starter.report_run(session_id, run_id)
        except Exception:
            logger.exception("could not deliver what run %s of session %s owes", run_id, session_id)

    async def _give_back(self, run_id: str, owner: str) -> None:
        async with self.store.transaction() as snapshot:
            found = snapshot.find_run(run_id)
            if found is not None and found[1]["state"] == "running" and found[1]["owner"] == self.owner:
                found[1]["owner"] = owner

    async def _end_and_report(self, session_id: str, run_id: str, end: RunEnd) -> None:
        await self.ended_run(session_id, run_id, end)
        await self._report(session_id, run_id)

    async def stop(self) -> None:
        """Stop the clock loop. The host leaves the running turns."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def changed(self, job_ids: Collection[str]) -> None:
        """Wake after an in-process store write."""
        if job_ids:
            self._dirty = True
            self._wake.set()

    async def toggle_job(self, job_id: str, expected_state: str) -> None:
        """Toggle a local operator's selected job under the scheduler and store locks."""
        if not isinstance(job_id, str) or not job_id:
            raise Refused("invalid_arguments", "job_id must be a nonempty string")
        if expected_state not in ("active", "paused"):
            raise Refused("invalid_arguments", "expected_state must be active or paused")
        async with self._gate:
            async with self.store.transaction() as snapshot:
                job = snapshot.jobs.get(job_id)
                if job is None:
                    raise Refused("job_not_found", "job_id no longer names a scheduled job; refresh Jobs")
                if job["ended"]:
                    raise Refused("job_ended", "Ended jobs need a new schedule before resuming")
                state = "active" if job["enabled"] else "paused"
                if state != expected_state:
                    raise Refused("no_change", "Job state changed; refresh Jobs before retrying")
                job["enabled"] = not job["enabled"]
                job["updated_iso_utc"] = _iso(self.clock.now())
                # A local socket operator has no messaging-platform user identity.
                job["updated_by_user_id"] = ""
                if job["enabled"]:
                    job["disabled_reason"] = ""
            self._plan.pop(job_id, None)
            self._update_status(snapshot)
            self.changed([job_id])

    async def delete_job(self, job_id: str, expected_state: str) -> None:
        """Delete a same-UID operator's job without its history, and stop its running runs."""
        if not isinstance(job_id, str) or not job_id:
            raise Refused("invalid_arguments", "job_id must be a nonempty string")
        if expected_state not in ("active", "paused", "ended"):
            raise Refused("invalid_arguments", "expected_state must be active, paused, or ended")
        async with self._gate:
            async with self.store.transaction() as snapshot:
                job = snapshot.jobs.get(job_id)
                if job is None:
                    raise Refused("job_not_found", "job_id no longer names a scheduled job; refresh Jobs")
                state = "ended" if job["ended"] else "active" if job["enabled"] else "paused"
                if state != expected_state:
                    raise Refused("stale_selection", "Job state changed; refresh Jobs before retrying")
                ending = delete_job_records(snapshot, job_id, keep_history=False, now=self.clock.now(), notify=True)
            self._plan.pop(job_id, None)
            self._update_status(snapshot)
            self.changed([job_id])
        for session_id, run_id in ending.cancelled:
            await self._report(session_id, run_id)
        await self.stop_runs(ending.stopping)

    async def end_listed(self, ending: Ending) -> list[dict[str, Any]]:
        """Stop the runs that ``ending`` marked, and return one row for each run that it ended or stopped.

        A row shows the run's state after the stop. A run whose session expired meanwhile shows the state
        ``expired``: its record is gone.
        """
        sent = set(await self.stop_runs(ending.stopping))
        stopping = {run_id for _, run_id in ending.stopping}
        rows = {run_id: {"run_id": run_id, "state": "running" if run_id in stopping else "cancelled",
                         "stop_requested": run_id in stopping, "stop_request_sent": run_id in sent}
                for _, run_id in ending.ended}
        async with self.store.transaction() as snapshot:
            for run_id, row in rows.items():
                found = snapshot.find_run(run_id)
                if found is None:
                    row["state"] = "expired"
                    continue
                run = found[1]
                row.update(state=run["state"], stop_requested=run["stop_requested"])
        return list(rows.values())

    async def stop_runs(self, runs: list[tuple[str, str]]) -> list[str]:
        """Ask the host to stop runs already marked ``stop_requested``. Return the run ids it stopped.

        A running run whose stop was not sent loses its mark, also when a stop raises: its end is not a cancellation.
        """
        stopped = []
        try:
            for session_id, run_id in runs:
                async with self.store.transaction() as snapshot:
                    found = snapshot.find_run(run_id)
                    if found is None:
                        continue
                    stored = (DetachedSession.from_json(deepcopy(found[0]), "session"),
                              SessionRun.from_json(deepcopy(found[1]), "run"))
                if await self.starter.stop_run(*stored):
                    stopped.append(run_id)
        finally:
            await self.unmark_stop([run_id for _, run_id in runs if run_id not in stopped])
        return stopped

    async def unmark_stop(self, run_ids: list[str]) -> None:
        """Clear ``stop_requested`` on each of these runs that still runs here. Belle sent no stop for them.

        The mark of another process's run stays: that process may hold the run until its session's conversation is
        free, and then it reads the mark and ends the run instead of starting it.
        """
        if not run_ids:
            return
        async with self.store.transaction() as snapshot:
            for run_id in run_ids:
                found = snapshot.find_run(run_id)
                if found is not None and found[1]["state"] == "running" and found[1]["owner"] == self.owner:
                    found[1]["stop_requested"] = False

    async def drop_pending_run(self, run_id: str) -> None:
        """Cancel only the selected pending run, never a running one."""
        if not isinstance(run_id, str) or not run_id:
            raise Refused("invalid_arguments", "run_id must be a nonempty string")
        async with self._gate:
            async with self.store.transaction() as snapshot:
                found = snapshot.find_run(run_id)
                if found is None:
                    raise Refused("stale_selection", "Waiting work no longer exists; refresh Activity")
                if found[1]["state"] != "pending":
                    raise Refused("stale_selection", "Work state changed; refresh Activity before retrying")
                cancel_pending(found[1], self.clock.now(), notify=True)
                session_id = found[0]["session_id"]
            self._update_status(snapshot)
            self._dirty = True
            self._wake.set()
        await self._report(session_id, run_id)

    @staticmethod
    def _unavailable_status(reason: str) -> Component:
        return {"id": "scheduling", "state": "unavailable", "reason": reason,
                "observed_at": None, "metrics": {}}

    def status(self) -> Component:
        """Return detached cached metadata without reading or changing the store."""
        return deepcopy(self._status)

    def pending_work(self) -> list[WorkRow]:
        """Return the cached pending runs for local status."""
        return deepcopy(self._pending_work)

    def _update_status(self, snapshot: Snapshot) -> None:
        self._stats = self._counts(snapshot)
        self._pending_work = [
            {"id": run["run_id"], "state": "queued", "type": "main", "agent": None,
             "parent": None, "harness": session["backend"],
             "platform": run["place"]["platform"], "workspace": run["place"]["workspace"],
             "conversation": run["place"]["conversation"], "started_at": None,
             "model": None, "tokens_in": None, "tokens_out": None}
            for session in snapshot.sessions.values() for run in session["runs"] if run["state"] == "pending"
        ]
        rows: list[ScheduleRow] = []
        for job_id, job in snapshot.jobs.items():
            state = "ended" if job["ended"] else "active" if job["enabled"] else "paused"
            plan = self._plan.get(job_id)
            due = plan.next_due if plan is not None and state == "active" else None
            when = job["when"]
            schedule = str(when["kind"]) + " " + " ".join(
                f"{key}={value}" for key, value in when.items() if key != "kind"
            )
            rows.append({"job_id": job_id, "name": job["name"], "state": state,
                         "when": schedule.rstrip(), "next_due": _iso(due) if due is not None else None})
        self._status = {"id": "scheduling", "state": "ready", "reason": None,
                        "observed_at": _iso(self.clock.now()), "rows": rows,
                        "metrics": {name: {"value": value, "reason": None} for name, value in (
                            ("enabled_jobs", self._stats.enabled_jobs), ("total_jobs", self._stats.total_jobs),
                            ("running_runs", self._stats.running_runs), ("pending_runs", self._stats.pending_runs),
                            ("deleting_sessions", self._stats.deleting_sessions),
                            ("blocked_sessions", self._stats.blocked_sessions))}}

    @staticmethod
    def _job_runs(snapshot: Snapshot, job_id: str) -> list[dict[str, Any]]:
        return [run for session in snapshot.job_sessions(job_id) for run in session["runs"]]

    def _overlap_refusal(self, snapshot: Snapshot, job: dict[str, Any]) -> str:
        """Return why the job's overlap rule refuses another run now, or an empty text."""
        runs = self._job_runs(snapshot, job["job_id"])
        active = any(run["state"] == "running" for run in runs)
        waiting = sum(run["state"] == "pending" for run in runs)
        if job["overlap"] == "skip" and (active or waiting):
            return "the previous run of the job had not ended, and overlap is skip"
        if job["overlap"] == "queue" and waiting >= job["queue_max"] + (0 if active else 1):
            return f"the job already had {waiting} waiting runs, and queue_max is {job['queue_max']}"
        return ""

    def _add_job_run(self, snapshot: Snapshot, job: dict[str, Any], run: SessionRun, now: datetime) -> str:
        """Put a run of a job in its session: a new one for fresh, the job's own one for shared. Return the session id."""
        if job["session"] == "shared":
            session = snapshot.sessions[job["shared_session_id"]]
            session["runs"].append(run.to_json())
            return str(session["session_id"])
        session_id = new_session_id()
        while session_id in snapshot.sessions:
            session_id = new_session_id()
        snapshot.sessions[session_id] = scheduled_session(session_id, job, "fresh", _iso(now), [run]).to_json()
        return session_id

    def _job_run(self, job: dict[str, Any], *, place: dict[str, str], turn: dict[str, Any], **start: Any) -> SessionRun:
        return new_run(prompt=job["prompt"], turn=deepcopy(turn), questions=job["questions"], place=dict(place),
                       delivery="post_final", owner=self.owner, **start)

    async def manual(
        self,
        job_id: str,
        *,
        deliver_to: dict[str, str] | None = None,
        turn: dict[str, Any] | None = None,
        approved_readers: list[str] | str | None = None,
    ) -> tuple[DetachedSession, SessionRun]:
        """Store a requested run under the store lock, then start it if a slot is free."""
        async with self._gate:
            now = self.clock.now()
            async with self.store.transaction() as snapshot:
                job = snapshot.jobs.get(job_id)
                if job is None:
                    raise Refused("job_not_found", f"job_id {job_id!r} does not name a job")
                refusal = self._overlap_refusal(snapshot, job)
                if refusal:
                    code = "job_running" if job["overlap"] == "skip" else "job_queue_full"
                    raise Refused(code, f"job_id {job_id!r} cannot start a run now: {refusal}")
                self._check_pending_limit(snapshot)
                run = self._job_run(
                    job, started_by="request", place=deliver_to if deliver_to is not None else job["deliver_to"],
                    turn=turn if turn is not None else job["turn"],
                    request=RequestStart("", "", {}, _iso(now), approved_readers),
                )
                session_id = self._add_job_run(snapshot, job, run, now)
            starts = await self._dispatch(now)
            current = await self._current(session_id, run.run_id)
            self._dirty = True
            self._wake.set()
        await asyncio.gather(*(self._launch(*start) for start in starts))
        return current

    async def _current(self, session_id: str, run_id: str) -> tuple[DetachedSession, SessionRun]:
        async with self.store.transaction() as snapshot:
            session = DetachedSession.from_json(deepcopy(snapshot.sessions[session_id]), "session")
        return session, next(run for run in session.runs if run.run_id == run_id)

    async def request_session(self, session: DetachedSession) -> DetachedSession:
        """Store a detached session with one pending run before it can take a run slot.

        A repeated request with the same idempotency key answers with the stored session.
        """
        (first,) = session.runs
        assert first.request is not None
        key, wanted = first.request.idempotency_key, first.request.fingerprint
        async with self._gate:
            async with self.store.transaction() as snapshot:
                existing = next((stored for stored in snapshot.sessions.values()
                                 if stored["runs"] and (stored["runs"][0]["request"] or {}).get("idempotency_key") == key), None)
                if existing is None:
                    if session.session_id in snapshot.sessions:
                        raise Refused("invalid_arguments", "session_id already exists")
                    self._check_pending_limit(snapshot)
                    run = replace(first, owner=self.owner)
                    snapshot.sessions[session.session_id] = replace(session, runs=[run]).to_json()
                    session_id = session.session_id
                else:
                    if existing["runs"][0]["request"]["fingerprint"] != wanted:
                        raise Refused("invalid_arguments", "idempotency_key already names a different session request")
                    session_id = existing["session_id"]
            starts = await self._dispatch(self.clock.now())
            async with self.store.transaction() as snapshot:
                current = DetachedSession.from_json(deepcopy(snapshot.sessions[session_id]), "session")
            self._dirty = True
            self._wake.set()
        await asyncio.gather(*(self._launch(*start) for start in starts))
        return current

    def _check_pending_limit(self, snapshot: Snapshot) -> None:
        """Refuse a requested run while the requested runs that wait to start reach the limit.

        The operator sets ``max_pending_sessions`` as a resource limit. Runs that the clock starts do not count.
        """
        pending = sum(run["state"] == "pending" and run["started_by"] == "request"
                      for session in snapshot.sessions.values() for run in session["runs"])
        if pending >= self.settings.max_pending_sessions:
            raise Refused("session_limit_reached",
                          f"this deployment holds at most {self.settings.max_pending_sessions} "
                          "requested runs that wait to start")

    async def continue_session(
        self, session_id: str, prompt: str, turn: dict[str, Any], chat_type: str, key: str,
    ) -> tuple[DetachedSession, SessionRun]:
        """Append one pending run only if the retained conversation can serve it."""
        wanted = fingerprint(prompt, turn, chat_type)
        async with self.store.transaction() as snapshot:
            found = self._continuation(snapshot, session_id, key, wanted)
            backend_record = DetachedSession.from_json(deepcopy(snapshot.sessions[session_id]), "session")
        if found is not None:
            return await self._current(session_id, found)
        if not await self.starter.can_continue_session(backend_record):
            async with self.store.transaction() as snapshot:
                found = self._continuation(snapshot, session_id, key, wanted)
            if found is not None:
                return await self._current(session_id, found)
            raise Refused("backend_conversation_unavailable", "backend conversation is no longer retained")
        async with self._gate:
            now = self.clock.now()
            async with self.store.transaction() as snapshot:
                found = self._continuation(snapshot, session_id, key, wanted)
                if found is None:
                    self._check_pending_limit(snapshot)
                    session = snapshot.sessions[session_id]
                    first = session["runs"][0]
                    inbound = first["request"]["inbound"] if first["request"] else {}
                    run = new_run(
                        started_by="request", prompt=prompt, turn=turn, questions="refuse",
                        place=dict(session["origin"]), delivery="notice", owner=self.owner,
                        request=RequestStart(key, wanted, dict(inbound), _iso(now), None, continues=True),
                    )
                    session["origin_chat_type"] = chat_type
                    session["runs"].append(run.to_json())
                    found = run.run_id
            starts = await self._dispatch(now)
            current = await self._current(session_id, found)
            self._dirty = True
            self._wake.set()
        await asyncio.gather(*(self._launch(*start) for start in starts))
        return current

    def _continuation(self, snapshot: Snapshot, session_id: str, key: str, wanted: str) -> str | None:
        """Return the run id of an earlier continuation with this key, or None when a new run may start."""
        session = snapshot.sessions.get(session_id)
        if session is None:
            raise Refused("session_not_found", "session_id is no longer retained")
        previous = next((run for run in session["runs"] if (run["request"] or {}).get("idempotency_key") == key), None)
        if previous is not None:
            if previous["request"]["fingerprint"] != wanted:
                raise Refused("invalid_arguments", "the command ID already names a different continuation")
            return str(previous["run_id"])
        if any(run["state"] in {"pending", "running"} for run in session["runs"]):
            raise Refused("session_busy", "session_id has a pending or running run")
        if not session["runs"]:
            raise Refused("session_has_no_run", "session_id names the session of a shared job that has no run yet")
        if (session["deletion"]["state"] != "none"
                or expired(expires_iso_utc(session, snapshot, self.settings), self.clock.now())):
            raise Refused("session_expired", "the detached session has expired")
        return None

    async def cancel_session(self, session_id: str) -> dict[str, Any]:
        """Stop every pending and running run of a session. Keep the session and its job.

        The caller reads the cancelled runs in the answer, so they owe no notice.
        """
        ending = Ending()
        async with self._gate:
            async with self.store.transaction() as snapshot:
                session = snapshot.sessions.get(session_id)
                if session is None:
                    raise Refused("session_not_found", f"session_id {session_id!r} does not name a session")
                ending.end(session, self.clock.now(), notify=False)
            self._dirty = True
            self._wake.set()
        rows = await self.end_listed(ending)
        async with self.store.transaction() as snapshot:
            stored = snapshot.sessions.get(session_id)
            latest = _latest(stored) if stored is not None else None
        state = "expired" if stored is None else latest["state"] if latest is not None else None
        return {"session_id": session_id, "state": state, "runs": rows}

    async def ended_run(self, session_id: str, run_id: str, end: RunEnd) -> None:
        """Store the end of one run and free its run slot."""
        starts: list[tuple[Job | None, DetachedSession, SessionRun]] = []
        async with self._gate:
            try:
                async with self.store.transaction() as snapshot:
                    found = snapshot.find_run(run_id)
                    if found is None:
                        logger.error("scheduler run %s of session %s ended without a stored record", run_id, session_id)
                    else:
                        session, run = found
                        if run["state"] in {"pending", "running"}:
                            state = _end_state(end.outcome, run["stop_requested"])
                            if end.outcome == "duplicate":
                                logger.error("scheduler run %s had a duplicate backend turn", run_id)
                            run.update(
                                state=state, outcome=end.outcome, ended_iso_utc=end.ended_iso_utc, prompt="",
                                result=end.final, result_available=state == "succeeded", posted=end.posted,
                                error=end.error, turn=end.turn or run["turn"],
                            )
                            job = snapshot.jobs.get(session["job_id"])
                            if job is not None and run["clock"] is not None:
                                self._count_end(job, state, end.error, run["clock"]["due_iso_utc"])
                    self._update_status(snapshot)
            except StoreUnreadable as exc:
                self._unreadable(exc)
            if run_id in self._active:
                self._active.remove(run_id)
                self._slots.release()
            self._dirty = True
            self._wake.set()
            if not self._blocked:
                try:
                    starts = await self._dispatch(self.clock.now())
                except StoreUnreadable as exc:
                    self._unreadable(exc)
        await asyncio.gather(*(self._launch(*start) for start in starts))

    def _count_end(self, job: dict[str, Any], state: str, error: str, due: str) -> None:
        if state == "failed":
            job["consecutive_failures"] += 1
            limit = self.settings.disable_after_failures
            if limit and job["consecutive_failures"] >= limit:
                job["enabled"] = False
                job["disabled_reason"] = f"{limit} runs failed one after the other: {error}"
        elif state == "succeeded":
            job["consecutive_failures"] = 0
        if job["when"].get("kind") == "once" or self._next(job, _instant(due)) is None:
            job["ended"] = True

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            await self._tick()
            if self._expired_at is None or self.clock.now() - self._expired_at >= EXPIRY_INTERVAL:
                await self._expire()
            now = self.clock.now()
            deadlines = [now + _POLL]
            deadlines.extend(item.next_due for item in self._plan.values() if item.next_due is not None and item.next_due > now)
            if self._heartbeat_at is not None:
                deadlines.append(self._heartbeat_at + _HEARTBEAT)
            if self._expired_at is not None:
                deadlines.append(self._expired_at + EXPIRY_INTERVAL)
            deadline = min(deadlines)
            sleep = asyncio.create_task(self.clock.sleep_until(deadline))
            wake = asyncio.create_task(self._wake.wait())
            try:
                done, _ = await asyncio.wait({sleep, wake}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (sleep, wake):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sleep, wake, return_exceptions=True)
            for finished in done:
                finished.result()

    async def _tick(self, *, recover: bool = False) -> None:
        if recover or self._heartbeat_at is None or self.clock.now() - self._heartbeat_at >= _HEARTBEAT:
            try:
                await self._recover_runs()
            except (StoreUnreadable, StoreBusy) as exc:
                self._unreadable(exc)
        async with self._gate:
            now = self.clock.now()
            try:
                stamp = self._file_stamp()
                if self._dirty or stamp != self._stamp or self._checked_at is None or now - self._checked_at >= _POLL:
                    await self._refresh()
                    self._stamp = self._file_stamp()
                    self._checked_at = now
                    self._dirty = False
                self._blocked = False
                await self._claim(now)
                starts = await self._dispatch(now)
                self._last_tick = now
                self._stamp = self._file_stamp()
            except (StoreUnreadable, StoreBusy) as exc:
                self._unreadable(exc)
                starts = []
        await asyncio.gather(*(self._launch(*start) for start in starts))

    def _unreadable(self, exc: StoreUnreadable | StoreBusy) -> None:
        now = self.clock.now()
        if self._last_error_log is None or now - self._last_error_log >= _ERROR_LOG:
            logger.error("scheduler store unreadable: %s", exc)
            self._last_error_log = now
        self._blocked = True
        self._dirty = True
        self._stats = RunnerStats()
        self._status = self._unavailable_status("store_unreadable")
        self._pending_work = []

    def _file_stamp(self) -> tuple[int, int, int] | None:
        try:
            stat = self.store.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreUnreadable(self.store.path, str(exc)) from exc
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    async def _refresh(self) -> None:
        async with self.store.transaction() as snapshot:
            for job_id, job in snapshot.jobs.items():
                previous = self._plan.get(job_id)
                if previous is not None and previous.revision == job["revision"]:
                    continue
                after = job["watermark_iso_utc"] or job["created_iso_utc"]
                due = self._next(job, _instant(after)) if job["enabled"] and not job["ended"] else None
                self._plan[job_id] = PlannedJob(job["revision"], due)
            for job_id in self._plan.keys() - snapshot.jobs.keys():
                del self._plan[job_id]
            self._update_status(snapshot)

    def _next(self, job: dict[str, Any], after: datetime) -> datetime | None:
        next_due = self.recurrence.next_after(job["when"], after)
        if next_due is not None and (
            next_due.tzinfo is None or next_due.utcoffset() != timedelta(0) or next_due <= after
        ):
            raise ValueError("recurrence must return a UTC instant after the previous instant")
        return next_due

    async def _claim(self, now: datetime) -> None:
        due_jobs = sorted(
            ((plan.next_due, job_id, plan) for job_id, plan in self._plan.items()
             if plan.next_due is not None and plan.next_due <= now),
            key=lambda item: (item[0], item[1]),
        )
        if not due_jobs:
            return
        async with self.store.transaction() as snapshot:
            for first, job_id, plan in due_jobs:
                job = snapshot.jobs.get(job_id)
                if job is None:
                    self._plan.pop(job_id, None)
                    continue
                if job["revision"] != plan.revision or not job["enabled"] or job["ended"]:
                    self._plan[job_id] = PlannedJob(job["revision"], None)
                    self._dirty = True
                    continue
                instants = [first]
                while (next_due := self._next(job, instants[-1])) is not None and next_due <= now:
                    instants.append(next_due)
                cutoff = (
                    now + timedelta(microseconds=1)
                    if self._last_tick is None or now - self._last_tick > _POLL
                    else self._last_tick
                )
                overdue = [instant for instant in instants if instant < cutoff]
                current = [instant for instant in instants if instant >= cutoff]
                selected: list[tuple[datetime, int]] = []
                if overdue:
                    catch_up = job["catch_up"]
                    if catch_up == "all":
                        selected.extend((instant, 0) for instant in overdue)
                    elif catch_up == "latest":
                        selected.append((overdue[-1], len(overdue) - 1))
                        self._missed(job, overdue[:-1])
                    else:
                        self._missed(job, overdue)
                selected.extend((instant, 0) for instant in current)
                for due, missed in selected:
                    self._claim_one(snapshot, job, due, missed, now)
                job["watermark_iso_utc"] = _iso(instants[-1])
                self._plan[job_id] = PlannedJob(plan.revision + 1, self._next(job, instants[-1]))
            self._update_status(snapshot)

    @staticmethod
    def _claim_key(job: dict[str, Any], due: datetime) -> str:
        return f"{job['job_id']}:{int(due.timestamp())}"

    def _missed(self, job: dict[str, Any], times: list[datetime]) -> None:
        if not times:
            return
        reason = (f"{len(times)} due times from {_iso(times[0])} to {_iso(times[-1])} passed while no scheduler ran, "
                  f"and catch_up is {job['catch_up']}")
        job["unrun"].append(UnrunDue(_iso(times[0]), "missed", reason, self._claim_key(job, times[0])).to_json())

    def _claim_one(self, snapshot: Snapshot, job: dict[str, Any], due: datetime, missed: int, now: datetime) -> None:
        key = self._claim_key(job, due)
        if any(unrun["claim_key"] == key for unrun in job["unrun"]) or any(
            (run["clock"] or {}).get("claim_key") == key for run in self._job_runs(snapshot, job["job_id"])
        ):
            return
        refusal = self._overlap_refusal(snapshot, job)
        if refusal:
            job["unrun"].append(UnrunDue(_iso(due), "skipped", refusal, key).to_json())
            return
        run = self._job_run(job, started_by="clock", place=job["deliver_to"], turn=job["turn"],
                            clock=ClockStart(_iso(due), 0, missed, key))
        self._add_job_run(snapshot, job, run, now)

    async def _dispatch(self, now: datetime) -> list[tuple[Job | None, DetachedSession, SessionRun]]:
        starts: list[tuple[Job | None, DetachedSession, SessionRun]] = []
        async with self.store.transaction() as snapshot:
            running = sum(run["state"] == "running" for session in snapshot.sessions.values() for run in session["runs"])
            pending = sorted(
                (
                    (_instant(SessionRun.from_json(run, "run").queued_iso_utc), session["job_id"], session["session_id"],
                     run["run_id"]), session, run,
                )
                for session in snapshot.sessions.values() for run in session["runs"]
                if run["state"] == "pending" and run["owner"] == self.owner
            )
            for _, session, run in pending:
                if running >= self.settings.max_concurrent_runs or len(self._active) + len(starts) >= self.settings.max_concurrent_runs:
                    break
                job = snapshot.jobs.get(session["job_id"])
                if job is not None and job["overlap"] == "queue" and any(
                    other["state"] == "running" for other in self._job_runs(snapshot, job["job_id"])
                ):
                    continue
                run["state"] = "running"
                run["started_iso_utc"] = _iso(now)
                if run["clock"] is not None:
                    run["clock"]["late_seconds"] = max(0, int((now - _instant(run["clock"]["due_iso_utc"])).total_seconds()))
                    if job is not None:
                        job["runs_started"] += 1
                        maximum = job["when"].get("max_runs")
                        if maximum is not None and job["runs_started"] >= maximum:
                            job["ended"] = True
                running += 1
                starts.append((
                    None if job is None else Job.from_json(deepcopy(job), "job"),
                    DetachedSession.from_json(deepcopy(session), "session"),
                    SessionRun.from_json(deepcopy(run), "run"),
                ))
            self._update_status(snapshot)
        if starts:
            self._dirty = True
        for _, _, run in starts:
            await self._slots.acquire()
            self._active.add(run.run_id)
        return starts

    async def _launch(self, job: Job | None, session: DetachedSession, run: SessionRun) -> None:
        try:
            await self.starter.start_run(job, session, run)
        except Refused as exc:
            await self._end_and_report(session.session_id, run.run_id, RunEnd(
                "failed", "", [], f"{exc.code}: {exc.detail}", run.turn, _iso(self.clock.now())
            ))
        except Exception as exc:  # noqa: BLE001 - a failed host start ends this run
            await self._end_and_report(session.session_id, run.run_id, RunEnd(
                "failed", "", [], str(exc), run.turn, _iso(self.clock.now())
            ))

    def _counts(self, snapshot: Snapshot) -> RunnerStats:
        runs = [run for session in snapshot.sessions.values() for run in session["runs"]]
        return RunnerStats(
            enabled_jobs=sum(job["enabled"] for job in snapshot.jobs.values()),
            total_jobs=len(snapshot.jobs),
            running_runs=sum(run["state"] == "running" for run in runs),
            pending_runs=sum(run["state"] == "pending" for run in runs),
            deleting_sessions=sum(session["deletion"]["state"] == "deleting" for session in snapshot.sessions.values()),
            blocked_sessions=sum(session["deletion"]["state"] == "blocked" for session in snapshot.sessions.values()),
        )


def _loop_ended(task: asyncio.Task[None]) -> None:
    """Log a scheduler loop that ended with an error: no job runs and no session expires until Belle restarts."""
    if not task.cancelled() and task.exception() is not None:
        logger.error("the scheduler loop stopped: no job runs and no session expires until Belle restarts",
                     exc_info=task.exception())


def scheduled_session(session_id: str, job: dict[str, Any], mode: str, now: str, runs: list[SessionRun]) -> DetachedSession:
    """Make a scheduled session of a job. It takes the job's origin and creator."""
    return DetachedSession(
        session_id=session_id, kind="scheduled", job_id=job["job_id"], job_mode=mode, backend=job["backend"],
        origin=dict(job["origin"]), origin_chat_type=job["origin_chat_type"],
        requested_by_user_id=job["created_by_user_id"], created_iso_utc=now, deletion=NO_DELETION, runs=runs,
    )
