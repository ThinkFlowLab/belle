"""How a turn ends when its prompt is still unwritten, the agent does not read it, or the agent dies."""

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest
from acp_kit import collect, kinds

from belle_agent.backend import EVENT_FAILED, EVENT_INTERRUPTED, EVENT_TEXT_DELTA, TurnRequest
from belle_agent.integrations.acp.adapter import UNRESOLVED

#: A prompt larger than a socket buffer, so that its write blocks while the agent reads nothing.
LARGE = "x" * 8_000_000


async def slow(prompt):
    await prompt.say("working")
    if await prompt.wait_cancel(timeout=30):
        return "cancelled"
    return "end_turn"


async def stubborn(prompt):
    await prompt.say("ignoring")
    await asyncio.sleep(30)
    return "end_turn"


async def mute_agent(path: str, after_session: Callable[[asyncio.StreamWriter], Awaitable[None]]):
    """An agent that answers ``initialize`` and ``session/new``, then reads nothing and runs ``after_session``."""

    async def serve(reader, writer):
        while line := await reader.readline():
            message = json.loads(line)
            new = message.get("method") == "session/new"
            result = {"sessionId": "s1"} if new else {"protocolVersion": 1, "agentCapabilities": {}}
            writer.write((json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n").encode())
            await writer.drain()
            if new:
                await after_session(writer)
                return

    return await asyncio.start_unix_server(serve, path)


async def prompt_blocked(backend) -> None:
    """Wait until the turn's prompt exists and its write blocks."""
    while not (backend._turns.get("c1") and backend._turns["c1"].prompt is not None):
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    assert not backend._turns["c1"].submitted.is_set()


async def test_a_reader_that_leaves_before_the_prompt_is_written_still_cancels_it(agent, make_backend):
    backend = await make_backend(agent.path, cancel_timeout=5)
    agent.on_prompt = slow

    async def read():
        async for _ in backend.run_turn(TurnRequest("c1", "r1", "go")):
            pass

    reader = asyncio.create_task(read())
    while not (backend._turns.get("c1") and backend._turns["c1"].prompt is not None):
        await asyncio.sleep(0)
    assert not backend._turns["c1"].submitted.is_set()
    reader.cancel()
    await asyncio.wait({reader}, timeout=3)
    assert reader.done()
    assert ("cancel", agent.prompts[0][0]) in agent.calls
    assert backend._state.request("c1 r1")["terminal"]["kind"] == EVENT_INTERRUPTED


async def test_a_cancel_returns_within_cancel_timeout_when_the_agent_reads_nothing(socket_dir, make_backend):
    stop = asyncio.Event()

    async def wait(writer):
        await stop.wait()
        writer.close()

    server = await mute_agent(f"{socket_dir}/mute.sock", wait)
    try:
        backend = await make_backend(f"{socket_dir}/mute.sock", cancel_timeout=0.3, tools_mount="none")
        reader = asyncio.create_task(collect(backend, text=LARGE))
        await prompt_blocked(backend)
        assert await asyncio.wait_for(backend.cancel("c1"), 3) is False
        events = await asyncio.wait_for(reader, 3)
        assert kinds(events) == [EVENT_FAILED] and events[0].text == UNRESOLVED
        assert await backend.active() == []
    finally:
        stop.set()
        server.close()


async def test_a_reader_that_leaves_a_prompt_that_does_not_end_records_an_unknown_outcome(agent, make_backend):
    backend = await make_backend(agent.path, cancel_timeout=0.2)
    agent.on_prompt = stubborn
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    async for event in stream:
        if event.kind == EVENT_TEXT_DELTA:
            break
    await stream.aclose()
    entry = backend._state.request("c1 r1")
    assert entry["terminal"]["kind"] == EVENT_FAILED and entry["terminal"]["text"] == UNRESOLVED


async def test_an_agent_that_dies_with_the_prompt_unread_ends_the_turn_and_frees_the_conversation(
    socket_dir, make_backend
):
    die = asyncio.Event()

    async def crash(writer):
        await die.wait()
        writer.transport.abort()

    server = await mute_agent(f"{socket_dir}/dies.sock", crash)
    try:
        backend = await make_backend(f"{socket_dir}/dies.sock", tools_mount="none")
        reader = asyncio.create_task(collect(backend, text=LARGE))
        await prompt_blocked(backend)
        die.set()
        events = await asyncio.wait_for(reader, 5)
        assert kinds(events)[-1] == EVENT_FAILED
        assert await backend.active() == []
        assert backend._state.request("c1 r1")["terminal"]["kind"] == EVENT_FAILED
    finally:
        server.close()


async def test_close_closes_every_connection_and_the_tool_server_when_one_close_fails(backend):
    class Broken:
        binding = ""

        async def close(self):
            raise RuntimeError("broken")

    shared = backend._links[""]
    backend._links["broken"] = Broken()
    with pytest.raises(RuntimeError, match="broken"):
        await backend.close()
    assert not shared.alive
    assert backend._tool_server is None


async def test_a_dropped_connection_never_lets_two_turns_open_one_at_once(backend, monkeypatch):
    await backend._drop("", backend._links[""])
    gate = asyncio.Event()
    opened: list[str] = []

    class Fake:
        alive, binding = True, ""

        async def close(self):
            pass

    async def fake_open(conversation):
        opened.append(conversation)
        if len(opened) == 1:
            await gate.wait()
        else:
            await asyncio.sleep(0)
        return Fake()

    monkeypatch.setattr(backend, "_open", fake_open)
    holder = asyncio.create_task(backend._link("a"))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(backend._link("b"))
    await asyncio.sleep(0)

    async def drop_after_gate():
        await gate.wait()
        await backend._drop("", backend._links[""])

    async def link_after_gate():
        await gate.wait()
        return await backend._link("c")

    # Woken in this order: the holder releases the lock, the drop runs before the waiter takes it, then a new turn.
    dropper, late = asyncio.create_task(drop_after_gate()), asyncio.create_task(link_after_gate())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(holder, waiter, dropper, late), 3)
    assert opened == ["a", "b"]
