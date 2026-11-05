"""Settle a scheduled turn within both its origin and target scopes."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from belle_agent.backend import BackendCapabilities, Catalog, TurnScope
from belle_agent.capability_catalog import scope_allows, skill_name, usable_skills
from belle_agent.scheduling.address import Address
from belle_agent.scheduling.refusals import Refused
from belle_agent.scopes import LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY

_FIELDS = frozenset({"model", "skills", "expert", "subagents"})
_LEVELS = (LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY)


@dataclass(frozen=True)
class JobTurn:
    model: str = ""
    skills: tuple[str, ...] = ()
    expert: str = ""
    subagents: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ScopeLimits:
    place: Address
    scope: TurnScope


def parse_turn(raw: Any) -> JobTurn:
    """Read the four turn selections without coercing their types."""
    if not isinstance(raw, Mapping) or set(raw) - _FIELDS:
        raise Refused("invalid_arguments", "turn must be an object with only model, skills, expert and subagents")
    model, expert = raw.get("model", ""), raw.get("expert", "")
    if not isinstance(model, str) or not isinstance(expert, str):
        raise Refused("invalid_arguments", "turn.model and turn.expert must be strings")
    skills, subagents = raw.get("skills", []), raw.get("subagents")
    for field, value in (("skills", skills), ("subagents", subagents)):
        if field == "subagents" and value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(name, str) or not name for name in value):
            raise Refused("invalid_arguments", f"turn.{field} must be a list of nonempty strings")
    return JobTurn(model, tuple(skills), expert, None if subagents is None else tuple(subagents))


def _unknown(kind: str, name: str, choices: Sequence[str], backend: str) -> Refused:
    listed = ", ".join(choices)
    detail = (
        f"turn.{kind} names {name}, but {backend} lists {listed}"
        if listed else f"turn.{kind} names {name}, but {backend} lists no {kind}"
    )
    return Refused(f"{kind.rstrip('s')}_unknown", detail)


def check_catalog(
    turn: JobTurn,
    catalog: Catalog | None,
    capabilities: BackendCapabilities,
    *,
    packaged: Callable[[str], bool],
    supplies_skills: bool,
) -> None:
    """Refuse a selection absent from the backend's current catalog."""
    if turn.model and (catalog is None or not catalog.models.accepts(turn.model)):
        choices = catalog.models.names + catalog.models.aliases if catalog else ()
        raise _unknown("model", turn.model, choices, capabilities.backend)
    usable = usable_skills(catalog.skills, None) if catalog else []
    skill_names = tuple(skill_name(item) for item in usable)
    for name in turn.skills:
        if (
            "skills_required" not in capabilities.scope_fields
            or name not in skill_names
            or (supplies_skills and not packaged(name))
        ):
            raise _unknown("skills", name, skill_names, capabilities.backend)
    for field, selected, choices, capability in (
        ("expert", (turn.expert,) if turn.expert else (), catalog.experts if catalog else (), "expert"),
        ("subagents", turn.subagents or (), catalog.subagents if catalog else (), "subagents_available"),
    ):
        for name in selected:
            if capability not in capabilities.scope_fields or name not in choices:
                raise _unknown(field, name, choices, capabilities.backend)


def _intersection(left: frozenset[str] | None, right: frozenset[str] | None) -> frozenset[str] | None:
    if left is None:
        return right
    if right is None:
        return left
    return left & right


def _outside(field: str, side: ScopeLimits, allowed: str) -> Refused:
    place = side.place
    return Refused(
        "turn_out_of_scope",
        f"turn.{field} is outside {place.platform}/{place.workspace}/{place.conversation}; its scope allows {allowed}",
    )


def _choose(field: str, selected: str, origin: ScopeLimits, target: ScopeLimits) -> str:
    first, second = getattr(origin.scope, field), getattr(target.scope, field)
    for side, limit in ((origin, first), (target, second)):
        if selected and limit and selected != limit:
            raise _outside(field, side, limit)
    if not selected and first and second and first != second:
        raise _outside(field, target, second)
    return selected or first or second


def settle_run_scope(
    job: JobTurn,
    origin: ScopeLimits,
    target: ScopeLimits,
    *,
    refuse_questions: bool,
    system_additions: Sequence[str] = (),
) -> TurnScope:
    """Intersect the job and both scope limits for one run."""
    o, t = origin.scope, target.scope
    tools = {
        name: _LEVELS[max(_LEVELS.index(o.tools.get(name, LEVEL_ALLOW)), _LEVELS.index(t.tools.get(name, LEVEL_ALLOW)))]
        for name in o.tools.keys() | t.tools.keys()
    }
    if refuse_questions:
        tools = {name: LEVEL_DENY if level == LEVEL_ASK else level for name, level in tools.items()}
    available = _intersection(o.skills_available, t.skills_available)
    unavailable = o.skills_unavailable | t.skills_unavailable
    required = tuple(dict.fromkeys((*o.skills_required, *t.skills_required, *job.skills)))
    for name in required:
        for side in (origin, target):
            if not scope_allows(name, side.scope.skills_available, side.scope.skills_unavailable):
                allowed = ", ".join(sorted(side.scope.skills_available or ())) if side.scope.skills_available is not None else "all installed skills"
                if side.scope.skills_unavailable:
                    allowed += f" except {', '.join(sorted(side.scope.skills_unavailable))}"
                raise _outside("skills", side, allowed or "no skills")
    subagents = _intersection(o.subagents_available, t.subagents_available)
    if job.subagents is not None:
        for name in job.subagents:
            for side in (origin, target):
                if not scope_allows(name, side.scope.subagents_available):
                    raise _outside("subagents", side, ", ".join(sorted(side.scope.subagents_available or ())) or "no subagents")
        subagents = _intersection(subagents, frozenset(job.subagents))
    return TurnScope(
        tools=tools,
        skills_available=available,
        skills_unavailable=unavailable,
        skills_required=required,
        expert=_choose("expert", job.expert, origin, target),
        subagents_available=subagents,
        model=_choose("model", job.model, origin, target),
        system_additions=tuple(system_additions),
    )
