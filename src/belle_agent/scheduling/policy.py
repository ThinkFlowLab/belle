"""Allow rules for delivery between scheduler addresses."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from belle_agent.scheduling.address import Address
from belle_agent.scheduling.settings import _check_delivery

_MATCH_FIELDS = ("platform", "workspace", "conversation")


@dataclass(frozen=True)
class Match:
    """An address pattern; absent fields match every value."""

    platform: str | None = None
    workspace: str | None = None
    conversation: str | None = None


@dataclass(frozen=True)
class DeliveryRule:
    """One origin-to-target allow rule from scheduling.delivery."""

    index: int
    origin: Match
    target: Match
    same: frozenset[str]


def parse_delivery_rules(raw: Any) -> tuple[DeliveryRule, ...]:
    """Validate and parse the operator's allow rules in their listed order."""
    _check_delivery(raw)
    return tuple(
        DeliveryRule(
            index,
            Match(**rule.get("from", {})),
            Match(**{key: value for key, value in rule["to"].items() if key != "same"}),
            frozenset(rule["to"].get("same", [])),
        )
        for index, rule in enumerate(raw, 1)
    )


def _checks(rule: DeliveryRule, origin: Address, target: Address) -> Iterable[tuple[str, bool]]:
    for field in _MATCH_FIELDS:
        expected = getattr(rule.origin, field)
        if expected is not None:
            yield f"from.{field}", getattr(origin, field) == expected
    for field in _MATCH_FIELDS:
        expected = getattr(rule.target, field)
        if expected is not None:
            yield f"to.{field}", getattr(target, field) == expected
    for field in _MATCH_FIELDS:
        if field in rule.same:
            yield f"to.same.{field}", getattr(origin, field) == getattr(target, field)


def allowed_by(rules: Iterable[DeliveryRule], origin: Address, target: Address) -> DeliveryRule | None:
    """Return the first rule that allows the origin to deliver to the target."""
    return next((rule for rule in rules if all(ok for _, ok in _checks(rule, origin, target))), None)


def delivery_refusal_detail(rules: Iterable[DeliveryRule], origin: Address, target: Address) -> str:
    """Name the origin, the target, and what the closest allowed delivery needs."""
    refused = (
        f"delivery from {origin.platform}/{origin.workspace}/{origin.conversation} "
        f"to {target.platform}/{target.workspace}/{target.conversation} is not allowed here"
    )
    scored = []
    for rule in rules:
        checks = list(_checks(rule, origin, target))
        scored.append((sum(ok for _, ok in checks), rule.index, next((name for name, ok in checks if not ok), "")))
    if not scored:
        return f"{refused}: this deployment allows no delivery"
    side, field = min(scored, key=lambda item: (-item[0], item[1]))[2].rsplit(".", 1)
    need = {
        "from": f"another origin {field}",
        "to": f"another target {field}",
        "to.same": f"the target {field} to match the origin {field}",
    }[side]
    return f"{refused}; the nearest allowed delivery needs {need}"
