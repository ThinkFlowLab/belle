"""Tests for Slack text normalization and splitting."""

from belle_agent.platforms.slack.text import (
    MAX_SLACK_TEXT_LENGTH,
    fenced_regions_stripped,
    normalize_slack_mrkdwn,
    split_text,
)


def test_normalize_slack_mrkdwn_converts_common_markdown() -> None:
    markdown = (
        "# Slack Channel Digest\n"
        "## **Topic 1**\n"
        "**Summary**: concise overview\n"
        "- **Fact**: A result ([Alice](https://example.slack.com/archives/C1/"
        "p123?thread_ts=120&cid=C1)).\n"
        "+ **Recommendation**: Capture the design.\n"
        "* plain bullet\n"
        "__init__\n"
        "#hashtag\n"
        "---"
    )

    assert normalize_slack_mrkdwn(markdown) == (
        "*Slack Channel Digest*\n"
        "*Topic 1*\n"
        "*Summary*: concise overview\n"
        "\u2022 *Fact:* A result "
        "(<https://example.slack.com/archives/C1/"
        "p123?thread_ts=120&cid=C1|Alice>).\n"
        "\u2022 *Recommendation:* Capture the design.\n"
        "\u2022 plain bullet\n"
        "__init__\n"
        "#hashtag\n"
        "---"
    )
    normalized = normalize_slack_mrkdwn(markdown)
    assert normalize_slack_mrkdwn(normalized) == normalized


def test_normalize_slack_mrkdwn_preserves_code_and_native_tokens() -> None:
    native_source = "<https://example.slack.com/archives/C1/p123?thread_ts=120&cid=C1|source>"
    markdown = (
        "Use `**raw** [x](https://example.com)` with <@U123>.\n"
        "```markdown\n"
        "# untouched\n"
        "**raw** [x](https://example.com)\n"
        "```\n"
        f"{native_source}"
    )

    normalized = normalize_slack_mrkdwn(markdown)

    assert normalized == markdown


def test_normalize_slack_mrkdwn_keeps_protected_tokens_in_line_context() -> None:
    source = "<https://example.slack.com/archives/C1/p123?thread_ts=120&cid=C1|source>"
    markdown = f"## Use `run()` safely\n## Source {source}\nPrefix <@U1> - not a list\n  - nested"

    assert normalize_slack_mrkdwn(markdown) == (
        f"*Use `run()` safely*\n*Source {source}*\nPrefix <@U1> - not a list\n  \u2022 nested"
    )


def test_normalize_slack_mrkdwn_handles_headings_and_balanced_urls() -> None:
    markdown = "## Technical **Findings**\n# C#\n# Title ###\n[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"

    assert normalize_slack_mrkdwn(markdown) == (
        "*Technical Findings*\n*C#*\n*Title*\n<https://en.wikipedia.org/wiki/Foo_(bar)|wiki>"
    )


def test_normalize_slack_mrkdwn_does_not_cross_plain_brackets() -> None:
    markdown = "Use [draft] then [source](https://example.com).\n[Fact] A claim ([source](https://example.com))."
    expected = "Use [draft] then <https://example.com|source>.\n[Fact] A claim (<https://example.com|source>)."

    assert normalize_slack_mrkdwn(markdown) == expected


def test_normalize_slack_mrkdwn_closes_fence_only_on_delimiter_line() -> None:
    markdown = '```python\nprint("```")\n## untouched\n```\n## converted'

    assert normalize_slack_mrkdwn(markdown) == ('```python\nprint("```")\n## untouched\n```\n*converted*')


def test_normalize_slack_mrkdwn_preserves_unclosed_fenced_code() -> None:
    markdown = "```markdown\n# untouched\n**raw** [x](https://example.com)"

    assert normalize_slack_mrkdwn(markdown) == markdown


def test_fenced_regions_stripped_keeps_only_prose() -> None:
    text = 'Brief\n```blockkit\n{"type": "section"}\n```\nDetail'

    assert fenced_regions_stripped(text) == "Brief\nDetail"


def test_split_text_prefers_paragraph_boundaries() -> None:
    half = MAX_SLACK_TEXT_LENGTH // 2
    first_paragraph = "a" * half
    second_paragraph = "b" * (half - 100)
    final_paragraph = "c" * 500

    assert split_text(f"{first_paragraph}\n\n{second_paragraph}\n\n{final_paragraph}") == [
        f"{first_paragraph}\n\n{second_paragraph}",
        final_paragraph,
    ]


def test_split_text_does_not_break_slack_link_near_limit() -> None:
    prefix = "x" * (MAX_SLACK_TEXT_LENGTH - 50)
    slack_link = "<https://github.com/example/repo/issues/123|Issue #123>"

    chunks = split_text(f"{prefix}\n{slack_link}\nMore detail")

    assert chunks == [prefix, f"{slack_link}\nMore detail"]


def test_split_text_respects_first_update_limit() -> None:
    assert split_text("a" * 30, first_limit=10) == ["a" * 10, "a" * 20]
