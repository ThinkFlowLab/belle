"""The adapter against the Docker Agent container and the stand-in model."""

import asyncio
import time
import uuid

import pytest
from docker_agent_kit import DockerAgentKit, asked
from standin import _chunk, call, said, script

from belle_agent import hook_server
from belle_agent.backend import EVENT_FAILED, EVENT_INTERRUPTED, EVENT_SUCCEEDED, EVENT_TEXT_DELTA, TurnRequest
from belle_agent.host import OUTCOME_FAILED, OUTCOME_POSTED_FINAL
from belle_agent.integrations.docker_agent import adapter
from belle_agent.platforms.base import MARK_DONE, MARK_QUEUED, MARK_RECEIVED, MARK_STARTED, MARK_STEERED
from belle_agent.tools.host import ToolHost

pytestmark = pytest.mark.docker_agent_server


def _request(text: str, conversation: str = "c1", request_id: str = "r1") -> TurnRequest:
    return TurnRequest(conversation=conversation, request_id=request_id, text=text)


async def _events(backend, request: TurnRequest) -> list:
    return [event async for event in backend.run_turn(request)]


async def _until_streaming(stream) -> None:
    async for event in stream:
        if event.kind == EVENT_TEXT_DELTA:
            return


async def test_a_conversation_is_one_session_and_keeps_its_earlier_turns(world, model):
    await world.send(world.direct("one SAY=FIRST", sender="U1"))
    await world.send(world.direct("two SAY=SECOND", sender="U1"))
    await world.send(world.direct("three SAY=THIRD", sender="U2"))

    assert [(post.conversation, post.text) for post in world.posts()] == [
        ("D-U1", "FIRST"),
        ("D-U1", "SECOND"),
        ("D-U2", "THIRD"),
    ]
    # The second turn of a conversation is asked with its first. The other conversation holds none of it.
    second = asked(model, "two SAY=SECOND")[-1]
    assert said(second, "user") == ["one SAY=FIRST", "two SAY=SECOND"]
    assert said(second, "assistant") == ["FIRST"]
    assert said(asked(model, "three SAY=THIRD")[-1], "user") == ["three SAY=THIRD"]


def _answer_once(model, monkeypatch, answer: int | str) -> None:
    """Answer the next provider request with ``answer``, an HTTP status or a final text, and the rest by their directives.

    A directive cannot do it: Docker Agent sends the message of a turn that did not end well again, joined to the next.
    """
    answers = iter([answer if isinstance(answer, int) else [_chunk({"content": answer}), _chunk({}, "stop")]])
    monkeypatch.setattr(model, "script", lambda body, session: next(answers, None) or script(body))


async def test_a_provider_error_is_one_failed_event_and_the_next_turn_completes(world, model, monkeypatch):
    backend = world.kit.backend
    _answer_once(model, monkeypatch, 400)

    failed = await _events(backend, _request("first"))

    assert [event.kind for event in failed if event.terminal] == [EVENT_FAILED]
    succeeded = await _events(backend, _request("SAY=fine", request_id="r2"))
    assert (succeeded[-1].kind, succeeded[-1].text) == (EVENT_SUCCEEDED, "fine")


async def test_the_host_tells_the_reader_of_a_failed_turn_and_answers_the_next(world, model, monkeypatch):
    _answer_once(model, monkeypatch, 400)
    await world.send(world.direct("first"))
    await world.send(world.direct("SAY=fine"))

    assert world.outcomes() == [OUTCOME_FAILED, OUTCOME_POSTED_FINAL]
    assert world.posts()[-1].text == "fine"


async def test_cancel_stops_a_running_turn_and_frees_its_session(world, model, monkeypatch):
    backend = world.kit.backend
    assert await backend.cancel("c1") is False

    stream = backend.run_turn(_request("SLOW TICKS=20"))
    await _until_streaming(stream)
    assert await backend.active() == ["c1"]
    started = time.monotonic()
    assert await backend.cancel("c1") is True

    # Twenty ticks at two a second would take ten seconds. The stop does not wait for them.
    assert time.monotonic() - started < 5
    assert [event.kind for event in [event async for event in stream] if event.terminal] == [EVENT_INTERRUPTED]
    assert await backend.active() == []
    # Control: the conversation runs again at once, so Docker Agent let the session go.
    _answer_once(model, monkeypatch, "after")
    assert (await _events(backend, _request("again", request_id="r2")))[-1].text == "after"


async def test_a_turn_whose_reader_leaves_is_stopped_in_docker_agent(world, model, monkeypatch):
    backend = world.kit.backend
    stream = backend.run_turn(_request("SLOW TICKS=20"))
    await _until_streaming(stream)

    await stream.aclose()

    assert await backend.active() == []
    _answer_once(model, monkeypatch, "after")
    assert (await _events(backend, _request("again", request_id="r2")))[-1].text == "after"


async def test_a_turn_whose_tools_do_not_start_fails_and_the_next_one_runs(world, model, monkeypatch):
    backend = world.kit.backend
    # The tool server's token travels under another name, so Docker Agent sends its placeholder and is refused.
    monkeypatch.setattr(adapter, "TOKEN_HEADER", "X-Not-The-Tools-Token")
    before = len(model.log)

    failed = await _events(backend, _request("no tools"))

    assert [(event.kind, event.text.startswith(adapter.TOOLS_MISSING)) for event in failed if event.terminal] == [
        (EVENT_FAILED, True)
    ]
    assert not [event for event in failed if event.kind == EVENT_TEXT_DELTA]
    print(f"provider requests during the refused turn: {len(model.log) - before}")
    monkeypatch.undo()
    assert (await _events(backend, _request("SAY=tooled", request_id="r2")))[-1].text == "tooled"


async def test_a_turn_whose_hooks_cannot_reach_belle_is_told_the_relays_reason_and_not_its_command(world, model):
    backend = world.kit.backend
    before = len(model.log)
    await backend._hooks.close()
    try:
        await world.send(world.direct("unrelayed"))
    finally:
        await backend._hooks.start()

    assert world.outcomes() == [OUTCOME_FAILED]
    told = world.posts()[-1].text
    assert told.startswith("The relay to Belle failed: curl: (7) "), told
    assert "curl -fsS" not in told and "BELLE_RELAY" not in told
    # The prompt's hook ended the run before the model was asked.
    assert len(model.log) == before
    await world.send(world.direct("SAY=relayed"))
    assert world.posts()[-1].text == "relayed"


async def test_a_hook_belle_refuses_ends_the_turn_with_the_endpoints_reason(world, model, monkeypatch):
    backend = world.kit.backend
    monkeypatch.setattr(hook_server, "MAX_REQUEST_BYTES", 16)

    failed = await _events(backend, _request("refused"))

    assert [event.kind for event in failed if event.terminal] == [EVENT_FAILED]
    relayed, refused = failed[-1].text.split("\n")
    assert relayed == "The relay to Belle failed: curl: (22) The requested URL returned error: 413"
    assert refused.startswith(f"{adapter.HOOK_REFUSED}: HTTP 413 for /hook/docker_agent: the event is "), refused
    assert refused.endswith("bytes; a hook event may hold at most 16")
    monkeypatch.undo()
    assert (await _events(backend, _request("SAY=answered", request_id="r2")))[-1].text == "answered"


async def test_a_new_adapter_finds_the_session_of_a_conversation_in_its_state_and_without_it_by_its_title(model):
    namespace = uuid.uuid4().hex
    texts = ["SAY=before", "SAY=after", "SAY=rebuilt"]
    for number, text in enumerate(texts):
        backend = DockerAgentKit(namespace).backend
        if number == 2:
            backend._sessions_file.unlink()
        await backend.start(ToolHost([]))
        try:
            assert (await _events(backend, _request(text, request_id=f"r{number}")))[-1].text == text[4:]
        finally:
            await backend.close()
    assert said(asked(model, "SAY=rebuilt")[-1], "user") == texts


async def test_a_refused_call_does_not_run_and_the_turn_goes_on_to_its_final(build_world, model):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback}\n    delivery: {mode: [all]}\n"
        "    permissions: {tools: {react_to_message: ask}}\n"
    )
    text = call(world.kit.backend.capabilities().tool_name("react_to_message"), emoji="eyes")
    await world.platform.inject(world.message(text, conversation="C1"))
    (shown,) = await world.wait_question(conversation="C1")

    await world.send(world.answer_to(shown, approved=False))
    await world.host.idle()

    assert [action for action in world.platform.log if action.kind == "reaction"] == []
    # Docker Agent tells the model why the call did not run, and the model writes the turn's final.
    assert adapter.REFUSED in "\n".join(said(asked(model, text)[-1], "tool"))
    assert (world.outcomes(), [post.text for post in world.posts()]) == ([OUTCOME_POSTED_FINAL], ["tool done"])


STEER = "scopes:\n  - match: {channel: loopback}\n    delivery: {mode: [all], mid_turn: steer}\n"


async def test_a_message_steered_after_the_last_model_call_gets_a_reply_of_its_own(build_world, model, monkeypatch):
    world = await build_world(STEER)
    first, late = world.message("first SAY=FIRST", thread="root"), world.message("late SAY=LATE", thread="root")
    outcomes = []
    run_turn = world.kit.backend.run_turn

    async def slow_reader(request):
        """A host slow to read: the late message is steered once Docker Agent has ended the first turn."""
        async for event in run_turn(request):
            if not outcomes:
                while await world.kit.backend.active():
                    await asyncio.sleep(0.05)
                outcomes.append(await world.platform.inject(late))
            yield event

    monkeypatch.setattr(world.kit.backend, "run_turn", slow_reader)
    await world.platform.inject(first)
    await world.host.idle()

    assert outcomes[0].startswith("steered:")
    assert [post.text for post in world.posts("C1")] == ["FIRST", "LATE"]
    # Docker Agent gave the message to the model once, in the turn that answered it.
    assert said(asked(model, late.text)[-1], "user") == [first.text, late.text]
    marks = {m.text: [state for mid, state in world.platform.marks if mid == m.message_id] for m in (first, late)}
    assert marks == {
        first.text: [MARK_RECEIVED, MARK_DONE],
        late.text: [MARK_STEERED, MARK_QUEUED, MARK_STARTED, MARK_DONE],
    }
