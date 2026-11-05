"""Slack text conversion and message splitting."""

import re

from belle_agent.text_split import split_text as split_long_text

MAX_SLACK_TEXT_LENGTH = 38000
MAX_SLACK_POST_FALLBACK_LENGTH = 40000
MAX_SLACK_UPDATE_TEXT_LENGTH = 4000

_INLINE_CODE_OR_SLACK_TOKEN_RE = re.compile(r"(?P<code>(?P<ticks>`+)[^\n]*?(?P=ticks))|(?P<slack><[^>\n]+>)")
_MARKDOWN_BOLD_RE = re.compile(r"(?<!\*)\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\*)")
_MARKDOWN_HEADING_RE = re.compile(r"^(?P<indent>[ \t]{0,3})#{1,6}[ \t]+(?P<title>.+?)$")
_MARKDOWN_CLOSING_HASHES_RE = re.compile(r"[ \t]+#+[ \t]*$")
_MARKDOWN_LABELED_BULLET_RE = re.compile(
    r"^(?P<indent>[ \t]*)[-+*][ \t]+\*"
    r"(?P<label>Fact|Inference(?:\s*\([^)]*\))?|"
    r"Recommendation|Proposal|Action Item)\*:[ \t]*(?P<body>.*)$",
    re.IGNORECASE,
)
_MARKDOWN_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)[-+*][ \t]+(?P<body>.*)$")
MARKDOWN_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")


def clamp(text: str, limit: int) -> str:
    """Shorten text to a Slack field limit, including the ellipsis."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def markdown_fence_closes(line: str, marker: str) -> bool:
    """Return whether a line closes the active fence."""
    closing = MARKDOWN_FENCE_CLOSE_RE.match(line)
    return bool(closing and closing.group("fence")[0] == marker[0] and len(closing.group("fence")) >= len(marker))


def markdown_fence_state(line: str, marker: str) -> tuple[str, bool]:
    """Return the next marker and whether this line belongs to a fence."""
    if marker:
        return ("" if markdown_fence_closes(line, marker) else marker), True
    opening = MARKDOWN_FENCE_OPEN_RE.match(line)
    return (opening.group("fence"), True) if opening else ("", False)


def fenced_regions_stripped(text: str) -> str:
    """Remove fenced code blocks, including their delimiter lines."""
    kept: list[str] = []
    marker = ""
    for line in text.splitlines():
        marker, fenced = markdown_fence_state(line, marker)
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


def markdown_fence_span(lines: list[str], start: int, marker: str) -> tuple[list[str], int, bool]:
    """Return the fence body, consumed line count, and closed flag."""
    for index in range(start + 1, len(lines)):
        if markdown_fence_closes(lines[index], marker):
            return lines[start + 1 : index], index - start + 1, True
    return lines[start + 1 :], len(lines) - start, False


def _markdown_link_end(content: str, start: int, *, forbidden: str = "") -> int:
    """Find a link's closing parenthesis after its URL, allowing nested pairs."""
    depth = 0
    for index in range(start, len(content)):
        character = content[index]
        if character.isspace() or character in forbidden:
            return -1
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return index
            depth -= 1
    return -1


def convert_markdown_links(content: str) -> str:
    """Convert valid HTTP(S) Markdown links to Slack tokens."""
    converted: list[str] = []
    index = 0
    while index < len(content):
        if content[index] != "[":
            converted.append(content[index])
            index += 1
            continue

        label_end = content.find("]", index + 1)
        if label_end < 0:
            converted.append(content[index:])
            break
        if label_end + 1 >= len(content) or content[label_end + 1] != "(":
            converted.append(content[index])
            index += 1
            continue

        label = content[index + 1 : label_end]
        url_start = label_end + 2
        remaining = content[url_start:].lower()
        if (
            not label
            or any(character in label for character in "[]<>|")
            or not remaining.startswith(("http://", "https://"))
        ):
            converted.append(content[index])
            index += 1
            continue

        link_end = _markdown_link_end(content, url_start, forbidden="<>|")
        if link_end < 0:
            converted.append(content[index])
            index += 1
            continue

        converted.append(f"<{content[url_start:link_end]}|{label}>")
        index = link_end + 1

    return "".join(converted)


def normalize_markdown_line(content: str) -> str:
    """Convert a line while preserving code spans and Slack tokens."""
    protected: list[tuple[str, str]] = []

    def preserve(match: re.Match[str]) -> str:
        placeholder = f"\x00JWS{len(protected)}\x00"
        protected.append((placeholder, match.group(0)))
        return placeholder

    working = _INLINE_CODE_OR_SLACK_TOKEN_RE.sub(preserve, content)
    heading = _MARKDOWN_HEADING_RE.match(working)
    if heading:
        title = _MARKDOWN_CLOSING_HASHES_RE.sub("", heading.group("title")).strip()
        title = convert_markdown_links(title)
        title = _MARKDOWN_BOLD_RE.sub(r"\1", title)
        working = f"{heading.group('indent')}*{title}*"
    else:
        working = convert_markdown_links(working)
        working = _MARKDOWN_BOLD_RE.sub(r"*\1*", working)
        labeled_bullet = _MARKDOWN_LABELED_BULLET_RE.match(working)
        if labeled_bullet:
            working = f"{labeled_bullet['indent']}• *{labeled_bullet['label']}:* {labeled_bullet['body']}"
        else:
            bullet = _MARKDOWN_BULLET_RE.match(working)
            if bullet:
                working = f"{bullet.group('indent')}• {bullet.group('body')}"

    for placeholder, value in protected:
        working = working.replace(placeholder, value)
    return working


def normalize_slack_mrkdwn(content: str) -> str:
    """Convert supported Markdown outside fenced code blocks."""
    normalized_lines: list[str] = []
    marker = ""
    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        marker, fenced = markdown_fence_state(body, marker)
        normalized_lines.append(line if fenced else normalize_markdown_line(body) + ending)
    return "".join(normalized_lines)


def preferred_split_index(content: str, limit: int) -> int:
    """Choose a split without breaking a Slack token."""
    lower_bound = max(1, limit // 2)
    for separator in ("\n\n", "\n", " "):
        split_at = content.rfind(separator, lower_bound, limit + 1)
        if split_at < 0:
            continue

        last_open = content.rfind("<", 0, split_at)
        last_close = content.rfind(">", 0, split_at)
        if last_open > last_close and last_open >= lower_bound:
            split_at = last_open
        return split_at
    return limit


def split_text(content: str, first_limit: int = 0) -> list[str]:
    """Split text into Slack-sized messages."""
    return split_long_text(content, MAX_SLACK_TEXT_LENGTH, first_limit, cut=preferred_split_index)


__all__ = [
    "MARKDOWN_FENCE_CLOSE_RE",
    "MARKDOWN_FENCE_OPEN_RE",
    "MAX_SLACK_TEXT_LENGTH",
    "MAX_SLACK_UPDATE_TEXT_LENGTH",
    "convert_markdown_links",
    "markdown_fence_closes",
    "markdown_fence_span",
    "markdown_fence_state",
    "normalize_markdown_line",
    "normalize_slack_mrkdwn",
    "preferred_split_index",
    "split_text",
]
