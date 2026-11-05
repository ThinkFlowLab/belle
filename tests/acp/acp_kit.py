"""Helpers of the ACP backend checks."""

import json
from typing import Any

from scripted_agent import SESSION_META

from belle_agent.backend import TurnEvent, TurnRequest
from belle_agent.integrations.acp import AcpBackend
from belle_agent.integrations.acp.settings import Profile, Settings
from belle_agent.tools.host import BoundTurn, ToolHost, request_metadata

TOKEN = "test-tools-token"


class Toolkit:
    """One tool, ``post``, that records the conversation its call was bound to."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def get_tool_definitions(self):
        schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        return [{"name": "post", "description": "post something", "input_params": schema}]

    async def post(self, text: str, conversation: str | None = None) -> str:
        self.posts.append((text, request_metadata().get("conversation", "")))
        return json.dumps({"ok": True})


def settings(socket: str, *, header: str = "", **profile) -> Settings:
    profile.setdefault("connection", "per_conversation" if header else "shared")
    profile.setdefault("tools_mount", "agent" if header else "session")
    return Settings(
        profile=Profile(
            name="test",
            socket=socket,
            cwd="/tmp",
            binding_header=header,
            binding_meta="" if header else SESSION_META,
            **profile,
        ),
        tools_bind="127.0.0.1",
        tools_port=0,
        tools_url="",
        tools_token=TOKEN if header else "",
    )


def bind(tools: ToolHost, conversation: str, request_id: str = "r1") -> None:
    """Open a conversation to tool calls, as the host does for one turn."""
    tools.bind(conversation, BoundTurn(request_id, metadata={"conversation": conversation}))


async def collect(
    backend: AcpBackend, conversation: str = "c1", request_id: str = "r1", text: str = "hi", **fields: Any
) -> list[TurnEvent]:
    """Run one turn to its end and return its events."""
    return [event async for event in backend.run_turn(TurnRequest(conversation, request_id, text, **fields))]


def kinds(events: list[TurnEvent]) -> list[str]:
    return [event.kind for event in events]
