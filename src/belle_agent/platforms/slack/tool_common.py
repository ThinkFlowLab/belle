"""Shared Slack response, timestamp, and credential helpers."""

import math
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from belle_agent.tools.inputs import iso_utc

#: The Slack tools import it from here.
_iso_utc = iso_utc

_SLACK_TOKEN_RE = re.compile(r"\bxox[a-z]-[A-Za-z0-9-]+\b", re.IGNORECASE)


_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|"
    r"bot[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|token|secret))"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def redact_credentials(value: Any, *, bot_token: str = "", cap: int | None = None) -> tuple[str, int, bool]:
    """Return safe text, substitution count, and whether ``cap`` truncated it."""
    text = str(value or "")
    redacted = 0

    if bot_token:
        occurrences = text.count(bot_token)
        if occurrences:
            text = text.replace(bot_token, "[REDACTED]")
            redacted += occurrences

    def replace(pattern: re.Pattern[str], replacement: str) -> None:
        nonlocal text, redacted
        text, count = pattern.subn(replacement, text)
        redacted += count

    replace(_SLACK_TOKEN_RE, "[REDACTED_SLACK_TOKEN]")
    replace(_BEARER_RE, "Bearer [REDACTED]")

    def replace_secret(match: re.Match[str]) -> str:
        nonlocal redacted
        redacted += 1
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    text = _SECRET_ASSIGNMENT_RE.sub(replace_secret, text)
    truncated = cap is not None and len(text) > cap
    if truncated:
        text = text[: cap - 1] + "…"  # type: ignore[operator]
    return text, redacted, truncated


class _SlackCallFailure(RuntimeError):
    """A Slack refusal with a safe code, method, and optional subject."""

    def __init__(self, code: str, method: str = "", *, subject: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.method = method
        self.subject = subject

    @property
    def where(self) -> str:
        """Name the method as Slack documents it."""
        method = self.method.replace("_", ".", 1) if self.method else "a Slack call"
        return f"{method} on {self.subject}" if self.subject else method


def _as_mapping(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    data = getattr(response, "data", None)
    if isinstance(data, Mapping):
        return dict(data)
    return {}


def _response_cursor(response: Mapping[str, Any]) -> str:
    """Return the next page cursor, or an empty string."""
    metadata = response.get("response_metadata")
    if not isinstance(metadata, Mapping):
        return ""
    return str(metadata.get("next_cursor") or "").strip()


class IncompleteMembers(RuntimeError):
    """Slack did not return a complete member list."""


async def conversation_members(
    call: Callable[..., Awaitable[dict[str, Any]]],
    channel_id: str,
    *,
    page_limit: int = 200,
    max_pages: int | None = None,
) -> frozenset[str]:
    """Collect complete membership, or refuse an incomplete page sequence."""
    members: set[str] = set()
    cursor = ""
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            raise IncompleteMembers("page_limit")
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": page_limit}
        if cursor:
            kwargs["cursor"] = cursor
        page = await call("conversations_members", **kwargs)
        pages += 1
        items = page.get("members")
        for member in items if isinstance(items, list) else []:
            identifier = str(member or "").strip()
            if identifier:
                members.add(identifier)
        cursor = _response_cursor(page)
        if page.get("has_more") and not cursor:
            raise IncompleteMembers("missing_cursor")
        if not cursor:
            return frozenset(members)
        if cursor in seen_cursors:
            raise IncompleteMembers("repeated_cursor")
        seen_cursors.add(cursor)


async def conversation_info(call: Callable[..., Awaitable[dict[str, Any]]], channel_id: str) -> dict[str, Any]:
    """Return the channel record from conversations.info."""
    response = await call("conversations_info", channel=channel_id)
    channel = response.get("channel")
    return dict(channel) if isinstance(channel, Mapping) else {}


def _cache_room(cache: dict[Any, tuple[float, Any]], limit: int) -> None:
    """Evict the oldest timestamped entry when the cache is full."""
    if len(cache) >= limit:
        del cache[min(cache, key=lambda key: cache[key][0])]


def _timestamp(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_timestamp(value: Any) -> float | None:
    """Return a finite instant after the epoch, or ``None``."""
    parsed = _timestamp(value)
    if parsed is None or not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _safe_error_code(value: Any, bot_token: str = "") -> str:
    """Reduce an SDK error to a credential-free, bounded identifier."""
    code = str(value or "slack_api_error")
    if bot_token:
        code = code.replace(bot_token, "redacted")
    code = _SLACK_TOKEN_RE.sub("redacted", code)
    code = _BEARER_RE.sub("bearer_redacted", code)
    code = re.sub(r"[^A-Za-z0-9_.:-]+", "_", code).strip("_")
    return (code or "slack_api_error")[:120]
