"""Exercise native OpenAI Decisions request and response translation."""

import json

import httpx
import pytest

from belle_agent.typed_decision import Question, TypedDecisionClient, TypedDecisionError
from belle_agent.typed_decision.adapters.openai import OpenAIDecisionsAdapter, decisions_url

QUESTIONS = {
    "urgent": Question(type="noul", instructions="Is this urgent?"),
    "team": Question(
        type="choice",
        instructions="Which team?",
        criteria={"billing": "Payments", "technical": "Bugs"},
    ),
    "severity": Question(type="score", instructions="How severe?", criteria=["Low", "Medium", "High"]),
}

RESPONSE = {
    "answers": [
        {"name": "urgent", "type": "predicate", "probability": 0.8},
        {
            "name": "team",
            "type": "choice",
            "choice": "technical",
            "confidence": 0.9,
            "probabilities": [
                {"value": "billing", "probability": 0.1},
                {"value": "technical", "probability": 0.9},
            ],
        },
        {
            "name": "severity",
            "type": "score",
            "score": 1.6,
            "confidence": 0.7,
            "probabilities": [
                {"value": 0, "label": "Low", "probability": 0.1},
                {"value": 1, "label": "Medium", "probability": 0.2},
                {"value": 2, "label": "High", "probability": 0.7},
            ],
        },
    ]
}


def adapter(handler=None) -> OpenAIDecisionsAdapter:
    return OpenAIDecisionsAdapter(
        model_name="gpt-6-luna",
        api_base="https://api.openai.com/v1",
        api_key="sk-test",
        timeout=2.0,
        transport=httpx.MockTransport(handler or (lambda request: httpx.Response(200, json=RESPONSE))),
    )


async def test_native_request_and_answers_keep_belle_question_meanings() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=RESPONSE)

    answers = await TypedDecisionClient(adapter(handler)).decide({"message": "Checkout failed"}, QUESTIONS)
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/decisions"
    assert requests[0].headers["Authorization"] == "Bearer sk-test"
    assert json.loads(body["input"]) == {"message": "Checkout failed"}
    assert body["questions"] == [
        {"name": "urgent", "type": "predicate", "instructions": "Is this urgent?"},
        {
            "name": "team",
            "type": "choice",
            "instructions": "Which team?",
            "choices": [
                {"value": "billing", "description": "Payments"},
                {"value": "technical", "description": "Bugs"},
            ],
        },
        {
            "name": "severity",
            "type": "score",
            "instructions": "How severe?",
            "levels": [{"label": "Low"}, {"label": "Medium"}, {"label": "High"}],
        },
    ]
    assert answers["urgent"].number == pytest.approx(0.8)
    assert answers["team"].value == "technical"
    assert answers["team"].probabilities == {"billing": 0.1, "technical": 0.9}
    assert answers["severity"].number == pytest.approx(1.6)
    assert answers["severity"].legend == {"0": "Low", "1": "Medium", "2": "High"}


@pytest.mark.parametrize(
    "response,error",
    [
        ({"answers": [{"name": "urgent", "type": "refusal"}]}, "refused"),
        ({"answers": RESPONSE["answers"][:-1]}, "missing answers"),
        ({"answers": [RESPONSE["answers"][0]] * 2}, "duplicate name"),
        ({"answers": [{"name": "urgent", "type": "choice", "choice": "yes"}]}, "wrong type"),
    ],
)
async def test_refused_or_incomplete_decisions_do_not_return_partial_advice(response, error) -> None:
    client = TypedDecisionClient(adapter(lambda request: httpx.Response(200, json=response)), attempts=1)
    with pytest.raises(TypedDecisionError, match=error):
        await client.decide("state", QUESTIONS)


def test_native_endpoint_and_choice_require_valid_configuration() -> None:
    assert decisions_url("https://api.openai.com/v1/") == "https://api.openai.com/v1/decisions"
    with pytest.raises(TypedDecisionError, match="ending in /v1"):
        decisions_url("https://openrouter.ai/api/v1/chat")
    with pytest.raises(TypedDecisionError, match="at least two options"):
        adapter().request_body("state", {"only": Question(type="choice", instructions="Pick.", criteria={"a": "A"})})


def test_native_score_preserves_structured_level_descriptions() -> None:
    body = adapter().request_body(
        "state",
        {
            "severity": Question(
                type="score",
                instructions="Rate it.",
                criteria=[
                    {"label": "Low", "description": "No impact."},
                    {"label": "High", "description": "Service stopped."},
                ],
            )
        },
    )
    assert body["questions"][0]["levels"] == [
        {"label": "Low", "description": "No impact."},
        {"label": "High", "description": "Service stopped."},
    ]
