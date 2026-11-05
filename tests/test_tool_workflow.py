"""Platform-neutral tool limits and partial-operation collection."""

import pytest

from belle_agent.tools.workflow import bounded_float, bounded_int, nonnegative_int, run_targets


@pytest.mark.parametrize(("value", "expected"), [("invalid", 5), (None, 5), (-2, 1), (100, 10), (3, 3)])
def test_integer_bounds(value, expected):
    assert bounded_int(value, 5, 10) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, "invalid"])
def test_invalid_timeout_uses_default(value):
    assert bounded_float(value, 5, 10) == 5


def test_float_caps_and_zero_annotation_bounds():
    assert bounded_float(20, 5, 10) == 10
    assert bounded_float(0.0001, 5, 10) == 0.001
    assert nonnegative_int(0, 5) == 0
    assert nonnegative_int(-1, 5) == 5


async def test_target_results_preserve_order_and_stop_before_expired_calls():
    attempted = []

    async def call(target):
        attempted.append(target)
        return "denied" if target == "b" else None

    result = await run_targets(["a", "b", "c", "d"], call, lambda: 1 if len(attempted) < 2 else 0)
    assert result == (["a"], [{"id": "b", "error": "denied"}], ["c", "d"])
    assert attempted == ["a", "b"]


async def test_unexpected_target_error_propagates():
    async def call(target):
        raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError, match="unexpected"):
        await run_targets(["a"], call, lambda: 1)


async def test_empty_error_code_still_counts_as_failure():
    async def call(target):
        return ""

    assert await run_targets(["a"], call, lambda: 1) == ([], [{"id": "a", "error": ""}], [])
