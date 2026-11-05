"""Filter capability catalogs and describe their entries without transport markup."""

import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from typing import Any

GLOSS_COLUMNS = 70

# Remove asides before shortening skill descriptions.
_PARENTHETICAL = re.compile(r"\s*[(（][^()（）]*[)）]")
# Keep the first clause without splitting file extensions or abbreviations.
_CLAUSE_END = re.compile(
    r"(?<=[^\W\d_]{3})[.!?](?=\s|$)"
    r"|\s*;"
    r"|\s*[(（]"
    r"|\s+[—–-]\s"
    r"|\s*—"
    r"|[。；！？：]"
)
# Prefer a word or CJK list boundary when truncating a gloss.
_SOFT_BREAK = re.compile(r"\s|(?<=[、，])")


def scope_allows(name: str, available: Collection[str] | None, unavailable: Collection[str] = ()) -> bool:
    """Whether a scope lets a turn use ``name``.

    ``available`` lists what a turn may use, and ``None`` lists everything. ``unavailable`` removes names from it.
    """
    return (available is None or name in available) and name not in unavailable


def usable_skills(
    catalog: Iterable[Any], available: Iterable[str] | None, unavailable: Iterable[str] = ()
) -> list[Mapping[str, Any]]:
    """Keep installed, enabled skills allowed by scope, in catalog order.

    ``None`` for ``available`` allows every skill. ``unavailable`` removes skills.
    """
    allowed = None if available is None else frozenset(available)
    denied = frozenset(unavailable)
    usable: dict[str, Mapping[str, Any]] = {}
    for item in catalog:
        if not isinstance(item, Mapping):
            continue
        name = skill_name(item)
        if not name or name in usable:
            continue
        if item.get("installed") is not True or item.get("enabled") is False:
            continue
        if not scope_allows(name, allowed, denied):
            continue
        usable[name] = item
    return list(usable.values())


def skill_name(item: Mapping[str, Any]) -> str:
    return str(item.get("name") or "").strip()


def usable_subagents(catalog: Iterable[Any], available: Iterable[str] | None) -> list[str]:
    """Keep mounted subagent names allowed by this conversation's scope."""
    allowed = None if available is None else frozenset(available)
    return list(dict.fromkeys(
        name for item in catalog if isinstance(item, str)
        if (name := item.strip()) and scope_allows(name, allowed)
    ))


def find_name(names: Iterable[str], name: str) -> str:
    """Resolve a mounted name exactly, or by unambiguous case-insensitive match."""
    listed = list(names)
    if name in listed:
        return name
    folded = [candidate for candidate in listed if candidate.lower() == name.lower()]
    return folded[0] if len(folded) == 1 else ""


def _columns(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def gloss(description: str, columns: int = GLOSS_COLUMNS) -> str:
    """Shorten a skill description to its first clause and ``columns`` width."""
    text = _PARENTHETICAL.sub("", " ".join(str(description or "").split()))
    if not text:
        return ""
    end = _CLAUSE_END.search(text)
    clause = text[: end.start()].rstrip(" ,:") if end and end.start() > 0 else text
    if _columns(clause) <= columns:
        return clause
    width = 0
    fits = 0
    for index, char in enumerate(clause):
        width += _columns(char)
        if width > columns - 1:
            break
        fits = index + 1
    head = clause[:fits]
    breaks = [match.start() for match in _SOFT_BREAK.finditer(head)]
    # Use a word boundary only if it keeps most of the line.
    if breaks and breaks[-1] > fits // 2:
        head = head[: breaks[-1]]
    return head.rstrip(" ,:、，") + "…"


def find_skill(skills: list[Mapping[str, Any]], name: str) -> str:
    """Find an exact or unambiguous case-insensitive skill name."""
    return find_name((skill_name(item) for item in skills), name)
