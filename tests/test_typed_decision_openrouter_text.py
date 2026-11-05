"""OpenRouter text-state decisions keep Belle's full state and typed answers."""

import json

import httpx
import pytest

from belle_agent.config import load_config
from belle_agent.typed_decision import Question, TypedDecisionClient, TypedDecisionError, build_client
from belle_agent.typed_decision.adapters.openrouter_text import OpenRouterTextStateAdapter

QUESTIONS = {
    "asked": Question(type="noul", instructions="Did someone ask a question?"),
    "audience": Question(type="choice", instructions="Who is addressed?", criteria={"bot": "The bot", "room": "The room"}),
    "value": Question(type="score", instructions="How useful?", criteria=["None", "Some", "High"]),
}

RESPONSE = {
    "answers": {
        "asked": {"type": "noul", "noul": 0.8},
        "audience": {"type": "choice", "choice": "bot", "confidence": 0.9},
        "value": {"type": "score", "score": 1.5, "confidence": 0.7},
    }
}


async def test_text_state_keeps_all_context_and_parses_all_question_types() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RESPONSE)

    adapter = OpenRouterTextStateAdapter(
        model_name="scaledown/classify-1",
        api_base="https://openrouter.ai/api/v1",
        api_key="sk-test",
        timeout=2,
        transport=httpx.MockTransport(handler),
    )
    state = {"history": [{"role": "assistant", "text": "Bonjour"}], "message": "Can you help?", "channel": "C1"}
    answers = await TypedDecisionClient(adapter).decide(state, QUESTIONS)

    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/api/alpha/decisions"
    assert seen[0].headers["Authorization"] == "Bearer sk-test"
    assert json.loads(body["state"]) == state
    assert body["model"] == "scaledown/classify-1"
    assert body["questions"] == {key: question.as_request_value() for key, question in QUESTIONS.items()}
    assert answers["asked"].number == pytest.approx(0.8)
    assert answers["audience"].value == "bot"
    assert answers["value"].number == pytest.approx(1.5)


def test_string_state_stays_plain_and_non_json_state_is_refused() -> None:
    adapter = OpenRouterTextStateAdapter(
        model_name="scaledown/classify-1",
        api_base="https://openrouter.ai/api/v1",
        api_key="sk-test",
        timeout=2,
    )
    assert adapter.request_body("plain text", QUESTIONS)["state"] == "plain text"
    with pytest.raises(TypedDecisionError, match="not JSON serializable"):
        adapter.request_body({"bad": object()}, QUESTIONS)


def test_explicit_api_selects_text_state_adapter() -> None:
    client = build_client(
        {"models": {"decision": {"entries": [
            {"id": "classify", "api": "openrouter_text_state", "api_base": "https://openrouter.ai/api/v1",
             "api_key": "sk-test", "model_name": "scaledown/classify-1"}
        ]}}}
    )
    assert isinstance(client.adapter, OpenRouterTextStateAdapter)


@pytest.mark.parametrize(
    ("api", "base", "model"),
    [
        ("openrouter_text_state", "https://openrouter.ai/api/v1", "scaledown/classify-1"),
        ("scaledown", "https://api.scaledown.xyz", "classify-1"),
    ],
)
def test_config_accepts_text_state_api(tmp_path, monkeypatch, api, base, model) -> None:
    monkeypatch.setenv("TEST_DECISION_KEY", "sk-test")
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms: {slack: {enforce_audience: true}}\n"
        f"models: {{decision: {{entries: [{{id: classify, api: {api}, "
        f"api_base: '{base}', api_key: '${{TEST_DECISION_KEY}}', "
        f"model_name: {model}}}]}}}}\n",
        encoding="utf-8",
    )
    client = build_client(load_config(path))
    assert client.adapter.name == api
