"""Second Brain services that need no agent harness."""

from typing import Any

from belle_agent.second_brain.backend import SecondBrainBackend, SummaryModel, provider_model
from belle_agent.second_brain.documents import extract_html, parse_file
from belle_agent.second_brain.embedder import build_embedder


def build_model(settings: dict[str, Any]) -> SummaryModel | None:
    """Build a summarizer for the selected Belle model entry."""
    return provider_model(settings) if settings.get("api") else None


SECOND_BRAIN_BACKEND = SecondBrainBackend(
    build_model=build_model,
    parse_file=parse_file,
    extract_html=extract_html,
    build_embedder=build_embedder,
)
