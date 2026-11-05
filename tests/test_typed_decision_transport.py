"""Decision providers share transport failures without exposing response bodies."""

import logging

import httpx
import pytest

from belle_agent.typed_decision import Question, TypedDecisionError
from belle_agent.typed_decision.adapters.jev import JevAdapter
from belle_agent.typed_decision.adapters.openai import OpenAIDecisionsAdapter


@pytest.mark.parametrize("adapter_type", [JevAdapter, OpenAIDecisionsAdapter])
@pytest.mark.parametrize("failure", ["status", "json", "network"])
async def test_provider_transport_errors(adapter_type, failure, caplog):
    def handle(request):
        if failure == "network":
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(403 if failure == "status" else 200, text="private response")

    adapter = adapter_type(
        model_name="test", api_base="https://example.test/v1", api_key="secret",
        timeout=2, transport=httpx.MockTransport(handle),
    )
    expected = {
        "status": "decisions endpoint returned 403",
        "json": "decisions response was not JSON",
        "network": "decisions request failed: connection failed",
    }[failure]
    with caplog.at_level(logging.DEBUG), pytest.raises(TypedDecisionError, match=expected) as error:
        await adapter.ask("state", {"urgent": Question(type="noul", instructions="Urgent?")})
    assert "private response" not in str(error.value) + caplog.text
    assert "secret" not in str(error.value) + caplog.text
    if failure == "status":
        assert "status=403" in caplog.text
