"""The OpenCode backend's kit for the conformance suite.

The backend under test is the adapter against the OpenCode container. The
model is the stand-in, in this process, at the address the harness gave
OpenCode. A ``ModelTurn`` becomes the stand-in's answers to one turn's
provider requests: one tool call per request, then the final text.
"""

import asyncio
import contextlib
import functools
import json
import os
import threading
import uuid
from collections import deque
from collections.abc import Iterator

from conformance_kit import ModelTurn
from standin import StandIn, _chunk, is_title, text_of

from belle_agent.integrations.opencode import OpenCodeBackend

ARTEFACTS = os.path.join(os.environ.get("OCB_STATE", "/state"), "artefacts")
#: What the kit's background work is told to do, and so what it answers and reports to the turn that started it.
BACKGROUND = "the-background-work-of-the-kit"


@functools.cache
def standin() -> StandIn:
    """The one stand-in of this process. OpenCode is configured with its port."""
    os.makedirs(ARTEFACTS, exist_ok=True)
    return StandIn(log_path=os.path.join(ARTEFACTS, "standin-integration.jsonl"))


TOOLS_PORT = 18300
# Where the relay plugin of the OpenCode image posts its hooks (its BELLE_RELAY_URL).
RELAY_PORT = 18200
#: The OpenCode container loads the relay plugin and the adapter's own endpoint answers it.
PLUGIN = os.environ.get("OCB_PLUGIN") == "1"


def backend(url: str = "", namespace: str = "") -> OpenCodeBackend:
    """An adapter on the OpenCode container, in sessions no other test shares unless it names them.

    OpenCode reaches the tool server from its own container, by this one's name, and in plugin
    mode the relay endpoint likewise.
    """
    relay = {"relay_bind": "0.0.0.0", "relay_port": RELAY_PORT, "relay_token": os.environ["BELLE_RELAY_TOKEN"]}
    return OpenCodeBackend(
        url or os.environ["OCB_OPENCODE_URL"],
        os.environ["OPENCODE_SERVER_PASSWORD"],
        namespace=namespace or uuid.uuid4().hex,
        tools_bind="0.0.0.0",
        tools_port=TOOLS_PORT,
        tools_url=f"http://{os.environ['OCB_PEER']}:{TOOLS_PORT}/mcp",
        **(relay if PLUGIN else {}),
    )


class OpenCodeKit:
    #: The second model that OPENCODE_CONFIG_CONTENT declares on a stand-in run, as a scope names it.
    other_model = "configured/standin/second"

    def __init__(self, url: str = "", namespace: str = "") -> None:
        self._models: list[str] = []
        self._url, self._namespace = url, namespace or uuid.uuid4().hex
        self.backend = backend(url, self._namespace)
        self.model = standin()
        self._script: deque[ModelTurn] = deque()
        # Session id to the model turn its execution is playing.
        self._playing: dict[str, ModelTurn] = {}
        self._released = threading.Event()
        self._holding = threading.Event()

    @classmethod
    @contextlib.contextmanager
    def scripted(cls) -> Iterator["OpenCodeKit"]:
        """A kit whose stand-in plays ``ModelTurn`` scripts, for as long as the block runs."""
        kit = cls()
        directive = kit.model.script
        kit.model.script = kit._answer
        try:
            yield kit
        finally:
            kit.model.script = directive
            kit.release()  # a held request must not outlive its test

    def restart(self) -> None:
        self.backend = backend(self._url, self._namespace)

    def play(self, *turns: ModelTurn) -> None:
        self._script.extend(turns)

    async def holding(self) -> None:
        await asyncio.to_thread(self._holding.wait)
        self._holding.clear()

    def release(self) -> None:
        self._released.set()

    def models(self) -> list[str]:
        return ["configured/" + model for model in self._models]

    def _answer(self, body: dict, session: str | None) -> list[dict]:
        """Answer one provider request from the script."""
        if is_title(body):
            return [_chunk({"content": "a title"}), _chunk({}, "stop")]
        messages = body.get("messages", [])
        user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        answered = sum(m.get("role") == "tool" for m in messages[user + 1 :])
        if user >= 0 and BACKGROUND in text_of(messages[user]):
            # The child's own request, which it answers with its task, or its report to the session that
            # started it, which the model of that session answers.
            started = self._playing.get(session)
            return [_chunk({"content": started.background if started else BACKGROUND}), _chunk({}, "stop")]
        if not answered:
            turn = self._playing[session] = self._script.popleft() if self._script else ModelTurn(final="ok")
            self._models.append(str(body.get("model")))
            if turn.hold:
                self._holding.set()
                self._released.wait()
                self._released.clear()
        turn = self._playing[session]
        # An invented question is the model calling the built-in `question` tool before its own calls.
        if turn.ask is not None and answered == 0:
            prompt, options = turn.ask
            arguments = {
                "questions": [{
                    "question": prompt,
                    "header": (prompt[:28] or "question"),
                    "options": [{"label": option, "description": option} for option in options],
                }]
            }
            return _call("question", arguments)
        done = answered - (1 if turn.ask is not None else 0)
        if done < len(turn.calls):
            name, arguments = turn.calls[done]
            return _call(self.backend.capabilities().tool_name(name), arguments)
        if turn.background and done == len(turn.calls):
            # Work the turn does not wait for is a subagent that runs in the background.
            return _call("subagent", {"agent": "general", "description": "work", "prompt": BACKGROUND, "background": True})
        return [_chunk({"content": turn.final}), _chunk({}, "stop")]


def _call(name: str, arguments: dict) -> list[dict]:
    """The chunks of one tool call."""
    function = {"name": name, "arguments": json.dumps(dict(arguments))}
    tool_call = {"index": 0, "id": f"call_{uuid.uuid4().hex}", "type": "function", "function": function}
    return [_chunk({"tool_calls": [tool_call]}), _chunk({}, "tool_calls")]
