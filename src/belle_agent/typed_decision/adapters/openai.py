"""Native OpenAI Decisions API adapter."""

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from belle_agent.typed_decision.adapters.transport import post_json
from belle_agent.typed_decision.schema import (
    CHOICE,
    NOUL,
    SCORE,
    Answer,
    Question,
    TypedDecisionError,
    parse_answer,
)

logger = logging.getLogger(__name__)


def decisions_url(api_base: str) -> str:
    parts = urlsplit(str(api_base or "").strip())
    if not parts.scheme or not parts.netloc or not parts.path.rstrip("/").endswith("/v1"):
        raise TypedDecisionError("OpenAI decision api_base must be an absolute URL ending in /v1")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/decisions", "", ""))


def _description(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _probabilities(entries: Any) -> dict[str, float] | None:
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise TypedDecisionError("OpenAI decision probabilities were not a list")
    try:
        return {str(item["value"]): float(item["probability"]) for item in entries}
    except (KeyError, TypeError, ValueError) as exc:
        raise TypedDecisionError("OpenAI decision probabilities were invalid") from exc


def _level(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping) and "label" in value:
        level = {"label": str(value["label"])}
        if "description" in value:
            level["description"] = str(value["description"])
        return level
    return {"label": _description(value)}


class OpenAIDecisionsAdapter:
    """Answer Belle questions through OpenAI's native Decisions API."""

    name = "openai"

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
        if extra_body:
            raise TypedDecisionError("decision.model_config_obj.extra_body requires an OpenRouter decision API")
        self.model_name = model_name
        self.url = decisions_url(api_base)
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    def request_body(self, state: Mapping[str, Any] | str, questions: Mapping[str, Question]) -> dict[str, Any]:
        if not questions:
            raise TypedDecisionError("a decision request needs at least one question")
        try:
            input_text = state if isinstance(state, str) else json.dumps(dict(state), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TypedDecisionError("decision state is not JSON serializable") from exc
        entries = []
        for name, question in questions.items():
            entry: dict[str, Any] = {"name": name, "instructions": question.instructions}
            if question.type == NOUL:
                entry["type"] = "predicate"
            elif question.type == CHOICE:
                if len(question.criteria) < 2:
                    raise TypedDecisionError("OpenAI choice questions need at least two options")
                entry.update(
                    type="choice",
                    choices=[
                        {"value": key, "description": _description(value)} for key, value in question.criteria.items()
                    ],
                )
            elif question.type == SCORE:
                entry.update(type="score", levels=[_level(level) for level in question.criteria])
            entries.append(entry)
        return {"model": self.model_name, "input": input_text, "questions": entries}

    async def ask(self, state: Mapping[str, Any] | str, questions: Mapping[str, Question]) -> dict[str, Answer]:
        body = self.request_body(state, questions)
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        payload = await post_json(
            self.url, body, headers, logger=logger, timeout=self._timeout, transport=self._transport
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("answers"), list):
            raise TypedDecisionError("OpenAI decisions response had no answers list")
        answers: dict[str, Answer] = {}
        for entry in payload["answers"]:
            if not isinstance(entry, Mapping):
                raise TypedDecisionError("OpenAI decision answer was not a record")
            name = entry.get("name")
            if not isinstance(name, str) or name not in questions or name in answers:
                raise TypedDecisionError("OpenAI decision answer had an unknown or duplicate name")
            question = questions[name]
            if entry.get("type") == "refusal":
                raise TypedDecisionError(f"OpenAI decision refused {name!r}")
            expected = {NOUL: "predicate", CHOICE: "choice", SCORE: "score"}[question.type]
            if entry.get("type") != expected:
                raise TypedDecisionError(f"OpenAI decision gave the wrong type for {name!r}")
            normalized: dict[str, Any] = {"type": question.type}
            if question.type == NOUL:
                normalized[NOUL] = entry.get("probability")
            elif question.type == CHOICE:
                choice = entry.get("choice")
                if not isinstance(choice, str) or choice not in question.criteria:
                    raise TypedDecisionError(f"OpenAI decision gave an unknown choice for {name!r}")
                normalized.update(
                    choice=choice,
                    confidence=entry.get("confidence"),
                    probabilities=_probabilities(entry.get("probabilities")),
                )
            else:
                normalized.update(
                    score=entry.get("score"),
                    confidence=entry.get("confidence"),
                    probabilities=_probabilities(entry.get("probabilities")),
                    legend={str(i): level for i, level in enumerate(question.criteria)},
                )
            answers[name] = parse_answer(normalized)
        missing = set(questions) - set(answers)
        if missing:
            raise TypedDecisionError(f"OpenAI decisions response missing answers for: {sorted(missing)}")
        return answers
