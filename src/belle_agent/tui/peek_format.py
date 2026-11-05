"""Format live peek content as completed markdown blocks plus a plain open tail.

Answer text and thinking are slightly indented. Structural kinds (steps, tools,
questions, usage, outcome) stay flush-left with a lead marker so meta lines
read apart from the stream.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from belle_agent.control.peek import (
    PEEK_KIND_OUTCOME,
    PEEK_KIND_QUESTION,
    PEEK_KIND_STEP,
    PEEK_KIND_TEXT,
    PEEK_KIND_THINKING,
    PEEK_KIND_TOOL_CALL,
    PEEK_KIND_TOOL_RESULT,
    PEEK_KIND_USAGE,
)

_HEADING_LINE = re.compile(r'^#{1,6}\s+\S')
#: Indent for answer / thinking so flush-left meta lines stand out.
_STREAM_INDENT = 2

PeekKind = Literal[
    'text', 'thinking', 'step', 'tool_call', 'tool_result', 'question', 'usage', 'outcome',
]


@dataclass
class PeekPart:
    """One contiguous run of peek content sharing a kind."""

    kind: PeekKind
    text: str


def format_peek_step(payload: Mapping[str, Any]) -> str:
    """Render a labelled step break; unlabeled steps are omitted (no empty ── line)."""
    label = payload.get('label')
    if isinstance(label, str) and label.strip():
        return f"\n· ── {label.strip()} ──\n"
    return ""


def peek_tool_body(payload: Mapping[str, Any], kind: PeekKind) -> str | None:
    """Optional expandable body for a tool frame, or null when absent/blank."""
    key = 'args_text' if kind == PEEK_KIND_TOOL_CALL else 'result_text'
    body = payload.get(key)
    if isinstance(body, str) and body.strip():
        return body
    return None


#: ``→ `` / ``← `` so ``└`` lines up with the first letter of the tool name.
_TOOL_NAME_INDENT = '  '


def format_peek_tool_body_marker(*, marker_style: str = 'bold') -> Text:
    """Indent + amber ``└`` + one space; payload (and its background) start after this."""
    out = Text(_TOOL_NAME_INDENT)
    out.append('└', style=marker_style)
    out.append(' ')
    return out


def format_peek_tool_call(payload: Mapping[str, Any], *, expanded: bool = False) -> str:
    name = str(payload.get('name') or 'tool')
    detail = payload.get('detail')
    line = f"→ {name}"
    if isinstance(detail, str) and detail.strip():
        line += f" · {detail.strip()}"
    if peek_tool_body(payload, PEEK_KIND_TOOL_CALL) is not None:
        line += "  −" if expanded else "  +"
    return f"{line}\n"


def format_peek_tool_result(payload: Mapping[str, Any], *, expanded: bool = False) -> str:
    name = payload.get('name')
    label = name.strip() if isinstance(name, str) and name.strip() else str(payload.get('call_id') or 'tool')
    ok = payload.get('ok')
    state = 'ok' if ok is True else 'failed'
    line = f"← {label} · {state}"
    summary = payload.get('summary')
    if isinstance(summary, str) and summary.strip():
        line += f" · {summary.strip()}"
    if peek_tool_body(payload, PEEK_KIND_TOOL_RESULT) is not None:
        line += "  −" if expanded else "  +"
    return f"{line}\n"


def format_peek_question(payload: Mapping[str, Any]) -> str:
    summary = str(payload.get('prompt_summary') or 'Question')
    kind = payload.get('kind')
    prefix = f"{kind}: " if isinstance(kind, str) and kind.strip() and kind != 'question' else ""
    return f"? {prefix}{summary}\n"


def format_peek_usage(payload: Mapping[str, Any]) -> str:
    """Compact one-line usage strip for the peek pane (tokens and optional cost)."""
    bits: list[str] = []
    tokens_in, tokens_out = payload.get('tokens_in'), payload.get('tokens_out')
    if type(tokens_in) is int or type(tokens_out) is int:
        inn = tokens_in if type(tokens_in) is int else '?'
        out = tokens_out if type(tokens_out) is int else '?'
        bits.append(f"tokens {inn}/{out}")
    cost = payload.get('cost')
    if type(cost) in (int, float) and not isinstance(cost, bool):
        bits.append(f"cost {float(cost):g}")
    return f"# {' · '.join(bits)}\n" if bits else ""


def split_closed_markdown(text: str, *, flush: bool = False) -> tuple[list[str], str]:
    """Split ``text`` into completed markdown blocks and an unfinished tail.

    A block completes when a fenced code block closes, when a blank line ends a
    paragraph, or when a later fence line starts (the preceding prose closes).
    With ``flush``, the remaining tail is emitted as a final block.
    """
    if not text:
        return [], ''
    blocks: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if _fence_open_at(text, i):
            end = _fence_close_end(text, i)
            if end is None:
                break
            blocks.append(text[i:end])
            i = end
            continue
        boundary = _next_prose_boundary(text, i)
        if boundary is None:
            break
        # Hold a heading until the following block closes (or flush), so the open
        # edge does not freeze a heading alone without its body.
        if _is_heading_block(text[i:boundary]):
            extended = _next_prose_boundary(text, boundary)
            if extended is not None:
                boundary = extended
            elif flush:
                boundary = n
            else:
                break
        blocks.append(text[i:boundary])
        i = boundary
    if flush and i < n:
        blocks.append(text[i:])
        i = n
    return blocks, text[i:]


def render_peek_parts(parts: Sequence[PeekPart], *, flush: bool = False) -> RenderableType:
    """Render peek parts: markdown for closed text blocks, plain for the open edge."""
    renderables: list[RenderableType] = []
    for index, part in enumerate(parts):
        # Closed runs (and non-text kinds) flush; only the last text run may keep a plain tail.
        part_flush = flush or index < len(parts) - 1 or part.kind != PEEK_KIND_TEXT
        renderables.extend(_render_one(part, flush=part_flush))
    if not renderables:
        return Text('')
    if len(renderables) == 1:
        return renderables[0]
    return Group(*renderables)


def _indent_stream(renderable: RenderableType) -> RenderableType:
    return Padding(renderable, (0, 0, 0, _STREAM_INDENT))


def _render_one(part: PeekPart, *, flush: bool) -> list[RenderableType]:
    # Meta lines stay flush-left with a lead marker; stream text is indented under them.
    if part.kind == PEEK_KIND_THINKING:
        return [_indent_stream(Text(part.text, style='dim italic'))] if part.text else []
    if part.kind == PEEK_KIND_USAGE:
        # Always leave one blank row above usage; prior text/tools end with uneven newlines.
        return [Padding(Text(part.text, style='dim'), (1, 0, 0, 0))] if part.text else []
    if part.kind == PEEK_KIND_OUTCOME:
        return [Text(part.text, style='dim')] if part.text else []
    if part.kind == PEEK_KIND_STEP:
        return [Text(part.text, style='dim')] if part.text else []
    if part.kind == PEEK_KIND_TOOL_CALL:
        return [Text(part.text, style='bold')] if part.text else []
    if part.kind == PEEK_KIND_TOOL_RESULT:
        style = 'bold red' if ' · failed' in part.text else 'dim'
        return [Text(part.text, style=style)] if part.text else []
    if part.kind == PEEK_KIND_QUESTION:
        return [Text(part.text, style='bold yellow')] if part.text else []
    blocks, tail = split_closed_markdown(part.text, flush=flush)
    # Join closed blocks into one Markdown so heading spacing matches a single document.
    out: list[RenderableType] = []
    if blocks:
        out.append(_indent_stream(Markdown(''.join(blocks))))
    if tail:
        out.append(_indent_stream(Text(tail)))
    return out


def _is_heading_block(block: str) -> bool:
    """True when the block's first content line is an ATFM ATX heading."""
    for line in block.lstrip('\n').splitlines():
        return bool(_HEADING_LINE.match(line.rstrip()))
    return False


def _line_start(text: str, index: int) -> bool:
    return index == 0 or text[index - 1] == '\n'


def _fence_open_at(text: str, index: int) -> bool:
    return _line_start(text, index) and text.startswith('```', index)


def _fence_tick_count(text: str, index: int) -> int:
    end = index
    while end < len(text) and text[end] == '`':
        end += 1
    return end - index


def _fence_close_end(text: str, open_at: int) -> int | None:
    """Return the index after the closing fence line, or None if still open."""
    ticks = _fence_tick_count(text, open_at)
    if ticks < 3:
        return None
    line_end = text.find('\n', open_at)
    if line_end < 0:
        return None
    pos = line_end + 1
    while pos < len(text):
        if not _line_start(text, pos):
            nxt = text.find('\n', pos)
            if nxt < 0:
                return None
            pos = nxt + 1
            continue
        if text.startswith('`' * ticks, pos):
            after = pos + ticks
            while after < len(text) and text[after] == '`':
                after += 1
            eol = text.find('\n', after)
            rest = text[after:] if eol < 0 else text[after:eol]
            if rest.strip() == '':
                return len(text) if eol < 0 else eol + 1
        nxt = text.find('\n', pos)
        if nxt < 0:
            return None
        pos = nxt + 1
    return None


def _next_fence_line(text: str, start: int) -> int | None:
    pos = start
    while pos < len(text):
        nxt = text.find('\n', pos)
        line_begin = nxt + 1 if nxt >= 0 else None
        if line_begin is None:
            return None
        if _fence_open_at(text, line_begin):
            return line_begin
        pos = line_begin
    return None


def _next_prose_boundary(text: str, start: int) -> int | None:
    """End index of the next completed prose block starting at ``start``."""
    blank = text.find('\n\n', start)
    fence = _next_fence_line(text, start)
    candidates = [index for index in (blank, fence) if index is not None and index > start]
    if not candidates:
        return None
    chosen = min(candidates)
    if blank is not None and chosen == blank:
        end = blank + 2
        while end < len(text) and text[end] == '\n':
            end += 1
        return end
    return chosen
