"""Slack decision questions, advisory answers, and shadow records."""

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from belle_agent import message_decisions
from belle_agent.message_decisions import (
    ADVISORY_PREAMBLE,
    ALREADY_ANSWERED,
    AUDIENCE,
    AUDIENCE_BOT,
    AUDIENCE_NOBODY,
    AUDIENCE_PERSON,
    AUDIENCE_ROOM,
    CLAIM,
    DESERVES_EMOJI,
    DISPOSITION_CONTEXT,
    DISPOSITION_HERE,
    DISPOSITION_LATER,
    DISPOSITION_SILENT,
    DISPOSITIONS,
    HIGH,
    LOG_VERSION,
    NEEDS_WORK,
    QUESTION_SET_VERSION,
    QUESTIONS,
    RECENT_MESSAGE_DEPTH,
    RESPONSE_REQUESTED,
    SHADOW_BUDGET_SECONDS,
    SHADOW_LABEL,
    STATE_VERSION,
    WORTH_REMEMBERING,
    QuestionConfigurationError,
    RecentMessages,
    answer_record,
    decision_record,
    disposition,
    disposition_is_compatible,
    format_advisory,
    format_decision,
    load_questions,
    message_entry,
    question_changes,
    question_definitions,
    question_set_hash,
)
from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.decision_facts import inbound_decision_facts
from belle_agent.platforms.slack.file_transfer import event_entries
from belle_agent.platforms.slack.identity import event_team_id
from belle_agent.typed_decision import Question

logger = logging.getLogger(__name__)

def bot_identity(bot_name: str, bot_user_id: str) -> str:
    """Include the bot name and Slack mention token for addressee detection."""
    token = str(bot_user_id or "").strip()
    return message_decisions.bot_identity(bot_name, f"<@{token}>" if token else "")


#: Slack's word for a kind of conversation, as the question state names it.
CONVERSATION_KINDS: Mapping[str, str] = {
    "im": message_decisions.KIND_DIRECT,
    "mpim": message_decisions.KIND_GROUP,
    "channel": message_decisions.KIND_PUBLIC,
    "group": message_decisions.KIND_PRIVATE,
}


def conversation_kind(chat_type: str) -> str:
    return CONVERSATION_KINDS.get(chat_type, "unknown")


def build_state(
    *,
    channel: str,
    bot_name: str,
    recent_messages: Sequence[Mapping[str, Any]],
    last_message: Mapping[str, Any],
    members: Iterable[str] | None = None,
    chat_type: str = "",
) -> dict[str, Any]:
    """Build question state from supported message facts, including bot turns."""
    return message_decisions.build_state(
        channel=channel,
        bot_name=bot_name,
        recent_messages=recent_messages,
        last_message=last_message,
        members=members,
        conversation_kind=conversation_kind(chat_type),
    )


_TAG = "[SlackChannel]"

#: Prefix for one-line JSON shadow records.
LOG_PREFIX = f"{_TAG} {SHADOW_LABEL}"


class ShadowDecisionGate(message_decisions.ShadowDecisionGate):
    """The gate as the Slack channel uses it: its lines under this module's name, and Slack's kinds of conversation."""

    _log = logger
    _tag = _TAG


class SlackDecisionObserver:
    """Observe Slack messages through a connector-provided decision gate."""

    def _init_decisions(self) -> None:
        # False records that no decision model is configured.
        self._decision_gate: ShadowDecisionGate | None | bool = None

    async def _close_decisions(self) -> None:
        gate = self._decision_gate
        self._decision_gate = None
        if isinstance(gate, ShadowDecisionGate):
            try:
                await gate.close()
            except Exception:
                logger.debug("[SlackChannel] shadow decisions unstopped", exc_info=True)

    def _load_decision_questions(self) -> dict[str, Question]:
        path = self.config.decision_questions_file
        if path:
            if not isinstance(path, str):
                raise QuestionConfigurationError("question file path must be a string")
            path = Path(path).expanduser()
        return load_questions(path)

    @staticmethod
    def _decision_timestamp(value: str) -> Decimal | None:
        return Decimal(value) if re.fullmatch(r"[0-9]+\.[0-9]+", value) else None

    async def _decision_window(
        self, channel_id: str, limit: int, before_ts: str
    ) -> list[dict[str, Any]]:
        """Read earlier messages in this conversation for the decision model.

        Include bot messages. Exclude the target message and newer messages."""
        client = self._client
        cutoff = self._decision_timestamp(before_ts)
        if client is None or not channel_id or cutoff is None:
            return []
        response = await client.conversations_history(
            channel=channel_id, limit=max(1, int(limit)), latest=before_ts, inclusive=False
        )
        messages = response.get("messages") or []
        bot_user_id = self._identity.bot_user_id
        window: list[dict[str, Any]] = []
        # Slack returns newest first and the state reads oldest first, which is
        # the order the conversation happened in.
        for raw in reversed(list(messages)):
            if not isinstance(raw, dict):
                continue
            timestamp = str(raw.get("ts") or "")
            # Exclude the target and newer messages even if the API includes them.
            parsed_ts = self._decision_timestamp(timestamp)
            if parsed_ts is None or parsed_ts >= cutoff:
                continue
            author = str(raw.get("user") or raw.get("bot_id") or "").strip()
            label = (
                self._bot_display_name()
                if author and author == bot_user_id
                else f"<@{author}>" if author else "someone"
            )
            window.append(message_entry(
                label,
                str(raw.get("text") or ""),
                ts=str(raw.get("ts") or ""),
                thread_ts=str(raw.get("thread_ts") or ""),
                file_count=len(event_entries(raw, "files")),
                attachment_count=len(event_entries(raw, "attachments")),
            ))
        return window

    def _observe_decision_in_shadow(
        self, event: Mapping[str, Any], body: Mapping[str, Any], **kwargs: Any
    ) -> None:
        """Observe a message only if forward advice is off."""
        if self.config.decision_forward:
            return
        self._observe_queued_decision(
            event, body, failure_note="shadow decision skipped", **kwargs
        )

    def _decision_input(
        self,
        event: Mapping[str, Any],
        body: Mapping[str, Any],
        *,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        user_id: str,
        chat_type: str,
        is_dm: bool,
        trigger: str,
        text: str,
    ) -> dict[str, Any]:
        """Build the same message-specific input for observation and advice."""
        inbound = InboundMessage(
            platform="slack", workspace=event_team_id(body, event), conversation=channel_id,
            message_id=message_ts, sender=user_id, text=text, thread=thread_ts,
            chat_type=chat_type, trigger=trigger,
        )
        return inbound_decision_facts(
            inbound, author=f"<@{user_id}>",
            bot_name=bot_identity(self._bot_display_name(), self._identity.bot_user_id),
            kinds=CONVERSATION_KINDS,
            last_message=message_entry(
                f"<@{user_id}>", text, ts=message_ts, thread_ts=thread_ts,
                file_count=len(event_entries(event, "files")),
                attachment_count=len(event_entries(event, "attachments")),
            ),
            identity={
                "team": event_team_id(body, event),
                "channel": channel_id,
                "ts": message_ts,
                "thread_ts": thread_ts,
                "user": user_id,
                "chat_type": chat_type,
                "is_dm": is_dm,
                "trigger": trigger,
            },
        )

    async def _decision_advice_for_turn(
        self, event: Mapping[str, Any], body: Mapping[str, Any], **kwargs: Any
    ) -> str:
        """Return advice for this event only; failure leaves the turn unchanged."""
        if not self.config.decision_forward:
            return ""
        try:
            gate = self._shadow_decision_gate()
            if gate is None:
                return ""
            return await gate.advise(**self._decision_input(event, body, **kwargs))
        except Exception:
            logger.debug("[SlackChannel] decision advice unavailable", exc_info=True)
            return ""

    def _observe_queued_decision(
        self, event: Mapping[str, Any], body: Mapping[str, Any],
        *, failure_note: str = "queued shadow decision skipped", **kwargs: Any,
    ) -> None:
        """Keep a queued arrival in shadow without storing stale advice."""
        try:
            gate = self._shadow_decision_gate()
            if gate is not None:
                gate.observe(**self._decision_input(event, body, **kwargs))
        except Exception:
            logger.debug("[SlackChannel] %s", failure_note, exc_info=True)


__all__ = [
    "ADVISORY_PREAMBLE",
    "ALREADY_ANSWERED",
    "AUDIENCE",
    "AUDIENCE_BOT",
    "AUDIENCE_NOBODY",
    "AUDIENCE_PERSON",
    "AUDIENCE_ROOM",
    "CLAIM",
    "CONVERSATION_KINDS",
    "DESERVES_EMOJI",
    "DISPOSITIONS",
    "DISPOSITION_CONTEXT",
    "DISPOSITION_HERE",
    "DISPOSITION_LATER",
    "DISPOSITION_SILENT",
    "HIGH",
    "LOG_PREFIX",
    "LOG_VERSION",
    "NEEDS_WORK",
    "QUESTIONS",
    "QUESTION_SET_VERSION",
    "RECENT_MESSAGE_DEPTH",
    "RESPONSE_REQUESTED",
    "SHADOW_BUDGET_SECONDS",
    "STATE_VERSION",
    "WORTH_REMEMBERING",
    "QuestionConfigurationError",
    "RecentMessages",
    "ShadowDecisionGate",
    "SlackDecisionObserver",
    "answer_record",
    "bot_identity",
    "build_state",
    "conversation_kind",
    "decision_record",
    "disposition",
    "disposition_is_compatible",
    "format_advisory",
    "format_decision",
    "load_questions",
    "message_entry",
    "question_changes",
    "question_definitions",
    "question_set_hash",
]
