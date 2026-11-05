"""A scripted ACP v1 agent on stdio, for a relay to start once per connection.

The prompt text picks what it does: ``slow`` streams until ``session/cancel``, ``call`` calls Belle's ``post``
tool with the binding of ``BELLE_TURN``, anything else says two chunks. Sessions are files in ``AGENT_STORE``, so
the next process can load them. Each process keeps a file named by its pid in ``AGENT_PIDS`` while it runs.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx
from acp import RequestError, run_agent, update_agent_message_text
from acp.schema import (
    AgentCapabilities,
    InitializeResponse,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    ToolCallUpdate,
)

STORE = Path(os.environ["AGENT_STORE"])
LOCAL_SESSIONS = os.environ.get("AGENT_LOCAL_SESSIONS") == "1"
if LOCAL_SESSIONS:
    STORE = STORE / str(os.getpid())
    STORE.mkdir()


class Agent:
    def on_connect(self, conn):
        self.conn, self.cancelled = conn, {}

    async def initialize(self, protocol_version, **kwargs):
        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(load_session=True, mcp_capabilities=McpCapabilities(http=True)),
        )

    async def new_session(self, cwd, mcp_servers=None, **kwargs):
        session = f"ses_{len(self.cancelled)}" if LOCAL_SESSIONS else f"ses_{os.getpid()}_{len(self.cancelled)}"
        (STORE / session).write_text("[]")
        self.cancelled[session] = asyncio.Event()
        return NewSessionResponse(session_id=session)

    async def load_session(self, cwd, session_id, mcp_servers=None, **kwargs):
        if not (STORE / session_id).exists():
            raise RequestError.invalid_params({"details": f"no session {session_id}"})
        self.cancelled[session_id] = asyncio.Event()
        for text in json.loads((STORE / session_id).read_text()):
            await self.conn.session_update(session_id=session_id, update=update_agent_message_text(text))
        return LoadSessionResponse()

    async def say(self, session_id, text):
        history = json.loads((STORE / session_id).read_text())
        (STORE / session_id).write_text(json.dumps([*history, text]))
        await self.conn.session_update(session_id=session_id, update=update_agent_message_text(text))

    async def prompt(self, prompt, session_id, **kwargs):
        text = " ".join(getattr(block, "text", "") for block in prompt)
        cancelled = self.cancelled[session_id]
        cancelled.clear()
        if "permission" in text:
            for _ in range(2 if "twice" in text else 1):
                response = await self.conn.request_permission(
                    session_id=session_id,
                    tool_call=ToolCallUpdate(tool_call_id="call-1", title=text, kind="execute", status="pending"),
                    options=[
                        PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                        PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
                    ],
                )
            await self.say(session_id, f"{text}: {getattr(response.outcome, 'option_id', response.outcome.outcome)}")
            return PromptResponse(stop_reason="end_turn")
        if "slow" in text:
            while not cancelled.is_set():
                await self.say(session_id, "tick ")
                await asyncio.sleep(0.1)
            return PromptResponse(stop_reason="cancelled")
        if "call" in text:
            headers = {
                "Authorization": f"Bearer {os.environ['BELLE_TOOLS_TOKEN']}",
                "X-Belle-Turn": os.environ["BELLE_TURN"],
            }
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "post", "arguments": {"text": text}},
            }
            async with httpx.AsyncClient() as client:
                answer = (await client.post(os.environ["BELLE_TOOLS_URL"], json=body, headers=headers)).json()
            await self.say(session_id, answer["result"]["content"][0]["text"])
            return PromptResponse(stop_reason="end_turn")
        await self.say(session_id, "Hello, ")
        await self.say(session_id, "world.")
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id, **kwargs):
        self.cancelled[session_id].set()


pid = Path(os.environ["AGENT_PIDS"]) / str(os.getpid())
pid.write_text(os.environ.get("BELLE_TURN", ""))
try:
    asyncio.run(run_agent(Agent()))
finally:
    pid.unlink()
