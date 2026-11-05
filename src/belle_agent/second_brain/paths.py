"""Locate the second-brain corpus and name its documents.

The index requires summaries under ``<corpus>/memory/``.
"""

import logging
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from belle_agent.paths import resolve_paths

logger = logging.getLogger(__name__)

#: The wildcard entry in ``second_brain.ingest.channels``.
ALL_CHANNELS = "*"

#: The entry in ``second_brain.ingest.channels`` for every public conversation. Which
#: conversations are public is a platform's knowledge.
PUBLIC_CHANNELS = "public"

#: SHA-256 hex characters in a document id.
DOC_SHA_PREFIX_LENGTH = 8

DEFAULT_CORPUS_DIRNAME = "second_brain"

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_CHARS = 60


def config_section(config: dict[str, Any] | None, key: str) -> dict[str, Any]:
    section = config.get(key) if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def second_brain_config(config: dict[str, Any] | None) -> dict[str, Any]:
    section = config_section(config, "second_brain")
    if "ingest" not in section:
        section = {**section, "ingest": {"channels": [PUBLIC_CHANNELS]}}
    elif isinstance(section["ingest"], dict) and "channels" not in section["ingest"]:
        section = {**section, "ingest": {"channels": [PUBLIC_CHANNELS], **section["ingest"]}}
    return {"enabled": True, **section}


def parse_ingest_channels(
    raw: Any, *, warn: Callable[..., None] = logger.warning
) -> tuple[bool, bool, frozenset[str]]:
    """Return ``(all, public, ids)`` from the value of ``second_brain.ingest.channels``.

    An invalid value selects nothing. ``public`` combines with IDs; ``*`` takes precedence.
    """
    if raw is None:
        return False, False, frozenset()
    if isinstance(raw, str):  # one channel, or a keyword, written by hand
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        warn(
            "second_brain.ingest.channels is not a list (%r); ingesting from no"
            " conversation",
            raw,
        )
        return False, False, frozenset()
    ids = {str(entry).strip() for entry in raw if str(entry).strip()}
    if ALL_CHANNELS in ids:
        if len(ids) > 1:
            warn(
                "second_brain.ingest.channels names %r alongside %d other"
                " entries; the wildcard covers every conversation that is not end-to-end encrypted, and the"
                " other entries select nothing",
                ALL_CHANNELS,
                len(ids) - 1,
            )
        return True, False, frozenset()
    public = PUBLIC_CHANNELS in ids
    ids.discard(PUBLIC_CHANNELS)
    return False, public, frozenset(ids)


def resolve_second_brain_dir(config: dict[str, Any] | None = None, *, workspace_dir: Path | str | None = None) -> Path:
    """Use the configured corpus or Belle's data directory."""
    configured = str(second_brain_config(config).get("corpus_dir") or "").strip()
    if not configured:
        return resolve_paths().data_dir / DEFAULT_CORPUS_DIRNAME
    candidate = Path(configured).expanduser()
    if candidate.is_absolute():
        return candidate
    workspace = Path(workspace_dir) if workspace_dir is not None else Path.cwd()
    return workspace / candidate


def summaries_dir(root: Path) -> Path:
    """The indexed directory. Named ``memory`` because retrieval requires it."""
    return Path(root) / "memory"


def sources_dir(root: Path) -> Path:
    """Raw originals, kept verbatim and never indexed."""
    return Path(root) / "sources"


def extracted_dir(root: Path) -> Path:
    """Flattened text, addressed by byte offset for drill-down."""
    return Path(root) / "extracted"


def manifest_path(root: Path) -> Path:
    return Path(root) / "manifest.json"


def doc_slug(title: str, sha256: str) -> str:
    """Make a readable ``<slug>-<sha8>`` document id."""
    digest = str(sha256 or "").strip().lower()
    if not digest:
        raise ValueError("doc_slug requires a sha256")
    short = digest[:DOC_SHA_PREFIX_LENGTH]

    normalized = unicodedata.normalize("NFKD", str(title or ""))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP_RE.sub("-", ascii_only).strip("-")
    if len(slug) > _SLUG_MAX_CHARS:
        slug = slug[:_SLUG_MAX_CHARS].rstrip("-")
    return f"{slug or 'doc'}-{short}"


def summary_path(root: Path, doc_id: str) -> Path:
    return summaries_dir(root) / f"{doc_id}.md"


def extract_path(root: Path, doc_id: str) -> Path:
    return extracted_dir(root) / f"{doc_id}.txt"


def ensure_layout(root: Path) -> None:
    """Create the three directories the corpus needs, idempotently."""
    for directory in (summaries_dir(root), sources_dir(root), extracted_dir(root)):
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    "ALL_CHANNELS",
    "DEFAULT_CORPUS_DIRNAME",
    "DOC_SHA_PREFIX_LENGTH",
    "PUBLIC_CHANNELS",
    "doc_slug",
    "ensure_layout",
    "extract_path",
    "extracted_dir",
    "manifest_path",
    "parse_ingest_channels",
    "resolve_second_brain_dir",
    "second_brain_config",
    "sources_dir",
    "summaries_dir",
    "summary_path",
]
