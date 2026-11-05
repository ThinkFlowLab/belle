"""Apply scoped permissions at tool-call time using conversation ContextVars."""

import logging
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.extensions.sdk.permission_layers import operator_approval_overrides
from jiuwenswarm.extensions.sdk.policy_provider import (
    set_permission_policy_provider,
)
from jiuwenswarm.extensions.sdk.tool_permission_context import (
    SKILLS_REBUILD_SILENT,
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_CHAT_ID,
)

from belle_agent.config import BelleConfigError
from belle_agent.integrations.jiuwenswarm.config import load_belle_config
from belle_agent.integrations.jiuwenswarm.scopes import register_host_channels
from belle_agent.integrations.openjiuwen.scopes.permissions import (
    denied_tool_level,
    narrow_config_for_scopes,
)
from belle_agent.scopes import (
    LEVEL_DENY,
    PEOPLE_KEY,
    ROLES_KEY,
    SECTION_PERMISSIONS,
    Scope,
    compile_scopes,
)

register_host_channels()

logger = logging.getLogger(__name__)

# Cache all three source blocks; role and people edits change resolved scopes.
_compiled_key: "str | None" = None
_compiled: tuple[Scope, ...] = ()


#: Fall back to the base policy if scope evaluation fails.
_UNAVAILABLE = (
    "[scopes] %s could not be settled for this conversation; no scope narrows"
    " permissions and the permissions block applies as written"
)


def runtime_scopes() -> tuple[Scope, ...]:
    """Return compiled scopes, or ``()`` if loading fails."""
    global _compiled_key, _compiled
    try:
        from jiuwenswarm.extensions.sdk.host_config import get_config

        data = get_config()
        if not isinstance(data, Mapping):
            return ()

        belle = load_belle_config()
        raw = belle.get("scopes")
        people = belle.get(PEOPLE_KEY)
        roles = belle.get(ROLES_KEY)
        channels = data.get("channels")
        key = repr((raw, people, roles, channels))
        if key == _compiled_key:
            return _compiled

        compiled = compile_scopes(
            raw,
            channels_config=channels if isinstance(channels, Mapping) else None,
            people=people,
            roles=roles,
        )
    except BelleConfigError:
        raise
    except Exception:
        logger.warning(_UNAVAILABLE, "the Belle scopes list", exc_info=True)
        return ()
    _compiled, _compiled_key = compiled, key
    return _compiled


def current_conversation() -> "tuple[str | None, str | None]":
    """The ``(channel, chat)`` this turn is running for, or ``(None, None)``."""
    channel = (TOOL_PERMISSION_CHANNEL_ID.get() or "").strip()
    chat = (TOOL_PERMISSION_CHAT_ID.get() or "").strip()
    return (channel or None, chat or None)


#: Returned by ``_scoped_conversation`` when scopes set tool levels but the call names no conversation.
_UNKNOWN = object()

#: The refusal of a tool call in that case.
UNKNOWN_CONVERSATION_REFUSAL = (
    "[PERMISSION_DENIED] The conversation of this tool call is unknown, so the"
    " scoped tool permissions (scopes: permissions.tools) cannot be applied"
)

_reported_unknown = False


def _sets_tool_levels(scope: Scope) -> bool:
    return bool(scope.section(SECTION_PERMISSIONS).get("tools"))


def _report_unknown_conversation() -> None:
    """Warn once per process that the conversation binding is missing."""
    global _reported_unknown
    if _reported_unknown:
        return
    _reported_unknown = True
    logger.warning(
        "[scopes] a tool call has neither TOOL_PERMISSION_CHANNEL_ID nor"
        " TOOL_PERMISSION_CHAT_ID bound, and a scope sets permissions.tools;"
        " tool calls without that binding are refused"
    )


def _scoped_conversation() -> Any:
    """Return ``(scopes, channel, chat)``, ``None`` if no scope applies, or ``_UNKNOWN``."""
    scopes = runtime_scopes()
    if not scopes:
        return None
    channel, chat = current_conversation()
    if channel is not None or chat is not None:
        return scopes, channel, chat
    if not any(_sets_tool_levels(scope) for scope in scopes):
        return None
    _report_unknown_conversation()
    return _UNKNOWN


def _refuse_every_tool(permission_config: Any) -> Any:
    """A config under which the engine denies every tool call."""
    if not isinstance(permission_config, Mapping):
        return permission_config
    return {
        **permission_config,
        "tools": {},
        "defaults": {"*": LEVEL_DENY},
        "rules": [],
        "approval_overrides": [],
    }


def narrow_permission_config(permission_config: Any) -> Any:
    """Narrow permissions for this conversation, preserving the base on failure."""
    if SKILLS_REBUILD_SILENT.get():
        return permission_config
    scoped = _scoped_conversation()
    if scoped is None:
        return permission_config
    if scoped is _UNKNOWN:
        return _refuse_every_tool(permission_config)
    scopes, channel, chat = scoped
    try:
        return narrow_config_for_scopes(
            permission_config, scopes, channel=channel, chat=chat
        )
    except Exception:
        logger.warning(_UNAVAILABLE, "the narrowed permission config", exc_info=True)
        return permission_config


def scope_refusal(tool_name: str) -> str | None:
    """Return why a scope refuses this call, or ``None``; ``ask`` is the engine's."""
    scoped = _scoped_conversation()
    if scoped is None:
        return None
    if scoped is _UNKNOWN:
        return UNKNOWN_CONVERSATION_REFUSAL
    scopes, channel, chat = scoped
    try:
        level = denied_tool_level(scopes, tool_name, channel=channel, chat=chat)
    except Exception:
        logger.warning(_UNAVAILABLE, "the tool levels a scope settles", exc_info=True)
        return None
    if level is None:
        return None
    return "[PERMISSION_DENIED] 该工具在当前会话被 scopes 规则禁用 (scopes: deny)"


def restore_operator_approval_overrides(
    permissions: dict[str, Any], session_id: str | None = None
) -> dict[str, Any]:
    """Keep scoped pruning out of a persisted approval overlay."""
    if not isinstance(permissions, dict):
        return permissions
    try:
        base = operator_approval_overrides(session_id)

        merged = permissions.get("approval_overrides")
        if not isinstance(merged, list):
            return permissions
        known = {
            str(item.get("id") or "").strip()
            for item in base
            if isinstance(item, dict)
        }
        added = [
            item
            for item in merged
            if not isinstance(item, dict)
            or str(item.get("id") or "").strip() not in known
        ]
        return {**permissions, "approval_overrides": [*base, *added]}
    except Exception as exc:  # noqa: BLE001 - storage failure keeps the original policy
        logger.warning("[InterruptHelpers] overlay source restore failed: %s", exc)
        return permissions


class _ScopedPermissionPolicy:
    deny_tool = staticmethod(scope_refusal)

    narrow_config = staticmethod(narrow_permission_config)
    prepare_persist = staticmethod(restore_operator_approval_overrides)


def activate() -> None:
    """Attach Belle's scoped policy to JiuwenSwarm's permission rail.

    Compile the scopes first, so that an invalid rule stops the agent start.
    """
    runtime_scopes()
    set_permission_policy_provider(_ScopedPermissionPolicy())


def deactivate() -> None:
    set_permission_policy_provider(None)


def reset_cache() -> None:
    """Forget the compiled scopes and the warning. For tests, and for a config reload."""
    global _compiled_key, _compiled, _reported_unknown
    _compiled_key = None
    _compiled = ()
    _reported_unknown = False


__all__ = [
    "activate",
    "current_conversation",
    "deactivate",
    "narrow_permission_config",
    "reset_cache",
    "restore_operator_approval_overrides",
    "runtime_scopes",
    "scope_refusal",
]
