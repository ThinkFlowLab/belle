"""Lexical ranking on the corpus index: the right document must come first."""

from functools import partial

import pytest

from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND
from belle_agent.second_brain.backend import SummaryModel
from belle_agent.second_brain.index import (
    get_second_brain_manager as _get_second_brain_manager,
)
from belle_agent.second_brain.ingest import (
    SecondBrainIngestService as _SecondBrainIngestService,
)
from belle_agent.second_brain.lexical import MAX_PRICED_TOKENS, build_query, search_chunks
from belle_agent.second_brain.retrieval import search_corpus as _search_corpus

SecondBrainIngestService = partial(_SecondBrainIngestService, backend=SECOND_BRAIN_BACKEND)
get_second_brain_manager = partial(_get_second_brain_manager, backend=SECOND_BRAIN_BACKEND)
search_corpus = partial(_search_corpus, backend=SECOND_BRAIN_BACKEND)


async def _echo(system, prompt, timeout):
    """Keep document vocabulary in offline summaries for ranking tests."""
    body = prompt.split("DOCUMENT TEXT BEGINS ---", 1)[-1]
    body = body.split("--- DOCUMENT TEXT ENDS", 1)[0]
    if "REGIONS BEGIN" in prompt:
        return "1: section summary."
    return " ".join(body.split())[:600]


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(SECOND_BRAIN_BACKEND, "build_model", lambda settings: SummaryModel(model="echo", invoke=_echo))


#: Several documents avoid zero BM25 IDF in a two-chunk index.
_CORPUS = {
    "quilted.md": b"# Quilted Latent Routing\n\n"
    + b"We ablate quilt sizes of 3, 7 and 11 latent cells. Overlapping quilts "
    b"are routed to expert heads by a gating network, and the recombination "
    b"uses a cosine taper to remove seam artefacts. " * 5,
    "codegen.md": b"# Register Allocation is Hard\n\n"
    + b"Code generation for expression trees is NP-complete. The reduction is "
    b"from feedback arc set, and holds even for a single register machine "
    b"with commutative operators. " * 5,
    "kubernetes.md": b"# Operators and Reconcile Loops\n\n"
    + b"The reconcile loop watches custom resource definitions and drives "
    b"observed state towards desired state, requeueing on conflict. " * 5,
    "sharding.md": b"# Consistent Hashing for Shard Placement\n\n"
    + b"Virtual nodes smooth the placement distribution, and rebalancing "
    b"moves only the keys whose owning token changed. " * 5,
    "raft.md": b"# Leader Election Under Partition\n\n"
    + b"A candidate increments its term and requests votes; a split vote "
    b"resolves through randomised election timeouts. " * 5,
}

_QUESTIONS = [
    ("What are the limitations of Quilted Latent Routing?", "quilted", "quilted"),
    ("is code generation NP-complete?", "generation", "codegen"),
    ("how does the reconcile loop handle conflict?", "reconcile", "kubernetes"),
    ("what do virtual nodes do for rebalancing?", "virtual", "sharding"),
    ("how is a split vote resolved?", "split", "raft"),
]


async def _indexed_corpus(root):
    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=root)
    for name, body in _CORPUS.items():
        await service.ingest_bytes(body, name, "text/markdown")
    return await get_second_brain_manager(corpus_dir=root)


async def lexical_search(manager, query, *, limit, min_score):
    return search_chunks(manager.db, query.strip(), limit=limit, min_score=min_score)


@pytest.mark.asyncio
async def test_the_right_document_wins_on_a_real_index(tmp_path, offline) -> None:
    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    for query, _, expected in _QUESTIONS:
        hits = await search_corpus(manager, query, limit=10, min_score=0.35)
        assert hits, f"nothing cleared the floor for {query!r}"
        assert expected in hits[0]["path"].lower(), (
            f"{query!r} -> wrong document first: "
            f"{[(h['path'], round(h['score'], 3)) for h in hits[:3]]}"
        )


@pytest.mark.asyncio
async def test_a_slack_turn_finds_its_document_through_the_rail(tmp_path, offline) -> None:
    from types import SimpleNamespace

    from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn
    from openjiuwen.core.single_agent.rail.base import InvokeInputs

    from belle_agent.integrations.openjiuwen.rails.second_brain_rail import (
        SecondBrainRail,
    )

    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    for query, distinctive, expected in _QUESTIONS:
        rendered = UserTurn(text=query, channel="slack", language="en", files={}).render()
        assert distinctive in build_query(manager.db, rendered).lower()

        ctx = SimpleNamespace(inputs=InvokeInputs(query=rendered))
        hits = await search_corpus(
            manager, SecondBrainRail._last_user_query(ctx), limit=10, min_score=0.35
        )
        assert hits, f"nothing cleared the floor for {query!r}"
        assert expected in hits[0]["path"].lower(), (
            f"{query!r} -> wrong document first: "
            f"{[(h['path'], round(h['score'], 3)) for h in hits[:3]]}"
        )


#: A long instruction preamble must not displace the question's terms.
_PREAMBLE = """Answer the question below from the enterprise documentation
available to you. Be specific: give figures, names, identifiers and dates
exactly as the documentation states them, rather than describing them in
general terms.

If the documentation does not cover the question, say so plainly rather than
answering from your own knowledge.

Use only the tools you already have. If none of them can reach the
documentation, or if what you can reach does not answer the question, say so
and stop. Do not search for additional tools and do not delegate.

Question: """


@pytest.mark.asyncio
async def test_a_preamble_does_not_displace_the_question(tmp_path, offline) -> None:

    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    for question, distinctive, expected in _QUESTIONS:
        prompted = _PREAMBLE + question
        assert distinctive in build_query(manager.db, prompted).lower(), (
            f"{distinctive!r} was priced out of {build_query(manager.db, prompted)!r}"
        )

        hits = await search_corpus(manager, prompted, limit=10, min_score=0.35)
        assert hits, f"nothing cleared the floor for {question!r} behind a preamble"
        assert expected in hits[0]["path"].lower(), (
            f"{question!r} behind a preamble -> wrong document first: "
            f"{[(h['path'], round(h['score'], 3)) for h in hits[:3]]}"
        )


@pytest.mark.asyncio
async def test_a_long_turn_is_priced_at_a_bounded_cost(tmp_path, offline) -> None:
    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    real = manager.db
    counting = _CountingDb(real)
    manager.db = counting
    try:
        # Distinct tokens, well past the cap, plus one repeated word to show
        # that repetition is not priced twice.
        paste = " ".join(f"token{n}" for n in range(5000)) + " quilted " * 50
        build_query(manager.db, paste)
    finally:
        manager.db = real

    assert counting.matches <= MAX_PRICED_TOKENS, (
        f"{counting.matches} MATCH queries for one turn"
    )


@pytest.mark.asyncio
async def test_an_unrelated_message_retrieves_nothing(tmp_path, offline) -> None:
    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    for message in ("thanks!", "ok great", "?", "👍"):
        assert await search_corpus(manager, message, limit=5, min_score=0.35) == []


@pytest.mark.asyncio
async def test_a_token_carried_by_most_of_the_index_is_dropped(tmp_path, offline) -> None:

    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    total = manager.db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    common = manager.db.execute(
        'SELECT COUNT(*) c FROM chunks_fts WHERE chunks_fts MATCH \'"the"\''
    ).fetchone()["c"]
    assert common / total >= 0.5, "fixture no longer has a common token to test"

    assert build_query(manager.db, "the") == ""
    assert "quilted" in build_query(manager.db, "the Quilted").lower()


class _Rows(list):
    def fetchall(self):
        return list(self)


class _CountingDb:
    """Count FTS matches and fetched chunk bodies."""

    def __init__(self, db) -> None:
        self._db = db
        self.matches = 0
        self.chunk_rows_read = 0

    def execute(self, sql, *args, **kwargs):
        if "chunks_fts MATCH" in sql:
            self.matches += 1
        cursor = self._db.execute(sql, *args, **kwargs)
        if "FROM chunks" in sql and "chunks_fts" not in sql and "COUNT(" not in sql:
            rows = _Rows(cursor.fetchall())
            self.chunk_rows_read += len(rows)
            return rows
        return cursor

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.mark.asyncio
async def test_a_search_reads_the_rows_it_returns_and_not_the_corpus(
    tmp_path, offline
) -> None:
    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    indexed = manager.db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    assert indexed > 3, "the fixture has to be bigger than the limit to say anything"

    counting = _CountingDb(manager.db)
    manager.db = counting
    try:
        hits = await lexical_search(
            manager,
            "What are the limitations of Quilted Latent Routing?",
            limit=3,
            min_score=0.35,
        )
    finally:
        manager.db = counting._db

    assert hits, "the fixture query still has to match something"
    assert counting.chunk_rows_read == len(hits)
    assert counting.chunk_rows_read < indexed


@pytest.mark.asyncio
async def test_only_the_asked_for_sources_come_back(tmp_path, offline) -> None:
    manager = await _indexed_corpus(tmp_path)
    if manager is None:
        pytest.skip("no index manager in this environment")

    # FTS has no source column, so only the chunk rows say where a match came from.
    manager.db.execute("UPDATE chunks SET source = 'sessions'")
    hits = await lexical_search(
        manager,
        "What are the limitations of Quilted Latent Routing?",
        limit=5,
        min_score=0.35,
    )
    assert hits == [], "a source other than the summaries must not answer"
