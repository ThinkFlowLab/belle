"""Library services the second brain reads, summarizes and embeds with."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from belle_agent.tools.providers import request_provider, summary_text

logger = logging.getLogger(__name__)

SUMMARY_TEMPERATURE = 0.2


@dataclass(frozen=True)
class Embedder:
    """One embedding endpoint: the model it embeds with, and a call for documents and for a query."""

    model: str
    embed_documents: Callable[[list[str]], Awaitable[list[list[float]]]]
    embed_query: Callable[[str], Awaitable[list[float]]]


@dataclass(frozen=True)
class SummaryModel:
    """One summarizer endpoint: the model it writes with, and a call of a system and a user prompt.

    ``model`` is empty when the endpoint chooses its own default model.

    ``invoke(system, prompt, timeout)`` returns the reply text. The caller bounds the call by ``timeout`` too.
    """

    model: str
    invoke: Callable[[str, str, float], Awaitable[str]]


def provider_model(
    settings: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> SummaryModel | None:
    """Build a summarizer that sends the request form of its API."""
    api = settings["api"]
    api_base = str(settings.get("api_base") or "").strip()
    if not api_base:
        logger.warning("[SecondBrain] %s summarizer has no api_base; skipping prose", api)
        return None
    name = str(settings.get("model") or "").strip()
    if api == "chat_completions" and not name:
        logger.warning("[SecondBrain] chat_completions summarizer has no model; skipping prose")
        return None
    # ScaleDown's handling of temperature is untested, so the scaledown route sends none.
    options = {"temperature": SUMMARY_TEMPERATURE} if api != "scaledown" else {}

    async def invoke(system: str, prompt: str, timeout: float) -> str:
        result = await request_provider(
            api=api,
            base_url=api_base,
            api_key=str(settings.get("api_key") or ""),
            model=name or None,
            operation="summarize",
            payload={"instructions": system, "text": prompt, **options},
            timeout=timeout,
            transport=transport,
        )
        return summary_text(api, result)

    return SummaryModel(model=name, invoke=invoke)


@dataclass
class SecondBrainBackend:
    #: Builds the summarizer from the selected ``models.summarizer`` entry, or nothing, and summaries have no prose.
    build_model: Callable[[dict[str, Any]], SummaryModel | None]
    parse_file: Callable[[Path], Awaitable[str]]
    extract_html: Callable[[str], tuple[str, str]]
    #: Builds the embedder from ``second_brain.retrieval.embeddings``, or nothing, and search is lexical.
    build_embedder: Callable[[dict[str, Any]], Embedder | None] = field(default=lambda settings: None)


class UnreadableDocument(ValueError):
    """A file type or file contents Belle cannot read."""
