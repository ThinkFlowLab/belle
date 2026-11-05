"""Typed-decision requests with one retry deadline."""

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from belle_agent.typed_decision.adapters.base import DecisionAdapter
from belle_agent.typed_decision.schema import (
    Answer,
    Question,
    TypedDecisionError,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 4.0

#: Total attempts, including the first.
DEFAULT_ATTEMPTS = 2

RETRY_DELAY_SECONDS = 0.25


class TypedDecisionClient:
    """Ask an implementation a set of independent questions about a state."""

    def __init__(
        self,
        adapter: DecisionAdapter,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
        retry_delay: float = RETRY_DELAY_SECONDS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        self.adapter = adapter
        self.timeout = timeout
        self.attempts = attempts
        self.retry_delay = retry_delay

    @property
    def name(self) -> str:
        """The implementation answering, for a log line to name."""
        return getattr(self.adapter, "name", "unknown")

    async def decide(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Answer]:
        """Answer every question or raise :class:`TypedDecisionError`."""
        deadline = time.monotonic() + self.timeout
        last: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(self.adapter.ask(state, questions), timeout=remaining)
            except (TypedDecisionError, TimeoutError, OSError) as exc:
                last = exc
                logger.debug(
                    "[typed_decision] attempt %s of %s failed: adapter=%s error=%s",
                    attempt,
                    self.attempts,
                    self.name,
                    exc,
                )
            if attempt < self.attempts:
                pause = min(self.retry_delay, deadline - time.monotonic())
                if pause > 0:
                    await asyncio.sleep(pause)
        raise TypedDecisionError(f"no typed decision after {self.attempts} attempt(s) within {self.timeout}s: {last}")
