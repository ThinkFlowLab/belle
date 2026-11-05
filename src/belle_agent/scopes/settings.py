"""Compose platform-neutral delivery and agent settings."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from typing import Any

from belle_agent.admission.policy import KEY_EVENTS
from belle_agent.scopes import (
    KEY_MID_TURN,
    KEY_REPLY,
    KEY_SESSION,
    SECTION_AGENT,
    SECTION_DELIVERY,
    Scope,
    compose_section,
    scoped_chats,
)


@dataclass(frozen=True)
class ConversationOverride:
    """Settled delivery and agent settings for one conversation."""

    mode: frozenset[str] | None = None
    prompt: str | None = None
    model_name: str | None = None
    mid_turn: str | None = None
    session: str | None = None
    reply: str | None = None
    events: dict[str, str] | None = None
    expert: str | None = None
    subagents: frozenset[str] | None = None
    skills: dict[str, list[str]] | None = None


def sections_as_override(
    sections: Mapping[str, Mapping[str, Any]],
    *,
    parse_mode: Callable[[Any], frozenset[str]],
    parse_events: Callable[[Any], dict[str, str] | None],
) -> ConversationOverride:
    """Convert composed scope sections to conversation settings."""
    delivery = sections.get(SECTION_DELIVERY) or {}
    agent = sections.get(SECTION_AGENT) or {}
    mode = delivery.get("mode")
    prompt = delivery.get("prompt")
    mid_turn = delivery.get(KEY_MID_TURN)
    session = delivery.get(KEY_SESSION)
    reply = delivery.get(KEY_REPLY)
    events = delivery.get(KEY_EVENTS)
    model_name = agent.get("model_name")
    expert = agent.get("expert")
    subagents = agent.get("subagents")
    skills = agent.get("skills")
    return ConversationOverride(
        mode=parse_mode(mode) if mode is not None else None,
        prompt=str(prompt) if prompt is not None else None,
        # The loader has already checked these closed vocabularies.
        mid_turn=str(mid_turn) if mid_turn is not None else None,
        session=str(session) if session is not None else None,
        reply=str(reply) if reply is not None else None,
        # Event policies can also arrive from callers outside the loader.
        events=parse_events(events),
        model_name=(str(model_name).strip() or None) if model_name is not None else None,
        expert=expert if isinstance(expert, str) else None,
        subagents=(frozenset(subagents) if isinstance(subagents, (list, tuple, set, frozenset)) else None),
        skills={key: list(names) for key, names in skills.items()} if isinstance(skills, Mapping) else None,
    )


def _compose_sections(
    scopes: Sequence[Scope],
    *,
    channel: str,
    chat: str | None,
    layer0: Mapping[str, Any],
    chat_type: str | None = None,
    workspace: str | None = None,
    user: str | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        section: compose_section(
            scopes,
            channel=channel,
            chat=chat,
            chat_type=chat_type,
            workspace=workspace,
            user=user,
            section=section,
            layer0=layer0 if section == SECTION_DELIVERY else None,
        )
        for section in (SECTION_DELIVERY, SECTION_AGENT)
    }


def settled_override(
    scopes: Sequence[Scope],
    channel_id: str,
    *,
    channel: str,
    conversation_overrides: Mapping[str, ConversationOverride],
    platform_override: ConversationOverride,
    layer0: Mapping[str, Any],
    convert: Callable[[Mapping[str, Mapping[str, Any]]], ConversationOverride],
    user_id: str = "",
    chat_type: str = "",
    workspace: str = "",
) -> ConversationOverride:
    """Recompose contextual scopes, otherwise inherit cached conversation values."""
    if any(
        (user_id and scope.match.constrains_identity)
        or (chat_type and scope.match.chat_type is not None)
        or (workspace and scope.match.workspace is not None)
        for scope in scopes
    ):
        return convert(
            _compose_sections(
                scopes,
                channel=channel,
                chat=channel_id or None,
                chat_type=chat_type or None,
                workspace=workspace or None,
                user=user_id,
                layer0=layer0,
            )
        )

    conversation = (conversation_overrides or {}).get(channel_id)
    platform = platform_override
    if conversation is None:
        return platform
    # None inherits the platform value. An empty value overrides it. Event
    # families were already composed and must not be merged a second time.
    return replace(
        platform,
        **{
            item.name: value for item in fields(conversation) if (value := getattr(conversation, item.name)) is not None
        },
    )


def compose_overrides(
    scopes: Sequence[Scope],
    *,
    channel: str,
    layer0: Mapping[str, Any],
    convert: Callable[[Mapping[str, Mapping[str, Any]]], ConversationOverride],
) -> tuple[ConversationOverride, dict[str, ConversationOverride]]:
    """Cache platform and named-chat settings; retain empty named-chat values."""

    def compose(chat: str | None) -> ConversationOverride:
        return convert(_compose_sections(scopes, channel=channel, chat=chat, layer0=layer0))

    return compose(None), {chat: compose(chat) for chat in sorted(scoped_chats(scopes, channel=channel))}
