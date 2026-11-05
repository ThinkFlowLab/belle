"""Clock boundary for the scheduler and its tests."""

import asyncio
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Give UTC time and wait until one UTC instant."""

    def now(self) -> datetime: ...

    async def sleep_until(self, instant: datetime) -> None: ...


class SystemClock:
    """Use the system clock in production."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep_until(self, instant: datetime) -> None:
        await asyncio.sleep(max(0, (instant - self.now()).total_seconds()))


def iso_utc(value: datetime) -> str:
    """Format an instant as UTC with second precision."""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
