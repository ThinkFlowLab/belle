"""Jev decisions endpoint adapter."""

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from belle_agent.typed_decision.adapters.transport import post_json
from belle_agent.typed_decision.schema import (
    Answer,
    Question,
    TypedDecisionError,
    parse_answers,
)

logger = logging.getLogger(__name__)

DECISIONS_PATH = "/api/alpha/decisions"

REQUEST_FIELDS = frozenset({"model", "state", "questions"})


def decisions_url(api_base: str, path: str = DECISIONS_PATH) -> str:
    """Keep the configured host and use the decisions endpoint."""
    base = str(api_base or "").strip()
    if not base:
        raise TypedDecisionError("a decision model needs an api_base")
    parts = urlsplit(base)
    if not parts.scheme or not parts.netloc:
        raise TypedDecisionError(f"api_base is not an absolute URL: {api_base!r}")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class JevAdapter:
    """Answer typed-decision questions with a TypeSafe decisions model."""

    name = "openrouter"
    endpoint_path = DECISIONS_PATH

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
        self.model_name = model_name
        self.url = decisions_url(api_base, self.endpoint_path)
        self._api_key = api_key
        self._timeout = timeout
        self._extra_body = dict(extra_body or {})
        reserved = REQUEST_FIELDS.intersection(self._extra_body)
        if reserved:
            raise TypedDecisionError(
                "decision extra_body cannot override request fields: " + ", ".join(sorted(reserved))
            )
        self._transport = transport

    @classmethod
    def claims(cls, model_name: str) -> bool:
        """Whether this adapter answers for the named model."""
        return str(model_name or "").strip().lower().startswith("typesafe/")

    def request_body(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Any]:
        """Build a request with questions keyed by id."""
        if not questions:
            raise TypedDecisionError("a decision request needs at least one question")
        body: dict[str, Any] = {
            "model": self.model_name,
            "state": state if isinstance(state, str) else dict(state),
            "questions": {str(question_id): question.as_request_value() for question_id, question in questions.items()},
        }
        body.update(self._extra_body)
        return body

    async def ask(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Answer]:
        body = self.request_body(state, questions)
        headers = self.request_headers()
        payload = await post_json(
            self.url, body, headers, logger=logger, timeout=self._timeout, transport=self._transport
        )
        if not isinstance(payload, Mapping):
            raise TypedDecisionError("decisions response was not a record")
        answers = parse_answers(payload)
        missing = set(questions) - set(answers)
        if missing:
            raise TypedDecisionError(f"decisions response missing answers for: {sorted(missing)}")
        for question_id, question in questions.items():
            if answers[question_id].type != question.type:
                raise TypedDecisionError(
                    f"decisions response gave {answers[question_id].type!r} for"
                    f" {question_id!r}, expected {question.type!r}"
                )
        return answers

    def request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers
