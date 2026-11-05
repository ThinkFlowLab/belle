"""Parse schedules and find their due instants."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

from belle_agent.scheduling.refusals import Refused

TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?$")
FIELDS = {
    "once": frozenset({"kind", "at", "after_seconds", "zone", "dst"}),
    "every": frozenset({"kind", "seconds", "start", "zone", "dst", "until", "max_runs"}),
    "cron": frozenset({"kind", "expr", "second", "zone", "dst", "until", "max_runs"}),
}


@dataclass(frozen=True)
class Schedule:
    """A validated, stored ``when`` mapping."""

    when: dict[str, Any]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must have a UTC offset")
    return value.astimezone(UTC)


def _zone(raw: str | None) -> ZoneInfo | None:
    if raw is None:
        return None
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise Refused("zone_unknown", f"zone must name an installed IANA time zone; got {raw!r}") from exc


def _time(
    value: str,
    field: str,
    zone: ZoneInfo | None,
    dst: str | None,
    *,
    verify_offset: bool = True,
) -> datetime:
    if not TIME_RE.fullmatch(value):
        raise Refused("time_malformed", f"{field} must be YYYY-MM-DDTHH:MM[:SS], with optional Z or ±HH:MM")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Refused("time_malformed", f"{field} must be a valid YYYY-MM-DDTHH:MM[:SS] time") from exc
    if parsed.tzinfo is not None:
        instant = _utc(parsed)
        if verify_offset and zone is not None and parsed.utcoffset() != instant.astimezone(zone).utcoffset():
            raise Refused("time_zone_mismatch", f"{field} offset does not match zone {zone.key} at that instant")
        return instant
    if zone is None:
        raise Refused("zone_required", f"{field} is a wall time and needs zone: no time zone was given and this deployment sets none")
    alternatives = [parsed.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    if alternatives[0] != alternatives[1]:
        actual = [candidate.astimezone(zone).replace(tzinfo=None) == parsed for candidate in alternatives]
        code = "local_time_ambiguous" if all(actual) else "local_time_nonexistent"
        ordered = sorted(alternatives)
        if dst is None:
            first, second = (instant.isoformat().replace("+00:00", "Z") for instant in ordered)
            condition = "falls in a DST gap" if code == "local_time_nonexistent" else "occurs twice"
            raise Refused(
                code,
                f"{field} in zone {zone.key} {condition}; dst earlier selects {first}, dst later selects {second}",
            )
        return ordered[0 if dst == "earlier" else 1]
    return alternatives[0]


def _seconds(raw: Any) -> list[int]:
    values = raw if isinstance(raw, list) else [raw]
    valid = values and all(type(value) is int and 0 <= value <= 59 for value in values)
    if not valid or len(set(values)) != len(values):
        raise Refused(
            "when_invalid",
            "second must be an integer from 0 to 59 or a non-empty list of distinct such integers",
        )
    return sorted(values)


def _now_detail(now: datetime, zone: ZoneInfo | None) -> str:
    detail = f"now is {now.isoformat().replace('+00:00', 'Z')} UTC"
    return f"{detail} ({now.astimezone(zone).isoformat()} in {zone.key})" if zone else detail


def parse_when(
    raw: Any,
    *,
    now: datetime,
    default_zone: str | None,
    min_interval_seconds: int = 300,
) -> tuple[Schedule, str]:
    """Validate ``when`` and return its stored form and zone source."""
    now = _utc(now)
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or raw["kind"] not in FIELDS:
        raise Refused("when_invalid", "when must be an object with kind once, every, or cron")
    kind = raw["kind"]
    allowed = FIELDS[kind]
    if set(raw) - allowed:
        raise Refused("when_invalid", f"when with kind {kind} accepts only {', '.join(sorted(allowed))}")
    if kind == "once" and ("at" in raw) == ("after_seconds" in raw):
        raise Refused("when_invalid", "when with kind once needs exactly one of at or after_seconds")
    required = {"every": "seconds", "cron": "expr"}.get(kind)
    if required and required not in raw:
        raise Refused("when_invalid", f"when with kind {kind} needs {required}")
    for field in ("after_seconds", "seconds", "max_runs"):
        if field in raw and (type(raw[field]) is not int or raw[field] < 1):
            raise Refused("when_invalid", f"{field} must be a positive integer")
        if field in ("after_seconds", "seconds") and field in raw:
            try:
                timedelta(seconds=raw[field])
            except OverflowError as exc:
                raise Refused("when_invalid", f"{field} exceeds the supported datetime range") from exc
    for field in ("at", "start", "until", "expr", "zone"):
        if field in raw and (not isinstance(raw[field], str) or not raw[field]):
            raise Refused("when_invalid", f"{field} must be a non-empty string")
    if "dst" in raw and raw["dst"] not in ("earlier", "later"):
        raise Refused("when_invalid", "dst must be earlier or later")
    if "second" in raw:
        _seconds(raw["second"])

    wall_field = next(
        (
            field
            for field in ("at", "start", "until")
            if field in raw and not raw[field].endswith("Z") and not re.search(r"[+-]\d{2}:\d{2}$", raw[field])
        ),
        None,
    )
    zone_name = raw.get("zone") or default_zone or None
    if zone_name is None and (kind == "cron" or wall_field):
        raise Refused("zone_required", f"{wall_field or 'cron'} needs zone: no time zone was given and this deployment sets none")
    zone = _zone(zone_name)
    zone_source = "deployment default" if "zone" not in raw and zone_name is not None else ""
    result = dict(raw)
    if zone_source:
        result["zone"] = zone_name
    parsed_times = {
        field: _time(raw[field], field, zone, raw.get("dst"), verify_offset="zone" in raw)
        for field in ("at", "start", "until")
        if field in raw
    }
    if kind == "cron":
        expr = raw["expr"]
        if len(expr.split()) != 5:
            raise Refused(
                "cron_invalid",
                "expr must have 5 fields: minute hour day-of-month month day-of-week; "
                "use second for seconds (@daily is 0 0 * * *)",
            )
        try:
            CronSim(expr, now.astimezone(zone))
        except (CronSimError, ValueError) as exc:
            raise Refused("cron_invalid", f"expr must be a valid 5-field cron expression: {exc}") from exc
    if kind == "once":
        if "after_seconds" in raw:
            try:
                parsed_times["at"] = now + timedelta(seconds=raw["after_seconds"])
            except OverflowError as exc:
                raise Refused("when_invalid", "after_seconds exceeds the supported datetime range from now") from exc
        if parsed_times["at"] <= now:
            raise Refused("time_in_past", f"at must be after {_now_detail(now, zone)}")
        result = {"kind": "once", "at": parsed_times["at"].isoformat().replace("+00:00", "Z")}
        if zone_name is not None:
            result["zone"] = zone_name
    elif kind == "every" and "start" not in raw:
        try:
            start = now + timedelta(seconds=raw["seconds"])
            # Stored times have whole seconds; round up so the first run never precedes now plus seconds.
            if start.microsecond:
                start = start.replace(microsecond=0) + timedelta(seconds=1)
            result["start"] = start.isoformat().replace("+00:00", "Z")
        except OverflowError as exc:
            raise Refused("when_invalid", "seconds exceeds the supported datetime range from now") from exc
    if "until" in parsed_times and parsed_times["until"] <= now:
        raise Refused("time_in_past", f"until must be after {_now_detail(now, zone)}")
    schedule = Schedule(result)
    if first_run(schedule, now) is None:
        raise Refused("schedule_never_runs", "when has no run after now and before until")
    if kind == "every" and raw["seconds"] < min_interval_seconds:
        raise Refused(
            "interval_too_short",
            f"seconds is {raw['seconds']}; runs of one job must be at least {min_interval_seconds} seconds apart",
        )
    if kind == "cron":
        gap = shortest_gap(schedule, now, minimum_seconds=min_interval_seconds)
        if gap is not None and gap[0].total_seconds() < min_interval_seconds:
            local = gap[1].astimezone(zone).isoformat()
            raise Refused(
                "interval_too_short",
                f"when: runs of one job must be at least {min_interval_seconds} seconds apart; "
                f"the shortest gap here is {gap[0].total_seconds():g} seconds at {gap[1].isoformat()} UTC ({local})",
            )
    return schedule, zone_source


def _limit(when: dict[str, Any], zone: ZoneInfo | None) -> datetime | None:
    if "until" not in when:
        return None
    return _time(when["until"], "until", zone, when.get("dst"), verify_offset=False)


def _cron_runs(when: dict[str, Any], after: datetime, end: datetime) -> Iterator[datetime]:
    zone = ZoneInfo(when["zone"])
    seconds = _seconds(when.get("second", 0))
    cursor = after - timedelta(minutes=1)
    while cursor < end:
        try:
            simulator = CronSim(when["expr"], cursor.astimezone(zone))
            while True:
                minute = next(simulator).astimezone(UTC)
                if minute > end:
                    return
                for second in seconds:
                    due = minute + timedelta(seconds=second)
                    if after < due <= end:
                        yield due
                cursor = minute
        except StopIteration:
            # cronsim searches 50 years per iterator; a Gregorian cycle spans 400.
            cursor = min(cursor + timedelta(days=49 * 365), end)


def next_runs(schedule: Schedule, after: datetime, count: int) -> list[datetime]:
    """Return up to ``count`` UTC due instants strictly after ``after``."""
    after = _utc(after)
    if type(count) is not int or count < 0:
        raise ValueError("count must be a non-negative integer")
    if count == 0:
        return []
    when = schedule.when
    zone = ZoneInfo(when["zone"]) if "zone" in when else None
    until = _limit(when, zone)
    if when["kind"] == "once":
        due = _utc(datetime.fromisoformat(when["at"]))
        return [due] if after < due and (until is None or due <= until) else []
    if when["kind"] == "every":
        start = _time(when["start"], "start", zone, when.get("dst"), verify_offset=False)
        step = timedelta(seconds=when["seconds"])
        number = max(0, (after - start) // step + 1)
        try:
            first = start + number * step
        except OverflowError:
            return []
        result = []
        for index in range(count):
            try:
                due = first + index * step
            except OverflowError:
                break
            if until is not None and due > until:
                break
            result.append(due)
        return result
    try:
        end = after + timedelta(days=146097)
    except OverflowError:
        end = datetime.max.replace(tzinfo=UTC)
    if until is not None:
        end = min(end, until)
    result = []
    for due in _cron_runs(when, after, end):
        result.append(due)
        if len(result) == count:
            break
    return result


def first_run(schedule: Schedule, now: datetime) -> datetime | None:
    """Find the first due instant in one Gregorian cycle, or before ``until``."""
    runs = next_runs(schedule, now, 1)
    return runs[0] if runs else None


def shortest_gap(
    schedule: Schedule,
    now: datetime,
    horizon_days: int = 366,
    *,
    minimum_seconds: int | None = None,
) -> tuple[timedelta, datetime] | None:
    """Find the shortest next-run gap and its later instant within the horizon."""
    when = schedule.when
    now = _utc(now)
    if when["kind"] == "once":
        return None
    if when["kind"] == "every":
        first = first_run(schedule, now)
        return (timedelta(seconds=when["seconds"]), first + timedelta(seconds=when["seconds"])) if first else None
    end = now + timedelta(days=horizon_days)
    zone = ZoneInfo(when["zone"])
    until = _limit(when, zone)
    if until is not None:
        end = min(end, until)
    previous = None
    best: tuple[timedelta, datetime] | None = None
    for due in _cron_runs(when, now, end):
        if previous is not None:
            gap = due - previous
            if best is None or gap < best[0]:
                best = (gap, due)
            if minimum_seconds is not None and gap.total_seconds() < minimum_seconds:
                return gap, due
        previous = due
    return best
