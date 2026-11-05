"""Put each message a host admits to the decision gate, and tell a turn what the gate answered.

The gate asks a typed-decision model, at an endpoint of its own, the
questions of ``message_decisions``. It runs in this process, beside the
turn, and is no turn of the backend.

Each admitted message is put to it once. With ``decision_forward`` off the
answers are logged and nothing waits for them. With it on, a message whose
turn starts at once waits for the answers, for as long as the gate's budget,
and its turn is told them as advice. A message that waits for another turn
is logged only: advice kept until its turn started would describe a
conversation that has moved on.

The answers decide nothing. Whether a message gets a turn is admission, and
whether the turn speaks is the model's. A gate that is absent, slow, failing
or wrong leaves the turn exactly as it would have been.
"""

import functools
import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.config import platform_section
from belle_agent.message_decisions import (
    QuestionConfigurationError,
    ShadowDecisionGate,
    load_questions,
    question_changes,
)
from belle_agent.platforms.base import InboundMessage, Platform, affordances
from belle_agent.platforms.decision_facts import inbound_decision_facts
from belle_agent.typed_decision import Question, TypedDecisionClient, TypedDecisionError, build_client

logger = logging.getLogger(__name__)

#: The settings of a platform's own section, as the Slack channel names them.
KEY_QUESTIONS_FILE = "decision_questions_file"
KEY_FORWARD = "decision_forward"


def _facts(inbound: InboundMessage) -> dict[str, Any]:
    """What the gate is told of a message, where its platform has nothing of its own to add."""
    return inbound_decision_facts(inbound, author=inbound.sender, bot_name="the assistant", kinds={})


class MessageDecisions:
    """The gate, at the three moments of a message's life where the host asks it."""

    def __init__(self, client: TypedDecisionClient, questions: Mapping[str, Question], platform: Platform, *, forward: bool) -> None:
        self._platform = platform
        self._client = client
        self._questions = questions
        self._facts = getattr(platform, "decision_facts", _facts)
        self._recent = getattr(platform, "recent_messages", None) if affordances(platform).history != "absent" else None
        self._forward = forward
        # One gate per workspace, as each reads the messages before a message in its own workspace only.
        self._gates: dict[str, ShadowDecisionGate] = {}

    def _gate(self, workspace: str) -> ShadowDecisionGate:
        if workspace not in self._gates:
            recent = (
                functools.partial(self._recent, workspace=workspace)
                if self._recent is not None and affordances(self._platform, workspace=workspace).history != "absent"
                else None
            )
            self._gates[workspace] = ShadowDecisionGate(self._client, recent_messages=recent, questions=self._questions)
        return self._gates[workspace]

    def _observe(self, inbound: InboundMessage) -> None:
        try:
            self._gate(inbound.workspace).observe(**self._facts(inbound))
        except Exception:  # An observation may not reach the turn it watches.
            logger.debug("decision observation skipped", exc_info=True)

    def arrived(self, inbound: InboundMessage) -> None:
        """A message was admitted. Unless its answers are forwarded, log them and wait for nothing."""
        if not self._forward:
            self._observe(inbound)

    def queued(self, inbound: InboundMessage) -> None:
        """A message waits for another turn. Its answers are logged now and are not kept for its own."""
        if self._forward:
            self._observe(inbound)

    async def advice(self, inbound: InboundMessage) -> str:
        """Return what the turn of a message that starts now is told, or nothing."""
        if not self._forward:
            return ""
        try:
            return await self._gate(inbound.workspace).advise(**self._facts(inbound))
        except Exception:  # Advice that cannot be had leaves the turn unchanged.
            logger.debug("decision advice unavailable", exc_info=True)
            return ""

    async def close(self) -> None:
        for gate in self._gates.values():
            await gate.close()


def decisions_from_config(config: Mapping[str, Any], platform: Platform) -> MessageDecisions | None:
    """Build the gate a configuration describes, or nothing where it describes none.

    ``models.decision`` names the endpoint, and no endpoint is the default:
    then nothing is built and nothing is ever called. An entry that cannot be
    used disables the gate and leaves the conversations as they are.
    """
    try:
        client = build_client(config)
    except (ValueError, TypedDecisionError) as exc:
        logger.error("decision disabled: %s", exc)
        return None
    if client is None:
        return None
    section = platform_section(config, platform.name)
    section = section if isinstance(section, Mapping) else {}
    try:
        questions = load_questions(section.get(KEY_QUESTIONS_FILE) or None)
    except QuestionConfigurationError as exc:
        logger.error("decision disabled: invalid %s (%s)", KEY_QUESTIONS_FILE, exc)
        return None
    for action, question_id in question_changes(questions):
        logger.info("decision question %s: %s", action, question_id)
    if not questions:
        logger.info("decision disabled: all questions disabled")
        return None
    forward = section.get(KEY_FORWARD, True)
    if not isinstance(forward, bool):
        logger.warning("Belle platforms.%s.%s must be a boolean; using true", platform.name, KEY_FORWARD)
        forward = True
    return MessageDecisions(client, questions, platform, forward=forward)
