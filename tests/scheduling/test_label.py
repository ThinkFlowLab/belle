"""The run label tells the model when and where its answer goes."""

from dataclasses import replace

from belle_agent.scheduling.address import Address
from belle_agent.scheduling.label import run_label
from belle_agent.scheduling.records import ClockStart, Job, new_run


def test_label_shows_local_due_time_and_question_rule() -> None:
    origin = Address("slack", "team", "origin", "thread-1")
    target = Address("slack", "team", "target", "new")
    job = Job(
        "job-1234567890", "Reminder", "prompt", {"kind": "once", "zone": "Europe/Paris"},
        True, False, "", target.json(), "fresh", "skip", 1, "skip", "ask_target", {},
        origin.json(), "channel", "user-1", "", "2026-10-18T00:00:00Z", "user-1",
        "2026-10-18T00:00:00Z", 0, 0, "opencode",
    )
    run = new_run(
        started_by="clock", prompt="prompt", turn={}, questions="ask_target", place=target.json(),
        delivery="post_final", owner="", clock=ClockStart("2026-10-19T07:00:00Z", 42, 0, "job-1234567890:1"),
    )

    label = run_label(job, "ses-1234567890", run, target, question_timeout_seconds=30)

    assert label.startswith('[trigger: schedule] Job "Reminder" (job-1234567890)')
    assert "due 2026-10-19T09:00:00+02:00 (Europe/Paris), started 42 s late" in label
    assert "A question goes to a new thread in slack/team/target, and waits up to 30 s" in label
    assert f"session ses-1234567890, run {run.run_id}," in label
    job = replace(job, questions="refuse")
    assert "A tool that asks a person is refused" in run_label(job, "ses-1234567890", run, target, question_timeout_seconds=30)
