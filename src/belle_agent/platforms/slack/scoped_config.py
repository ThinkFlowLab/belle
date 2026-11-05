"""Slack trigger modes and composed conversation settings."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from belle_agent.admission import policy as admission_policy
from belle_agent.platforms.slack.events import normalize_event_policy
from belle_agent.scopes import (
    Scope,
    scope_enables_chat,
)
from belle_agent.scopes.settings import (
    ConversationOverride as SlackChannelOverride,
)
from belle_agent.scopes.settings import (
    compose_overrides,
)
from belle_agent.scopes.settings import (
    sections_as_override as _sections_as_override,
)
from belle_agent.scopes.settings import (
    settled_override as _settled_override,
)

logger = logging.getLogger(__name__)


CHAT_TYPE_PUBLIC_CHANNEL = "channel"

GROUP_MODE_MENTION = "mention"
GROUP_MODE_REPLY = "reply"
GROUP_MODE_ALL = "all"
GROUP_MODE_OFF = "off"
GROUP_CHAT_MODES = (
    GROUP_MODE_MENTION,
    GROUP_MODE_REPLY,
    GROUP_MODE_ALL,
    GROUP_MODE_OFF,
)

# Triggers must be decidable from the inbound payload before a turn starts.
TRIGGER_MENTION = "mention"
TRIGGER_REPLY = admission_policy.TRIGGER_REPLY
TRIGGER_ALL = admission_policy.MODE_ALL
TRIGGER_URL = admission_policy.MODE_URL
TRIGGER_HAS_FILE = admission_policy.MODE_HAS_FILE
CHANNEL_MODE_TRIGGERS = (
    TRIGGER_MENTION,
    TRIGGER_REPLY,
    TRIGGER_ALL,
    TRIGGER_URL,
    TRIGGER_HAS_FILE,
)

# Event turns have no inbound message, so this is a label, not a mode trigger.
TRIGGER_EVENT = "event"

# app_mention has its own listener. Both mention and all claim that route.
MENTION_ROUTE_TRIGGERS: frozenset[str] = frozenset({TRIGGER_MENTION, TRIGGER_ALL})


# How the legacy single-string group_chat_mode reads as a trigger set. "reply"
# and "all" include "mention" because Slack delivers mentions on a separate
# app_mention subscription that neither of those modes switches off, and "off"
# is the empty set. The table is Slack's alone: Telegram's modes are exclusive,
# so the same strings there would map to {reply} and {all}, and unifying the two
# would silently change what live Telegram bots answer.
_LEGACY_MODE_TRIGGERS: dict[str, frozenset[str]] = {
    GROUP_MODE_MENTION: frozenset({TRIGGER_MENTION}),
    GROUP_MODE_REPLY: frozenset({TRIGGER_MENTION, TRIGGER_REPLY}),
    GROUP_MODE_ALL: frozenset({TRIGGER_MENTION, TRIGGER_ALL}),
    GROUP_MODE_OFF: frozenset(),
}

class SlackScopeConfig(Protocol):
    """Fields used to settle Slack conversation scopes."""

    scopes: tuple[Scope, ...]
    group_chat_mode: str
    conversation_overrides: dict[str, SlackChannelOverride]
    platform_override: SlackChannelOverride

def resolve_group_chat_mode(raw: Any) -> str:
    """Normalize the mode, including YAML booleans for unquoted ``off``."""
    if isinstance(raw, bool):
        return GROUP_MODE_OFF if raw is False else GROUP_MODE_MENTION
    return str(raw or "").strip().lower()


def _slack_layer0_triggers(group_chat_mode: Any) -> frozenset[str]:
    """Seed scope trigger composition from the platform's group-chat mode."""
    mode = resolve_group_chat_mode(group_chat_mode)
    return _LEGACY_MODE_TRIGGERS.get(mode, _LEGACY_MODE_TRIGGERS[GROUP_MODE_MENTION])


def _as_trigger_set(value: Any) -> frozenset[str]:
    """Convert a settled mode without splitting a bare string into letters."""
    if isinstance(value, str):
        return _LEGACY_MODE_TRIGGERS.get(value.strip().lower(), frozenset())
    if isinstance(value, (frozenset, set, list, tuple)):
        return frozenset(str(item) for item in value)
    return frozenset()


def sections_as_override(
    sections: Mapping[str, Mapping[str, Any]],
) -> SlackChannelOverride:
    """Apply Slack trigger and event vocabularies to composed settings."""
    return _sections_as_override(
        sections, parse_mode=_as_trigger_set, parse_events=normalize_event_policy,
    )


def apply_scopes_to_slack_overrides(
    slack_conf: Mapping[str, Any],
    *,
    scopes: Sequence[Scope] | None = None,
) -> tuple[SlackChannelOverride, dict[str, SlackChannelOverride]]:
    """Cache scoped settings over Slack's legacy group-chat trigger default."""
    if not scopes:
        return SlackChannelOverride(), {}
    platform, resolved = compose_overrides(
        scopes, channel="slack",
        layer0={"mode": _slack_layer0_triggers(slack_conf.get("group_chat_mode"))},
        convert=sections_as_override,
    )
    for channel_id, override in resolved.items():
        if override.mode is not None:
            _warn_if_mode_is_unusual(f"scopes for {channel_id}", override.mode)
    if platform.mode is not None:
        _warn_if_mode_is_unusual("the scope for channel slack", platform.mode)
    return platform, resolved


def _warn_if_mode_is_unusual(label: str, triggers: frozenset[str]) -> None:
    """Say when a composed trigger set is legal but probably not what was meant.

    Deliberately not refusals. A channel that watches for links or files without
    being conversational is a legitimate pure-ingest configuration, and silence
    is a legitimate thing to ask for; refusing either would refuse a use case
    this exists to support. They are unusual enough to be worth one line,
    because the same result is more often a slip -- particularly here, where it
    can be the *composition* of two correct-looking layers rather than anything
    an operator wrote in one place.
    """
    if not triggers:
        logger.warning(
            "%s settle on no triggers at all; that conversation is now silent"
            " and answers nothing, not even @mentions of the bot",
            label,
        )
        return
    if TRIGGER_ALL in triggers:
        # ``mention`` is strictly redundant beside ``all`` too, and is left out
        # of this list on purpose. ``+all`` written on a conversation that
        # already answers mentions composes to exactly {all, mention}, so
        # naming it here would warn at the composition operators reach for
        # first. The set arriving here says what a conversation settled on and
        # not which layer wrote each entry, so the warning cannot tell that
        # composition apart from someone typing both by hand.
        redundant = sorted(triggers & {TRIGGER_REPLY, TRIGGER_URL, TRIGGER_HAS_FILE})
        if redundant:
            logger.warning(
                "%s settle on all alongside %s; all already matches every"
                " channel message, so those entries add nothing",
                label,
                ", ".join(redundant),
            )
    # ``all`` counts as listing it: the mention route honours ``all`` the same
    # way the message route does, so a conversation set to answer everything
    # answers mentions and must not be told otherwise.
    if not (triggers & MENTION_ROUTE_TRIGGERS):
        logger.warning(
            "%s settle on [%s] and name neither mention nor all; that"
            " conversation ignores @mentions of the bot. Add mention, or"
            " +mention, to keep answering them",
            label,
            ", ".join(sorted(triggers)),
        )


def settled_override(
    config: SlackScopeConfig,
    channel_id: str,
    *,
    user_id: str = "",
    chat_type: str = "",
    workspace: str = "",
) -> SlackChannelOverride:
    """Resolve scoped settings for one Slack conversation and sender."""
    return _settled_override(
        config.scopes, channel_id, channel="slack",
        conversation_overrides=config.conversation_overrides,
        platform_override=config.platform_override,
        layer0={"mode": _slack_layer0_triggers(config.group_chat_mode)},
        convert=sections_as_override, user_id=user_id,
        chat_type=chat_type, workspace=workspace,
    )


def slack_scope_enables_channel(config: SlackScopeConfig, channel_id: str) -> bool:
    """Whether a room's scope opts it in beyond ``allowed_channel_ids``."""
    if not config.scopes:
        # Legacy callers can still supply settled overrides directly.
        return channel_id in (config.conversation_overrides or {})
    return scope_enables_chat(config.scopes, channel="slack", chat=channel_id)



def channel_triggers(
    config: SlackScopeConfig,
    channel_id: str,
    *,
    group_chat_mode: str,
    user_id: str = "",
    chat_type: str = "",
    workspace: str = "",
) -> frozenset[str]:
    """Use scoped triggers, or fall back to ``group_chat_mode``.

    An omitted identity, chat type, or workspace matches no rule requiring it.
    """
    override = settled_override(
        config,
        channel_id,
        user_id=user_id,
        chat_type=chat_type,
        workspace=workspace,
    )
    if override.mode is not None:
        return override.mode

    return _LEGACY_MODE_TRIGGERS.get(
        group_chat_mode, _LEGACY_MODE_TRIGGERS[GROUP_MODE_MENTION]
    )


def describe_configured_channels(config: SlackScopeConfig) -> str:
    """List configured channels and their triggers without Slack API calls."""
    mode = str(config.group_chat_mode or "").strip().lower()
    if mode not in _LEGACY_MODE_TRIGGERS:
        mode = GROUP_MODE_MENTION

    # A platform scope can replace the connector-wide fallback.
    fallback = f"group_chat_mode={mode}"
    platform = config.platform_override
    if platform.mode is not None:
        fallback = (
            "the scope for channel slack:"
            f" mode=[{','.join(sorted(platform.mode)) or 'silent'}]"
        )

    channel_ids = sorted(config.conversation_overrides or {})
    if not channel_ids:
        message = (
            "channels.slack has no per-channel configuration; every channel"
            f" follows {fallback}"
        )
        logger.info(message)
        return message

    entries: list[str] = []
    for channel_id in channel_ids:
        override = settled_override(config, channel_id)
        triggers = sorted(channel_triggers(config, channel_id, group_chat_mode=mode))
        via = "scopes"
        prompt = via if override.prompt is not None else "none"
        model = override.model_name or "default"
        mid_turn = f" mid_turn={override.mid_turn}" if override.mid_turn else ""
        session = f" session={override.session}" if override.session else ""
        reply = f" reply={override.reply}" if override.reply else ""
        entries.append(
            f"{channel_id} mode=[{','.join(triggers) or 'silent'}]"
            f" prompt={prompt} model={model}{mid_turn}{session}{reply}"
            f" via={via}"
        )

    message = (
        f"channels.slack is configured for {len(entries)} channel(s):"
        f" {'; '.join(entries)}; every other channel follows {fallback}"
    )
    logger.info(message)
    return message
