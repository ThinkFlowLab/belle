"""Native ScaleDown Decisions transport and response contract."""

import json

import httpx
import pytest

from belle_agent.typed_decision import Question, TypedDecisionClient, TypedDecisionError, build_client
from belle_agent.typed_decision.adapters.scaledown import ScaleDownAdapter

QUESTIONS = {
    "claim": Question(type="noul", instructions="Is this a factual claim?"),
    "audience": Question(type="choice", instructions="Who is addressed?", criteria={"bot": "The bot", "room": "The room"}),
    "urgency": Question(type="score", instructions="How urgent?", criteria=["None", "Some", "High"]),
}

RESPONSE = {
    "model": "classify-1",
    "answers": {
        "claim": {"type": "noul", "noul": 0.8},
        "audience": {"type": "choice", "choice": "bot", "confidence": 0.9},
        "urgency": {"type": "score", "score": 1.5, "confidence": 0.7},
    },
}


def adapter(handler=None, **kwargs) -> ScaleDownAdapter:
    return ScaleDownAdapter(
        model_name=kwargs.pop("model_name", "classify-1"),
        api_base="https://api.scaledown.xyz",
        api_key="sk-test",
        timeout=2,
        transport=httpx.MockTransport(handler or (lambda request: httpx.Response(200, json=RESPONSE))),
        **kwargs,
    )


async def test_native_request_and_all_typed_answers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=RESPONSE)

    state = {"history": [{"role": "assistant", "text": "Hello"}], "message": "Can you help?", "chat_type": "dm"}
    answers = await TypedDecisionClient(adapter(handler)).decide(state, QUESTIONS)

    request = requests[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/scaledown"
    assert request.headers["x-api-key"] == "sk-test"
    assert "authorization" not in request.headers
    assert json.loads(body["state"]["text"]) == state
    assert body["model"] == "classify-1"
    assert body["questions"] == {key: question.as_request_value() for key, question in QUESTIONS.items()}
    assert answers["claim"].number == pytest.approx(0.8)
    assert answers["audience"].value == "bot"
    assert answers["urgency"].number == pytest.approx(1.5)


def test_native_request_refuses_unsupported_model_options_and_score_levels() -> None:
    with pytest.raises(TypedDecisionError, match="model_name must be classify or classify-1"):
        adapter(model_name="scaledown/classify-1")
    with pytest.raises(TypedDecisionError, match="do not accept model_config_obj.extra_body"):
        adapter(extra_body={"provider": "x"})
    with pytest.raises(TypedDecisionError, match="at most 10 levels"):
        adapter().request_body("state", {"many": Question(type="score", instructions="Rate.", criteria=list(range(11)))})


def test_explicit_api_selects_native_adapter() -> None:
    client = build_client(
        {"models": {"decision": {"entries": [
            {"id": "classify", "api": "scaledown", "api_base": "https://api.scaledown.xyz",
             "api_key": "sk-test", "model_name": "classify-1"}
        ]}}}
    )
    assert isinstance(client.adapter, ScaleDownAdapter)
