"""Name the origin, due time, and delivery of a scheduled turn."""

from datetime import datetime
from zoneinfo import ZoneInfo

from belle_agent.scheduling.address import Address
from belle_agent.scheduling.records import Job, SessionRun


def _place(address: Address) -> str:
    path = f"{address.platform}/{address.workspace}/{address.conversation}"
    if address.thread == "new":
        return f"a new thread in {path}"
    if address.thread:
        return f"thread {address.thread} in {path}"
    return path


def run_label(job: Job, session_id: str, run: SessionRun, target: Address, *, question_timeout_seconds: int) -> str:
    """Return the first system addition of a scheduled turn."""
    zone_name = str(job.when.get("zone") or "UTC")
    due = datetime.fromisoformat(run.queued_iso_utc).astimezone(ZoneInfo(zone_name))
    late = run.clock.late_seconds if run.clock is not None else 0
    origin = Address(**job.origin)
    question = (
        f"A question goes to {_place(target)}, and waits up to {question_timeout_seconds} s for an answer."
        if job.questions == "ask_target" else "A tool that asks a person is refused."
    )
    return (
        f'[trigger: schedule] Job "{job.name}" ({job.job_id}), session {session_id}, run {run.run_id}, '
        f"due {due.isoformat()} ({zone_name}), started {late} s late. "
        f"Created by user {job.created_by_user_id} in {_place(origin)}. "
        f"Nobody is waiting for this turn. {question} Your reply is posted to {_place(target)}."
    )
