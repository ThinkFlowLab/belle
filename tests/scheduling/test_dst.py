"""Zone and Debian-cron daylight-saving behavior."""

from datetime import UTC, datetime

import pytest

from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.when import next_runs, parse_when

PARIS = "Europe/Paris"


@pytest.mark.parametrize(
    ("wall", "code", "earlier", "later"),
    [
        ("2026-03-29T02:30", "local_time_nonexistent", "2026-03-29T00:30:00+00:00", "2026-03-29T01:30:00+00:00"),
        ("2026-10-25T02:30", "local_time_ambiguous", "2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00"),
    ],
)
def test_gap_and_overlap_need_dst_and_select_the_named_instant(wall, code, earlier, later):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    raw = {"kind": "once", "at": wall, "zone": PARIS}
    with pytest.raises(Refused) as error:
        parse_when(raw, now=now, default_zone=None)
    assert error.value.code == code
    assert earlier.replace("+00:00", "Z") in error.value.detail
    assert later.replace("+00:00", "Z") in error.value.detail
    for choice, expected in (("earlier", earlier), ("later", later)):
        schedule, _ = parse_when({**raw, "dst": choice}, now=now, default_zone=None)
        assert schedule.when == {"kind": "once", "at": expected.replace("+00:00", "Z"), "zone": PARIS}
        assert next_runs(schedule, now, 1) == [datetime.fromisoformat(expected)]


def test_fixed_cron_has_one_gap_and_one_overlap_run_with_seconds():
    now = datetime(2026, 3, 28, tzinfo=UTC)
    schedule, _ = parse_when({"kind": "cron", "expr": "0 2 * * *", "second": 15, "zone": PARIS}, now=now, default_zone=None)
    assert next_runs(schedule, datetime(2026, 3, 29, 0, tzinfo=UTC), 1) == [datetime(2026, 3, 29, 1, 0, 15, tzinfo=UTC)]
    assert next_runs(schedule, datetime(2026, 10, 25, 0, tzinfo=UTC), 2) == [
        datetime(2026, 10, 25, 0, 0, 15, tzinfo=UTC),
        datetime(2026, 10, 26, 1, 0, 15, tzinfo=UTC),
    ]


def test_star_minute_with_fixed_hour_follows_real_time():
    now = datetime(2026, 3, 28, tzinfo=UTC)
    schedule, _ = parse_when({"kind": "cron", "expr": "*/30 2 * * *", "zone": PARIS}, now=now, default_zone=None)
    spring = next_runs(schedule, datetime(2026, 3, 29, 0, tzinfo=UTC), 1)
    assert spring == [datetime(2026, 3, 30, 0, tzinfo=UTC)]
    autumn = next_runs(schedule, datetime(2026, 10, 24, 23, tzinfo=UTC), 4)
    assert autumn == [
        datetime(2026, 10, 25, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
    ]


def test_lord_howe_half_hour_overlap_runs_both_passes():
    now = datetime(2026, 4, 1, tzinfo=UTC)
    schedule, _ = parse_when(
        {"kind": "cron", "expr": "*/10 * * * *", "zone": "Australia/Lord_Howe"},
        now=now,
        default_zone=None,
    )
    after = datetime(2026, 4, 4, 14, 20, tzinfo=UTC)
    assert next_runs(schedule, after, 6) == [
        datetime(2026, 4, 4, 14, 30, tzinfo=UTC),
        datetime(2026, 4, 4, 14, 40, tzinfo=UTC),
        datetime(2026, 4, 4, 14, 50, tzinfo=UTC),
        datetime(2026, 4, 4, 15, 0, tzinfo=UTC),
        datetime(2026, 4, 4, 15, 10, tzinfo=UTC),
        datetime(2026, 4, 4, 15, 20, tzinfo=UTC),
    ]


@pytest.mark.parametrize("expr", ["*/30 2 * * *", "0 */2 * * *"])
@pytest.mark.parametrize("second", [0, 30])
@pytest.mark.xfail(strict=True, reason="cronsim 2.7 skips 02:00 after Lord Howe's 30-minute rollback")
def test_lord_howe_real_time_cron_runs_at_two_after_rollback(expr, second):
    now = datetime(2026, 4, 1, tzinfo=UTC)
    schedule, _ = parse_when(
        {"kind": "cron", "expr": expr, "second": second, "zone": "Australia/Lord_Howe"},
        now=now,
        default_zone=None,
    )
    after = datetime(2026, 4, 4, 14, 0, tzinfo=UTC)
    assert next_runs(schedule, after, 1) == [datetime(2026, 4, 4, 15, 30, second, tzinfo=UTC)]


def test_dst_gap_can_create_a_shorter_gap():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    raw = {"kind": "cron", "expr": "59 2,3 * * *", "zone": PARIS}
    with pytest.raises(Refused) as error:
        parse_when(raw, now=now, default_zone=None, min_interval_seconds=3600)
    assert error.value.code == "interval_too_short"
    assert "3540 seconds" in error.value.detail
    assert "2026-03-29" in error.value.detail
