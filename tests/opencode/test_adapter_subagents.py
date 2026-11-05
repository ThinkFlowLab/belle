"""A turn whose subagents run in the background, on the scripted wire.

The sequences are the ones OpenCode 2.0.24 emitted for a ``subagent`` call
made with ``background``: the call returns at once, the execution ends, and
OpenCode runs the session again for the report of each child. The same turns
against the pinned build are in ``test_adapter_subagents_opencode.py``. One
check plays an order that was not recorded and says so.
"""

import asyncio

import httpx
import pytest
from test_adapter_wire import C1, FakeOpenCode, _accepted, _rest, opencode  # noqa: F401 - `opencode` is a fixture

from belle_agent.backend import (
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_STEP,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    SUBAGENT,
)
from belle_agent.integrations.opencode import OpenCodeBackend, adapter
from belle_agent.tools.host import ToolHost

A, B = "ses_child_a", "ses_child_b"
LAUNCHED = "The subagent is working in the background."


@pytest.fixture
async def backend(opencode):  # noqa: F811
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    yield backend
    await backend.close()


def launch(opencode: FakeOpenCode, child: str, call: str) -> None:  # noqa: F811
    """One step that calls ``subagent`` with ``background``: the call returns while its child runs."""
    running = {"sessionID": child, "status": "running"}
    arguments = {"agent": "general", "description": "d", "prompt": "p", "background": True}
    opencode.parents[child] = C1
    opencode.running.add(child)
    opencode.emit("session.step.started")
    opencode.emit("session.tool.input.started", id=call, name="subagent")
    opencode.emit("session.tool.called", id=call, input=arguments)
    opencode.emit("session.tool.progress", id=call, metadata=running)
    opencode.emit("session.tool.success", id=call, content=[{"type": "text", "text": LAUNCHED}], metadata=running)
    opencode.emit("session.step.ended", finish="tool-calls")
    part = {"type": "tool", "id": call, "name": "subagent", "state": {"status": "completed", "metadata": running}}
    opencode.store("assistant", content=[part], finish="tool-calls")


def answer(opencode: FakeOpenCode, text: str) -> None:  # noqa: F811
    """One step that calls no tool and writes ``text``."""
    opencode.emit("session.step.started")
    opencode.emit("session.text.delta", delta=text)
    opencode.emit("session.text.ended", text=text)
    opencode.emit("session.step.ended", finish="stop")
    opencode.store("assistant", content=[{"type": "text", "text": text}], finish="stop")


def succeed(opencode: FakeOpenCode) -> None:  # noqa: F811
    opencode.store("idle", outcome="succeeded")
    opencode.running.discard(C1)
    opencode.emit("session.execution.succeeded")


def shape(events) -> list[tuple]:
    """Each event as its kind and what tells it apart: its text, or the id of its call."""
    return [(event.kind, event.data.get("call_id") or event.text) for event in events]


async def test_a_turn_ends_after_its_last_background_child_reports_and_its_final_is_each_answer_once(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    launch(opencode, B, "call_b")
    answer(opencode, "Both are started.")
    succeed(opencode)
    for child, said in ((A, "The first says ten."), (B, "The second says twenty.")):
        opencode.report(child)
        opencode.wake(child)
        answer(opencode, said)
        succeed(opencode)

    events = await _rest(stream)

    assert shape(events) == [
        (EVENT_STEP, ""), (EVENT_TOOL_CALLED, "call_a"),
        (EVENT_STEP, ""), (EVENT_TOOL_CALLED, "call_b"),
        (EVENT_STEP, ""), (EVENT_TEXT_DELTA, "Both are started."),
        # The first execution ended here. The turn did not: a call gets its result when its child reports.
        (EVENT_TOOL_RESULT, "call_a"), (EVENT_STEP, ""), (EVENT_TEXT_DELTA, "The first says ten."),
        (EVENT_TOOL_RESULT, "call_b"), (EVENT_STEP, ""), (EVENT_TEXT_DELTA, "The second says twenty."),
        (EVENT_SUCCEEDED, "Both are started.\n\nThe first says ten.\n\nThe second says twenty."),
    ]
    results = [event for event in events if event.kind == EVENT_TOOL_RESULT]
    assert [(event.data[SUBAGENT], event.data.get("failed"), "done" in event.text) for event in results] == [("general", None, True)] * 2
    assert not any(path.endswith("/interrupt") for path in opencode.paths())


async def test_a_report_that_reaches_a_running_execution_is_answered_in_it(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    # The child ends while the session still writes its first answer. OpenCode hands the report to that execution.
    opencode.report(A)
    answer(opencode, "It is started.")
    opencode.emit("session.inbox.delivered", inboxID=f"msg_report_{A}")
    answer(opencode, "And it is done.")
    succeed(opencode)

    events = await _rest(stream)

    assert events[-1].kind == EVENT_SUCCEEDED and events[-1].text == "It is started.\n\nAnd it is done."


async def test_an_execution_that_ends_before_it_was_handed_a_report_does_not_end_the_turn(opencode, backend):  # noqa: F811
    """Not a recorded order: a report that arrives as the execution ends, which OpenCode then runs the session for."""
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    opencode.report(A)
    succeed(opencode)
    opencode.wake(A)
    answer(opencode, "And it is done.")
    succeed(opencode)

    events = await _rest(stream)

    assert events[-1].kind == EVENT_SUCCEEDED and events[-1].text == "It is started.\n\nAnd it is done."


async def test_a_child_that_failed_or_was_stopped_ends_its_call_as_failed(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    launch(opencode, B, "call_b")
    answer(opencode, "Both are started.")
    succeed(opencode)
    opencode.report(A, "error", "the provider failed")
    opencode.report(B, "cancelled", "Subagent cancelled")
    opencode.wake(A)
    answer(opencode, "The first failed.")
    opencode.emit("session.inbox.delivered", inboxID=f"msg_report_{B}")
    answer(opencode, "The second was stopped.")
    succeed(opencode)

    events = await _rest(stream)

    results = [(event.data["call_id"], event.data.get("failed")) for event in events if event.kind == EVENT_TOOL_RESULT]
    assert results == [("call_a", True), ("call_b", True)]
    assert events[-1].text == "Both are started.\n\nThe first failed.\n\nThe second was stopped."


async def test_a_call_that_waits_for_its_child_is_one_execution_as_before(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    done = {"sessionID": A, "status": "completed"}
    opencode.emit("session.step.started")
    opencode.emit("session.tool.input.started", id="call_a", name="subagent")
    opencode.emit("session.tool.called", id="call_a", input={"agent": "general", "description": "d", "prompt": "p"})
    opencode.emit("session.tool.progress", id="call_a", metadata={"sessionID": A, "status": "running"})
    opencode.emit("session.tool.success", id="call_a", content=[{"type": "text", "text": "ten"}], metadata=done)
    opencode.emit("session.step.ended", finish="tool-calls")
    answer(opencode, "It says ten.")
    succeed(opencode)

    events = await _rest(stream)

    assert shape(events) == [
        (EVENT_STEP, ""), (EVENT_TOOL_CALLED, "call_a"), (EVENT_TOOL_RESULT, "call_a"),
        (EVENT_STEP, ""), (EVENT_TEXT_DELTA, "It says ten."), (EVENT_SUCCEEDED, "It says ten."),
    ]


async def test_a_child_sessions_text_and_tools_are_forwarded_on_the_parent_subagent_call(opencode, backend):  # noqa: F811
    """Child SSE rides the same stream under another sessionID; peek attributes via call_id."""
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    opencode.emit("session.tool.input.started", A, id="call_shell", name="shell")
    opencode.emit("session.tool.called", A, id="call_shell", input={"command": "echo hi"})
    opencode.emit("session.text.delta", A, delta="child is writing")
    opencode.emit("session.tool.success", A, id="call_shell", content=[{"type": "text", "text": "hi"}])
    answer(opencode, "Parent notes the launch.")
    succeed(opencode)
    opencode.report(A)
    opencode.wake(A)
    answer(opencode, "Child is done.")
    succeed(opencode)

    events = await _rest(stream)

    child_tools = [
        (
            event.kind, event.data.get("name"), event.data.get("call_id"),
            event.data.get(SUBAGENT), event.data.get("child"),
        )
        for event in events
        if event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT) and event.data.get("name") == "shell"
    ]
    assert child_tools == [
        (EVENT_TOOL_CALLED, "shell", "call_a", None, True),
        (EVENT_TOOL_RESULT, "shell", "call_a", None, True),
    ]
    attributed = [event for event in events if event.kind == EVENT_TEXT_DELTA and event.data.get("call_id") == "call_a"]
    assert [event.text for event in attributed] == ["child is writing"]


async def test_a_stop_while_a_child_runs_interrupts_the_child_and_the_execution_its_report_starts(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    succeed(opencode)
    events = [await anext(stream) for _ in range(4)]

    # The session is idle, so an interrupt of it alone stops nothing. The stop is taken all the same.
    assert await backend.cancel("c1") is True
    events += await _rest(stream)

    assert [event.kind for event in events if event.terminal] == [EVENT_INTERRUPTED]
    interrupts = [path.split("/")[3] for path in opencode.paths() if path.endswith("/interrupt")]
    # The session, its child, and the session again once the child's report has started an execution in it.
    assert interrupts == [C1, A, C1]
    assert opencode.running == set()
    assert not backend._turns


async def test_a_reader_that_leaves_while_a_child_runs_stops_the_child_too(opencode, backend):  # noqa: F811
    """What the host does at a turn's time limit: it leaves the stream."""
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    succeed(opencode)
    for _ in range(4):
        await anext(stream)

    await stream.aclose()

    assert [path.split("/")[3] for path in opencode.paths() if path.endswith("/interrupt")] == [C1, A, C1]
    assert opencode.running == set()


async def test_an_execution_that_fails_while_a_child_runs_ends_the_turn_and_stops_the_child(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    opencode.emit("session.execution.failed", error={"type": "provider.internal", "message": "the provider failed"})

    events = await _rest(stream)

    assert [(event.kind, event.text) for event in events if event.terminal] == [(EVENT_FAILED, "the provider failed")]
    assert A not in opencode.running


async def test_a_turn_that_fails_as_the_host_stops_still_stops_its_child(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    backend.shutting_down()
    opencode.emit("session.execution.failed", error={"type": "provider.internal", "message": "the provider failed"})

    events = await _rest(stream)

    assert [event.kind for event in events if event.terminal] == [EVENT_FAILED]
    assert A not in opencode.running


async def test_a_failure_after_an_answer_keeps_the_answer(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    succeed(opencode)
    opencode.report(A)
    opencode.wake(A)
    opencode.emit("session.execution.failed", error={"type": "provider.internal", "message": "the provider failed"})

    events = await _rest(stream)

    assert [(event.kind, event.text) for event in events if event.terminal] == [
        (EVENT_FAILED, "It is started.\n\nThe model's response stopped: the provider failed.")
    ]


async def test_a_stopped_turn_does_not_wait_for_ever_for_a_child_that_never_reports(opencode, backend, monkeypatch):  # noqa: F811
    monkeypatch.setattr(adapter, "STOP_TIMEOUT_SECONDS", 0.05)
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    succeed(opencode)
    for _ in range(4):
        await anext(stream)
    opencode.reports_interrupt = False

    assert await backend.cancel("c1") is True
    async with asyncio.timeout(5):
        (ended,) = await _rest(stream)

    assert ended.kind == EVENT_INTERRUPTED
    assert not backend._turns


async def _reconnected(opencode: FakeOpenCode, stream) -> asyncio.Task:  # noqa: F811
    """Drop the event stream and return the reading of the turn once the adapter has read the session back."""
    asked = len(opencode.paths("GET"))
    opencode.drop_stream()
    reading = asyncio.create_task(_rest(stream))
    async with asyncio.timeout(5):
        while f"/api/session/{C1}/message" not in opencode.paths("GET")[asked:]:
            await asyncio.sleep(0.01)
    return reading


async def test_a_lost_stream_while_a_child_runs_does_not_end_the_turn_and_the_final_is_read_whole(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    succeed(opencode)
    seen = [await anext(stream) for _ in range(4)]

    # The session is idle and has ended an execution. Its child still runs, so the turn does.
    reading = await _reconnected(opencode, stream)
    await asyncio.sleep(0.05)
    assert not reading.done()
    opencode.report(A)
    opencode.wake(A)
    answer(opencode, "And it is done.")
    succeed(opencode)

    seen += await reading

    assert [(event.kind, event.text) for event in seen if event.terminal] == [
        (EVENT_SUCCEEDED, "It is started.\n\nAnd it is done.")
    ]


async def test_what_a_child_did_while_no_event_could_be_read_is_read_from_the_session(opencode, backend, monkeypatch):  # noqa: F811
    # A child whose report is stored is not one the turn gives a moment to. This read must not take that moment.
    monkeypatch.setattr(adapter, "RECONCILE_SETTLE_SECONDS", 60)
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    succeed(opencode)
    seen = [await anext(stream) for _ in range(4)]
    # The stream drops. The child reports and the session answers it, and no event of that is seen.
    opencode.stream.put_nowait(None)
    await asyncio.sleep(0)
    opencode.running.discard(A)
    metadata = {"source": "subagent", "childID": A, "state": "completed"}
    opencode.store("synthetic", metadata=metadata, text="<subagent>ten</subagent>")
    opencode.store("assistant", content=[{"type": "text", "text": "And it is done."}], finish="stop")
    opencode.store("idle", outcome="succeeded")

    async with asyncio.timeout(5):
        seen += await _rest(stream)

    assert [(event.kind, event.text) for event in seen if event.terminal] == [
        (EVENT_SUCCEEDED, "It is started.\n\nAnd it is done.")
    ]


async def test_a_child_that_was_lost_with_its_process_does_not_hold_the_turn(opencode, backend, monkeypatch):  # noqa: F811
    monkeypatch.setattr(adapter, "RECONCILE_SETTLE_SECONDS", 0)
    stream = await _accepted(backend)
    launch(opencode, A, "call_a")
    answer(opencode, "It is started.")
    succeed(opencode)
    seen = [await anext(stream) for _ in range(4)]
    # OpenCode restarted: the child no longer runs and will never report.
    opencode.running.discard(A)
    opencode.drop_stream()

    async with asyncio.timeout(5):
        seen += await _rest(stream)

    assert [(event.kind, event.text) for event in seen if event.terminal] == [(EVENT_SUCCEEDED, "It is started.")]


async def test_a_call_that_waits_for_its_child_is_not_read_back_as_a_child_in_the_background(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    waiting = {"sessionID": A, "status": "running"}
    opencode.running |= {C1, A}
    opencode.emit("session.step.started")
    opencode.emit("session.tool.input.started", id="call_a", name="subagent")
    opencode.emit("session.tool.called", id="call_a", input={"agent": "general", "description": "d", "prompt": "p"})
    seen = [await anext(stream) for _ in range(2)]
    # The stream drops while the call waits. The session stores the call as running, with its child running.
    part = {"type": "tool", "id": "call_a", "name": "subagent", "state": {"status": "running", "metadata": waiting}}
    opencode.store("assistant", content=[part])
    reading = await _reconnected(opencode, stream)
    opencode.running.discard(A)
    opencode.emit("session.tool.success", id="call_a", content=[{"type": "text", "text": "ten"}], metadata={"status": "completed"})
    opencode.messages[C1][-1].update(finish="tool-calls")
    answer(opencode, "It says ten.")
    succeed(opencode)

    async with asyncio.timeout(5):
        seen += await reading

    # No report will come for a call that waited, and the turn does not wait for one.
    assert [(event.kind, event.text) for event in seen if event.terminal] == [(EVENT_SUCCEEDED, "It says ten.")]
