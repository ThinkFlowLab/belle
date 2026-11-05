"""Schedule input and recurrence contracts."""

from datetime import UTC, datetime, timedelta

import pytest

from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.when import Schedule, first_run, next_runs, parse_when, shortest_gap

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def parse(raw, **kwargs):
    return parse_when(raw, now=NOW, default_zone=None, **kwargs)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"kind": "once", "at": "2026-10-20T12:00Z", "seconds": 10}, "when_invalid"),
        ({"kind": "every", "seconds": 600, "expr": "0 * * * *"}, "when_invalid"),
        ({"kind": "cron", "expr": "0 * * * *", "at": "2026-10-20T12:00Z"}, "when_invalid"),
        ({"kind": "once", "at": "2026-10-20T12:00Z", "after_seconds": 10}, "when_invalid"),
        ({"kind": "every", "seconds": True}, "when_invalid"),
        ({"kind": "cron", "expr": "0 * * * *", "second": [0, 0]}, "when_invalid"),
        ({"kind": "cron", "expr": "0 * * * *"}, "zone_required"),
        ({"kind": "once", "at": "2026-10-20T12:00"}, "zone_required"),
        ({"kind": "once", "at": "2026-10-20T12:00Z", "zone": "No/Such_Zone"}, "zone_unknown"),
        ({"kind": "once", "at": "2026-10-20 12:00Z"}, "time_malformed"),
        ({"kind": "once", "at": "2026-10-20T12:00+03:00", "zone": "Europe/Paris"}, "time_zone_mismatch"),
        ({"kind": "once", "at": "2026-10-18T12:00Z"}, "time_in_past"),
        ({"kind": "cron", "expr": "0 0 30 2 *", "zone": "UTC"}, "cron_invalid"),
        ({"kind": "cron", "expr": "0 1 * * *", "zone": "UTC", "until": "2026-10-18T12:01Z"}, "schedule_never_runs"),
        ({"kind": "every", "seconds": 10}, "interval_too_short"),
        ({"kind": "once", "after_seconds": 10**20}, "when_invalid"),
    ],
)
def test_refusals(raw, code):
    with pytest.raises(Refused) as error:
        parse(raw)
    assert error.value.code == code
    assert error.value.detail


def test_checks_run_in_order_and_kind_detail_lists_fields():
    with pytest.raises(Refused) as error:
        parse({"kind": "once", "at": "bad", "expr": "bad"})
    assert error.value.code == "when_invalid"
    assert "after_seconds" in error.value.detail
    with pytest.raises(Refused) as error:
        parse({"kind": "cron", "expr": "bad"})
    assert error.value.code == "zone_required"
    assert "scheduling." not in error.value.detail


def test_default_zone_normalizes_once_to_an_instant():
    schedule, source = parse_when(
        {"kind": "once", "at": "2026-10-20T12:00"}, now=NOW, default_zone="Europe/Paris"
    )
    assert source == "deployment default"
    assert schedule.when == {"kind": "once", "at": "2026-10-20T10:00:00Z", "zone": "Europe/Paris"}
    assert first_run(Schedule(schedule.when), NOW) == datetime(2026, 10, 20, 10, tzinfo=UTC)


def test_default_zone_does_not_reject_an_explicit_instant():
    schedule, source = parse_when(
        {"kind": "once", "at": "2026-10-20T12:00Z"}, now=NOW, default_zone="Europe/Paris"
    )
    assert source == "deployment default"
    assert schedule.when == {"kind": "once", "at": "2026-10-20T12:00:00Z", "zone": "Europe/Paris"}


def test_after_seconds_uses_the_clock_and_every_survives_restart():
    once, _ = parse({"kind": "once", "after_seconds": 90})
    assert once.when == {"kind": "once", "at": "2026-10-18T12:01:30Z"}
    every, _ = parse({"kind": "every", "seconds": 3600})
    assert every.when["start"] == "2026-10-18T13:00:00Z"
    later = NOW + timedelta(hours=3, minutes=10)
    assert next_runs(Schedule(every.when), later, 2) == [NOW + timedelta(hours=4), NOW + timedelta(hours=5)]


def test_every_without_start_rounds_a_fractional_clock_up() -> None:
    now = NOW + timedelta(microseconds=1)
    every, _ = parse_when({"kind": "every", "seconds": 3600}, now=now, default_zone=None)
    assert every.when["start"] == "2026-10-18T13:00:01Z"
    assert first_run(every, now) == NOW + timedelta(hours=1, seconds=1)


def test_one_shot_can_be_more_than_one_calendar_cycle_away():
    schedule, _ = parse({"kind": "once", "at": "2500-01-01T00:00Z"})
    assert first_run(schedule, NOW) == datetime(2500, 1, 1, tzinfo=UTC)


def test_second_adds_to_each_cron_minute():
    schedule, _ = parse({"kind": "cron", "expr": "* * * * *", "second": [30, 0], "zone": "UTC"}, min_interval_seconds=30)
    assert next_runs(schedule, NOW, 4) == [
        NOW + timedelta(seconds=30),
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=1, seconds=30),
        NOW + timedelta(minutes=2),
    ]
    assert shortest_gap(schedule, NOW, horizon_days=1)[0] == timedelta(seconds=30)


def test_cron_invalid_field_count_and_macro_have_a_correction():
    for expr in ("0 0 2 * * *", "0 0 0 2 * * *", "@daily"):
        with pytest.raises(Refused) as error:
            parse({"kind": "cron", "expr": expr, "zone": "UTC"})
        assert error.value.code == "cron_invalid"
        assert "5 fields" in error.value.detail
        assert "second" in error.value.detail


def test_or_semantics_of_day_fields():
    schedule, _ = parse({"kind": "cron", "expr": "0 0 29 2 1", "zone": "UTC"})
    assert first_run(schedule, NOW) == datetime(2027, 2, 1, tzinfo=UTC)


def test_until_bounds_runs_and_interval_names_gap():
    schedule, _ = parse({"kind": "every", "seconds": 3600, "until": "2026-10-18T14:00Z"})
    assert next_runs(schedule, NOW, 4) == [NOW + timedelta(hours=1), NOW + timedelta(hours=2)]
    with pytest.raises(Refused) as error:
        parse({"kind": "cron", "expr": "* * * * *", "zone": "UTC", "second": [0, 15]})
    assert error.value.code == "interval_too_short"
    assert "45 seconds" in error.value.detail
    assert "300 seconds" in error.value.detail
    assert "scheduling." not in error.value.detail
