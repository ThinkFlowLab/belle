"""Search the corpus index."""

from typing import Any

from belle_agent.second_brain.backend import SecondBrainBackend
from belle_agent.second_brain.index import use_embeddings


async def search_corpus(
    manager: Any,
    query: str,
    *,
    limit: int,
    min_score: float,
    config: dict[str, Any] | None = None,
    backend: SecondBrainBackend | None = None,
) -> list[dict[str, Any]]:
    if manager is None or backend is None:
        return []
    return await manager.search(query, limit=limit, min_score=min_score, semantic=use_embeddings(config))


__all__ = ["search_corpus"]
