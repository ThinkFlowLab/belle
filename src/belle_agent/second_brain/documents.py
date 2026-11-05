"""Read documents and web pages for the Second Brain."""

import asyncio
import csv
import io
import json
from collections.abc import Iterable
from pathlib import Path

import trafilatura
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook

from belle_agent.second_brain.backend import UnreadableDocument
from belle_agent.second_brain.failures import describe

_TEXT = frozenset({".md", ".markdown", ".txt", ".rst", ".yaml", ".yml"})
_SUPPORTED = ".pdf, .docx, .xlsx, .csv, .tsv, .json, .html, .htm and text files (.md, .markdown, .txt, .rst, .yaml, .yml)"


def extract_html(html: str) -> tuple[str, str]:
    """Return a page's title and main text."""
    title = ""
    try:
        metadata = trafilatura.extract_metadata(html)
        title = str(metadata.title or "").strip() if metadata else ""
        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    except Exception:  # noqa: BLE001
        return "", ""
    return title, text.strip()


async def parse_file(path: Path) -> str:
    """Parse a file that the core cannot read as PDF or plain text."""
    return await asyncio.to_thread(_parse_file, path)


def _parse_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _TEXT or suffix == ".pdf":
        return ""
    readers = {
        ".docx": _docx,
        ".xlsx": _xlsx,
        ".csv": lambda file: _delimited(file, ","),
        ".tsv": lambda file: _delimited(file, "\t"),
        ".json": _json,
        ".html": _html,
        ".htm": _html,
    }
    reader = readers.get(suffix)
    if reader is None:
        raise UnreadableDocument(f"{path.name}: Belle reads {_SUPPORTED}, not {suffix or 'a file without an extension'}")
    try:
        return reader(path)
    except UnreadableDocument:
        raise
    except UnicodeError as exc:
        raise UnreadableDocument(f"{path.name}: expected UTF-8 text") from exc
    except Exception as exc:
        raise UnreadableDocument(f"{path.name}: cannot read it as {suffix} ({describe(exc)})") from exc


def _read_utf8(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if "\x00" in text:
        raise UnreadableDocument(f"{path.name}: expected UTF-8 text without NUL bytes")
    return text


def _json(path: Path) -> str:
    text = _read_utf8(path)

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        normalized = json.dumps(json.loads(text, object_pairs_hook=unique_pairs), indent=2, ensure_ascii=False)
        normalized.encode("utf-8")
        return normalized
    except (ValueError, UnicodeError):
        return text


def _html(path: Path) -> str:
    return extract_html(_read_utf8(path))[1]


def _cell(value: object) -> str:
    return str(value if value is not None else "").replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _table(rows: list[list[object]], title: str = "") -> str:
    if not rows:
        return ""
    width = max(map(len, rows))
    headers = [
        str(rows[0][i]) if i < len(rows[0]) and rows[0][i] not in (None, "") else f"Column {i + 1}"
        for i in range(width)
    ]
    lines = [f"## {title}"] if title else []
    lines.extend(("| " + " | ".join(map(_cell, headers)) + " |", "| " + " | ".join("---" for _ in headers) + " |"))
    for row in rows[1:]:
        if any(value is not None and str(value).strip() for value in row):
            lines.append("| " + " | ".join(_cell(row[i] if i < len(row) else None) for i in range(width)) + " |")
    return "\n".join(lines)


def _delimited(path: Path, delimiter: str) -> str:
    source = _read_utf8(path)
    csv.field_size_limit(max(csv.field_size_limit(), len(source)))
    return _table([list(row) for row in csv.reader(io.StringIO(source, newline=""), delimiter=delimiter)])


def _with_formulas(
    cached_rows: Iterable[tuple[object, ...]], formula_rows: Iterable[tuple[object, ...]]
) -> list[list[object]]:
    return [
        [cached if cached is not None else formula for cached, formula in zip(row, raw, strict=True)]
        for row, raw in zip(cached_rows, formula_rows, strict=True)
    ]


def _xlsx(path: Path) -> str:
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        formulas = load_workbook(path, read_only=True, data_only=False)
        try:
            return "\n\n".join(
                table for sheet, raw_sheet in zip(book.worksheets, formulas.worksheets, strict=True)
                if (table := _table(_with_formulas(sheet.values, raw_sheet.values), sheet.title))
            )
        finally:
            formulas.close()
    finally:
        book.close()


def _docx(path: Path) -> str:
    doc = Document(str(path))
    parts: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            style = block.style.name if block.style else ""
            if style == "Title":
                text = f"# {text}"
            elif style.startswith("Heading ") and style[8:].isdigit():
                text = f"{'#' * (int(style[8:]) + 1)} {text}"
            parts.append(text)
        elif isinstance(block, Table):
            table = _table([[cell.text for cell in row.cells] for row in block.rows])
            if table:
                parts.append(table)
    return "\n".join(parts)
