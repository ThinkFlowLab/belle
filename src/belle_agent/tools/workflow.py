"""Platform-neutral limits and deadline-bounded tool operations."""

import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any


def bounded_int(value: Any, default: int, hard_max: int | None, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed if hard_max is None else min(parsed, hard_max))


def nonnegative_int(value: Any, default: int) -> int:
    """Accept zero and positive bounds. Use the default for invalid values."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def bounded_float(
    value: Any,
    default: float,
    hard_max: float | None,
    *,
    minimum: float = 0.001,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed) or parsed <= 0:
        parsed = default
    return max(minimum, parsed if hard_max is None else min(parsed, hard_max))


async def run_targets(
    ids: Sequence[str],
    call: Callable[[str], Awaitable[str | None]],
    remaining: Callable[[], float],
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Collect successes, error codes, and targets not attempted before expiry.

    The adapter translates expected failures to error codes and enforces the
    deadline during each call. Unexpected exceptions propagate.
    """
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []
    for index, target in enumerate(ids):
        if remaining() <= 0:
            return succeeded, failed, list(ids[index:])
        error = await call(target)
        if error is None:
            succeeded.append(target)
        else:
            failed.append({"id": target, "error": error})
    return succeeded, failed, []
