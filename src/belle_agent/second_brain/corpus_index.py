"""The corpus index: the lexical index, with a vector for each chunk where an embedder is configured.

A search ranks lexically, or mixes the lexical score of each chunk with the
cosine of its vector and the query's. Vectors are stored by model and text
hash, so a chunk whose text did not change keeps its vector across syncs and
restarts, and a change of model embeds the corpus again.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from belle_agent.second_brain.backend import Embedder
from belle_agent.second_brain.failures import describe
from belle_agent.second_brain.lexical import SOURCE_FILTER, LexicalIndex, hit_from_row, search_chunks
from belle_agent.second_brain.loop_bound import LoopBound
from belle_agent.second_brain.paths import ensure_layout

logger = logging.getLogger(__name__)

DEFAULT_VECTOR_WEIGHT = 0.7
DEFAULT_TEXT_WEIGHT = 0.3
#: Each ranking brings this many times the rows asked for to the merge.
CANDIDATE_MULTIPLIER = 3
#: Texts per call to the embedder. Each batch is committed, so an interrupted backfill keeps what it embedded.
EMBED_BATCH = 64

_VECTOR_SCHEMA = "CREATE TABLE IF NOT EXISTS embeddings (model TEXT, hash TEXT, vec BLOB, PRIMARY KEY (model, hash));"

#: One sync of a corpus at a time: ingest and the first search can both start one.
_LOCKS: LoopBound[asyncio.Lock] = LoopBound(asyncio.Lock)
_OPEN: dict[Path, "CorpusIndex"] = {}


class CorpusIndex(LexicalIndex):
    def __init__(
        self,
        root: Path,
        embedder: Embedder | None = None,
        *,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
        text_weight: float = DEFAULT_TEXT_WEIGHT,
    ) -> None:
        super().__init__(root)
        self.db.executescript(_VECTOR_SCHEMA)
        self.embedder = embedder
        self.vector_weight = vector_weight
        self.text_weight = text_weight
        self._rowids: list[int] = []
        self._vector_ids: set[str] = set()
        self._matrix: np.ndarray | None = None

    async def refresh(self) -> None:
        """Index each changed summary, then embed each chunk with no vector. Raises what the lexical sync raises."""
        async with _LOCKS.get(self.root):
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        self.sync()
        if self.embedder is not None:
            await self._embed_missing(self.embedder)
        self._matrix = None

    async def _embed_missing(self, embedder: Embedder) -> None:
        self.db.execute(
            "DELETE FROM embeddings WHERE model != ? OR hash NOT IN (SELECT hash FROM chunks)", (embedder.model,)
        )
        self.db.commit()
        # Newest first, so a summary just ingested is searchable by meaning before an old backfill ends.
        missing = [
            (row["hash"], row["text"])
            for row in self.db.execute(
                "SELECT c.hash, c.text FROM chunks c LEFT JOIN embeddings e ON e.hash = c.hash AND e.model = ?"
                " WHERE e.hash IS NULL GROUP BY c.hash ORDER BY MAX(c.rowid) DESC",
                (embedder.model,),
            )
            if row["text"].strip()
        ]
        started, embedded = time.monotonic(), 0
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start : start + EMBED_BATCH]
            try:
                vectors = await embedder.embed_documents([text for _, text in batch])
            except Exception as exc:  # noqa: BLE001 - the lexical index stays usable, and the next sync tries again
                logger.warning(
                    "[SecondBrain] embedding failed with %d chunk(s) left without a vector: %s",
                    len(missing) - embedded,
                    describe(exc),
                )
                break
            if len(vectors) != len(batch):
                logger.warning("[SecondBrain] the embedder returned %d vector(s) for %d chunk(s)", len(vectors), len(batch))
                break
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings (model, hash, vec) VALUES (?, ?, ?)",
                [(embedder.model, digest, np.asarray(vector, dtype=np.float32).tobytes()) for (digest, _), vector in zip(batch, vectors)],
            )
            self.db.commit()
            embedded += len(batch)
        if embedded:
            logger.info(
                "[SecondBrain] embedded %d chunk(s) with %s in %.1fs", embedded, embedder.model, time.monotonic() - started
            )

    def _vectors(self) -> tuple[list[int], np.ndarray]:
        """The chunks with a vector, and their vectors as unit rows."""
        if self._matrix is None:
            rows = self.db.execute(
                f"SELECT c.rowid, c.id, e.vec FROM chunks c JOIN embeddings e ON e.hash = c.hash AND e.model = ?"
                f" WHERE c.{SOURCE_FILTER} ORDER BY c.rowid",
                (self.embedder.model if self.embedder else "",),
            ).fetchall()
            vectors = [(row["rowid"], row["id"], np.frombuffer(row["vec"], dtype=np.float32)) for row in rows]
            dims = len(vectors[0][2]) if vectors else 0
            vectors = [(rowid, chunk_id, vector) for rowid, chunk_id, vector in vectors if len(vector) == dims]
            self._rowids = [rowid for rowid, _, _ in vectors]
            self._vector_ids = {chunk_id for _, chunk_id, _ in vectors}
            matrix = np.vstack([vector for _, _, vector in vectors]) if vectors else np.zeros((0, 0), dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            self._matrix = matrix / np.where(norms == 0, 1.0, norms)
        return self._rowids, self._matrix

    def _vector_hits(self, query_vector: list[float], limit: int) -> list[dict[str, Any]]:
        rowids, matrix = self._vectors()
        query = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if not rowids or query.shape != (matrix.shape[1],) or norm == 0.0:
            return []
        scores = matrix @ (query / norm)
        picked = {rowids[i]: float(scores[i]) for i in np.argsort(-scores)[:limit]}
        rows = self.db.execute(
            f"SELECT rowid, id, path, source, start_line, end_line, text FROM chunks WHERE rowid IN ({','.join('?' * len(picked))})",
            tuple(picked),
        ).fetchall()
        return [hit_from_row(row, picked[row["rowid"]]) for row in rows]

    async def search(self, query: str, *, limit: int, min_score: float, semantic: bool) -> list[dict[str, Any]]:
        """Rank chunks for one query. Lexical alone without an embedder, without vectors, or with ``semantic`` off."""
        cleaned = (query or "").strip()
        if not cleaned:
            return []
        if not (semantic and self.embedder is not None):
            return search_chunks(self.db, cleaned, limit=limit, min_score=min_score)
        try:
            query_vector = await self.embedder.embed_query(cleaned)
        except Exception as exc:  # noqa: BLE001 - the turn still gets the lexical ranking
            logger.warning("[SecondBrain] query embedding failed, ranking lexically: %s", describe(exc))
            return search_chunks(self.db, cleaned, limit=limit, min_score=min_score)
        pool = max(1, limit) * CANDIDATE_MULTIPLIER
        vector_hits = self._vector_hits(query_vector, pool)
        if not vector_hits:
            return search_chunks(self.db, cleaned, limit=limit, min_score=min_score)
        merged = {hit["id"]: {**hit, "score": self.vector_weight * hit["score"]} for hit in vector_hits}
        for hit in search_chunks(self.db, cleaned, limit=pool, min_score=0.0):
            weight = self.text_weight if hit["id"] in self._vector_ids else 1.0
            merged.setdefault(hit["id"], {**hit, "score": 0.0})["score"] += weight * hit["score"]
        ranked = sorted((hit for hit in merged.values() if hit["score"] >= min_score), key=lambda hit: hit["score"], reverse=True)
        return ranked[:limit]


async def open_index(
    root: Path | str,
    *,
    embedder_factory: Callable[[], Embedder | None] = lambda: None,
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    text_weight: float = DEFAULT_TEXT_WEIGHT,
) -> CorpusIndex | None:
    """Open a corpus index once per process and bring it up to date. ``None`` if it cannot be opened.

    The first open embeds every chunk that has no vector yet, so on a large
    corpus that was never embedded it takes as long as the embedder does.
    """
    key = Path(root).resolve()
    async with _LOCKS.get(key):
        index = _OPEN.get(key)
        if index is None:
            try:
                embedder = embedder_factory()
            except Exception as exc:  # noqa: BLE001 - no embedder means a lexical index, not none
                logger.warning("[SecondBrain] no embedder, ranking lexically: %s", exc)
                embedder = None
            try:
                ensure_layout(key)
                index = CorpusIndex(key, embedder, vector_weight=vector_weight, text_weight=text_weight)
                await index._refresh_locked()
            except (OSError, sqlite3.Error) as exc:
                logger.warning("[SecondBrain] no corpus index at %s: %s", key, exc)
                return None
            _OPEN[key] = index
    return index


async def refresh_index(index: CorpusIndex) -> bool:
    """Index and embed what changed since the last sync. False if the lexical sync failed."""
    try:
        await index.refresh()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("[SecondBrain] corpus index sync failed: %s", exc)
        return False
    return True


__all__ = [
    "CANDIDATE_MULTIPLIER",
    "DEFAULT_TEXT_WEIGHT",
    "DEFAULT_VECTOR_WEIGHT",
    "CorpusIndex",
    "open_index",
    "refresh_index",
]
