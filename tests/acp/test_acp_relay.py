"""Through socat, the relay of a deployment: one agent process per connection, ended with its connection."""

import asyncio
import dataclasses
import os
import shutil
import socket as sockets
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from acp_kit import TOKEN, bind, collect, kinds, settings

from belle_agent.backend import (
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_QUESTION,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    RECOVERY_TERMINAL,
    Answer,
    TurnRequest,
)
from belle_agent.integrations.acp import AcpBackend
from belle_agent.integrations.acp.adapter import STREAM_LOST

pytestmark = pytest.mark.skipif(shutil.which("socat") is None, reason="the relay checks need socat")

HERE = Path(__file__).resolve().parent
ENTRY = HERE.parents[1] / "containers/acp-trial/relay-entry.sh"


def free_port() -> int:
    with sockets.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def relay(socket_dir, tmp_path):
    """Start socat as a deployment does. ``start(entry)`` returns the socket and the tool server port."""
    started: list[subprocess.Popen] = []
    pids, store = tmp_path / "pids", tmp_path / "store"
    pids.mkdir(), store.mkdir()

    def start(entry: bool, *, local_sessions: bool = False) -> tuple[str, int]:
        path, port = f"{socket_dir}/relay.sock", free_port()
        agent = f"{sys.executable} {HERE / 'stdio_agent.py'}"
        command = f"{ENTRY} {agent}" if entry else agent
        environment = {
            **os.environ,
            "AGENT_PIDS": str(pids),
            "AGENT_LOCAL_SESSIONS": "1" if local_sessions else "0",
            "AGENT_STORE": str(store),
            "BELLE_TOOLS_TOKEN": TOKEN,
            "BELLE_TOOLS_URL": f"http://127.0.0.1:{port}/mcp",
        }
        started.append(
            subprocess.Popen(
                ["socat", f"UNIX-LISTEN:{path},fork,unlink-early,umask=077", f"EXEC:{command},pipes"], env=environment
            )
        )
        return path, port

    start.pids = pids  # type: ignore[attr-defined]
    yield start
    for process in started:
        process.terminate()
        process.wait(5)


async def wait_for(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.05)


def alive(pids: Path) -> dict[int, str]:
    """The agent processes still running, with the binding each one got."""
    found = {}
    for entry in pids.iterdir():
        try:
            os.kill(int(entry.name), 0)
        except ProcessLookupError:
            continue
        found[int(entry.name)] = entry.read_text()
    return found


async def started(make, path: str, **options) -> AcpBackend:
    async with asyncio.timeout(10):
        while not os.path.exists(path):
            await asyncio.sleep(0.05)
    return await make(path, **options)


async def test_each_conversation_gets_its_own_process_and_binding(relay, tmp_path, tools, toolkit):
    path, port = relay(entry=True)
    config = settings(path, header="X-Belle-Turn")
    config = dataclasses.replace(config, tools_port=port)
    backend = AcpBackend(config, tmp_path / "state")
    await wait_for(lambda: os.path.exists(path))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700  # socat's umask=077: the relay's user only
    await backend.start(tools)
    try:
        bind(tools, "c1"), bind(tools, "c2")
        first, second = await asyncio.gather(
            collect(backend, "c1", text="call 1"), collect(backend, "c2", text="call 2")
        )
        assert (first[-1].kind, second[-1].kind) == (EVENT_SUCCEEDED, EVENT_SUCCEEDED)
        assert sorted(toolkit.posts) == [("call 1", "c1"), ("call 2", "c2")]
        processes = alive(relay.pids)
        assert len(processes) == 2 and set(processes.values()) == set(backend._bindings)

        # A cancel stops a slow turn, and the conversation's process takes the next one.
        stream = backend.run_turn(TurnRequest("c1", "r2", "slow"))
        async for event in stream:
            if event.kind == EVENT_TEXT_DELTA:
                break
        assert await backend.cancel("c1") is True
        assert [event.kind async for event in stream][-1] == EVENT_INTERRUPTED
        assert kinds(await collect(backend, "c1", "r3"))[-1] == EVENT_SUCCEEDED
        assert alive(relay.pids) == processes
    finally:
        await backend.close()
    # Each connection closed, so the relay ended its process.
    await wait_for(lambda: not alive(relay.pids))


async def test_a_restart_through_the_plain_relay_ends_the_turn_and_keeps_the_session(relay, make_backend, tmp_path):
    path, _ = relay(entry=False)
    first = await started(make_backend, path, state=tmp_path / "live")
    assert kinds(await collect(first, "c1", "r1"))[-1] == EVENT_SUCCEEDED
    stream = first.run_turn(TurnRequest("c1", "r2", "slow"))
    async for event in stream:
        if event.kind == EVENT_TEXT_DELTA:
            break
    # The process stops here: what it recorded is all the next one reads.
    shutil.copytree(tmp_path / "live", tmp_path / "after")
    await first.close()
    assert [event.kind async for event in stream if event.terminal] == [EVENT_FAILED]
    await wait_for(lambda: not alive(relay.pids))

    second = await make_backend(path, state=tmp_path / "after")
    ended = await second.recover(TurnRequest("c1", "r1", "hi"))
    assert (ended.state, ended.terminal.kind, ended.terminal.text) == (
        RECOVERY_TERMINAL,
        EVENT_SUCCEEDED,
        "Hello, world.",
    )
    cut = await second.recover(TurnRequest("c1", "r2", "slow"))
    assert (cut.state, cut.terminal.kind, cut.terminal.text) == (RECOVERY_TERMINAL, EVENT_FAILED, STREAM_LOST)
    # The new process loads the session from the store, and the replay reaches no turn.
    events = await collect(second, "c1", "r3")
    assert [event.text for event in events if event.kind == EVENT_TEXT_DELTA] == ["Hello, ", "world."]
    assert events[-1].kind == EVENT_SUCCEEDED


async def test_independent_processes_reusing_session_ids_keep_turns_separate(relay, make_backend):
    path, _ = relay(entry=True, local_sessions=True)
    backend = await started(make_backend, path, header="X-Belle-Turn", connection="per_conversation")
    first = backend.run_turn(TurnRequest("c1", "r1", "permission twice c1"))
    second = backend.run_turn(TurnRequest("c2", "r2", "permission c2"))

    async def question(stream):
        async for event in stream:
            if event.kind == EVENT_QUESTION:
                return event.question
        raise AssertionError("no question")

    try:
        async with asyncio.timeout(10):
            q1 = await question(first)
            q2 = await question(second)
            assert (q1.tool, q2.tool) == ("permission twice c1", "permission c2")
            assert backend._state.session("c1") == backend._state.session("c2") == "ses_0"
            assert len(alive(relay.pids)) == 2
            await backend.answer("c1", q1.id, Answer(approved=True))
            next_question = await question(first)
            assert next_question.tool == "permission twice c1"
            assert await backend.pending("c2") == [q2]
            await backend.answer("c1", next_question.id, Answer(approved=True))
            events1 = [event async for event in first]
            assert (events1[-1].kind, events1[-1].text) == (EVENT_SUCCEEDED, "permission twice c1: allow")
            assert await backend.active() == ["c2"]
            assert await backend.pending("c2") == [q2]
            await backend.answer("c2", q2.id, Answer(approved=False))
            events2 = [event async for event in second]
            assert (events2[-1].kind, events2[-1].text) == (EVENT_SUCCEEDED, "permission c2: reject")
            assert [event.text for event in events2 if event.kind == EVENT_TEXT_DELTA] == ["permission c2: reject"]
            assert await backend.active() == []
    finally:
        await first.aclose()
        await second.aclose()
        await backend.close()
    await wait_for(lambda: not alive(relay.pids))
