"""The window and the coverage block of a conversation read, as every platform's ``read_conversation`` states them."""

from collections.abc import Mapping, Sequence
from typing import Any

from belle_agent.tools.inputs import iso_instant, iso_utc

#: The most messages one read returns, and the number it returns where the call names none.
MAX_MESSAGES = 200
DEFAULT_MESSAGES = 50


class ReadWindowRefused(ValueError):
    """A read window argument that does not fit, with the refusal's code and detail."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def read_window(after_iso_utc: Any, max_messages: Any) -> tuple[float | None, int]:
    """Read the start instant and the message count of a read. Raise ``ReadWindowRefused`` for a value that does not fit."""
    after = None
    if after_iso_utc is not None and str(after_iso_utc).strip():
        after = iso_instant(after_iso_utc)
        if after is None:
            raise ReadWindowRefused(
                "after_iso_utc_must_be_an_iso_instant",
                "after_iso_utc is a UTC instant or a date, such as 2026-08-31T09:00:00Z or 2026-08-31",
            )
    if max_messages is not None and (
        isinstance(max_messages, bool) or not isinstance(max_messages, int) or not 1 <= max_messages <= MAX_MESSAGES
    ):
        raise ReadWindowRefused("max_messages_invalid", f"max_messages is a whole number from 1 to {MAX_MESSAGES}")
    return after, max_messages if max_messages is not None else DEFAULT_MESSAGES


def read_coverage(
    messages: Sequence[Mapping[str, Any]],
    *,
    more: bool,
    limit: int,
    next_before: str | None,
    after: float | None,
    inaccessible_before: float | None = None,
) -> dict[str, Any]:
    """What a read covered. ``messages`` are its records, oldest first, each with ``created_iso_utc``.

    ``more``: older messages remain, and ``next_before`` reads them. ``inaccessible_before``: the server keeps the
    messages before this instant out of reach.
    """
    coverage: dict[str, Any] = {
        "status": "partial" if more else "complete",
        "returned": len(messages),
        "max_messages": limit,
        "next_before_message_id": next_before if more else None,
        "earliest_message_iso_utc": messages[0]["created_iso_utc"] if messages else None,
        "latest_message_iso_utc": messages[-1]["created_iso_utc"] if messages else None,
    }
    if after is not None:
        coverage["after_iso_utc"] = iso_utc(after)
    if inaccessible_before:
        coverage["inaccessible_before_iso_utc"] = iso_utc(inaccessible_before)
    return coverage
