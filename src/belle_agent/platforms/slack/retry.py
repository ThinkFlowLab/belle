"""Slack Web API retry timing."""

from collections.abc import Mapping
from typing import Any

MAX_RETRY_AFTER_SECONDS = 60.0
DEFAULT_RETRY_AFTER_SECONDS = 1.0
#: Error codes of a Web API call that the same call can succeed after.
TRANSIENT_ERRORS = frozenset({"ratelimited", "request_timeout", "service_unavailable", "internal_error", "fatal_error"})


def retry_after_seconds(exc: Exception) -> float | None:
    """Return the bounded Retry-After delay for a rate limit, or ``None``."""
    response = getattr(exc, "response", None)
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping):
        data = {}
    if getattr(response, "status_code", None) != 429 and data.get("error") != "ratelimited":
        return None
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        headers = data.get("headers")
    value: Any = None
    if isinstance(headers, Mapping):
        value = headers.get("Retry-After") or headers.get("retry-after")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return max(0.0, min(float(value), MAX_RETRY_AFTER_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
