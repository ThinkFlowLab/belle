"""Exercise the client and Jev adapter with mock responses."""

import asyncio
import json
import logging

import httpx
import pytest

from belle_agent.typed_decision import (
    JevAdapter,
    Question,
    TypedDecisionClient,
    TypedDecisionError,
)
from belle_agent.typed_decision.adapters.jev import DECISIONS_PATH, decisions_url

DECISION_RESPONSE = {
    "answers": {
        "audience": {"type": "choice", "choice": "the room"},
        "claim": {"type": "noul", "noul": 0.74},
        "value": {"type": "score", "score": 1.88, "confidence": 0.82},
    },
}

QUESTIONS = {
    "audience": Question(
        type="choice",
        instructions="who is the request in this message directed at",
        criteria={
            "nobody": "it asks nothing of anybody",
            "the room": "it puts something to everyone present",
            "a person": "it is aimed at one named person",
        },
    ),
    "claim": Question(
        type="noul",
        instructions="the last line asserts something as fact",
    ),
    "value": Question(
        type="score",
        instructions="judge the last line on its own",
        criteria=["nobody benefits", "mildly useful", "important"],
    ),
}


def adapter(handler=None, **kwargs) -> JevAdapter:
    return JevAdapter(
        model_name=kwargs.pop("model_name", "typesafe/jev-1.13"),
        api_base=kwargs.pop("api_base", "https://openrouter.example/api/v1"),
        api_key=kwargs.pop("api_key", "sk-test"),
        timeout=kwargs.pop("timeout", 2.0),
        transport=httpx.MockTransport(handler or (lambda request: httpx.Response(200, json=DECISION_RESPONSE))),
        **kwargs,
    )


def test_the_endpoint_is_the_decisions_path_not_chat_completions() -> None:
    """The host is kept and the path replaced: the two are siblings."""
    assert decisions_url("https://openrouter.ai/api/v1") == (f"https://openrouter.ai{DECISIONS_PATH}")
    assert decisions_url("https://openrouter.ai") == (f"https://openrouter.ai{DECISIONS_PATH}")
    with pytest.raises(TypedDecisionError):
        decisions_url("")
    with pytest.raises(TypedDecisionError):
        decisions_url("openrouter.ai/api/v1")


def test_questions_are_keyed_by_id_in_the_request_body() -> None:
    built = adapter().request_body("the state", QUESTIONS)
    assert set(built["questions"]) == {"audience", "claim", "value"}
    assert built["questions"]["claim"] == {
        "type": "noul",
        "instructions": "the last line asserts something as fact",
    }
    assert built["model"] == "typesafe/jev-1.13"


def test_extra_body_reaches_the_top_level_of_the_request() -> None:
    """Keep provider options outside the request fields."""
    body = adapter(extra_body={"provider": {"order": ["TypeSafe"]}}).request_body({"channel": "C1"}, QUESTIONS)
    assert body["provider"] == {"order": ["TypeSafe"]}
    assert "require_parameters" not in json.dumps(body)


@pytest.mark.parametrize("field", ["model", "state", "questions"])
def test_extra_body_cannot_replace_decision_request_fields(field: str) -> None:
    with pytest.raises(TypedDecisionError, match=rf"request fields: {field}$"):
        adapter(
            extra_body={"provider": {"order": ["TypeSafe"]}, field: "replacement"},
        )


async def test_response_yields_one_typed_answer_per_question() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DECISION_RESPONSE)

    client = TypedDecisionClient(adapter(handler))
    answers = await client.decide({"channel": "C1"}, QUESTIONS)

    assert set(answers) == {"audience", "claim", "value"}
    assert answers["audience"].value == "the room"
    assert answers["claim"].number == pytest.approx(0.74)
    assert answers["value"].number == pytest.approx(1.88)
    assert answers["value"].confidence == pytest.approx(0.82)
    assert seen[0].url.path == DECISIONS_PATH
    assert seen[0].headers["Authorization"] == "Bearer sk-test"


async def test_the_wrong_endpoint_refusal_becomes_one_error_class(caplog) -> None:
    client = TypedDecisionClient(
        adapter(lambda request: httpx.Response(404, json={"error": "private response"})),
        attempts=1,
    )
    with caplog.at_level(logging.DEBUG, logger="belle_agent.typed_decision.adapters.jev"):
        with pytest.raises(TypedDecisionError):
            await client.decide("state", QUESTIONS)
    assert "status=404" in caplog.text
    assert "private response" not in caplog.text


async def test_a_partial_response_is_refused_rather_than_returned() -> None:
    partial = {"model": "x", "answers": {"claim": {"type": "noul", "noul": 0.2}}}
    client = TypedDecisionClient(adapter(lambda request: httpx.Response(200, json=partial)), attempts=1)
    with pytest.raises(TypedDecisionError, match="missing answers for"):
        await client.decide("state", QUESTIONS)


async def test_an_answer_with_the_wrong_type_is_refused() -> None:
    response = {
        "answers": {
            **DECISION_RESPONSE["answers"],
            "value": {"type": "choice", "choice": "high"},
        }
    }
    client = TypedDecisionClient(adapter(lambda request: httpx.Response(200, json=response)), attempts=1)
    with pytest.raises(TypedDecisionError, match="expected 'score'"):
        await client.decide("state", QUESTIONS)


async def test_a_transient_failure_is_retried_and_the_second_answer_stands() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=DECISION_RESPONSE)

    client = TypedDecisionClient(adapter(handler), attempts=2, retry_delay=0.0)
    answers = await client.decide("state", QUESTIONS)
    assert len(calls) == 2
    assert answers["claim"].number == pytest.approx(0.74)


async def test_the_deadline_covers_the_whole_call_not_one_attempt() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=DECISION_RESPONSE)

    client = TypedDecisionClient(adapter(handler, timeout=5.0), timeout=0.2, attempts=3, retry_delay=0.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TypedDecisionError):
        await client.decide("state", QUESTIONS)
    assert loop.time() - started < 2.0


async def test_an_unreadable_body_is_an_error_and_not_an_empty_answer() -> None:
    client = TypedDecisionClient(
        adapter(lambda request: httpx.Response(200, text="not json at all")),
        attempts=1,
    )
    with pytest.raises(TypedDecisionError):
        await client.decide("state", QUESTIONS)


async def test_asking_nothing_is_refused() -> None:
    client = TypedDecisionClient(adapter(), attempts=1)
    with pytest.raises(TypedDecisionError):
        await client.decide("state", {})
