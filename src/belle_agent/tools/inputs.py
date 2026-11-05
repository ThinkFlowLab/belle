"""Validate model-supplied collections and ids without platform dependencies."""

import functools
import inspect
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The shape of one kind of id, and the form a refusal states for it.
IdForm = tuple[re.Pattern[str], str]


def iso_instant(value: Any) -> float | None:
    """Parse an ISO-8601 instant into seconds since the epoch, or ``None``. Dates and offset-free times use UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    if text[-1:] in {"z", "Z"}:
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def iso_utc(value: float | None) -> str | None:
    """Write seconds since the epoch as a UTC instant ending in ``Z``, or ``None`` for ``None``."""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def enum_values(value: Any, allowed: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Return accepted and rejected model-supplied enum values."""
    if value is None:
        return [], []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return [], [str(value)]
    accepted: list[str] = []
    rejected: list[str] = []
    for item in items:
        name = str(item or "").strip().lower()
        if not name:
            continue
        if name not in allowed:
            rejected.append(name)
        elif name not in accepted:
            accepted.append(name)
    return accepted, rejected


def string_ids(value: Any) -> list[str] | None:
    """Return trimmed ids, or ``None`` when the value is not an id list."""
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if text:
            ids.append(text)
    return ids


def object_list(value: Any) -> list[dict[str, Any]] | None:
    """Return a list of objects, or ``None`` for a malformed value."""
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        out.append(dict(item))
    return out


def id_error(
    arguments: Mapping[str, Any], kinds: Mapping[str, str], forms: Mapping[str, IdForm], platform: str
) -> tuple[str, str] | None:
    """Why an id argument does not hold an id of its kind, or ``None`` when each does.

    ``kinds`` maps an argument name to its id kind. An argument whose name ends in ``s`` holds a list
    of ids. ``forms`` maps a kind to its shape and its stated form. ``platform`` names the platform in
    the detail. An argument left empty is not checked: the tool decides what its absence means.
    """
    for name, value in arguments.items():
        kind = kinds.get(name)
        if kind is None or value is None or value == "":
            continue
        if name.endswith("s"):
            # The tool reports a value that is not a list of strings in its own words.
            values = [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
        else:
            values = [str(value)]
        shape, form = forms[kind]
        if any(not shape.match(item.strip()) for item in values):
            return f"{kind}_id_malformed", f"{name} is not a {platform} {kind} id. A {platform} {kind} id is {form}"
    return None


def checks_ids(
    kinds: Mapping[str, str], forms: Mapping[str, IdForm], platform: str, refuse: Callable[[str, str], str]
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Make a decorator that refuses a call with a malformed id argument, before the tool checks anything else.

    The arguments are those of ``id_error``. ``refuse(code, detail)`` is the refusal the tool returns.
    """

    def decorate(tool: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(tool)

        @functools.wraps(tool)
        async def checked(*args: Any, **kwargs: Any) -> Any:
            try:
                arguments = signature.bind(*args, **kwargs).arguments
            except TypeError:
                arguments = {}  # The tool raises its own error for arguments it does not take.
            if problem := id_error(arguments, kinds, forms, platform):
                logger.warning("%s %s refused: %s -- %s", platform.lower(), tool.__name__, *problem)
                return refuse(*problem)
            return await tool(*args, **kwargs)

        return checked

    return decorate
