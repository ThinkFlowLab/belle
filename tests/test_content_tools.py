"""Content tools preserve inputs and use the turn's attachment permissions."""

import json

import httpx
import pytest

from belle_agent.backend import ToolCall
from belle_agent.tools.content import ContentToolkit
from belle_agent.tools.host import BoundTurn, ToolHost
from belle_agent.tools.server import PATH, ToolServer


@pytest.fixture
def world():
    calls = []

    async def request(**kwargs):
        calls.append(kwargs)
        return {"result": "provider output"}

    return ContentToolkit({name: {} for name in ("summarize", "extract", "classify")}, request=request), calls


async def test_arguments_reach_provider_unchanged(world):
    kit, calls = world
    assert json.loads(
        await kit.classify(
            labels=[{"name": "urgent", "rubric": "Needs immediate action"}],
            chunks=[{"id": "1", "text": "hello"}],
            multi_label=False,
            threshold=0,
            reasoning=True,
            system_prompt="rules",
        )
    ) == {"result": "provider output"}
    assert calls == [
        {
            "operation": "classify",
            "payload": {
                "labels": [{"name": "urgent", "rubric": "Needs immediate action"}],
                "chunks": [{"id": "1", "text": "hello"}],
                "multi_label": False,
                "threshold": 0,
                "reasoning": True,
                "system_prompt": "rules",
            },
        }
    ]
    await kit.extract({"person": "Full name"}, text="Alice", threshold=0.5)
    await kit.summarize(text="hello", instructions="brief", max_tokens=20)
    assert calls[-1]["payload"] == {"text": "hello", "instructions": "brief", "max_tokens": 20}


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"text": "x", "attachment_ref": "F1"},
        {"text": ""},
        {"text": "x", "max_tokens": True},
        {"text": "x", "max_tokens": 0},
    ],
)
async def test_invalid_source_or_options_never_reach_provider(world, arguments):
    kit, calls = world
    assert json.loads(await kit.summarize(**arguments))["error"] == "invalid_arguments"
    assert calls == []


@pytest.mark.parametrize("threshold", [-1, 2, True, float("nan")])
async def test_invalid_threshold_never_reaches_provider(world, threshold):
    kit, calls = world
    assert json.loads(await kit.extract({"x": "name"}, text="x", threshold=threshold))["error"] == "invalid_arguments"
    assert calls == []


async def test_attachment_resolution_uses_bound_metadata_and_tool_permissions(world):
    kit, calls = world
    seen = []

    async def resolve(ref, metadata):
        seen.append((ref, metadata))
        return {"document": "encoded", "document_mime_type": "application/pdf"}

    kit._resolve_attachment = resolve
    host = ToolHost([kit])
    host.bind("chat", BoundTurn("turn", metadata={"workspace": "W1"}, tools={"extract": "deny"}))
    await host.call(ToolCall(conversation="chat", name="summarize", arguments={"attachment_ref": "F1"}))
    assert seen == [("F1", {"workspace": "W1"})]
    assert calls[0]["payload"] == {"document": "encoded", "document_mime_type": "application/pdf"}
    denied = await host.call(
        ToolCall(conversation="chat", name="extract", arguments={"text": "x", "entities": {"x": "x"}})
    )
    assert json.loads(denied)["error"] == "tool_denied"
    assert len(calls) == 1


def test_only_configured_operations_are_advertised():
    kit = ContentToolkit({"summarize": {}}, request=None)
    assert [card["name"] for card in kit.get_tool_definitions()] == ["summarize"]
    assert "document" not in kit.get_tool_definitions()[0]["input_params"]["properties"]


async def test_mcp_catalog_advertises_content_arguments():
    kit = ContentToolkit({"summarize": {}}, request=None)

    async def conversation_of(metadata, headers):
        return ""

    server = ToolServer(ToolHost([kit]), conversation_of=conversation_of)
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            response = await client.post(
                PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Authorization": f"Bearer {server.token}", "Accept": "application/json, text/event-stream"},
            )
        schema = response.json()["result"]["tools"][0]["inputSchema"]
    finally:
        await server.close()
    assert "text" in schema["properties"]
    assert "attachment_ref" in schema["properties"]


@pytest.mark.parametrize("entities", [{}, {"": "name"}, {"person": ""}, ["person"]])
async def test_invalid_entity_descriptions_never_reach_provider(world, entities):
    kit, calls = world
    result = json.loads(await kit.extract(entities, text="Alice"))
    assert result["error"] == "invalid_arguments"
    assert "entities" in result["detail"]
    assert calls == []


async def test_missing_attachment_resolver_is_explicit(world):
    kit, calls = world
    result = json.loads(await kit.summarize(attachment_ref="F1"))
    assert result["error"] == "invalid_arguments"
    assert "attachment_ref" in result["detail"]
    assert calls == []


async def test_attachment_refusal_does_not_reach_provider(world):
    kit, calls = world

    async def resolve(ref, metadata):
        raise ValueError("attachment_ref is outside this conversation")

    kit._resolve_attachment = resolve
    result = json.loads(await kit.summarize(attachment_ref="other-file"))
    assert result["detail"] == "attachment_ref is outside this conversation"
    assert calls == []


async def test_role_selection_registers_only_content_provider_entries(monkeypatch):
    import sys
    from types import SimpleNamespace

    from belle_agent.tools.content import content_toolkit_from_config

    calls = []

    async def request(**kwargs):
        calls.append(kwargs)
        return {"summary": "done"}

    monkeypatch.setitem(sys.modules, "belle_agent.tools.providers", SimpleNamespace(request_provider=request))
    config = {
        "models": {
            "summarizer": {"entries": [{"id": "a", "api": "chat_completions"}]},
            "extractor": {
                "default": "selected",
                "entries": [
                    {"id": "other", "api": "chat_completions"},
                    {
                        "id": "selected",
                        "api": "scaledown",
                        "api_base": "https://example.test",
                        "api_key": "test-key",
                        "model_name": "model",
                    },
                ],
            },
        }
    }
    kit = content_toolkit_from_config(config, object())
    assert kit is not None
    assert [card["name"] for card in kit.get_tool_definitions()] == ["extract"]
    await kit.extract({"person": "name"}, text="Alice")
    assert calls[0] == {
        "api": "scaledown",
        "base_url": "https://example.test",
        "api_key": "test-key",
        "model": "model",
        "operation": "extract",
        "payload": {"text": "Alice", "entities": {"person": "name"}},
    }
    assert content_toolkit_from_config({}, object()) is None


@pytest.mark.parametrize(
    "labels",
    [
        ["urgent"],
        [],
        [{"name": "urgent"}],
        [{"name": "urgent", "rubric": ""}],
        [{"name": "urgent", "rubric": "now", "extra": "x"}],
    ],
)
async def test_classification_refuses_malformed_labels(world, labels):
    kit, calls = world
    result = json.loads(await kit.classify(labels=labels, text="hello"))
    assert result["error"] == "invalid_arguments"
    assert "labels" in result["detail"]
    assert calls == []


@pytest.mark.parametrize("chunks", [["hello"], [], [{"text": "hello"}], [{"id": "", "text": "hello"}]])
async def test_classification_refuses_malformed_chunks(world, chunks):
    kit, calls = world
    result = json.loads(await kit.classify(labels=[{"name": "urgent", "rubric": "now"}], chunks=chunks))
    assert result["error"] == "invalid_arguments"
    assert "chunks" in result["detail"]
    assert calls == []


@pytest.mark.parametrize(
    "entry",
    [
        {"api": "scaledown"},
        {"api": "scaledown", "api_base": "https://example.test", "api_key": ""},
        {"api": "chat_completions", "api_base": "https://example.test", "api_key": "key"},
    ],
)
def test_disabled_or_non_content_entries_do_not_register(entry):
    from belle_agent.tools.content import content_toolkit_from_config

    assert (
        content_toolkit_from_config({"models": {"summarizer": {"entries": [{"id": "only", **entry}]}}}, object())
        is None
    )


async def test_classification_combines_text_and_document_but_excludes_chunks(world):
    kit, calls = world

    async def resolve(ref, metadata):
        return {"document": "encoded", "document_mime_type": "application/pdf"}

    kit._resolve_attachment = resolve
    labels = [{"name": "urgent", "rubric": "now"}]
    await kit.classify(labels=labels, text="context", attachment_ref="F1")
    assert calls[0]["payload"] == {
        "text": "context",
        "labels": labels,
        "document": "encoded",
        "document_mime_type": "application/pdf",
    }
    result = json.loads(await kit.classify(labels=labels, text="context", chunks=[{"id": "1", "text": "hello"}]))
    assert result["error"] == "invalid_arguments"
    assert len(calls) == 1


async def test_provider_refusal_preserves_safe_bounded_detail():
    import httpx

    async def request(**kwargs):
        response = httpx.Response(
            422,
            request=httpx.Request("POST", "https://example.test"),
            json={"detail": "PDF must contain text; key=secret-value " + "x" * 1200, "private": "do not echo"},
        )
        response.raise_for_status()

    kit = ContentToolkit({"summarize": {"api_key": "secret-value"}}, request=request)
    result = json.loads(await kit.summarize(text="hello"))
    assert result["error"] == "provider_error"
    assert result["status"] == 422
    assert result["detail"].startswith("PDF must contain text; key=[redacted]")
    assert len(result["detail"]) == 1000
    assert "secret-value" not in result["detail"]
    assert "private" not in result


async def test_a_provider_reply_with_an_error_goes_to_the_model_with_the_key_redacted():
    from belle_agent.tools.providers import ProviderError

    async def request(**kwargs):
        raise ProviderError({"message": "cannot summarize 'hello marker'; key=secret-value"})

    kit = ContentToolkit({"summarize": {"api_key": "secret-value"}}, request=request)
    result = json.loads(await kit.summarize(text="hello marker"))
    assert result == {
        "ok": False,
        "error": "provider_error",
        "status": 200,
        "detail": "cannot summarize 'hello marker'; key=[redacted]",
    }
