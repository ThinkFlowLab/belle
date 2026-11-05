"""Build and read store records in scheduler tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from belle_agent.scheduling.records import (
    NO_DELETION,
    DetachedSession,
    Job,
    RequestStart,
    fingerprint,
    new_run,
)
from belle_agent.scheduling.store import JobStore

BASE = datetime(2026, 10, 18, tzinfo=UTC)
PLACE = {"platform": "loopback", "workspace": "", "conversation": "one", "thread": ""}


def job(number: int, *, when: dict[str, Any] | None = None, overlap: str = "skip", catch_up: str = "all",
        queue_max: int = 1, **fields: Any) -> Job:
    """Make a fresh job that falls due one second after ``BASE``."""
    iso = BASE.isoformat()
    return replace(Job(
        f"job-{number:010d}", "name", "prompt",
        when or {"kind": "once", "at": (BASE + timedelta(seconds=1)).isoformat()},
        True, False, "", PLACE.copy(), "fresh", overlap, queue_max, catch_up, "refuse", {},
        PLACE.copy(), "O", "user", "", iso, "user", iso, 0, 0, "opencode",
    ), **fields)


async def put(store: JobStore, *jobs: Job) -> None:
    """Store jobs. A shared job gets its session."""
    async with store.transaction() as snapshot:
        for item in jobs:
            snapshot.jobs[item.job_id] = item.to_json()
            if item.shared_session_id:
                snapshot.sessions[item.shared_session_id] = DetachedSession(
                    item.shared_session_id, "scheduled", item.job_id, "shared", item.backend, item.origin,
                    item.origin_chat_type, item.created_by_user_id, item.created_iso_utc, NO_DELETION, [],
                ).to_json()


async def jobs(store: JobStore) -> dict[str, dict[str, Any]]:
    """Read every stored job."""
    async with store.transaction() as snapshot:
        return snapshot.jobs


async def sessions(store: JobStore) -> dict[str, dict[str, Any]]:
    """Read every stored session."""
    async with store.transaction() as snapshot:
        return snapshot.sessions


async def history(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    """Return the runs and the unrun due times of one job, by due time: each a run or an unrun record."""
    async with store.transaction() as snapshot:
        runs = [{**run, "session_id": session["session_id"], "due": run["clock"]["due_iso_utc"] if run["clock"]
                 else run["request"]["requested_iso_utc"]}
                for session in snapshot.job_sessions(job_id) for run in session["runs"]]
        owner = snapshot.jobs.get(job_id) or snapshot.deleted[job_id]
        unrun = [{**item, "due": item["due_iso_utc"]} for item in owner["unrun"]]
    return sorted(runs + unrun, key=lambda item: (datetime.fromisoformat(item["due"]), item.get("run_id", "")))


def detached(number: int = 1, *, prompt: str = "task", notify: bool = True, key: str = "") -> DetachedSession:
    """Make a detached session with one pending run."""
    origin = {"platform": "loopback", "workspace": "", "conversation": "room", "thread": ""}
    request = RequestStart(key or f"key-{number}", fingerprint(number, prompt), {}, BASE.isoformat(), None)
    run = new_run(started_by="request", prompt=prompt, turn={}, questions="refuse", place=origin,
                  delivery="notice" if notify else "store", owner="", request=request)
    return DetachedSession(f"ses-{number:010d}", "standalone", "", "", "opencode", origin, "dm", "alice",
                   BASE.isoformat(), NO_DELETION, [run])
