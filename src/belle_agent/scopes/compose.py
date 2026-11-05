"""Compose matching scope settings over connector defaults."""

from collections.abc import Mapping, Sequence
from typing import Any

from belle_agent.scopes.capabilities import channel_capabilities
from belle_agent.scopes.schema import (
    APPEND_SUFFIX,
    CLICK_APPROVE,
    CONNECTOR_SECTIONS,
    LEVEL_ASK,
    LEVEL_DENY,
    PERMISSION_LEVELS,
    SECTION_AGENT,
    SECTION_CLICKS,
    SECTION_DELIVERY,
    SECTION_PERMISSIONS,
    ClickRule,
    Scope,
    matching_scopes,
    signed_entries,
)

_MISSING = object()


def _as_set(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (frozenset, set, list, tuple)):
        return frozenset(map(str, value))
    return frozenset()


def _apply_signed(base: frozenset[str], entries: Sequence[str]) -> frozenset[str]:
    result = set(base)
    for entry in entries:
        sign, name = entry[0], entry[1:].strip()
        if not name:
            continue
        if sign == "+":
            result.add(name)
        else:
            result.discard(name)
    return frozenset(result)


def compose_values(
    layers: Sequence[Mapping[str, Any]],
    *,
    base: "Mapping[str, Any] | None" = None,
) -> dict[str, Any]:
    """Fold layers, leaving keys absent when scopes do not set them."""
    seed: Mapping[str, Any] = base if isinstance(base, Mapping) else {}
    result: dict[str, Any] = {}

    for layer in layers:
        for key, value in layer.items():
            if key.endswith(APPEND_SUFFIX):
                continue
            inherited = result.get(key, seed.get(key, _MISSING))

            if isinstance(value, (list, tuple)):
                signed = signed_entries(value)
                if signed:
                    start = _as_set(None if inherited is _MISSING else inherited)
                    result[key] = _apply_signed(start, signed)
                else:
                    result[key] = frozenset(str(item) for item in value)
                continue

            if isinstance(value, Mapping):
                merged: dict[str, Any] = {}
                if isinstance(inherited, Mapping):
                    merged.update(inherited)
                merged.update(value)
                result[key] = merged
                continue

            result[key] = value

        for key, value in layer.items():
            if not key.endswith(APPEND_SUFFIX):
                continue
            target = key[: -len(APPEND_SUFFIX)]
            inherited = result.get(target, seed.get(target, _MISSING))
            existing = "" if inherited is _MISSING or inherited is None else str(inherited)
            addition = str(value)
            result[target] = f"{existing}\n\n{addition}".strip() if existing else addition.strip()

    return result


def compose_section(
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
    chat_type: "str | None" = None,
    workspace: "str | None" = None,
    user: "str | None" = None,
    section: str = SECTION_DELIVERY,
    layer0: "Mapping[str, Any] | None" = None,
) -> dict[str, Any]:
    """Return keys set by scopes matching one conversation."""
    applicable = matching_scopes(
        scopes,
        channel=channel,
        chat=chat,
        chat_type=chat_type,
        workspace=workspace,
        user=user,
        section=section,
    )
    return compose_values([scope.section(section) for scope in applicable], base=layer0)


def click_rule(
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
    kind: str = CLICK_APPROVE,
) -> "ClickRule | None":
    """Find the click gate without matching the clicker as a sender."""
    settled = compose_section(scopes, channel=channel, chat=chat, section=SECTION_CLICKS)
    rule = settled.get(kind)
    return rule if isinstance(rule, ClickRule) else None


def strictest_tool_levels(
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
) -> dict[str, str]:
    """Settle each tool on the strictest level a matching scope names.

    Scopes only tighten, so rule order does not change the result. ``ask``
    becomes ``deny`` on a channel declared unattended.
    """
    capabilities = channel_capabilities(channel) if channel is not None else None
    unattended = capabilities is not None and not capabilities.attended
    settled: dict[str, str] = {}
    for scope in matching_scopes(scopes, channel=channel, chat=chat, section=SECTION_PERMISSIONS):
        tools = scope.section(SECTION_PERMISSIONS).get("tools")
        if not isinstance(tools, Mapping):
            continue
        for tool, level in tools.items():
            if level not in PERMISSION_LEVELS:
                continue
            if unattended and level == LEVEL_ASK:
                level = LEVEL_DENY
            if tool not in settled or PERMISSION_LEVELS.index(level) > PERMISSION_LEVELS.index(settled[tool]):
                settled[tool] = level
    return settled


def scoped_chats(
    scopes: Sequence[Scope],
    *,
    channel: str,
    sections: Sequence[str] = CONNECTOR_SECTIONS,
) -> tuple[str, ...]:
    """List chats with connector-consumed settings, sorted."""
    named = {
        scope.match.chat
        for scope in scopes
        if scope.match.chat is not None
        and scope.match.channel in (None, channel)
        and any(scope.section(name) for name in sections)
    }
    return tuple(sorted(named))


def scope_enables_chat(scopes: Sequence[Scope], *, channel: str, chat: str) -> bool:
    """Whether a rule written for one chat opts it in beyond a connector's list of allowed chats.

    A rule that sets how the chat is answered, or which model answers it, does.
    One that only narrows what a turn may use does not.
    """
    return any(
        scope.match.channel in (None, channel)
        and scope.match.chat == chat
        and (
            bool(scope.section(SECTION_DELIVERY))
            or "model_name" in scope.section(SECTION_AGENT)
        )
        for scope in scopes
    )
