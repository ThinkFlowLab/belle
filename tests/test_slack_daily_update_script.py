"""Checks for the packaged daily-update script."""

import runpy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/belle_agent/skills/slack-daily-update/scripts/daily_update.py"


def test_ownerless_rows_are_reported_as_unassigned() -> None:
    module = runpy.run_path(str(SCRIPT), run_name="daily_update_test")
    now = datetime.now(UTC)
    item = module["Item"]("1", "Unassigned task", [], "WIP", module["Moment"](now, False))

    reports = module["_member_reports"](
        [item],
        names={},
        wip_status="WIP",
        done_status="Done",
        cutoff=now - timedelta(hours=1),
        cutoff_day=(now - timedelta(hours=1)).date(),
    )

    assert [(report.name, report.started) for report in reports] == [("Unassigned", ["Unassigned task"])]


def test_date_cells_use_an_inclusive_calendar_day_cutoff() -> None:
    module = runpy.run_path(str(SCRIPT), run_name="daily_update_test")
    moment = module["_parse_moment"]({"date": ["2026-10-18"]})

    assert module["_is_recent"](
        moment,
        cutoff=datetime(2026, 10, 18, 23, tzinfo=UTC),
        cutoff_day=date(2026, 10, 18),
    )
