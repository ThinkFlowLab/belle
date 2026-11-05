"""Render Matrix Markdown and HTML within an event's byte budget."""

import json
from collections.abc import Mapping
from typing import Any

from markdown_it import MarkdownIt

from belle_agent.platforms.rendering import RenderedPart, markdown_cut_index, markdown_parts

FORMAT = "org.matrix.custom.html"
# Leave room under Matrix's 65,536-byte event limit for metadata and encryption.
MESSAGE_BUDGET = {False: 60_000, True: 44_000}
# Edits include old and new content, so their budget is smaller.
STREAM_BUDGET = {False: 30_000, True: 22_000}

# Raw HTML in the text is shown as text. A client sanitizes what is left.
_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def content(text: str) -> dict[str, str]:
    """The text fields of a message: the Markdown as ``body``, and its HTML."""
    return {"body": text, "format": FORMAT, "formatted_body": _MD.render(text).strip()}


def notice(text: str, thread: str = "") -> dict[str, Any]:
    """A notice of the bot's, in ``thread`` where one is given."""
    found: dict[str, Any] = {"msgtype": "m.notice", **content(text)}
    if thread:
        found["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread}
    return found


def note(text: str, reply_to: str, thread: str = "") -> dict[str, Any]:
    """A notice of the bot's in reply to the event ``reply_to``, in ``thread`` where one is given, that notifies nobody."""
    relates: dict[str, Any] = {"m.in_reply_to": {"event_id": reply_to}}
    if thread:
        relates.update(rel_type="m.thread", event_id=thread, is_falling_back=False)
    return {"msgtype": "m.notice", **content(text), "m.mentions": {}, "m.relates_to": relates}


def edit(event_id: str, new: Mapping[str, Any]) -> dict[str, Any]:
    """An edit of the message ``event_id`` that gives it the content ``new``. A client without edits shows ``* <text>``."""
    fallback = {key: f"* {new[key]}" for key in ("body", "formatted_body") if key in new}
    return {**new, **fallback, "m.new_content": dict(new), "m.relates_to": {"rel_type": "m.replace", "event_id": event_id}}


def cut_index(text: str, budget: int) -> int:
    """Return the first part's cut index within ``budget``."""
    return markdown_cut_index(text, budget, size)


def size(text: str) -> int:
    """The budget a text takes: ``body`` and ``formatted_body`` as escaped JSON, in bytes."""
    fields = content(text)
    return sum(len(json.dumps(fields[key], ensure_ascii=False).encode()) - 2 for key in ("body", "formatted_body"))


def parts(text: str, budget: int) -> list[str]:
    """Split Markdown into parts within ``budget``."""
    return [text] if size(text) <= budget else markdown_parts(text, budget, size)


class MatrixRenderer:
    """Render Matrix message bodies and formatted HTML."""

    def measure(self, markdown: str) -> int:
        """Measure the rendered event content in bytes."""
        return size(markdown)

    def parts(self, markdown: str, budget: int) -> list[RenderedPart[dict[str, str]]]:
        """Render parts within the byte budget."""
        return [RenderedPart(part, content(part)) for part in parts(markdown, budget)]

    def cut_index(self, markdown: str, budget: int) -> int:
        """Return the first part's cut index."""
        return cut_index(markdown, budget)

    def escape(self, text: str) -> str:
        """Leave raw text to the Markdown renderer."""
        return text


RENDERER = MatrixRenderer()
