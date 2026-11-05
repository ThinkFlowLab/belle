"""Route tool calls to the installation that received the turn."""

import functools
from collections.abc import Callable, Mapping
from typing import Any


class RoutedToolkit:
    """Expose one toolkit interface while selecting an installation per call."""

    def __init__(
        self,
        first: Any,
        select: Callable[[str], Any | None],
        metadata_provider: Callable[[], Mapping[str, Any]],
        metadata_key: str,
        unknown: Callable[[str, str], str | None],
    ) -> None:
        self._first = first
        self._select = select
        self._metadata_provider = metadata_provider
        self._metadata_key = metadata_key
        self._unknown = unknown
        self.active_turn_only = getattr(first, "active_turn_only", frozenset())
        names = [definition["name"] for definition in first.get_tool_definitions()]
        if hasattr(first, "widening_question"):
            names.append("widening_question")
        for name in names:
            setattr(self, name, self._routed(name, getattr(first, name)))

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return self._first.get_tool_definitions()

    def _routed(self, name: str, signature_of: Any) -> Any:
        @functools.wraps(signature_of)
        async def call(*args: Any, **kwargs: Any) -> Any:
            installation = str(self._metadata_provider().get(self._metadata_key) or "").strip()
            kit = self._select(installation)
            if kit is None:
                return self._unknown(name, installation)
            return await getattr(kit, name)(*args, **kwargs)

        return call
