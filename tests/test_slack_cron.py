"""Portable scheduled-run presentation contract."""

from belle_agent.platforms.slack.cron import CronRecord, cron_status_record


def test_terminal_push_rewrites_placeholder_and_preserves_report():
    records = {("run-1", "C1"): CronRecord("run-1", "C1", "123.456")}
    push = cron_status_record(
        {"cron": {"run_id": "run-1", "status": "succeeded", "job_name": "Digest"}},
        "C1", "Finished report", records,
    )

    assert push is not None
    assert push.anchor_ts == "123.456"
    assert push.card == {
        "type": "task_card", "task_id": "cron-run-1", "title": "Digest", "status": "complete"
    }
    assert push.body_below_card is True
    assert records == {}
