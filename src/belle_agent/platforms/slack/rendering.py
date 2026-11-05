"""Render Slack messages with optional blocks and bounded mrkdwn."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from belle_agent.platforms.rendering import RenderedPart, measure_of
from belle_agent.platforms.slack.commands import escape
from belle_agent.platforms.slack.streaming import extract_block_request
from belle_agent.platforms.slack.text import (
    normalize_slack_mrkdwn,
    preferred_split_index,
)
from belle_agent.text_split import split_text

_MEASURE = measure_of("chars")

BlocksAndKind = Callable[[str, bool | None], tuple[list[dict[str, Any]] | None, str]]


@dataclass(frozen=True)
class SlackMessage:
    """Fallback text, blocks, and block source for one message."""

    text: str
    blocks: list[dict[str, Any]] | None
    block_kind: str


class SlackRenderer:
    """Render model Markdown as Slack message parts."""

    def __init__(self, blocks_and_kind: BlocksAndKind) -> None:
        self._blocks_and_kind = blocks_and_kind

    def measure(self, markdown: str) -> int:
        """Measure message characters."""
        return _MEASURE(markdown)

    def parts(self, markdown: str, budget: int) -> list[RenderedPart[SlackMessage]]:
        """Render bounded parts after removing block markers."""
        content, block_request = extract_block_request(markdown)
        found = []
        for chunk in split_text(normalize_slack_mrkdwn(content), budget, cut=preferred_split_index):
            blocks, kind = self._blocks_and_kind(chunk, block_request)
            found.append(RenderedPart(chunk, SlackMessage(chunk, blocks, kind)))
        return found

    def cut_index(self, markdown: str, budget: int) -> int:
        """Cut outside Slack link and mention tokens."""
        return preferred_split_index(markdown, budget)

    def escape(self, text: str) -> str:
        """Escape Belle-authored text for Slack."""
        return escape(text)
