"""Protocol for typed-decision adapters."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from belle_agent.typed_decision.schema import Answer, Question


@runtime_checkable
class DecisionAdapter(Protocol):
    """One way of answering a typed-decision request."""

    name: str

    async def ask(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Answer]: ...
