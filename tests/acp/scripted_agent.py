"""A scripted ACP v1 agent on a Unix socket, in the test's own event loop.

Each connection is one agent process as a relay would start it: with ``binding_line``, its first line is the
binding and the rest is ACP. Sessions are kept by the server, so a new connection can load or resume them, as a
harness with a session store does. A test scripts each prompt with ``on_prompt``.
"""

import asyncio
import contextlib
import itertools
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from acp import run_agent, update_agent_message_text, update_agent_thought_text
from acp.schema import (
    AgentCapabilities,
    HttpMcpServer,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    RequestPermissionResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionResumeCapabilities,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
)

#: The ``_meta`` key under which the agent names its session on a tool call, as OpenCode does with its own key.
SESSION_META = "scripted/sessionID"
TURN_HEADER = "X-Belle-Turn"
OPTIONS = [
    PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
    PermissionOption(option_id="always", name="Always", kind="allow_always"),
    PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
]


def capabilities(*, load: bool = True, resume: bool = False, http: bool = True, **prompt: bool) -> AgentCapabilities:
    return AgentCapabilities(
        load_session=load,
        mcp_capabilities=McpCapabilities(http=http),
        prompt_capabilities=PromptCapabilities(**prompt),
        session_capabilities=SessionCapabilities(resume=SessionResumeCapabilities() if resume else None),
    )


@dataclass
class Session:
    id: str
    mounts: list[Any]
    #: What the session said, for a replay: ``(kind, text)``.
    history: list[tuple[str, str]] = field(default_factory=list)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class Prompt:
    """One ``session/prompt`` as the script sees it."""

    def __init__(self, agent: "ScriptedAgent", connection: "_Connection", session: Session, text: str) -> None:
        self.agent, self.session, self.text = agent, session, text
        self._connection = connection
        self.binding = connection.binding

    @property
    def cancelled(self) -> bool:
        return self.session.cancelled.is_set()

    async def say(self, text: str, message_id: str | None = None) -> None:
        update = update_agent_message_text(text)
        if message_id is not None:
            update = update.model_copy(update={"message_id": message_id})
        self.session.history.append(("agent", text))
        await self.send(update)

    async def think(self, text: str) -> None:
        await self.send(update_agent_thought_text(text))

    async def send(self, update: Any) -> None:
        await self._connection.client.session_update(session_id=self.session.id, update=update)

    async def tool(self, call_id: str, title: str, status: str = "pending", raw_input: Any = None) -> None:
        await self.send(
            ToolCallStart(
                session_update="tool_call", tool_call_id=call_id, title=title, status=status, raw_input=raw_input
            )
        )

    async def tool_done(self, call_id: str, text: str, status: str = "completed") -> None:
        content = [{"type": "content", "content": {"type": "text", "text": text}}]
        await self.send(
            ToolCallProgress.model_validate(
                {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status, "content": content}
            )
        )

    async def ask(
        self, call_id: str = "call-1", title: str = "touch /tmp/x", options=None
    ) -> RequestPermissionResponse:
        call = ToolCallUpdate(tool_call_id=call_id, title=title, kind="execute", status="pending")
        return await self._connection.client.request_permission(
            options=options or OPTIONS, session_id=self.session.id, tool_call=call
        )

    async def wait_cancel(self, timeout: float = 10.0) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.session.cancelled.wait(), timeout)
        return self.cancelled

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one of Belle's tools as the harness would: through the session's mount, or the agent's own."""
        mounts = [mount for mount in self.session.mounts if isinstance(mount, HttpMcpServer)]
        if mounts:
            url, headers = mounts[0].url, {header.name: header.value for header in mounts[0].headers}
        else:
            url, headers = self.agent.tools_url, {"Authorization": f"Bearer {self.agent.tools_token}"}
            headers[TURN_HEADER] = self.binding
        params = {"name": name, "arguments": arguments, "_meta": {SESSION_META: self.session.id}}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        async with httpx.AsyncClient(timeout=30) as client:
            answer = (await client.post(url, json=body, headers=headers)).json()
        return answer


Script = Callable[[Prompt], Awaitable[str]]


async def say_twice(prompt: Prompt) -> str:
    await prompt.say("Hello, ")
    await prompt.say("world.")
    return "end_turn"


class _Connection:
    def __init__(self, agent: "ScriptedAgent", binding: str) -> None:
        self.agent, self.binding = agent, binding
        self.client: Any = None
        self.writer: asyncio.StreamWriter | None = None

    def on_connect(self, conn: Any) -> None:
        self.client = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        self.agent.asked_versions.append(protocol_version)
        return InitializeResponse(
            protocol_version=self.agent.version,
            agent_capabilities=self.agent.capabilities,
            agent_info=Implementation(name="scripted", version="1"),
        )

    async def new_session(self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any) -> NewSessionResponse:
        session = Session(f"ses_{next(self.agent._ids)}", list(mcp_servers or ()))
        self.agent.sessions[session.id] = session
        self.agent.calls.append(("new", session.id))
        return NewSessionResponse(session_id=session.id)

    async def load_session(self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any):
        session = self._known(session_id)
        self.agent.calls.append(("load", session_id))
        session.mounts = list(mcp_servers or ())
        for kind, text in session.history:  # the replay of session/load
            update = (
                UserMessageChunk(session_update="user_message_chunk", content=TextContentBlock(type="text", text=text))
                if kind == "user"
                else update_agent_message_text(text)
            )
            await self.client.session_update(session_id=session_id, update=update)
        return LoadSessionResponse()

    async def resume_session(self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any):
        session = self._known(session_id)
        self.agent.calls.append(("resume", session_id))
        session.mounts = list(mcp_servers or ())
        return ResumeSessionResponse()

    def _known(self, session_id: str) -> Session:
        from acp import RequestError

        if session_id not in self.agent.sessions:
            raise RequestError.invalid_params({"details": f"no session {session_id}"})
        return self.agent.sessions[session_id]

    async def prompt(self, prompt: list[Any], session_id: str, **kwargs: Any) -> PromptResponse:
        session = self.agent.sessions[session_id]
        session.cancelled.clear()
        text = " ".join(getattr(block, "text", "") for block in prompt)
        self.agent.prompts.append((session_id, text, prompt))
        session.history.append(("user", text))
        stop = await self.agent.on_prompt(Prompt(self.agent, self, session, text))
        return PromptResponse(stop_reason=stop)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.agent.calls.append(("cancel", session_id))
        if session_id in self.agent.sessions:
            self.agent.sessions[session_id].cancelled.set()


class ScriptedAgent:
    """The server. ``on_prompt`` answers each prompt and returns its stop reason."""

    def __init__(
        self,
        path: str,
        *,
        binding_line: bool = False,
        capabilities: AgentCapabilities | None = None,
        version: int = 1,
        tools_url: str = "",
        tools_token: str = "",
    ) -> None:
        self.path, self.binding_line, self.version = path, binding_line, version
        self.capabilities = capabilities or globals()["capabilities"]()
        self.tools_url, self.tools_token = tools_url, tools_token
        self.on_prompt: Script = say_twice
        self.sessions: dict[str, Session] = {}
        self.prompts: list[tuple[str, str, list[Any]]] = []
        self.calls: list[tuple[str, str]] = []
        self.asked_versions: list[int] = []
        #: The binding line of each connection, in order, and how many are open.
        self.bindings: list[str] = []
        self.open = 0
        self._ids = itertools.count(1)
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._serve, self.path)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._tasks.add(asyncio.current_task())  # type: ignore[arg-type]
        self.open += 1
        try:
            binding = (await reader.readline()).decode().strip() if self.binding_line else ""
            self.bindings.append(binding)
            connection = _Connection(self, binding)
            connection.writer = writer
            await run_agent(connection, writer, reader, use_unstable_protocol=True)
        finally:
            self.open -= 1
            writer.close()
            self._tasks.discard(asyncio.current_task())  # type: ignore[arg-type]

    async def drop(self) -> None:
        """End every connection, as agent processes that exit."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self.drop()
            await self._server.wait_closed()


def result_text(answer: dict[str, Any]) -> dict[str, Any]:
    """The JSON a Belle tool answered, from an MCP ``tools/call`` answer."""
    return json.loads(answer["result"]["content"][0]["text"])
