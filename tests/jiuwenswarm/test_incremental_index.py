"""Ingesting reindexes only the new summary, whatever the corpus already holds.

Each ingest runs the index's ``sync``, which hashes every summary. What these
tests count is how many files that pass reindexes: ``_add`` is where the
chunking cost is, and each chunk it adds is one the embedder is asked for.
"""

from functools import partial

import pytest
from belle_agent.integrations.jiuwenswarm.second_brain_backend import (
    SECOND_BRAIN_BACKEND,
)
from belle_agent.second_brain.index import (
    get_second_brain_manager as _get_second_brain_manager,
)
from belle_agent.second_brain.ingest import (
    STATUS_INGESTED,
)
from belle_agent.second_brain.ingest import (
    SecondBrainIngestService as _SecondBrainIngestService,
)

SecondBrainIngestService = partial(_SecondBrainIngestService, backend=SECOND_BRAIN_BACKEND)
get_second_brain_manager = partial(_get_second_brain_manager, backend=SECOND_BRAIN_BACKEND)


@pytest.fixture
def offline(monkeypatch):
    """No summariser model, so the tests exercise the indexing and not an LLM."""
    monkeypatch.setattr(SECOND_BRAIN_BACKEND, "build_model", lambda settings: None)


#: Distinct subjects, because BM25 is being asked real questions below. A
#: corpus of near-identical documents gives every term the same document
#: frequency, and the ranking it produces measures nothing.
_TOPICS = [
    ("Quilted Latent Routing", "quilted", "tokens are assigned to experts by a learned quilt"),
    ("Raft Leader Election", "raft", "a split vote is resolved by randomised election timeouts"),
    ("Consistent Hashing Shards", "sharding", "virtual nodes smooth rebalancing across the ring"),
    ("Kubernetes Reconcile Loops", "kubernetes", "the controller drives observed state toward desired"),
    ("Codegen Complexity Bounds", "codegen", "synthesis from a specification is NP-complete"),
]


def _document(n: int) -> tuple[bytes, str]:
    title, slug, sentence = _TOPICS[n % len(_TOPICS)]
    body = (
        f"# {title}\n\n## Method\n\n{sentence}.\n"
        f"This is document number {n}.\n" + "filler " * 60
    )
    return body.encode("utf-8"), f"{slug}-{n}.md"


class _CountingIndex:
    """Wraps ``_add`` on one index and records the paths it indexes."""

    def __init__(self, manager) -> None:
        self.paths: list[str] = []
        self._inner = manager._add
        manager._add = self

    def __call__(self, path, digest, text):
        self.paths.append(path)
        return self._inner(path, digest, text)


async def _counted_manager(tmp_path):
    # Built first so the counter is installed before any ingest runs. The
    # index is opened once per corpus, so the service's own lookup returns
    # this instance.
    manager = await get_second_brain_manager(corpus_dir=tmp_path)
    assert manager is not None
    return manager, _CountingIndex(manager)


@pytest.mark.asyncio
async def test_ingesting_reindexes_only_the_new_summary(tmp_path, offline) -> None:
    manager, counter = await _counted_manager(tmp_path)

    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)
    for n in range(4):
        content, name = _document(n)
        assert (await service.ingest_bytes(content, name, "text/markdown"))[
            "status"
        ] == STATUS_INGESTED

    assert len(counter.paths) == 4, (
        f"{len(counter.paths)} files reindexed for 4 ingests; unchanged"
        " summaries are being indexed again"
    )
    assert len(set(counter.paths)) == 4

    indexed = manager.db.execute(
        "SELECT COUNT(DISTINCT path) c FROM chunks"
    ).fetchone()["c"]
    assert indexed == 4


@pytest.mark.asyncio
async def test_a_summary_deleted_from_disk_leaves_the_index(tmp_path, offline) -> None:
    """The per-ingest sync also drops rows for summaries that are gone."""
    manager, _ = await _counted_manager(tmp_path)

    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)
    content, name = _document(0)
    await service.ingest_bytes(content, name, "text/markdown")
    (removed,) = (tmp_path / "memory").glob("*.md")
    removed.unlink()

    content, name = _document(1)
    await service.ingest_bytes(content, name, "text/markdown")

    paths = {
        row["path"]
        for row in manager.db.execute("SELECT DISTINCT path FROM chunks").fetchall()
    }
    assert len(paths) == 1
    assert removed.name not in next(iter(paths))


@pytest.mark.asyncio
async def test_a_summary_is_searchable_when_the_ingest_returns(tmp_path, offline) -> None:
    from belle_agent.second_brain.retrieval import search_corpus

    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)

    # The rest of the corpus first, so the last ingest is the one being asked
    # about and BM25 has something to discriminate against. With one document
    # every term has the same document frequency and no query means anything.
    for n in range(1, len(_TOPICS)):
        content, name = _document(n)
        await service.ingest_bytes(content, name, "text/markdown")

    content, name = _document(0)
    result = await service.ingest_bytes(content, name, "text/markdown")
    assert result["status"] == STATUS_INGESTED

    manager = await get_second_brain_manager(corpus_dir=tmp_path)
    assert manager is not None

    hits = await search_corpus(
        manager, "quilted latent routing", limit=5, min_score=0.1,
        backend=SECOND_BRAIN_BACKEND,
    )
    assert hits, "the document was not searchable when its ingest returned"
    assert result["doc_id"] in hits[0]["path"]
