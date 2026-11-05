"""The core's lexical index: what it indexes, what it ranks first, and what it forgets."""

import sqlite3

import pytest

from belle_agent.second_brain import lexical
from belle_agent.second_brain.corpus_index import open_index, refresh_index
from belle_agent.second_brain.paths import summaries_dir

# Synthetic summaries, written for these checks.
_SUMMARIES = {
    "lamp-rotation-0a1b2c3d": "The Fresnel lens at Gannet Rock turns once every twelve seconds. Keepers trim the mantle at dusk. ",
    "starter-hydration-1b2c3d4e": "A rye starter kept at full hydration doubles in six hours. Fold the dough every thirty minutes. ",
    "harbour-tides-2c3d4e5f": "Spring tides in the estuary reach four metres. The ferry slipway floods before high water. ",
    "kite-bridle-3d4e5f6a": "A delta kite stalls when the bridle point moves aft. Shorten the tow line in a light wind. ",
    "hive-inspection-4e5f6a7b": "Check the brood frames for capped cells and count varroa mites on the sticky board. ",
}


def _write(root, doc_id: str, prose: str) -> None:
    directory = summaries_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{doc_id}.md").write_text(f'---\ndoc_id: "{doc_id}"\n---\n\n## Summary\n\n{prose * 6}\n', encoding="utf-8")


@pytest.fixture
async def index(tmp_path):
    for doc_id, prose in _SUMMARIES.items():
        _write(tmp_path, doc_id, prose)
    return await open_index(tmp_path)


async def _first(index, query: str) -> str:
    hits = await index.search(query, limit=10, min_score=0.35, semantic=False)
    return hits[0]["path"] if hits else ""


async def test_the_document_a_question_is_about_ranks_first(index):
    for query, expected in (
        ("how often does the Fresnel lens turn?", "lamp-rotation"),
        ("when does a rye starter double?", "starter-hydration"),
        ("does the ferry slipway flood?", "harbour-tides"),
        ("why does a delta kite stall?", "kite-bridle"),
        ("how do I count varroa mites?", "hive-inspection"),
    ):
        assert expected in await _first(index, query), query


async def test_a_hit_names_its_summary_and_carries_the_matching_passage(index):
    (hit, *_) = await index.search("varroa mites", limit=3, min_score=0.35, semantic=False)
    assert hit["path"] == "memory/hive-inspection-4e5f6a7b.md"
    assert hit["source"] == "memory" and "varroa" in hit["snippet"] and 0.35 <= hit["score"] < 1.0
    assert hit["startLine"] >= 1 and hit["endLine"] >= hit["startLine"]


async def test_a_message_about_nothing_in_the_corpus_retrieves_nothing(index):
    for message in ("thanks!", "ok great", "?", "", "zeppelin"):
        assert await index.search(message, limit=5, min_score=0.35, semantic=False) == []


async def test_a_long_summary_is_indexed_in_chunks_of_bounded_size(tmp_path):
    for doc_id, prose in _SUMMARIES.items():
        _write(tmp_path, doc_id, prose)
    lines = "\n".join(f"Line {n} of the ledger records a cargo of salted herring." for n in range(200))
    (summaries_dir(tmp_path) / "ledger-5f6a7b8c.md").write_text(lines + "\nThe last entry names the schooner Kittiwake.\n")
    index = await open_index(tmp_path)

    rows = index.db.execute("SELECT text, start_line, end_line FROM chunks WHERE path LIKE '%ledger%'").fetchall()
    assert len(rows) > 5 and all(len(row["text"]) <= 1100 for row in rows)
    # The chunks cover the file once, in order, with no line in two of them.
    assert [row["start_line"] for row in rows][1:] == [row["end_line"] + 1 for row in rows][:-1]
    # A hit is the whole chunk that matched, not a truncated prefix or the whole file.
    (hit, *_) = await index.search("schooner Kittiwake", limit=3, min_score=0.35, semantic=False)
    assert (hit["startLine"], hit["endLine"]) == (rows[-1]["start_line"], rows[-1]["end_line"])
    assert hit["snippet"] == rows[-1]["text"]


async def test_a_match_after_long_chunk_prose_is_preserved(index):
    prefix = "Earlier summary prose. " * 8
    fact = "The rare schooner Kittiwake carries the sealed archive."
    _write(index.root, "late-fact-6a7b8c9d", prefix + "x " * 300 + fact)
    index.sync()

    (hit, *_) = await index.search("schooner Kittiwake sealed archive", limit=3, min_score=0.35, semantic=False)

    assert len(prefix) >= 80
    assert hit["path"].endswith("late-fact-6a7b8c9d.md")
    assert fact in hit["snippet"]


async def test_a_sync_indexes_what_changed_and_forgets_what_is_gone(index, tmp_path):
    assert await _first(index, "zeppelin mooring mast") == ""

    _write(tmp_path, "kite-bridle-3d4e5f6a", "The zeppelin is moored to a mast and vents hydrogen at dawn. ")
    (summaries_dir(tmp_path) / "hive-inspection-4e5f6a7b.md").unlink()
    assert await refresh_index(index)

    assert "kite-bridle" in await _first(index, "zeppelin mooring mast")
    # The old text of the rewritten summary is gone. Its id, which the new text still carries, is not asked for.
    assert await _first(index, "what stalls when the point moves aft?") == ""
    assert await _first(index, "how do I count varroa mites?") == ""
    # Nothing is left behind in either table for the rewritten and the removed summary.
    counts = [index.db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] for table in ("chunks", "chunks_fts")]
    assert counts[0] == counts[1] == index.db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 4


async def test_the_index_is_kept_on_disk_and_opened_once(index, tmp_path):
    assert await open_index(tmp_path) is index
    stored = sqlite3.connect(tmp_path / lexical.INDEX_FILENAME)
    assert stored.execute("SELECT COUNT(*) FROM files").fetchone()[0] == len(_SUMMARIES)


async def test_a_corpus_that_cannot_be_opened_has_no_index(tmp_path):
    (tmp_path / "blocked").write_text("a file where the corpus directory should be")
    assert await open_index(tmp_path / "blocked") is None


@pytest.fixture
def score_index(tmp_path):
    index = lexical.LexicalIndex(tmp_path)

    def add(text, source="memory"):
        cursor = index.db.execute("INSERT INTO chunks_fts(text) VALUES (?)", (text,))
        index.db.execute(
            "INSERT INTO chunks(rowid, id, path, source, start_line, end_line, text) VALUES (?, ?, ?, ?, 1, 1, ?)",
            (cursor.lastrowid, str(cursor.lastrowid), f"memory/{cursor.lastrowid}.md", source, text),
        )

    yield index.db, add
    index.db.close()


@pytest.mark.parametrize("corpus_size", [1, 2, 8])
def test_complete_matches_clear_the_default_floor_in_small_and_large_corpora(score_index, corpus_size):
    db, add = score_index
    add("Fresnel lens rotation guide")
    for _ in range(corpus_size - 1):
        add("rye starter hydration notes")

    for query in ("Fresnel lens", "Fresnel lens rotation guide"):
        (hit,) = lexical.search_chunks(db, query, limit=5, min_score=0.35)
        assert hit["score"] == pytest.approx(0.5)
    assert lexical.search_chunks(db, "unmatched zeppelin", limit=5, min_score=0.35) == []


def test_partial_matches_remain_bounded_and_scores_preserve_fts_ranking(score_index):
    db, add = score_index
    add("alpha beta gamma delta")
    add("alpha filler filler filler")
    query = "alpha beta gamma delta"
    hits = lexical.search_chunks(db, query, limit=5, min_score=0.0)
    ranked = db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank", (lexical.build_query(db, query),)
    ).fetchall()
    assert [hit["id"] for hit in hits] == [str(row["rowid"]) for row in ranked]
    assert hits[0]["score"] == pytest.approx(0.5)
    assert 0 < hits[1]["score"] < 0.35
    assert lexical.search_chunks(db, query, limit=5, min_score=0.35) == hits[:1]


def test_normalization_uses_fts_phrases_and_all_fts_rows(score_index):
    db, add = score_index
    add("café alpha beta")
    for _ in range(4):
        add("rye starter notes", source="sessions")

    (hit,) = lexical.search_chunks(db, "CAFE alpha_beta", limit=5, min_score=0.35)
    assert hit["score"] == pytest.approx(0.5)


async def test_fixed_drill_down_instructions_are_not_searchable_after_an_index_upgrade(tmp_path):
    from belle_agent.second_brain.summarize import render_summary

    directory = summaries_dir(tmp_path)
    directory.mkdir(parents=True)
    path = directory / "footer-1.md"
    summary = render_summary(
        doc_id="footer-1", title="Archive", frontmatter={}, prose="Kittiwake archive.", rows=[],
    )
    path.write_text(summary, encoding="utf-8")
    old = lexical.LexicalIndex(tmp_path)
    old.sync()
    # Seed an existing index with the old unfiltered chunk and unchanged file hash.
    old.db.execute("UPDATE chunks SET text = ?", (summary,))
    old.db.execute("UPDATE chunks_fts SET text = ?", (summary,))
    old.db.execute("PRAGMA user_version = 1")
    old.db.commit()
    assert lexical.search_chunks(old.db, "exactly shown", limit=3, min_score=0.35)
    old.db.close()

    upgraded = lexical.LexicalIndex(tmp_path)
    try:
        upgraded.sync()
        assert lexical.search_chunks(upgraded.db, "exactly shown", limit=3, min_score=0.35) == []
        assert lexical.search_chunks(upgraded.db, "Kittiwake archive", limit=3, min_score=0.35)
        assert path.read_text(encoding="utf-8") == summary
    finally:
        upgraded.db.close()


async def test_search_logs_hold_no_query_text(index, monkeypatch, caplog):
    secret = "Gannet Rock varroa"
    caplog.set_level("INFO", logger=lexical.__name__)
    assert lexical.search_chunks(index.db, "zzqx plover", limit=3, min_score=0.35) == []

    def fail(db, fts_query):
        raise sqlite3.OperationalError(f"fts5: syntax error near {fts_query}")

    monkeypatch.setattr(lexical, "_query_idf", fail)
    assert lexical.search_chunks(index.db, secret, limit=3, min_score=0.35) == []

    logged = caplog.text
    assert "no term distinctive" in logged and "OperationalError" in logged
    for word in ("zzqx", "plover", "Gannet", "varroa"):
        assert word not in logged
