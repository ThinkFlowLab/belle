"""The Docker Agent backend's kit for the conformance suite.

The backend under test is the adapter against the Docker Agent container. The
model is the stand-in, in this process, at the address the harness gave
Docker Agent. A ``ModelTurn`` becomes the stand-in's answers to one turn's
provider requests: one tool call per request, then the final text.

Docker Agent sends the provider no session id, so the kit tells turns apart by
what the model is asked: the conversation up to the turn's own message. The
requests of one turn share it, and the first of them has no tool result after it.
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
from pathlib import Path

from conformance_kit import ModelTurn
from standin import StandIn, _chunk, said, text_of

from belle_agent.integrations.docker_agent import DockerAgentBackend

ARTEFACTS = os.path.join(os.environ.get("DAB_STATE", "/state"), "artefacts")
#: Where Docker Agent reaches the tool server and the hook endpoint (its BELLE_TOOLS_URL and BELLE_RELAY_URL).
TOOLS_PORT = 18300
RELAY_PORT = 18200
#: The provider's name of each model of the agent YAML, to the YAML's own name, which the backend uses.
#: The Docker Agent container sets MODEL_NAME, which the YAML's `default` model reads. `second` is declared by agent_yaml.py.
YAML_MODELS = {"standin": "default", "second": "second"}


@functools.cache
def standin() -> StandIn:
    """The one stand-in of this process. Docker Agent is configured with its port."""
    os.makedirs(ARTEFACTS, exist_ok=True)
    return StandIn(log_path=os.path.join(ARTEFACTS, "standin-integration.jsonl"))


def backend(namespace: str = "") -> DockerAgentBackend:
    """An adapter on the Docker Agent container, in sessions no other test shares unless it names them.

    A namespace has one state directory, so the adapters of one namespace share what they keep.
    """
    namespace = namespace or uuid.uuid4().hex
    return DockerAgentBackend(
        os.environ["DAB_AGENT_URL"],
        os.environ["DOCKER_AGENT_AUTH_TOKEN"],
        relay_token=os.environ["BELLE_RELAY_TOKEN"],
        state_dir=Path(ARTEFACTS) / "adapters" / namespace,
        namespace=namespace,
        tools_bind="0.0.0.0",
        tools_port=TOOLS_PORT,
        tools_url=os.environ.get(
            "BELLE_TOOLS_URL", f"http://{os.environ.get('DAB_PEER', 'belle')}:{TOOLS_PORT}/mcp"
        ),
        relay_bind="0.0.0.0",
        relay_port=RELAY_PORT,
    )


def asked(model: StandIn, text: str) -> list[dict]:
    """The provider requests whose last user message is ``text``."""
    return [entry["body"] for entry in list(model.log) if said(entry["body"], "user")[-1:] == [text]]


#: Prefix of the user message that carries images and documents from a tool result.
ATTACHED = "Attached content from tool result"


def _person(message: dict) -> bool:
    """Whether a message is a person's: a user message that does not carry a tool result's content."""
    return message.get("role") == "user" and not text_of(message).startswith(ATTACHED)


def turn_of(body: dict) -> tuple[str, int]:
    """Name the turn a provider request belongs to, and count the tool results it already holds.

    The name is the conversation up to the last user message, without system messages: Docker Agent puts a
    turn's hook context there, and it may change between turns.
    """
    messages = body.get("messages", [])
    last = max((i for i, m in enumerate(messages) if _person(m)), default=-1)
    prior = [m for m in messages[: last + 1] if m.get("role") != "system"]
    return json.dumps(prior, sort_keys=True), sum(m.get("role") == "tool" for m in messages[last + 1 :])


class DockerAgentKit:
    #: The second model of the YAML the harness serves.
    other_model = "second"

    def __init__(self, namespace: str = "") -> None:
        self._models: list[str] = []
        self._namespace = namespace or uuid.uuid4().hex
        self.backend = backend(self._namespace)
        self.model = standin()
        self._script: deque[ModelTurn] = deque()
        # ponytail: two sessions that ask the very same conversation at once share one script entry.
        # A per-session header from Docker Agent would tell them apart; it sends none to the provider.
        self._playing: dict[str, ModelTurn] = {}
        self._released = threading.Event()
        self._holding = threading.Event()

    @classmethod
    @contextlib.contextmanager
    def scripted(cls) -> Iterator["DockerAgentKit"]:
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
        self.backend = backend(self._namespace)

    def play(self, *turns: ModelTurn) -> None:
        # Belle's agent YAML has no tool for either, so the stand-in cannot play them.
        if any(turn.ask or turn.background for turn in turns):
            raise ValueError("the Docker Agent kit plays no ModelTurn with ask or background")
        self._script.extend(turns)

    async def holding(self) -> None:
        await asyncio.to_thread(self._holding.wait)
        self._holding.clear()

    def release(self) -> None:
        self._released.set()

    def models(self) -> list[str]:
        return [YAML_MODELS.get(model, model) for model in self._models]

    def _answer(self, body: dict, session: str | None) -> list[dict]:
        """Answer one provider request from the script."""
        key, answered = turn_of(body)
        if not answered:
            turn = self._playing[key] = self._script.popleft() if self._script else ModelTurn(final="ok")
            self._models.append(str(body.get("model")))
            if turn.hold:
                self._holding.set()
                self._released.wait()
                self._released.clear()
        turn = self._playing[key]
        if answered < len(turn.calls):
            name, arguments = turn.calls[answered]
            function = {"name": self.backend.capabilities().tool_name(name), "arguments": json.dumps(dict(arguments))}
            call = {"index": 0, "id": f"call_{uuid.uuid4().hex}", "type": "function", "function": function}
            return [_chunk({"tool_calls": [call]}), _chunk({}, "tool_calls")]
        return [_chunk({"content": turn.final}), _chunk({}, "stop")]
