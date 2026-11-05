"""Minute-stepping cron oracle independent of cronsim."""

import calendar
from datetime import timedelta
from zoneinfo import ZoneInfo

MINUTE = timedelta(minutes=1)


def _values(field, low, high):
    values = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if base == "*":
            start, stop = low, high
        elif "-" in base:
            first, last = base.split("-", 1)
            start, stop = int(first), int(last)
        else:
            start = stop = int(base)
            if step_text:
                stop = high
        values.update(range(start, stop + 1, step))
    return values


def _compile(expr):
    minute, hour, day, month, weekday = expr.split()
    return (
        _values(minute, 0, 59),
        _values(hour, 0, 23),
        None if day == "L" else _values(day, 1, 31),
        _values(month, 1, 12),
        {value % 7 for value in _values(weekday, 0, 7)},
        day.startswith("*") or weekday.startswith("*"),
        not minute.startswith("*") and not hour.startswith("*"),
    )


def _matches(spec, local):
    minutes, hours, days, months, weekdays, day_and, _special = spec
    if local.minute not in minutes or local.hour not in hours:
        return False
    if local.month not in months:
        return False
    last_day = calendar.monthrange(local.year, local.month)[1]
    day_match = local.day == last_day if days is None else local.day in days
    dow_match = (local.weekday() + 1) % 7 in weekdays
    return (day_match and dow_match) if day_and else (day_match or dow_match)


def oracle_runs_many(expressions, zone_name, start, end):
    """Evaluate each ``(expr, seconds)`` at each real minute, including DST repair."""
    zone = ZoneInfo(zone_name)
    specs = [_compile(expr) for expr, _ in expressions]
    current = start.replace(second=0, microsecond=0)
    previous_wall = None
    results = [[] for _ in expressions]
    while current <= end:
        local = current.astimezone(zone)
        wall = local.replace(tzinfo=None)
        gap = []
        if previous_wall is not None and wall - previous_wall > MINUTE:
            gap = []
            missing = previous_wall + MINUTE
            while missing < wall:
                gap.append(missing)
                missing += MINUTE
        for index, (spec, (_expr, seconds)) in enumerate(zip(specs, expressions)):
            special = spec[-1]
            missed = special and any(_matches(spec, missing) for missing in gap)
            regular = (not special or not local.fold) and _matches(spec, local)
            if missed or regular:
                results[index].extend(current + timedelta(seconds=second) for second in seconds if current + timedelta(seconds=second) <= end)
        previous_wall = wall
        current += MINUTE
    return [sorted(result) for result in results]


def oracle_runs(expr, zone_name, start, end, seconds=(0,)):
    """Evaluate one expression at each real minute, including Debian DST repair."""
    return oracle_runs_many([(expr, seconds)], zone_name, start, end)[0]
