"""Build the Slack decision gate from JiuwenSwarm's Belle configuration."""

import json
import logging

from belle_agent.integrations.jiuwenswarm.config import load_belle_config
from belle_agent.platforms.slack.decision_questions import (
    QuestionConfigurationError,
    ShadowDecisionGate,
    SlackDecisionObserver,
    question_changes,
)

logger = logging.getLogger(__name__)


class SlackDecisionMixin(SlackDecisionObserver):
    def _shadow_decision_gate(self) -> ShadowDecisionGate | None:
        """Cache the configured decision gate, or its absence."""
        if self._decision_gate is False:
            return None
        if isinstance(self._decision_gate, ShadowDecisionGate):
            return self._decision_gate
        try:
            from belle_agent.typed_decision import build_client

            client = build_client(load_belle_config())
        except ValueError as exc:
            logger.error("[SlackChannel] decision disabled: %s", exc)
            self._decision_gate = False
            return None
        except Exception:
            logger.debug("[SlackChannel] no decision gate", exc_info=True)
            self._decision_gate = False
            return None
        if client is None:
            self._decision_gate = False
            return None
        try:
            questions = self._load_decision_questions()
        except Exception as exc:  # noqa: BLE001 - optional questions must not break chat
            logger.error(
                "[SlackChannel] decision disabled: invalid decision_questions_file (%s)",
                str(exc)
                if isinstance(exc, QuestionConfigurationError)
                else type(exc).__name__,
            )
            self._decision_gate = False
            return None
        for action, question_id in question_changes(questions):
            logger.info(
                "[SlackChannel] decision question %s: %s",
                action,
                json.dumps(question_id, ensure_ascii=True),
            )
        if not questions:
            logger.info("[SlackChannel] decision disabled: all questions disabled")
            self._decision_gate = False
            return None
        gate = ShadowDecisionGate(
            client, recent_messages=self._decision_window, questions=questions
        )
        self._decision_gate = gate
        return gate
