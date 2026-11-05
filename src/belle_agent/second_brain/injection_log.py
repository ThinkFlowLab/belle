"""Record recall attempts and the last injected text under the debug flag."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_FLAG = "SECOND_BRAIN_DEBUG_INJECTION"
SNAPSHOT_NAME = "last_injected.md"
LOG_NAME = "injections.jsonl"

OUTCOME_INJECTED = "injected"  # documents reached the model
OUTCOME_NO_INDEX = "no_index"  # the rail has no manager yet
OUTCOME_SEARCH_FAILED = "search_failed"
OUTCOME_NO_MATCH = "no_match"  # nothing scored above min_score
OUTCOME_NO_DOC_IDS = "no_doc_ids"  # hits whose path named no document
OUTCOME_NO_SUMMARY = "no_summary"  # documents matched, no summary on disk

_SEQUENCE = count(1)


def enabled() -> bool:
    """Treat any nonempty value except an explicit off switch as enabled."""
    raw = str(os.getenv(ENV_FLAG) or "").strip().lower()
    return bool(raw) and raw not in {"0", "false", "no", "off"}


@dataclass
class RecallRecord:
    """Describe one recall attempt for optional debug logging."""

    query: str
    top_k: int
    min_score: float
    outcome: str = OUTCOME_NO_INDEX
    #: All search hits, before deduplication and the top_k limit.
    hits: list[dict[str, Any]] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    #: Documents included within the prompt byte budget.
    injected_doc_ids: list[str] = field(default_factory=list)
    injected_bytes: int = 0
    error: str = ""
    #: Snapshot text, excluded from the JSONL log.
    text: str = ""

    def note_hits(self, results: list[dict[str, Any]], doc_id_of) -> None:
        for rank, result in enumerate(results, start=1):
            path = str(result.get("path", ""))
            self.hits.append(
                {
                    "rank": rank,
                    "doc_id": doc_id_of(path),
                    "path": Path(path).name,
                    "score": round(float(result.get("score", 0.0)), 4),
                }
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seq": next(_SEQUENCE),
            "outcome": self.outcome,
            "query": self.query,
            "top_k": self.top_k,
            "min_score": self.min_score,
            "doc_ids": list(self.doc_ids),
            "injected_doc_ids": list(self.injected_doc_ids),
            "injected_bytes": self.injected_bytes,
            "hits": list(self.hits),
            **({"error": self.error} if self.error else {}),
        }


async def write(corpus_dir: Path | str, record: RecallRecord) -> None:
    """Append the record and refresh the snapshot, off the loop. Never raises."""
    if not enabled():
        return
    try:
        await asyncio.to_thread(_write_sync, Path(corpus_dir), record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SecondBrain] could not record the injection: %s", exc)


def _write_sync(root: Path, record: RecallRecord) -> None:
    """Append one JSONL record and update the best-effort snapshot."""
    root.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record.to_json(), ensure_ascii=False)
    with (root / LOG_NAME).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    header = [
        f"<!-- {record.outcome} · {len(record.injected_doc_ids)} document(s) · {record.injected_bytes} bytes -->",
        "",
        f"**Query:** {record.query}",
        "",
        f"**Documents:** {', '.join(record.injected_doc_ids) or 'none'}",
        "",
        "---",
        "",
    ]
    body = record.text or "_Nothing was injected on this turn._"
    (root / SNAPSHOT_NAME).write_text("\n".join(header) + body + "\n", encoding="utf-8")


__all__ = [
    "ENV_FLAG",
    "LOG_NAME",
    "OUTCOME_INJECTED",
    "OUTCOME_NO_DOC_IDS",
    "OUTCOME_NO_INDEX",
    "OUTCOME_NO_MATCH",
    "OUTCOME_NO_SUMMARY",
    "OUTCOME_SEARCH_FAILED",
    "SNAPSHOT_NAME",
    "RecallRecord",
    "enabled",
    "write",
]
