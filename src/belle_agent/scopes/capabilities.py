"""Register each channel's supported scope axes and settings."""

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover - avoid a runtime import cycle
    from belle_agent.scopes.schema import ScopeMatch

logger = logging.getLogger(__name__)

ValueValidator = Callable[[Any], "str | None"]

ValueCaution = Callable[[Any, "ScopeMatch"], "str | None"]

Warn = Callable[..., None]

_FrozenValue = TypeVar("_FrozenValue")


def check_agent_names(value: Any) -> str | None:
    if not isinstance(value, (list, tuple)):
        return f"={value!r} is not a list of agent or skill names"
    if any(
        not isinstance(name, str) or not name or name != name.strip()
        or name.startswith(("+", "-"))
        for name in value
    ):
        return f"={value!r} must contain non-empty names without surrounding spaces or +/- prefixes"
    if len(set(value)) != len(value):
        return f"={value!r} repeats a name"
    return None


def check_expert(value: Any) -> str | None:
    if isinstance(value, str) and value == "":
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return f"={value!r} must be one expert package name, or an empty string to clear the inherited expert"
    return None


SKILL_SETTINGS = ("available", "unavailable", "required")


def check_skills(value: Any) -> str | None:
    if not isinstance(value, Mapping) or not value:
        return f"={value!r} must be a mapping with available, unavailable or required"
    unknown = set(value) - set(SKILL_SETTINGS)
    if unknown:
        return (
            f" names unsupported skill settings {sorted(str(key) for key in unknown)!r}."
            f" The settings are {', '.join(SKILL_SETTINGS)}"
        )
    for key, names in value.items():
        if reason := check_agent_names(names):
            return f".{key}{reason}"
    unavailable = frozenset(value.get("unavailable") or ())
    if both := [name for name in value.get("required") or () if name in unavailable]:
        return f".required and .unavailable both name {both!r}. A required skill must be usable"
    return None


def text_keys(raw: Mapping[Any, Any], *, label: str, noun: str, refuse: Warn) -> dict[str, Any]:
    """Strip each key. Refuse a key that is not text, is empty, or repeats after stripping."""
    settled: dict[str, Any] = {}
    for raw_key, value in raw.items():
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if not key:
            refuse("%s has the key %r. Write each %s as text", label, raw_key, noun)
        elif key in settled:
            refuse("%s names %s more than once. Write each %s once", label, key, noun)
        else:
            settled[key] = value
    return settled


#: Wildcard for every key in an ``axis_restrictions`` section.
ANY_KEY = "*"


@dataclass(frozen=True)
class AxisRestriction:
    """Allow specified match axes for a section or key."""

    allowed: frozenset[str] = frozenset()
    because: str = ""

    def __post_init__(self) -> None:
        axes = tuple(self.allowed)
        for axis in axes:
            if not isinstance(axis, str) or not axis.strip():
                raise ValueError(f"an axis restriction names {axis!r}, which is not an axis")
        object.__setattr__(self, "allowed", frozenset(axis.strip() for axis in axes))

    @classmethod
    def only_on(cls, *axes: str, because: str = "") -> "AxisRestriction":
        """Permit only ``axes``."""
        return cls(allowed=frozenset(axes), because=because)

    def permits(self, axis: str) -> bool:
        return axis in self.allowed

    def refuses(self, axes: "Iterable[str]") -> tuple[str, ...]:
        """Return barred axes in sorted order."""
        return tuple(sorted(axis for axis in axes if not self.permits(axis)))

    def narrowed_by(self, other: "AxisRestriction | None") -> "AxisRestriction":
        """Intersect restrictions, preferring the tighter rule's reason."""
        if other is None:
            return self
        allowed = self.allowed & other.allowed
        if allowed == other.allowed and allowed != self.allowed:
            return AxisRestriction(allowed=allowed, because=other.because or self.because)
        return AxisRestriction(allowed=allowed, because=self.because or other.because)

    def describe(self) -> str:
        """The permitted axes as one clause, for a warning."""
        return ", ".join(sorted(self.allowed)) or "nothing"


def _freeze_sections(
    value: Mapping[str, "frozenset[str] | None"],
) -> Mapping[str, "frozenset[str] | None"]:
    """Freeze each section's key set."""
    return MappingProxyType({str(k): None if v is None else frozenset(v) for k, v in value.items()})


def freeze_nested(
    value: Mapping[str, Mapping[str, _FrozenValue]],
) -> Mapping[str, Mapping[str, _FrozenValue]]:
    """Freeze a two-level mapping at both levels."""
    return MappingProxyType({str(k): MappingProxyType(dict(v)) for k, v in value.items()})


@dataclass(frozen=True)
class ChannelCapabilities:
    """Scope fields and settings a channel supports."""

    channel: str
    axes: frozenset[str]
    sections: Mapping[str, "frozenset[str] | None"] = field(default_factory=dict)
    attended: bool = True
    identity_keys: tuple[str, ...] = ()
    layer0_keys: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    validators: Mapping[str, ValueValidator] = field(default_factory=dict)
    cautions: Mapping[str, ValueCaution] = field(default_factory=dict)
    axis_restrictions: Mapping[str, AxisRestriction] = field(default_factory=dict)
    axis_values: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "axes", frozenset(self.axes))
        object.__setattr__(self, "sections", _freeze_sections(self.sections))
        object.__setattr__(self, "layer0_keys", freeze_nested(self.layer0_keys))
        for name in ("validators", "cautions", "axis_restrictions"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(
            self,
            "axis_values",
            MappingProxyType({str(k): frozenset(v) for k, v in self.axis_values.items()}),
        )

    def populates(self, axis: str) -> bool:
        return axis in self.axes

    def reads(self, section: str) -> bool:
        return section in self.sections

    def acts_on(self, section: str, key: str) -> bool:
        """Whether ``section.key`` is something this channel does anything with."""
        if section not in self.sections:
            return False
        allowed = self.sections[section]
        return allowed is None or key in allowed

    def keys_for(self, section: str) -> "frozenset[str] | None":
        return self.sections.get(section)

    def layer0_key(self, section: str, key: str) -> "str | None":
        return self.layer0_keys.get(section, {}).get(key)

    def validator(self, section: str, key: str) -> "ValueValidator | None":
        return self.validators.get(f"{section}.{key}")

    def caution(self, section: str, key: str) -> "ValueCaution | None":
        """What this channel wants said about a valid ``section.key``."""
        return self.cautions.get(f"{section}.{key}")

    def axis_restriction(self, section: str, key: str = ANY_KEY) -> "AxisRestriction | None":
        """What this channel says about how ``section.key`` may be addressed."""
        return restriction_in(self.axis_restrictions, section, key)

    def values_for(self, axis: str) -> "frozenset[str] | None":
        """Return legal values. ``None`` allows any value. Empty allows none."""
        return self.axis_values.get(axis)


def restriction_in(table: Mapping[str, AxisRestriction], section: str, key: str = ANY_KEY) -> "AxisRestriction | None":
    """Intersect section-wide and per-key restrictions from one table."""
    section_wide = table.get(f"{section}.{ANY_KEY}")
    if key == ANY_KEY:
        return section_wide
    per_key = table.get(f"{section}.{key}")
    if per_key is None:
        return section_wide
    return per_key.narrowed_by(section_wide)


_REGISTRY: dict[str, ChannelCapabilities] = {}


def _check_identity_declaration(capabilities: ChannelCapabilities) -> None:
    """Warn if sender axes and identity keys disagree."""
    declares_axis = "user" in capabilities.axes
    if declares_axis and not capabilities.identity_keys:
        logger.warning(
            "scopes: %s declares the user axis but names no identity_keys. User scopes will never match",
            capabilities.channel,
        )
    elif capabilities.identity_keys and not declares_axis:
        logger.warning(
            "scopes: %s names identity_keys (%s) but does not declare the user axis. Those ids are never matched",
            capabilities.channel,
            ", ".join(capabilities.identity_keys),
        )


def _check_axis_restriction_declaration(capabilities: ChannelCapabilities) -> None:
    """Warn if a restriction names a key this channel ignores."""
    for entry in sorted(capabilities.axis_restrictions):
        section, _, key = entry.partition(".")
        if not capabilities.reads(section):
            logger.warning(
                "scopes: %s restricts %s but does not read %s. That restriction enforces nothing",
                capabilities.channel,
                entry,
                section,
            )
            continue
        if key != ANY_KEY and not capabilities.acts_on(section, key):
            logger.warning(
                "scopes: %s restricts %s but does not name %s among the %s keys it acts on."
                " That restriction enforces nothing",
                capabilities.channel,
                entry,
                key,
                section,
            )


def _check_axis_values_declaration(capabilities: ChannelCapabilities) -> None:
    """Warn if a vocabulary names an axis this channel cannot fill."""
    for axis in sorted(capabilities.axis_values):
        if not capabilities.populates(axis):
            logger.warning(
                "scopes: %s lists legal %s values but does not populate that axis. The check has no effect",
                capabilities.channel,
                axis,
            )


def register_channel(capabilities: ChannelCapabilities) -> None:
    """Register or replace a channel declaration after validation."""
    _check_identity_declaration(capabilities)
    _check_axis_restriction_declaration(capabilities)
    _check_axis_values_declaration(capabilities)
    _REGISTRY[capabilities.channel] = capabilities


def channel_capabilities(channel: str) -> "ChannelCapabilities | None":
    """The declaration for ``channel``, or ``None`` if it has not opted in."""
    return _REGISTRY.get(channel)


def known_channels() -> tuple[str, ...]:
    """Every channel that has declared, sorted, for use in warning text."""
    return tuple(sorted(_REGISTRY))


def snapshot_registry() -> dict[str, ChannelCapabilities]:
    """Copy the registry so tests can restore imported declarations."""
    return dict(_REGISTRY)


def restore_registry(snapshot: Mapping[str, ChannelCapabilities]) -> None:
    """Put back what :func:`snapshot_registry` returned."""
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)
