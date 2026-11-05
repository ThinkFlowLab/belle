"""Extract deduplicated strings from bounded structured data."""

from collections.abc import Mapping
from typing import Any

MAX_TEXT_DEPTH = 12
MAX_TEXT_STRINGS = 200


def walk_strings_bounded(value: Any, keys: frozenset[str] | None) -> tuple[list[str], bool]:
    """Return reader-facing strings and whether a limit stopped the walk."""
    found: list[str] = []
    seen: set[str] = set()
    bounded = False

    def keep(text: str) -> None:
        stripped = text.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            found.append(stripped)

    def walk(node: Any, depth: int) -> None:
        nonlocal bounded
        if depth > MAX_TEXT_DEPTH:
            # Report the bound only when it hides content.
            if isinstance(node, str) or (isinstance(node, (Mapping, list, tuple)) and len(node) > 0):
                bounded = True
            return
        if isinstance(node, Mapping):
            for key, item in node.items():
                if len(found) >= MAX_TEXT_STRINGS:
                    bounded = True
                    return
                if isinstance(item, str):
                    if keys is None or str(key) in keys:
                        keep(item)
                else:
                    walk(item, depth + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                if len(found) >= MAX_TEXT_STRINGS:
                    bounded = True
                    return
                walk(item, depth + 1)
        elif isinstance(node, str) and keys is None:
            keep(node)

    walk(value, 0)
    return found, bounded


def walk_strings(value: Any, keys: frozenset[str] | None) -> list[str]:
    """Return the visible strings without the truncation flag."""
    found, _ = walk_strings_bounded(value, keys)
    return found
