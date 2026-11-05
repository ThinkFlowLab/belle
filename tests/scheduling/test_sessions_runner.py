"""Requested sessions share the run budget of scheduled runs."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from belle_agent.backend import EVENT_SUCCEEDED, RECOVERY_RUNNING, RECOVERY_TERMINAL, Recovery, TurnEvent
from belle_agent.scheduling.records import DetachedSession, Job, SessionRun, fingerprint
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.runner import RunEnd, Runner
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore

from .fake_clock import FakeClock
from .records_kit import detached, job, put

NOW = datetime(2026, 10, 18, tzinfo=UTC)


class NoRecurrence:
    def next_after(self, when: dict[str, Any], after: datetime) -> None:
        return None


class Starter:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.stopped_runs: list[str] = []
        self.recovered: Recovery | None = None
        self.recovering: list[str] = []
        self.reported: list[str] = []
        self.stop_succeeds = True
        self.retained = True

    async def start_run(self, job: Job | None, session: DetachedSession, run: SessionRun) -> None:
        self.started.append(session.session_id)

    async def stop_run(self, session: DetachedSession, run: SessionRun) -> bool:
        self.stopped.append(session.session_id)
        self.stopped_runs.append(run.run_id)
        return self.stop_succeeds

    async def recover_run(self, session: DetachedSession, run: SessionRun) -> Recovery | None:
        self.recovering.append(run.run_id)
        return self.recovered

    async def report_run(self, session_id: str, run_id: str) -> None:
        self.reported.append(session_id)

    async def can_continue_session(self, session: DetachedSession) -> bool:
        return self.retained

    async def delete_owned_conversation(self, session: DetachedSession) -> None:
        return None


def requested(number: int) -> DetachedSession:
    return detached(number, notify=False, key=f"trusted-{number}")


def runner(path: Path, starter: Starter) -> Runner:
    clock = FakeClock(NOW)
    return Runner(JobStore(path, clock=clock), SchedulingSettings(max_concurrent_runs=1),
                  starter, clock, NoRecurrence(), owner="one")


def done(final: str = "done", outcome: str = "stored-final") -> RunEnd:
    return RunEnd(outcome, final, [], "", {}, NOW.isoformat())


async def stored(worker: Runner, session_id: str) -> dict[str, Any]:
    async with worker.store.transaction() as snapshot:
        return snapshot.sessions[session_id]


async def end_latest(worker: Runner, session_id: str, end: RunEnd) -> None:
    await worker.ended_run(session_id, (await stored(worker, session_id))["runs"][-1]["run_id"], end)


async def test_request_is_durable_idempotent_and_uses_one_slot(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    first = await worker.request_session(requested(1))
    duplicate = await worker.request_session(requested(1))
    second = await worker.request_session(requested(2))
    assert first.session_id == duplicate.session_id
    assert first.runs[0].state == "running"
    assert second.runs[0].state == "pending"
    assert [row["id"] for row in worker.pending_work()] == [second.runs[-1].run_id]
    assert starter.started == ["ses-0000000001"]
    await end_latest(worker, "ses-0000000001", done(outcome="posted-final"))
    assert starter.started == ["ses-0000000001", "ses-0000000002"]
    assert (await worker.request_session(requested(1))).session_id == first.session_id
    ended = (await stored(worker, "ses-0000000001"))["runs"][0]
    assert ended["prompt"] == "" and ended["result"] == "done"


async def test_every_requested_run_refuses_at_the_pending_limit(tmp_path: Path) -> None:
    worker = runner(tmp_path, Starter())
    worker.settings = replace(worker.settings, max_pending_sessions=1)
    await put(worker.store, job(1, overlap="parallel"))
    await worker.request_session(requested(1))
    await worker.request_session(requested(2))
    for request in (lambda: worker.request_session(requested(3)), lambda: worker.manual(job(1).job_id)):
        with pytest.raises(Refused) as error:
            await request()
        assert error.value.code == "session_limit_reached"
        assert "at most 1 requested runs that wait to start" in error.value.detail
        assert "scheduling." not in error.value.detail


async def test_reused_request_identity_cannot_change_the_task(tmp_path: Path) -> None:
    worker = runner(tmp_path, Starter())
    await worker.request_session(requested(1))
    original = requested(1)
    run = original.runs[0]
    assert run.request is not None
    altered = replace(original, runs=[replace(run, prompt="another task", request=replace(
        run.request, fingerprint=fingerprint("another task")))])
    with pytest.raises(Refused, match="idempotency_key already names a different session request"):
        await worker.request_session(altered)


async def test_cancel_pending_never_starts_it(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    await worker.request_session(requested(1))
    await worker.request_session(requested(2))
    cancelled = await worker.cancel_session("ses-0000000002")
    assert cancelled["state"] == "cancelled"
    await end_latest(worker, "ses-0000000001", done())
    assert starter.started == ["ses-0000000001"]


async def test_cancel_running_requests_backend_stop(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    await worker.request_session(requested(1))
    await worker.cancel_session("ses-0000000001")
    assert starter.stopped == ["ses-0000000001"]
    await end_latest(worker, "ses-0000000001", done("", "interrupted"))
    assert (await stored(worker, "ses-0000000001"))["runs"][0]["state"] == "cancelled"


async def test_unconfirmed_stop_does_not_claim_cancellation(tmp_path: Path) -> None:
    starter = Starter()
    starter.stop_succeeds = False
    worker = runner(tmp_path, starter)
    await worker.request_session(requested(1))
    response = await worker.cancel_session("ses-0000000001")
    (row,) = response["runs"]
    assert row["stop_request_sent"] is False and row["stop_requested"] is False
    await end_latest(worker, "ses-0000000001", done("", "interrupted"))
    assert (await stored(worker, "ses-0000000001"))["runs"][0]["state"] == "interrupted"


async def test_a_stop_that_raises_does_not_claim_cancellation(tmp_path: Path) -> None:
    starter = Starter()

    async def stop_run(session: DetachedSession, run: SessionRun) -> bool:
        raise RuntimeError("the backend refused the stop")

    starter.stop_run = stop_run  # type: ignore[method-assign]
    worker = runner(tmp_path, starter)
    await worker.request_session(requested(1))
    with pytest.raises(RuntimeError):
        await worker.cancel_session("ses-0000000001")
    assert (await stored(worker, "ses-0000000001"))["runs"][0]["stop_requested"] is False
    await end_latest(worker, "ses-0000000001", done("", "interrupted"))
    assert (await stored(worker, "ses-0000000001"))["runs"][0]["state"] == "interrupted"


async def test_continuation_keeps_prior_result_and_uses_same_conversation(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    record = requested(1)
    await worker.request_session(record)
    await end_latest(worker, record.session_id, done("first"))
    continued, run = await worker.continue_session(record.session_id, "next", {}, "im", "continue:event-two")
    first, second = continued.runs
    assert first.result == "first" and first.run_id != second.run_id == run.run_id
    assert continued.owned_conversation == record.owned_conversation
    assert second.delivery == "notice" and second.report.state == "pending"
    assert second.prompt == "next" and second.state == "running" and second.started_by == "request"
    assert starter.started == [record.session_id, record.session_id]
    await end_latest(worker, record.session_id, done("second"))
    saved = await stored(worker, record.session_id)
    assert [item["result"] for item in saved["runs"]] == ["first", "second"]
    assert saved["runs"][-1]["prompt"] == ""


async def test_continuation_refuses_busy_expired_or_missing_backend(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    record = requested(1)
    await worker.request_session(record)
    with pytest.raises(Refused, match="pending or running"):
        await worker.continue_session(record.session_id, "next", {}, "im", "continue:two")
    await end_latest(worker, record.session_id, done("first"))
    starter.retained = False
    with pytest.raises(Refused, match="no longer retained"):
        await worker.continue_session(record.session_id, "next", {}, "im", "continue:two")
    starter.retained = True
    assert isinstance(worker.clock, FakeClock)
    worker.clock.advance(72 * 3600)
    with pytest.raises(Refused, match="detached session has expired"):
        await worker.continue_session(record.session_id, "next", {}, "im", "continue:two")


async def test_continuation_retry_is_idempotent(tmp_path: Path) -> None:
    worker = runner(tmp_path, Starter())
    record = requested(1)
    await worker.request_session(record)
    await end_latest(worker, record.session_id, done("first"))
    _, first = await worker.continue_session(record.session_id, "next", {}, "im", "continue:event-two")
    second_session, second = await worker.continue_session(record.session_id, "next", {}, "im", "continue:event-two")
    assert first.run_id == second.run_id
    assert len(second_session.runs) == 2
    with pytest.raises(Refused, match="different continuation"):
        await worker.continue_session(record.session_id, "changed", {}, "im", "continue:event-two")


async def test_concurrent_continuations_release_store_lock_during_backend_check(tmp_path: Path) -> None:
    class CheckingStarter(Starter):
        def __init__(self) -> None:
            super().__init__()
            self.checks = 0
            self.both_checking = asyncio.Event()
            self.release = asyncio.Event()

        async def can_continue_session(self, session: DetachedSession) -> bool:
            self.checks += 1
            if self.checks == 2:
                self.both_checking.set()
            await self.release.wait()
            return True

    starter = CheckingStarter()
    first = runner(tmp_path, starter)
    second = runner(tmp_path, starter)
    record = requested(1)
    await first.request_session(record)
    await end_latest(first, record.session_id, done("first"))
    attempts = [asyncio.create_task(worker.continue_session(
        record.session_id, "next", {}, "im", f"continue:{number}"
    )) for number, worker in enumerate((first, second))]
    try:
        await asyncio.wait_for(starter.both_checking.wait(), timeout=5)
    finally:
        starter.release.set()
    results = await asyncio.gather(*attempts, return_exceptions=True)
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert sum(isinstance(result, Refused) and result.code == "session_busy" for result in results) == 1
    assert len((await stored(first, record.session_id))["runs"]) == 2


async def test_backend_check_does_not_block_other_scheduling_calls(tmp_path: Path) -> None:
    class CheckingStarter(Starter):
        def __init__(self) -> None:
            super().__init__()
            self.checking = asyncio.Event()
            self.release = asyncio.Event()

        async def can_continue_session(self, session: DetachedSession) -> bool:
            self.checking.set()
            await self.release.wait()
            return True

    starter = CheckingStarter()
    worker = runner(tmp_path, starter)
    record = requested(1)
    await worker.request_session(record)
    await end_latest(worker, record.session_id, done("first"))
    continuation = asyncio.create_task(worker.continue_session(record.session_id, "next", {}, "im", "continue:next"))
    try:
        await asyncio.wait_for(starter.checking.wait(), timeout=5)
        other = await asyncio.wait_for(worker.request_session(requested(2)), timeout=5)
        assert other.session_id == "ses-0000000002"
    finally:
        starter.release.set()
    assert (await continuation)[1].state == "pending"


async def _stale(worker: Runner, *records: DetachedSession, running: bool = True) -> None:
    async with worker.store.transaction() as snapshot:
        for index, record in enumerate(records):
            run = replace(record.runs[0], owner="old", state="running" if running and index == 0 else "pending",
                          started_iso_utc=NOW.isoformat() if running and index == 0 else "")
            snapshot.sessions[record.session_id] = replace(record, runs=[run]).to_json()


async def test_restart_recovers_a_continued_run_without_resubmission(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    record = requested(1)
    await worker.request_session(record)
    await end_latest(worker, record.session_id, done("first"))
    await worker.continue_session(record.session_id, "next", {}, "im", "continue:event-two")
    starter.recovered = Recovery(RECOVERY_TERMINAL, TurnEvent(EVENT_SUCCEEDED, text="after restart"))
    clock = FakeClock(NOW)
    restarted = Runner(JobStore(tmp_path, clock=clock), SchedulingSettings(max_concurrent_runs=1),
                       starter, clock, NoRecurrence(), owner="two")
    await restarted.start()
    try:
        runs = (await stored(restarted, record.session_id))["runs"]
        assert [item["result"] for item in runs] == ["first", "after restart"]
        assert runs[-1]["state"] == "succeeded"
        assert starter.started == [record.session_id, record.session_id]
    finally:
        await restarted.stop()


async def test_restart_recovers_terminal_without_resubmitting(tmp_path: Path) -> None:
    starter = Starter()
    starter.recovered = Recovery(RECOVERY_TERMINAL, TurnEvent(EVENT_SUCCEEDED, text="recovered"))
    worker = runner(tmp_path, starter)
    record = requested(1)
    await _stale(worker, record)
    await worker.start()
    try:
        run = (await stored(worker, record.session_id))["runs"][0]
        assert run["state"] == "succeeded" and run["result"] == "recovered"
        assert starter.started == []
        assert starter.reported == [record.session_id]
    finally:
        await worker.stop()


async def test_restart_marks_unrecoverable_started_turn_interrupted(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    record = requested(1)
    await _stale(worker, record)
    await worker.start()
    try:
        run = (await stored(worker, record.session_id))["runs"][0]
        assert run["state"] == "interrupted" and "final outcome unknown" in run["error"]
        assert starter.started == []
    finally:
        await worker.stop()


async def test_recovered_active_turn_holds_shared_budget(tmp_path: Path) -> None:
    starter = Starter()
    starter.recovered = Recovery(RECOVERY_RUNNING)
    worker = runner(tmp_path, starter)
    running, waiting = requested(1), requested(2)
    await _stale(worker, running, waiting)
    await worker.start()
    try:
        assert (await stored(worker, waiting.session_id))["runs"][0]["state"] == "pending"
        assert starter.started == []
    finally:
        await worker.stop()


@pytest.mark.parametrize("state", ["pending", "running", "replacement"])
async def test_operator_drop_targets_exact_pending_run(tmp_path: Path, state: str) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    await worker.request_session(requested(1))
    queued = await worker.request_session(requested(2))
    identity = queued.runs[-1].run_id
    if state != "pending":
        async with worker.store.transaction() as snapshot:
            run = snapshot.sessions[queued.session_id]["runs"][-1]
            if state == "running":
                run.update(state="running", started_iso_utc=NOW.isoformat())
            else:
                run["run_id"] = "run-replace001"
    if state == "pending":
        await worker.drop_pending_run(identity)
        assert worker.pending_work() == []
        run = (await stored(worker, queued.session_id))["runs"][-1]
        assert run["state"] == "cancelled" and run["prompt"] == ""
    else:
        with pytest.raises(Refused) as error:
            await worker.drop_pending_run(identity)
        assert error.value.code == "stale_selection"
    assert starter.stopped == []



def _shared_runs(*states: str) -> list[SessionRun]:
    template = requested(1).runs[0]
    return [replace(template, run_id=f"run-{number:010d}", state=state, owner="old",
                    started_iso_utc=NOW.isoformat() if state == "running" else "",
                    request=replace(template.request, idempotency_key=f"key-{number}"))  # type: ignore[arg-type]
            for number, state in enumerate(states, 1)]


async def _shared(worker: Runner, overlap: str, *states: str) -> str:
    await put(worker.store, job(1, overlap=overlap, queue_max=2, session="shared", shared_session_id="ses-shared0001"))
    async with worker.store.transaction() as snapshot:
        snapshot.sessions["ses-shared0001"]["runs"] = [run.to_json() for run in _shared_runs(*states)]
    return "ses-shared0001"


@pytest.mark.parametrize("overlap", ["parallel", "queue"])
async def test_restart_recovers_every_running_run_of_a_shared_session(tmp_path: Path, overlap: str) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    worker.settings = replace(worker.settings, max_concurrent_runs=4)
    states = ("running", "running", "pending") if overlap == "parallel" else ("running", "pending", "pending")
    session_id = await _shared(worker, overlap, *states)
    await worker.start()
    try:
        runs = (await stored(worker, session_id))["runs"]
        running = [f"run-{number:010d}" for number, state in enumerate(states, 1) if state == "running"]
        assert starter.recovering == running
        assert [run["state"] for run in runs if run["run_id"] in running] == ["interrupted"] * len(running)
        assert all(run["owner"] == "one" for run in runs)
    finally:
        await worker.stop()


async def test_cancel_stops_every_run_of_a_shared_session(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    session_id = await _shared(worker, "parallel", "succeeded", "running", "running", "pending")
    result = await worker.cancel_session(session_id)
    assert starter.stopped_runs == ["run-0000000002", "run-0000000003"]
    assert [(row["run_id"], row["state"], row["stop_request_sent"]) for row in result["runs"]] == [
        ("run-0000000002", "running", True), ("run-0000000003", "running", True),
        ("run-0000000004", "cancelled", False)]


async def test_an_owner_on_another_host_is_recovered_once_its_heartbeat_is_old(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    record = requested(1)
    await _stale(worker, record)
    async with worker.store.transaction() as snapshot:
        snapshot.owners["old"] = {"pid": 1, "host": "another-host", "seen_iso_utc": NOW.isoformat()}
    await worker.start()
    try:
        assert starter.recovering == []
        assert isinstance(worker.clock, FakeClock)
        worker.clock.advance(91)
        await worker._tick()
        assert starter.recovering == [record.runs[0].run_id]
        assert (await stored(worker, record.session_id))["runs"][0]["state"] == "interrupted"
    finally:
        await worker.stop()


async def test_restart_sends_the_notice_that_a_stopped_owner_still_owed(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    record = detached(1, key="trusted-1")
    async with worker.store.transaction() as snapshot:
        run = replace(record.runs[0], state="succeeded", outcome="stored-final", owner="old",
                      ended_iso_utc=NOW.isoformat())
        snapshot.sessions[record.session_id] = replace(record, runs=[run]).to_json()
    await worker.start()
    try:
        assert starter.reported == [record.session_id]
    finally:
        await worker.stop()


async def test_a_shared_session_continues_while_its_job_holds_it(tmp_path: Path) -> None:
    from .records_kit import job, put

    starter = Starter()
    worker = runner(tmp_path, starter)
    await put(worker.store, job(1, session="shared", shared_session_id="ses-shared0001"))
    with pytest.raises(Refused) as refused:
        await worker.continue_session("ses-shared0001", "next", {}, "dm", "continue:one")
    assert refused.value.code == "session_has_no_run"
    session, run = await worker.manual(job(1).job_id)
    with pytest.raises(Refused) as refused:
        await worker.continue_session("ses-shared0001", "next", {}, "dm", "continue:one")
    assert refused.value.code == "session_busy"
    await worker.ended_run(session.session_id, run.run_id, done("first", "posted-final"))
    continued, again = await worker.continue_session("ses-shared0001", "next", {}, "dm", "continue:one")
    assert continued.job_mode == "shared" and again.request is not None and again.request.continues


async def test_scheduled_session_continues(tmp_path: Path) -> None:
    from .records_kit import job, put

    starter = Starter()
    worker = runner(tmp_path, starter)
    await put(worker.store, job(1))
    session, run = await worker.manual(job(1).job_id)
    await worker.ended_run(session.session_id, run.run_id, done("first", "posted-final"))
    continued, again = await worker.continue_session(session.session_id, "next", {}, "dm", "continue:one")
    assert continued.kind == "scheduled" and continued.owned_conversation == session.owned_conversation
    assert [item.started_by for item in continued.runs] == ["request", "request"]
    assert again.delivery == "notice" and again.state == "running"
    assert starter.started == [session.session_id, session.session_id]


async def test_a_session_whose_pending_notice_run_is_cancelled_expires(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    await worker.request_session(requested(1))
    waiting = await worker.request_session(detached(2, key="trusted-2"))
    assert waiting.runs[0].state == "pending" and waiting.runs[0].report.state == "pending"
    result = await worker.cancel_session(waiting.session_id)
    assert result["runs"][0]["state"] == "cancelled"
    assert (await stored(worker, waiting.session_id))["runs"][0]["report"]["state"] == "none"
    assert isinstance(worker.clock, FakeClock)
    worker.clock.advance(73 * 3600)
    await worker.expiry.tick()
    async with worker.store.transaction() as snapshot:
        assert waiting.session_id not in snapshot.sessions
    assert starter.reported == []


async def test_an_operator_drop_sends_the_notice_of_the_dropped_run(tmp_path: Path) -> None:
    starter = Starter()
    worker = runner(tmp_path, starter)
    await worker.request_session(requested(1))
    waiting = await worker.request_session(detached(2, key="trusted-2"))
    await worker.drop_pending_run(waiting.runs[0].run_id)
    assert starter.reported == [waiting.session_id]


async def test_cancel_answers_when_the_session_expires_during_the_stop(tmp_path: Path) -> None:
    class ExpiringStarter(Starter):
        async def stop_run(self, session: DetachedSession, run: SessionRun) -> bool:
            async with worker.store.transaction() as snapshot:
                del snapshot.sessions[session.session_id]
            return True

    worker = runner(tmp_path, ExpiringStarter())
    started = await worker.request_session(requested(1))
    result = await worker.cancel_session(started.session_id)
    assert result["state"] == "expired"
    assert result["runs"] == [{"run_id": started.runs[0].run_id, "state": "expired", "stop_requested": True,
                               "stop_request_sent": True}]


class FailingReports(Starter):
    def __init__(self) -> None:
        super().__init__()
        self.third = asyncio.Event()

    async def report_run(self, session_id: str, run_id: str) -> None:
        self.reported.append(session_id)
        if session_id == "ses-0000000003":
            self.third.set()
        raise KeyError("slack")


async def _owed(worker: Runner, *numbers: int) -> None:
    """Store an ended run of a stopped owner that still owes its notice, for each number."""
    async with worker.store.transaction() as snapshot:
        for number in numbers:
            record = detached(number, key=f"trusted-{number}")
            run = replace(record.runs[0], state="succeeded", outcome="stored-final", owner="old",
                          ended_iso_utc=NOW.isoformat())
            snapshot.sessions[record.session_id] = replace(record, runs=[run]).to_json()


async def test_a_failed_delivery_in_the_recovery_pass_stops_nothing(tmp_path: Path) -> None:
    starter = FailingReports()
    worker = runner(tmp_path, starter)
    await _owed(worker, 1, 2)
    await worker.start()
    try:
        assert starter.reported == ["ses-0000000001", "ses-0000000002"]
        await _owed(worker, 3)
        assert isinstance(worker.clock, FakeClock)
        worker.clock.advance(31)
        # The loop's next heartbeat pass delivers what the third session owes.
        await asyncio.wait_for(starter.third.wait(), 5)
        assert worker._task is not None and not worker._task.done()
    finally:
        await worker.stop()


async def test_recovery_keeps_an_owed_shared_final_until_delivery(tmp_path: Path) -> None:
    delivered: list[str] = []

    class DeliveringStarter(Starter):
        async def report_run(self, session_id: str, run_id: str) -> None:
            async with worker.store.transaction() as snapshot:
                found = snapshot.find_run(run_id)
                if found is not None:
                    delivered.append(found[1]["result"])
                    found[1]["outcome"] = "posted-final"

    shared = job(1, session="shared", shared_session_id="ses-shared0001")
    seed = JobStore(tmp_path, kept_runs_per_job=2)
    await put(seed, shared)
    owed = replace(requested(1).runs[0], state="succeeded", outcome="stored-final", delivery="post_final",
                   result="recovered answer", result_available=True, owner="old", ended_iso_utc=NOW.isoformat())
    newer = replace(owed, run_id="run-newer00001", outcome="posted-final", result="newer answer")
    async with seed.transaction() as snapshot:
        snapshot.sessions[shared.shared_session_id]["runs"] = [owed.to_json(), newer.to_json()]

    worker = runner(tmp_path, DeliveringStarter())
    worker.store = JobStore(tmp_path, kept_runs_per_job=1, clock=worker.clock)
    await worker._recover_runs()

    assert delivered == ["recovered answer"]
    async with worker.store.transaction() as snapshot:
        assert [run["run_id"] for run in snapshot.sessions[shared.shared_session_id]["runs"]] == [newer.run_id]
