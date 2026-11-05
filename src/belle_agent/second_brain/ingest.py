"""Ingest bytes or a URL into an indexed summary."""

import asyncio
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from belle_agent.config import model_entry
from belle_agent.second_brain.backend import SecondBrainBackend, UnreadableDocument
from belle_agent.second_brain.corpus_index import refresh_index
from belle_agent.second_brain.extract import extract_text
from belle_agent.second_brain.index import (
    get_second_brain_manager,
)
from belle_agent.second_brain.links import fetch_link
from belle_agent.second_brain.loop_bound import LoopBound
from belle_agent.second_brain.paths import (
    DOC_SHA_PREFIX_LENGTH,
    config_section,
    doc_slug,
    resolve_second_brain_dir,
    second_brain_config,
    sources_dir,
)
from belle_agent.second_brain.store import (
    STATUS_FILTERED as DOC_STATUS_FILTERED,
)
from belle_agent.second_brain.store import (
    BrainCorpus,
    BrainDoc,
)
from belle_agent.second_brain.summarize import summarize_document

logger = logging.getLogger(__name__)

STATUS_INGESTED = "ingested"
STATUS_DUPLICATE = "skipped_duplicate"
STATUS_FILTERED = "skipped_filtered"

_DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
_UNSAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Maximum concurrent link fetches.
_FETCH_CONCURRENCY = 2

#: Maximum concurrent summaries.
_DEFAULT_SUMMARY_CONCURRENCY = 4
_SUMMARY_CONCURRENCY_ENV = "SECOND_BRAIN_SUMMARY_CONCURRENCY"


def _summary_concurrency() -> int:
    raw = str(os.environ.get(_SUMMARY_CONCURRENCY_ENV, "") or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_SUMMARY_CONCURRENCY
    except ValueError:
        logger.warning(
            "[SecondBrain] %s=%r is not an integer; using %d",
            _SUMMARY_CONCURRENCY_ENV,
            raw,
            _DEFAULT_SUMMARY_CONCURRENCY,
        )
        return _DEFAULT_SUMMARY_CONCURRENCY
    if value < 1:
        logger.warning("[SecondBrain] %s=%d is below 1; using 1", _SUMMARY_CONCURRENCY_ENV, value)
        return 1
    return value


#: Commit locks are per corpus. Fetch and summary gates are process-wide.
_COMMIT_LOCKS: LoopBound[asyncio.Lock] = LoopBound(asyncio.Lock)
_FETCH_GATE: LoopBound[asyncio.Semaphore] = LoopBound(lambda: asyncio.Semaphore(_FETCH_CONCURRENCY))
_SUMMARY_GATE: LoopBound[asyncio.Semaphore] = LoopBound(lambda: asyncio.Semaphore(_summary_concurrency()))


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class _Prepared:
    doc: BrainDoc
    markdown: str


@dataclass
class _Rejected:
    reason: str
    remember: bool
    source_path: Path | None = None


class SecondBrainIngestService:
    """Ingest documents into one corpus."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        corpus_dir: Path | str | None = None,
        backend: SecondBrainBackend | None = None,
    ) -> None:
        self._config = config or {}
        self._backend = backend
        self._root = Path(corpus_dir) if corpus_dir else resolve_second_brain_dir(config)
        self._corpus = BrainCorpus(self._root)
        self._summary_settings = self._summarizer_settings("model")
        self._outline_settings = (
            self._summarizer_settings("outline_model")
            if config_section(self._ingest_config(), "summarizer").get("outline_model")
            else None
        )

    @property
    def corpus(self) -> BrainCorpus:
        return self._corpus

    async def ingest_bytes(
        self,
        content: bytes,
        filename: str,
        mime: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not content:
            return self._skip(STATUS_FILTERED, f"{filename} is empty")

        sha256 = BrainCorpus.sha256_of_bytes(content)
        stem = Path(str(filename or "")).stem.strip()
        title = re.sub(r"[_-]+", " ", stem).strip() or "Untitled document"
        max_bytes = self._max_file_bytes()
        if len(content) > max_bytes:
            # Oversize is a byte-stable rejection.
            return await self._commit(
                sha256=sha256,
                title=title,
                prepared=_Rejected(f"{filename} is over the {max_bytes}-byte limit", remember=True),
            )

        return await self._ingest_document(
            sha256=sha256,
            content=content,
            filename=filename,
            mime=mime or mimetypes.guess_type(filename)[0] or "application/octet-stream",
            title=title,
            meta=meta or {},
            source_kind="file",
        )

    async def ingest_path(
        self,
        path: Path | str,
        mime: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ingest a previously downloaded file."""
        file_path = Path(path)
        if not file_path.is_file():
            return self._skip(STATUS_FILTERED, f"{file_path} is not a file")

        # Check size before loading the file.
        max_bytes = self._max_file_bytes()
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            return self._skip(STATUS_FILTERED, f"cannot read {file_path.name}: {exc}")
        if size > max_bytes:
            return self._skip(STATUS_FILTERED, f"{file_path.name} is over the {max_bytes}-byte limit")

        try:
            content = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:
            return self._skip(STATUS_FILTERED, f"cannot read {file_path.name}: {exc}")
        return await self.ingest_bytes(content, file_path.name, mime, meta)

    async def ingest_link(self, url: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch a URL and ingest it if it turns out to be a document."""
        link_config = config_section(self._ingest_config(), "link")
        if link_config.get("enabled") is False:
            return self._skip(STATUS_FILTERED, "link ingestion is disabled")

        # The fetch gate does not cover summarisation.
        async with _FETCH_GATE.get():
            try:
                fetched = await fetch_link(
                    url, cfg=link_config, extract_html=self._backend.extract_html if self._backend else None
                )
            except ValueError as exc:
                return self._skip(STATUS_FILTERED, str(exc))
            if fetched is None:
                return self._skip(STATUS_FILTERED, f"{url} is not a document")

        content = fetched.content if fetched.kind == "pdf" else fetched.text.encode("utf-8")
        sha256 = BrainCorpus.sha256_of_bytes(content)
        enriched = dict(meta or {})
        enriched["origin_url"] = fetched.url
        return await self._ingest_document(
            sha256=sha256,
            content=content,
            filename=fetched.filename,
            mime=fetched.mime_type,
            title=fetched.title or fetched.url,
            meta=enriched,
            source_kind="link",
        )

    async def _lookup_fresh(self, sha256: str) -> BrainDoc | None:
        """Recheck the manifest under the commit lock."""

        def _read() -> BrainDoc | None:
            self._corpus.load()
            return self._corpus.find_by_sha256(sha256)

        return await asyncio.to_thread(_read)

    async def _ingest_document(
        self,
        *,
        sha256: str,
        content: bytes,
        filename: str,
        mime: str,
        title: str,
        meta: dict[str, Any],
        source_kind: str,
    ) -> dict[str, Any]:
        """Stamp submission order, prepare, then commit under the corpus lock."""
        seen = self._corpus.find_by_sha256(sha256)
        if seen is not None:
            return self._seen_before(seen)
        ingested_at = _utc_stamp()
        async with _SUMMARY_GATE.get():
            prepared = await self._prepare_document(
                sha256=sha256,
                content=content,
                filename=filename,
                mime=mime,
                title=title,
                meta=meta,
                source_kind=source_kind,
                ingested_at=ingested_at,
            )
        return await self._commit(sha256=sha256, title=title, prepared=prepared)

    async def _commit(
        self,
        *,
        sha256: str,
        title: str,
        prepared: _Prepared | _Rejected,
    ) -> dict[str, Any]:
        """Check and write atomically to deduplicate concurrent arrivals."""
        async with _COMMIT_LOCKS.get(str(self._root)):
            existing = await self._lookup_fresh(sha256)
            if existing is not None:
                if isinstance(prepared, _Prepared) and prepared.doc.doc_id != existing.doc_id:
                    # Discard an unlisted extract from the losing preparation.
                    await asyncio.to_thread(Path(prepared.doc.extract_path).unlink, missing_ok=True)
                return self._seen_before(existing)

            if isinstance(prepared, _Rejected):
                if prepared.remember:
                    await self._remember_rejection(
                        sha256,
                        title,
                        prepared.reason,
                        source_path=prepared.source_path,
                    )
                return self._skip(STATUS_FILTERED, prepared.reason)

            summary_file = Path(prepared.doc.summary_path)
            await asyncio.to_thread(summary_file.write_text, prepared.markdown, encoding="utf-8")
            await asyncio.to_thread(self._corpus.put, prepared.doc)
            await self._reindex(prepared.doc.doc_id)

        logger.info(
            "[SecondBrain] ingested %s (%s, %d bytes, %d pages)",
            prepared.doc.doc_id,
            prepared.doc.source_kind,
            prepared.doc.bytes,
            prepared.doc.pages,
        )
        return {"status": STATUS_INGESTED, "doc_id": prepared.doc.doc_id, "title": title}

    async def _prepare_document(
        self,
        *,
        sha256: str,
        content: bytes,
        filename: str,
        mime: str,
        title: str,
        meta: dict[str, Any],
        source_kind: str,
        ingested_at: str,
    ) -> _Prepared | _Rejected:
        self._corpus.ensure_layout()
        doc_id = doc_slug(title, sha256)

        safe_name = self._safe_component(filename, f"document-{sha256[:8]}")
        source_path = sources_dir(self._root) / f"{sha256[:DOC_SHA_PREFIX_LENGTH]}_{safe_name}"
        try:
            await asyncio.to_thread(source_path.write_bytes, content)
        except OSError as exc:
            logger.warning("[SecondBrain] cannot write source for %s: %s", doc_id, exc)
            return _Rejected(f"cannot store {filename}: {exc}", remember=False)

        try:
            text, blocks = await extract_text(
                source_path, mime, parse_file=self._backend.parse_file if self._backend else None
            )
        except UnreadableDocument as exc:
            return _Rejected(str(exc), remember=True, source_path=source_path)
        if not text.strip():
            # Keep the raw copy for inspection.
            return _Rejected(
                f"no readable text could be extracted from {filename}",
                remember=True,
                source_path=source_path,
            )

        extract_file = self._corpus.extract_path_for(doc_id)
        await asyncio.to_thread(extract_file.write_text, text, encoding="utf-8", errors="replace")

        pages = sum(1 for block in blocks if block.kind == "page")
        origin = {
            key: str(meta.get(key) or "")
            for key in ("origin_url", "slack_permalink", "slack_channel", "poster", "posted_at")
        }
        frontmatter = {
            "doc_id": doc_id,
            "title": title,
            "source_kind": source_kind,
            "source_path": self._relative(source_path),
            "extract_path": self._relative(extract_file),
            **origin,
            "ingested_at": ingested_at,
            "sha256": sha256,
            "pages": str(pages),
        }

        markdown, rows = await summarize_document(
            text=text,
            blocks=blocks,
            doc_id=doc_id,
            title=title,
            frontmatter=frontmatter,
            context_note=self._context_note(meta),
            cfg=self._summary_settings,
            model_factory=self._backend.build_model if self._backend else None,
            outline_cfg=self._outline_settings,
        )

        summary_file = self._corpus.summary_path_for(doc_id)
        doc = BrainDoc(
            doc_id=doc_id,
            sha256=sha256,
            title=title,
            source_kind=source_kind,
            source_path=str(source_path),
            extract_path=str(extract_file),
            summary_path=str(summary_file),
            **origin,
            ingested_at=ingested_at,
            bytes=len(content),
            pages=pages,
            outline=rows,
        )
        return _Prepared(doc=doc, markdown=markdown)

    def _summarizer_settings(self, selector: str) -> dict[str, Any]:
        """Merge the summarizer settings with the entry that ``selector`` names, or name a host model."""
        settings = config_section(self._ingest_config(), "summarizer")
        entry = model_entry(self._config, "summarizer", settings.get(selector) or "")
        if entry:
            return {**settings, **entry, "model": entry.get("model_name") or ""}
        return {**settings, "model": settings.get(selector) or ""}

    async def _reindex(self, doc_id: str) -> None:
        """Index the summary before returning to the poster."""
        manager = await get_second_brain_manager(self._config, corpus_dir=self._root, backend=self._backend)
        if manager is None or not await refresh_index(manager):
            logger.warning("[SecondBrain] %s written but not indexed", doc_id)

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._root))
        except ValueError:
            return str(path)

    @staticmethod
    def _context_note(meta: dict[str, Any]) -> str:
        poster = str(meta.get("poster") or "").strip()
        channel = str(meta.get("slack_channel_name") or meta.get("slack_channel") or "").strip()
        comment = str(meta.get("comment") or "").strip()
        if not poster and not channel and not comment:
            return ""
        who = poster or "Someone"
        where = f" in {channel}" if channel else ""
        said = f': "{comment}"' if comment else "."
        return f"Posted by {who}{where}{said}"

    def _ingest_config(self) -> dict[str, Any]:
        return config_section(second_brain_config(self._config), "ingest")

    def _max_file_bytes(self) -> int:
        return int(self._ingest_config().get("max_file_bytes") or _DEFAULT_MAX_FILE_BYTES)

    @staticmethod
    def _safe_component(value: str, fallback: str) -> str:
        """Sanitize one filename component."""
        name = Path(str(value or "")).name.strip()
        if not name or name in {".", ".."}:
            name = fallback
        return _UNSAFE_PATH_CHARS_RE.sub("_", name)[:180] or fallback

    async def _remember_rejection(
        self,
        sha256: str,
        title: str,
        reason: str,
        *,
        source_path: Path | None = None,
    ) -> None:
        """Persist a byte-stable rejection off the event loop."""
        doc = BrainDoc(
            doc_id=doc_slug(title, sha256),
            sha256=sha256,
            title=title,
            source_kind="file",
            source_path=self._relative(source_path) if source_path else "",
            ingested_at=_utc_stamp(),
            status=DOC_STATUS_FILTERED,
            skip_reason=reason,
        )

        def _write() -> None:
            self._corpus.ensure_layout()
            self._corpus.put(doc)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SecondBrain] could not record rejection: %s", exc)

    @staticmethod
    def _skip(status: str, reason: str) -> dict[str, Any]:
        logger.info("[SecondBrain] %s: %s", status, reason)
        return {"status": status, "reason": reason, "doc_id": "", "title": ""}

    @staticmethod
    def _seen_before(doc: BrainDoc) -> dict[str, Any]:
        """Report a prior rejection without calling it a duplicate."""
        if not doc.is_ingested:
            return {
                "status": STATUS_FILTERED,
                "reason": doc.skip_reason or "previously rejected",
                "doc_id": "",
                "title": doc.title,
            }
        return {"status": STATUS_DUPLICATE, "doc_id": doc.doc_id, "title": doc.title}


__all__ = [
    "STATUS_DUPLICATE",
    "STATUS_FILTERED",
    "STATUS_INGESTED",
    "SecondBrainIngestService",
]
