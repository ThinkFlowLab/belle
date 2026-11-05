"""Rendering contracts and size-bounded Markdown splitting."""

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar

from markdown_it import MarkdownIt

from belle_agent.text_split import split_index

# Treat raw HTML as text when finding Markdown block boundaries.
_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

P = TypeVar("P")


@dataclass(frozen=True)
class RenderedPart(Generic[P]):
    """One message's source Markdown and platform payload."""

    markdown: str
    payload: P


class Renderer(Protocol[P]):
    """Render Markdown for a platform using the current message budget."""

    def measure(self, markdown: str) -> int:
        """Measure one message in the platform's unit."""

    def parts(self, markdown: str, budget: int) -> list[RenderedPart[P]]:
        """Split Markdown into messages within ``budget``."""

    def cut_index(self, markdown: str, budget: int) -> int:
        """Return the first message's cut index."""

    def escape(self, text: str) -> str:
        """Escape Belle-authored text, leaving model Markdown untouched."""


def measure_of(unit: Literal["chars", "bytes"]) -> Callable[[str], int]:
    """Return a character or UTF-8 byte counter."""
    return len if unit == "chars" else _utf8_bytes


def _utf8_bytes(text: str) -> int:
    return len(text.encode())


def markdown_cut_index(text: str, budget: int, measure: Callable[[str], int]) -> int:
    """Cut at the last fitting paragraph, line, or word boundary."""
    best, fence, offset = 0, "", 0
    for line in text.split("\n"):
        mark = line.lstrip()[:3]
        if fence:
            fence = "" if mark == fence else fence
        elif mark in ("```", "~~~"):
            fence = mark
        offset += len(line) + 1
        if not fence and not line.strip() and offset > 1:
            if measure(text[: offset - 1].rstrip()) > budget:
                break
            best = offset - 1
    if best:
        return best
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[: split_index(text, middle)].rstrip()) <= budget:
            low = middle
        else:
            high = middle - 1
    return max(1, split_index(text, low))


def markdown_parts(text: str, budget: int, measure: Callable[[str], int]) -> list[str]:
    """Split Markdown within ``budget``, repeating fences and table headers as needed."""
    if budget <= 0:
        raise ValueError("budget must be positive")
    found: list[str] = []
    current = ""
    for block in _blocks(text):
        joined = f"{current}\n\n{block}" if current else block
        if measure(joined) <= budget:
            current = joined
            continue
        if current:
            found.append(current)
        if measure(block) <= budget:
            current = block
        else:
            found.extend(_cut_block(block, budget, measure))
            current = ""
    if current:
        found.append(current)
    return found


def _blocks(text: str) -> list[str]:
    """Return top-level Markdown blocks."""
    lines = text.strip().split("\n")
    starts = sorted({token.map[0] for token in _MD.parse("\n".join(lines)) if token.level == 0 and token.map})
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    bounds = [*starts, len(lines)]
    return [block for a, b in itertools.pairwise(bounds) if (block := "\n".join(lines[a:b]).strip())]


def _cut_block(block: str, budget: int, measure: Callable[[str], int]) -> list[str]:
    lines = block.split("\n")
    first = lines[0].lstrip()
    if first.startswith(("```", "~~~")):
        fence = first[: len(first) - len(first.lstrip(first[0]))]
        body = lines[1:-1] if len(lines) > 1 and lines[-1].strip().startswith(fence) else lines[1:]
        return _pack(body, budget, measure, head=[lines[0]], tail=[fence])
    if len(lines) > 2 and set(lines[1].replace("|", "").replace(":", "").strip()) == {"-"}:
        return _pack(lines[2:], budget, measure, head=lines[:2], tail=[])
    return _cut_text(block, budget, measure)


def _pack(lines: list[str], budget: int, measure: Callable[[str], int], *, head: list[str], tail: list[str]) -> list[str]:
    """Pack lines while repeating ``head`` and ``tail`` in each part."""
    def whole(chunk: list[str]) -> str:
        return "\n".join([*head, *chunk, *tail])

    if measure(whole(["x"])) > budget:
        return _cut_text(whole(lines), budget, measure)

    found: list[str] = []
    chunk: list[str] = []
    for line in lines:
        if measure(whole([*chunk, line])) <= budget:
            chunk.append(line)
            continue
        if chunk:
            found.append(whole(chunk))
            chunk = []
        if measure(whole([line])) <= budget:
            chunk = [line]
        else:
            found.extend(whole([piece]) for piece in _cut_text(line, budget, lambda piece: measure(whole([piece]))))
    if chunk:
        found.append(whole(chunk))
    return found


def _cut_text(text: str, budget: int, measure: Callable[[str], int]) -> list[str]:
    """Split oversized text at word or line boundaries."""
    found: list[str] = []
    rest = text
    while measure(rest) > budget:
        if measure(rest[0]) > budget:
            raise ValueError("one character exceeds the message budget")
        low, high = 1, len(rest)
        while low < high:
            middle = (low + high + 1) // 2
            if measure(rest[: split_index(rest, middle)].rstrip()) <= budget:
                low = middle
            else:
                high = middle - 1
        cut = max(1, split_index(rest, low))
        found.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        found.append(rest)
    return found
