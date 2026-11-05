"""Locks and semaphores scoped to each event loop."""

import asyncio
from collections.abc import Callable, Hashable
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class LoopBound(Generic[T]):
    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._items: dict[Hashable, tuple[Any, T]] = {}

    def get(self, key: Hashable = None) -> T:
        loop = asyncio.get_running_loop()
        entry = self._items.get(key)
        if entry is None or entry[0] is not loop:
            entry = (loop, self._factory())
            self._items[key] = entry
        return entry[1]


__all__ = ["LoopBound"]
