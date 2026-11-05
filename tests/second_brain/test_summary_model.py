"""A summarizer entry of api openrouter or scaledown sends its provider's request form."""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from belle_agent.second_brain.backend import SecondBrainBackend, SummaryModel, provider_model
from belle_agent.second_brain.extract import Block
from belle_agent.second_brain.ingest import SecondBrainIngestService
from belle_agent.second_brain.summarize import summarize_document

SYSTEM, PROMPT = "Summarise for the index.", "--- DOCUMENT TEXT BEGINS ---\nTide tables.\n--- DOCUMENT TEXT ENDS ---"


def scaledown_reply(text: str) -> dict[str, Any]:
    """The form that direct ScaleDown returned in a live probe, with synthetic content."""
    inner = {
        "summary": text, "input_chars": 4853, "output_chars": len(text), "latency_ms": 1378,
        "ocr_text": None, "input_tokens": 1523, "finish_reason": "stop", "notice": None,
    }
    return {
        "id": "chatcmpl-" + "0" * 32, "object": "chat.completion", "created": 1792963069, "model": "summarize",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(inner)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1523, "completion_tokens": 0, "total_tokens": 1523},
    }


def openrouter_reply(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]}


CASES = {
    "chat_completions": ("https://model.test/v1", "/v1/chat/completions", "vendor/model", openrouter_reply),
    "openrouter": ("https://openrouter.test/api/v1", "/api/v1/chat/completions", "scaledown/summarize", openrouter_reply),
    "scaledown": ("https://scaledown.test", "/v1/chat/completions", "summarize", scaledown_reply),
}


def _model(api: str, handler: Callable[[httpx.Request], httpx.Response], model_name: str | None = None) -> SummaryModel:
    if api == "chat_completions":
        model_name = model_name or "vendor/model"
    model = provider_model(
        {"api": api, "api_base": CASES[api][0], "api_key": "test-key", "model": model_name},
        transport=httpx.MockTransport(handler),
    )
    assert model is not None
    return model


@pytest.mark.parametrize("api", sorted(CASES))
async def test_each_api_sends_its_form_and_reads_its_reply(api: str) -> None:
    _, path, default_model, reply = CASES[api]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply("Spring tides reach four metres."))

    assert await _model(api, handler).invoke(SYSTEM, PROMPT, 5.0) == "Spring tides reach four metres."

    (request,) = seen
    body = json.loads(request.content)
    assert request.url.path == path
    if api == "scaledown":
        assert request.headers["x-api-key"] == "test-key" and "authorization" not in request.headers
        assert (body["instructions"], body["text"]) == (SYSTEM, PROMPT)
    else:
        assert request.headers["authorization"] == "Bearer test-key" and "x-api-key" not in request.headers
        assert "instructions" not in body and "text" not in body
    assert body["model"] == default_model
    # ScaleDown's handling of temperature is untested; OpenRouter gets the chat request's.
    assert body.get("temperature") == (None if api == "scaledown" else 0.2)
    assert body["messages"] == [{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT}]


async def test_an_entry_model_name_is_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "vendor/model"
        return httpx.Response(200, json=openrouter_reply("ok"))

    assert await _model("openrouter", handler, "vendor/model").invoke(SYSTEM, PROMPT, 5.0) == "ok"


@pytest.mark.parametrize(
    ("api", "answer", "error"),
    [
        ("scaledown", openrouter_reply("plain text"), "scaledown response has no text at choices[0].message.content.summary"),
        ("scaledown", openrouter_reply('{"notice": null}'), "scaledown response has no text at choices[0].message.content.summary"),
        ("openrouter", {"choices": []}, "openrouter response has no text at choices[0].message.content"),
    ],
)
async def test_a_reply_without_its_text_is_refused(api: str, answer: dict[str, Any], error: str) -> None:
    model = _model(api, lambda request: httpx.Response(200, json=answer))
    with pytest.raises(ValueError) as raised:
        await model.invoke(SYSTEM, PROMPT, 5.0)
    assert str(raised.value) == error


async def test_a_malformed_reply_costs_the_prose_and_keeps_the_outline(caplog: pytest.LogCaptureFixture) -> None:
    model = _model("scaledown", lambda request: httpx.Response(200, json=openrouter_reply("plain text")))
    blocks = [Block("page", f"p.{i}", (i - 1) * 10, i * 10) for i in (1, 2)]

    with caplog.at_level(logging.WARNING):
        md, rows = await summarize_document(
            text="x" * 20, blocks=blocks, doc_id="d-1", title="Findable", frontmatter={"doc_id": "d-1"}, model=model
        )

    assert "_No summary was produced._" in md and "plain text" not in md
    assert [row["location"] for row in rows] == ["p.1", "p.2"]
    # The log names the exception type only: a provider's message can quote the request.
    assert "summary generation failed: ValueError" in caplog.text


def test_an_entry_without_api_base_writes_no_prose() -> None:
    assert provider_model({"api": "scaledown", "api_key": "test-key"}) is None
    assert provider_model({"api": "chat_completions", "api_base": "https://model.test/v1"}) is None


async def _no_parser(path: object) -> str:
    return ""


async def _ingest(tmp_path: Path, summarizer: dict[str, Any]) -> list[httpx.Request]:
    """Ingest one Markdown file with a ScaleDown entry and an OpenRouter entry, and return the requests sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if "REGIONS BEGIN" in request.content.decode():
            text = "1: The tide table."
        else:
            text = "Spring tides reach four metres."
        return httpx.Response(200, json=(scaledown_reply if request.url.host == "scaledown.test" else openrouter_reply)(text))

    config = {
        "models": {"summarizer": {"default": "prose", "entries": [
            {"id": "prose", "api": "scaledown", "api_base": CASES["scaledown"][0], "api_key": "test-key"},
            {"id": "notes", "api": "openrouter", "api_base": CASES["openrouter"][0], "api_key": "test-key"},
        ]}},
        "second_brain": {"enabled": True, "ingest": {"summarizer": summarizer}},
    }
    backend = SecondBrainBackend(
        build_model=lambda settings: provider_model(settings, transport=httpx.MockTransport(handler)),
        parse_file=_no_parser,
        extract_html=lambda html: ("", ""),
    )
    service = SecondBrainIngestService(config, corpus_dir=tmp_path, backend=backend)
    result = await service.ingest_bytes(b"# Tides\n\nSpring tides in the estuary reach four metres.\n", "tides.md", "text/markdown")
    summary = service.corpus.summary_path_for(result["doc_id"]).read_text()
    assert "Spring tides reach four metres." in summary and "The tide table." in summary
    return sent


async def test_the_outline_notes_go_to_their_own_entry_in_its_own_form(tmp_path: Path) -> None:
    prose, outline = await _ingest(tmp_path, {"outline_model": "notes"})

    assert (prose.url.host, prose.url.path, prose.headers["x-api-key"]) == ("scaledown.test", "/v1/chat/completions", "test-key")
    assert "DOCUMENT TEXT BEGINS" in json.loads(prose.content)["text"]
    assert (outline.url.host, outline.url.path, outline.headers["authorization"]) == (
        "openrouter.test", "/api/v1/chat/completions", "Bearer test-key",
    )
    assert json.loads(outline.content)["model"] == "scaledown/summarize"
    assert "REGIONS BEGIN" in json.loads(outline.content)["messages"][-1]["content"]


async def test_without_an_outline_selector_both_calls_go_to_the_prose_entry(tmp_path: Path) -> None:
    sent = await _ingest(tmp_path, {})

    assert [(request.url.host, request.url.path) for request in sent] == [("scaledown.test", "/v1/chat/completions")] * 2
