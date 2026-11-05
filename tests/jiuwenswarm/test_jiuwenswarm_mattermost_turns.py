"""The host's turns run as Gateway requests: what a turn sends, and what its events become."""

import asyncio
import time
from dataclasses import replace

import pytest
from jiuwenswarm.extensions.sdk.host_types import EventType, Message, ReqMethod

from belle_agent.backend import (
    DUPLICATE,
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_QUESTION,
    EVENT_STEP,
    EVENT_SUBAGENT_UPDATE,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    Answer,
    TurnRequest,
    TurnScope,
)
from belle_agent.integrations.jiuwenswarm import channel_turns
from belle_agent.integrations.jiuwenswarm.channel_turns import JiuwenSwarmTurns
from belle_agent.models import ConfiguredModels
from belle_agent.silence import REPLY_OPTIONAL_KEY, SILENCE_UNSTORED_KEY

METADATA = {"mattermost_channel_id": "c" * 26, "mattermost_user_id": "a" * 26, "mattermost_server": ""}
SESSION = "mattermost_direct_" + "c" * 26
TURN_KEYS = {"channel": "mattermost", "chat_key": "mattermost_channel_id", "user_key": "mattermost_user_id"}


def request(request_id: str = "p1", **fields) -> TurnRequest:
    return TurnRequest(conversation=SESSION, request_id=request_id, text="hello", metadata=METADATA, **fields)


def event(request_id: str, kind: EventType, payload: dict) -> Message:
    return Message(id=request_id, type="event", channel_id="mattermost", session_id=SESSION, params={},
                   timestamp=time.time(), ok=True, payload=payload, event_type=kind)


class Gateway:
    """What the channel routes, in order."""

    def __init__(self) -> None:
        self.routed: list[Message] = []

    async def route(self, msg: Message) -> None:
        self.routed.append(msg)


async def started(turns: JiuwenSwarmTurns, gateway: Gateway, turn: TurnRequest):
    """A turn whose request has reached the Gateway."""
    events = turns.run_turn(turn)
    first = asyncio.ensure_future(events.__anext__())
    while not gateway.routed:
        await asyncio.sleep(0)
    return events, first


def test_a_request_carries_the_scope_the_text_and_the_conversation() -> None:
    turns = JiuwenSwarmTurns(Gateway().route, **TURN_KEYS, models=lambda: ConfiguredModels(("deep",)))
    assert turns.capabilities().scheduler == "jiuwenswarm"
    scope = TurnScope(
        expert="helper", skills_available=frozenset({"b", "a"}), skills_required=("a",),
        subagents_available=frozenset({"r"}), system_additions=("Reply to the sender.",), withhold_final=True,
    )
    msg = turns.request_message(request(scope=scope))
    assert (msg.channel_id, msg.session_id, msg.chat_id, msg.user_id) == ("mattermost", SESSION, "c" * 26, "a" * 26)
    assert (msg.req_method, msg.is_stream, msg.id) == (ReqMethod.CHAT_SEND, True, "p1")
    assert msg.params["query"] == msg.params["content"] == "hello\n\nReply to the sender."
    assert {key: msg.params[key] for key in msg.params if key not in ("query", "content")} == {
        "agent_template_name": "helper", "agent_skills_available": ["a", "b"], "agent_skills_required": ["a"],
        "agent_subagents_available": ["r"], "model_name": "deep",
    }
    assert msg.metadata[REPLY_OPTIONAL_KEY] is msg.metadata[SILENCE_UNSTORED_KEY] is True


def test_a_scope_model_comes_before_the_default_and_an_empty_expert_clears_one() -> None:
    turns = JiuwenSwarmTurns(Gateway().route, **TURN_KEYS, models=lambda: ConfiguredModels(("deep",)))
    params = turns.request_message(request(scope=TurnScope(model="fast"))).params
    assert (params["model_name"], params["agent_template_name"]) == ("fast", "")
    assert SILENCE_UNSTORED_KEY not in turns.request_message(request()).metadata


def test_files_are_saved_where_the_agent_server_reads_uploads(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(channel_turns, "get_agent_sessions_dir", lambda: tmp_path)
    files = ({"uri": "data:text/plain;base64,aGk=", "name": "../note.txt"},)
    msg = JiuwenSwarmTurns(Gateway().route, **TURN_KEYS).request_message(request(files=files))
    saved = tmp_path / SESSION / "uploads" / "note.txt"
    assert saved.read_bytes() == b"hi"
    assert msg.params["files"] == {"uploaded_documents": msg.params["media_items"]}
    assert msg.params["media_items"][0]["path"] == str(saved)
    assert str(saved) in msg.params["query"]


async def test_events_of_the_request_become_the_turns_events_up_to_its_final() -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    events, first = await started(turns, gateway, request())
    assert not turns.deliver(event("other", EventType.CHAT_DELTA, {"content": "not mine"}))
    for msg in (
        event("p1", EventType.CHAT_DELTA, {"content": "thinking", "source_chunk_type": "llm_reasoning"}),
        event("p1", EventType.CHAT_DELTA, {"content": "Hel"}),
        event("p1", EventType.CHAT_TOOL_CALL, {"tool_call": {"name": "post_message", "tool_call_id": "t1"}}),
        event("p1", EventType.CHAT_TOOL_RESULT, {"tool_result": {"tool_name": "post_message", "tool_call_id": "t1", "is_error": True}}),
        event("p1", EventType.CHAT_DELTA, {"content": "lo"}),
        event("p1", EventType.CHAT_FINAL, {"content": "Hello"}),
    ):
        assert turns.deliver(msg)
    seen = [await first]
    async for item in events:
        seen.append(item)
    assert [(item.kind, item.text, dict(item.data)) for item in seen] == [
        (EVENT_TEXT_DELTA, "thinking", {"thinking": True}),
        (EVENT_TEXT_DELTA, "Hel", {}),
        (EVENT_TOOL_CALLED, "", {"name": "post_message", "call_id": "t1"}),
        (EVENT_TOOL_RESULT, "", {"name": "post_message", "call_id": "t1", "failed": True}),
        (EVENT_STEP, "", {}),
        (EVENT_TEXT_DELTA, "lo", {}),
        # JiuwenSwarm's final joins the text of the two model calls; each is a paragraph, as it was shown.
        (EVENT_SUCCEEDED, "Hel\n\nlo", {}),
    ]
    assert not turns.deliver(event("p1", EventType.CHAT_FINAL, {"content": "late"}))


async def test_a_final_that_is_not_the_text_of_the_model_calls_ends_the_turn_as_it_came() -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    events, first = await started(turns, gateway, request())
    for msg in (
        event("p1", EventType.CHAT_DELTA, {"content": "One moment."}),
        event("p1", EventType.CHAT_TOOL_RESULT, {"tool_result": {"tool_name": "search", "tool_call_id": "t1"}}),
        event("p1", EventType.CHAT_DELTA, {"content": "Found it."}),
        event("p1", EventType.CHAT_FINAL, {"content": "Found it."}),
    ):
        assert turns.deliver(msg)
    seen = [await first]
    async for item in events:
        seen.append(item)
    assert (seen[-1].kind, seen[-1].text) == (EVENT_SUCCEEDED, "Found it.")


async def test_an_error_fails_the_turn_and_a_second_run_of_one_request_runs_nothing() -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    _events, first = await started(turns, gateway, request())
    turns.deliver(event("p1", EventType.CHAT_ERROR, {"content": "model unreachable"}))
    assert ((await first).kind, (await first).text) == (EVENT_FAILED, "model unreachable")
    again = [item async for item in turns.run_turn(request())]
    assert [(item.kind, dict(item.data)) for item in again] == [(EVENT_FAILED, {DUPLICATE: True})]
    assert len(gateway.routed) == 1


async def test_subtask_updates_report_backend_subagent_state() -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    events, first = await started(turns, gateway, request())
    assert turns.deliver(event("p1", EventType.CHAT_SUBTASK_UPDATE,
                               {"subagent_id": "child-1", "subagent_type": "research_agent",
                                "status": "running"}))
    assert turns.deliver(event("p1", EventType.CHAT_SUBTASK_UPDATE,
                               {"subagent_id": "child-1", "subagent_type": "research_agent",
                                "status": "closed", "turn_outcome": "completed"}))
    turns.deliver(event("p1", EventType.CHAT_FINAL, {"content": "done"}))
    seen = [await first, *[item async for item in events]]
    assert [(item.kind, item.data.get("state")) for item in seen] == [
        (EVENT_SUBAGENT_UPDATE, "running"), (EVENT_SUBAGENT_UPDATE, "completed"),
        (EVENT_SUCCEEDED, None)]


PERMISSION = {
    "request_id": "q1",
    "source": "permission_interrupt",
    "questions": [{
        "header": "Approve", "question": "Run react_to_message?",
        "options": [{"label": "Allow once", "value": "allow_once"}, {"label": "Reject", "value": "reject"}],
    }],
}


async def test_a_permission_question_is_answered_by_a_resume_whose_events_end_the_turn() -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    events, first = await started(turns, gateway, request(scope=TurnScope(expert="helper")))
    turns.deliver(event("p1", EventType.CHAT_ASK_USER_QUESTION, PERMISSION))
    asked = await first
    assert asked.kind == EVENT_QUESTION
    assert (asked.question.id, asked.question.text, asked.question.options) == (
        "q1", "Approve: Run react_to_message?", ("Allow once", "Reject")
    )
    # The paused request ends. Its turn waits for the resume.
    assert turns.deliver(event("p1", EventType.CHAT_FINAL, {"content": "paused"}))
    await turns.answer(SESSION, "q1", Answer(values=("Allow once",)))
    resume = gateway.routed[-1]
    assert (resume.req_method, resume.is_stream, resume.session_id) == (ReqMethod.CHAT_SEND, True, SESSION)
    assert resume.params["answers"] == [
        {"question": "Run react_to_message?", "selected_options": ["allow_once"], "custom_input": ""}
    ]
    assert (resume.params["source"], resume.params["request_id"], resume.params["agent_template_name"]) == (
        "permission_interrupt", "q1", "helper"
    )
    assert turns.deliver(event(resume.id, EventType.CHAT_FINAL, {"content": "reacted"}))
    assert [(item.kind, item.text) async for item in events] == [(EVENT_SUCCEEDED, "reacted")]


async def test_a_question_of_a_tool_is_answered_in_place() -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    events, first = await started(turns, gateway, request())
    turns.deliver(event("p1", EventType.CHAT_ASK_USER_QUESTION, {**PERMISSION, "source": "ask_user"}))
    await first
    await turns.answer(SESSION, "q1", Answer(values=("Reject",)))
    assert (gateway.routed[-1].req_method, gateway.routed[-1].is_stream) == (ReqMethod.CHAT_ANSWER, False)
    turns.deliver(event("p1", EventType.CHAT_FINAL, {"content": "done"}))
    assert [item.kind async for item in events] == [EVENT_SUCCEEDED]


async def test_a_stop_cancels_the_request_and_interrupts_the_turn() -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    events, first = await started(turns, gateway, request())
    assert await turns.cancel(SESSION)
    assert (gateway.routed[-1].req_method, gateway.routed[-1].params) == (
        ReqMethod.CHAT_CANCEL, {"intent": "cancel", "session_id": SESSION}
    )
    assert (await first).kind == EVENT_INTERRUPTED
    await events.aclose()
    assert not await turns.cancel(SESSION)


@pytest.mark.parametrize("payload", [{}, {"request_id": "q2", "questions": []}])
async def test_a_question_with_nothing_to_answer_is_not_shown(payload) -> None:
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS)
    _events, first = await started(turns, gateway, request())
    turns.deliver(event("p1", EventType.CHAT_ASK_USER_QUESTION, payload))
    turns.deliver(event("p1", EventType.CHAT_FINAL, {"content": "done"}))
    assert (await first).kind == EVENT_SUCCEEDED


async def test_identical_request_ids_in_different_conversations_keep_their_own_events():
    gateway = Gateway()
    turns = JiuwenSwarmTurns(gateway.route, **TURN_KEYS, session_id=lambda name: f"host-{name}")
    first = turns.run_turn(request())
    second = turns.run_turn(replace(request(), conversation="other-conversation"))
    pending = [asyncio.create_task(stream.__anext__()) for stream in (first, second)]
    while len(gateway.routed) < 2:
        await asyncio.sleep(0)
    try:
        for sent in reversed(gateway.routed):
            final = event(sent.id, EventType.CHAT_FINAL, {"content": sent.session_id})
            final.session_id = sent.session_id
            assert turns.deliver(final)
        results = await asyncio.wait_for(asyncio.gather(*pending), timeout=1)
        assert [result.text for result in results] == [f"host-{SESSION}", "host-other-conversation"]
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await first.aclose()
        await second.aclose()
