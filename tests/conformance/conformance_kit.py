"""What the conformance suite asks of a backend fixture, and its loopback world.

A backend joins the suite with one fixture parameter that returns a kit:

- ``backend``: the ``belle_agent.backend.Backend`` under test, not started.
- ``play(*turns)``: script what the model does in the next turns, in order.
  A turn with no script answers with a plain final text.
- ``holding()``: wait until a turn scripted with ``hold=True`` has reached the model and waits there.
- ``release()``: let a turn scripted with ``hold=True`` go on.
- ``other_model``: a model name a scope can give, other than the backend's default.
- ``models()``: the model that served each turn so far, in order, as the backend names it.

Nothing else about a backend may reach a test. A check that needs more marks
behaviour that leaked into an adapter.

A platform joins the suite the same way, with a platform kit:

- ``name``: the platform a configuration selects, and the channel its scopes match.
- ``configure(settings)``: the configuration file a host is built from. A check writes
  its settings for ``loopback``, and a kit puts its own platform's name in their place.
- ``attach(host)``: what a check drives the platform through. ``inject(record)`` delivers
  one inbound record the way that platform receives it and returns the host's outcome.
  ``log`` is every outbound action the platform performed, in order.
- ``said(text)``: a turn's text as its sender wrote it, without what the platform adds.
- ``shows``: the settings that turn on all the platform shows of a running turn, where it shows any.
  What ``attach`` returns then has ``progressed``, each ``(message id, event)`` the host handed the
  platform, and ``concluded``, each ``(message id, state, final)`` it ended a turn's showing with.
- ``holds(platform, name, mime, data)``: arrange that a turn of conversation ``C1`` can read a file of that
  name, type and content with one of the platform's tools. Returns the tool's name and its arguments.
- ``widens(platform, origin, target)``: arrange that a post from a turn of ``origin`` into ``target``
  would reach somebody who cannot read ``origin``, so that the platform's toolkit has a question about it.

A check that passes on one platform and fails on another marks behaviour that
leaked into the host, or a difference between the two that is recorded with its
reason.
"""

import asyncio
import base64
import contextlib
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from belle_agent.backend import Answer, Backend, TurnEvent, TurnRequest
from belle_agent.config import load_config
from belle_agent.host import ConversationHost, host_from_config
from belle_agent.platforms.base import TRIGGER_DM, TRIGGER_NONE, InboundAnswer, InboundMessage, InboundStop
from belle_agent.scopes import capabilities as scope_capabilities
from belle_agent.second_brain.backend import SecondBrainBackend, SummaryModel

# The suite names a platform only through its configuration and these records.
# The platform itself is built from its section, which turns it on.
PLATFORM = "loopback"
WORKSPACE = "W1"


class PlatformKit(Protocol):
    name: str

    def configure(self, settings: str) -> str: ...

    def attach(self, host: ConversationHost) -> Any: ...

    def said(self, text: str) -> str: ...

    def widens(self, platform: Any, origin: str, target: str) -> None: ...

    def holds(self, platform: Any, name: str, mime: str, data: bytes) -> tuple[str, dict[str, Any]]: ...


def with_section(settings: str, name: str, *, defaults: dict[str, Any], forced: dict[str, Any] | None = None) -> str:
    """A check's settings with the section of the platform ``name`` under ``platforms``.

    ``defaults`` fill what a check leaves out of the section, and ``forced`` replaces what it writes.
    """
    config = yaml.safe_load(settings) or {}
    platforms = dict(config.pop("platforms", None) or {})
    section = {**defaults, **(platforms.pop(name, None) or {}), **(forced or {})}
    return yaml.safe_dump({**config, "platforms": {**platforms, name: section}})


class LoopbackKit:
    """The platform every check is written for. It needs nothing between a check and itself."""

    name = PLATFORM
    #: The settings that turn on all this platform shows of a running turn. It has none: it keeps every event.
    shows = ""

    def configure(self, settings: str) -> str:
        return with_section(settings, PLATFORM, defaults={}, forced={"enabled": True})

    def attach(self, host: ConversationHost) -> Any:
        return host.platform

    def said(self, text: str) -> str:
        return text

    def widens(self, platform: Any, origin: str, target: str, *, workspace: str = WORKSPACE) -> None:
        platform.confirmable.add((workspace, target))

    def holds(self, platform: Any, name: str, mime: str, data: bytes) -> tuple[str, dict[str, Any]]:
        platform.files[name] = (mime, data)
        return "read_file", {"name": name}


@dataclass(frozen=True)
class ModelTurn:
    """What the model does in one turn: tool calls in order, then a final text."""

    final: str = ""
    calls: tuple[tuple[str, Mapping[str, Any]], ...] = ()
    #: Do nothing until the kit's ``release()``.
    hold: bool = False
    #: Put a question to the operator before the final, as ``(prompt, options)``, and wait for the answer.
    ask: tuple[str, tuple[str, ...]] | None = None
    #: Start work the turn does not wait for. It ends after ``final`` is written, and the model answers it with this.
    background: str = ""


#: Synthetic documents, written for these checks. A corpus of one cannot be ranked: BM25
#: gives a term every chunk carries no weight.
DOCUMENTS = {
    "lamp-rotation.md": "# Lamp Rotation Schedule\n\n"
    + "The Fresnel lens at Gannet Rock turns once every twelve seconds. Keepers trim"
    " the mantle at dusk and log the level of the mercury bath. " * 4,
    "starter-hydration.md": "# Starter Hydration Notes\n\n"
    + "A rye starter kept at full hydration doubles in six hours. Fold the dough every"
    " thirty minutes while the bulk fermentation runs. " * 4,
    "harbour-tides.md": "# Harbour Tide Tables\n\n"
    + "Spring tides in the estuary reach four metres. The ferry slipway floods two"
    " hours before high water and the pilots wait offshore. " * 4,
    "kite-bridle.md": "# Kite Bridle Geometry\n\n"
    + "A delta kite stalls when the bridle point moves aft. Shorten the tow line to"
    " steepen the angle of attack in a light wind. " * 4,
    "hive-inspection.md": "# Hive Inspection Checklist\n\n"
    + "Check the brood frames for capped cells and count varroa mites on the sticky"
    " board. Smoke the entrance before lifting the crown board. " * 4,
}


def as_file(name: str, text: str | None = None) -> dict[str, str]:
    """One synthetic document as a platform hands a file over."""
    content = (DOCUMENTS[name] if text is None else text).encode("utf-8")
    return {"name": name, "uri": "data:text/markdown;base64," + base64.b64encode(content).decode("ascii")}


async def _echo_summary(system: str, prompt: str, timeout: float) -> str:
    """Stand in for the summarizer: keep the document's own words, so a search has something to rank."""
    if "REGIONS BEGIN" in prompt:
        return "1: what the section says."
    body = prompt.split("DOCUMENT TEXT BEGINS ---", 1)[-1].split("--- DOCUMENT TEXT ENDS", 1)[0]
    return " ".join(body.split())[:600]


async def _no_parser(path: Path) -> str:
    return ""


def scripted_second_brain() -> SecondBrainBackend:
    """A second brain backend with no library behind it: no embedder, and a summarizer that echoes."""
    return SecondBrainBackend(
        build_model=lambda settings: SummaryModel(model="echo", invoke=_echo_summary),
        parse_file=_no_parser,
        extract_html=lambda html: ("", ""),
    )


class Kit(Protocol):
    backend: Backend
    other_model: str

    def models(self) -> list[str]: ...

    def play(self, *turns: ModelTurn) -> None: ...

    async def holding(self) -> None: ...

    def release(self) -> None: ...

    def restart(self) -> None:
        """Give the next host a new backend on the same sessions, as a restarted process builds one."""


class RecordedBackend:
    """Pass every operation through and keep the requests the backend received."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self.requests: list[TurnRequest] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        self.requests.append(request)
        return self._backend.run_turn(request)


@dataclass
class World:
    """One host on a platform under test, with a backend under test behind it."""

    host: ConversationHost
    platform: Any
    kit: Kit
    backend: RecordedBackend
    platform_kit: PlatformKit = field(default_factory=LoopbackKit)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    @property
    def requests(self) -> list[TurnRequest]:
        return self.backend.requests

    def texts(self) -> list[str]:
        """The text of each turn the backend was handed, as its sender wrote it."""
        return [self.platform_kit.said(request.text) for request in self.requests]

    def message(self, text: str, *, conversation: str = "C1", sender: str = "U1", **fields: Any) -> InboundMessage:
        """Build a channel message with a fresh message id and event id."""
        number = next(self._ids)
        fields.setdefault("chat_type", "channel")
        fields.setdefault("trigger", TRIGGER_NONE)
        return InboundMessage(
            platform=self.platform_kit.name,
            workspace=WORKSPACE,
            conversation=conversation,
            message_id=f"m{number}",
            event_id=f"e{number}",
            sender=sender,
            text=text,
            **fields,
        )

    def direct(self, text: str, *, sender: str = "U1", **fields: Any) -> InboundMessage:
        return self.message(text, conversation=f"D-{sender}", sender=sender, chat_type="dm", trigger=TRIGGER_DM, **fields)

    def stop(self, message: InboundMessage, *, sender: str | None = None) -> InboundStop:
        """Build a person's request to stop the turn ``message`` started, its sender's unless another is named."""
        return InboundStop(
            platform=self.platform_kit.name,
            workspace=WORKSPACE,
            conversation=message.conversation,
            message_id=message.message_id,
            sender=message.sender if sender is None else sender,
        )

    async def send(self, record: Any) -> str:
        """Deliver one record and wait for every turn it leads to."""
        outcome = await self.platform.inject(record)
        await self.host.idle()
        return outcome

    async def handed(self, count: int) -> None:
        """Wait until the backend has been handed ``count`` turns."""
        async with asyncio.timeout(5):
            while len(self.requests) < count:
                await asyncio.sleep(0)

    async def turn_ended(self, request_id: str) -> None:
        """Wait until the host has ended the turn of ``request_id``."""
        async with asyncio.timeout(5):
            while request_id not in [record.request_id for record in self.host.ended]:
                await asyncio.sleep(0)

    def posts(self, conversation: str | None = None) -> list[Any]:
        return [
            action
            for action in self.platform.log
            if action.kind == "post" and conversation in (None, action.conversation)
        ]

    def questions(self, conversation: str | None = None) -> list[Any]:
        """Approval prompts and invented questions the platform was asked to show."""
        return [
            action
            for action in self.platform.log
            if action.kind in ("approval", "question") and conversation in (None, action.conversation)
        ]

    async def wait_question(self, count: int = 1, *, conversation: str | None = None, timeout: float = 30) -> list[Any]:
        """Wait until ``count`` questions have been shown, whoever the backend is."""
        async with asyncio.timeout(timeout):
            while len(self.questions(conversation)) < count:
                await asyncio.sleep(0.05)
        return self.questions(conversation)

    def answer_to(self, shown: Any, *, sender: str = "U1", approved: bool | None = None, values: tuple = ()) -> InboundAnswer:
        """Build one person's answer to a question the platform showed."""
        return InboundAnswer(
            platform=self.platform_kit.name,
            workspace=WORKSPACE,
            conversation=shown.conversation,
            question_id=shown.question.id,
            sender=sender,
            answer=Answer(values=values, approved=approved),
        )

    def outcomes(self) -> list[str]:
        return [record.outcome for record in self.host.ended]


@contextlib.asynccontextmanager
async def worlds(
    kit: Kit, directory: Path, platform: PlatformKit | None = None
) -> AsyncIterator[Callable[..., Awaitable[World]]]:
    """Yield a builder that starts one host from a Belle configuration file, and stop what it built."""
    registry = scope_capabilities.snapshot_registry()
    built: list[World] = []
    platform = platform or LoopbackKit()

    async def build(settings: str = "", *, second_brain: SecondBrainBackend | None = None) -> World:
        path = directory / "config.yaml"
        path.write_text(platform.configure(settings), encoding="utf-8")
        backend = RecordedBackend(kit.backend)
        host = host_from_config(
            load_config(path), backend, state_dir=directory / "state", second_brain=second_brain
        )
        await host.start()
        built.append(World(host=host, platform=platform.attach(host), kit=kit, backend=backend, platform_kit=platform))
        return built[-1]

    try:
        yield build
    finally:
        for world in built:
            await world.host.stop()
        scope_capabilities.restore_registry(registry)
