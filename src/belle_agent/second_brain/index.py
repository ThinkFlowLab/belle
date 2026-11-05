"""Open the corpus index with the backend's embedder, and decide whether a search uses vectors."""

import logging
import os
from pathlib import Path
from typing import Any

from belle_agent.second_brain.backend import SecondBrainBackend
from belle_agent.second_brain.corpus_index import DEFAULT_TEXT_WEIGHT, DEFAULT_VECTOR_WEIGHT, CorpusIndex, open_index
from belle_agent.second_brain.paths import config_section, resolve_second_brain_dir, second_brain_config

logger = logging.getLogger(__name__)

#: Override vector ranking for one process without rebuilding the corpus.
ENV_USE_EMBEDDINGS = "SECOND_BRAIN_USE_EMBEDDINGS"

_ON_VALUES = frozenset({"1", "true", "yes", "on"})


def use_embeddings(config: dict[str, Any] | None = None) -> bool:
    """Whether vectors are kept and searched. Off unless the environment or ``retrieval.use_embeddings`` says on."""
    raw = os.getenv(ENV_USE_EMBEDDINGS)
    if raw is None:
        raw = _retrieval(config).get("use_embeddings")
    return str(raw).strip().lower() in _ON_VALUES


def embedding_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """The embedding endpoint, under ``second_brain.retrieval.embeddings``."""
    return config_section(_retrieval(config), "embeddings")


def _retrieval(config: dict[str, Any] | None) -> dict[str, Any]:
    return config_section(second_brain_config(config), "retrieval")


def _weight(retrieval: dict[str, Any], key: str, default: float) -> float:
    """Use the default for an empty or invalid weight."""
    value = retrieval.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("[SecondBrain] retrieval.%s=%r is not a number; using %s", key, value, default)
        return default


async def get_second_brain_manager(
    config: dict[str, Any] | None = None,
    *,
    corpus_dir: Path | str | None = None,
    backend: SecondBrainBackend | None = None,
) -> CorpusIndex | None:
    """The corpus index, or ``None`` with no backend or where it cannot be opened."""
    if backend is None:
        return None
    root = Path(corpus_dir) if corpus_dir else resolve_second_brain_dir(config)
    retrieval = _retrieval(config)

    def embedder():
        return backend.build_embedder(embedding_settings(config)) if use_embeddings(config) else None

    return await open_index(
        root,
        embedder_factory=embedder,
        vector_weight=_weight(retrieval, "vector_weight", DEFAULT_VECTOR_WEIGHT),
        text_weight=_weight(retrieval, "text_weight", DEFAULT_TEXT_WEIGHT),
    )


__all__ = [
    "ENV_USE_EMBEDDINGS",
    "embedding_settings",
    "get_second_brain_manager",
    "use_embeddings",
]
