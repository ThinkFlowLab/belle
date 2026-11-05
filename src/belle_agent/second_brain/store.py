"""Store seen documents in an atomic JSON manifest keyed by SHA-256."""

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from heapq import nlargest
from pathlib import Path
from typing import Any

import portalocker

from belle_agent.second_brain.paths import (
    ensure_layout,
    extract_path,
    manifest_path,
    summary_path,
)

logger = logging.getLogger(__name__)

STATUS_OK = "ingested"
STATUS_FILTERED = "filtered"

_MANIFEST_LOCK_TIMEOUT_SEC = 10.0


@dataclass
class BrainDoc:
    """One source seen by the corpus, including rejected sources."""

    doc_id: str
    sha256: str
    title: str
    source_kind: str = "file"
    source_path: str = ""
    extract_path: str = ""
    summary_path: str = ""
    origin_url: str = ""
    slack_permalink: str = ""
    slack_channel: str = ""
    poster: str = ""
    posted_at: str = ""
    ingested_at: str = ""
    bytes: int = 0
    pages: int = 0
    outline: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_OK
    skip_reason: str = ""

    @property
    def is_ingested(self) -> bool:
        return self.status == STATUS_OK

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "BrainDoc":
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


class BrainCorpus:
    """Manifest and files for indexed documents."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._docs: dict[str, BrainDoc] = {}
        self._loaded = False

    def ensure_layout(self) -> None:
        ensure_layout(self.root)

    def summary_path_for(self, doc_id: str) -> Path:
        return summary_path(self.root, doc_id)

    def extract_path_for(self, doc_id: str) -> Path:
        return extract_path(self.root, doc_id)

    @staticmethod
    def sha256_of_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def load(self) -> None:
        """Treat an unreadable manifest as empty and log the error."""
        self._docs = {}
        self._loaded = True
        path = manifest_path(self.root)
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("[SecondBrain] manifest unreadable, starting empty: %s", exc)
            return
        entries = raw.get("documents") if isinstance(raw, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                doc = BrainDoc.from_json(entry)
            except TypeError:
                continue
            if doc.sha256:
                self._docs[doc.sha256] = doc

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _save_locked(self) -> None:
        """Replace the manifest atomically. The caller holds the manifest lock."""
        target = manifest_path(self.root)
        payload = {
            "version": 1,
            "documents": [doc.to_json() for doc in self._docs.values()],
        }
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - the cleanup below needs its name
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=".manifest-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, target)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def find_by_sha256(self, sha256: str) -> BrainDoc | None:
        self._ensure_loaded()
        return self._docs.get(str(sha256 or "").strip().lower())

    def get(self, doc_id: str) -> BrainDoc | None:
        """Find one usable document by ID."""
        self._ensure_loaded()
        wanted = str(doc_id or "").strip()
        if not wanted:
            return None
        for doc in self._docs.values():
            if doc.doc_id == wanted and doc.is_ingested:
                return doc
        return None

    def put(self, doc: BrainDoc) -> None:
        """Add one document to the manifest as it is on disk now.

        Another process may share the corpus, so the reload and the write
        happen under one file lock. Writing this instance's older copy would
        drop whatever the other process committed since.
        """
        target = manifest_path(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(target.name + ".lock")
        with portalocker.Lock(str(lock_path), timeout=_MANIFEST_LOCK_TIMEOUT_SEC):
            self.load()
            self._docs[doc.sha256] = doc
            self._save_locked()

    def list(self, limit: int | None = None, *, include_filtered: bool = False) -> list[BrainDoc]:
        """List documents by ingestion time, excluding rejected entries by default."""
        self._ensure_loaded()
        docs = (d for d in self._docs.values() if include_filtered or d.is_ingested)

        def key(doc: BrainDoc) -> tuple[str, str]:
            return doc.ingested_at or "", doc.doc_id

        if limit is not None and limit >= 0:
            return nlargest(limit, docs, key=key)
        return sorted(docs, key=key, reverse=True)

    def __len__(self) -> int:
        """Count usable documents."""
        self._ensure_loaded()
        return sum(1 for doc in self._docs.values() if doc.is_ingested)


__all__ = ["STATUS_FILTERED", "STATUS_OK", "BrainCorpus", "BrainDoc"]
