"""Deterministic clock for scheduler runner tests."""

import asyncio
from datetime import datetime, timedelta


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now
        self._waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._now

    async def sleep_until(self, instant: datetime) -> None:
        if self._now >= instant:
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((instant, future))
        try:
            await future
        finally:
            self._waiters = [(time, waiter) for time, waiter in self._waiters if waiter is not future]

    def advance(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)
        for instant, future in self._waiters:
            if instant <= self._now and not future.done():
                future.set_result(None)
