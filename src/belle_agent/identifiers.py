"""Read configured identifiers without changing their order."""

from typing import Any


def id_list(raw: Any) -> tuple[str, ...]:
    """Clean and deduplicate IDs; accept a single string as one ID."""
    if isinstance(raw, str):
        entry = raw.strip()
        return (entry,) if entry else ()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    entries = dict.fromkeys(str(item or "").strip() for item in raw)
    return tuple(entry for entry in entries if entry)
