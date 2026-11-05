"""Extract text and parser-derived byte offsets for brain_read."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from belle_agent.second_brain.backend import UnreadableDocument
from belle_agent.second_brain.failures import describe

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$", re.MULTILINE)

_PLAIN_TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".csv"})

# Keep short pages in the text but omit their empty drill-down regions.
_MIN_PAGE_CHARS_FOR_OUTLINE = 40


@dataclass(frozen=True)
class Block:
    """Addressable region with UTF-8 byte offsets."""

    kind: str  # "page" | "heading"
    label: str  # "p.12" | "## Method"
    start: int
    end: int


async def extract_text(
    path: Path | str, mime: str = "", *, parse_file: Callable[[Path], Awaitable[str]] | None = None
) -> tuple[str, list[Block]]:
    """Extract text and blocks. Return an empty result for unreadable files."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    mimetype = str(mime or "").strip().lower()

    if suffix == ".pdf" or mimetype == "application/pdf":
        text, blocks = await asyncio.to_thread(_extract_pdf_sync, file_path)
        # Ignore a PDF with no text layer. The parser fallback writes cropped
        # page images into the working directory.
        return (text, blocks) if text.strip() else ("", [])

    text = await parse_file(file_path) if parse_file else ""
    if not text.strip() and suffix in _PLAIN_TEXT_SUFFIXES:
        text = await asyncio.to_thread(_read_text_sync, file_path)

    if not text.strip():
        return "", []
    return text, _heading_blocks(text)


def _read_text_sync(file_path: Path) -> str:
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise UnreadableDocument(f"{file_path.name}: expected UTF-8 text") from exc
    except OSError as exc:
        logger.warning("[SecondBrain] cannot read %s: %s", file_path.name, exc)
        return ""
    if "\x00" in text:
        raise UnreadableDocument(f"{file_path.name}: expected UTF-8 text without NUL bytes")
    return text


def _extract_pdf_sync(file_path: Path) -> tuple[str, list[Block]]:
    """Extract PDF pages and byte offsets on the caller's worker thread."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("[SecondBrain] pdfplumber unavailable; cannot read %s", file_path.name)
        return "", []

    parts: list[str] = []
    blocks: list[Block] = []
    cursor = 0
    try:
        with pdfplumber.open(str(file_path)) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                try:
                    page_text = (page.extract_text() or "").strip()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[SecondBrain] page %d of %s unreadable: %s", index, file_path.name, describe(exc))
                    page_text = ""
                # The separator is part of the text, so it is part of the offsets.
                piece = f"{page_text}\n\n" if page_text else "\n"
                piece_bytes = len(piece.encode("utf-8"))
                if len(page_text) >= _MIN_PAGE_CHARS_FOR_OUTLINE:
                    blocks.append(Block("page", f"p.{index}", cursor, cursor + piece_bytes))
                parts.append(piece)
                cursor += piece_bytes
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SecondBrain] pdfplumber failed on %s: %s", file_path.name, describe(exc))
        return "", []

    return "".join(parts), blocks


def _heading_blocks(text: str) -> list[Block]:
    """Make each heading's region end at the next heading."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return []
    offsets = _byte_offsets(text, [m.start() for m in matches] + [len(text)])
    return [
        Block("heading", f"{m.group(1)} {m.group(2).strip()}", offsets[i], offsets[i + 1])
        for i, m in enumerate(matches)
    ]


def _byte_offsets(text: str, char_offsets: list[int]) -> list[int]:
    """Convert character indices to UTF-8 byte offsets in one pass."""
    result: list[int] = []
    encoded = 0
    previous = 0
    for offset in char_offsets:
        encoded += len(text[previous:offset].encode("utf-8"))
        result.append(encoded)
        previous = offset
    return result


__all__ = ["Block", "extract_text"]
