"""Interactive questions for the JiuwenSwarm Slack channel."""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.extensions.sdk.channel_runtime import RoutingTarget
from jiuwenswarm.extensions.sdk.host_types import Message

from belle_agent.integrations.jiuwenswarm.slack_inbound import _INTERRUPT_RESUME_SOURCES
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack import inputs as slack_inputs
from belle_agent.platforms.slack import questions as slack_questions
from belle_agent.platforms.slack.admission import SlackTurnInitiator
from belle_agent.platforms.slack.delivery import SlackDeliveryError
from belle_agent.platforms.slack.questions import QuestionPresenter
from belle_agent.platforms.slack.text import MAX_SLACK_TEXT_LENGTH

logger = logging.getLogger("belle_agent.integrations.jiuwenswarm.slack_channel")
_clamp = slack_inputs.clamp
_MAX_SLACK_TEXT_LENGTH = MAX_SLACK_TEXT_LENGTH

# The action ids are Slack's side of a question, so the platform package states them.
_QUESTION_ACTION_ID_PREFIX = slack_questions.QUESTION_ACTION_ID_PREFIX
_QUESTION_SUBMIT_ACTION_ID = slack_questions.QUESTION_SUBMIT_ACTION_ID
_QUESTION_INPUT_ACTION_ID_PREFIX = slack_questions.QUESTION_INPUT_ACTION_ID_PREFIX
_QUESTION_ACTION_ID_RE = slack_questions.QUESTION_ACTION_ID_RE

# Slack accepts only primary and danger button styles.
_BUTTON_STYLE_PRIMARY = "primary"
_BUTTON_STYLE_DANGER = "danger"

_WITHDRAWN_QUESTION_NOTICE = (
    "_This request is no longer waiting for an answer: an ordinary message sent "
    "while it was pending withdrew it, and the task carried on without it. Ask "
    "again if it still needs doing._"
)


def _option_button_style(intent: str, value: str) -> str:
    """Select the Slack button style for an option."""
    # Defer the rail package import until a button needs styling.
    from jiuwenswarm.extensions.sdk.permission_options import (
        ALLOW_ONCE,
        REJECT,
        resolve_permission_action,
    )

    action = resolve_permission_action(intent) if intent else None
    if action is None:
        action = resolve_permission_action(value)
    if action == REJECT:
        return _BUTTON_STYLE_DANGER
    if action == ALLOW_ONCE:
        return _BUTTON_STYLE_PRIMARY
    return ""


_QUESTION_PRESENTER = QuestionPresenter(
    action_id_prefix=_QUESTION_ACTION_ID_PREFIX, option_style=_option_button_style, logger=logger
)


@dataclass
class _SlackPendingQuestion:
    """Keep a posted question available for answer and stale-click checks."""

    request_id: str
    session_id: str
    source: str
    question: str
    labels: list[str]
    values: list[str]
    inputs: list[slack_inputs.QuestionInput] = field(default_factory=list)
    channel_id: str = ""
    message_ts: str = ""
    notification_text: str = ""
    # The turn ID survives an interrupt that changes the question request ID.
    turn_request_id: str = ""
    initiator: SlackTurnInitiator | None = None
    # Once withdrawn, stale clicks must remain identifiable.
    withdrawn_at: float | None = None
    touched_at: float = field(default_factory=time.monotonic)


class SlackQuestionMixin:
    async def _send_question(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> None:
        """Post an interactive question and record its pending answer."""
        parsed = slack_questions.extract_request(getattr(msg, "payload", None))
        if parsed is None:
            logger.warning(
                "Slack ask_user_question carried no answerable question: id=%s",
                msg.id,
            )
            return
        request_id, source, question, dropped = parsed
        if dropped:
            logger.warning(
                "Slack posts question 1 of %d and drops the others: "
                "request_id=%s source=%s dropped=%s",
                len(dropped) + 1,
                request_id,
                source or "-",
                " | ".join(entry or "(no prompt)" for entry in dropped),
            )

        try:
            inputs, options = _QUESTION_PRESENTER.answer_choices(
                question, allow_text_input=source == "ask_user_interrupt", request_id=request_id,
            )
        except slack_inputs.InputRenderError as exc:
            logger.warning(
                "Slack cannot render the inputs a question declared: "
                "request_id=%s source=%s error=%s",
                request_id,
                source,
                exc,
            )
            await self._post_unanswerable_notice(
                msg, routing_target, request_id, source, question
            )
            return

        if not inputs and not options:
            logger.warning(
                "Slack cannot render a free-form question, which needs a typed "
                "answer this channel cannot route: request_id=%s source=%s",
                request_id,
                source,
            )
            await self._post_unanswerable_notice(
                msg, routing_target, request_id, source, question
            )
            return

        channel_id, thread_ts = self._require_delivery(msg, routing_target)

        session_id = str(msg.session_id or "")
        blocks = _QUESTION_PRESENTER.answer_blocks(
            request_id=request_id, source=source, session_id=session_id,
            question=question, declared=inputs, options=options, dropped=dropped,
        )
        if blocks is None:
            raise SlackDeliveryError(
                f"question {request_id} could not be rendered as Block Kit",
                channel_id=channel_id,
            )

        notification_text = _QUESTION_PRESENTER.fallback_text(question)
        sent, message_ts, error = await self._post_text(
            channel_id=channel_id,
            text=notification_text,
            thread_ts=thread_ts,
            blocks=blocks,
            block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
            block_text_route=slack_questions.text_route(source, _INTERRUPT_RESUME_SOURCES),
            chunk_index=1,
            chunk_total=1,
        )
        if not sent:
            raise SlackDeliveryError(error, channel_id=channel_id)

        initiator = self.turn_initiator(session_id)
        self._remember_question(
            _SlackPendingQuestion(
                request_id=request_id,
                session_id=session_id,
                source=source,
                question=str(question.get("question") or "").strip(),
                labels=[option.label for option in options],
                values=[option.value for option in options],
                inputs=inputs,
                channel_id=channel_id,
                message_ts=message_ts,
                notification_text=notification_text,
                turn_request_id=str(msg.id or "").strip(),
                initiator=initiator,
            )
        )
        detail_name = "inputs" if inputs else "options"
        detail = (
            ",".join(f"{entry.name}:{entry.spec.name}" for entry in inputs)
            if inputs else len(options)
        )
        logger.info(
            "[SlackChannel] delivered question: channel=%s ts=%s request_id=%s "
            "source=%s %s=%s",
            channel_id,
            message_ts or "-",
            request_id,
            source or "-",
            detail_name,
            detail,
        )

    async def _post_unanswerable_notice(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
        request_id: str,
        source: str,
        question: Mapping[str, Any],
    ) -> None:
        """Report an unanswerable question without raising a second error."""
        if self._client is None:
            return
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id:
            logger.warning(
                "Slack could not report an unanswerable question, no target "
                "channel resolved: request_id=%s source=%s",
                request_id,
                source,
            )
            return

        text = (
            f"{_QUESTION_PRESENTER.fallback_text(question)}\n\n"
            "_This question needs a typed answer, which this channel cannot "
            "route back to the waiting task. The task stays paused until it is "
            "answered elsewhere or times out._"
        )
        try:
            sent, _, error = await self._post_text(
                channel_id=channel_id,
                text=text,
                thread_ts=thread_ts,
                chunk_index=1,
                chunk_total=1,
            )
        except Exception as exc:  # noqa: BLE001
            sent, error = False, str(exc)
        if not sent:
            logger.warning(
                "Slack failed to report an unanswerable question: "
                "request_id=%s source=%s error=%s",
                request_id,
                source,
                error,
            )

    async def _withdraw_questions_for_session(self, session_id: str) -> None:
        """Retire questions superseded by a new message."""
        if not session_id:
            return
        withdrawn = [
            entry
            for entry in self._pending_questions.values()
            if entry.session_id == session_id
            and entry.withdrawn_at is None
            and entry.source in _INTERRUPT_RESUME_SOURCES
        ]
        for entry in withdrawn:
            entry.withdrawn_at = time.monotonic()
            logger.info(
                "[SlackChannel] question withdrawn by an ordinary message in the "
                "same session: request_id=%s source=%s",
                entry.request_id,
                entry.source or "-",
            )
            await self._strip_question_buttons(entry)

    async def _strip_question_buttons(self, pending: _SlackPendingQuestion) -> None:
        """Remove a withdrawn question's buttons but retain its text."""
        if self._client is None or not pending.channel_id or not pending.message_ts:
            return
        text = _clamp(
            f"{pending.notification_text}\n\n{_WITHDRAWN_QUESTION_NOTICE}".strip(),
            _MAX_SLACK_TEXT_LENGTH,
        )
        try:
            sent, _, error = await self._post_text(
                channel_id=pending.channel_id,
                text=text,
                thread_ts="",
                update_ts=pending.message_ts,
                blocks=[],
            )
        except Exception as exc:  # noqa: BLE001
            sent, error = False, str(exc)
        if not sent:
            logger.warning(
                "Slack could not withdraw the buttons of a question the session "
                "moved past: request_id=%s error=%s",
                pending.request_id,
                error,
            )
