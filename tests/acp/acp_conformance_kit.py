"""The ACP backend's kit for the conformance suite."""

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from acp_kit import settings
from conformance_kit import ModelTurn
from scripted_agent import Prompt, ScriptedAgent

from belle_agent.integrations.acp import AcpBackend
from belle_agent.tools.host import ToolHost


class _KitBackend(AcpBackend):
    """The backend under test, which serves its scripted agent while it runs, as a harness container would."""

    def __init__(self, kit: "AcpKit") -> None:
        super().__init__(settings(kit.agent.path), kit.state)
        self._kit = kit

    async def start(self, tools: ToolHost) -> None:
        await self._kit.agent.start()
        await super().start(tools)

    async def close(self) -> None:
        await super().close()
        await self._kit.agent.close()


class AcpKit:
    """The ACP backend's kit: the adapter against a scripted agent that plays each ``ModelTurn``.

    A tool call of the script goes through Belle's tool server, as a harness's call does, bound by its session.
    """

    #: ACP v1 names no model, so a scope's model is not applied and every turn runs on the agent's own.
    other_model = "other"

    def __init__(self) -> None:
        self._directory = tempfile.mkdtemp(prefix="acp", dir="/tmp")
        self.state = Path(self._directory) / "state"
        self.agent = ScriptedAgent(f"{self._directory}/agent.sock")
        self.agent.on_prompt = self._play
        self.backend: AcpBackend = _KitBackend(self)
        self._script: deque[ModelTurn] = deque()
        self._models: list[str] = []
        self._holding = asyncio.Event()
        self._released = asyncio.Event()
        self._stopped_hold = False

    @classmethod
    @contextlib.contextmanager
    def scripted(cls) -> Iterator["AcpKit"]:
        kit = cls()
        try:
            yield kit
        finally:
            kit.release()
            shutil.rmtree(kit._directory, ignore_errors=True)

    def restart(self) -> None:
        self.backend = _KitBackend(self)

    def play(self, *turns: ModelTurn) -> None:
        # The scripted agent offers no question of its own and runs no work in the background.
        if any(turn.ask or turn.background for turn in turns):
            raise ValueError("the ACP kit plays no ModelTurn with ask or background")
        self._script.extend(turns)

    async def holding(self) -> None:
        await self._holding.wait()
        self._holding.clear()

    def release(self) -> None:
        # A check releases a turn that a stop ended, as the scripted backend's stop and release meet in one wake.
        if self._stopped_hold:
            self._stopped_hold = False
            return
        self._released.set()

    def models(self) -> list[str]:
        return list(self._models)

    async def _play(self, prompt: Prompt) -> str:
        turn = self._script.popleft() if self._script else ModelTurn(final="ok")
        self._models.append("default")
        if turn.hold:
            self._holding.set()
            waits = {
                asyncio.ensure_future(self._released.wait()),
                asyncio.ensure_future(prompt.session.cancelled.wait()),
            }
            _, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            self._released.clear()
            self._stopped_hold = prompt.cancelled
        for number, (name, arguments) in enumerate(turn.calls):
            if prompt.cancelled:
                return "cancelled"
            call_id = f"call-{len(self._models)}-{number}"
            await prompt.tool(call_id, name, raw_input=dict(arguments))
            answer = await prompt.call_tool(name, dict(arguments))
            content = (answer.get("result") or {}).get("content") or [{"text": json.dumps(answer.get("error"))}]
            await prompt.tool_done(call_id, content[0].get("text", ""))
        if prompt.cancelled:
            return "cancelled"
        half = len(turn.final) // 2
        for piece in filter(None, (turn.final[:half], turn.final[half:])):
            await prompt.say(piece)
        return "end_turn"
