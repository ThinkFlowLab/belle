"""Native ScaleDown Decisions API adapter."""

from collections.abc import Mapping
from typing import Any

import httpx

from belle_agent.typed_decision.adapters.openrouter_text import OpenRouterTextStateAdapter
from belle_agent.typed_decision.schema import SCORE, Question, TypedDecisionError


class ScaleDownAdapter(OpenRouterTextStateAdapter):
    name = "scaledown"
    endpoint_path = "/v1/scaledown"

    def __init__(
        self,
        *,
        model_name: str,
        api_base: str,
        api_key: str,
        timeout: float,
        extra_body: Mapping[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if model_name not in ("classify", "classify-1"):
            raise TypedDecisionError("ScaleDown decision model_name must be classify or classify-1")
        if extra_body:
            raise TypedDecisionError("ScaleDown decisions do not accept model_config_obj.extra_body")
        super().__init__(
            model_name=model_name,
            api_base=api_base,
            api_key=api_key,
            timeout=timeout,
            transport=transport,
        )

    def request_body(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Any]:
        for name, question in questions.items():
            if question.type == SCORE and len(question.criteria) > 10:
                raise TypedDecisionError(f"ScaleDown score question {name!r} accepts at most 10 levels")
        body = super().request_body(state, questions)
        body["state"] = {"text": body["state"]}
        return body

    def request_headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-api-key": self._api_key}
