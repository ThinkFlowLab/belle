"""Slack scope capabilities and validators."""

from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.slack.events import (
    EVENT_DISPOSITIONS,
    EVENT_FAMILIES,
    EVENT_FAMILY_APP_HOME,
    EVENT_TURN,
    EVENT_TURN_FAMILIES,
    KEY_EVENTS,
    disposition_holds,
    disposition_word,
    turn_withheld_reason,
)
from belle_agent.platforms.slack.scoped_config import CHANNEL_MODE_TRIGGERS
from belle_agent.scopes import (
    AXIS_CHANNEL,
    AXIS_CHAT,
    AXIS_CHAT_TYPE,
    AxisRestriction,
    ChannelCapabilities,
    ScopeMatch,
)
from belle_agent.scopes.capabilities import ValueValidator

# Slack treats im and mpim as direct messages. Channel and group are rooms.
_DM_CHAT_TYPES: "frozenset[str]" = frozenset({"im", "mpim"})
_ROOM_CHAT_TYPES: "frozenset[str]" = frozenset({"channel", "group"})
#: Every kind of conversation Slack states. A member event may instead carry an id prefix.
SLACK_CHAT_TYPES: "frozenset[str]" = _DM_CHAT_TYPES | _ROOM_CHAT_TYPES

def _trigger_name(entry: str) -> str:
    """One ``mode`` entry with its sign taken off, if it had one."""
    entry = entry.strip()
    return entry[1:].strip() if entry[:1] in ("+", "-") else entry


def _check_mode(value: Any) -> "str | None":
    """Validate Slack triggers."""
    if isinstance(value, str):
        return f"={value!r} is one word. Write a list of triggers: [{value}]"
    if not isinstance(value, (list, tuple)):
        return f"={value!r} is not a list of triggers"

    # Strip only one sign, matching the signed-entry fold.
    unknown = [
        str(entry)
        for entry in value
        if _trigger_name(str(entry)) not in CHANNEL_MODE_TRIGGERS
    ]
    if unknown:
        return (
            f" names {', '.join(repr(name) for name in unknown)}, which"
            f" {'are' if len(unknown) > 1 else 'is'} not"
            f" {'triggers' if len(unknown) > 1 else 'a trigger'}."
            f" The triggers are {'/'.join(CHANNEL_MODE_TRIGGERS)}"
        )
    return None


def _check_events(value: Any) -> "str | None":
    """Validate event families and their dispositions."""
    if not isinstance(value, Mapping):
        return (
            f"={value!r} is not a mapping of event families to dispositions."
            f" Write events with the families you want, one per line."
            f" The families are {', '.join(EVENT_FAMILIES)} and each takes"
            f" {' or '.join(EVENT_DISPOSITIONS)}"
        )
    unknown_families = [
        str(family)
        for family in value
        if str(family).strip() not in EVENT_FAMILIES
    ]
    if unknown_families:
        return (
            f" names {', '.join(repr(name) for name in unknown_families)},"
            f" which {'are' if len(unknown_families) > 1 else 'is'} not"
            f" {'event families' if len(unknown_families) > 1 else 'an event family'}."
            f" The families are {'/'.join(EVENT_FAMILIES)}, each covering both"
            f" directions of one state change -- reaction covers added and"
            f" removed together, and so do the other two"
        )
    bad_words = [
        f"{family}={disposition!r}"
        for family, disposition in value.items()
        if disposition_word(disposition) not in EVENT_DISPOSITIONS
    ]
    if bad_words:
        return (
            f" sets {', '.join(bad_words)}. A family takes"
            f" {' or '.join(EVENT_DISPOSITIONS)}, written exactly and in lower"
            f" case. off drops the event, context keeps it for the next turn,"
            f" and turn starts one over the event itself"
        )
    # A valid disposition can still lack a destination for its event family.
    undestined = sorted(
        str(family).strip()
        for family, disposition in value.items()
        if not disposition_holds(str(family).strip(), disposition_word(disposition))
    )
    if undestined:
        why = "; ".join(
            dict.fromkeys(turn_withheld_reason(name) for name in undestined)
        )
        return (
            f" sets {', '.join(f'{name}={EVENT_TURN}' for name in undestined)},"
            f" and {' and '.join(undestined)} cannot start a turn:"
            f" {why.rstrip('.')}. The families that take"
            f" {EVENT_TURN} are {'/'.join(sorted(EVENT_TURN_FAMILIES))}. Write"
            f" context for the rest, which keeps the event for the next turn"
            f" that runs anyway"
        )
    return None


def _caution_events(value: Any, match: ScopeMatch) -> "str | None":
    """Warn when a non-DM rule names an App Home event, which arrives in a DM.

    Keep the mapping: other event families in it may still apply. Do not infer
    chat type from an ID prefix or query Slack while loading scopes.
    """
    if not isinstance(value, Mapping):
        return None
    families = {str(family).strip() for family in value}
    # Only chat_type proves App Home cannot arrive. An id prefix does not.
    kinds = match.chat_type
    if EVENT_FAMILY_APP_HOME not in families or not kinds:
        return None
    if any(kind in _DM_CHAT_TYPES for kind in kinds):
        return None
    return (
        f" sets {EVENT_FAMILY_APP_HOME} on a rule matching"
        f" {AXIS_CHAT_TYPE}: {', '.join(kinds)}, which will never see one: an"
        f" App Home event arrives in this app's direct message with the person"
        f" who opened it, and never in a conversation of that kind. Keeping the"
        f" mapping, since the other families do arrive where this rule is"
        f" looking, but nothing will come of that entry. Write"
        f" {EVENT_FAMILY_APP_HOME} on a rule that matches the direct message --"
        f" {{{AXIS_CHAT_TYPE}: im}}, or that conversation by id -- or on"
        f" {{{AXIS_CHANNEL}: slack}}, which matches whatever conversation an"
        f" event arrives in"
    )


def slack_capabilities(*, check_model_name: ValueValidator | None = None) -> ChannelCapabilities:
    """Declare what scopes may match and set on Slack.

    Which model names exist is the knowledge of whatever runs the turns, so the
    check of ``agent.model_name`` is its caller's to give. Without one the name
    is passed through unchecked.
    """
    return ChannelCapabilities(
        channel="slack",
        # Workspace is the connected installation team, including Slack Connect.
        axes=frozenset({"channel", "chat", "chat_type", "user", "workspace"}),
        sections={
            "delivery": frozenset(
                {"mode", "prompt", "mid_turn", "session", "reply", KEY_EVENTS}
            ),
            "agent": frozenset(
                {
                    "model_name",
                    "expert",
                    "subagents",
                    "skills",
                }
            ),
            "permissions": None,
            "clicks": frozenset({"approve", "stop", "queue_drop"}),
        },
        # Slack approval buttons let an ask remain ask.
        attended=True,
        identity_keys=("user",),
        # Slack supplies chat_type. Id prefixes cannot distinguish mpim from a room.
        axis_values={
            AXIS_CHAT_TYPE: _DM_CHAT_TYPES | _ROOM_CHAT_TYPES,
        },
        # Only group_chat_mode is a connector setting for the same scoped value.
        layer0_keys={
            "delivery": {"mode": "group_chat_mode"},
        },
        # Validate Slack vocabularies here. The shared loader validates generic ones.
        validators={
            "delivery.mode": _check_mode,
            f"delivery.{KEY_EVENTS}": _check_events,
            **({"agent.model_name": check_model_name} if check_model_name is not None else {}),
        },
        cautions={
            f"delivery.{KEY_EVENTS}": _caution_events,
        },
        # These values describe a conversation and cannot vary by sender.
        axis_restrictions={
            "delivery.session": AxisRestriction.only_on(
                AXIS_CHANNEL,
                AXIS_CHAT,
                AXIS_CHAT_TYPE,
                because=(
                    "How wide a session is belongs to the conversation rather than"
                    " to whoever is typing in it: settled per sender, one room"
                    " would hold one history for the people the rule names and"
                    " another for everybody else. Write it on {channel: slack}, on"
                    " one conversation, or on a kind of conversation -- every room"
                    " of that kind then says the same thing about itself"
                ),
            ),
            "delivery.reply": AxisRestriction.only_on(
                AXIS_CHANNEL,
                AXIS_CHAT,
                AXIS_CHAT_TYPE,
                because=(
                    "Whether an answer is owed belongs to the conversation rather"
                    " than to whoever is typing in it, and the reply is matched with"
                    " no sender to read, so a rule naming people would have the"
                    " token posted at the very people told to write it. Write it on"
                    " {channel: slack}, on one conversation, or on a kind of"
                    " conversation -- the kind reaches the outbound half, where a"
                    " sender does not"
                ),
            ),
            f"delivery.{KEY_EVENTS}": AxisRestriction.only_on(
                AXIS_CHANNEL,
                AXIS_CHAT,
                AXIS_CHAT_TYPE,
                because=(
                    "What a room feeds the model belongs to the conversation rather"
                    " than to whoever is typing in it: under context the events are"
                    " folded into whichever turn runs next, which need not be the"
                    " turn of the sender the rule names, and under turn the event"
                    " starts a turn of its own that no sender asked for. The person"
                    " who reacted is not a sender either way. Write it on"
                    " {channel: slack}, on one conversation, or on a kind of"
                    " conversation -- though an event Slack sends without a kind on"
                    " it is matched by no rule naming one"
                ),
            ),
        },
    )
