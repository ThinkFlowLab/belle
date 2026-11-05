"""Rank corpus summaries lexically in a SQLite full-text index.

The search picks the distinctive terms of a query, ranks chunks with BM25 and
loads the rows that cleared the floor. It reads any database that holds a
``chunks`` table and an FTS5 table ``chunks_fts`` over the same row ids.
``LexicalIndex`` keeps that database with the standard library alone. The
corpus index adds vectors beside it.
"""

import hashlib
import logging
import math
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from belle_agent.second_brain.failures import describe
from belle_agent.second_brain.paths import summaries_dir

logger = logging.getLogger(__name__)

FTS_TABLE = "chunks_fts"
#: Summaries are the only source the corpus index holds.
SOURCE = "memory"
SOURCE_FILTER = f"source = '{SOURCE}'"

#: FTS query tokens.
_TOKEN_RE = re.compile(r"\w+")

#: At 100 terms, benchmark recall reached 50/64 and then flattened.
_MAX_QUERY_TOKENS = 100

#: Bound frequency checks before selecting rare terms.
MAX_PRICED_TOKENS = 200

#: Keep terms found in at most two chunks, even in small corpora.
_RARE_TOKEN_MAX_DOCS = 2

#: Exclude terms present in at least half the index.
_COMMON_TOKEN_SHARE = 0.5


def rank_to_score(rank: float) -> float:
    """Map a negative FTS5 BM25 rank to a score in 0..1. Any other rank scores zero."""
    strength = -float(rank)
    return strength / (1.0 + strength) if strength > 0.0 else 0.0


def _distinct_tokens(cleaned: str) -> list[str]:
    """Keep first spellings and order. FTS5 folds case."""
    tokens: dict[str, str] = {}
    for token in _TOKEN_RE.findall(cleaned):
        tokens.setdefault(token.lower(), token)
        if len(tokens) >= MAX_PRICED_TOKENS:
            break
    return list(tokens.values())


def build_query(db: Any, cleaned: str) -> str:
    """Select rare query terms, preserving order on ties."""
    tokens = _distinct_tokens(cleaned)
    if not tokens:
        return ""

    try:
        total = db.execute(f"SELECT COUNT(*) c FROM chunks WHERE {SOURCE_FILTER}").fetchone()["c"]
    except Exception:  # noqa: BLE001
        total = 0
    if not total:
        return ""

    priced: list[tuple[int, int, str]] = []  # (df, position, token)
    for position, token in enumerate(tokens):
        try:
            hits = db.execute(
                f"SELECT COUNT(*) c FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
                (f'"{token}"',),
            ).fetchone()["c"]
        except Exception:  # noqa: BLE001, S112
            continue
        if hits:
            priced.append((int(hits), position, token))

    kept = [entry for entry in priced if entry[0] <= _RARE_TOKEN_MAX_DOCS or entry[0] / total < _COMMON_TOKEN_SHARE]
    if not kept:
        return ""

    kept.sort(key=lambda entry: (entry[0], entry[1]))
    chosen = sorted(kept[:_MAX_QUERY_TOKENS], key=lambda entry: entry[1])
    return " OR ".join(f'"{entry[2]}"' for entry in chosen)


def _query_idf(db: Any, fts_query: str) -> float:
    """Sum FTS5 phrase weights, including its floor for frequent phrases."""
    total = db.execute(f"SELECT COUNT(*) c FROM {FTS_TABLE}").fetchone()["c"]
    weight = 0.0
    for phrase in fts_query.split(" OR "):
        hits = db.execute(
            f"SELECT COUNT(*) c FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?", (phrase,)
        ).fetchone()["c"]
        # FTS5 clamps nonpositive IDF to 1e-6; use its phrase matching and corpus size.
        idf = math.log((total - hits + 0.5) / (hits + 0.5))
        weight += idf if idf > 0.0 else 1e-6
    return weight


def search_chunks(
    db: Any,
    cleaned: str,
    *,
    limit: int,
    min_score: float,
    score: Callable[[float], float] = rank_to_score,
) -> list[dict[str, Any]]:
    """Rank with BM25, then load text for the selected rows."""
    fts_query = build_query(db, cleaned)
    if not fts_query:
        logger.info("[SecondBrain] a %d-character query has no term distinctive enough to search on", len(cleaned))
        return []

    try:
        query_idf = _query_idf(db, fts_query)
        ranked = db.execute(
            f"SELECT rowid, rank FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, max(1, int(limit))),
        ).fetchall()

        scored: dict[int, float] = {}
        for row in ranked:
            value = score(float(row["rank"]) / query_idf)
            if value >= min_score:
                scored[int(row["rowid"])] = value
        if not scored:
            return []

        # Apply the source filter when reading rows. FTS has no source column.
        placeholders = ", ".join("?" * len(scored))
        rows = db.execute(
            f"SELECT rowid, id, path, source, start_line, end_line, text "
            f"FROM chunks WHERE rowid IN ({placeholders}) "
            f"AND ({SOURCE_FILTER})",
            tuple(scored),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SecondBrain] lexical search failed: %s", describe(exc))
        return []

    results = [hit_from_row(row, scored[int(row["rowid"])]) for row in rows]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def hit_from_row(row: Any, score: float) -> dict[str, Any]:
    """One search hit from a ``chunks`` row with its rowid."""
    return {
        "id": str(row["id"]),
        "path": str(row["path"]),
        "source": str(row["source"]),
        "startLine": int(row["start_line"]),
        "endLine": int(row["end_line"]),
        "snippet": str(row["text"]),
        "score": score,
    }


# ── an index with no host service ────────────────────────────────────────

INDEX_FILENAME = "lexical_index.db"

#: About 256 tokens.
_CHUNK_CHARS = 1000

#: Raised when the tables or indexed content change. An index file of an older version is rebuilt from the summaries.
SCHEMA_VERSION = 2

# A plain FTS5 table: it stores its text a second time, and needs no SQLite newer than FTS5 itself.
# A chunk's hash is the hash of its text, which is what its vector is stored under.
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, path TEXT, source TEXT, start_line INTEGER, end_line INTEGER, text TEXT, hash TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(text);
"""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(text: str) -> Iterator[tuple[int, int, str]]:
    """Split on line ends into ``(first line, last line, text)`` of about ``_CHUNK_CHARS`` each."""
    lines = text.split("\n")
    start = size = 0
    for number, line in enumerate(lines):
        if size and size + len(line) > _CHUNK_CHARS:
            yield start + 1, number, "\n".join(lines[start:number])
            start, size = number, 0
        size += len(line) + 1
    rest = "\n".join(lines[start:])
    if rest.strip():
        yield start + 1, len(lines), rest


class LexicalIndex:
    """The summaries of one corpus as chunks in one SQLite file beside them."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        # ponytail: one connection, used from the event loop as the host memory index is. A corpus
        # whose sync takes long enough to notice needs a worker thread and a connection of its own.
        self.db = sqlite3.connect(self.root / INDEX_FILENAME)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # Everything here is derived from the summaries, so an older layout is dropped and rebuilt.
            self.db.executescript(f"DROP TABLE IF EXISTS {FTS_TABLE}; DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS files;")
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.executescript(_SCHEMA)

    def sync(self) -> None:
        """Index each summary whose content changed, and drop each that is gone from disk."""
        known = {row["path"]: row["hash"] for row in self.db.execute("SELECT path, hash FROM files")}
        for file in sorted(summaries_dir(self.root).glob("*.md")):
            path = file.relative_to(self.root).as_posix()
            text = file.read_text(encoding="utf-8", errors="replace")
            digest = text_hash(text)
            if known.pop(path, None) != digest:
                self._remove(path)
                self._add(path, digest, text)
        for path in known:
            self._remove(path)
        self.db.commit()

    def _remove(self, path: str) -> None:
        self.db.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid IN (SELECT rowid FROM chunks WHERE path = ?)", (path,))
        self.db.execute("DELETE FROM chunks WHERE path = ?", (path,))
        self.db.execute("DELETE FROM files WHERE path = ?", (path,))

    def _add(self, path: str, digest: str, text: str) -> None:
        # The generated footer describes tool usage, not the source document.
        text = text.partition("\n## Drill-down\n")[0]
        for first, last, chunk in _chunks(text):
            row = self.db.execute(
                "INSERT INTO chunks (id, path, source, start_line, end_line, text, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"{path}:{first}:{last}", path, SOURCE, first, last, chunk, text_hash(chunk)),
            )
            self.db.execute(f"INSERT INTO {FTS_TABLE} (rowid, text) VALUES (?, ?)", (row.lastrowid, chunk))
        self.db.execute("INSERT INTO files (path, hash) VALUES (?, ?)", (path, digest))


__all__ = [
    "INDEX_FILENAME",
    "SCHEMA_VERSION",
    "LexicalIndex",
    "build_query",
    "hit_from_row",
    "rank_to_score",
    "search_chunks",
    "text_hash",
]
