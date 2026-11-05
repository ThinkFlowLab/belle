"""Render tables and supported fences as safe Slack Block Kit."""

import json
import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent.platforms.slack.text import (
    MARKDOWN_FENCE_OPEN_RE,
    clamp,
    markdown_fence_span,
)

logger = logging.getLogger(__name__)

# Slack's published limits.
MAX_BLOCKS_PER_MESSAGE = 50
MAX_BLOCKS_PER_VIEW = 100
MAX_SECTION_TEXT_LENGTH = 3000
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 20
MAX_TABLE_CHARACTERS_PER_MESSAGE = 20000
MAX_PLAIN_TABLE_ROWS = 100
MAX_PLAIN_TABLE_CHARACTERS_PER_MESSAGE = 10000

# Table rendering modes.
RENDER_TABLES_OFF = "off"
RENDER_TABLES_BASIC = "basic"
RENDER_TABLES_DATA = "data_table"
RENDER_TABLES_MODES = (RENDER_TABLES_OFF, RENDER_TABLES_BASIC, RENDER_TABLES_DATA)
RENDER_TABLES_DEFAULT = RENDER_TABLES_DATA


def render_tables_setting(slack: Mapping[str, Any]) -> str:
    """Read the table mode, including YAML's boolean ``off`` value."""
    raw = slack.get("render_tables")
    if isinstance(raw, bool):
        return RENDER_TABLES_DEFAULT if raw else RENDER_TABLES_OFF
    mode = str(raw or "").strip().lower()
    return mode if mode in RENDER_TABLES_MODES else RENDER_TABLES_DEFAULT


DATA_TABLE_PAGE_SIZE = 20

DEFAULT_TABLE_CAPTION = "Table"
MAX_TABLE_CAPTION_LENGTH = 75

# Model-supplied controls must not imitate the app's controls.
INTERACTIVE_FIELD = "action_id"
INTERACTIVE_TYPES = frozenset(
    {
        "actions",
        "input",
        "button",
        "workflow_button",
        "checkboxes",
        "radio_buttons",
        "overflow",
        "datepicker",
        "datetimepicker",
        "timepicker",
        "file_input",
        "email_text_input",
        "url_text_input",
        "number_input",
        "plain_text_input",
        "rich_text_input",
        "static_select",
        "external_select",
        "users_select",
        "conversations_select",
        "channels_select",
        "multi_static_select",
        "multi_external_select",
        "multi_users_select",
        "multi_conversations_select",
        "multi_channels_select",
    }
)
MAX_DATA_VISUALIZATIONS_PER_MESSAGE = 2

# Only these fence languages render; others remain visible source.
MERMAID_FENCE_LANGUAGE = "mermaid"
VEGA_LITE_FENCE_LANGUAGE = "vega-lite"
BLOCK_KIT_FENCE_LANGUAGE = "blockkit"

# Source kind selects the fallback when Slack rejects blocks.
# Interactive payloads must never appear as fallback text.
BLOCK_KIND_UNKNOWN = "unknown"
BLOCK_KIND_CHROME = "chrome"
BLOCK_KIND_DATA = "data"
BLOCK_KIND_FENCE = "fence"
BLOCK_KIND_INTERACTIVE = "interactive"
BLOCK_KIND_PRECEDENCE: tuple[str, ...] = (
    BLOCK_KIND_INTERACTIVE,
    BLOCK_KIND_FENCE,
    BLOCK_KIND_DATA,
    BLOCK_KIND_CHROME,
    BLOCK_KIND_UNKNOWN,
)


def combine_block_kinds(kinds: Collection[str]) -> str:
    """Choose the safest failure treatment for a mixed block payload."""
    present = {kind for kind in kinds if kind}
    for kind in BLOCK_KIND_PRECEDENCE:
        if kind in present:
            return kind
    return BLOCK_KIND_UNKNOWN


# Slack data_visualization limits.
MAX_CHART_TITLE_LENGTH = 50
MAX_CHART_SEGMENTS = 12
MAX_CHART_LABEL_LENGTH = 20
MAX_CHART_SERIES = 12
MAX_CHART_CATEGORIES = 20
MAX_AXIS_LABEL_LENGTH = 50
DEFAULT_CHART_TITLE = "Chart"

PIE_CHART = "pie"
BAR_CHART = "bar"
AREA_CHART = "area"
LINE_CHART = "line"

MERMAID_PLOT_CHARTS = {"bar": BAR_CHART, "line": LINE_CHART}

# Unsupported Vega-Lite marks remain visible source.
VEGA_LITE_MARKS = {
    "bar": BAR_CHART,
    "area": AREA_CHART,
    "line": LINE_CHART,
    "arc": PIE_CHART,
}

# Slack cannot render Vega-Lite composition or transforms.
VEGA_LITE_COMPOSITION_KEYS = (
    "layer",
    "facet",
    "repeat",
    "concat",
    "hconcat",
    "vconcat",
    "spec",
)
VEGA_LITE_DERIVED_KEYS = ("aggregate", "bin", "timeUnit", "stack")

# GFM delimiter rows distinguish tables from prose containing pipes.
_DELIMITER_CELL_RE = re.compile(r"^:?-{1,}:?$")

_LINK_RE = re.compile(r"<(?P<url>[^<>|]*)(?:\|(?P<label>[^<>]*))?>")
# Mentions and channel references lack URL schemes.
_URL_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.\-]*:", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")
# Normalized headings appear as bold lines or Markdown headings.
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*#*$")
_BOLD_LINE_RE = re.compile(r"^(?P<marker>\*{1,2}|_{1,2})(?P<text>\S.*?)(?P=marker)\s*:?$")
# Sort numerically only when the visible cell is a plain number.
_NUMBER_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_MERMAID_PIE_RE = re.compile(r"^pie(?:\s+showData)?(?:\s+title\s+(?P<title>.*\S))?\s*$", re.IGNORECASE)
_MERMAID_TITLE_RE = re.compile(r"^title\s+(?P<title>.*\S)\s*$", re.IGNORECASE)
_MERMAID_SEGMENT_RE = re.compile(
    r"^(?:\"(?P<quoted>[^\"]*)\"|(?P<bare>[^:]+?))\s*:\s*"
    r"(?P<value>[+-]?\d+(?:\.\d+)?)$"
)
# Mermaid accepts both xychart and xychart-beta; Slack draws vertical charts.
_XYCHART_OPEN_RE = re.compile(r"^xychart(?:-beta)?(?P<rest>\s+\S.*?)?\s*$", re.IGNORECASE)
XYCHART_DEFAULT_ORIENTATION = "vertical"
_XYCHART_TITLE_RE = re.compile(r'^title\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>.*\S))\s*$', re.IGNORECASE)
# Only numeric plot values can map to Slack series.
_XYCHART_PLOT_RE = re.compile(
    r'^(?P<plot>bar|line)\s*(?:"(?P<quoted>[^"]*)"|(?P<bare>[^\[\]"]*\S))?\s*'
    r"\[(?P<values>[^\[\]]*)\]\s*$",
    re.IGNORECASE,
)
_XYCHART_AXIS_RE = re.compile(r"^(?P<axis>x-axis|y-axis)\b(?P<rest>.*)$", re.IGNORECASE)
# Slack scales axes automatically; Mermaid's numeric range is ignored.
_XYCHART_RANGE_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*-->\s*[+-]?\d+(?:\.\d+)?$")
_XYCHART_RANGE_SEARCH_RE = re.compile(r"[+-]?\d+(?:\.\d+)?\s*-->\s*[+-]?\d+(?:\.\d+)?")
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_MERMAID_NOISE_RE = re.compile(r"^(?:%%|accTitle\s*:|accDescr\s*[:{])", re.IGNORECASE)
_EMPHASIS_RE = re.compile(r"(?<!\w)(?P<marker>[*_~])(?=\S)(?P<body>.+?)(?<=\S)(?P=marker)(?!\w)")


@dataclass(slots=True)
class _Table:
    rows: list[list[str]]
    alignments: list[str]
    raw_rows: list[list[str]]


@dataclass(slots=True)
class _Blocks:
    blocks: list[dict[str, Any]]
    kind: str


def render_blocks(
    mrkdwn: str,
    *,
    requested: bool = False,
    render_tables: str = RENDER_TABLES_DEFAULT,
) -> list[dict[str, Any]] | None:
    """Render blocks without source-kind metadata."""
    rendered = render_blocks_with_kind(
        mrkdwn,
        requested=requested,
        render_tables=render_tables,
    )
    return None if rendered is None else rendered[0]


def render_blocks_with_kind(
    mrkdwn: str,
    *,
    requested: bool = False,
    render_tables: str = RENDER_TABLES_DEFAULT,
) -> tuple[list[dict[str, Any]], str] | None:
    """Render blocks and their source kind, or return None to keep the text."""
    mode = render_tables if render_tables in RENDER_TABLES_MODES else RENDER_TABLES_DEFAULT
    segments = _segment(
        mrkdwn,
        tables=mode != RENDER_TABLES_OFF,
    )
    if not any(isinstance(segment, (_Table, _Blocks)) for segment in segments):
        return None

    blocks: list[dict[str, Any]] = []
    # One budget per block type, because Slack counts them apart.
    characters = {"table": 0, "data_table": 0}
    budgets = {
        "table": MAX_PLAIN_TABLE_CHARACTERS_PER_MESSAGE,
        "data_table": MAX_TABLE_CHARACTERS_PER_MESSAGE,
    }
    # Adjacent tables must not reuse the preceding caption.
    preceding_prose: str | None = None
    kinds: list[str] = []
    for segment in segments:
        if isinstance(segment, _Table):
            block = _table_block(
                segment,
                caption=_caption_for(preceding_prose),
                force_data_table=requested or mode == RENDER_TABLES_DATA,
            )
            if block is None:
                return None
            kind = block["type"]
            characters[kind] += _table_characters(segment) + len(block.get("caption", ""))
            if characters[kind] > budgets[kind]:
                return None
            blocks.append(block)
            kinds.append(BLOCK_KIND_DATA)
            preceding_prose = None
            continue
        if isinstance(segment, _Blocks):
            blocks.extend(segment.blocks)
            kinds.append(segment.kind)
            preceding_prose = None
            if len(blocks) > MAX_BLOCKS_PER_MESSAGE:
                return None
            continue
        preceding_prose = segment
        blocks.extend(section_blocks(segment))
        if len(blocks) > MAX_BLOCKS_PER_MESSAGE:
            return None

    if not blocks or len(blocks) > MAX_BLOCKS_PER_MESSAGE:
        return None
    charts = sum(1 for block in blocks if block["type"] == "data_visualization")
    if charts > MAX_DATA_VISUALIZATIONS_PER_MESSAGE:
        return None
    return blocks, combine_block_kinds(kinds)


def streamable_prose_prefix(mrkdwn: str) -> int:
    """Return the prefix safe to stream before a table or fence completes."""
    lines = (mrkdwn or "").splitlines(keepends=True)
    bodies = [line.rstrip("\r\n") for line in lines]
    prose: list[str] = []
    offset = 0
    index = 0

    while index < len(lines):
        body = bodies[index]

        opening = MARKDOWN_FENCE_OPEN_RE.match(body)
        if opening:
            span, consumed, closed = markdown_fence_span(bodies, index, opening.group("fence"))
            if not closed:
                return offset
            rendered = _fenced_blocks(
                (opening.group("info") or "").strip(),
                span,
                preceding_prose="\n".join(prose),
                # The compose-time call logs declined fences once.
                log=False,
            )
            if rendered is not None:
                return offset
            prose.extend(bodies[index : index + consumed])
            offset += sum(len(line) for line in lines[index : index + consumed])
            index += consumed
            continue

        if "|" in body:
            if index + 1 >= len(lines):
                return offset
            table, _ = _parse_table(bodies, index)
            if table is not None:
                return offset

        prose.append(body)
        offset += len(lines[index])
        index += 1

    return offset


def _table_characters(table: _Table) -> int:
    """What one table costs the message's character budget."""
    return sum(len(cell) for row in table.rows for cell in row)


def _segment(
    mrkdwn: str,
    *,
    tables: bool = True,
) -> list[Any]:
    """Split text into prose, tables, and supported fences. Keep declined syntax as prose."""
    lines = (mrkdwn or "").splitlines()
    segments: list[Any] = []
    prose: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]

        opening = MARKDOWN_FENCE_OPEN_RE.match(line)
        if opening:
            body, consumed, closed = markdown_fence_span(lines, index, opening.group("fence"))
            rendered = (
                _fenced_blocks(
                    (opening.group("info") or "").strip(),
                    body,
                    preceding_prose="\n".join(prose),
                )
                if closed
                else None
            )
            if rendered is not None:
                if prose:
                    segments.append("\n".join(prose))
                    prose = []
                segments.append(rendered)
            else:
                prose.extend(lines[index : index + consumed])
            index += consumed
            continue

        table, consumed = _parse_table(lines, index) if tables else (None, 0)
        if table is not None:
            if prose:
                segments.append("\n".join(prose))
                prose = []
            segments.append(table)
            index += consumed
            continue

        prose.append(line)
        index += 1

    if prose:
        segments.append("\n".join(prose))
    return segments


def _fenced_blocks(
    info: str,
    body: list[str],
    *,
    preceding_prose: str = "",
    log: bool = True,
) -> _Blocks | None:
    """Render supported fences. Log declined render requests unless log is false."""
    tokens = info.split()
    language = tokens[0].lower() if tokens else ""
    rendered: list[dict[str, Any]] | None
    # Block Kit is author-supplied; charts are compiled data.
    kind = BLOCK_KIND_FENCE
    if language == BLOCK_KIT_FENCE_LANGUAGE:
        rendered = _parse_blockkit_fence(body)
    elif language in (MERMAID_FENCE_LANGUAGE, VEGA_LITE_FENCE_LANGUAGE):
        renderer = _mermaid_chart if language == MERMAID_FENCE_LANGUAGE else _vega_lite_chart
        chart = renderer(body, preceding_prose)
        rendered = None if chart is None else [chart]
        kind = BLOCK_KIND_DATA
    else:
        return None
    if rendered is None:
        if not log:
            return None
        # Log the fence type, never its contents.
        logger.info(
            "[SlackChannel] a `%s` fence is outside what this connector can "
            "render and stays in the message as its own source (%d line(s))",
            language,
            len(body),
        )
        return None
    return _Blocks(rendered, kind)


def _parse_blockkit_fence(body: list[str]) -> list[dict[str, Any]] | None:
    """Parse a Block Kit fence, leaving refused payloads as source text."""
    try:
        payload = json.loads("\n".join(body))
    except ValueError:
        return None
    return apply_block_policy(payload)


def apply_block_policy(payload: Any) -> list[dict[str, Any]] | None:
    """Accept block objects and reject model-supplied interactive controls."""
    if isinstance(payload, dict):
        # The shape Block Kit Builder copies out, and a bare single block.
        inner = payload.get("blocks")
        payload = inner if isinstance(inner, list) else [payload]
    if not isinstance(payload, list) or not payload:
        return None
    for block in payload:
        if not isinstance(block, dict):
            return None
        kind = block.get("type")
        if not isinstance(kind, str) or not kind.strip():
            return None
        if _is_interactive(block):
            return None
    return payload


def _mermaid_chart(body: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Render supported Mermaid pie and xychart diagrams."""
    lines = [line.strip() for line in body if line.strip() and not _MERMAID_NOISE_RE.match(line.strip())]
    if not lines:
        return None
    if _MERMAID_PIE_RE.match(lines[0]):
        return _mermaid_pie(lines, preceding_prose)
    if _XYCHART_OPEN_RE.match(lines[0]):
        return _xychart(lines, preceding_prose)
    return None


def _mermaid_pie(lines: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Render a Mermaid pie chart, or decline invalid segments."""
    opening = _MERMAID_PIE_RE.match(lines[0])
    if opening is None:
        return None

    title = opening.group("title") or ""
    segments: list[dict[str, Any]] = []
    for line in lines[1:]:
        heading = _MERMAID_TITLE_RE.match(line)
        if heading is not None:
            title = title or heading.group("title")
            continue
        segment = _MERMAID_SEGMENT_RE.match(line)
        if segment is None:
            # An unrecognised line means this is not the diagram it looked like,
            # and half a pie chart is a misleading one.
            return None
        value = float(segment.group("value"))
        if value <= 0:
            return None
        label = (segment.group("quoted") or segment.group("bare") or "").strip()
        segments.append({"label": clamp(label, MAX_CHART_LABEL_LENGTH), "value": _chart_number(value)})

    chart = _pie_chart(segments)
    if chart is None:
        return None
    return _chart_block(chart, title, preceding_prose)


def _chart_block(chart: dict[str, Any], title: str, preceding_prose: str) -> dict[str, Any]:
    """Wrap a chart with its title or the preceding heading."""
    return {
        "type": "data_visualization",
        "title": clamp(
            title.strip() or _caption_for(preceding_prose, DEFAULT_CHART_TITLE),
            MAX_CHART_TITLE_LENGTH,
        ),
        "chart": chart,
    }


def _pie_chart(segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Slack's ``pie`` payload for *segments*, or ``None`` if it is out of bounds."""
    if not segments or len(segments) > MAX_CHART_SEGMENTS:
        return None
    return {"type": PIE_CHART, "segments": segments}


def _chart_number(value: float) -> int | float:
    """Use an integer for whole-valued chart numbers."""
    return int(value) if float(value).is_integer() else value


def _series_chart(
    kind: str,
    names: list[str],
    categories: list[str],
    rows: list[list[float]],
    *,
    x_label: str = "",
    y_label: str = "",
) -> dict[str, Any] | None:
    """Build a series chart with one value per category and series."""
    if not names or len(names) > MAX_CHART_SERIES:
        return None
    if not categories or len(categories) > MAX_CHART_CATEGORIES:
        return None

    clamped_categories = [clamp(label, MAX_CHART_LABEL_LENGTH) for label in categories]
    if len(set(clamped_categories)) != len(clamped_categories):
        return None
    clamped_names = [clamp(name, MAX_CHART_LABEL_LENGTH) for name in names]
    if len(set(clamped_names)) != len(clamped_names):
        return None
    if any(len(values) != len(categories) for values in rows):
        return None

    chart: dict[str, Any] = {
        "type": kind,
        "series": [
            {
                "name": name,
                "data": [
                    {"label": label, "value": _chart_number(value)} for label, value in zip(clamped_categories, values)
                ],
            }
            for name, values in zip(clamped_names, rows)
        ],
        # Slack requires category order and axis titles under axis_config.
        "axis_config": {"categories": clamped_categories},
    }
    if x_label.strip():
        chart["axis_config"]["x_label"] = clamp(x_label.strip(), MAX_AXIS_LABEL_LENGTH)
    if y_label.strip():
        chart["axis_config"]["y_label"] = clamp(y_label.strip(), MAX_AXIS_LABEL_LENGTH)
    return chart


def _xychart(lines: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Render a vertical Mermaid xychart without inventing missing values."""
    opening = _XYCHART_OPEN_RE.match(lines[0])
    if opening is None:
        return None
    orientation = (opening.group("rest") or "").strip().lower()
    if orientation and orientation != XYCHART_DEFAULT_ORIENTATION:
        return None

    title = ""
    x_label = ""
    y_label = ""
    categories: list[str] | None = None
    seen_y_axis = False
    plots: list[tuple[str, str, list[float]]] = []

    for line in lines[1:]:
        heading = _XYCHART_TITLE_RE.match(line)
        if heading is not None:
            if title:
                return None
            title = (heading.group("quoted") or heading.group("bare") or "").strip()
            continue

        axis = _XYCHART_AXIS_RE.match(line)
        if axis is not None:
            parsed = _xychart_axis(axis.group("rest"))
            if parsed is None:
                return None
            label, items = parsed
            if axis.group("axis").lower() == "x-axis":
                if categories is not None or items is None or not items:
                    return None
                categories, x_label = items, label
            else:
                # A y-axis holds a title and a range, never a category list.
                if seen_y_axis or items is not None:
                    return None
                seen_y_axis, y_label = True, label
            continue

        plot = _XYCHART_PLOT_RE.match(line)
        if plot is None:
            return None
        values = _split_bracketed(plot.group("values"))
        if not values or any(_NUMERIC_LITERAL_RE.match(value) is None for value in values):
            return None
        name = (plot.group("quoted") or plot.group("bare") or "").strip()
        plots.append((plot.group("plot").lower(), name, [float(value) for value in values]))

    if not categories or not plots:
        return None
    kinds = {plot for plot, _, _ in plots}
    if len(kinds) != 1:
        return None
    kind = MERMAID_PLOT_CHARTS[kinds.pop()]

    names = [
        name or _xychart_default_name(index, len(plots), y_label, kind)
        for index, (_, name, _) in enumerate(plots, start=1)
    ]
    chart = _series_chart(
        kind,
        names,
        categories,
        [values for _, _, values in plots],
        x_label=x_label,
        y_label=y_label,
    )
    return None if chart is None else _chart_block(chart, title, preceding_prose)


def _xychart_default_name(index: int, total: int, y_label: str, kind: str) -> str:
    """Name an unnamed plot from its axis or chart type."""
    if total == 1:
        return y_label.strip() or kind.title()
    return f"Series {index}"


def _xychart_axis(rest: str) -> tuple[str, list[str] | None] | None:
    """Parse an axis title, categories, or ignored numeric range."""
    rest = rest.strip()
    title = ""
    if rest.startswith('"'):
        closing = rest.find('"', 1)
        if closing < 0:
            return None
        title = rest[1:closing].strip()
        rest = rest[closing + 1 :].strip()

    bracket = rest.find("[")
    if bracket >= 0:
        leading = rest[:bracket].strip()
        if title and leading:
            # A quoted title and then loose words before the list: not a shape
            # mermaid draws, so not one to guess at.
            return None
        if not rest.endswith("]"):
            return None
        items = _split_bracketed(rest[bracket + 1 : -1])
        return None if items is None else (title or leading, items)

    # No category list, so what is left is a bare title, a numeric range, or a
    # title and then a range.
    if not title:
        span = _XYCHART_RANGE_SEARCH_RE.search(rest)
        if span is not None:
            title, rest = rest[: span.start()].strip(), rest[span.start() :].strip()
        else:
            title, rest = rest, ""
    if rest and _XYCHART_RANGE_RE.match(rest) is None:
        return None
    return title, None


def _split_bracketed(text: str) -> list[str] | None:
    """Split comma-separated values while preserving quoted values."""
    if not text.strip():
        return []
    items: list[str] = []
    current: list[str] = []
    quoted = False
    for character in text:
        if character == '"':
            quoted = not quoted
            continue
        if character == "," and not quoted:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    if quoted:
        return None
    items.append("".join(current).strip())
    return None if any(not item for item in items) else items


def _vega_lite_chart(body: list[str], preceding_prose: str) -> dict[str, Any] | None:
    """Render a supported single-view Vega-Lite chart."""
    try:
        spec = json.loads("\n".join(body))
    except ValueError:
        return None
    if not isinstance(spec, dict):
        return None
    if any(key in spec for key in VEGA_LITE_COMPOSITION_KEYS) or "transform" in spec:
        return None

    mark = spec.get("mark")
    if isinstance(mark, dict):
        mark = mark.get("type")
    if not isinstance(mark, str):
        return None
    kind = VEGA_LITE_MARKS.get(mark.strip().lower())
    if kind is None:
        return None

    rows = _vega_lite_rows(spec.get("data"))
    if rows is None:
        return None

    encoding = spec.get("encoding")
    if not isinstance(encoding, dict) or not encoding:
        return None
    # Decline channels that change the chart but have no Slack equivalent.
    read = {"x", "y", "color", "theta"}
    inert = {"tooltip", "description", "href", "key"}
    for channel, definition in encoding.items():
        if channel not in read and channel not in inert:
            return None
        if channel in inert:
            continue
        if not isinstance(definition, dict):
            return None
        if any(key in definition for key in VEGA_LITE_DERIVED_KEYS):
            return None
        if channel != "x" and "sort" in definition:
            # Slack can preserve x category order only.
            return None

    chart = _vega_lite_pie(encoding, rows) if kind == PIE_CHART else _vega_lite_series(kind, encoding, rows)
    if chart is None:
        return None
    return _chart_block(chart, _vega_lite_title(spec.get("title")), preceding_prose)


def _vega_lite_rows(data: Any) -> list[dict[str, Any]] | None:
    """Read inline row data without fetching external sources."""
    if not isinstance(data, dict) or set(data) != {"values"}:
        return None
    values = data.get("values")
    if not isinstance(values, list) or not values:
        return None
    if not all(isinstance(row, dict) for row in values):
        return None
    return values


def _vega_lite_title(title: Any) -> str:
    """Read a string title or join a title object's text lines."""
    if isinstance(title, str):
        return title
    if isinstance(title, dict):
        text = title.get("text")
        if isinstance(text, str):
            return text
        if isinstance(text, list) and all(isinstance(part, str) for part in text):
            return " ".join(text)
    return ""


def _vega_lite_label(definition: dict[str, Any]) -> str:
    """Use an explicit axis title, or fall back to its field name."""
    if "title" in definition:
        title = definition["title"]
        return title if isinstance(title, str) else ""
    field = definition.get("field")
    return field if isinstance(field, str) else ""


def _vega_lite_value(cell: Any) -> float | None:
    """Accept numeric chart values, excluding booleans."""
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        return None
    return float(cell)


def _vega_lite_key(cell: Any) -> str | None:
    """Read a category or series key, excluding booleans and empty values."""
    if isinstance(cell, bool) or not isinstance(cell, (str, int, float)):
        return None
    text = str(cell).strip()
    return text or None


def _vega_lite_pie(encoding: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build pie segments from theta values and color labels."""
    theta = encoding.get("theta")
    colour = encoding.get("color")
    if not isinstance(theta, dict) or not isinstance(colour, dict):
        return None
    value_field = theta.get("field")
    label_field = colour.get("field")
    if not isinstance(value_field, str) or not isinstance(label_field, str):
        return None

    segments: list[dict[str, Any]] = []
    for row in rows:
        if value_field not in row or label_field not in row:
            return None
        value = _vega_lite_value(row[value_field])
        label = _vega_lite_key(row[label_field])
        if value is None or value <= 0 or label is None:
            return None
        segments.append(
            {
                "label": clamp(label, MAX_CHART_LABEL_LENGTH),
                "value": _chart_number(value),
            }
        )
    return _pie_chart(segments)


def _vega_lite_category_order(
    x_channel: dict[str, Any],
) -> tuple[bool, list[str] | None]:
    """Read a literal x.sort list; decline computed sort forms."""
    if "sort" not in x_channel:
        return True, None
    sort = x_channel["sort"]
    if sort is None:
        return True, None
    if not isinstance(sort, list):
        return False, None
    order: list[str] = []
    for entry in sort:
        label = _vega_lite_key(entry)
        if label is None:
            return False, None
        if label not in order:
            order.append(label)
    return True, order


def _vega_lite_series(kind: str, encoding: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build a chart from rows without filling missing category values."""
    x_channel = encoding.get("x")
    y_channel = encoding.get("y")
    if not isinstance(x_channel, dict) or not isinstance(y_channel, dict):
        return None
    x_field = x_channel.get("field")
    y_field = y_channel.get("field")
    if not isinstance(x_field, str) or not isinstance(y_field, str):
        return None

    x_type = x_channel.get("type")
    if x_type is not None and not isinstance(x_type, str):
        return None
    if isinstance(x_type, str) and x_type.strip().lower() == "quantitative":
        return None
    y_type = y_channel.get("type")
    if y_type is not None and not isinstance(y_type, str):
        return None
    if isinstance(y_type, str) and y_type.strip().lower() != "quantitative":
        return None

    sortable, category_order = _vega_lite_category_order(x_channel)
    if not sortable:
        return None

    colour = encoding.get("color")
    series_field: str | None = None
    if colour is not None:
        if not isinstance(colour, dict):
            return None
        series_field = colour.get("field")
        if not isinstance(series_field, str):
            return None
        if series_field == x_field:
            # Slack cannot color categories separately. Treat matching color
            # and x fields as one series to avoid gaps in every series.
            series_field = None

    default_name = _vega_lite_label(y_channel) or y_field
    categories: list[str] = []
    grouped: dict[str, dict[str, float]] = {}
    x_all_numeric = True

    for row in rows:
        if x_field not in row or y_field not in row:
            return None
        raw_x = row[x_field]
        if not isinstance(raw_x, (int, float)) or isinstance(raw_x, bool):
            x_all_numeric = False
        label = _vega_lite_key(raw_x)
        value = _vega_lite_value(row[y_field])
        if label is None or value is None:
            return None

        if series_field is None:
            name = default_name
        else:
            if series_field not in row:
                return None
            name = _vega_lite_key(row[series_field])
            if name is None:
                return None

        if label not in categories:
            categories.append(label)
        if name not in grouped:
            grouped[name] = {}
        if label in grouped[name]:
            # Two rows for one series and category. Vega-Lite would aggregate
            # them; without a declared aggregate there is no saying which.
            return None
        grouped[name][label] = value

    # An undeclared x whose every value is a number is the quantitative channel
    # Vega-Lite would infer, and it is declined for the reason a declared one is.
    if x_type is None and x_all_numeric:
        return None
    if category_order is not None:
        # Ignore sort labels absent from the data.
        wanted = [label for label in category_order if label in categories]
        if len(wanted) != len(categories):
            # Do not invent an order for categories omitted from sort.
            return None
        categories = wanted
    if any(len(values) != len(categories) for values in grouped.values()):
        return None

    return _series_chart(
        kind,
        list(grouped),
        categories,
        [[values[label] for label in categories] for values in grouped.values()],
        x_label=_vega_lite_label(x_channel),
        y_label=_vega_lite_label(y_channel),
    )


def _is_interactive(value: Any) -> bool:
    """Find interactive elements or action IDs at any depth."""
    if isinstance(value, dict):
        if any(str(key).lower() == INTERACTIVE_FIELD for key in value):
            return True
        kind = value.get("type")
        if isinstance(kind, str) and kind.strip().lower() in INTERACTIVE_TYPES:
            return True
        return any(_is_interactive(item) for item in value.values())
    if isinstance(value, list):
        return any(_is_interactive(item) for item in value)
    return False


def _parse_table(lines: list[str], start: int) -> tuple[_Table | None, int]:
    """Parse a GFM table; pad or truncate ragged body rows."""
    header_line = lines[start]
    if "|" not in header_line or start + 1 >= len(lines):
        return None, 0

    header = _split_row(header_line)
    if not header:
        return None, 0

    delimiter_cells = _split_row(lines[start + 1])
    if len(delimiter_cells) != len(header) or not delimiter_cells:
        return None, 0
    if not all(_DELIMITER_CELL_RE.match(cell) for cell in delimiter_cells):
        return None, 0

    alignments = [_alignment(cell) for cell in delimiter_cells]
    raw_rows = [list(header)]
    rows = [[_plain_text(cell) for cell in header]]
    index = start + 2
    while index < len(lines):
        line = lines[index]
        if "|" not in line or not line.strip():
            break
        cells = _split_row(line)
        if not cells:
            break
        cells = (cells + [""] * len(header))[: len(header)]
        raw_rows.append(cells)
        rows.append([_plain_text(cell) for cell in cells])
        index += 1

    return _Table(rows, alignments, raw_rows), index - start


def _split_row(line: str) -> list[str]:
    """Split GFM cells while preserving escaped, linked, and code-span pipes."""
    stripped = line.strip()
    if not stripped or "|" not in stripped:
        return []
    stripped = stripped.removeprefix("|")
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    in_link = False
    in_code = False
    index = 0
    while index < len(stripped):
        character = stripped[index]
        if character == "\\" and index + 1 < len(stripped):
            current.append(stripped[index : index + 2])
            index += 2
            continue
        if character == "`":
            in_code = not in_code
        elif not in_code and character == "<":
            in_link = True
        elif not in_code and character == ">":
            in_link = False
        elif character == "|" and not in_link and not in_code:
            cells.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    cells.append("".join(current))
    return [cell.strip() for cell in cells]


def _alignment(delimiter_cell: str) -> str:
    if delimiter_cell.startswith(":") and delimiter_cell.endswith(":"):
        return "center"
    if delimiter_cell.endswith(":"):
        return "right"
    return "left"


def _plain_text(cell: str) -> str:
    """Remove markup that Slack raw_text cells cannot render."""
    text = _LINK_RE.sub(lambda match: match.group("label") or match.group("url"), cell)
    return _reduce_markup(text)


def _reduce_inline(text: str) -> str:
    """Remove inline markup while retaining whitespace."""
    text = _CODE_SPAN_RE.sub(lambda match: match.group("body"), text)
    # Twice, so "*_both_*" loses both markers; a third pass buys nothing.
    text = _EMPHASIS_RE.sub(lambda match: match.group("body"), text)
    text = _EMPHASIS_RE.sub(lambda match: match.group("body"), text)
    return text.replace("\\|", "|")


def _reduce_markup(text: str) -> str:
    """``_reduce_inline`` for a value that stands on its own, so also trimmed."""
    return _reduce_inline(text).strip()


def _cell(raw: str, plain: str) -> dict[str, Any]:
    """Use rich text only when a cell has a link destination to preserve."""
    if not _link_matches(raw):
        return {"type": "raw_text", "text": plain}
    elements = _rich_text_elements(raw)
    if not elements:
        return {"type": "raw_text", "text": plain}
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": elements}],
    }


def _link_matches(raw: str) -> list[re.Match[str]]:
    """Match URL links, excluding mentions and channel references."""
    return [match for match in _LINK_RE.finditer(raw) if _URL_SCHEME_RE.match(match.group("url") or "")]


def _rich_text_elements(raw: str) -> list[dict[str, Any]]:
    """Convert cell text and links into ordered rich-text elements."""
    elements: list[dict[str, Any]] = []
    position = 0
    for match in _link_matches(raw):
        _append_text(elements, raw[position : match.start()])
        url = match.group("url") or ""
        label = _reduce_markup(match.group("label") or "")
        link: dict[str, Any] = {"type": "link", "url": url}
        # Slack shows an unlabeled link's URL.
        if label and label != url:
            link["text"] = label
        elements.append(link)
        position = match.end()
    _append_text(elements, raw[position:])
    return _trim_ends(elements)


def _append_text(elements: list[dict[str, Any]], run: str) -> None:
    """Append one text run, reduced, unless nothing is left of it."""
    text = _reduce_inline(run)
    if text:
        elements.append({"type": "text", "text": text})


def _trim_ends(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim the outer edges of the cell, dropping runs that empty out."""
    if elements and elements[0]["type"] == "text":
        elements[0]["text"] = elements[0]["text"].lstrip()
    if elements and elements[-1]["type"] == "text":
        elements[-1]["text"] = elements[-1]["text"].rstrip()
    return [element for element in elements if element["type"] != "text" or element["text"]]


def _caption_for(prose: str | None, fallback: str = DEFAULT_TABLE_CAPTION) -> str:
    """Use the final prose line as a caption only if it is a heading or bold."""
    for line in reversed((prose or "").splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING_RE.match(stripped) or _BOLD_LINE_RE.match(stripped)
        if heading is None:
            return fallback
        text = _reduce_markup(heading.group("text")).rstrip(":").strip()
        return clamp(text, MAX_TABLE_CAPTION_LENGTH) or fallback
    return fallback


def _table_block(
    table: _Table,
    *,
    caption: str = DEFAULT_TABLE_CAPTION,
    force_data_table: bool = False,
) -> dict[str, Any] | None:
    """Build a plain or data table within Slack's row and column limits."""
    if not table.rows:
        return None
    if len(table.rows[0]) > MAX_TABLE_COLUMNS:
        return None

    if force_data_table:
        return _data_table_block(table, caption)

    if len(table.rows) > MAX_PLAIN_TABLE_ROWS:
        return None
    block: dict[str, Any] = {"type": "table", "rows": _cells(table)}
    if any(alignment != "left" for alignment in table.alignments):
        block["column_settings"] = [{"align": alignment} for alignment in table.alignments]
    return block


def _data_table_block(table: _Table, caption: str) -> dict[str, Any] | None:
    """Build a data table with numeric cells when whole columns qualify."""
    if len(table.rows) > MAX_TABLE_ROWS:
        return None

    numeric = _numeric_columns(table)
    # Slack requires raw_text in header cells.
    rows: list[list[dict[str, Any]]] = [[{"type": "raw_text", "text": plain} for plain in table.rows[0]]]
    for raw_row, row in zip(table.raw_rows[1:], table.rows[1:]):
        rows.append(
            [
                _number_cell(plain) if index in numeric else _cell(raw, plain)
                for index, (raw, plain) in enumerate(zip(raw_row, row))
            ]
        )
    return {
        "type": "data_table",
        "caption": caption,
        "rows": rows,
        "page_size": _page_size(len(table.rows) - 1),
    }


def _page_size(data_rows: int) -> int:
    """Show up to 20 data rows per page, with at least one row."""
    return max(1, min(DATA_TABLE_PAGE_SIZE, data_rows))


def _numeric_columns(table: _Table) -> set[int]:
    """Use numeric sorting only when every body cell in a column qualifies."""
    if len(table.rows) < 2:
        return set()
    return {
        column
        for column in range(len(table.rows[0]))
        if all(
            _NUMBER_RE.match(row[column]) and not _link_matches(raw[column])
            for row, raw in zip(table.rows[1:], table.raw_rows[1:])
        )
    }


def _number_cell(plain: str) -> dict[str, Any]:
    """Include text as well as value; Slack requires both for raw_number."""
    return {"type": "raw_number", "value": _number_value(plain), "text": plain}


def _number_value(plain: str) -> int | float:
    bare = plain.replace(",", "")
    return float(bare) if "." in bare else int(bare)


def _cells(table: _Table) -> list[list[dict[str, Any]]]:
    return [[_cell(raw, plain) for raw, plain in zip(raw_row, row)] for raw_row, row in zip(table.raw_rows, table.rows)]


def data_fallback_text(blocks: list[dict[str, Any]], *, already_in: str = "") -> str:
    """Recover table and chart data as Markdown, excluding visible source."""
    pieces: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("table", "data_table"):
            piece = _rows_as_markdown(block)
        elif kind == "data_visualization":
            piece = _chart_as_markdown(block)
        else:
            continue
        if piece and not _already_shown(piece, already_in):
            pieces.append(piece)
    return "\n\n".join(pieces)


def _already_shown(piece: str, already_in: str) -> bool:
    """Check whether source text already contains a fallback piece."""
    if not already_in:
        return False
    cells = [cell.strip() for line in piece.splitlines() for cell in line.strip().strip("|").split("|")]
    wanted = [cell for cell in cells if cell and set(cell) != {"-"}]
    return bool(wanted) and all(cell in already_in for cell in wanted)


def blockkit_fallback_summary(blocks: list[dict[str, Any]]) -> str:
    """Summarize blocks after Slack refuses a Block Kit payload."""
    for field in ("title", "alt_text"):
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            text = _text_object(block.get(field))
            if text:
                return text
    for kind in ("section", "header"):
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != kind:
                continue
            text = _text_object(block.get("text"))
            if text:
                return text
    return ""


def _text_object(value: Any) -> str:
    """Extract visible text from a Slack text object."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return ""


def _rows_as_markdown(block: dict[str, Any]) -> str:
    """Render table rows and their caption as Markdown."""
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        return ""
    grid = [[_cell_text(cell) for cell in row] for row in rows if isinstance(row, list) and row]
    return _markdown_table(grid, str(block.get("caption") or "").strip())


def _markdown_table(rows: list[list[str]], title: str = "") -> str:
    """Format data fallback rows with a shared header and separator."""
    if not rows:
        return ""
    width = max(map(len, rows))
    lines = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    table = "\n".join(lines)
    return f"*{title}*\n{table}" if title else table


def _cell_text(cell: Any) -> str:
    """Read the visible text from a table cell."""
    if not isinstance(cell, dict):
        return ""
    kind = cell.get("type")
    if kind in ("raw_text", "raw_number"):
        return str(cell.get("text") or "").strip()
    if kind == "rich_text":
        return "".join(_rich_text_text(cell.get("elements"))).strip()
    return ""


def _rich_text_text(value: Any) -> list[str]:
    """Every readable run under a ``rich_text`` cell, in order."""
    if isinstance(value, list):
        return [part for item in value for part in _rich_text_text(item)]
    if not isinstance(value, dict):
        return []
    if value.get("type") == "link":
        url = str(value.get("url") or "")
        label = str(value.get("text") or "")
        return [f"<{url}|{label}>" if label else f"<{url}>"]
    if value.get("type") == "text":
        return [str(value.get("text") or "")]
    return _rich_text_text(value.get("elements"))


def _chart_as_markdown(block: dict[str, Any]) -> str:
    """Render chart data as a Markdown table."""
    chart = block.get("chart")
    if not isinstance(chart, dict):
        return ""
    title = str(block.get("title") or "").strip()
    if chart.get("type") == PIE_CHART:
        rows = [
            [str(item.get("label") or ""), str(item.get("value"))]
            for item in chart.get("segments") or []
            if isinstance(item, dict)
        ]
        return _markdown_table([["Label", "Value"], *rows], title) if rows else ""

    series = [item for item in chart.get("series") or [] if isinstance(item, dict)]
    if not series:
        return ""
    axis = chart.get("axis_config")
    categories = list((axis or {}).get("categories") or []) if isinstance(axis, dict) else []
    if not categories:
        categories = [str(point.get("label") or "") for point in series[0].get("data") or [] if isinstance(point, dict)]
    names = [str(item.get("name") or "") for item in series]
    values = [
        {
            str(point.get("label") or ""): point.get("value")
            for point in item.get("data") or []
            if isinstance(point, dict)
        }
        for item in series
    ]
    rows = [["", *names]]
    rows.extend([str(category), *(str(mapping.get(category, "")) for mapping in values)] for category in categories)
    return _markdown_table(rows, title)


def section_blocks(prose: str) -> list[dict[str, Any]]:
    """Split prose into Slack section blocks."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": piece}}
        for piece in _split_to_limit(prose.strip("\n"), MAX_SECTION_TEXT_LENGTH)
    ]


def _split_to_limit(text: str, limit: int) -> list[str]:
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append("\n".join(current))
                current, length = [], 0
            pieces.append(line[:limit])
            line = line[limit:]
        # +1 for the newline that rejoins this line to the ones before it.
        if current and length + 1 + len(line) > limit:
            pieces.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += (1 if length else 0) + len(line)
    if current:
        pieces.append("\n".join(current))
    return [piece for piece in pieces if piece.strip()]
