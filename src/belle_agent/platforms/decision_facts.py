"""Shared decision facts for inbound messages."""

from collections.abc import Mapping
from typing import Any

from belle_agent.message_decisions import message_entry
from belle_agent.platforms.base import TRIGGER_DM, InboundMessage


def inbound_decision_facts(
    inbound: InboundMessage, *, author: str, bot_name: str, kinds: Mapping[str, str],
    last_message: Mapping[str, Any] | None = None, identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build decision facts; unknown chat types have kind ``unknown``."""
    return {
        "channel": inbound.conversation,
        "bot_name": bot_name,
        "last_message": dict(last_message) if last_message is not None else message_entry(
            author, inbound.text, ts=inbound.message_id, thread_ts=inbound.thread, file_count=len(inbound.files)
        ),
        "conversation_kind": kinds.get(inbound.chat_type, "unknown"),
        "identity": dict(identity) if identity is not None else {
            "workspace": inbound.workspace,
            "channel": inbound.conversation,
            "ts": inbound.message_id,
            "thread_ts": inbound.thread,
            "user": inbound.sender,
            "chat_type": inbound.chat_type,
            "is_dm": inbound.trigger == TRIGGER_DM,
            "trigger": inbound.trigger,
        },
    }
