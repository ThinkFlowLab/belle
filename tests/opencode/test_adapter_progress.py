"""What the adapter reports of a running turn, on the scripted wire: tool names, subagents and the deltas of a reply."""

import asyncio

import httpx
import pytest
from test_adapter_wire import C1, FakeOpenCode, _accepted, _PostToolkit, _rest, opencode  # noqa: F401 - `opencode` is a fixture

from belle_agent.backend import (
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    PROGRESS_EVENTS,
    SUBAGENT,
)
from belle_agent.integrations.opencode import OpenCodeBackend
from belle_agent.tools.host import ToolHost


@pytest.fixture
async def backend(opencode):  # noqa: F811
    """An adapter that serves one of Belle's tools, ``post_message``."""
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([_PostToolkit()]))
    yield backend
    await backend.close()


def _tools(events) -> list[tuple[str, dict]]:
    return [(event.kind, dict(event.data)) for event in events if event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT)]


def test_the_adapter_declares_every_progress_event():
    assert OpenCodeBackend("http://opencode.test", "password").capabilities().reports == PROGRESS_EVENTS


async def test_a_tool_is_named_as_the_core_knows_it_and_not_as_opencode_advertises_it(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    for call, name in (("call_1", "belle_post_message"), ("call_2", "shell"), ("call_3", "belle_not_one_of_ours")):
        opencode.emit("session.tool.input.started", id=call, name=name)
        opencode.emit("session.tool.called", id=call, input={})
        opencode.emit("session.tool.success", id=call, content=[])
    opencode.emit("session.execution.succeeded")

    names = [(kind, data["name"]) for kind, data in _tools(await _rest(stream))]

    assert names == [
        # Belle's own tool loses the name of the server OpenCode mounts it under.
        (EVENT_TOOL_CALLED, "post_message"), (EVENT_TOOL_RESULT, "post_message"),
        # A built-in tool keeps its name, which is the name a scope gives it a level under.
        (EVENT_TOOL_CALLED, "shell"), (EVENT_TOOL_RESULT, "shell"),
        # Control: a name that only looks like one of Belle's is some other server's tool, and is left as it is.
        (EVENT_TOOL_CALLED, "belle_not_one_of_ours"), (EVENT_TOOL_RESULT, "belle_not_one_of_ours"),
    ]


async def test_a_subagent_is_reported_as_the_call_that_runs_it_with_its_type_and_no_more(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    opencode.emit("session.tool.input.started", id="call_1", name="subagent")
    opencode.emit("session.tool.called", id="call_1", input={"agent": "explore", "description": "d", "prompt": "p"})
    # Progress names the child early; its text and tools share the global SSE under another sessionID.
    opencode.parents["ses_child"] = C1
    opencode.emit("session.tool.progress", id="call_1", metadata={"sessionID": "ses_child", "status": "running"})
    opencode.emit("session.tool.input.started", "ses_child", id="call_9", name="shell")
    opencode.emit("session.tool.called", "ses_child", id="call_9", input={})
    opencode.emit("session.text.delta", "ses_child", delta="the child's own words")
    opencode.emit("session.tool.failed", id="call_1", error={"message": "the child failed"})
    opencode.emit("session.tool.input.started", id="call_2", name="shell")
    opencode.emit("session.tool.called", id="call_2", input={})
    opencode.emit("session.tool.success", id="call_2", content=[])
    opencode.emit("session.execution.succeeded")

    events = await _rest(stream)

    assert [
        (kind, data.get(SUBAGENT), data["call_id"], data.get("name"), data.get("child"))
        for kind, data in _tools(events)
    ] == [
        (EVENT_TOOL_CALLED, "explore", "call_1", "subagent", None),
        # Child tools are attributed to the parent subagent call id for peek source_work_id.
        (EVENT_TOOL_CALLED, None, "call_1", "shell", True),
        (EVENT_TOOL_RESULT, "explore", "call_1", "subagent", None),
        # Control: a call that runs no subagent carries no type.
        (EVENT_TOOL_CALLED, None, "call_2", "shell", None),
        (EVENT_TOOL_RESULT, None, "call_2", "shell", None),
    ]
    assert _tools(events)[2][1]["failed"] is True
    child_text = [event for event in events if event.kind == EVENT_TEXT_DELTA]
    assert [(event.text, event.data.get("call_id"), event.data.get("child")) for event in child_text] == [
        ("the child's own words", "call_1", True),
    ]


async def test_the_deltas_of_a_reply_stop_once_events_were_missed_and_the_final_is_whole(opencode, backend):  # noqa: F811
    stream = await _accepted(backend)
    opencode.running.add(C1)
    opencode.emit("session.step.started")
    opencode.emit("session.text.delta", delta="The first ")
    seen = [await anext(stream), await anext(stream)]
    # The event stream drops. "half is lost, " is sent while nobody reads, and OpenCode replays nothing.
    opencode.drop_stream()
    reading = asyncio.create_task(_rest(stream))
    async with asyncio.timeout(5):
        while f"/api/session/{C1}/message" not in opencode.paths("GET"):
            await asyncio.sleep(0.01)
    # The adapter reads events again and finds the turn running. The reply goes on being written.
    opencode.emit("session.text.delta", delta="and the rest arrives.")
    opencode.store("assistant", content=[{"type": "text", "text": "The first half is lost, and the rest arrives."}])
    opencode.store("idle", outcome="succeeded")
    opencode.emit("session.execution.succeeded")

    seen += await reading

    # What is reported stops where the hole begins: a delta after it would read as if it continued the text before.
    assert [event.text for event in seen if event.kind == EVENT_TEXT_DELTA] == ["The first "]
    # The final is read from the session, whole, once.
    assert [(event.kind, event.text) for event in seen if event.terminal] == [
        (EVENT_SUCCEEDED, "The first half is lost, and the rest arrives.")
    ]
