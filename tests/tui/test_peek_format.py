"""Block-level markdown splitting for the live peek pane."""

from io import StringIO

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from belle_agent.tui.peek_format import (
    PEEK_KIND_QUESTION,
    PEEK_KIND_STEP,
    PEEK_KIND_TEXT,
    PEEK_KIND_THINKING,
    PEEK_KIND_TOOL_CALL,
    PEEK_KIND_TOOL_RESULT,
    PEEK_KIND_USAGE,
    PeekPart,
    format_peek_question,
    format_peek_step,
    format_peek_tool_call,
    format_peek_tool_result,
    format_peek_usage,
    render_peek_parts,
    split_closed_markdown,
)


def _export(renderable) -> str:
    console = Console(file=StringIO(), width=60, record=True)
    console.print(renderable)
    return console.export_text()


def test_split_closed_markdown_holds_open_paragraph():
    blocks, tail = split_closed_markdown("Hello **world")
    assert blocks == []
    assert tail == "Hello **world"


def test_split_closed_markdown_closes_on_blank_line():
    blocks, tail = split_closed_markdown("Hello **world**.\n\nNext")
    assert blocks == ["Hello **world**.\n\n"]
    assert tail == "Next"


def test_split_closed_markdown_keeps_heading_with_following_block():
    blocks, tail = split_closed_markdown("## Title\n\nParagraph one.\n\n### Sub\n\nMore")
    assert blocks == ["## Title\n\nParagraph one.\n\n"]
    assert tail == "### Sub\n\nMore"
    blocks, tail = split_closed_markdown("## Title\n\nParagraph one.\n\n### Sub\n\nMore", flush=True)
    assert blocks == ["## Title\n\nParagraph one.\n\n", "### Sub\n\nMore"]
    assert tail == ""
    # Heading with no following closed block stays in the open tail while streaming.
    blocks, tail = split_closed_markdown("## Title\n\nOpen **tail")
    assert blocks == []
    assert tail == "## Title\n\nOpen **tail"


def test_split_closed_markdown_closes_fence():
    text = "Intro\n```python\nx = 1\n```\nMore"
    blocks, tail = split_closed_markdown(text)
    assert blocks == ["Intro\n", "```python\nx = 1\n```\n"]
    assert tail == "More"


def test_split_closed_markdown_keeps_open_fence():
    blocks, tail = split_closed_markdown("```\npartial")
    assert blocks == []
    assert tail == "```\npartial"


def test_split_closed_markdown_flush_emits_tail():
    blocks, tail = split_closed_markdown("Done **here**", flush=True)
    assert blocks == ["Done **here**"]
    assert tail == ""


def test_render_peek_parts_markdown_and_plain_tail():
    from rich.padding import Padding

    # Closed prose + open fence tail; stream content is padded.
    rendered = render_peek_parts(
        [PeekPart(PEEK_KIND_TEXT, "Hello **world**.\n\n```\npartial")], flush=False,
    )
    assert isinstance(rendered, Group)
    assert isinstance(rendered.renderables[0], Padding)
    assert isinstance(rendered.renderables[0].renderable, Markdown)
    assert isinstance(rendered.renderables[-1], Padding)
    assert isinstance(rendered.renderables[-1].renderable, Text)
    assert rendered.renderables[-1].renderable.plain == "```\npartial"


def test_render_heading_spacing_not_before_first():
    rendered = render_peek_parts(
        [PeekPart(PEEK_KIND_TEXT, "## Title\n\nParagraph one.\n\n### Sub\n\nMore.")],
        flush=True,
    )
    text = "\n".join(line.rstrip() for line in _export(rendered).splitlines())
    # Indentation is visual only; heading keep-with-next spacing is preserved.
    stripped = "\n".join(line.lstrip() for line in text.splitlines())
    assert not stripped.startswith("\n")
    assert "Title\n\nParagraph one." in stripped
    assert "Paragraph one.\n\nSub" in stripped
    assert "Sub\n\nMore." in stripped


def test_render_thinking_stays_plain_and_indented():
    from rich.padding import Padding

    rendered = render_peek_parts([PeekPart(PEEK_KIND_THINKING, "reason **not** md")], flush=False)
    assert isinstance(rendered, Padding)
    assert isinstance(rendered.renderable, Text)
    assert rendered.renderable.plain == "reason **not** md"
    assert rendered.left == 2


def test_format_structural_peek_lines():
    from belle_agent.tui.peek_format import peek_tool_body

    assert format_peek_step({'label': None}) == ""
    assert format_peek_step({'label': '  '}) == ""
    assert format_peek_step({'label': 'Plan'}) == "\n· ── Plan ──\n"
    assert format_peek_tool_call({'name': 'shell', 'detail': 'list files'}) == "→ shell · list files\n"
    assert format_peek_tool_result({'name': 'shell', 'ok': True}) == "← shell · ok\n"
    assert format_peek_tool_result({'call_id': 'c1', 'ok': False, 'summary': 'denied'}) == (
        "← c1 · failed · denied\n"
    )
    assert format_peek_tool_call(
        {'name': 'shell', 'detail': 'ls', 'args_text': '{"command":"ls"}'},
    ) == "→ shell · ls  +\n"
    assert format_peek_tool_call(
        {'name': 'shell', 'detail': 'ls', 'args_text': '{"command":"ls"}'}, expanded=True,
    ) == "→ shell · ls  −\n"
    assert format_peek_tool_result(
        {'name': 'shell', 'ok': True, 'result_text': 'ok\n'},
    ) == "← shell · ok  +\n"
    assert peek_tool_body({'args_text': 'x'}, 'tool_call') == 'x'
    assert peek_tool_body({'args_text': None}, 'tool_call') is None
    from belle_agent.tui.peek_format import format_peek_tool_body_marker

    # ``└`` under the tool name (after ``→ ``); panel background starts after this.
    assert format_peek_tool_body_marker(marker_style='bold yellow').plain == '  └ '
    assert format_peek_question({'prompt_summary': 'Allow shell?', 'kind': 'approval'}) == (
        "? approval: Allow shell?\n"
    )
    assert format_peek_question({'prompt_summary': 'Which path?', 'kind': 'question'}) == "? Which path?\n"


def test_render_structural_kinds_flush_left_stream_indented():
    parts = [
        PeekPart(PEEK_KIND_STEP, format_peek_step({'label': 'Plan'})),
        PeekPart(PEEK_KIND_TOOL_CALL, format_peek_tool_call({'name': 'shell', 'detail': 'ls'})),
        PeekPart(PEEK_KIND_TOOL_RESULT, format_peek_tool_result({'name': 'shell', 'ok': True})),
        PeekPart(PEEK_KIND_QUESTION, format_peek_question({'prompt_summary': 'Go?', 'kind': 'question'})),
        PeekPart(PEEK_KIND_TEXT, "answer line\n"),
        PeekPart(PEEK_KIND_USAGE, format_peek_usage({"tokens_in": 1, "tokens_out": 2, "cost": None})),
    ]
    rendered = render_peek_parts(parts, flush=True)
    text = _export(rendered)
    assert "· ── Plan ──" in text
    assert "→ shell · ls" in text
    assert "← shell · ok" in text
    assert "? Go?" in text
    assert "# tokens 1/2" in text
    # Meta markers sit at column 0; answer text is indented.
    lines = [line for line in text.splitlines() if line.strip()]
    meta = [line for line in lines if line.lstrip()[:1] in {"·", "→", "←", "?", "#"}]
    assert meta and all(not line.startswith(" ") for line in meta)
    assert any(line.startswith("  ") and "answer" in line for line in lines)


def test_format_peek_usage_is_compact():
    from rich.padding import Padding

    assert format_peek_usage({"tokens_in": 10, "tokens_out": 2, "cost": 0.25}) == "# tokens 10/2 · cost 0.25\n"
    assert format_peek_usage({"tokens_in": None, "tokens_out": None, "cost": None}) == ""
    rendered = render_peek_parts([PeekPart(PEEK_KIND_USAGE, "# tokens 1/2\n")], flush=True)
    assert isinstance(rendered, Padding)
    assert rendered.top == 1
    assert isinstance(rendered.renderable, Text)
    assert "dim" in str(rendered.renderable.style)


def test_usage_has_blank_line_after_stream_text():
    from rich.padding import Padding

    rendered = render_peek_parts(
        [
            PeekPart(PEEK_KIND_TEXT, "final answer with no trailing blank"),
            PeekPart(PEEK_KIND_USAGE, format_peek_usage({"tokens_in": 3, "tokens_out": 1, "cost": None})),
        ],
        flush=True,
    )
    assert isinstance(rendered, Group)
    usage = rendered.renderables[-1]
    assert isinstance(usage, Padding) and usage.top == 1
    text = "\n".join(line.rstrip() for line in _export(rendered).splitlines())
    assert "final answer with no trailing blank" in text
    # Collapse console pad spaces so a blank layout row still shows as \n\n before #.
    compact = "\n".join(line.strip() for line in text.splitlines())
    assert "\n\n# tokens 3/1" in compact
