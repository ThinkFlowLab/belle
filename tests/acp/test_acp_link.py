"""The transport: Belle opens the relay's socket, initializes ACP v1, and leaves nothing running when it is done."""

import asyncio
import socket as sockets
import subprocess
import sys

import pytest
from acp_kit import collect, kinds
from scripted_agent import ScriptedAgent, say_twice

from belle_agent.backend import EVENT_ACCEPTED, EVENT_FAILED, EVENT_SUCCEEDED, EVENT_TEXT_DELTA, TurnRequest
from belle_agent.integrations.acp.adapter import STREAM_LOST
from belle_agent.integrations.acp.link import AcpError, open_link


async def test_a_missing_socket_is_named(socket_dir, make_backend):
    with pytest.raises(AcpError, match=f"cannot connect to the ACP relay at {socket_dir}/none.sock"):
        await make_backend(f"{socket_dir}/none.sock")


async def test_a_socket_nobody_listens_on_is_refused(socket_dir, make_backend):
    path = f"{socket_dir}/stale.sock"
    stale = sockets.socket(sockets.AF_UNIX)
    stale.bind(path)  # bound and never listening: a connection is refused
    try:
        with pytest.raises(AcpError, match="Connection refused"):
            await make_backend(path)
    finally:
        stale.close()


async def test_an_agent_that_does_not_answer_initialize_in_time_is_named(socket_dir, make_backend):
    async def silent(reader, writer):
        await reader.read()

    server = await asyncio.start_unix_server(silent, f"{socket_dir}/slow.sock")
    try:
        with pytest.raises(AcpError, match=r"did not answer initialize within 0.2s \(start_timeout_seconds\)"):
            await make_backend(f"{socket_dir}/slow.sock", start_timeout=0.2)
    finally:
        server.close()


async def test_a_cancel_during_initialize_closes_the_socket(socket_dir):
    closed = asyncio.Event()

    async def silent(reader, writer):
        await reader.read()  # returns when Belle closes its end
        closed.set()

    server = await asyncio.start_unix_server(silent, f"{socket_dir}/slow.sock")
    try:
        opening = asyncio.create_task(
            open_link(f"{socket_dir}/slow.sock", lambda link: object(), binding="", timeout=30)
        )
        await asyncio.sleep(0.2)
        opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        await asyncio.wait_for(closed.wait(), 5)
    finally:
        server.close()


async def test_an_agent_of_another_version_is_refused_with_both_versions(socket_dir, make_backend):
    agent = ScriptedAgent(f"{socket_dir}/v2.sock", version=2)
    await agent.start()
    try:
        with pytest.raises(AcpError, match="answered ACP version 2 to Belle's version 1"):
            await make_backend(agent.path)
        assert agent.asked_versions == [1]
        await asyncio.sleep(0.1)
        assert agent.open == 0  # the connection was closed
    finally:
        await agent.close()


async def test_several_sessions_share_one_connection_without_mixing(agent, backend):
    async def echo(prompt):
        await asyncio.sleep(0.05)
        await prompt.say(f"to {prompt.text}")
        return "end_turn"

    agent.on_prompt = echo
    first, second = await asyncio.gather(collect(backend, "c1", text="one"), collect(backend, "c2", text="two"))
    assert (first[-1].text, second[-1].text) == ("to one", "to two")
    assert len(agent.bindings) == 1  # the connection opened at start serves every session
    assert len({session for session, _, _ in agent.prompts}) == 2


async def test_a_malformed_frame_is_skipped_and_the_turn_goes_on(agent, backend):
    async def garbled(prompt):
        prompt._connection.writer.write(b"{not json\n")
        await prompt.say("fine")
        return "end_turn"

    agent.on_prompt = garbled
    events = await collect(backend)
    assert kinds(events) == [EVENT_ACCEPTED, EVENT_TEXT_DELTA, EVENT_SUCCEEDED] and events[-1].text == "fine"


async def test_an_agent_that_exits_mid_turn_ends_it_once_with_an_unknown_outcome(agent, backend):
    sessions = []

    async def dies(prompt):
        sessions.append(prompt.session.id)
        await prompt.say("partial")
        prompt._connection.writer.close()
        await asyncio.sleep(60)
        return "end_turn"

    agent.on_prompt = dies
    events = await collect(backend)
    assert kinds(events) == [EVENT_ACCEPTED, EVENT_TEXT_DELTA, EVENT_FAILED]
    assert events[-1].text == STREAM_LOST  # the partial text is not reported as a final
    assert events[-1].data["backend_session"] == sessions[0]
    # The next turn opens a new connection.
    agent.on_prompt = say_twice
    assert (await collect(backend, request_id="r2"))[-1].kind == EVENT_SUCCEEDED


async def test_the_late_end_of_a_turn_on_a_dead_connection_keeps_the_new_connection(agent, backend):
    release = asyncio.Event()

    async def script(prompt):
        if prompt.text == "second":
            await release.wait()
        else:
            await asyncio.sleep(30)
        await prompt.say("done")
        return "end_turn"

    agent.on_prompt = script
    first = backend.run_turn(TurnRequest("c1", "r1", "first"))
    assert (await first.__anext__()).kind == EVENT_ACCEPTED
    await agent.drop()  # The agent process exits. The reader of c1 reads the end of its turn later.
    await asyncio.sleep(0.2)
    second = backend.run_turn(TurnRequest("c2", "r2", "second"))
    assert (await second.__anext__()).kind == EVENT_ACCEPTED  # on a new connection
    assert [event.kind async for event in first][-1] == EVENT_FAILED
    release.set()
    assert [event.kind async for event in second][-1] == EVENT_SUCCEEDED


async def test_a_close_during_a_turn_ends_its_stream_and_leaves_no_task(agent, backend):
    before = asyncio.all_tasks()
    holding = asyncio.Event()

    async def slow(prompt):
        holding.set()
        await prompt.wait_cancel(60)
        return "cancelled"

    agent.on_prompt = slow
    stream = backend.run_turn(TurnRequest("c1", "r1", "hi"))
    reader = asyncio.ensure_future(stream.__anext__())
    assert (await reader).kind == EVENT_ACCEPTED
    await holding.wait()
    await backend.close()
    events = [event async for event in stream]
    assert [event.kind for event in events if event.terminal] == [EVENT_FAILED]
    await asyncio.sleep(0.1)
    left = {task for task in asyncio.all_tasks() - before if not task.done()}
    assert {task.get_name() for task in left} <= {task.get_name() for task in before}, left


def test_the_launcher_refuses_backend_acp_without_the_extra_and_names_it():
    script = """
import sys
class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name == "acp" or name.startswith("acp."):
            raise ImportError("acp is not importable in this check")
sys.meta_path.insert(0, Refuse())
from belle_agent.config import BelleConfigError
from belle_agent.launcher import create_backend
try:
    create_backend({"backend": "acp"}, None)
except BelleConfigError as exc:
    print(exc)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert "backend=acp cannot be loaded" in result.stdout and "the acp extra" in result.stdout
