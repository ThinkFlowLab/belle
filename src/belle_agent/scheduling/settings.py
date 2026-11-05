"""Validate Belle's scheduler settings at config load."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from belle_agent.config import BelleConfigError


def _default_delivery() -> list[dict[str, Any]]:
    return [{"to": {"same": ["platform", "workspace"]}}]


@dataclass(frozen=True)
class SchedulingSettings:
    """Operator limits and delivery policy for Belle's scheduler."""

    engine: str = "auto"
    max_jobs: int = 50
    min_interval_seconds: int = 300
    max_concurrent_runs: int = 4
    max_pending_sessions: int = 64
    job_visibility: str = "conversation"
    operator_role: str = ""
    default_zone: str = ""
    delivery: list[dict[str, Any]] = field(default_factory=_default_delivery)
    kept_runs_per_job: int = 20
    session_retention_hours: int = 72
    disable_after_failures: int = 3
    question_timeout_seconds: int = 900


_NUMBERS = {
    "max_jobs": (1, "a whole number of 1 or more"),
    "min_interval_seconds": (1, "a whole number of seconds, 1 or more"),
    "max_concurrent_runs": (1, "a whole number of 1 or more"),
    "max_pending_sessions": (1, "a whole number of 1 or more"),
    "kept_runs_per_job": (1, "a whole number of 1 or more"),
    "session_retention_hours": (1, "a whole number of hours, 1 or more"),
    "disable_after_failures": (0, "a whole number, 0 or more. 0 never pauses a job"),
    "question_timeout_seconds": (1, "a whole number of seconds, 1 or more"),
}
_ADDRESS_KEYS = frozenset({"platform", "workspace", "conversation"})


def _check_delivery(value: Any) -> None:
    if not isinstance(value, list):
        raise BelleConfigError("Belle config scheduling.delivery must be a list of rules")
    for index, rule in enumerate(value):
        prefix = f"Belle config scheduling.delivery[{index}]"
        if not isinstance(rule, Mapping) or "to" not in rule or set(rule) - {"to", "from"}:
            raise BelleConfigError(f"{prefix} must be a mapping with to and optional from")
        for side in ("from", "to"):
            if side not in rule:
                continue
            part = rule[side]
            allowed = _ADDRESS_KEYS | ({"same"} if side == "to" else set())
            if not isinstance(part, Mapping) or set(part) - allowed:
                raise BelleConfigError(f"{prefix}.{side} must be a mapping of {', '.join(sorted(allowed))}")
            for key, item in part.items():
                if key == "same":
                    if not isinstance(item, list) or any(not isinstance(name, str) or name not in _ADDRESS_KEYS for name in item) or len(set(item)) != len(item):
                        raise BelleConfigError(f"{prefix}.to.same must list distinct platform, workspace or conversation names")
                elif not isinstance(item, str):
                    raise BelleConfigError(f"{prefix}.{side}.{key} must be a string")


def scheduling_settings(config: Mapping[str, Any]) -> SchedulingSettings:
    """Read and validate the optional scheduling section."""
    section = config.get("scheduling", {})
    if not isinstance(section, Mapping):
        raise BelleConfigError("Belle config scheduling must be a mapping")
    if "deleted_job_history_days" in section:
        raise BelleConfigError(
            "Belle config scheduling.deleted_job_history_days is no longer read. "
            "A deleted job's sessions follow scheduling.session_retention_hours. Remove the key"
        )
    known = {item.name for item in fields(SchedulingSettings)}
    unknown = set(section) - known
    if unknown:
        key = min(unknown, key=str)
        raise BelleConfigError(f"Belle config scheduling.{key} is not a setting. Settings: {', '.join(sorted(known))}")
    engine = section.get("engine", "auto")
    if not isinstance(engine, str) or engine not in {"auto", "belle", "host", "disabled"}:
        raise BelleConfigError("Belle config scheduling.engine must be auto, belle, host or disabled. An unquoted off is read as false: write disabled")
    visibility = section.get("job_visibility", "conversation")
    if not isinstance(visibility, str) or visibility not in {"conversation", "all"}:
        raise BelleConfigError("Belle config scheduling.job_visibility must be conversation or all")
    defaults = SchedulingSettings()
    for key, (minimum, description) in _NUMBERS.items():
        value = section.get(key, getattr(defaults, key))
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise BelleConfigError(f"Belle config scheduling.{key} must be {description}")
    role = section.get("operator_role", "")
    roles = config.get("roles") or {}
    if not isinstance(role, str):
        raise BelleConfigError("Belle config scheduling.operator_role must be empty or a name in roles:")
    if role and (not isinstance(roles, Mapping) or role not in roles):
        names = ", ".join(sorted(str(name) for name in roles)) if isinstance(roles, Mapping) else "none"
        raise BelleConfigError(f"Belle config scheduling.operator_role names {role!r}, which is not a role in roles:. Roles: {names}")
    zone = section.get("default_zone", "")
    if not isinstance(zone, str):
        raise BelleConfigError("Belle config scheduling.default_zone must be an IANA zone name, such as Europe/Paris")
    if zone:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise BelleConfigError(f"Belle config scheduling.default_zone must be an IANA zone name, such as Europe/Paris. {zone!r} is not in this host's time zone data") from None
    delivery = section.get("delivery", _default_delivery())
    _check_delivery(delivery)
    return SchedulingSettings(**dict(section))


def validate_scheduling(config: Mapping[str, Any]) -> None:
    """Refuse an invalid scheduling section."""
    scheduling_settings(config)
