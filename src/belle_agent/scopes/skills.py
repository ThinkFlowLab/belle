"""Per-turn Skill catalogs and required instructions, independent of the harness."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

_AVAILABLE: ContextVar[frozenset[str] | None] = ContextVar("agent_skills_available", default=None)
_UNAVAILABLE: ContextVar[frozenset[str]] = ContextVar("agent_skills_unavailable", default=frozenset())
_INSTALLED: ContextVar[Callable[[], Iterable[str]] | None] = ContextVar("agent_skills_installed", default=None)
_REQUIRED: ContextVar[tuple[str, ...]] = ContextVar("agent_skills_required", default=())
_REQUIRED_CONTENT: ContextVar[str] = ContextVar("agent_skills_required_content", default="")

def _valid_skill_names(value: Any, *, allow_tuple: bool = False) -> bool:
    accepted = (list, tuple) if allow_tuple else list
    return isinstance(value, accepted) and all(
        isinstance(name, str) and bool(name.strip()) and name == name.strip()
        for name in value
    )


def bind_scoped_skills(
    params: dict[str, Any], installed: Callable[[], Iterable[str]] | None = None
) -> tuple[Token, ...]:
    """Bind per-turn Skill settings; an absent catalog inherits all.

    ``installed`` gives the names of the installed Skills. Without
    ``agent_skills_available``, ``agent_skills_unavailable`` removes Skills from it.
    """
    has_available = "agent_skills_available" in params
    raw_available = params.get("agent_skills_available")
    raw_unavailable = params.get("agent_skills_unavailable", ())
    raw_required = params.get("agent_skills_required", ())
    if has_available and not _valid_skill_names(raw_available):
        raise ValueError("agent_skills_available must be a list of Skill names")
    if not _valid_skill_names(raw_unavailable, allow_tuple=True):
        raise ValueError("agent_skills_unavailable must be a list of Skill names")
    if not _valid_skill_names(raw_required, allow_tuple=True):
        raise ValueError("agent_skills_required must be a list of Skill names")
    available = frozenset(raw_available) if has_available else None
    unavailable = frozenset(raw_unavailable)
    required = tuple(dict.fromkeys(raw_required))
    if available is not None:
        missing = [name for name in required if name not in available]
        if missing:
            raise ValueError(
                "Required Skills are outside agent_skills_available: "
                + ", ".join(missing)
            )
    denied = [name for name in required if name in unavailable]
    if denied:
        raise ValueError(
            "agent_skills_required and agent_skills_unavailable both name: "
            + ", ".join(denied)
        )
    return (
        _AVAILABLE.set(available),
        _UNAVAILABLE.set(unavailable),
        _INSTALLED.set(installed),
        _REQUIRED.set(required),
        _REQUIRED_CONTENT.set(""),
    )


def reset_scoped_skills(tokens: tuple[Token, ...]) -> None:
    for token in reversed(tokens):
        token.var.reset(token)


def scoped_skill_names() -> frozenset[str] | None:
    """Return the active Skill catalog. ``None`` leaves every Skill available."""
    available, unavailable = _AVAILABLE.get(), _UNAVAILABLE.get()
    if not unavailable:
        return available
    if available is None:
        installed = _INSTALLED.get()
        if installed is None:
            raise ValueError("agent_skills_unavailable needs the names of the installed Skills, and none were given")
        available = frozenset(installed())
    return available - unavailable


def required_skill_text() -> str:
    return _REQUIRED_CONTENT.get()


@contextmanager
def unrestricted_skill_inventory() -> Iterator[None]:
    """Build the session inventory without the first request's scope."""
    tokens = (_AVAILABLE.set(None), _UNAVAILABLE.set(frozenset()))
    try:
        yield
    finally:
        reset_scoped_skills(tokens)
