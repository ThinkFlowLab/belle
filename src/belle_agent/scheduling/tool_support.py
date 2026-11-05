"""Shared response and cursor boundaries for scheduling tools."""

import base64
import binascii
import functools
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.store import StoreBusy, StoreUnreadable
from belle_agent.tools.host import refusal


def success(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def answer(method: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    @functools.wraps(method)
    async def wrapped(*args: Any, **kwargs: Any) -> str:
        try:
            return await method(*args, **kwargs)
        except Refused as exc:
            return refusal(exc.code, exc.detail)
        except (StoreUnreadable, StoreBusy) as exc:
            return refusal("store_unreadable", str(exc))

    return wrapped


def positive(value: Any, field: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise Refused("invalid_arguments", f"{field} must be an integer of at least {minimum}")
    return value


def encode_cursor(*values: Any) -> str:
    data = json.dumps(values, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode_cursor(raw: Any, *, form: str, count: int, max_chars: int | None = None) -> list[Any]:
    if (not isinstance(raw, str) or (max_chars is not None and len(raw) > max_chars)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", raw)):
        raise Refused("invalid_arguments", form)
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        values = json.loads(data)
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise Refused("invalid_arguments", form) from exc
    if not isinstance(values, list) or len(values) != count:
        raise Refused("invalid_arguments", form)
    return values
