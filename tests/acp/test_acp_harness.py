"""Belle on a real harness: a text turn, a stop, permissions, Belle's tools, and a restart of Belle.

These checks spend model requests. They run only when ``E2E_HARNESS`` names the harness, ``docker_agent`` or
``opencode``. ``E2E_ROUTE`` names the route: ``acp`` (the default) or ``adapter``, the harness's existing
integration. Both routes run the same tasks, so that they can be compared. The loopback platform stands in for a
chat platform.

Environment: ``BELLE_TOOLS_TOKEN``, and ``E2E_TOOLS_URL`` where the harness reaches Belle's tool server.
ACP: ``BELLE_ACP_SOCKET``, and ``E2E_WORK``, the ACP working directory, which this container mounts too.
Existing adapters: ``E2E_HARNESS_URL``, the relay token and the server's secret, as their guides say.
"""

import asyncio
import os
import secrets
from pathlib import Path
from typing import Any

import pytest
from conformance_kit import worlds

from belle_agent.backend import EVENT_TEXT_DELTA, TurnRequest
from belle_agent.host import OUTCOME_FAILED, OUTCOME_INTERRUPTED
from belle_agent.launcher import create_backend

HARNESS = os.environ.get("E2E_HARNESS", "")
ROUTE = os.environ.get("E2E_ROUTE", "acp")
pytestmark = pytest.mark.skipif(HARNESS not in ("docker_agent", "opencode", "jiuwenswarm"), reason="needs E2E_HARNESS")
WORK = Path(os.environ.get("E2E_WORK", "/work"))
TOOLS_URL = os.environ.get("E2E_TOOLS_URL", "http://127.0.0.1:18300/mcp")
TOOLS = {"bind": "0.0.0.0", "port": 18300, "url": TOOLS_URL}

PROFILES = {
    "docker_agent": {
        "connection": "per_conversation",
        "tools_mount": "agent",
        "binding": {"header": "X-Belle-Turn"},
        "tool_name_format": "belle_{name}",
    },
    "opencode": {
        "connection": "shared",
        "tools_mount": "session",
        "binding": {"meta": "ai.opencode/sessionID"},
        "tool_name_format": "belle_{name}",
    },
    # JiuwenSwarm's ACP bridge advertises no HTTP MCP and names no session on a tool call.
    "jiuwenswarm": {"connection": "shared", "tools_mount": "none"},
}
#: Whether the harness's ACP agent advertises session/load or session/resume. JiuwenSwarm's bridge advertises neither.
RESTORES = HARNESS != "jiuwenswarm"
#: Where a person must agree before a shell command runs, on a route whose harness does not ask by itself.
SHELL_ASKS = """
scopes:
  - match: {channel: loopback}
    permissions: {tools: {shell: ask, bash: ask}}
"""


def config(namespace: str) -> dict[str, Any]:
    """The deployment configuration of the route under test. ``namespace`` keeps each check's sessions apart."""
    if ROUTE == "acp":
        profile = {"socket": os.environ["BELLE_ACP_SOCKET"], "cwd": str(WORK), **PROFILES[HARNESS]}
        acp: dict[str, Any] = {"profile": HARNESS, "profiles": {HARNESS: profile}, "tools": TOOLS}
        if profile["tools_mount"] != "none":
            acp["tools_token"] = "${BELLE_TOOLS_TOKEN}"
        return {"backend": "acp", "backends": {"acp": acp}}
    section: dict[str, Any] = {
        "url": os.environ["E2E_HARNESS_URL"],
        "namespace": namespace,
        "tools": TOOLS,
        "relay": {"bind": "0.0.0.0", "port": 18200},
    }
    if HARNESS == "opencode":
        section["username"] = "opencode"
    return {"backend": HARNESS, "backends": {HARNESS: section}}


class Watched:
    """A backend that says when a turn has written its first text, and keeps each turn's event kinds."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.writing = asyncio.Event()
        #: Each turn's event kinds in order, a run of the same kind once, for a comparison of the routes.
        self.sequences: list[list[str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    async def run_turn(self, request: TurnRequest):  # type: ignore[no-untyped-def]
        kinds: list[str] = []
        self.sequences.append(kinds)
        async for event in self._backend.run_turn(request):
            if event.kind == EVENT_TEXT_DELTA and not event.data.get("thinking"):
                self.writing.set()
            if not kinds or kinds[-1] != event.kind:
                kinds.append(event.kind)
            yield event


class Harness:
    other_model = ""

    def __init__(self, state: Path) -> None:
        self._state = state
        # The harness outlives the checks, and the existing adapters name its sessions by the conversation.
        self._namespace = secrets.token_hex(4)
        self.restart()

    def restart(self) -> None:
        """A new backend on the same state and sessions, as a restarted Belle builds one."""
        self.backend = Watched(create_backend(config(self._namespace), self._state))


@pytest.fixture
async def build(tmp_path):
    harness = Harness(tmp_path / "harness")
    async with worlds(harness, tmp_path) as build:
        yield build
    print(f"event sequences: {harness.backend.sequences}")


@pytest.fixture
async def world(build):
    return await build(SHELL_ASKS)


def answered(world) -> list[str]:
    return [post.text for post in world.posts()]


async def test_a_text_turn_posts_its_answer(world):
    await world.send(world.direct("Reply with exactly the word: pong. Use no tool."))
    assert [text.strip().strip(".").lower() for text in answered(world)] == ["pong"]


async def test_a_stop_mid_turn_ends_it_and_posts_nothing(world):
    message = world.direct("Count from 1 to 400 in words, one number per line. Use no tool.")
    await world.platform.inject(message)
    async with asyncio.timeout(180):
        await world.kit.backend.writing.wait()
    stopped = await world.platform.inject(world.stop(message))
    await world.host.idle()
    print(f"stop: {stopped} outcomes={world.outcomes()} posts={len(world.posts())}")
    assert stopped.startswith("stopped")
    assert world.outcomes() == [OUTCOME_INTERRUPTED] and world.posts() == []


@pytest.mark.parametrize("approved", [True, False])
async def test_a_shell_action_runs_only_when_a_person_approves_it(world, approved):
    marker = WORK / f"m-{ROUTE}-{HARNESS}-{'approve' if approved else 'reject'}"
    marker.unlink(missing_ok=True)
    text = f"Use your shell tool to run exactly this command once: touch {marker}\nUse no other tool. Then reply: done."
    await world.platform.inject(world.direct(text))
    shown = (await world.wait_question(timeout=180))[0]
    print(f"question: {shown.text}")
    await world.platform.inject(world.answer_to(shown, approved=approved))
    await world.host.idle()
    print(f"outcomes: {world.outcomes()}")
    assert marker.exists() is approved


@pytest.mark.skipif(PROFILES.get(HARNESS, {}).get("tools_mount") == "none", reason="the profile offers no tool")
async def test_two_conversations_call_belles_tool_each_under_its_own(world):
    def asking(emoji: str) -> str:
        if (ROUTE, HARNESS) == ("acp", "opencode"):
            # OpenCode shows a session mount's tools through its code-mode execute tool only.
            call = f'Call the execute tool once with this code: return await tools.belle.react_to_message({{ emoji: "{emoji}" }})'
        else:
            call = f"Make one direct call to the tool belle_react_to_message with emoji set to {emoji}"
        return f"{call}\nUse no other tool. Then reply: done."

    first, second = world.direct(asking("rocket"), sender="U1"), world.direct(asking("tada"), sender="U2")
    await asyncio.gather(world.platform.inject(first), world.platform.inject(second))
    # A harness may ask a person before it calls a tool of Belle's. A person approves that call only.
    approvals: set[str] = set()
    idle = asyncio.ensure_future(world.host.idle())
    async with asyncio.timeout(300):
        while not idle.done():
            for shown in world.questions():
                if shown.question.id not in approvals and "react_to_message" in shown.text:
                    approvals.add(shown.question.id)
                    await world.platform.inject(world.answer_to(shown, sender=shown.conversation[2:], approved=True))
            await asyncio.sleep(0.2)
    print(f"questions asked before Belle's tool ran: {len(approvals)}")
    reactions = {(action.conversation, action.name) for action in world.platform.log if action.kind == "reaction"}
    assert reactions == {("D-U1", "rocket"), ("D-U2", "tada")}


async def test_a_restart_after_a_turn_keeps_the_conversation(build):
    word = secrets.choice(["heron", "basalt", "juniper", "quartz", "falcon"]) + str(secrets.randbelow(90) + 10)
    first = await build(SHELL_ASKS)
    await first.send(first.direct(f"Remember this code word: {word}. Reply with exactly: ok. Use no tool."))
    await first.host.stop()

    first.kit.restart()
    second = await build(SHELL_ASKS)
    second._ids = first._ids  # a platform gives the next message a new id
    await second.send(second.direct("What was the code word I gave you? Reply with the word only. Use no tool."))
    print(f"restart after a turn: {answered(second)}")
    assert [word in text.lower() for text in answered(second)] == [RESTORES]


async def test_a_restart_during_a_turn_ends_it_and_runs_it_never_again(build):
    first = await build(SHELL_ASKS)
    message = first.direct("Count from 1 to 400 in words, one number per line. Use no tool.")
    await first.platform.inject(message)
    async with asyncio.timeout(180):
        await first.kit.backend.writing.wait()
    await first.host.stop()

    first.kit.restart()
    second = await build(SHELL_ASKS)
    second._ids = first._ids
    # The start of the next process settles the turn. A route that cannot deliver it records no outcome.
    for _ in range(120):
        if second.host.ended:
            break
        await asyncio.sleep(0.5)
    await second.host.idle()
    # The turn is reported by the next process, and never handed to the harness again.
    print(f"after the restart: outcomes={second.outcomes()} posts={len(answered(second))}")
    assert [request.request_id for request in second.requests] == []
    if ROUTE == "acp":
        # The relay ended the turn with Belle's connection, so its outcome is unknown.
        assert second.outcomes() in ([OUTCOME_FAILED], [OUTCOME_INTERRUPTED])
