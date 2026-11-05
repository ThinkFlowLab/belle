#!/usr/bin/env python3
"""Print one hardcoded Slack-mrkdwn fixture, selected by ``--type``.

Every fixture below is a fixed string (or built from a fixed, non-random range
of numbers) chosen to land on one side or the other of a specific rule in the
Slack rich-block renderer: the row count that turns a ``table`` into a
``data_table``, the column that must sort as numbers, the fence language that
decides whether a code block is drawn or shown, the block types a hand-written
fence may carry, the separate refusal of anything a reader could click, and the
two-chart ceiling a message may not cross locally. Nothing here is generated
from live data, and the script takes no input beyond the selector -- no network,
no state file, no scratch directory.

The fence vocabulary the negative cases turn on, in full::

    ```mermaid      drawn as a diagram
    ```vega-lite    drawn as a chart, however this connector can draw it
    ```blockkit     drawn as the Block Kit blocks its JSON describes
    ```slack-raw    never drawn; shown as the source it holds
    ```             an unnamed fence, likewise never drawn

Charts reach Slack from four of those -- a mermaid ``pie``, a mermaid
``xychart-beta``, a ``vega-lite`` spec, and hand-written ``blockkit`` -- and
each of the four has a fixture here. Every parser declines a shape it does not
recognise rather than guessing at it, so two fixtures below carry a shape that
must decline and be shown as source.

Usage::

    probe.py --type <kind>

Print the available kinds and exit non-zero when ``--type`` is missing or
unrecognised, so a caller who mistypes one is not left guessing.
"""

from __future__ import annotations

import argparse
import json
import sys

# --- tables -----------------------------------------------------------------

# Five data rows: under the renderer's threshold, so this stays a plain
# ``table`` rendered whole -- no pager.
_TABLE_SMALL = """Here is a short roster.

| Name | Role |
| --- | --- |
| Ada | Engineer |
| Grace | Engineer |
| Alan | Researcher |
| Katherine | Mathematician |
| Margaret | Programmer |
"""

# Twenty-two data rows: over the threshold, so this becomes a paginated
# ``data_table``. The line right above the table is an ordinary sentence, not
# a heading, so the caption falls back to the renderer's generic word.
_TABLE_LARGE_HEADER = "| Row | Sample |\n| --- | --- |\n"
_TABLE_LARGE_BODY = "\n".join(
    f"| {index} | Sample {index} |" for index in range(1, 23)
)
_TABLE_LARGE = (
    "Full sample listing follows, unfiltered.\n\n"
    f"{_TABLE_LARGE_HEADER}{_TABLE_LARGE_BODY}\n"
)

# Twenty-one data rows under a Markdown heading, to exercise caption
# derivation: the heading immediately above the table becomes the
# ``data_table``'s caption.
_TABLE_HEADING_BODY = "\n".join(
    f"| {index} | Item {index} |" for index in range(1, 22)
)
_TABLE_HEADING = (
    "## Fixture Roster\n\n"
    "| Row | Item |\n| --- | --- |\n"
    f"{_TABLE_HEADING_BODY}\n"
)

# Twenty-four data rows with a numeric ID column running 1 through 24, which
# straddles the point where alphabetical and numeric order disagree (\"10\"
# sorts before \"9\"). Every cell in the ID column is a bare integer, so the
# column is emitted as ``raw_number`` and the client sorts it as numbers.
_TABLE_NUMERIC_BODY = "\n".join(
    f"| {index} | Metric {index} |" for index in range(1, 25)
)
_TABLE_NUMERIC = (
    "## Fixture Metrics\n\n"
    "| ID | Label |\n| --- | --- |\n"
    f"{_TABLE_NUMERIC_BODY}\n"
)

# --- charts -------------------------------------------------------------

# A mermaid fence with nothing beside it. Naming the language is the whole
# request: no marker, no second word, nothing in the reply around it.
_PIE_SOURCE = """pie showData
    title Fixture pie chart
    "Alpha" : 42
    "Beta" : 17
    "Gamma" : 9
    "Delta" : 32"""

_CHART_PIE = (
    "Distribution for the fixture period.\n\n"
    f"```mermaid\n{_PIE_SOURCE}\n```\n"
)

# ``x_label`` and ``y_label`` go inside ``axis_config``, beside ``categories``,
# and nowhere else. Written as siblings of ``series`` instead they do not merely
# go unread: Slack refuses the whole message with
# ``invalid_blocks: failed to match exactly one allowed schema``. This fixture
# and chart-area below had them at the top level and so had never once rendered
# on a live channel -- each was refused and fell back to plain text, which looks
# from the outside exactly like a renderer that declined. Both spellings were
# posted to a real channel to settle it.
_CHART_BAR_PAYLOAD = {
    "type": "data_visualization",
    "title": "Fixture bar chart",
    "chart": {
        "type": "bar",
        "series": [
            {
                "name": "This run",
                "data": [
                    {"label": "Mon", "value": 4},
                    {"label": "Tue", "value": 7},
                    {"label": "Wed", "value": 3},
                ],
            }
        ],
        "axis_config": {
            "categories": ["Mon", "Tue", "Wed"],
            "x_label": "Day",
            "y_label": "Count",
        },
    },
}
_CHART_BAR = (
    "Daily counts for the fixture period.\n\n"
    f"```blockkit\n{json.dumps(_CHART_BAR_PAYLOAD, indent=2)}\n```\n"
)

_CHART_AREA_PAYLOAD = {
    "type": "data_visualization",
    "title": "Fixture area chart",
    "chart": {
        "type": "area",
        "series": [
            {
                "name": "Series A",
                "data": [
                    {"label": "Q1", "value": 12},
                    {"label": "Q2", "value": 18},
                    {"label": "Q3", "value": 9},
                ],
            },
            {
                "name": "Series B",
                "data": [
                    {"label": "Q1", "value": 5},
                    {"label": "Q2", "value": 11},
                    {"label": "Q3", "value": 14},
                ],
            },
        ],
        "axis_config": {
            "categories": ["Q1", "Q2", "Q3"],
            "x_label": "Quarter",
            "y_label": "Units",
        },
    },
}
_CHART_AREA = (
    "Two-series comparison for the fixture period.\n\n"
    f"```blockkit\n{json.dumps(_CHART_AREA_PAYLOAD, indent=2)}\n```\n"
)

# --- charts from the portable fences --------------------------------------

# mermaid's own bar chart, in a plain ``mermaid`` fence. Nothing beside the
# fence asks for it: the language is the whole request, exactly as it is for
# the pie above.
_CHART_XY_BAR = """Fixture throughput by day.

```mermaid
xychart-beta
    title "Fixture throughput"
    x-axis "Day" [Mon, Tue, Wed, Thu]
    y-axis "Runs" 0 --> 10
    bar [4, 7, 3, 9]
```
"""

# Two named plots, which mermaid puts in a legend and Slack turns into two
# series. Also the ``line`` chart type, which the block's own summary sentence
# omits and which was confirmed by posting one to a live channel.
_CHART_XY_LINE = """Fixture latency percentiles across the run.

```mermaid
xychart-beta
    title "Fixture latency"
    x-axis [Mon, Tue, Wed, Thu]
    y-axis "ms"
    line "P50" [12, 15, 11, 14]
    line "P99" [88, 94, 76, 91]
```
"""

# A Vega-Lite bar chart. The overlap with the blockkit fence above is the
# point: this says "here is a chart specification, render it however you can"
# rather than "I know this is Slack, draw exactly this", so the same fence
# travels to a renderer that has never heard of Block Kit.
_CHART_VEGA_BAR_SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "title": "Fixture runs by day",
    "mark": "bar",
    "data": {
        "values": [
            {"day": "Mon", "runs": 4},
            {"day": "Tue", "runs": 7},
            {"day": "Wed", "runs": 3},
            {"day": "Thu", "runs": 9},
        ]
    },
    "encoding": {
        "x": {"field": "day", "type": "nominal", "title": "Day"},
        "y": {"field": "runs", "type": "quantitative", "title": "Runs"},
    },
}
_CHART_VEGA_BAR = (
    "Daily counts, as a chart specification rather than as Block Kit.\n\n"
    f"```vega-lite\n{json.dumps(_CHART_VEGA_BAR_SPEC, indent=2)}\n```\n"
)

# An area chart with two series, split by a ``color`` channel. Area is the one
# chart type no mermaid diagram reaches -- mermaid has no area chart of any
# kind -- so vega-lite and blockkit are the only two fences that draw one.
_CHART_VEGA_AREA_SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "title": "Fixture users by tier",
    "mark": "area",
    "data": {
        "values": [
            {"day": "Mon", "users": 12000, "tier": "Free"},
            {"day": "Tue", "users": 13500, "tier": "Free"},
            {"day": "Wed", "users": 15200, "tier": "Free"},
            {"day": "Mon", "users": 4500, "tier": "Paid"},
            {"day": "Tue", "users": 4800, "tier": "Paid"},
            {"day": "Wed", "users": 5100, "tier": "Paid"},
        ]
    },
    "encoding": {
        "x": {"field": "day", "type": "ordinal", "title": "Day"},
        "y": {"field": "users", "type": "quantitative", "title": "Users"},
        "color": {"field": "tier", "type": "nominal"},
    },
}
_CHART_VEGA_AREA = (
    "Two tiers over the fixture period, layered as areas.\n\n"
    f"```vega-lite\n{json.dumps(_CHART_VEGA_AREA_SPEC, indent=2)}\n```\n"
)

# Vega-Lite's pie, which is an ``arc`` mark -- there is no ``pie`` mark. The
# value comes from ``theta`` and the slice label from ``color``.
_CHART_VEGA_PIE_SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "title": "Fixture status split",
    "mark": "arc",
    "data": {
        "values": [
            {"status": "Done", "count": 3},
            {"status": "In progress", "count": 2},
            {"status": "Blocked", "count": 1},
        ]
    },
    "encoding": {
        "theta": {"field": "count", "type": "quantitative"},
        "color": {"field": "status", "type": "nominal"},
    },
}
_CHART_VEGA_PIE = (
    "The same status split, as an arc mark rather than a mermaid pie.\n\n"
    f"```vega-lite\n{json.dumps(_CHART_VEGA_PIE_SPEC, indent=2)}\n```\n"
)

# Three data_visualization blocks in one fence. Slack's own ceiling is two per
# message; the renderer is expected to decline locally rather than let Slack
# refuse the post, so this whole message should arrive as plain text with the
# fence visible as source.
_CHART_TRIPLE_PAYLOAD = [
    {**_CHART_BAR_PAYLOAD, "title": "Fixture chart one"},
    {**_CHART_BAR_PAYLOAD, "title": "Fixture chart two"},
    {**_CHART_BAR_PAYLOAD, "title": "Fixture chart three"},
]
_CHART_TRIPLE = (
    "Three charts in one fence, deliberately one over Slack's own cap.\n\n"
    f"```blockkit\n{json.dumps(_CHART_TRIPLE_PAYLOAD, indent=2)}\n```\n"
)

# --- block types ----------------------------------------------------------

# A plain ``section``: no chart, no table, nothing interactive. Whether it
# renders is decided entirely by the operator's block-type allow-list, which
# restricts nothing unless it is configured -- so on a default channel this
# arrives as a rendered section, and on one whose allow-list omits ``section``
# it arrives as source.
_SECTION_PLAIN_PAYLOAD = {
    "type": "section",
    "text": {
        "type": "mrkdwn",
        "text": "*Fixture section*\nA hand-written block carrying nothing to click.",
    },
}
_SECTION_PLAIN = (
    "A hand-written section, which no allow-list refuses by default.\n\n"
    f"```blockkit\n{json.dumps(_SECTION_PLAIN_PAYLOAD, indent=2)}\n```\n"
)

# --- shown as source, not drawn -------------------------------------------

# The same pie source as chart-pie, in a ``slack-raw`` fence. This is the
# escape hatch: the author is showing a reader what the diagram is written as,
# and the renderer must not draw it.
_RAW_MERMAID = (
    "The same diagram, shown as the source it is written in.\n\n"
    f"```slack-raw\n{_PIE_SOURCE}\n```\n"
)

# The same pie source again, in a fence that names no language at all. An
# unnamed fence is source, exactly as a named one that is not mermaid or
# blockkit would be.
_BARE_MERMAID = (
    "The same diagram again, in a fence naming no language.\n\n"
    f"```\n{_PIE_SOURCE}\n```\n"
)

# The chart-bar payload in a ``slack-raw`` fence, shown as source.
_RAW_BLOCKKIT = (
    "Block Kit JSON, shown as source rather than drawn.\n\n"
    f"```slack-raw\n{json.dumps(_CHART_BAR_PAYLOAD, indent=2)}\n```\n"
)

# The same payload in a ``json`` fence. A named language that is not one of
# the two rendering ones is source, which is what a fence is usually for.
_JSON_FENCE = (
    "Block Kit JSON in an ordinary json fence.\n\n"
    f"```json\n{json.dumps(_CHART_BAR_PAYLOAD, indent=2)}\n```\n"
)

# --- refused --------------------------------------------------------------

# A hand-authored ``section`` carrying a button. The block type is not what
# stops this -- nothing restricts ``section`` by default, and section-plain
# above proves it renders. The interactive check is what stops it, and it is a
# separate control that defaults to refusing: a reader cannot tell a button a
# reply drew from a real approval prompt in the same channel.
_NEG_SECTION_BUTTON_PAYLOAD = {
    "type": "section",
    "text": {"type": "mrkdwn", "text": "Approve this fixture?"},
    "accessory": {
        "type": "button",
        "text": {"type": "plain_text", "text": "Approve"},
        "action_id": "fixture_approve",
    },
}
_NEG_SECTION_BUTTON = (
    "A hand-written approval prompt that should never reach the channel.\n\n"
    f"```blockkit\n{json.dumps(_NEG_SECTION_BUTTON_PAYLOAD, indent=2)}\n```\n"
)

# A ``data_table`` carrying an ``action_id`` field it has no business having.
# Nothing about the block's type is objectionable; the recursive search for an
# interactive field is what catches it, which is the half of the interactive
# check that a bare button would not exercise.
_NEG_HIDDEN_ACTION_ID_PAYLOAD = {
    "type": "data_table",
    "caption": "Fixture",
    "rows": [[{"type": "raw_text", "text": "x"}]],
    "action_id": "fixture_hidden",
}
_NEG_HIDDEN_ACTION_ID = (
    "An inert block type with an interactive field hidden inside it.\n\n"
    f"```blockkit\n{json.dumps(_NEG_HIDDEN_ACTION_ID_PAYLOAD, indent=2)}\n```\n"
)

# A Vega-Lite mark Slack has no equivalent for. A scatter needs a continuous
# x-axis and ``data_visualization`` has not got one, so the spec is declined
# rather than redrawn as the nearest chart Slack does have. The fence stays
# visible as the specification that was written, which is the whole failure
# mode: the reader sees the spec instead of a chart that disagrees with it.
_NEG_VEGA_SCATTER_SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "title": "Fixture scatter",
    "mark": "point",
    "data": {
        "values": [
            {"latency": 12, "throughput": 4},
            {"latency": 31, "throughput": 9},
            {"latency": 22, "throughput": 6},
        ]
    },
    "encoding": {
        "x": {"field": "latency", "type": "quantitative"},
        "y": {"field": "throughput", "type": "quantitative"},
    },
}
_NEG_VEGA_SCATTER = (
    "A scatter plot, which Slack cannot draw at all.\n\n"
    f"```vega-lite\n{json.dumps(_NEG_VEGA_SCATTER_SPEC, indent=2)}\n```\n"
)

# A malformed xychart: three categories and two values. Slack requires exactly
# one data point for every category and refuses a series that skips one, and
# filling the gap with a zero the author never wrote would be a wrong chart
# rather than an incomplete one -- so the whole diagram declines and stays on
# screen as source. The same thing a future mermaid release would produce if it
# moved the syntax, which is why the beta diagram type is safe to read at all.
_NEG_XY_BROKEN = """A chart whose plot does not line up with its categories.

```mermaid
xychart-beta
    title "Fixture mismatch"
    x-axis [Mon, Tue, Wed]
    bar [4, 7]
```
"""

# Malformed JSON beside a valid, short table. The fence should degrade to
# source on its own -- json.loads fails on it -- while the table beside it
# still renders normally: one bad fence costs only itself.
_NEG_MALFORMED_JSON = """A fixture with a broken fence beside a good table.

```blockkit
{"type": "data_table", "rows": [
```

| Name | Role |
| --- | --- |
| Ada | Engineer |
| Grace | Engineer |
"""

# --- mixed -----------------------------------------------------------------

_MIXED_REPORT = """Weekly fixture summary follows.

## Highlights

Three items moved this period, and the numbers below cover the full set.

```mermaid
pie showData
    title Fixture status split
    "Done" : 3
    "In progress" : 2
    "Blocked" : 1
```

## Full roster

| Row | Item | Status |
| --- | --- | --- |
| 1 | Alpha | Done |
| 2 | Beta | Done |
| 3 | Gamma | Done |
| 4 | Delta | In progress |
| 5 | Epsilon | In progress |
| 6 | Zeta | Blocked |
"""

FIXTURES: dict[str, str] = {
    "table-small": _TABLE_SMALL,
    "table-large": _TABLE_LARGE,
    "table-heading": _TABLE_HEADING,
    "table-numeric": _TABLE_NUMERIC,
    "chart-pie": _CHART_PIE,
    "chart-bar": _CHART_BAR,
    "chart-area": _CHART_AREA,
    "chart-triple": _CHART_TRIPLE,
    "chart-xy-bar": _CHART_XY_BAR,
    "chart-xy-line": _CHART_XY_LINE,
    "chart-vega-bar": _CHART_VEGA_BAR,
    "chart-vega-area": _CHART_VEGA_AREA,
    "chart-vega-pie": _CHART_VEGA_PIE,
    "section-plain": _SECTION_PLAIN,
    "mermaid-raw": _RAW_MERMAID,
    "mermaid-bare": _BARE_MERMAID,
    "blockkit-raw": _RAW_BLOCKKIT,
    "json-fence": _JSON_FENCE,
    "section-button": _NEG_SECTION_BUTTON,
    "hidden-action-id": _NEG_HIDDEN_ACTION_ID,
    "malformed-json": _NEG_MALFORMED_JSON,
    "vega-scatter": _NEG_VEGA_SCATTER,
    "xy-broken": _NEG_XY_BROKEN,
    "mixed-report": _MIXED_REPORT,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", dest="kind", choices=sorted(FIXTURES), default=None)
    args = parser.parse_args(argv)

    if args.kind is None:
        print("usage: probe.py --type <kind>", file=sys.stderr)
        print("available kinds:", file=sys.stderr)
        for kind in sorted(FIXTURES):
            print(f"  {kind}", file=sys.stderr)
        return 1

    sys.stdout.write(FIXTURES[args.kind])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
