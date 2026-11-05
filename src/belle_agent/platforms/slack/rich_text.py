"""Render Markdown as Slack rich-text elements."""

import re
from typing import Any

from .text import (
    MARKDOWN_FENCE_OPEN_RE,
    _markdown_link_end,
    markdown_fence_span,
)

LIST_STYLE_BULLET = "bullet"
LIST_STYLE_ORDERED = "ordered"

TEXT_STYLE_FLAGS = ("bold", "italic", "strike", "code")
LINK_STYLE_FLAGS = ("bold", "italic", "strike")

TAB_WIDTH = 4

BROADCAST_RANGES = {
    "here": "here",
    "channel": "channel",
    "everyone": "everyone",
    "group": "everyone",
}

_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?(?P<body>.*)$")
_BULLET_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)[-+*][ \t]+(?P<body>.*)$")
_ORDERED_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<number>\d{1,9})[.)][ \t]+(?P<body>.*)$")

_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)", re.DOTALL)
_BOLD_STAR_RE = re.compile(r"\*\*(?=\S)(?P<body>.+?)(?<=\S)\*\*", re.DOTALL)
_BOLD_UNDER_RE = re.compile(r"__(?=\S)(?P<body>.+?)(?<=\S)__(?![0-9A-Za-z_])")
_ITALIC_STAR_RE = re.compile(r"\*(?=\S)(?P<body>[^*\n]+?)(?<=\S)\*")
_ITALIC_UNDER_RE = re.compile(r"_(?=\S)(?P<body>[^_\n]+?)(?<=\S)_(?![0-9A-Za-z_])")
_STRIKE_DOUBLE_RE = re.compile(r"~~(?=\S)(?P<body>.+?)(?<=\S)~~", re.DOTALL)
_STRIKE_SINGLE_RE = re.compile(r"~(?=\S)(?P<body>[^~\n]+?)(?<=\S)~")

_EMOJI_RE = re.compile(r":(?P<name>[a-z0-9][a-z0-9_+\-]*(?:::skin-tone-[2-6])?):")

_USER_TOKEN_RE = re.compile(r"<@(?P<id>[UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_TOKEN_RE = re.compile(r"<#(?P<id>C[A-Z0-9]+)(?:\|[^>]*)?>")
_USERGROUP_TOKEN_RE = re.compile(r"<!subteam\^(?P<id>S[A-Z0-9]+)(?:\|[^>]*)?>")
_BROADCAST_TOKEN_RE = re.compile(r"<!(?P<range>here|channel|everyone|group)(?:\|[^>]*)?>")
_AUTOLINK_RE = re.compile(r"<(?P<url>[a-zA-Z][a-zA-Z0-9+.\-]*:[^<>\s|]+)>")

_URL_SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
_ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def render_rich_text(markdown: str) -> list[dict[str, Any]]:
    """Return rich-text elements for a block or cell. Blank input yields none."""
    if not markdown or not markdown.strip():
        return []

    lines = markdown.splitlines()
    elements: list[dict[str, Any]] = []
    prose: list[str] = []
    index = 0

    def flush_prose() -> None:
        section = _section(prose, {})
        prose.clear()
        if section is not None:
            elements.append(section)

    while index < len(lines):
        line = lines[index]

        fence = MARKDOWN_FENCE_OPEN_RE.match(line)
        if fence:
            flush_prose()
            block, index = _preformatted(lines, index, fence)
            if block is not None:
                elements.append(block)
            continue

        if _QUOTE_RE.match(line):
            flush_prose()
            block, index = _quote(lines, index)
            if block is not None:
                elements.append(block)
            continue

        if _list_item(line) is not None:
            flush_prose()
            blocks, index = _lists(lines, index)
            elements.extend(blocks)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_prose()
            section = _section([heading.group("text")], {"bold": True})
            if section is not None:
                elements.append(section)
            index += 1
            continue

        prose.append(line)
        index += 1

    flush_prose()
    return elements


def rich_text_block(markdown: str, *, block_id: str | None = None) -> dict[str, Any] | None:
    """Wrap nonempty Markdown in a rich-text block."""
    elements = render_rich_text(markdown)
    if not elements:
        return None
    block: dict[str, Any] = {"type": "rich_text", "elements": elements}
    if block_id:
        block["block_id"] = block_id
    return block


def _section(lines: list[str], style: dict[str, bool]) -> dict[str, Any] | None:
    """Render prose in one section, preserving paragraph breaks."""
    trimmed = _collapse_blanks(lines)
    if not trimmed:
        return None
    elements: list[dict[str, Any]] = []
    for position, line in enumerate(trimmed):
        if position:
            elements.append({"type": "text", "text": "\n"})
        elements.extend(_inline(line, style))
    merged = _merge(elements)
    if not merged:
        return None
    return {"type": "rich_text_section", "elements": merged}


def _collapse_blanks(lines: list[str]) -> list[str]:
    """Trim blank lines and collapse consecutive blanks."""
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    collapsed: list[str] = []
    for line in lines[start:end]:
        if line.strip():
            collapsed.append(line)
        elif collapsed and collapsed[-1]:
            collapsed.append("")
    return collapsed


def _quote(lines: list[str], start: int) -> tuple[dict[str, Any] | None, int]:
    """Render consecutive quote lines. Nested markers remain literal."""
    body: list[str] = []
    index = start
    while index < len(lines):
        match = _QUOTE_RE.match(lines[index])
        if not match:
            break
        body.append(match.group("body"))
        index += 1
    section = _section(body, {})
    if section is None:
        return None, index
    return {"type": "rich_text_quote", "elements": section["elements"]}, index


def _preformatted(lines: list[str], start: int, fence: re.Match[str]) -> tuple[dict[str, Any] | None, int]:
    """Render fenced code through its closing marker or EOF."""
    info = fence.group("info").strip()
    body, consumed, _ = markdown_fence_span(lines, start, fence.group("fence"))
    index = start + consumed
    text = "\n".join(body)
    if not text:
        return None, index
    block: dict[str, Any] = {
        "type": "rich_text_preformatted",
        "elements": [{"type": "text", "text": text}],
    }
    language = info.split()[0].lower() if info else ""
    if language:
        block["language"] = language
    return block, index


def _list_item(line: str) -> tuple[int, str, int, str] | None:
    """Return (indent, style, number, body) for a list item."""
    ordered = _ORDERED_ITEM_RE.match(line)
    if ordered:
        return (
            _indent_width(ordered.group("indent")),
            LIST_STYLE_ORDERED,
            int(ordered.group("number")),
            ordered.group("body"),
        )
    bullet = _BULLET_ITEM_RE.match(line)
    if bullet:
        return (
            _indent_width(bullet.group("indent")),
            LIST_STYLE_BULLET,
            1,
            bullet.group("body"),
        )
    return None


def _indent_width(indent: str) -> int:
    return len(indent.replace("\t", " " * TAB_WIDTH))


def _lists(lines: list[str], start: int) -> tuple[list[dict[str, Any]], int]:
    """Group consecutive list items by style and relative indent depth."""
    items: list[tuple[int, str, int, str]] = []
    index = start
    while index < len(lines):
        item = _list_item(lines[index])
        if item is not None:
            items.append(item)
            index += 1
            continue
        if not lines[index].strip():
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines) and _list_item(lines[lookahead]) is not None:
                index = lookahead
                continue
        break

    widths: list[int] = []
    blocks: list[dict[str, Any]] = []
    current_key: tuple[str, int] | None = None
    for width, style, number, body in items:
        while widths and width < widths[-1]:
            widths.pop()
        if not widths or width > widths[-1]:
            widths.append(width)
        depth = len(widths) - 1
        section = _section([body], {}) or {
            "type": "rich_text_section",
            "elements": [{"type": "text", "text": ""}],
        }
        if current_key != (style, depth):
            current: dict[str, Any] = {"type": "rich_text_list", "style": style, "elements": []}
            if depth:
                current["indent"] = depth
            if style == LIST_STYLE_ORDERED and number > 1:
                current["offset"] = number - 1
            blocks.append(current)
            current_key = (style, depth)
        blocks[-1]["elements"].append(section)
    return blocks, index


def _inline(text: str, style: dict[str, bool]) -> list[dict[str, Any]]:
    """Scan one line left to right and apply inherited style to each element."""
    elements: list[dict[str, Any]] = []
    buffer: list[str] = []
    position = 0
    length = len(text)

    def flush() -> None:
        if buffer:
            elements.append(_text("".join(buffer), style))
            buffer.clear()

    while position < length:
        character = text[position]

        if character == "\\" and position + 1 < length and text[position + 1] in _ESCAPABLE:
            buffer.append(text[position + 1])
            position += 2
            continue

        if character == "`":
            code = _CODE_SPAN_RE.match(text, position)
            if code:
                flush()
                elements.append(_text(code.group("body"), {**style, "code": True}))
                position = code.end()
                continue

        token = None
        if character in "![":
            token = _markdown_link(text, position, style)
        elif character == "<":
            token = _angle_token(text, position, style)
        if token is not None:
            element, position = token
            flush()
            elements.append(element)
            continue

        if character == ":" and _emoji_boundary(text, position):
            emoji = _EMOJI_RE.match(text, position)
            if emoji:
                flush()
                elements.append({"type": "emoji", "name": emoji.group("name")})
                position = emoji.end()
                continue

        emphasis = _emphasis(text, position, style)
        if emphasis is not None:
            inner, end = emphasis
            flush()
            elements.extend(inner)
            position = end
            continue

        buffer.append(character)
        position += 1

    flush()
    return _merge(elements)


def _text(value: str, style: dict[str, bool]) -> dict[str, Any]:
    return _with_style({"type": "text", "text": value}, style, TEXT_STYLE_FLAGS)


def _emphasis(text: str, position: int, style: dict[str, bool]) -> tuple[list[dict[str, Any]], int] | None:
    """Parse emphasis at position, preferring long markers."""
    character = text[position]
    if character == "*":
        candidates = ((_BOLD_STAR_RE, "bold"), (_ITALIC_STAR_RE, "italic"))
    elif character == "_":
        if position and (text[position - 1].isalnum() or text[position - 1] == "_"):
            return None
        candidates = ((_BOLD_UNDER_RE, "bold"), (_ITALIC_UNDER_RE, "italic"))
    elif character == "~":
        candidates = ((_STRIKE_DOUBLE_RE, "strike"), (_STRIKE_SINGLE_RE, "strike"))
    else:
        return None

    for pattern, flag in candidates:
        match = pattern.match(text, position)
        if match:
            return _inline(match.group("body"), {**style, flag: True}), match.end()
    return None


def _emoji_boundary(text: str, position: int) -> bool:
    """Reject colons within words, URLs, and times as emoji starts."""
    if position == 0:
        return True
    previous = text[position - 1]
    return not (previous.isalnum() or previous == "_")


def _markdown_link(text: str, position: int, style: dict[str, bool]) -> tuple[dict[str, Any], int] | None:
    """Parse a link or image at position, including balanced parentheses."""
    start = position
    if text[start] == "!":
        start += 1
    if start >= len(text) or text[start] != "[":
        return None

    depth = 0
    cursor = start + 1
    label_end = -1
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "[":
            depth += 1
        elif text[cursor] == "]":
            if depth == 0:
                label_end = cursor
                break
            depth -= 1
        cursor += 1
    if label_end < 0 or label_end + 1 >= len(text) or text[label_end + 1] != "(":
        return None

    url_end = _markdown_link_end(text, label_end + 2)
    if url_end < 0:
        return None

    url = text[label_end + 2 : url_end]
    if not _URL_SCHEME_RE.match(url):
        return None
    label = text[start + 1 : label_end]
    return _link(url, label, style), url_end + 1


def _link(url: str, label: str, style: dict[str, bool]) -> dict[str, Any]:
    """Build a link, retaining only style shared by its entire label."""
    label_text, label_style = _label(label)
    element: dict[str, Any] = {"type": "link", "url": url}
    if label_text and label_text != url:
        element["text"] = label_text
    return _with_style(element, {**style, **label_style}, LINK_STYLE_FLAGS)


def _label(label: str) -> tuple[str, dict[str, bool]]:
    """Return a label's text and shared style flags."""
    elements = _inline(label, {})
    words: list[str] = []
    styles: list[frozenset[str]] = []
    for element in elements:
        if element["type"] == "text":
            words.append(element["text"])
        elif element["type"] == "emoji":
            words.append(f":{element['name']}:")
        else:
            return "".join(words).strip(), {}
        styles.append(frozenset(element.get("style", {})))
    text = "".join(words).strip()
    shared = set(styles[0]) if styles else set()
    for flags in styles[1:]:
        shared &= flags
    return text, {name: True for name in shared if name in LINK_STYLE_FLAGS}


def _angle_token(text: str, position: int, style: dict[str, bool]) -> tuple[dict[str, Any], int] | None:
    """Render a typed Slack token or Markdown autolink."""
    for pattern, kind, field in (
        (_USER_TOKEN_RE, "user", "user_id"),
        (_CHANNEL_TOKEN_RE, "channel", "channel_id"),
        (_USERGROUP_TOKEN_RE, "usergroup", "usergroup_id"),
    ):
        match = pattern.match(text, position)
        if match:
            return _with_style({"type": kind, field: match.group("id")}, style, LINK_STYLE_FLAGS), match.end()
    broadcast = _BROADCAST_TOKEN_RE.match(text, position)
    if broadcast:
        element = {
            "type": "broadcast",
            "range": BROADCAST_RANGES[broadcast.group("range")],
        }
        return _with_style(element, style, LINK_STYLE_FLAGS), broadcast.end()
    autolink = _AUTOLINK_RE.match(text, position)
    if autolink:
        url = autolink.group("url")
        return _link(url, "", style), autolink.end()
    return None


def _with_style(element: dict[str, Any], style: dict[str, bool], allowed: tuple[str, ...]) -> dict[str, Any]:
    flags = {name: True for name in allowed if style.get(name)}
    if flags:
        element["style"] = flags
    return element


def _merge(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent text with the same style and drop empty text."""
    merged: list[dict[str, Any]] = []
    for element in elements:
        if element["type"] != "text":
            merged.append(element)
            continue
        if not element["text"]:
            continue
        if merged and merged[-1]["type"] == "text" and merged[-1].get("style") == element.get("style"):
            merged[-1]["text"] += element["text"]
            continue
        merged.append(element)
    return merged


__all__ = [
    "BROADCAST_RANGES",
    "LINK_STYLE_FLAGS",
    "LIST_STYLE_BULLET",
    "LIST_STYLE_ORDERED",
    "TAB_WIDTH",
    "TEXT_STYLE_FLAGS",
    "render_rich_text",
    "rich_text_block",
]
