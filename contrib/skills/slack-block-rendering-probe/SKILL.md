---
name: slack-block-rendering-probe
description: Post a fixed fixture to check Slack rich-block rendering. Use only when asked to test Slack Block Kit, tables, charts, or fenced source text.
---

# Slack Block Rendering Probe

Run `<skill>/scripts/probe.py --type <kind>`. Send its stdout as your entire reply, unchanged. Do not describe, reformat, or wrap the output. The test checks how the channel renders the raw text.

Run the script without `--type` to list valid kinds.

## Fence behavior

- `mermaid`: render supported diagrams and charts.
- `vega-lite`: render supported charts.
- `blockkit`: render supported Block Kit JSON.
- `slack-raw` and unnamed fences: display their contents as source text.

Unsupported or invalid content stays visible as source text. Each fixture below names its expected result.

## Tables

- `table-small`: 5 data rows; plain table.
- `table-large`: 22 data rows; paginated `data_table` with a generic caption.
- `table-heading`: 21 data rows below a heading; `data_table` captioned from the heading.
- `table-numeric`: 24 data rows; `data_table` whose ID column sorts numerically.

## Charts

- `chart-pie`: Mermaid pie chart.
- `chart-bar`: Block Kit bar chart with `Day` and `Count` axis titles.
- `chart-area`: Block Kit area chart with two series and `Quarter` and `Units` axis titles.
- `chart-triple`: three Block Kit charts in one fence; the whole message falls back to text because Slack allows at most two charts per message.
- `chart-xy-bar`: Mermaid bar chart with `Mon=4`, `Tue=7`, `Wed=3`, `Thu=9`; Slack scales the y-axis to the data.
- `chart-xy-line`: Mermaid line chart with named `P50` and `P99` series.
- `chart-vega-bar`: Vega-Lite bar chart with the same values as `chart-xy-bar`.
- `chart-vega-area`: Vega-Lite area chart with `Free` and `Paid` series.
- `chart-vega-pie`: Vega-Lite pie chart with `Done=3`, `In progress=2`, and `Blocked=1`.

## Blocks and source text

- `section-plain`: render a noninteractive Block Kit section.
- `mermaid-raw`, `mermaid-bare`: display the Mermaid pie chart as source text.
- `blockkit-raw`, `json-fence`: display the Block Kit bar chart as JSON source text.
- `section-button`, `hidden-action-id`: refuse model-supplied interactive controls and display the source text.
- `malformed-json`: display the invalid Block Kit fence as source; render the adjacent table.
- `vega-scatter`: display the unsupported scatter chart as source.
- `xy-broken`: display the Mermaid chart with mismatched category and value counts as source.
- `mixed-report`: render the supported chart and table within the surrounding prose.
