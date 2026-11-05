"""The corpus index answers a search, with vectors where the configuration allows them."""

import pytest

from belle_agent.second_brain.retrieval import search_corpus


class _Index:
    def __init__(self) -> None:
        self.seen = []

    async def search(self, query, *, limit, min_score, semantic):
        self.seen.append((query, limit, min_score, semantic))
        return [{"path": "doc.md"}]


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.asyncio
async def test_the_index_searches_with_the_configured_switch(semantic):
    index = _Index()
    config = {"second_brain": {"retrieval": {"use_embeddings": semantic}}}

    assert await search_corpus(index, "query", limit=3, min_score=0.2, config=config, backend=object()) == [{"path": "doc.md"}]
    assert index.seen == [("query", 3, 0.2, semantic)]


@pytest.mark.asyncio
async def test_with_no_index_or_no_backend_nothing_is_searched():
    index = _Index()

    assert await search_corpus(None, "query", limit=3, min_score=0.2, backend=object()) == []
    assert await search_corpus(index, "query", limit=3, min_score=0.2, backend=None) == []
    assert index.seen == []
