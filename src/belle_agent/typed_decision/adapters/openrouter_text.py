"""OpenRouter Decisions models that require text state."""

import json
from collections.abc import Mapping
from typing import Any

from belle_agent.typed_decision.adapters.jev import JevAdapter
from belle_agent.typed_decision.schema import Question, TypedDecisionError


class OpenRouterTextStateAdapter(JevAdapter):
    name = "openrouter_text_state"

    def request_body(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Any]:
        if not isinstance(state, str):
            try:
                state = json.dumps(dict(state), ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise TypedDecisionError("decision state is not JSON serializable") from exc
        return super().request_body(state, questions)
