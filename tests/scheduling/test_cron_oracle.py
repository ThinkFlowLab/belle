"""Compare the cron planner with a separate minute-stepping evaluator."""

from datetime import UTC, datetime, timedelta
from itertools import zip_longest

import pytest

from belle_agent.scheduling.when import Schedule, next_runs

from .cron_oracle import oracle_runs_many

EXPRESSIONS = [
    "0 2 * * *",
    "30 2 * * *",
    "*/30 2 * * *",
    "*/10 * * * *",
    "0 * * * *",
    "0 */2 * * *",
    "0 1-3 * * *",
    "30 2,3 * * *",
    "15 1 * * 0",
    "0 0 29 2 1",
    "45 23 L * *",
]


@pytest.mark.parametrize("zone", ["Europe/Paris", "Australia/Lord_Howe"])
def test_planner_matches_the_oracle_over_2026_and_2027(zone):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2028, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
    cases = [
        (expr, seconds)
        for expr in EXPRESSIONS
        for seconds in ((0,), (0, 30))
        if zone != "Australia/Lord_Howe" or expr not in ("*/30 2 * * *", "0 */2 * * *")
    ]
    expected = oracle_runs_many(cases, zone, start, end)
    mismatches = []
    for (expr, seconds), oracle in zip(cases, expected):
        when = {"kind": "cron", "expr": expr, "second": list(seconds), "zone": zone, "until": end.isoformat().replace("+00:00", "Z")}
        actual = next_runs(Schedule(when), start - timedelta(seconds=1), 1_000_000)
        if actual != oracle:
            first_difference = next((pair for pair in zip_longest(actual, oracle) if pair[0] != pair[1]), None)
            mismatches.append((expr, seconds, len(actual), len(oracle), first_difference))
    assert not mismatches, (zone, mismatches)
