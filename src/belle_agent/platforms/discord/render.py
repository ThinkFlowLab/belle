"""The model's Markdown as Discord messages: Discord's Markdown, in parts of at most 2000 characters.

Discord shows ``#`` to ``###`` headings, emphasis, strikethrough, code, quotes, lists and links, and no
table. So a table becomes a fenced block with aligned columns, a heading of level 4 to 6 a bold line, a
Setext heading a ``#`` one, ``__strong__`` (underline on Discord) ``**strong**``, and an image a link. Code
is never changed. A table or heading inside a list or a quote is left as it is.
"""

import re
from typing import Any

from markdown_it import MarkdownIt

from belle_agent.platforms.rendering import RenderedPart, markdown_cut_index, markdown_parts, measure_of

#: Who a message may notify: the users it names. ``@everyone``, ``@here`` and roles notify a whole guild or
#: role, so they never do (decision DX3).
ALLOWED_MENTIONS: dict[str, Any] = {"parse": ["users"], "replied_user": False}
#: Discord counts the 2000 in characters; Belle counts code points (note 14, verified facts).
_MEASURE = measure_of("chars")
#: The characters that are Markdown on Discord, escaped in Belle's own text.
_SPECIAL = re.compile(r"([\\*_~`|>#\-\[\]()])")
_STRONG_UNDERSCORES = re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
# The grammar the tables and headings are found by: as the shared splitter's, CommonMark with tables.
_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def _plain(inline: Any) -> str:
    """The text of an inline token, without its markup: a cell of a table or a heading as words."""
    found = []
    for child in inline.children or ():
        if child.type in ("text", "code_inline"):
            found.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            found.append(" ")
        elif child.type == "image":
            found.append(_plain(child) or child.attrs.get("src", ""))
    return "".join(found).strip()


def _table(tokens: list[Any], start: int) -> tuple[list[str], int]:
    """The table that opens at ``tokens[start]`` as the lines of a fenced block, and the index after it."""
    rows: list[list[str]] = []
    align: list[str] = []
    index = start
    while tokens[index].type != "table_close":
        token = tokens[index]
        if token.type == "tr_open":
            rows.append([])
        elif token.type in ("th_open", "td_open"):
            if token.type == "th_open":
                align.append(str(token.attrs.get("style", "")))
            rows[-1].append(_plain(tokens[index + 1]))
        index += 1
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    widths = [max(len(row[column]) for row in rows) for column in range(width)]

    def line(row: list[str]) -> str:
        cells = [cell.rjust(widths[n]) if "right" in (align[n] if n < len(align) else "") else cell.ljust(widths[n])
                 for n, cell in enumerate(row)]
        return " | ".join(cells).rstrip()

    rule = "-+-".join("-" * size for size in widths)
    return ["```", line(rows[0]), rule, *(line(row) for row in rows[1:]), "```"], index + 1


def _blocks(text: str) -> str:
    """Rewrite the top-level tables and headings Discord cannot show, by the lines markdown-it maps them to."""
    lines = text.split("\n")
    tokens = _MD.parse(text)
    replacements: list[tuple[int, int, list[str]]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.level == 0 and token.map and token.type == "table_open":
            fenced, index = _table(tokens, index)
            replacements.append((token.map[0], token.map[1], fenced))
            continue
        if token.level == 0 and token.map and token.type == "heading_open":
            level, words = int(token.tag[1]), _plain(tokens[index + 1])
            setext = token.markup in ("=", "-")
            if level > 3:
                replacements.append((token.map[0], token.map[1], [f"**{words}**"]))
            elif setext:
                replacements.append((token.map[0], token.map[1], [f"{'#' * level} {tokens[index + 1].content}"]))
        index += 1
    for start, end, new in reversed(replacements):
        lines[start:end] = new
    return "\n".join(lines)


def _inline(text: str) -> str:
    """``__strong__`` as ``**strong**`` and images as links, outside fenced blocks and code spans."""
    found, fence = [], ""
    for line in text.split("\n"):
        mark = line.lstrip()[:3]
        if fence:
            fence = "" if mark == fence else fence
            found.append(line)
            continue
        if mark in ("```", "~~~"):
            fence = mark
            found.append(line)
            continue
        pieces, last = [], 0
        for code in _CODE_SPAN.finditer(line):
            pieces.append(_IMAGE.sub(r"[\1](", _STRONG_UNDERSCORES.sub(r"**\1**", line[last:code.start()])))
            pieces.append(code.group(0))
            last = code.end()
        pieces.append(_IMAGE.sub(r"[\1](", _STRONG_UNDERSCORES.sub(r"**\1**", line[last:])))
        found.append("".join(pieces))
    return "\n".join(found)


def discord_markdown(markdown: str) -> str:
    """The model's Markdown as Discord shows it."""
    return _inline(_blocks(markdown))


class DiscordRenderer:
    """A Discord message: Discord Markdown as its ``content``, notifying only the users it names."""

    def measure(self, markdown: str) -> int:
        """The characters ``markdown`` takes in a message."""
        return _MEASURE(markdown)

    def parts(self, markdown: str, budget: int) -> list[RenderedPart[dict[str, Any]]]:
        """``markdown`` as Discord Markdown, in messages of at most ``budget`` characters, no block broken."""
        return [
            RenderedPart(part, {"content": part, "allowed_mentions": ALLOWED_MENTIONS})
            for part in markdown_parts(discord_markdown(markdown), budget, _MEASURE)
        ]

    def cut_index(self, markdown: str, budget: int) -> int:
        """Where the first message of ``markdown`` that fits ``budget`` ends."""
        return markdown_cut_index(markdown, budget, _MEASURE)

    def escape(self, text: str) -> str:
        """Belle's own text, such as a file name in a notice, with Discord's Markdown characters escaped."""
        return _SPECIAL.sub(r"\\\1", text)


RENDERER = DiscordRenderer()
