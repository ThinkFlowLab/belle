"""Runner claims, queues and recovers job runs using a fake clock."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from belle_agent.scheduling.records import DetachedSession, Job, SessionRun
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.runner import RunEnd, Runner
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore

from .fake_clock import FakeClock
from .records_kit import BASE, history, job, jobs, put, sessions


class Recurrence:
    def next_after(self, when: dict[str, Any], after: Any) -> Any:
        from datetime import datetime

        first = datetime.fromisoformat(when["at"])
        if when["kind"] == "once":
            return first if first > after else None
        seconds = when["seconds"]
        count = max(0, int((after - first).total_seconds() // seconds) + 1)
        due = first + timedelta(seconds=count * seconds)
        until = when.get("until")
        return due if until is None or due <= datetime.fromisoformat(until) else None


class Starter:
    def __init__(self) -> None:
        self.started: list[tuple[DetachedSession, SessionRun]] = []
        self.event = asyncio.Event()

    async def start_run(self, job: Job | None, session: DetachedSession, run: SessionRun) -> None:
        self.started.append((session, run))
        self.event.set()

    async def stop_run(self, session: DetachedSession, run: SessionRun) -> bool:
        return True

    async def recover_run(self, session: DetachedSession, run: SessionRun) -> None:
        return None

    async def report_run(self, session_id: str, run_id: str) -> None:
        return None

    async def can_continue_session(self, session: DetachedSession) -> bool:
        return True


def runner(path: Path, clock: FakeClock, starter: Starter, *, owner: str = "one", slots: int = 4, failures: int = 3) -> Runner:
    return Runner(JobStore(path, clock=clock), SchedulingSettings(max_concurrent_runs=slots, disable_after_failures=failures), starter, clock, Recurrence(), owner=owner)


def end(clock: FakeClock, outcome: str = "posted-final") -> RunEnd:
    return RunEnd(outcome, "done", [], "problem" if outcome == "failed" else "", {}, clock.now().isoformat())


async def finish(worker: Runner, started: tuple[DetachedSession, SessionRun], ended: RunEnd) -> None:
    await worker.ended_run(started[0].session_id, started[1].run_id, ended)


async def test_start_loop_wakes_at_due_time_and_stops(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    await worker.start()
    clock.advance(1)
    await asyncio.wait_for(starter.event.wait(), 1)
    await worker.stop()
    assert [session.job_id for session, _ in starter.started] == [job(1).job_id]


async def test_fresh_due_time_makes_a_session(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    (session,) = (await sessions(worker.store)).values()
    assert session["kind"] == "scheduled" and session["job_id"] == job(1).job_id and session["job_mode"] == "fresh"
    assert session["owned_conversation"] == f"belle_session_{session['session_id']}"
    (run,) = session["runs"]
    assert run["started_by"] == "clock" and run["request"] is None and run["delivery"] == "post_final"
    assert run["clock"]["claim_key"] == f"{job(1).job_id}:{int((BASE + timedelta(seconds=1)).timestamp())}"


async def test_shared_due_time_joins_the_job_session(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    shared = job(1, when={"kind": "every", "at": (BASE + timedelta(seconds=1)).isoformat(), "seconds": 1},
                 overlap="parallel", session="shared", shared_session_id="ses-shared0001")
    await put(worker.store, shared)
    await worker._tick(recover=True)
    for _ in range(2):
        clock.advance(1)
        await worker._tick()
    stored = await sessions(worker.store)
    assert list(stored) == ["ses-shared0001"]
    assert [run["state"] for run in stored["ses-shared0001"]["runs"]] == ["running", "running"]
    assert {session.session_id for session, _ in starter.started} == {"ses-shared0001"}


async def test_two_jobs_due_in_the_same_second_both_run(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1), job(2))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    assert {session.job_id for session, _ in starter.started} == {job(1).job_id, job(2).job_id}
    assert worker.status()["metrics"]["running_runs"]["value"] == 2


async def test_cancel_scheduled_session_keeps_its_job(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    session, _ = starter.started[0]
    result = await worker.cancel_session(session.session_id)
    (row,) = result["runs"]
    assert row["stop_request_sent"] and row["stop_requested"]
    assert job(1).job_id in await jobs(worker.store)


async def test_ten_jobs_due_with_four_slots_all_run(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, *(job(number) for number in range(10)))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    assert len(starter.started) == 4
    assert worker.status()["metrics"]["pending_runs"]["value"] == 6
    for index in range(0, 10, 4):
        clock.advance(1)
        for started in starter.started[index:index + 4]:
            await finish(worker, started, end(clock))
        await worker._tick()
    assert len(starter.started) == 10
    assert all(run.clock is not None and run.clock.late_seconds > 0 for _, run in starter.started[4:])


@pytest.mark.parametrize("overlap,states", [
    ("skip", ["running", "skipped", "skipped"]),
    ("queue", ["running", "pending", "skipped"]),
    ("parallel", ["running", "running", "running"]),
])
async def test_overlap_skip_queue_parallel(tmp_path: Path, overlap: str, states: list[str]) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1, when={"kind": "every", "at": (BASE + timedelta(seconds=1)).isoformat(), "seconds": 1}, overlap=overlap))
    await worker._tick(recover=True)
    for _ in range(3):
        clock.advance(1)
        await worker._tick()
    assert [item["state"] for item in await history(worker.store, job(1).job_id)] == states


async def test_skipped_due_time_is_unrun(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1, when={"kind": "every", "at": (BASE + timedelta(seconds=1)).isoformat(), "seconds": 1}))
    await worker._tick(recover=True)
    for _ in range(2):
        clock.advance(1)
        await worker._tick()
    assert len(await sessions(worker.store)) == 1
    (skipped,) = (await jobs(worker.store))[job(1).job_id]["unrun"]
    assert skipped["state"] == "skipped" and "overlap is skip" in skipped["reason"]


@pytest.mark.parametrize("catch_up,states,started", [
    ("skip", ["missed"], 0),
    ("latest", ["missed", "running"], 1),
    ("all", ["running", "skipped", "skipped", "skipped"], 1),
])
async def test_catch_up_after_two_hours(tmp_path: Path, catch_up: str, states: list[str], started: int) -> None:
    clock, starter = FakeClock(BASE + timedelta(hours=2)), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1, when={"kind": "every", "at": (BASE + timedelta(minutes=30)).isoformat(), "seconds": 1800}, catch_up=catch_up))
    await worker._tick(recover=True)
    items = await history(worker.store, job(1).job_id)
    assert [item["state"] for item in items] == states
    assert len(starter.started) == started
    if catch_up != "all":
        assert items[0]["due_iso_utc"] == (BASE + timedelta(minutes=30)).isoformat(timespec="seconds").replace("+00:00", "Z")
        assert items[0]["reason"].startswith("4 due times" if catch_up == "skip" else "3 due times")
    if catch_up == "latest":
        assert items[1]["clock"]["missed_runs"] == 3


async def test_two_runners_claim_and_start_each_run_once(tmp_path: Path) -> None:
    # Both runners share this test's pid, so each takes the other for an earlier process of its own pid.
    clock, first, second = FakeClock(BASE + timedelta(seconds=1)), Starter(), Starter()
    one, two = runner(tmp_path, clock, first, owner="one"), runner(tmp_path, clock, second, owner="two")
    await put(one.store, *(job(number) for number in range(100)))
    await one._tick(recover=True)
    await two._tick(recover=True)
    stored = await sessions(one.store)
    assert len(stored) == 100
    assert len({session["runs"][0]["clock"]["claim_key"] for session in stored.values()}) == 100
    started = [run.run_id for _, run in first.started + second.started]
    assert len(started) == len(set(started)) == 8


async def test_failures_pause_job(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1, when={"kind": "every", "at": (BASE + timedelta(seconds=1)).isoformat(), "seconds": 1}))
    await worker._tick(recover=True)
    for number in range(1, 4):
        clock.advance(1)
        await worker._tick()
        await finish(worker, starter.started[-1], end(clock, "failed"))
        stored = (await jobs(worker.store))[job(1).job_id]
        assert stored["consecutive_failures"] == number
    assert stored["enabled"] is False
    assert "3 runs failed" in stored["disabled_reason"]


async def test_manual_failure_does_not_count(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter, failures=1)
    await put(worker.store, job(1))
    session, run = await worker.manual(job(1).job_id)
    assert run.started_by == "request" and run.delivery == "post_final" and run.clock is None
    assert [started.run_id for _, started in starter.started] == [run.run_id]
    await finish(worker, (session, run), end(clock, "failed"))
    stored = (await jobs(worker.store))[job(1).job_id]
    assert stored["enabled"] and stored["runs_started"] == 0 and stored["consecutive_failures"] == 0


async def test_unreadable_store_pauses_claims_and_recovers(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    await worker._tick(recover=True)
    valid = worker.store.path.read_bytes()
    worker.store.path.write_text("{", encoding="utf-8")
    clock.advance(1)
    await worker._tick()
    assert worker._blocked and not starter.started
    assert job(1).job_id in worker._plan
    worker.store.path.write_bytes(valid)
    await worker._tick()
    assert not worker._blocked and [session.job_id for session, _ in starter.started] == [job(1).job_id]


async def test_status_uses_detached_cache_without_store_io(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.control.protocol import validate_component

    clock = FakeClock(BASE)
    worker = runner(tmp_path, clock, Starter())
    assert worker.status()["state"] == "unavailable"
    await put(worker.store, job(1), job(2, enabled=False), job(3, ended=True))
    await worker._tick(recover=True)
    before = worker.store.path.read_bytes()

    def forbidden() -> None:
        raise AssertionError("status must not touch the job store")

    monkeypatch.setattr(worker.store, "transaction", forbidden)
    status = worker.status()
    validate_component(status)
    assert [row["state"] for row in status["rows"]] == ["active", "paused", "ended"]
    assert status["rows"][0]["next_due"] == "2026-10-18T00:00:01Z"
    assert status["rows"][1]["next_due"] is None
    assert status["metrics"]["total_jobs"]["value"] == 3
    shown = str(status)
    assert "prompt" not in shown and "deliver_to" not in shown and "user" not in shown
    status["rows"][0]["name"] = "changed by reader"
    assert worker.status()["rows"][0]["name"] == "name"
    assert worker.store.path.read_bytes() == before


async def test_status_drops_cached_rows_when_store_becomes_unreadable(tmp_path: Path) -> None:
    clock = FakeClock(BASE)
    worker = runner(tmp_path, clock, Starter())
    await put(worker.store, job(1))
    await worker._tick(recover=True)
    assert worker.status()["rows"]
    worker.store.path.write_text("invalid JSON")
    await worker._tick()
    assert worker.status()["state"] == "unavailable"
    assert "rows" not in worker.status()


async def test_operator_toggle_persists_state_and_wakes_scheduler(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    item = job(1)
    await put(worker.store, item)
    await worker._tick()
    for expected, enabled in [("active", False), ("paused", True)]:
        await worker.toggle_job(item.job_id, expected)
        saved = (await jobs(worker.store))[item.job_id]
        assert saved["enabled"] is enabled
        assert saved["revision"] == (2 if not enabled else 3)
        assert saved["updated_by_user_id"] == ""
        assert worker.status()["rows"][0]["state"] == ("active" if enabled else "paused")
        assert worker._wake.is_set()
    clock.advance(1)
    await worker._tick()
    assert len(starter.started) == 1


@pytest.mark.parametrize("case,code", [("missing", "job_not_found"), ("ended", "job_ended"),
                                       ("changed", "no_change"), ("invalid", "invalid_arguments")])
async def test_operator_toggle_refuses_without_changing_job(tmp_path: Path, case: str, code: str) -> None:
    worker = runner(tmp_path, FakeClock(BASE), Starter())
    item = job(1, ended=case == "ended")
    await put(worker.store, item)
    before = await jobs(worker.store)
    with pytest.raises(Refused) as error:
        await worker.toggle_job("missing" if case == "missing" else item.job_id,
                                "paused" if case == "changed" else "ended" if case == "invalid" else "active")
    assert error.value.code == code
    assert await jobs(worker.store) == before


@pytest.mark.parametrize("state", ["active", "paused", "ended"])
async def test_operator_delete_removes_idle_job_and_cached_status(tmp_path: Path, state: str) -> None:
    worker = runner(tmp_path, FakeClock(BASE), Starter())
    item = job(1, enabled=state != "paused", ended=state == "ended")
    await put(worker.store, item)
    await worker.delete_job(item.job_id, state)
    assert await jobs(worker.store) == {}
    assert worker.status()["rows"] == []
    assert worker._wake.is_set()


async def test_operator_delete_stops_a_running_run(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    stopped: list[str] = []

    async def stop_run(session: DetachedSession, run: SessionRun) -> bool:
        stopped.append(run.run_id)
        return True

    starter.stop_run = stop_run  # type: ignore[method-assign]
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    session, run = starter.started[0]
    await worker.delete_job(job(1).job_id, "active")
    assert stopped == [run.run_id]
    stored = await sessions(worker.store)
    assert stored[session.session_id]["runs"][0]["stop_requested"]
    async with worker.store.transaction() as snapshot:
        assert snapshot.deleted[job(1).job_id]["history_kept"] is False
    await finish(worker, (session, run), end(clock, "interrupted"))
    assert (await sessions(worker.store))[session.session_id]["runs"][0]["state"] == "cancelled"


@pytest.mark.parametrize("fails", ["returns false", "raises"])
async def test_operator_delete_whose_stop_fails_does_not_claim_cancellation(tmp_path: Path, fails: str) -> None:
    clock, starter = FakeClock(BASE), Starter()

    async def stop_run(session: DetachedSession, run: SessionRun) -> bool:
        if fails == "raises":
            raise RuntimeError("the backend refused the stop")
        return False

    starter.stop_run = stop_run  # type: ignore[method-assign]
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    session, run = starter.started[0]
    if fails == "raises":
        with pytest.raises(RuntimeError):
            await worker.delete_job(job(1).job_id, "active")
    else:
        await worker.delete_job(job(1).job_id, "active")
    assert not (await sessions(worker.store))[session.session_id]["runs"][0]["stop_requested"]
    await finish(worker, (session, run), end(clock, "interrupted"))
    assert (await sessions(worker.store))[session.session_id]["runs"][0]["state"] == "interrupted"


async def test_a_stop_that_another_process_cannot_send_keeps_the_mark(tmp_path: Path) -> None:
    """The owner may hold the run until its session is free: it then reads the mark and does not start the run."""
    clock, starter, elsewhere = FakeClock(BASE), Starter(), Starter()

    async def stop_run(session: DetachedSession, run: SessionRun) -> bool:
        return False

    elsewhere.stop_run = stop_run  # type: ignore[method-assign]
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    session, run = starter.started[0]
    await runner(tmp_path, clock, elsewhere, owner="two").delete_job(job(1).job_id, "active")
    assert (await sessions(worker.store))[session.session_id]["runs"][0]["stop_requested"]


@pytest.mark.parametrize("case,code", [("missing", "job_not_found"), ("changed", "stale_selection"),
                                       ("invalid", "invalid_arguments")])
async def test_operator_delete_refuses_without_mutating_job(tmp_path: Path, case: str, code: str) -> None:
    worker = runner(tmp_path, FakeClock(BASE), Starter())
    item = job(1)
    await put(worker.store, item)
    before = await jobs(worker.store)
    with pytest.raises(Refused) as error:
        await worker.delete_job("missing" if case == "missing" else item.job_id,
                                "paused" if case == "changed" else "bad" if case == "invalid" else "active")
    assert error.value.code == code
    assert await jobs(worker.store) == before
