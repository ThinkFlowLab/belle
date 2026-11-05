"""Every session expires by one rule: the harness conversation goes first, then the record."""

import asyncio
import multiprocessing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from belle_agent.backend import OP_FORGET, BackendCapabilities
from belle_agent.host.expiry import delete_owned_conversation
from belle_agent.scheduling.expiry import Blocked, Expiry, expires_iso_utc
from belle_agent.scheduling.records import NO_DELETION, ClockStart, DetachedSession, SessionRun, new_run
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.runner import RunEnd, Runner, delete_job_records
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore

from .fake_clock import FakeClock
from .records_kit import BASE, PLACE, detached, job, put, sessions
from .test_runner import Recurrence, Starter

HOUR = 3600
ENDED = BASE.isoformat()


class Harness(Starter):
    """A starter that deletes conversations, or fails to."""

    def __init__(self, store: JobStore | None = None) -> None:
        super().__init__()
        self.forgotten: list[str] = []
        self.failures: list[Exception] = []
        self.store = store

    async def delete_owned_conversation(self, session: DetachedSession) -> None:
        if self.store is not None:
            assert not self.store.lock.locked(), "a backend call ran inside a store transaction"
        self.forgotten.append(session.owned_conversation)
        if self.failures:
            raise self.failures.pop(0)


def worker(path: Path, harness: Harness, **settings: Any) -> Runner:
    clock = FakeClock(BASE)
    store = JobStore(path, clock=clock, kept_runs_per_job=settings.get("kept_runs_per_job", 20))
    harness.store = store
    return Runner(store, SchedulingSettings(**settings), harness, clock, Recurrence(), owner="one")


def ended(session: DetachedSession, **run: Any) -> DetachedSession:
    """End every run of a session, its notice sent."""
    values = {"state": "succeeded", "outcome": "posted-final", "ended_iso_utc": ENDED, **run}
    return replace(session, runs=[replace(item, report=replace(item.report, state="sent"), **{
        key: value for key, value in values.items()}) if "report" not in values else replace(item, **values)
        for item in session.runs])


def clock_run(number: int, **fields: Any) -> SessionRun:
    run = new_run(started_by="clock", prompt="", turn={}, questions="refuse", place=PLACE.copy(),
                  delivery="post_final", owner="one", clock=ClockStart(ENDED, 0, 0, f"job-0000000001:{number}"))
    return replace(run, **{"state": "succeeded", "outcome": "posted-final", "ended_iso_utc": ENDED, **fields})


def scheduled(number: int, mode: str, runs: list[SessionRun], job_id: str = "job-0000000001") -> DetachedSession:
    return DetachedSession(f"ses-sched{number:05d}", "scheduled", job_id, mode, "opencode", PLACE.copy(), "dm", "user",
                   ENDED, NO_DELETION, runs)


async def store_sessions(runner: Runner, *items: DetachedSession) -> None:
    async with runner.store.transaction() as snapshot:
        for item in items:
            snapshot.sessions[item.session_id] = item.to_json()


async def deleted_stub(runner: Runner, session: DetachedSession, *, history: bool) -> None:
    """Store a session of a deleted job, with the job's stub."""
    async with runner.store.transaction() as snapshot:
        snapshot.deleted["job-0000000001"] = {"origin": PLACE.copy(), "deliver_to": PLACE.copy(),
                                              "created_by_user_id": "user", "history_kept": history, "unrun": [],
                                              "dropped_through_iso_utc": "", "backend": "opencode"}
        snapshot.sessions[session.session_id] = session.to_json()


@pytest.mark.parametrize("kind", ["standalone", "fresh", "shared"])
async def test_every_session_kind_expires_by_one_rule(tmp_path: Path, kind: str) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    if kind == "standalone":
        session = ended(detached(1))
        await store_sessions(runner, session)
    else:
        session = scheduled(1, kind, [clock_run(1)])
        await deleted_stub(runner, session, history=True)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR - 1)
    await runner.expiry.tick()
    assert harness.forgotten == [] and session.session_id in await sessions(runner.store)
    runner.clock.advance(1)
    await runner.expiry.tick()
    assert harness.forgotten == [session.owned_conversation]
    assert await sessions(runner.store) == {}


async def test_a_run_an_owed_notice_or_the_shared_job_holds_a_session(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    shared = replace(job(1), session="shared", shared_session_id="ses-sched00001")
    await put(runner.store, shared)
    pending = replace(detached(2), runs=[replace(detached(2).runs[0], state="pending")])
    owed = ended(detached(3), report=replace(detached(3).runs[0].report, state="pending"))
    running = replace(detached(4), runs=[replace(detached(4).runs[0], state="running", started_iso_utc=ENDED)])
    uncertain = ended(detached(5), report=replace(detached(5).runs[0].report, state="uncertain"))
    await store_sessions(runner, pending, owed, running, uncertain)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(1000 * HOUR)
    await runner.expiry.tick()
    assert harness.forgotten == [uncertain.owned_conversation]
    assert set(await sessions(runner.store)) == {"ses-sched00001", pending.session_id, owed.session_id, running.session_id}


async def test_a_recovered_final_that_waits_for_its_post_holds_its_session(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    final = {"delivery": "post_final", "outcome": "stored-final", "report": replace(detached().runs[0].report, state="none")}
    unposted = ended(detached(1), **final)
    refused = ended(detached(2), **final, error="platform gone is not served")
    await store_sessions(runner, unposted, refused)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(1000 * HOUR)
    await runner.expiry.tick()
    assert harness.forgotten == [refused.owned_conversation]
    assert set(await sessions(runner.store)) == {unposted.session_id}


async def test_fresh_count_bound_expires_oldest(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    await put(runner.store, job(1))
    items = [scheduled(number, "fresh", [clock_run(number, ended_iso_utc=(BASE + timedelta(seconds=number)).isoformat())])
             for number in range(21)]
    await store_sessions(runner, *items)
    await runner.expiry.tick()
    assert harness.forgotten == [items[0].owned_conversation]
    assert len(await sessions(runner.store)) == 20


async def test_shared_run_bound_keeps_the_conversation(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    runs = [clock_run(number, ended_iso_utc=(BASE + timedelta(seconds=number)).isoformat()) for number in range(21)]
    await put(runner.store, replace(job(1), session="shared", shared_session_id="ses-sched00001"))
    async with runner.store.transaction() as snapshot:
        snapshot.sessions["ses-sched00001"]["runs"] = [run.to_json() for run in runs]
    await runner.expiry.tick()
    stored = (await sessions(runner.store))["ses-sched00001"]
    assert [run["run_id"] for run in stored["runs"]] == [run.run_id for run in runs[1:]]
    assert harness.forgotten == []


async def test_delete_job_without_history_expires_its_sessions(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    await put(runner.store, replace(job(1), session="shared", shared_session_id="ses-sched00001"))
    fresh = scheduled(2, "fresh", [clock_run(2)])
    await store_sessions(runner, fresh)
    async with runner.store.transaction() as snapshot:
        delete_job_records(snapshot, "job-0000000001", keep_history=False, now=runner.clock.now(), notify=True)
    await runner.expiry.tick()
    assert sorted(harness.forgotten) == sorted([fresh.owned_conversation, "belle_session_ses-sched00001"])
    async with runner.store.transaction() as snapshot:
        assert snapshot.sessions == {} and snapshot.deleted == {}


async def test_failed_forget_keeps_the_record(tmp_path: Path) -> None:
    harness = Harness()
    harness.failures.append(RuntimeError("the harness answered 503"))
    runner = worker(tmp_path, harness)
    session = ended(detached(1))
    await store_sessions(runner, session)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR)
    await runner.expiry.tick()
    deletion = (await sessions(runner.store))[session.session_id]["deletion"]
    assert deletion == {"state": "deleting", "claimed_by": "", "attempts": 1,
                        "last_error": "RuntimeError: the harness answered 503",
                        "last_attempt_iso_utc": "2026-10-21T00:00:00Z"}
    await runner.expiry.tick()
    assert harness.forgotten == [session.owned_conversation] * 2
    assert await sessions(runner.store) == {}


async def test_crash_after_forget_is_safe(tmp_path: Path) -> None:
    class Crashing(Harness):
        """The process cannot write the store after the harness deleted the conversation, as in a crash."""

        async def delete_owned_conversation(self, session: DetachedSession) -> None:
            await super().delete_owned_conversation(session)
            if len(self.forgotten) == 1:
                tmp_path.chmod(0o500)

    harness = Crashing()
    runner = worker(tmp_path, harness)
    session = ended(detached(1))
    await store_sessions(runner, session)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR)
    try:
        with pytest.raises(OSError):
            await runner.expiry.tick()
    finally:
        tmp_path.chmod(0o700)
    await runner.expiry.tick()
    assert harness.forgotten == [session.owned_conversation] * 2
    assert await sessions(runner.store) == {}


async def test_deleting_session_refuses_continue(tmp_path: Path) -> None:
    runner = worker(tmp_path, Harness())
    session = replace(ended(detached(1)), deletion=replace(NO_DELETION, state="deleting"))
    await store_sessions(runner, session)
    with pytest.raises(Refused) as refused:
        await runner.continue_session(session.session_id, "next", {}, "dm", "continue:one")
    assert refused.value.code == "session_expired"


async def test_other_backend_is_blocked(tmp_path: Path) -> None:
    class Forgetting:
        async def forget(self, conversation: str) -> None:
            raise AssertionError("a blocked session must not reach the backend")

    session = ended(detached(1))
    other = BackendCapabilities("docker_agent", operations=frozenset({OP_FORGET}))
    with pytest.raises(Blocked, match="on backend opencode, and Belle runs on docker_agent"):
        await delete_owned_conversation(Forgetting(), other, session)  # type: ignore[arg-type]
    with pytest.raises(Blocked, match="cannot delete a conversation"):
        await delete_owned_conversation(Forgetting(), BackendCapabilities("opencode"), session)  # type: ignore[arg-type]

    harness = Harness()
    harness.failures.append(Blocked("backend opencode cannot delete a conversation"))
    runner = worker(tmp_path, harness)
    await store_sessions(runner, session)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR)
    await runner.expiry.tick()
    stored = (await sessions(runner.store))[session.session_id]
    assert stored["deletion"]["state"] == "blocked"
    assert stored["deletion"]["last_error"] == "backend opencode cannot delete a conversation"
    await runner._tick()
    assert runner.status()["metrics"]["blocked_sessions"]["value"] == 1


async def test_a_blocked_session_is_claimed_and_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    harness = Harness()
    harness.failures.append(Blocked("backend opencode cannot delete a conversation"))
    runner = worker(tmp_path, harness)
    session = ended(detached(1))
    await store_sessions(runner, session)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR)
    with caplog.at_level("WARNING", logger="belle_agent.scheduling.expiry"):
        await runner.expiry.tick()
        await runner.expiry.tick()
        harness.failures.append(Blocked("backend opencode cannot delete a conversation"))
        restarted = Expiry(runner.store, harness, runner.clock, runner.settings, "two")
        await restarted.tick()
    assert harness.forgotten == [session.owned_conversation] * 2
    assert (await sessions(runner.store))[session.session_id]["deletion"]["attempts"] == 2
    assert caplog.text.count("stays in the store") == 1


async def test_one_expiry_function(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    session = ended(detached(1))
    await store_sessions(runner, session)
    async with runner.store.transaction() as snapshot:
        assert expires_iso_utc(snapshot.sessions[session.session_id], snapshot, runner.settings) == "2026-10-21T00:00:00Z"
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR - 1)
    async with runner.store.transaction() as snapshot:
        assert runner._continuation(snapshot, session.session_id, "continue:one", "x") is None
    await runner.expiry.tick()
    assert harness.forgotten == []
    runner.clock.advance(1)
    with pytest.raises(Refused) as refused:
        async with runner.store.transaction() as snapshot:
            runner._continuation(snapshot, session.session_id, "continue:one", "x")
    assert refused.value.code == "session_expired"
    await runner.expiry.tick()
    assert harness.forgotten == [session.owned_conversation]


def _pass_process(state_dir: str, name: str, log: str) -> None:
    class Logging(Harness):
        async def delete_owned_conversation(self, session: DetachedSession) -> None:
            with open(log, "a", encoding="utf-8") as handle:  # noqa: ASYNC230 - a log line of a test process
                handle.write(session.session_id + "\n")
            await asyncio.sleep(0.05)

    async def run() -> None:
        clock = FakeClock(BASE + timedelta(hours=73))
        store = JobStore(Path(state_dir), clock=clock)
        await Expiry(store, Logging(), clock, SchedulingSettings(), name).tick()

    asyncio.run(run())


async def test_concurrent_passes_delete_once(tmp_path: Path) -> None:
    runner = worker(tmp_path, Harness())
    await store_sessions(runner, *(ended(detached(number)) for number in range(10)))
    log = tmp_path / "forgotten.log"
    processes = [multiprocessing.Process(target=_pass_process, args=(str(tmp_path), name, str(log)))
                 for name in ("one", "two")]
    for process in processes:
        process.start()
    for process in processes:
        await asyncio.to_thread(process.join, 20)
        assert process.exitcode == 0
    lines = log.read_text(encoding="utf-8").split()
    assert sorted(lines) == sorted(f"ses-{number:010d}" for number in range(10))
    assert await sessions(runner.store) == {}


async def test_dead_claim_lapses(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    session = replace(ended(detached(1)), deletion=replace(NO_DELETION, state="deleting", claimed_by="dead"))
    await store_sessions(runner, session)
    async with runner.store.transaction() as snapshot:
        snapshot.owners["dead"] = {"pid": 1, "host": "elsewhere", "seen_iso_utc": "2026-10-21T00:00:00Z"}
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR + 89)
    await runner.expiry.tick()
    assert harness.forgotten == []
    runner.clock.advance(2)
    await runner.expiry.tick()
    assert harness.forgotten == [session.owned_conversation]


async def test_deleted_job_running_run_is_recorded(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    await put(runner.store, job(1))
    session, run = await runner.manual("job-0000000001")
    async with runner.store.transaction() as snapshot:
        delete_job_records(snapshot, "job-0000000001", keep_history=False, now=runner.clock.now(), notify=True)
    await runner.expiry.tick()
    assert harness.forgotten == []
    await runner.ended_run(session.session_id, run.run_id, RunEnd("interrupted", "", [], "", {}, ENDED))
    assert (await sessions(runner.store))[session.session_id]["runs"][0]["state"] == "cancelled"
    await runner.expiry.tick()
    assert harness.forgotten == [session.owned_conversation]
    assert await sessions(runner.store) == {}


async def test_start_runs_a_deletion_pass(tmp_path: Path) -> None:
    harness = Harness()
    runner = worker(tmp_path, harness)
    session = ended(detached(1))
    await store_sessions(runner, session)
    assert isinstance(runner.clock, FakeClock)
    runner.clock.advance(72 * HOUR)
    await runner.start()
    try:
        assert harness.forgotten == [session.owned_conversation]
    finally:
        await runner.stop()
