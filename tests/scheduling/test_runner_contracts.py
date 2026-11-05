"""Runner contracts across edits, completion, and owner recovery."""

import asyncio
import multiprocessing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.runner import delete_job_records

from .fake_clock import FakeClock
from .records_kit import BASE, history, job, jobs, put, sessions
from .test_runner import Recurrence, Starter, end, finish, runner


async def test_an_edit_does_not_drop_another_jobs_due_run(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1), job(2, when={"kind": "once", "at": (BASE + timedelta(seconds=2)).isoformat()}))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    async with worker.store.transaction() as snapshot:
        snapshot.jobs[job(1).job_id]["name"] = "edited"
    worker.changed([job(1).job_id])
    clock.advance(1)
    await worker._tick()
    assert {session.job_id for session, _ in starter.started} == {job(1).job_id, job(2).job_id}


async def test_a_run_end_one_second_before_another_due_drops_nothing(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1), job(2, when={"kind": "once", "at": (BASE + timedelta(seconds=2)).isoformat()}))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    await finish(worker, starter.started[0], end(clock))
    clock.advance(1)
    await worker._tick()
    assert [session.job_id for session, _ in starter.started] == [job(1).job_id, job(2).job_id]


async def test_max_runs_and_until_end_the_job(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    first = job(1, when={"kind": "every", "at": (BASE + timedelta(seconds=1)).isoformat(), "seconds": 1, "max_runs": 2})
    second = job(2, when={"kind": "every", "at": (BASE + timedelta(seconds=1)).isoformat(), "seconds": 1, "until": (BASE + timedelta(seconds=1)).isoformat()})
    await put(worker.store, first, second)
    await worker._tick(recover=True)
    for _ in range(3):
        clock.advance(1)
        await worker._tick()
        for started in list(starter.started):
            if (await sessions(worker.store))[started[0].session_id]["runs"][0]["state"] == "running":
                await finish(worker, started, end(clock))
    stored = await jobs(worker.store)
    assert stored[first.job_id]["runs_started"] == 2
    assert stored[second.job_id]["runs_started"] == 1
    assert stored[first.job_id]["ended"] and stored[second.job_id]["ended"]


async def test_interrupted_and_cancelled_follow_stop_request(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1), job(2))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    stopped = next(session.session_id for session, _ in starter.started if session.job_id == job(2).job_id)
    async with worker.store.transaction() as snapshot:
        snapshot.sessions[stopped]["runs"][0]["stop_requested"] = True
    for started in starter.started:
        await finish(worker, started, end(clock, "interrupted"))
    states = {item["job_id"]: item["runs"][0]["state"] for item in (await sessions(worker.store)).values()}
    assert states == {job(1).job_id: "interrupted", job(2).job_id: "cancelled"}


async def test_manual_run_claims_and_enforces_overlap(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1, overlap="skip"), job(2, overlap="queue", queue_max=1))
    place = {**job(1).deliver_to, "conversation": "direct"}
    _, first = await worker.manual(job(1).job_id, deliver_to=place, turn={}, approved_readers=["new-reader"])
    assert first.started_by == "request" and first.state == "running" and first.delivery == "post_final"
    assert first.place == place and first.turn == {}
    assert first.request is not None and first.request.approved_readers == ["new-reader"]
    with pytest.raises(Refused) as refused:
        await worker.manual(job(1).job_id)
    assert refused.value.code == "job_running"
    await worker.manual(job(2).job_id)
    _, queued = await worker.manual(job(2).job_id)
    assert queued.state == "pending"
    with pytest.raises(Refused) as refused:
        await worker.manual(job(2).job_id)
    assert refused.value.code == "job_queue_full"


async def test_deleted_job_accepts_the_stopped_runs_end(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, job(1))
    session, run = await worker.manual(job(1).job_id)
    async with worker.store.transaction() as snapshot:
        ending = delete_job_records(snapshot, job(1).job_id, keep_history=True, now=clock.now(), notify=True)
        assert ending.stopping == ending.ended == [(session.session_id, run.run_id)]
    assert await worker.stop_runs([(session.session_id, run.run_id)]) == [run.run_id]
    await finish(worker, (session, run), end(clock, "interrupted"))
    (stored,) = await history(worker.store, job(1).job_id)
    assert stored["state"] == "cancelled" and stored["session_id"] == session.session_id
    async with worker.store.transaction() as snapshot:
        assert snapshot.sessions[session.session_id]["job_id"] == job(1).job_id
        assert snapshot.deleted[job(1).job_id]["history_kept"] is True


async def test_deleting_frees_the_slot_and_starts_pending(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter, slots=1)
    await put(worker.store, job(1), job(2))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    first = starter.started[0]
    async with worker.store.transaction() as snapshot:
        delete_job_records(snapshot, first[0].job_id, keep_history=False, now=clock.now(), notify=True)
    await finish(worker, first, end(clock, "interrupted"))
    assert len(starter.started) == 2
    await finish(worker, starter.started[1], end(clock))
    await put(worker.store, job(3))
    _, manual = await worker.manual(job(3).job_id)
    assert manual.state == "running"


async def test_dead_owner_interrupts_running_and_reclaims_pending(tmp_path: Path) -> None:
    clock, old_starter = FakeClock(BASE), Starter()
    old = runner(tmp_path, clock, old_starter, owner="old", slots=1)
    await put(old.store, job(1), job(2))
    await old._tick(recover=True)
    clock.advance(1)
    await old._tick()
    assert len(old_starter.started) == 1
    clock.advance(91)
    new_starter = Starter()
    new = runner(tmp_path, clock, new_starter, owner="new", slots=1)
    await new._tick(recover=True)
    states = {item["job_id"]: (item["runs"][0]["state"], item["runs"][0]["owner"])
              for item in (await sessions(new.store)).values()}
    assert states[job(1).job_id][0] == "interrupted"
    assert states[job(2).job_id] == ("running", "new")
    assert [session.job_id for session, _ in new_starter.started] == [job(2).job_id]


def _claim_process(state_dir: str, name: str, result: Any) -> None:
    async def claim() -> None:
        clock, starter = FakeClock(BASE + timedelta(seconds=1)), Starter()
        worker = runner(Path(state_dir), clock, starter, owner=name)
        await worker._tick(recover=True)
        result.put([run.run_id for _, run in starter.started])
    asyncio.run(claim())


async def test_two_processes_claim_each_run_once(tmp_path: Path) -> None:
    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter)
    await put(worker.store, *(job(number) for number in range(20)))
    queue: Any = multiprocessing.Queue()
    processes = [multiprocessing.Process(target=_claim_process, args=(str(tmp_path), name, queue)) for name in ("one", "two")]
    for process in processes:
        process.start()
    for process in processes:
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode == 0
    # A process that ended before the other one claimed leaves its pending runs to it.
    started = [run_id for _ in processes for run_id in queue.get(timeout=1)]
    assert len(started) == len(set(started)) and len(started) in {4, 8}
    claims = [session["runs"][0]["clock"]["claim_key"] for session in (await sessions(worker.store)).values()]
    assert len(claims) == len(set(claims)) == 20


async def test_one_queue_for_every_starter(tmp_path: Path) -> None:
    from .records_kit import detached

    clock, starter = FakeClock(BASE), Starter()
    worker = runner(tmp_path, clock, starter, slots=1)
    await put(worker.store, job(1))
    await worker.request_session(detached(1))
    await worker._tick(recover=True)
    clock.advance(1)
    await worker._tick()
    assert [session.kind for session, _ in starter.started] == ["standalone"]
    assert worker.status()["metrics"]["pending_runs"]["value"] == 1
    await finish(worker, starter.started[0], end(clock))
    assert [session.kind for session, _ in starter.started] == ["standalone", "scheduled"]


def test_recurrence_protocol_accepts_schedule_mapping() -> None:
    next_after = Recurrence().next_after
    assert next_after({"kind": "once", "at": (BASE + timedelta(seconds=1)).isoformat()}, BASE) == BASE + timedelta(seconds=1)
