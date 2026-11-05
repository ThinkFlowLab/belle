"""Find things by name in a platform's directory: how a name matches, which matches come first, and what was searched.

A platform supplies the records of each kind it names, and says which kinds
it could not read and which it read only in part. Matching, ordering, the
result cap and the coverage block are the same on every platform.
"""

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from belle_agent.tools.inputs import enum_values

#: The most matches one result lists. The count of every match is reported.
MAX_MATCHES = 20

#: Name decoration stripped from a query, never from a record.
QUERY_DECORATION = "#@:"

MATCH_EXACT = "exact"
MATCH_PREFIX = "prefix"
MATCH_CONTAINS = "contains"
#: Exact matches first when the result cap applies.
MATCH_RANK: Mapping[str, int] = {MATCH_EXACT: 0, MATCH_PREFIX: 1, MATCH_CONTAINS: 2}


def fold(value: Any) -> str:
    """Normalize Unicode and case without removing accents."""
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def match_type(query: str, candidate: Any) -> str:
    """Return the strongest match type of a folded query on one name, or an empty string."""
    folded = fold(candidate)
    if not folded:
        return ""
    if folded == query:
        return MATCH_EXACT
    if folded.startswith(query):
        return MATCH_PREFIX
    if query in folded:
        return MATCH_CONTAINS
    return ""


def best_match(query: str, fields: "list[tuple[str, Any]]") -> "tuple[str, str]":
    """Return the field and type of the strongest name match."""
    best_field = ""
    best_how = ""
    for name, value in fields:
        how = match_type(query, value)
        if not how:
            continue
        if not best_how or MATCH_RANK[how] < MATCH_RANK[best_how]:
            best_field, best_how = name, how
            if best_how == MATCH_EXACT:
                break
    return best_field, best_how


def matches_in(records: list[dict[str, Any]], query: str, fields: Sequence[str] | None) -> list[dict[str, Any]]:
    """The records whose name matches, each with ``match_type``, and ``matched`` where ``fields`` names several."""
    found: list[dict[str, Any]] = []
    for record in records:
        field, how = (
            best_match(query, [(name, record.get(name)) for name in fields])
            if fields
            else ("", match_type(query, record.get("name")))
        )
        if not how:
            continue
        match = dict(record)
        if field:
            match["matched"] = field
        match["match_type"] = how
        found.append(match)
    return found


def lookup_query(query: Any) -> "tuple[str, str]":
    """The query as given, without its decoration, and folded for matching. The folded form is empty for no name."""
    text = str(query or "").strip().strip(QUERY_DECORATION).strip()
    return text, fold(text)


#: The refusal of a lookup with no name.
QUERY_REQUIRED = ("query_required", "query is the name to look for and was empty.")


def lookup_kinds(value: Any, kinds: tuple[str, ...]) -> "tuple[tuple[str, ...], list[str]]":
    """Read one kind or a list of kinds. An absent or empty value selects all kinds."""
    if value is None:
        return kinds, []
    wanted, unknown = enum_values(value, kinds)
    if not wanted and not unknown:
        return kinds, []
    return tuple(wanted), unknown


def unknown_kind(unknown: list[str], kinds: tuple[str, ...]) -> "tuple[str, str]":
    """The refusal of a ``kind`` this platform does not name."""
    detail = (
        f"This platform names no such kind of thing: {', '.join(unknown)}. "
        f"Choose from {', '.join(kinds)}, or omit kind to search "
        "all of them."
    )
    return "unknown_kind", detail


def ordering(kinds: tuple[str, ...]) -> Callable[[Mapping[str, Any]], "tuple[int, int, int, str]"]:
    """Where one match sits in the returned list.

    Ordering is not selection: every match is counted and the count is
    reported. What the order decides is which survive the response-size cap,
    and there an exact match on a live account is a better thing to keep than
    a partial match on a deactivated one.
    """

    def key(match: Mapping[str, Any]) -> "tuple[int, int, int, str]":
        how = str(match.get("match_type") or MATCH_CONTAINS)
        kind = str(match.get("kind") or "")
        return (
            MATCH_RANK.get(how, len(MATCH_RANK)),
            0 if match.get("is_active") else 1,
            kinds.index(kind) if kind in kinds else len(kinds),
            fold(match.get("name")),
        )

    return key


def lookup_result(
    text: str,
    wanted: tuple[str, ...],
    kinds: tuple[str, ...],
    matches: list[dict[str, Any]],
    scanned: dict[str, Any],
    unavailable: dict[str, Any],
    incomplete: list[str],
    *,
    unavailable_note: str = "",
    scan_note: str = "",
    max_matches: int = MAX_MATCHES,
) -> dict[str, Any]:
    """The result of a lookup: the first ``max_matches`` matches in order, their count, and its coverage.

    The platform gives its own words for the kinds it could not read
    (``unavailable_note``) and for the kinds it read in part (``scan_note``).
    """
    matches = sorted(matches, key=ordering(kinds))
    match_count = len(matches)
    returned = matches[:max_matches]

    truncated = match_count > len(returned)
    warnings: list[str] = []
    partial_reasons: list[str] = []
    if incomplete:
        warnings.append("directory_scan_incomplete")
        partial_reasons.append("directory_scan_incomplete")
    if unavailable:
        warnings.append("some_kinds_unavailable")
        partial_reasons.append("kinds_not_granted_to_this_app")
    if truncated:
        warnings.append("more_matches_than_returned")
        partial_reasons.append("more_matches_than_returned")

    payload: dict[str, Any] = {
        "ok": True,
        "query": text,
        "kinds": list(wanted),
        "matches": returned,
        "match_count": match_count,
        "returned": len(returned),
        "truncated": truncated,
        "coverage": {
            "status": "partial" if partial_reasons else "complete",
            "scope_note": (
                "Coverage describes which kinds of thing were actually "
                "searched. A kind under unavailable was not searched at "
                "all, so an absence there is not evidence that the name "
                "does not exist."
            ),
            "kinds_searched": [one_kind for one_kind in wanted if one_kind not in unavailable],
            "kinds_unavailable": [one_kind for one_kind in wanted if one_kind in unavailable],
            "partial_reasons": partial_reasons,
            "scanned": scanned,
            "unavailable": unavailable,
            "warnings": warnings,
        },
    }
    if unavailable_note:
        payload["coverage"]["unavailable_note"] = unavailable_note
    if truncated:
        payload["coverage"]["truncation_note"] = (
            f"{match_count} things match this name and {len(returned)} are "
            "listed. The rest are not shown and nothing here chose between "
            "them; raise the result limit or narrow the query to see them."
        )
    if scan_note:
        payload["coverage"]["scan_note"] = scan_note
    return payload
