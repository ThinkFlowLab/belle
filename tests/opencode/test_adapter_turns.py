"""The adapter against the OpenCode container, with no plugin loaded and the stand-in model."""

import asyncio
import json
import re

import pytest
from drive import Client
from standin import _chunk, is_title, last_user, said

from belle_agent.backend import (
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_STEP,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    TurnRequest,
)
from belle_agent.host import OUTCOME_FAILED, OUTCOME_POSTED_FINAL
from belle_agent.integrations.opencode import adapter
from belle_agent.integrations.opencode.sessions import message_id

pytestmark = pytest.mark.adapter


def _request(text: str, conversation: str = "c1", request_id: str = "r1") -> TurnRequest:
    return TurnRequest(conversation=conversation, request_id=request_id, text=text)


async def _events(backend, request: TurnRequest) -> list:
    return [event async for event in backend.run_turn(request)]


async def _until_streaming(stream) -> None:
    async for event in stream:
        if event.kind == EVENT_TEXT_DELTA:
            return


def _session(model, text: str) -> str:
    """The OpenCode session of the latest turn whose message was ``text``."""
    return next(entry["session"] for entry in reversed(model.log) if said(entry["body"], "user")[-1:] == [text])


@pytest.mark.no_plugin
async def test_no_plugin_is_loaded(world):
    # The adapter drives a plain OpenCode: without the relay plugin no TypeScript of Belle's runs in it.
    sources = {plugin["id"]: plugin["source"]["type"] for plugin in Client().api("GET", "/api/plugin")}
    assert set(sources.values()) == {"builtin"}, sources


async def test_a_conversation_is_one_session_and_keeps_its_earlier_turns(world, model):
    await world.send(world.direct("one SAY=FIRST", sender="U1"))
    await world.send(world.direct("two SAY=SECOND", sender="U1"))
    await world.send(world.direct("three SAY=THIRD", sender="U2"))

    first, second, other = (_session(model, request.text) for request in world.requests)
    assert first == second != other
    assert [(post.conversation, post.text) for post in world.posts()] == [
        ("D-U1", "FIRST"),
        ("D-U1", "SECOND"),
        ("D-U2", "THIRD"),
    ]
    # The second provider request of the conversation holds its first turn.
    request = model.requests(first)[-1]
    assert said(request, "user") == ["one SAY=FIRST", "two SAY=SECOND"]
    assert said(request, "assistant") == ["FIRST"]
    # The other conversation's session holds none of it.
    assert said(model.requests(other)[-1], "user") == ["three SAY=THIRD"]


async def test_a_provider_error_is_one_failed_event_and_the_next_turn_completes(world):
    backend = world.kit.backend

    failed = await _events(backend, _request("FAIL=400"))

    assert [event.kind for event in failed if event.terminal] == [EVENT_FAILED]
    data = dict(failed[-1].data)
    assert data.pop("backend_session").startswith("ses_")
    assert data == {"failure_kind": "provider", "status": 400, "error_type": "provider.invalid-request"}
    succeeded = await _events(backend, _request("SAY=fine", request_id="r2"))
    assert (succeeded[-1].kind, succeeded[-1].text) == (EVENT_SUCCEEDED, "fine")


async def test_the_host_tells_the_reader_of_a_failed_turn_and_answers_the_next(world):
    await world.send(world.direct("FAIL=400"))
    await world.send(world.direct("SAY=fine"))

    assert world.outcomes() == [OUTCOME_FAILED, OUTCOME_POSTED_FINAL]
    assert [post.text for post in world.posts()] == ["scripted failure", "fine"]


async def test_a_second_prompt_during_a_turn_is_refused_and_reaches_no_model(world, model):
    backend = world.kit.backend
    first = backend.run_turn(_request("SLOW TICKS=6"))
    await _until_streaming(first)

    refused = await _events(backend, _request("SAY=joined", request_id="r2"))

    assert [(event.kind, event.text) for event in refused] == [(EVENT_FAILED, adapter.ALREADY_RUNNING)]
    rest = [event async for event in first]
    # The first turn ends as its own, with its own text and nothing of the second prompt.
    assert (rest[-1].kind, rest[-1].text) == (EVENT_SUCCEEDED, "t0 t1 t2 t3 t4 t5 ")
    session = _session(model, "SLOW TICKS=6")
    assert [said(request, "user") for request in model.requests(session)] == [["SLOW TICKS=6"]]


async def test_cancel_stops_a_running_turn(world):
    backend = world.kit.backend
    assert await backend.cancel("c1") is False

    stream = backend.run_turn(_request("SLOW TICKS=20"))
    await _until_streaming(stream)
    assert await backend.cancel("c1") is True

    assert [event.kind for event in [event async for event in stream] if event.terminal] == [EVENT_INTERRUPTED]
    # Control: the conversation runs again.
    assert (await _events(backend, _request("SAY=after", request_id="r2")))[-1].text == "after"


async def test_a_turn_whose_reader_leaves_is_stopped_in_opencode(world, model):
    backend = world.kit.backend
    stream = backend.run_turn(_request("SLOW TICKS=20"))
    await _until_streaming(stream)

    await stream.aclose()

    session = _session(model, "SLOW TICKS=20")
    assert await asyncio.to_thread(lambda: Client().api("GET", f"/api/session/{session}")["outcome"]) == "interrupted"
    assert (await _events(backend, _request("SAY=after", request_id="r2")))[-1].text == "after"


async def _stored(backend, model, text: str) -> list:
    """The messages of the turn of ``text``, as OpenCode's HTTP API lists them, oldest first."""
    prompt = message_id(backend._namespace, "c1", "r1")
    return await backend._messages_since_prompt(_session(model, text), prompt)


# What OpenCode puts to the model after a response cut at the output limit.
CONTINUE = "Your last response hit the output token limit"
# OpenCode's message when a response is cut three times in a row.
STILL_CUT = "Response still truncated after two output token limit continuations"
# What OpenCode answers for a tool call that a cut response made.
NOT_RUN = "The tool call was not executed because the output token limit"
# OpenCode's message for a response that the provider ends with finish_reason "content_filter".
BLOCKED = "Provider blocked the response"


@pytest.fixture
def cutting(model):
    """A model that cuts each response at the output limit until told how often: ``CUT=<n>`` cuts n responses.

    Response ``i``, from 0, says ``[i]``. ``CUT=call`` cuts a response that calls ``read``, and answers what
    comes back. ``BREAK=<n>`` ends n responses with no finish_reason instead, ``BREAK=odd`` ends one with the
    finish_reason "banana", which OpenCode does not know, and ``BREAK=filter`` ends one with finish_reason
    "content_filter".
    """
    def answer(body, session):
        users = said(body, "user")
        if users == ["CUT=call"]:
            if last_user(body)[1]:
                return [_chunk({"content": "Done."}), _chunk({}, "stop")]
            read = {"index": 0, "id": "call_cut", "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"path": "/workspace"})}}
            return [_chunk({"content": "I will read the folder."}), _chunk({"tool_calls": [read]}), _chunk({}, "length")]
        broken = re.match(r"BREAK=(\d+|odd|filter)$", users[0]) if users and not is_title(body) else None
        if broken:
            done = sum(adapter.CONTINUE_BROKEN in text for text in users)
            if broken.group(1) == "filter":
                return [_chunk({"content": "[0]"}), _chunk({}, "content_filter")]
            if broken.group(1) == "odd":
                return [_chunk({"content": f"[{done}]"}), _chunk({}, "stop" if done else "banana")]
            return [_chunk({"content": f"[{done}]"})] + ([] if done < int(broken.group(1)) else [_chunk({}, "stop")])
        cut = re.match(r"CUT=(\d+)$", users[0]) if users and not is_title(body) else None
        if not cut:
            return kept(body, session)
        done = sum(CONTINUE in text for text in users)
        return [_chunk({"content": f"[{done}]"}), _chunk({}, "length" if done < int(cut.group(1)) else "stop")]

    model.script, kept = answer, model.script
    yield model
    model.script = kept


async def test_a_response_cut_at_the_output_limit_is_continued_into_one_answer(world, cutting):
    ended = await _events(world.kit.backend, _request("CUT=1"))

    assert (ended[-1].kind, ended[-1].text) == (EVENT_SUCCEEDED, "[0][1]")
    # The continuation is a step of its own, and it says that it continues the cut text.
    assert [event.data for event in ended if event.kind == EVENT_STEP] == [{}, {"continues": True}]
    # The continuation is a request of its own, after OpenCode's prompt to go on.
    assert CONTINUE in last_user(cutting.requests(_session(cutting, "CUT=1"))[-1])[0]
    # The stored messages give the same answer.
    stored = await _stored(world.kit.backend, cutting, "CUT=1")
    assert adapter.stored(stored, "succeeded").text == "[0][1]"


async def test_a_response_still_cut_after_the_continuations_fails_with_opencodes_reason(world, cutting):
    ended = await _events(world.kit.backend, _request("CUT=3"))

    final = f"[0][1][2]\n\nThe model's response stopped: {STILL_CUT}."
    assert (ended[-1].kind, ended[-1].text) == (EVENT_FAILED, final)
    assert len(cutting.requests(_session(cutting, "CUT=3"))) == 3
    # The stored messages end with the idle message that holds the reason, and give the same final.
    stored = await _stored(world.kit.backend, cutting, "CUT=3")
    assert (stored[-1]["type"], stored[-1]["error"]["message"]) == ("idle", STILL_CUT)
    assert adapter.stored(stored, "failed").text == final


async def test_a_cut_response_that_called_a_tool_is_not_continued_and_its_text_is_no_answer(world, cutting):
    ended = await _events(world.kit.backend, _request("CUT=call"))

    assert (ended[-1].kind, ended[-1].text) == (EVENT_SUCCEEDED, "Done.")
    # OpenCode did not run the cut call and put no prompt to go on: the next request answers the failure of the call.
    requests = cutting.requests(_session(cutting, "CUT=call"))
    assert len(requests) == 2 and last_user(requests[-1]) == ("CUT=call", 1)
    assert NOT_RUN in said(requests[-1], "tool")[-1]
    # OpenCode stores the call as one that did not run, and the stored messages give the same answer.
    stored = await _stored(world.kit.backend, cutting, "CUT=call")
    steps = [message for message in stored if message["type"] == "assistant"]
    calls = [part for step in steps for part in step["content"] if part["type"] == "tool"]
    assert [call.get("executed") for call in calls] == [False]
    assert adapter.stored(stored, "succeeded").text == "Done."


@pytest.mark.parametrize("prompt", ["BREAK=1", "BREAK=odd"])
async def test_a_response_that_stops_after_it_starts_is_continued_into_one_answer(world, cutting, prompt):
    backend = world.kit.backend
    turn = asyncio.create_task(_events(backend, _request(prompt)))
    # While OpenCode waits to retry the failed step, the step holds the retry and the text it wrote.
    waiting = []
    async with asyncio.timeout(30):
        while not waiting and not turn.done():
            if any(said(entry["body"], "user")[-1:] == [prompt] for entry in cutting.log):
                stored = await _stored(backend, cutting, prompt)
                waiting = [message for message in stored if message["type"] == "assistant" and message.get("retry")]
            await asyncio.sleep(0.05)
    ended = await turn

    assert (ended[-1].kind, ended[-1].text) == (EVENT_SUCCEEDED, "[0][1]")
    assert [event.data for event in ended if event.kind == EVENT_STEP] == [{}, {"continues": True}]
    (failed,) = waiting
    assert failed["error"] and failed["retry"]["attempt"] and adapter._text(failed["content"]) == "[0]"
    # The continuation request ends with OpenCode's prompt to go on, which the adapter's constant holds whole.
    assert last_user(cutting.requests(_session(cutting, prompt))[-1])[0] == adapter.CONTINUE_BROKEN
    # The failed step keeps its retry after the next step started, and the stored prompt follows it.
    stored = await _stored(backend, cutting, prompt)
    assert [message["id"] for message in stored if message.get("retry")] == [failed["id"]]
    assert [message["text"] for message in stored if message["type"] == "synthetic"] == [adapter.CONTINUE_BROKEN]
    assert adapter.stored(stored, "succeeded").text == "[0][1]"


async def test_a_response_that_fails_after_it_starts_and_is_not_continued_fails_after_its_text(world, cutting):
    ended = await _events(world.kit.backend, _request("BREAK=filter"))

    final = f"[0]\n\nThe model's response stopped: {BLOCKED}."
    assert (ended[-1].kind, ended[-1].text) == (EVENT_FAILED, final)
    assert len(cutting.requests(_session(cutting, "BREAK=filter"))) == 1
    stored = await _stored(world.kit.backend, cutting, "BREAK=filter")
    assert adapter.stored(stored, "failed").text == final
