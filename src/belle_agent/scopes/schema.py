"""Compile scope rules into frozen matches and settings."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from types import MappingProxyType
from typing import Any

from belle_agent.scopes.capabilities import (
    ANY_KEY,
    AxisRestriction,
    ValueValidator,
    Warn,
    channel_capabilities,
    check_agent_names,
    check_expert,
    check_skills,
    freeze_nested,
    known_channels,
    restriction_in,
    text_keys,
)
from belle_agent.scopes.people import (
    EMPTY_DIRECTORY,
    PEOPLE_KEY,
    ROLES_KEY,
    PeopleDirectory,
    compile_people,
)

logger = logging.getLogger(__name__)

AXIS_CHANNEL = "channel"
AXIS_WORKSPACE = "workspace"
AXIS_CHAT = "chat"
AXIS_CHAT_TYPE = "chat_type"
AXIS_USER = "user"
AXIS_ROLE = "role"

MATCH_NOT = "not"

#: Matcher axes, from coarse to narrow.
SUPPORTED_AXES: tuple[str, ...] = (
    AXIS_CHANNEL,
    AXIS_WORKSPACE,
    AXIS_CHAT,
    AXIS_CHAT_TYPE,
    AXIS_USER,
    AXIS_ROLE,
)

#: Valid keys in a ``match:`` block.
SUPPORTED_MATCH_KEYS: tuple[str, ...] = (*SUPPORTED_AXES, MATCH_NOT)

#: Sender selectors combine with OR.
IDENTITY_AXES: tuple[str, ...] = (AXIS_USER, AXIS_ROLE)

NOT_AXES: tuple[str, ...] = IDENTITY_AXES

SECTION_DELIVERY = "delivery"
SECTION_AGENT = "agent"
SECTION_PERMISSIONS = "permissions"
SECTION_CLICKS = "clicks"

CLICK_APPROVE = "approve"
CLICK_STOP = "stop"
CLICK_QUEUE_DROP = "queue_drop"

LEVEL_ALLOW = "allow"
LEVEL_ASK = "ask"
LEVEL_DENY = "deny"

#: Tool permission levels, from least to most restrictive.
PERMISSION_LEVELS: tuple[str, ...] = (LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY)

KEY_MID_TURN = "mid_turn"

MID_TURN_CANCEL = "cancel"
MID_TURN_STEER = "steer"
MID_TURN_QUEUE = "queue"

MID_TURN_VALUES: tuple[str, ...] = (MID_TURN_QUEUE, MID_TURN_CANCEL, MID_TURN_STEER)

MID_TURN_DEFAULT = MID_TURN_QUEUE

KEY_SESSION = "session"

SESSION_THREAD = "thread"
SESSION_CHANNEL = "channel"

SESSION_VALUES: tuple[str, ...] = (SESSION_THREAD, SESSION_CHANNEL)

SESSION_DEFAULT = SESSION_THREAD

KEY_REPLY = "reply"

REPLY_REQUIRED = "required"
REPLY_OPTIONAL = "optional"

REPLY_VALUES: tuple[str, ...] = (REPLY_REQUIRED, REPLY_OPTIONAL)

REPLY_DEFAULT = REPLY_REQUIRED

_DELIVERY_VOCABULARIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        KEY_MID_TURN: MID_TURN_VALUES,
        KEY_SESSION: SESSION_VALUES,
        KEY_REPLY: REPLY_VALUES,
    }
)

PERMISSION_KEYS: frozenset[str] = frozenset({"tools"})

CLICK_KEYS: frozenset[str] = frozenset({CLICK_APPROVE, CLICK_STOP, CLICK_QUEUE_DROP})

#: Shared axis restrictions; connectors may narrow them.
MATCH_AXIS_RESTRICTIONS: Mapping[str, AxisRestriction] = MappingProxyType(
    {
        f"{SECTION_CLICKS}.{ANY_KEY}": AxisRestriction.only_on(
            AXIS_CHANNEL,
            AXIS_CHAT,
            because=(
                "It has no effect on a rule that also names a sender, chat type,"
                " or workspace. A click supplies only its conversation. Match"
                " the channel or chat instead"
            ),
        ),
        f"{SECTION_PERMISSIONS}.{ANY_KEY}": AxisRestriction.only_on(
            AXIS_CHANNEL,
            AXIS_CHAT,
            because=(
                "Its consumer is given the channel and the chat alone. Rules"
                " naming a sender, chat type, or workspace cannot take effect."
                " Match the channel or chat instead"
            ),
        ),
    }
)

#: For a key whose value describes the conversation: the host settles it with no sender to read.
PER_CONVERSATION = AxisRestriction.only_on(
    AXIS_CHANNEL,
    AXIS_CHAT,
    AXIS_CHAT_TYPE,
    because="It describes the conversation, and the host settles it with no sender to read",
)

SUPPORTED_SECTIONS: tuple[str, ...] = (
    SECTION_DELIVERY,
    SECTION_AGENT,
    SECTION_PERMISSIONS,
    SECTION_CLICKS,
)

#: Sections settled per conversation by the connector.
CONNECTOR_SECTIONS: tuple[str, ...] = (SECTION_DELIVERY, SECTION_AGENT)

ENTRY_KEYS: frozenset[str] = frozenset({"match", *SUPPORTED_SECTIONS})

APPEND_SUFFIX = "_append"


def _describe_identity(users: "tuple[str, ...] | None", roles: "tuple[str, ...] | None") -> "list[str]":
    """Describe user IDs and roles, preserving empty selectors."""
    parts: list[str] = []
    if users is not None:
        parts.append(f"{AXIS_USER}: [{', '.join(users)}]")
    if roles is not None:
        parts.append(f"{AXIS_ROLE}: [{', '.join(roles)}]")
    return parts


def _describe_kinds(kinds: "tuple[str, ...]") -> str:
    """Print one conversation kind bare or several as a list."""
    if len(kinds) == 1:
        return kinds[0]
    return f"[{', '.join(kinds)}]"


@dataclass(frozen=True)
class ScopeMatch:
    """Match requests by platform, conversation, workspace, and sender."""

    channel: "str | None" = None
    workspace: "str | None" = None
    chat: "str | None" = None
    chat_type: "tuple[str, ...] | None" = None
    users: "tuple[str, ...] | None" = None
    not_users: "tuple[str, ...] | None" = None
    roles: "tuple[str, ...] | None" = None
    not_roles: "tuple[str, ...] | None" = None

    @property
    def constrains_identity(self) -> bool:
        """An empty resolved role still constrains the sender."""
        return (
            self.users is not None or self.not_users is not None or self.roles is not None or self.not_roles is not None
        )

    @property
    def specificity(self) -> tuple[int, int, int, int]:
        """Order by platform, workspace, conversation, then sender."""
        sender_grain = 0
        if self.constrains_identity:
            sender_grain = 2 if self.users is not None and self.roles is None else 1
        return (
            1 if self.channel is not None else 0,
            1 if self.workspace is not None else 0,
            2 if self.chat is not None else 1 if self.chat_type is not None else 0,
            sender_grain,
        )

    def selects(
        self,
        *,
        channel: "str | None",
        chat: "str | None",
        chat_type: "str | None" = None,
        workspace: "str | None" = None,
        user: "str | None" = None,
    ) -> bool:
        """Select a request. Unknown senders fail positive lists and pass exclusions."""
        if self.channel is not None and self.channel != channel:
            return False
        if self.workspace is not None and self.workspace != workspace:
            return False
        if self.chat is not None and self.chat != chat:
            return False
        if self.chat_type is not None and (chat_type or "") not in self.chat_type:
            return False
        sender = (user or "").strip()
        if self.users is not None and sender not in self.users:
            return False
        return self.not_users is None or sender not in self.not_users

    def describe(self) -> str:
        """Format this match for logs and startup summaries."""
        parts = [
            f"{name}: {value}"
            for name, value in (
                (AXIS_CHANNEL, self.channel),
                (AXIS_WORKSPACE, self.workspace),
                (AXIS_CHAT, self.chat),
            )
            if value is not None
        ]
        if self.chat_type is not None:
            parts.append(f"{AXIS_CHAT_TYPE}: {_describe_kinds(self.chat_type)}")
        parts.extend(_describe_identity(self.users, self.roles))
        written = _describe_identity(self.not_users, self.not_roles)
        if written:
            parts.append(f"{MATCH_NOT}: {{{', '.join(written)}}}")
        return "{" + ", ".join(parts) + "}" if parts else "{}"


@dataclass(frozen=True)
class ClickRule:
    """Allowed clickers for one gesture. An empty list refuses everyone."""

    users: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()

    def permits(self, user_id: "str | None") -> bool:
        """Reject unidentified clickers and IDs outside the allowed set."""
        clicker = (user_id or "").strip()
        return bool(clicker) and clicker in self.users

    def describe(self) -> str:
        """Format this rule, including an empty refusal, for logs."""
        parts = _describe_identity(self.users, self.roles or None)
        return "{" + ", ".join(parts) + "}"


@dataclass(frozen=True)
class Scope:
    """One compiled rule. Its index breaks specificity ties."""

    match: ScopeMatch
    sections: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", freeze_nested(self.sections))

    @property
    def layer(self) -> tuple[int, int, int, int]:
        return self.match.specificity

    def selects(
        self,
        *,
        channel: "str | None",
        chat: "str | None",
        chat_type: "str | None" = None,
        workspace: "str | None" = None,
        user: "str | None" = None,
    ) -> bool:
        return self.match.selects(
            channel=channel,
            chat=chat,
            chat_type=chat_type,
            workspace=workspace,
            user=user,
        )

    def section(self, name: str) -> Mapping[str, Any]:
        return self.sections.get(name, {})


def signed_entries(value: Any) -> "tuple[str, ...] | None":
    """Return signed entries, or reject a mixed signed/plain list."""
    if not isinstance(value, (list, tuple)):
        return None
    entries = [str(item).strip() for item in value if isinstance(item, str)]
    if len(entries) != len(value):
        return None
    signed = [e for e in entries if e[:1] in ("+", "-")]
    if not signed:
        return None
    return tuple(signed) if len(signed) == len(entries) else ()


def _describe_list_mix(value: Sequence[Any]) -> str:
    return repr(list(value))


def _compile_string_items(
    values: Sequence[Any],
    *,
    position: str,
    label: str,
    invalid_item: str,
    singular: str,
    refuse: Warn,
) -> tuple[str, ...] | None:
    names: list[str] = []
    for item in values:
        if not isinstance(item, str):
            refuse(f"scopes%s.%s=%s {invalid_item}", position, label, _describe_list_mix(values))
            return None
        text = item.strip()
        if not text:
            refuse(f"scopes%s.%s contains an empty {singular}. Write each {singular} as text", position, label)
            return None
        names.append(text)
    return tuple(sorted(set(names)))


def _refuse_empty_selector(position: str, label: str, singular: str, refuse: Warn) -> None:
    # An empty selector could mean everyone or nobody.
    refuse("scopes%s.%s is an empty list. Write at least one %s", position, label, singular)


def _compile_ids(raw_value: Any, *, position: str, label: str, refuse: Warn) -> "tuple[str, ...] | None":
    """Compile identity IDs."""
    if isinstance(raw_value, str):
        refuse("scopes%s.%s=%r is one id. Write a list of ids: [%s]", position, label, raw_value, raw_value)
        return None
    if not isinstance(raw_value, (list, tuple)):
        refuse("scopes%s.%s=%r is not a list of ids", position, label, raw_value)
        return None
    ids = _compile_string_items(
        raw_value,
        position=position,
        label=label,
        invalid_item="is not a list of ids. Quote each id so that YAML keeps it as text",
        singular="id",
        refuse=refuse,
    )
    if ids == ():
        _refuse_empty_selector(position, label, "id", refuse)
        return None
    return ids


def _compile_names(
    raw_value: Any,
    *,
    position: str,
    label: str,
    singular: str,
    plural: str,
    refuse: Warn,
) -> "tuple[str, ...] | None":
    values = [raw_value] if isinstance(raw_value, str) else raw_value
    if not isinstance(values, (list, tuple)):
        refuse("scopes%s.%s=%r is not a %s or a list of %s", position, label, raw_value, singular, plural)
        return None
    names = _compile_string_items(
        values,
        position=position,
        label=label,
        invalid_item=f"is not a list of {plural}",
        singular=singular,
        refuse=refuse,
    )
    if names == ():
        _refuse_empty_selector(position, label, singular, refuse)
        return None
    return names


#: User IDs require a list; role and chat-type names accept a bare string.
_SELECTOR_PARSERS = {
    AXIS_USER: _compile_ids,
    AXIS_ROLE: partial(_compile_names, singular="role name", plural="role names"),
    AXIS_CHAT_TYPE: partial(_compile_names, singular="kind of conversation", plural="kinds of conversation"),
}


def _compile_not(raw: Any, *, position: str, refuse: Warn) -> "dict[str, tuple[str, ...]] | None":
    """Compile excluded users and roles."""
    if not isinstance(raw, Mapping) or not raw:
        refuse("scopes%s.match.%s=%r is not a mapping with %s", position, MATCH_NOT, raw, " or ".join(NOT_AXES))
        return None

    excluded: dict[str, tuple[str, ...]] = {}
    label = f"scopes{position}.match.{MATCH_NOT}"
    for key, raw_value in text_keys(raw, label=label, noun="sender selector", refuse=refuse).items():
        if key not in NOT_AXES:
            # Exclusion currently applies only to identity axes.
            refuse(
                "scopes%s.match.%s.%s is not a sender selector. %s takes %s only",
                position,
                MATCH_NOT,
                key,
                MATCH_NOT,
                " or ".join(NOT_AXES),
            )
            return None
        names = _SELECTOR_PARSERS[key](raw_value, position=position, label=f"match.{MATCH_NOT}.{key}", refuse=refuse)
        if names is None:
            return None
        excluded[key] = names

    return excluded


def _compile_match(raw: Any, *, position: str, refuse: Warn) -> "ScopeMatch | None":
    """Settle one ``match`` block, or ``None`` if it is invalid."""
    if raw is None:
        # A missing match block applies everywhere.
        return ScopeMatch()
    if not isinstance(raw, Mapping):
        refuse("scopes%s.match=%r is not a mapping. The match keys are %s", position, raw, ", ".join(SUPPORTED_MATCH_KEYS))
        return None

    values: dict[str, str] = {}
    selectors: dict[str, tuple[str, ...]] = {}
    not_users: tuple[str, ...] | None = None
    not_roles: tuple[str, ...] | None = None
    for key, raw_value in text_keys(raw, label=f"scopes{position}.match", noun="match key", refuse=refuse).items():
        if key == MATCH_NOT:
            excluded = _compile_not(raw_value, position=position, refuse=refuse)
            if excluded is None:
                return None
            not_users, not_roles = excluded.get(AXIS_USER), excluded.get(AXIS_ROLE)
            continue
        parser = _SELECTOR_PARSERS.get(key)
        if parser is not None:
            names = parser(raw_value, position=position, label=f"match.{key}", refuse=refuse)
            if names is None:
                return None
            selectors[key] = names
            continue
        if key not in SUPPORTED_AXES:
            refuse(
                "scopes%s.match.%s is not a match key. The match keys are %s",
                position,
                key,
                ", ".join(SUPPORTED_MATCH_KEYS),
            )
            return None
        if not isinstance(raw_value, str):
            refuse("scopes%s.match.%s=%r is not text. Quote the value so that YAML keeps it as text", position, key, raw_value)
            return None
        text = raw_value.strip()
        if not text:
            refuse("scopes%s.match.%s is empty. Write one value", position, key)
            return None
        values[key] = text

    return ScopeMatch(
        channel=values.get(AXIS_CHANNEL),
        workspace=values.get(AXIS_WORKSPACE),
        chat=values.get(AXIS_CHAT),
        chat_type=selectors.get(AXIS_CHAT_TYPE),
        users=selectors.get(AXIS_USER),
        not_users=not_users,
        roles=selectors.get(AXIS_ROLE),
        not_roles=not_roles,
    )


def _refuse_chat_with_chat_type(match: "ScopeMatch", *, position: str, refuse: Warn) -> bool:
    """Reject a rule naming both a chat and its kind."""
    if match.chat is None or match.chat_type is None:
        return True
    refuse(
        "scopes%s.match names both %s=%s and %s=%s. Write only one of them."
        " The pair is redundant when they agree and never matches when they differ",
        position,
        AXIS_CHAT,
        match.chat,
        AXIS_CHAT_TYPE,
        _describe_kinds(match.chat_type),
    )
    return False


def _require_channel(match: "ScopeMatch", *, position: str, refuse: Warn) -> bool:
    """Require a channel for platform-specific selectors."""
    if match.channel is not None:
        return True
    if match.chat is not None:
        named, value = AXIS_CHAT, match.chat
    elif match.chat_type is not None:
        named, value = AXIS_CHAT_TYPE, _describe_kinds(match.chat_type)
    else:
        named = None
    if named is not None:
        refuse(
            "scopes%s.match.%s=%s names a conversation but no channel. Add channel to the match block",
            position,
            named,
            value,
        )
    elif match.workspace is not None:
        refuse(
            "scopes%s.match.%s=%s names a workspace but no channel. Add channel to the match block",
            position,
            AXIS_WORKSPACE,
            match.workspace,
        )
    elif match.constrains_identity:
        named = [
            *(match.users or ()),
            *(match.not_users or ()),
            *(match.roles or ()),
            *(match.not_roles or ()),
        ]
        refuse(
            "scopes%s.match names senders (%s) but no channel. Add channel to the match block",
            position,
            ", ".join(named),
        )
    else:
        return True
    return False


def _identity_spelling(users: "tuple[str, ...] | None", roles: "tuple[str, ...] | None") -> str:
    """Name the identity spelling used in the source rule."""
    written = [name for name, value in ((AXIS_USER, users), (AXIS_ROLE, roles)) if value is not None]
    return "/".join(written) or AXIS_USER


def _match_axes(match: ScopeMatch) -> frozenset[str]:
    """Return source axes before role resolution, including exclusions."""
    axes = {
        axis for axis in (AXIS_CHANNEL, AXIS_WORKSPACE, AXIS_CHAT, AXIS_CHAT_TYPE) if getattr(match, axis) is not None
    }
    if match.users is not None or match.not_users is not None:
        axes.add(AXIS_USER)
    if match.roles is not None or match.not_roles is not None:
        axes.add(AXIS_ROLE)
    return frozenset(axes)


def _refuse_unknown_roles(
    names: "tuple[str, ...]",
    *,
    directory: PeopleDirectory,
    position: str,
    label: str,
    refuse: Warn,
) -> bool:
    """Reject roles absent from ``roles:``: nothing says who is in them."""
    unknown = sorted({name for name in names if not directory.knows_role(name)})
    if not unknown:
        return False
    refuse(
        "scopes%s.%s names %s, which %s not a valid role under %s. The valid roles are %s",
        position,
        label,
        ", ".join(unknown),
        "are" if len(unknown) > 1 else "is",
        ROLES_KEY,
        ", ".join(sorted(directory.roles)) or "none",
    )
    return True


def _resolve_roles(
    match: ScopeMatch,
    *,
    directory: PeopleDirectory,
    position: str,
    warn: Warn,
    refuse: Warn,
) -> "ScopeMatch | None":
    """Resolve roles, retaining declared roles without platform IDs."""
    if match.roles is None and match.not_roles is None:
        return match

    channel = match.channel
    named = (*(match.roles or ()), *(match.not_roles or ()))
    if _refuse_unknown_roles(
        named,
        directory=directory,
        position=position,
        label="match",
        refuse=refuse,
    ):
        return None

    def _ids(names: "tuple[str, ...] | None", *, excluding: bool) -> "tuple[str, ...] | None":
        if names is None:
            return None
        found: set[str] = set()
        for name in names:
            ids = directory.ids_for_role(name, channel=channel)
            if ids:
                found.update(ids)
                continue
            elsewhere = ", ".join(directory.channels_for_role(name)) or "no platform"
            label = f"{MATCH_NOT}.{AXIS_ROLE}" if excluding else AXIS_ROLE
            action = "excludes" if excluding else "adds"
            effect = " The restriction applies to them too." if excluding else ""
            warn(
                "scopes%s.match.%s=%s %s nobody on %s: nobody in that role"
                " has an id there.%s Add their %s id under %s; they are"
                " identified on %s",
                position,
                label,
                name,
                action,
                channel,
                effect,
                channel,
                PEOPLE_KEY,
                elsewhere,
            )
        return tuple(sorted(found))

    def _union(written: "tuple[str, ...] | None", from_roles: "tuple[str, ...] | None") -> "tuple[str, ...] | None":
        """The OR of the two spellings, as one set of ids (``IDENTITY_AXES``)."""
        if from_roles is None:
            return written
        return tuple(sorted(set(written or ()) | set(from_roles)))

    return replace(
        match,
        users=_union(match.users, _ids(match.roles, excluding=False)),
        not_users=_union(match.not_users, _ids(match.not_roles, excluding=True)),
    )


def _check_chat_type_value(
    match: ScopeMatch,
    capabilities: Any,
    *,
    position: str,
    warn: Warn,
    refuse: Warn,
) -> None:
    """Refuse a kind of conversation that the channel does not have."""
    if not capabilities.populates(AXIS_CHAT_TYPE):
        warn(
            "scopes%s.match.%s is set but %s does not say what kind of conversation this is. The rule will never match",
            position,
            AXIS_CHAT_TYPE,
            match.channel,
        )
        return
    declared = capabilities.values_for(AXIS_CHAT_TYPE)
    if declared is None:
        return
    unknown = tuple(kind for kind in match.chat_type if kind not in declared)
    if not unknown:
        return
    refuse(
        "scopes%s.match.%s names %s, which %s does not have. The kinds of conversation on %s are %s",
        position,
        AXIS_CHAT_TYPE,
        _describe_kinds(unknown),
        match.channel,
        match.channel,
        ", ".join(sorted(declared)) or "none",
    )


#: The platforms whose unused scopes this process has already reported.
_REPORTED_OFF: set[str] = set()


def _platform_is_off(channel: str) -> bool:
    """Whether ``channel`` names a platform that Belle has but this process does not run."""
    from belle_agent.platforms.base import platform_names  # The platform port imports this package.

    return channel_capabilities(channel) is None and channel in platform_names()


def _report_platforms_that_are_off(counts: Mapping[str, int]) -> None:
    """Say once per process, for each platform that is off, how many scopes do not apply."""
    for name in sorted(set(counts) - _REPORTED_OFF):
        _REPORTED_OFF.add(name)
        logger.info(
            "scopes: the platform %s is off in this process, so %d scope%s with match.channel=%s do%s not apply",
            name,
            counts[name],
            "" if counts[name] == 1 else "s",
            name,
            "es" if counts[name] == 1 else "",
        )


def _check_axes_against_capabilities(
    match: ScopeMatch, *, position: str, restricts: bool, warn: Warn, refuse: Warn
) -> None:
    """Refuse a channel that is not known, and report an axis that the channel cannot fill."""
    from belle_agent.platforms.base import platform_names  # The platform port imports this package.

    if match.channel is None:
        return
    capabilities = channel_capabilities(match.channel)
    if capabilities is None:
        # A misspelled name would leave the restrictions of the rule unenforced.
        refuse(
            "scopes%s.match.channel=%s is not a platform or a connector. The channels are %s",
            position,
            match.channel,
            ", ".join(sorted({*known_channels(), *platform_names()})),
        )
        return
    # A restriction whose match cannot hold would not apply, or would apply to the people it exempts.
    report = refuse if restricts else warn
    if match.chat is not None and not capabilities.populates(AXIS_CHAT):
        report(
            "scopes%s.match.chat is set but %s does not identify a conversation,"
            " so that scope will never match. Match on channel alone",
            position,
            match.channel,
        )
    if match.workspace is not None and not capabilities.populates(AXIS_WORKSPACE):
        report(
            "scopes%s.match.%s is set but %s does not say which workspace a"
            " request came from, so that scope will never match. Match on"
            " channel alone",
            position,
            AXIS_WORKSPACE,
            match.channel,
        )
    if match.chat_type is not None:
        _check_chat_type_value(match, capabilities, position=position, warn=warn, refuse=refuse)
    if match.constrains_identity and not capabilities.populates(AXIS_USER):
        if match.users is not None or match.roles is not None:
            report(
                "scopes%s.match.%s is set but %s does not identify a sender, so"
                " that scope will never match. Match on the conversation"
                " instead",
                position,
                _identity_spelling(match.users, match.roles),
                match.channel,
            )
        if match.not_users is not None or match.not_roles is not None:
            report(
                "scopes%s.match.%s excludes senders but %s does not identify"
                " one, so nobody is excluded and that scope applies to everyone"
                " there -- including whoever it was written to exempt",
                position,
                MATCH_NOT,
                match.channel,
            )


def _effective_axis_restriction(
    section: str,
    key: str,
    capabilities: "Any | None",
) -> "AxisRestriction | None":
    """Intersect shared and connector axis restrictions."""
    shared = restriction_in(MATCH_AXIS_RESTRICTIONS, section, key)
    declared = capabilities.axis_restriction(section, key) if capabilities is not None else None
    restriction = declared.narrowed_by(shared) if declared is not None else shared
    if restriction is None:
        return None

    unknown = sorted(restriction.allowed - set(SUPPORTED_AXES))
    if unknown:
        logger.warning(
            "scopes: the axis restriction on %s.%s permits %s, which %s not"
            " %s; the axes are %s. Nothing matches on it, so the restriction is"
            " narrower than it reads",
            section,
            key,
            ", ".join(unknown),
            "are" if len(unknown) > 1 else "is",
            "axes" if len(unknown) > 1 else "an axis",
            ", ".join(SUPPORTED_AXES),
        )
    return restriction


def _refuse_for_axis(
    restriction: AxisRestriction,
    matched_axes: "frozenset[str]",
    *,
    position: str,
    section: str,
    key: "str | None",
    refuse: Warn,
) -> bool:
    """Reject a section or key addressed on a barred axis."""
    refused = restriction.refuses(matched_axes)
    if not refused:
        return False
    refuse(
        "scopes%s.%s%s cannot be in a rule that matches on %s. %s. The permitted axes are %s",
        position,
        section,
        f".{key}" if key is not None else "",
        ", ".join(refused),
        restriction.because or "That axis is not one this key has a meaning on",
        restriction.describe(),
    )
    return True


def _settle_permission_tools(
    raw: Any,
    *,
    position: str,
    channel: "str | None",
    attended: bool,
    warn: Warn,
    refuse: Warn,
) -> "dict[str, str] | None":
    """Settle tool levels."""
    levels = "/".join(PERMISSION_LEVELS)
    if not isinstance(raw, Mapping):
        refuse("scopes%s.%s.tools=%r is not a mapping of tool name to %s", position, SECTION_PERMISSIONS, raw, levels)
        return None

    settled: dict[str, str] = {}
    label = f"scopes{position}.{SECTION_PERMISSIONS}.tools"
    for tool, raw_level in text_keys(raw, label=label, noun="tool name", refuse=refuse).items():
        level = str(raw_level).strip().lower() if isinstance(raw_level, str) else ""
        if level not in PERMISSION_LEVELS:
            refuse("scopes%s.%s.tools.%s=%r is not one of %s", position, SECTION_PERMISSIONS, tool, raw_level, levels)
            continue
        if level == LEVEL_ALLOW:
            warn(
                "scopes%s.%s.tools.%s=allow grants nothing. Scopes only tighten permissions. Use ask or deny",
                position,
                SECTION_PERMISSIONS,
                tool,
            )
        elif level == LEVEL_ASK and not attended:
            warn(
                "scopes%s.%s.tools.%s=ask asks a question nobody can answer on %s. It is enforced as deny",
                position,
                SECTION_PERMISSIONS,
                tool,
                channel,
            )
        settled[tool] = level

    return settled or None


def _attached_reason(reason: str) -> str:
    """Join a refusal reason to its key without duplicate spaces."""
    text = reason.lstrip()
    return text if text.startswith("=") else f" {text}"


def _report_key_reason(report: Warn, position: str, section: str, key: str, reason: str) -> None:
    report("scopes%s.%s.%s%s", position, section, key, _attached_reason(reason))


def _settle_closed_word(key: str, value: Any, *, values: "tuple[str, ...]", setting: str) -> "tuple[str | None, str]":
    """Validate a shared delivery setting."""
    listed = ", ".join(values)
    if key.endswith(APPEND_SUFFIX):
        return None, f" has nothing to append to. Set {setting} to one of {listed}"
    if isinstance(value, str):
        settled = value.strip().lower()
        if settled in values:
            return settled, ""
    return None, f"={value!r} is not one of {listed}"


def _settle_click_rule(
    raw: Any,
    *,
    position: str,
    kind: str,
    channel: "str | None",
    directory: PeopleDirectory,
    identifies_clicker: bool,
    warn: Warn,
    refuse: Warn,
) -> "ClickRule | None":
    """Compile a clicker restriction."""
    if not isinstance(raw, Mapping) or not raw:
        refuse(
            "scopes%s.%s.%s=%r is not a mapping with %s",
            position,
            SECTION_CLICKS,
            kind,
            raw,
            " or ".join(IDENTITY_AXES),
        )
        return None

    selectors: dict[str, tuple[str, ...]] = {}
    written = text_keys(raw, label=f"scopes{position}.{SECTION_CLICKS}.{kind}", noun="clicker selector", refuse=refuse)
    for key, raw_value in written.items():
        label = f"{SECTION_CLICKS}.{kind}.{key}"
        if key not in IDENTITY_AXES:
            refuse(
                "scopes%s.%s.%s.%s is not a clicker selector. Write %s",
                position,
                SECTION_CLICKS,
                kind,
                key,
                " or ".join(IDENTITY_AXES),
            )
            return None
        names = _SELECTOR_PARSERS[key](raw_value, position=position, label=label, refuse=refuse)
        if names is None:
            return None
        selectors[key] = names

    users = selectors.get(AXIS_USER)
    roles = selectors.get(AXIS_ROLE)
    settled = set(users or ())
    if roles is not None:
        # A role resolves to IDs per platform, so it needs the platform.
        if channel is None:
            refuse(
                "scopes%s.%s.%s.%s=%s names a role but the match names no channel. Add channel to the match block",
                position,
                SECTION_CLICKS,
                kind,
                AXIS_ROLE,
                ", ".join(roles),
            )
            return None
        if _refuse_unknown_roles(
            roles,
            directory=directory,
            position=position,
            label=f"{SECTION_CLICKS}.{kind}",
            refuse=refuse,
        ):
            return None
        for name in roles:
            ids = directory.ids_for_role(name, channel=channel)
            if ids:
                settled.update(ids)
                continue
            elsewhere = ", ".join(directory.channels_for_role(name)) or "no platform"
            warn(
                "scopes%s.%s.%s.%s=%s admits nobody on %s. A click is refused unless another selector admits its user."
                " Add a %s id under %s. IDs exist on %s",
                position,
                SECTION_CLICKS,
                kind,
                AXIS_ROLE,
                name,
                channel,
                channel,
                PEOPLE_KEY,
                elsewhere,
            )

    if not identifies_clicker:
        warn(
            "scopes%s.%s.%s says who may click but %s does not identify who"
            " clicked. Every %s click is refused rather than gated",
            position,
            SECTION_CLICKS,
            kind,
            channel,
            kind,
        )

    return ClickRule(users=tuple(sorted(settled)), roles=roles or ())


#: Checks of values whose form is the same on every platform.
_SHARED_VALIDATORS: Mapping[str, ValueValidator] = MappingProxyType(
    {
        f"{SECTION_AGENT}.expert": check_expert,
        f"{SECTION_AGENT}.subagents": check_agent_names,
        f"{SECTION_AGENT}.skills": check_skills,
    }
)


def _value_refusals(section: str, key: str, value: Any, channel: "str | None") -> list[str]:
    """Check a value with the shared check and the check of each channel that reads it.

    A rule with no channel applies on every registered channel, so each of their checks applies.
    """
    shared = _SHARED_VALIDATORS.get(f"{section}.{key}")
    checks: list[tuple[str | None, ValueValidator]] = [(None, shared)] if shared is not None else []
    for name in (channel,) if channel is not None else known_channels():
        capabilities = channel_capabilities(name)
        validator = capabilities.validator(section, key) if capabilities and capabilities.acts_on(section, key) else None
        if validator is not None:
            checks.append((None if channel is not None else name, validator))
    grouped: dict[str, list[str]] = {}
    for where, check in checks:
        reason = check(value)
        if reason:
            grouped.setdefault(reason, []).extend([where] if where else [])
    return [
        reason + (f". A rule with no channel applies on {', '.join(names)}" if names else "")
        for reason, names in grouped.items()
    ]


#: Keys of the sections that Belle settles itself, whatever the connector.
_SHARED_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {SECTION_CLICKS: CLICK_KEYS, SECTION_PERMISSIONS: PERMISSION_KEYS}
)


def _compile_section(
    name: str,
    raw: Any,
    match: ScopeMatch,
    *,
    position: str,
    matched_axes: "frozenset[str]",
    channels_config: Mapping[str, Any],
    directory: PeopleDirectory = EMPTY_DIRECTORY,
    warn: Warn,
    refuse: Warn,
) -> dict[str, Any]:
    """Compile valid keys, naming source axes in messages."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        refuse("scopes%s.%s=%r is not a mapping of settings", position, name, raw)
        return {}

    capabilities = channel_capabilities(match.channel) if match.channel is not None else None
    if capabilities is not None and not capabilities.reads(name):
        warn(
            "scopes%s.%s has no effect: %s does not read the %s section. Supported: %s",
            position,
            name,
            match.channel,
            name,
            ", ".join(sorted(capabilities.sections)) or "none",
        )
        return {}

    # Section and key restrictions both apply.
    section_restriction = _effective_axis_restriction(name, ANY_KEY, capabilities)
    if section_restriction is not None and _refuse_for_axis(
        section_restriction,
        matched_axes,
        position=position,
        section=name,
        key=None,
        refuse=refuse,
    ):
        return {}

    connector_block = channels_config.get(match.channel) if match.channel else None
    connector_block = connector_block if isinstance(connector_block, Mapping) else {}

    settled: dict[str, Any] = {}
    for key, value in text_keys(raw, label=f"scopes{position}.{name}", noun="setting", refuse=refuse).items():
        base_key = key.removesuffix(APPEND_SUFFIX)

        shared_keys = _SHARED_KEYS.get(name)
        if shared_keys is not None and base_key not in shared_keys:
            refuse(
                "scopes%s.%s.%s is not a %s setting. The settings are %s",
                position,
                name,
                key,
                name,
                ", ".join(sorted(shared_keys)),
            )
            continue

        if capabilities is not None and not capabilities.acts_on(name, base_key):
            allowed = capabilities.keys_for(name)
            warn(
                "scopes%s.%s.%s has no effect: %s ignores it. The %s settings it reads are %s",
                position,
                name,
                key,
                match.channel,
                name,
                ", ".join(sorted(allowed)) if allowed else "none",
            )
            continue

        key_restriction = _effective_axis_restriction(name, base_key, capabilities)
        if key_restriction is not None and _refuse_for_axis(
            key_restriction,
            matched_axes,
            position=position,
            section=name,
            key=key,
            refuse=refuse,
        ):
            continue

        if key.endswith(APPEND_SUFFIX):
            if not isinstance(value, str):
                refuse("scopes%s.%s.%s=%r is not text. Write the text to append to %s", position, name, key, value, base_key)
                continue
        elif isinstance(value, (list, tuple)):
            signed = signed_entries(value)
            if signed == ():
                refuse(
                    "scopes%s.%s.%s=%s mixes plain entries with +/- entries. Sign all entries or none",
                    position,
                    name,
                    key,
                    _describe_list_mix(value),
                )
                continue
            if signed is not None and any(len(entry) < 2 for entry in signed):
                refuse(
                    "scopes%s.%s.%s=%s has a + or - with no name after it",
                    position,
                    name,
                    key,
                    _describe_list_mix(value),
                )
                continue
            if signed is None and any(not isinstance(item, str) for item in value):
                refuse("scopes%s.%s.%s=%s is not a list of names", position, name, key, _describe_list_mix(value))
                continue

        if name == SECTION_CLICKS:
            rule = _settle_click_rule(
                value,
                position=position,
                kind=key,
                channel=match.channel,
                directory=directory,
                identifies_clicker=(capabilities.populates(AXIS_USER) if capabilities is not None else True),
                warn=warn,
                refuse=refuse,
            )
            if rule is None:
                continue
            settled[key] = rule
            continue

        if name == SECTION_PERMISSIONS:
            settled_tools = _settle_permission_tools(
                value,
                position=position,
                channel=match.channel,
                attended=capabilities.attended if capabilities is not None else True,
                warn=warn,
                refuse=refuse,
            )
            if settled_tools is None:
                continue
            settled[key] = settled_tools
            continue

        closed = _DELIVERY_VOCABULARIES.get(base_key) if name == SECTION_DELIVERY else None
        if closed is not None:
            settled_word, refusal = _settle_closed_word(key, value, values=closed, setting=base_key)
            if settled_word is None:
                _report_key_reason(refuse, position, name, key, refusal)
                continue
            value = settled_word

        refusals = _value_refusals(name, base_key, value, match.channel)
        for reason in refusals:
            _report_key_reason(refuse, position, name, key, reason)
        if refusals:
            continue

        caution = capabilities.caution(name, base_key) if capabilities else None
        if caution is not None:
            note = caution(value, match)
            if note:
                _report_key_reason(warn, position, name, key, note)

        # Warn when a platform-wide scope shadows its connector default.
        if capabilities is not None and match.chat is None and match.chat_type is None and match.workspace is None:
            layer0 = capabilities.layer0_key(name, base_key)
            if layer0 is not None and connector_block.get(layer0) not in (None, "", [], {}):
                source = f"Belle platforms.{match.channel}" if match.channel == "slack" else f"channels.{match.channel}"
                warn(
                    "scopes%s.%s.%s and %s.%s both set this. The connector setting now governs nothing. Keep one setting",
                    position,
                    name,
                    key,
                    source,
                    layer0,
                )

        settled[key] = value

    return settled


def compile_scopes(
    raw: Any,
    *,
    channels_config: "Mapping[str, Any] | None" = None,
    people: Any = None,
    roles: Any = None,
    directory: "PeopleDirectory | None" = None,
    warn: "Warn | None" = None,
) -> tuple[Scope, ...]:
    """Compile scope rules.

    Raise ``BelleConfigError`` that names every invalid entry: a rule that Belle
    cannot apply as written must not leave its restrictions out. ``warn`` gets
    the notes about valid rules, such as a setting that a connector ignores.
    """
    # Imported here: the config module needs yaml, and the control protocol imports scopes without it.
    from belle_agent.config import BelleConfigError

    emit: Warn = warn if warn is not None else logger.warning
    problems: list[str] = []

    def refuse(message: str, *args: Any) -> None:
        problems.append(message % args)

    config = channels_config if isinstance(channels_config, Mapping) else {}
    if directory is None:
        directory = compile_people(people, roles)

    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BelleConfigError(f"Belle config scopes={raw!r} is not a list of rules")

    compiled: list[Scope] = []
    off: dict[str, int] = {}
    for index, entry in enumerate(raw):
        position = f"[{index}]"
        if entry is None:
            continue
        if not isinstance(entry, Mapping):
            refuse("scopes%s=%r is not a rule. A rule is a mapping with match and the sections it sets", position, entry)
            continue

        for unknown in sorted(str(key) for key in entry if str(key) not in ENTRY_KEYS):
            refuse(
                "scopes%s.%s is not a rule key. The rule keys are match, %s",
                position,
                unknown,
                ", ".join(SUPPORTED_SECTIONS),
            )

        match = _compile_match(entry.get("match"), position=position, refuse=refuse)
        if match is None:
            continue
        if not _refuse_chat_with_chat_type(match, position=position, refuse=refuse) or not _require_channel(
            match, position=position, refuse=refuse
        ):
            continue
        if match.channel is not None and _platform_is_off(match.channel):
            off[match.channel] = off.get(match.channel, 0) + 1
        else:
            # Check source axes before role resolution changes their spelling.
            _check_axes_against_capabilities(
                match,
                position=position,
                restricts=SECTION_PERMISSIONS in entry or SECTION_CLICKS in entry,
                warn=emit,
                refuse=refuse,
            )
        matched_axes = _match_axes(match)
        resolved = _resolve_roles(match, directory=directory, position=position, warn=emit, refuse=refuse)
        if resolved is None:
            continue
        match = resolved

        sections: dict[str, Mapping[str, Any]] = {}
        for name in SUPPORTED_SECTIONS:
            if name not in entry:
                continue
            settled = _compile_section(
                name,
                entry.get(name),
                match,
                position=position,
                matched_axes=matched_axes,
                channels_config=config,
                directory=directory,
                warn=emit,
                refuse=refuse,
            )
            if settled:
                sections[name] = settled

        if not sections:
            continue
        compiled.append(Scope(match=match, sections=sections, index=index))

    if problems:
        raise BelleConfigError("Belle config scopes is not valid. Correct these entries:\n" + "\n".join(problems))
    _report_platforms_that_are_off(off)
    return tuple(compiled)


def matching_scopes(
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
    chat_type: "str | None" = None,
    workspace: "str | None" = None,
    user: "str | None" = None,
    section: str = SECTION_DELIVERY,
) -> tuple[Scope, ...]:
    """Return matches in cascade order: specificity, then file position.

    Later rules win within a specificity tier.
    """
    applicable = [
        scope
        for scope in scopes
        if scope.section(section)
        and scope.selects(
            channel=channel,
            chat=chat,
            chat_type=chat_type,
            workspace=workspace,
            user=user,
        )
    ]
    applicable.sort(key=lambda scope: (scope.layer, scope.index))
    return tuple(applicable)
