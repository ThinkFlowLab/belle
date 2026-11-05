"""Scheduler store durability, version and record checks."""

import asyncio
import json
import multiprocessing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from belle_agent.scheduling.records import (
    ClockStart,
    DetachedSession,
    Job,
    RecordInvalid,
    SessionRun,
    UnrunDue,
    new_run,
    request_id_of,
)
from belle_agent.scheduling.store import JobStore, StoreUnreadable

from .records_kit import detached, job, put


def _write_many(state_dir: str, first: int) -> None:
    async def write() -> None:
        store = JobStore(Path(state_dir))
        for number in range(first, first + 100):
            async with store.transaction() as snapshot:
                snapshot.jobs[f"job-{number:010d}"] = job(number).to_json()

    asyncio.run(write())


def _file(**sessions: dict[str, Any]) -> dict[str, Any]:
    return {"version": 4, "owners": {}, "jobs": {}, "deleted": {}, "sessions": sessions}


async def _refused(store: JobStore, data: dict[str, Any], match: str) -> None:
    raw = json.dumps(data)
    store.path.write_text(raw, encoding="utf-8")
    with pytest.raises(StoreUnreadable, match=match):
        async with store.transaction() as snapshot:
            snapshot.owners["new"] = {"pid": 1, "host": "h", "seen_iso_utc": "2026-10-18T00:00:00Z"}
    assert store.path.read_text(encoding="utf-8") == raw


async def test_missing_store_is_empty(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    async with store.transaction() as snapshot:
        assert snapshot.jobs == {}
    assert not store.path.exists()


def test_two_processes_share_the_lock(tmp_path: Path) -> None:
    processes = [multiprocessing.Process(target=_write_many, args=(str(tmp_path), first)) for first in (0, 100)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    data = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert len(data["jobs"]) == 200


@pytest.mark.parametrize("raw", ["{", "[]", '{"version":5,"owners":{},"jobs":{},"deleted":{},"sessions":{}}',
                                 '{"version":4,"owners":{},"jobs":{},"deleted":{}}'])
async def test_unreadable_store_is_never_written(tmp_path: Path, raw: str) -> None:
    store = JobStore(tmp_path)
    store.path.write_text(raw, encoding="utf-8")
    with pytest.raises(StoreUnreadable):
        async with store.transaction() as snapshot:
            snapshot.jobs["anything"] = {}
    assert store.path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("version", [1, 2, 3])
async def test_old_versions_are_refused_unchanged(tmp_path: Path, version: int) -> None:
    store = JobStore(tmp_path)
    original = json.dumps({"version": version, "owners": {}, "jobs": {}, "deleted": {}, "sessions": {}}).encode()
    store.path.write_bytes(original)
    with pytest.raises(StoreUnreadable) as refused:
        async with store.transaction() as snapshot:
            snapshot.jobs["job-1234567890"] = job(1).to_json()
    assert str(store.path) in str(refused.value)
    assert f"version {version} holds records of the earlier session model" in str(refused.value)
    assert "delete the file" in str(refused.value)
    assert store.path.read_bytes() == original
    assert not any(path.suffix == ".bak" for path in tmp_path.iterdir())


async def test_a_job_write_increments_revision(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    await put(store, job(1))
    async with store.transaction() as snapshot:
        snapshot.jobs[job(1).job_id]["name"] = "after"
    async with store.transaction() as snapshot:
        assert snapshot.jobs[job(1).job_id]["revision"] == 2


async def test_invalid_mutation_does_not_replace_store(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    await put(store, job(1))
    original = store.path.read_bytes()
    with pytest.raises(StoreUnreadable, match=r"jobs\.job-0000000001\.name must be a string"):
        async with store.transaction() as snapshot:
            snapshot.jobs[job(1).job_id]["name"] = 10
    assert store.path.read_bytes() == original


async def test_write_failure_keeps_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobStore(tmp_path)
    await put(store, job(1))
    old = store.path.read_bytes()

    def fail_replace(*args: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("belle_agent.scheduling.store.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        async with store.transaction() as snapshot:
            snapshot.jobs[job(1).job_id]["name"] = "after"
    assert store.path.read_bytes() == old


async def test_a_deleted_job_goes_with_its_last_session(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    async with store.transaction() as snapshot:
        snapshot.deleted[job(1).job_id] = {"origin": job(1).origin, "deliver_to": job(1).deliver_to,
                                           "created_by_user_id": "u", "history_kept": True, "unrun": [],
                                           "dropped_through_iso_utc": "", "backend": "opencode"}
    async with store.transaction() as snapshot:
        assert snapshot.deleted == {}


def test_session_owned_conversation_is_derived() -> None:
    session = detached(1)
    stored = session.to_json()
    assert stored["owned_conversation"] == "belle_session_ses-0000000001"
    assert DetachedSession.from_json(stored, "s") == session
    with pytest.raises(RecordInvalid, match=r"sessions\.x\.owned_conversation must be belle_session_ses-0000000001"):
        DetachedSession.from_json({**stored, "owned_conversation": "slack_T1_C1_1700.1"}, "sessions.x")


def test_session_refuses_chat_record_fields() -> None:
    with pytest.raises(RecordInvalid, match="unknown keys key"):
        DetachedSession.from_json({**detached(1).to_json(), "key": "slack_T1_C1_1700.1"}, "s")


async def test_awaited_kind_is_refused(tmp_path: Path) -> None:
    stored = {**detached(1).to_json(), "kind": "awaited"}
    await _refused(JobStore(tmp_path), _file(**{"ses-0000000001": stored}),
                   r"sessions\.ses-0000000001\.kind must be one of: scheduled, standalone")


def test_run_has_one_start() -> None:
    run = detached(1).runs[0].to_json()
    clock = ClockStart("2026-10-18T00:00:00Z", 0, 0, "k").to_json()
    for wrong in ({**run, "clock": clock}, {**run, "request": None}):
        with pytest.raises(RecordInvalid, match="must have clock for started_by clock"):
            SessionRun.from_json(wrong, "run")


def test_started_by_message_is_refused() -> None:
    with pytest.raises(RecordInvalid, match="started_by must be one of: clock, request"):
        SessionRun.from_json({**detached(1).runs[0].to_json(), "started_by": "message"}, "run")


def test_run_has_no_request_id_field() -> None:
    run = detached(1).runs[0]
    with pytest.raises(RecordInvalid, match="unknown keys request_id"):
        SessionRun.from_json({**run.to_json(), "request_id": run.run_id}, "run")
    assert request_id_of(run) == run.run_id


async def test_shared_job_has_one_session(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    shared = replace(job(1), session="shared", shared_session_id="ses-shared0001")
    await put(store, shared)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    second = {**data["sessions"]["ses-shared0001"], "session_id": "ses-shared0002",
              "owned_conversation": "belle_session_ses-shared0002"}
    await _refused(store, {**data, "sessions": {**data["sessions"], "ses-shared0002": second}},
                   "second shared session of job job-0000000001")
    await _refused(store, {**data, "sessions": {}}, r"jobs\.job-0000000001\.shared_session_id must name a session")


async def test_owned_conversations_are_unique(tmp_path: Path) -> None:
    stored = detached(1).to_json()
    await _refused(JobStore(tmp_path), _file(**{"ses-0000000001": stored, "ses-0000000002": stored}),
                   r"sessions\.ses-0000000002\.session_id must be ses-0000000002")


async def test_claim_keys_are_unique(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    await put(store, job(1))
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["jobs"][job(1).job_id]["unrun"] = [UnrunDue("2026-10-18T00:00:00Z", "skipped", "x", "k").to_json()] * 2
    await _refused(store, data, r"unrun\[1\]\.claim_key must be unique")


def test_public_fields_are_explicit() -> None:
    from belle_agent.scheduling.records import JOB_PUBLIC, RUN_ROW, job_json, run_row

    session = detached(1)
    assert list(job_json(job(1)))[:-2] == list(JOB_PUBLIC)
    assert "shared_session_id" not in job_json(job(1)) and "unrun" not in job_json(job(1))
    assert list(run_row(session, session.runs[0])) == list(RUN_ROW)


def test_clock_run_inbound_has_no_message_id() -> None:
    run = new_run(started_by="clock", prompt="p", turn={}, questions="refuse", place=job(1).deliver_to,
                  delivery="post_final", owner="", clock=ClockStart("2026-10-18T00:00:00Z", 0, 0, "k"))
    assert run.request is None and request_id_of(run) == run.run_id


def test_job_round_trip() -> None:
    assert Job.from_json(job(1).to_json(), "job") == job(1)


async def test_a_cancelled_acquisition_leaves_the_file_unlocked(tmp_path: Path) -> None:
    import portalocker

    store = JobStore(tmp_path)
    holder = portalocker.Lock(str(store.path.with_suffix(".json.lock")), timeout=10)
    holder.acquire()
    try:
        waiter = asyncio.create_task(store.transaction().__aenter__())
        await asyncio.sleep(0.2)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    finally:
        holder.release()
    await asyncio.sleep(0.2)
    async with asyncio.timeout(5):
        async with store.transaction() as snapshot:
            assert snapshot.jobs == {}
