"""Recall preserves matching outline evidence within its byte budget."""

import pytest

from belle_agent.second_brain.corpus_index import CorpusIndex
from belle_agent.second_brain.recall import SecondBrainRecall
from belle_agent.second_brain.store import BrainCorpus, BrainDoc


@pytest.mark.parametrize(
    ("query", "name"),
    [("CAFE", "café"), ("café", "CAFE"), ("alpha", "alpha_beta"), ("alpha_beta", "alpha beta")],
)
async def test_normalized_outline_match_survives_recall_budget(tmp_path, query, name):
    corpus = BrainCorpus(tmp_path)
    corpus.ensure_layout()
    corpus.put(BrainDoc(doc_id="archive-1", sha256="0" * 64, title="Archive"))
    fact = f"The {name} opens at midnight."
    rows = [
        f"| {i} | Section {i} | `## Section {i}` | Routine 記録 notes for the archive shelves and opening hours. |"
        for i in range(1, 10)
    ]
    passage = "\n".join([*rows, f"| 10 | Section 10 | `## Section 10` | {fact} |"])
    assert len(passage) <= 1000
    index = CorpusIndex(tmp_path)
    try:
        cursor = index.db.execute("INSERT INTO chunks_fts(text) VALUES (?)", (passage,))
        index.db.execute(
            "INSERT INTO chunks(rowid, id, path, source, start_line, end_line, text) "
            "VALUES (?, 'outline', 'memory/archive-1.md', 'memory', 1, 10, ?)",
            (cursor.lastrowid, passage),
        )
        recall = SecondBrainRecall(
            corpus_dir=tmp_path, manager=index, backend=object(), max_total_bytes=1024,
        )

        result = await recall.recall(query)

        assert result is not None
        assert fact in result
        assert "`## Section 10`" in result
        assert len(result.encode("utf-8")) < 1024 + 200
    finally:
        index.db.close()
