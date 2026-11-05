"""The corpus index with vectors: what it finds by meaning, what it embeds, and what it keeps."""

import sqlite3

import numpy as np
import pytest
from topic_embedder import TopicEmbedder

from belle_agent.second_brain.backend import Embedder
from belle_agent.second_brain.corpus_index import CorpusIndex, open_index, refresh_index
from belle_agent.second_brain.lexical import INDEX_FILENAME, search_chunks
from belle_agent.second_brain.paths import summaries_dir

# Synthetic summaries, written for these checks.
_SUMMARIES = {
    "lamp-rotation-0a1b2c3d": "The Fresnel lens at Gannet Rock turns once every twelve seconds. Keepers trim the mantle at dusk. ",
    "starter-hydration-1b2c3d4e": "A rye starter kept at full hydration doubles in six hours. Fold the dough every thirty minutes. ",
    "harbour-tides-2c3d4e5f": "Spring tides in the estuary reach four metres. The ferry slipway floods before high water. ",
    "kite-bridle-3d4e5f6a": "A delta kite stalls when the bridle point moves aft. Shorten the tow line in a light wind. ",
    "hive-inspection-4e5f6a7b": "Check the brood frames for capped cells and count varroa mites on the sticky board. ",
}

#: About the lighthouse, with no word of its summary: only its meaning can find it.
_BY_MEANING = "where does the beacon shine?"


def _write(root, doc_id: str, prose: str) -> None:
    directory = summaries_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{doc_id}.md").write_text(f'---\ndoc_id: "{doc_id}"\n---\n\n## Summary\n\n{prose * 6}\n', encoding="utf-8")


def _corpus(root) -> None:
    for doc_id, prose in _SUMMARIES.items():
        _write(root, doc_id, prose)


def _chunk_count(index) -> int:
    return index.db.execute("SELECT COUNT(DISTINCT hash) FROM chunks").fetchone()[0]


async def _search(index, query: str, *, semantic: bool = True, min_score: float = 0.35):
    return await index.search(query, limit=5, min_score=min_score, semantic=semantic)


async def test_a_question_that_shares_no_word_finds_its_document_by_meaning(tmp_path, topics):
    _corpus(tmp_path)
    index = await open_index(tmp_path, embedder_factory=topics.embedder)

    assert await _search(index, _BY_MEANING, semantic=False) == []
    (hit, *_) = await _search(index, _BY_MEANING)
    assert hit["path"] == "memory/lamp-rotation-0a1b2c3d.md"
    assert 0.35 <= hit["score"] <= 1.0 and "Fresnel" in hit["snippet"]


async def test_every_chunk_is_embedded_once_and_kept_across_a_restart(tmp_path, topics):
    _corpus(tmp_path)
    index = await open_index(tmp_path, embedder_factory=topics.embedder)
    assert len(topics.documents) == _chunk_count(index) > 0

    restarted = TopicEmbedder()
    reopened = CorpusIndex(tmp_path, restarted.embedder())
    await reopened.refresh()

    assert restarted.documents == []
    assert (await _search(reopened, _BY_MEANING))[0]["path"] == "memory/lamp-rotation-0a1b2c3d.md"


async def test_a_changed_summary_embeds_only_what_changed(tmp_path, topics):
    _corpus(tmp_path)
    index = await open_index(tmp_path, embedder_factory=topics.embedder)
    before = len(topics.documents)

    _write(tmp_path, "kite-bridle-3d4e5f6a", "A glider is towed aloft and released when the wind backs. ")
    assert await refresh_index(index)

    (changed,) = topics.documents[before:]
    assert "glider" in changed
    # The vector of the old text went with it.
    stored = index.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert stored == _chunk_count(index)


async def test_a_new_model_embeds_the_corpus_again_and_drops_the_old_vectors(tmp_path, topics):
    _corpus(tmp_path)
    await open_index(tmp_path, embedder_factory=topics.embedder)

    upgraded = TopicEmbedder(model="topics-v2")
    index = CorpusIndex(tmp_path, upgraded.embedder())
    await index.refresh()

    assert len(upgraded.documents) == _chunk_count(index)
    models = {row[0] for row in index.db.execute("SELECT DISTINCT model FROM embeddings")}
    assert models == {"topics-v2"}


async def test_an_unreachable_embedder_leaves_a_lexical_index_that_the_next_sync_completes(tmp_path, topics):
    _corpus(tmp_path)
    topics.failing = True
    index = await open_index(tmp_path, embedder_factory=topics.embedder)

    assert index is not None
    assert index.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    # With no vector to rank, the search is the lexical one, at its own scores.
    assert (await _search(index, "count varroa mites"))[0]["path"] == "memory/hive-inspection-4e5f6a7b.md"
    assert await _search(index, _BY_MEANING) == []

    topics.failing = False
    assert await refresh_index(index)
    assert (await _search(index, _BY_MEANING))[0]["path"] == "memory/lamp-rotation-0a1b2c3d.md"


async def test_a_new_summary_stays_searchable_when_only_its_embedding_fails(tmp_path, topics):
    _corpus(tmp_path)
    index = await open_index(tmp_path, embedder_factory=topics.embedder)
    _write(tmp_path, "quasar-observation-5f6a7b8c", "A quasar emits bright radio waves. ")

    async def fail_documents(_texts):
        raise RuntimeError("embedding endpoint unreachable")

    index.embedder = Embedder(topics.model, fail_documents, topics.embed_query)
    assert await refresh_index(index)

    lexical = await _search(index, "quasar", semantic=False)
    assert lexical and lexical[0]["score"] >= 0.35
    assert (await _search(index, "quasar"))[0]["path"] == lexical[0]["path"]


async def test_a_query_that_cannot_be_embedded_is_ranked_lexically(tmp_path, topics):
    _corpus(tmp_path)
    index = await open_index(tmp_path, embedder_factory=topics.embedder)
    topics.failing = True

    lexical = search_chunks(index.db, "count varroa mites", limit=5, min_score=0.35)
    assert lexical and await _search(index, "count varroa mites") == lexical


async def test_the_scores_mix_by_the_configured_weights(tmp_path, topics):
    _corpus(tmp_path)
    query = "how do I count varroa mites?"
    index = await open_index(tmp_path, embedder_factory=topics.embedder, vector_weight=0.0, text_weight=1.0)
    # All weight on the text: the lexical ranking, at the lexical scores.
    assert await _search(index, query) == search_chunks(index.db, query, limit=5, min_score=0.35)

    index.vector_weight, index.text_weight = 1.0, 0.0
    (hit, *_) = await _search(index, query)
    text = index.db.execute("SELECT text FROM chunks WHERE id = ?", (hit["id"],)).fetchone()[0]
    left, right = np.array(topics.vector(query)), np.array(topics.vector(text))
    assert hit["score"] == pytest.approx(float(left @ right / np.linalg.norm(left) / np.linalg.norm(right)), rel=1e-5)


async def test_with_embeddings_off_nothing_is_embedded_and_the_search_is_lexical(tmp_path, topics):
    _corpus(tmp_path)
    index = await open_index(tmp_path, embedder_factory=topics.embedder)
    queried = len(topics.queries)

    assert await _search(index, _BY_MEANING, semantic=False) == []
    assert len(topics.queries) == queried


async def test_an_index_of_the_older_layout_is_rebuilt_from_the_summaries(tmp_path):
    _corpus(tmp_path)
    old = sqlite3.connect(tmp_path / INDEX_FILENAME)
    old.executescript(
        "CREATE TABLE files (path TEXT PRIMARY KEY, hash TEXT);"
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, path TEXT, source TEXT, start_line INTEGER, end_line INTEGER, text TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(text);"
        # A file the old index says is current, with no chunk to show for it.
        "INSERT INTO files VALUES ('memory/hive-inspection-4e5f6a7b.md', 'stale');"
    )
    old.commit()
    old.close()

    index = await open_index(tmp_path)

    assert index.db.execute("PRAGMA user_version").fetchone()[0] >= 1
    assert index.db.execute("SELECT COUNT(*) FROM chunks WHERE hash IS NULL").fetchone()[0] == 0
    assert (await _search(index, "count varroa mites", semantic=False))[0]["path"] == "memory/hive-inspection-4e5f6a7b.md"


async def test_one_document_mixes_the_normalized_lexical_score_with_its_vector(tmp_path, topics):
    _write(tmp_path, "lamp-rotation", "Fresnel lens rotation")
    index = await open_index(tmp_path, embedder_factory=topics.embedder)
    query = "Fresnel lens"
    (lexical,) = await _search(index, query, semantic=False)
    (mixed,) = await _search(index, query)
    text = index.db.execute("SELECT text FROM chunks WHERE id = ?", (mixed["id"],)).fetchone()[0]
    left, right = np.array(topics.vector(query)), np.array(topics.vector(text))
    cosine = float(left @ right / np.linalg.norm(left) / np.linalg.norm(right))
    assert mixed["score"] == pytest.approx(0.7 * cosine + 0.3 * lexical["score"], rel=1e-5)
