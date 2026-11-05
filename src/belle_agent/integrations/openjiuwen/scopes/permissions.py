"""Narrow tool permissions with matching scope restrictions."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from belle_agent.scopes.capabilities import channel_capabilities
from belle_agent.scopes.schema import (
    LEVEL_ALLOW,
    LEVEL_ASK,
    LEVEL_DENY,
    SECTION_PERMISSIONS,
    Scope,
    matching_scopes,
)

logger = logging.getLogger(__name__)

#: An empty default becomes ``ask`` in agent-core.
_NEUTRAL_BASE: dict[str, Any] = {"tools": {}, "defaults": {"*": LEVEL_ALLOW}}

_reported_rule_conflicts: set[tuple[str, str]] = set()

_reported_narrowing_failure = False


def _scopes_unavailable(what: str) -> None:
    """Warn once when merging fails and preserve the base policy."""
    global _reported_narrowing_failure
    if _reported_narrowing_failure:
        return
    _reported_narrowing_failure = True
    logger.warning(
        "[scopes] %s could not be evaluated; no scope narrows permissions and the permissions block applies as written",
        what,
        exc_info=True,  # noqa: LOG014
    )


def _narrow_permissions() -> Any:
    """Import agent-core's narrowing function.

    Patches ``tiered_policy`` when it is missing ``_parse_level``, which
    ``narrowing`` imports from it.
    """
    try:
        from openjiuwen.agent_teams.security.narrowing import narrow_permissions
    except ImportError:
        from openjiuwen.harness.security import tiered_policy as _shim
        from openjiuwen.harness.security.permission_engine.toolguard import (
            tool_policy as _canonical,
        )

        if hasattr(_shim, "_parse_level") or not hasattr(_canonical, "_parse_level"):
            raise
        _shim._parse_level = _canonical._parse_level
        logger.debug(
            "[scopes] restored _parse_level on the tiered_policy shim so agent_teams.security.narrowing can be imported"
        )
        from openjiuwen.agent_teams.security.narrowing import narrow_permissions

    return narrow_permissions


def _narrow(base: Mapping[str, Any], override: Mapping[str, str]) -> dict[str, Any]:
    return _narrow_permissions()(dict(base), dict(override))


def _tool_levels(value: Any) -> dict[str, str]:
    """A ``tools`` mapping as names to level words, or ``{}``."""
    if not isinstance(value, Mapping):
        return {}
    return {str(name): str(level) for name, level in value.items()}


def _scope_layers(scopes: Sequence[Scope], *, channel: str | None, chat: str | None) -> tuple[dict[str, str], ...]:
    """Return matching tool restrictions in cascade order."""
    return tuple(
        levels
        for scope in matching_scopes(scopes, channel=channel, chat=chat, section=SECTION_PERMISSIONS)
        if (levels := _tool_levels(scope.section(SECTION_PERMISSIONS).get("tools")))
    )


def _degrade_unattended(levels: Mapping[str, str], *, channel: str | None) -> dict[str, str]:
    """Treat ``ask`` as ``deny`` only on declared unattended channels."""
    capabilities = channel_capabilities(channel) if channel is not None else None
    if not capabilities or capabilities.attended:
        return dict(levels)
    return {tool: (LEVEL_DENY if level == LEVEL_ASK else level) for tool, level in levels.items()}


def settled_tool_levels(scopes: Sequence[Scope], *, channel: str | None, chat: str | None = None) -> dict[str, str]:
    """Fold matching tool restrictions over an all-allow base."""
    layers = _scope_layers(scopes, channel=channel, chat=chat)
    if not layers:
        return {}
    settled: dict[str, Any] = dict(_NEUTRAL_BASE)
    try:
        for layer in layers:
            settled = _narrow(settled, _degrade_unattended(layer, channel=channel))
    except Exception:  # noqa: BLE001
        _scopes_unavailable("the permission levels a scope settles")
        return {}
    return _tool_levels(settled.get("tools"))


def _entry_tools(entry: Mapping[str, Any]) -> list[Any] | None:
    tools = entry.get("tools")
    if isinstance(tools, str):
        return [tools]
    return tools if isinstance(tools, list) else None


def _prune_approval_overrides(config: dict[str, Any], narrowed_tools: Mapping[str, str]) -> None:
    """Remove approval overrides that bypass scoped ``ask``."""
    overrides = config.get("approval_overrides")
    if not isinstance(overrides, list) or not overrides:
        return

    kept: list[Any] = []
    for entry in overrides:
        if not isinstance(entry, dict) or (tools := _entry_tools(entry)) is None:
            kept.append(entry)
            continue
        remaining = [name for name in tools if narrowed_tools.get(str(name).strip(), LEVEL_ALLOW) == LEVEL_ALLOW]
        if not remaining:
            logger.info(
                "[scopes] approval_overrides[%s] dropped for this conversation:"
                " a scope narrows %s, and an override would otherwise allow it"
                " before the narrowed level is ever consulted",
                entry.get("id", "?"),
                ", ".join(sorted(str(name) for name in tools)),
            )
            continue
        if len(remaining) != len(tools):
            entry = {**entry, "tools": remaining}
        kept.append(entry)

    config["approval_overrides"] = kept


def _report_rule_conflicts(config: Mapping[str, Any], narrowed_tools: Mapping[str, str]) -> None:
    """Warn once when a rule's ``allow`` bypasses scoped ``ask``."""
    rules = config.get("rules")
    if not isinstance(rules, list):
        return
    for entry in rules:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("action") or "").strip().lower() != LEVEL_ALLOW:
            continue
        tools = _entry_tools(entry)
        if tools is None:
            continue
        rule_id = str(entry.get("id") or entry.get("pattern") or "?")
        for name in tools:
            tool = str(name).strip()
            if narrowed_tools.get(tool) != LEVEL_ASK:
                continue
            if (tool, rule_id) in _reported_rule_conflicts:
                continue
            _reported_rule_conflicts.add((tool, rule_id))
            logger.warning(
                "[scopes] permissions.tools.%s is narrowed to ask, but"
                " permissions.rules[%s] allows %s and is evaluated before the"
                " tool baseline, so that call may still run without asking."
                " Narrow it to deny, or change that rule",
                tool,
                rule_id,
                tool,
            )


def narrow_config_for_scopes(
    permission_config: Mapping[str, Any],
    scopes: Sequence[Scope],
    *,
    channel: str | None,
    chat: str | None = None,
) -> dict[str, Any]:
    """Return narrowed permissions, or the original config if merging fails."""
    if not isinstance(permission_config, Mapping):
        return permission_config  # type: ignore[return-value]
    as_written = permission_config if isinstance(permission_config, dict) else dict(permission_config)
    levels = settled_tool_levels(scopes, channel=channel, chat=chat)
    tightened = {tool: level for tool, level in levels.items() if level != LEVEL_ALLOW}
    if not tightened:
        return as_written

    try:
        narrowed = _narrow(permission_config, tightened)
        _prune_approval_overrides(narrowed, tightened)
        _report_rule_conflicts(narrowed, tightened)
    except Exception:  # noqa: BLE001
        _scopes_unavailable("the permission config a scope narrows")
        return as_written
    logger.info(
        "[scopes] permissions narrowed for channel=%s chat=%s: %s",
        channel,
        chat,
        ", ".join(f"{tool}={level}" for tool, level in sorted(tightened.items())),
    )
    return narrowed


def denied_tool_level(
    scopes: Sequence[Scope],
    tool_name: str,
    *,
    channel: str | None,
    chat: str | None = None,
) -> str | None:
    """Return scoped ``deny`` before any approval check."""
    levels = settled_tool_levels(scopes, channel=channel, chat=chat)
    return LEVEL_DENY if levels.get(tool_name) == LEVEL_DENY else None


__all__ = [
    "denied_tool_level",
    "narrow_config_for_scopes",
    "settled_tool_levels",
]
