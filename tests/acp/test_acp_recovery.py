"""Recovery after a crash: each state of a request gives a recovery backed by the state file, never a guess.

A crash is a copy of the state file taken at that moment: the next process starts from it.
"""

import asyncio
import json
import shutil

import pytest
from acp_kit import collect, kinds
from scripted_agent import say_twice

from belle_agent.backend import (
    EVENT_ACCEPTED,
    EVENT_FAILED,
    EVENT_SUCCEEDED,
    RECOVERY_NOT_SUBMITTED,
    RECOVERY_RUNNING,
    RECOVERY_TERMINAL,
    TurnRequest,
)
from belle_agent.integrations.acp.adapter import STREAM_LOST, UNRECORDED

REQUEST = TurnRequest("c1", "r1", "hi")


async def crash_during(agent, make_backend, tmp_path, script):
    """Run a turn until ``script`` holds, copy the state as a crash would leave it, and recover from the copy."""
    holding, release = asyncio.Event(), asyncio.Event()

    async def held(prompt):
        await script(prompt)
        holding.set()
        await release.wait()
        return "end_turn"

    agent.on_prompt = held
    backend = await make_backend(agent.path, state=tmp_path / "live")
    running = asyncio.ensure_future(collect(backend))
    await holding.wait()
    shutil.copytree(tmp_path / "live", tmp_path / "crash")
    release.set()
    await running
    return await (await make_backend(agent.path, state=tmp_path / "crash")).recover(REQUEST)


async def test_a_request_never_admitted_was_not_submitted(agent, backend):
    assert (await backend.recover(REQUEST)).state == RECOVERY_NOT_SUBMITTED
    # A request refused before submission is not recorded either.
    await collect(backend, files=({"name": "a.mp3", "uri": "data:audio/mpeg;base64,SUQz"},))
    assert (await backend.recover(REQUEST)).state == RECOVERY_NOT_SUBMITTED
    assert agent.prompts == []


async def test_a_crash_after_the_prompt_before_any_update_is_an_unknown_outcome(agent, make_backend, tmp_path):
    async def nothing(prompt):
        pass

    recovered = await crash_during(agent, make_backend, tmp_path, nothing)
    assert recovered.state == RECOVERY_TERMINAL
    assert (recovered.terminal.kind, recovered.terminal.text) == (EVENT_FAILED, STREAM_LOST)


async def test_a_crash_during_a_tool_call_is_an_unknown_outcome(agent, make_backend, tmp_path):
    async def calling(prompt):
        await prompt.tool("t1", "shell")

    recovered = await crash_during(agent, make_backend, tmp_path, calling)
    assert (recovered.state, recovered.terminal.kind) == (RECOVERY_TERMINAL, EVENT_FAILED)


async def test_a_turn_that_ended_keeps_its_recorded_terminal_event(agent, make_backend, tmp_path):
    agent.on_prompt = say_twice
    await collect(await make_backend(agent.path, state=tmp_path / "s"))
    recovered = await (await make_backend(agent.path, state=tmp_path / "s")).recover(REQUEST)
    assert (recovered.state, recovered.terminal.kind, recovered.terminal.text) == (
        RECOVERY_TERMINAL,
        EVENT_SUCCEEDED,
        "Hello, world.",
    )


async def test_a_turn_that_failed_keeps_the_session_it_failed_in(agent, make_backend, tmp_path):
    sessions = []

    async def refuses(prompt):
        sessions.append(prompt.session.id)
        return "refusal"

    agent.on_prompt = refuses
    await collect(await make_backend(agent.path, state=tmp_path / "s"))
    recovered = await (await make_backend(agent.path, state=tmp_path / "s")).recover(REQUEST)
    assert (recovered.terminal.kind, recovered.terminal.data["backend_session"]) == (EVENT_FAILED, sessions[0])


async def test_a_turn_running_in_this_process_is_running(agent, backend):
    release = asyncio.Event()

    async def held(prompt):
        await release.wait()
        return "end_turn"

    agent.on_prompt = held
    stream = backend.run_turn(REQUEST)
    assert (await stream.__anext__()).kind == EVENT_ACCEPTED
    assert (await backend.recover(REQUEST)).state == RECOVERY_RUNNING
    release.set()
    assert [event.kind async for event in stream][-1] == EVENT_SUCCEEDED


async def test_once_a_request_was_dropped_a_missing_one_proves_nothing(agent, make_backend):
    backend = await make_backend(agent.path, remembered_requests=1)
    await collect(backend, request_id="r1")
    await collect(backend, request_id="r2")
    for dropped in ("r0", "r1"):  # never admitted, and admitted then dropped: the record cannot tell them apart
        recovered = await backend.recover(TurnRequest("c1", dropped, "hi"))
        assert (recovered.state, recovered.terminal.text) == (RECOVERY_TERMINAL, UNRECORDED)
    # The dropped request is no longer known as a duplicate: the window is the operator's resource limit.
    assert kinds(await collect(backend, request_id="r1"))[-1] == EVENT_SUCCEEDED


async def test_finishing_an_evicted_turn_does_not_exceed_the_request_window(agent, make_backend, tmp_path):
    release = asyncio.Event()
    started = asyncio.Queue()

    async def held(prompt):
        started.put_nowait(True)
        await release.wait()
        return "end_turn"

    agent.on_prompt = held
    backend = await make_backend(agent.path, remembered_requests=1)
    first = asyncio.create_task(collect(backend, conversation="c1", request_id="r1"))
    await started.get()
    second = asyncio.create_task(collect(backend, conversation="c2", request_id="r2"))
    await started.get()
    release.set()
    await asyncio.gather(first, second)

    stored = json.loads(next((tmp_path / "state").glob("acp_*.json")).read_text())
    assert list(stored["requests"]) == ["c2 r2"]
    recovered = await backend.recover(TurnRequest("c1", "r1", "hi"))
    assert (recovered.state, recovered.terminal.text) == (RECOVERY_TERMINAL, UNRECORDED)


@pytest.mark.parametrize("stored", [
    [], None, {}, {"sessions": []}, {"sessions": {"c1": 7}},
    {"requests": []}, {"requests": {"c1 r1": None}},
    {"requests": {"c1 r1": {"terminal": {}}}},
    {"requests": {"c1 r1": {"terminal": {"kind": "succeeded", "data": []}}}},
    {"dropped": "false"},
])
def test_invalid_state_structure_cannot_prove_a_request_was_not_submitted(tmp_path, stored):
    from belle_agent.integrations.acp.state import State

    if isinstance(stored, dict) and stored:
        stored = {"sessions": {}, "requests": {}, "dropped": False, **stored}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(stored))
    state = State(path, 1)
    state.load()
    assert state.dropped
    assert state.request("c1 r1") is None
    assert state.session("c1") is None
