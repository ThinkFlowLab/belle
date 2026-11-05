"""Provider task requests preserve wire inputs and complete responses."""

import json
from typing import Any

import httpx
import pytest

from belle_agent.tools.providers import request_provider


async def call(api: str, operation: str, payload: dict[str, Any], handler: Any, **kwargs: Any) -> dict[str, Any]:
    return await request_provider(
        api=api, operation=operation, payload=payload, model=kwargs.pop("model", None),
        base_url="https://provider.test/v1", api_key="test-key",
        transport=httpx.MockTransport(handler), **kwargs,
    )


@pytest.mark.parametrize("operation", ["summarize", "extract"])
async def test_direct_chat_preserves_native_fields_and_response(operation: str) -> None:
    payload: dict[str, Any] = {"text": "Acme", "document": "cGRm", "document_mime_type": "application/pdf"}
    if operation == "extract":
        payload.update(entities={"company": {"description": "Company", "top_n": 2}}, threshold=0.5)
    else:
        payload.update(instructions="One sentence", max_tokens=100)
    expected = {"choices": [{"message": {"content": '{"finish_reason":"length"}'}}], "usage": {"total_tokens": 4}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["x-api-key"] == "test-key"
        body = json.loads(request.content)
        assert body["model"] == operation
        assert body["messages"][-1] == {"role": "user", "content": "Acme"}
        assert all(body[key] == value for key, value in payload.items())
        return httpx.Response(200, json=expected)

    assert await call("scaledown", operation, payload, handler) == expected


async def test_openrouter_extract_maps_options_and_pdf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["provider"]["options"]["scaledown"] == {"entities": {"company": "Company name"}, "threshold": 0.8}
        assert body["model"] == "scaledown/extract"
        assert body["messages"][0] == {"role": "system", "content": "Keep names"}
        assert "instruction" not in body["provider"]["options"]["scaledown"]
        assert body["messages"][-1]["content"][1]["file"]["file_data"] == "data:application/pdf;base64,cGRm"
        assert "document" not in body
        return httpx.Response(200, json={"choices": []})

    await call("openrouter", "extract", {
        "entities": {"company": "Company name"}, "threshold": 0.8, "instruction": "Keep names",
        "document": "cGRm", "document_mime_type": "application/pdf",
    }, handler)


async def test_classify_preserves_batch_and_native_options() -> None:
    payload = {"chunks": ["one", "two"], "labels": [{"name": "topic", "rubric": "About topic?"}],
               "multi_label": True, "reasoning": True, "threshold": 0.4, "system_prompt": "Read carefully"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/classify"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json={"results": [{"scores": {"topic": 0.8}}]})

    assert "results" in await call("scaledown", "classify", payload, handler, model="classify-1")


@pytest.mark.parametrize("response", [httpx.Response(422, json={"detail": "invalid"}),
                                      httpx.Response(200, text="invalid JSON"),
                                      httpx.Response(200, json=[]),
                                      httpx.Response(200, json={"error": {"message": "failed"}})])
async def test_provider_errors_propagate(response: httpx.Response) -> None:
    with pytest.raises((httpx.HTTPStatusError, ValueError, TypeError)):
        await call("scaledown", "summarize", {"text": "input"}, lambda request: response)


async def test_a_reply_with_an_error_keeps_the_error_out_of_the_message() -> None:
    from belle_agent.tools.providers import ProviderError

    reply = httpx.Response(200, json={"error": {"message": "echo of input marker"}})
    with pytest.raises(ProviderError) as raised:
        await call("scaledown", "summarize", {"text": "input marker"}, lambda request: reply)
    assert raised.value.detail == {"message": "echo of input marker"}
    assert "marker" not in str(raised.value)


async def test_openrouter_classify_refused_before_network() -> None:
    with pytest.raises(ValueError, match="Decisions API"):
        await call("openrouter", "classify", {}, lambda request: pytest.fail("unexpected network"))


async def test_chat_completions_summarize_uses_chat_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "vendor/model"
        assert body["messages"] == [
            {"role": "system", "content": "Summarize"}, {"role": "user", "content": "Tide table"}
        ]
        assert "text" not in body and "instructions" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "Tides"}]}}]})

    from belle_agent.tools.providers import summary_text

    result = await call("chat_completions", "summarize", {"instructions": "Summarize", "text": "Tide table"},
                        handler, model="vendor/model")
    assert summary_text("chat_completions", result) == "Tides"


@pytest.mark.parametrize("operation", ["extract", "classify"])
async def test_chat_completions_refuses_other_operations(operation: str) -> None:
    with pytest.raises(ValueError, match="operation must be summarize"):
        await call("chat_completions", operation, {}, lambda request: pytest.fail("unexpected network"))
