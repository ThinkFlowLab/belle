"""Translate one turn's ACP ``session/update`` notifications into Belle's turn events.

Chunk rules: the text chunks of one agent message are its text in order. A chunk with another ``messageId``
starts a new message, and so does text or a tool call after a tool result, which the model reads in a new
request: a ``step`` comes first, and the final is the text since the last ``step``. A tool call after text gets
a ``step`` first too, because that text is no part of the final. A tool call update replaces
the fields it names. Thought chunks are peek-only text.
"""

from dataclasses import dataclass, field
from typing import Any

from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent

ENDED = ("completed", "failed")


@dataclass
class TurnText:
    """What one turn has written so far, by the chunk rules."""

    #: The text since the last ``step``.
    current: list[str] = field(default_factory=list)
    message_id: str | None = None
    #: A tool result came since the last ``step``, so the next text or tool call is a new model request.
    after_tool: bool = False
    #: Tool call id to its title, and the ids whose result was reported.
    titles: dict[str, str] = field(default_factory=dict)
    reported: set[str] = field(default_factory=set)

    @property
    def final(self) -> str:
        """The text since the last ``step``: the reply as far as it is written."""
        return "".join(self.current)


def _text(content: Any) -> str | None:
    return content.text if isinstance(content, TextContentBlock) else None


def _result(update: ToolCallStart | ToolCallProgress) -> str:
    """The text parts of a tool call's content."""
    parts = (_text(item.content) for item in update.content or () if isinstance(item, ContentToolCallContent))
    return "\n".join(part for part in parts if part)


def translate(update: Any, turn: TurnText) -> list[TurnEvent]:
    """The events one update gives, in order. An update Belle shows nothing of gives none."""
    if isinstance(update, AgentMessageChunk):
        text = _text(update.content)
        if text is None:
            return []
        new_message = update.message_id is not None and turn.message_id not in (None, update.message_id)
        events = _step(turn, force=new_message)
        turn.message_id = update.message_id or turn.message_id
        turn.current.append(text)
        return [*events, TurnEvent(EVENT_TEXT_DELTA, text=text)]
    if isinstance(update, AgentThoughtChunk):
        text = _text(update.content)
        return [TurnEvent(EVENT_TEXT_DELTA, text=text, data={"thinking": True})] if text else []
    if isinstance(update, ToolCallStart):
        # Text before a call is no part of the final, so a step ends it.
        events = _step(turn, force=bool(turn.current))
        turn.titles[update.tool_call_id] = update.title
        arguments = update.raw_input if isinstance(update.raw_input, dict) else {}
        data = {"name": update.title, "call_id": update.tool_call_id, "arguments": arguments}
        return [*events, TurnEvent(EVENT_TOOL_CALLED, data=data), *_ended(update, turn)]
    if isinstance(update, ToolCallProgress):
        if update.title:
            turn.titles[update.tool_call_id] = update.title
        return _ended(update, turn)
    return []


def _step(turn: TurnText, *, force: bool = False) -> list[TurnEvent]:
    """A ``step`` where the model is asked again: after a tool result, or where a new message starts."""
    if not (turn.after_tool or force):
        return []
    turn.after_tool = False
    turn.current.clear()
    return [TurnEvent(EVENT_STEP)]


def _ended(update: ToolCallStart | ToolCallProgress, turn: TurnText) -> list[TurnEvent]:
    """The result of a tool call whose status says it ended, once."""
    if update.status not in ENDED or update.tool_call_id in turn.reported:
        return []
    turn.reported.add(update.tool_call_id)
    turn.after_tool = True
    data: dict[str, Any] = {"name": turn.titles.get(update.tool_call_id, ""), "call_id": update.tool_call_id}
    if update.status == "failed":
        data["failed"] = True
    return [TurnEvent(EVENT_TOOL_RESULT, text=_result(update), data=data)]
