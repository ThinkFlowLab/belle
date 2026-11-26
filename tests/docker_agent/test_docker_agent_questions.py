"""Questions of Docker Agent's `user_prompt`, against the Docker Agent container and the stand-in model."""

import asyncio
import json

import pytest
from agent_yaml import SUBAGENT
from docker_agent_kit import asked, helper_reads_its_task
from standin import call, said

from belle_agent.backend import QuestionClosed
from belle_agent.host import OUTCOME_POSTED_FINAL
from belle_agent.integrations.docker_agent import adapter

pytestmark = pytest.mark.docker_agent_server

ANSWER_EVERYTHING = "scopes:\n  - match: {channel: loopback}\n    delivery: {mode: [all]}\n"
CHOICE = {"type": "string", "enum": ["red", "blue"]}


def _session(stream: list[dict]) -> str:
    return next(event["session_id"] for event in stream if event.get("type") == "stream_started")


def _result(model, text: str) -> dict:
    """What ``user_prompt`` answered the model in the turn whose message is ``text``."""
    return json.loads(said(asked(model, text)[-1], "tool")[-1])


@pytest.fixture
def stream(monkeypatch) -> list[dict]:
    """The events the adapter reads, from the turns' streams and the elicitations of the sessions' event logs."""
    seen = []

    def translate(event, turn):
        seen.append(dict(event))
        return original(event, turn)

    async def elicit(self, event, turn):
        seen.append(dict(event))
        return await elicited(self, event, turn)

    original, elicited = adapter.translate, adapter.DockerAgentBackend._elicit
    monkeypatch.setattr(adapter, "translate", translate)
    monkeypatch.setattr(adapter.DockerAgentBackend, "_elicit", elicit)
    return seen


async def _logged(backend, session: str, until: str) -> list[dict]:
    """The events of the session's event log, up to the first of type ``until``."""
    logged = []
    async with asyncio.timeout(10), backend._wire.session_events(session, 0) as events:
        async for _, event in events:
            logged.append(event)
            if event.get("type") == until:
                return logged
    return logged


async def test_a_choice_is_asked_in_the_turns_conversation_and_its_answer_is_the_models(build_world, model, stream):
    world = await build_world(ANSWER_EVERYTHING)
    text = call("user_prompt", message="Which colour?", title="Colour", schema=CHOICE)
    await world.platform.inject(world.message(text, conversation="C1"))
    (shown,) = await world.wait_question(conversation="C1")

    assert (shown.question.text, shown.question.options, shown.question.kind) == (
        "Colour: Which colour?",
        ("red", "blue"),
        "question",
    )
    await world.send(world.answer_to(shown, values=("blue",)))
    await world.host.idle()

    assert _result(model, text) == {"action": "accept", "content": {"value": "blue"}}
    assert (world.outcomes(), [post.text for post in world.posts()]) == ([OUTCOME_POSTED_FINAL], ["tool done"])
    types = [event.get("type") for event in stream]
    print(f"the stream of a turn with a question: {types}")
    request = next(event for event in stream if event.get("type") == "elicitation_request")
    assert (request["agent_name"], request["session_id"]) == ("root", _session(stream))
    assert types.index("elicitation_request") < types.index("tool_call_response")
    # Docker Agent writes the elicitation to the session's event log, and closes it there once answered.
    logged = await _logged(world.kit.backend, _session(stream), "elicitation_closed")
    print(f"the event log of a turn with a question: {logged}")
    assert [(event["type"], event["elicitation_id"]) for event in logged] == [
        ("elicitation_request", request["elicitation_id"]),
        ("elicitation_closed", request["elicitation_id"]),
    ]
    # The log holds no other event of the turn: its stream is the only source of the rest.
    assert not {event["type"] for event in logged} & {"stream_started", "tool_call", "token_usage", "stream_stopped"}


async def test_a_question_without_schema_takes_free_text(build_world, model):
    world = await build_world(ANSWER_EVERYTHING)
    text = call("user_prompt", message="Your name?")
    await world.platform.inject(world.message(text, conversation="C1"))
    (shown,) = await world.wait_question(conversation="C1")

    assert shown.question.options == ()
    await world.send(world.answer_to(shown, values=("Ada",)))
    await world.host.idle()

    assert _result(model, text) == {"action": "accept", "content": {"response": "Ada"}}


async def test_the_hosts_no_declines_and_the_turn_goes_on(build_world, model):
    world = await build_world(ANSWER_EVERYTHING)
    text = call("user_prompt", message="Which colour?", schema=CHOICE)
    await world.platform.inject(world.message(text, conversation="C1"))
    (shown,) = await world.wait_question(conversation="C1")

    await world.send(world.answer_to(shown, values=("reject",), approved=False))
    await world.host.idle()

    assert _result(model, text) == {"action": "decline"}
    assert [post.text for post in world.posts()] == ["tool done"]


async def test_a_schema_belle_cannot_ask_is_refused_to_the_model_with_what_it_expects(build_world, model):
    world = await build_world(ANSWER_EVERYTHING)
    text = call("user_prompt", message="How many?", schema={"type": "integer"})
    await world.platform.inject(world.message(text, conversation="C1"))
    await world.host.idle()

    refused = "\n".join(said(asked(model, text)[-1], "tool"))
    assert world.questions() == []
    assert "schema has type 'integer'" in refused and "enum of strings" in refused
    assert [post.text for post in world.posts()] == ["tool done"]


async def test_a_stopped_turn_takes_its_question_with_it_and_a_late_answer_runs_nothing(build_world, model, stream):
    world = await build_world(ANSWER_EVERYTHING)
    text = call("user_prompt", message="Which colour, before the stop?", schema=CHOICE)
    message = world.message(text, conversation="C1")
    await world.platform.inject(message)
    (shown,) = await world.wait_question(conversation="C1")

    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    await world.host.idle()
    with pytest.raises(QuestionClosed):
        await world.kit.backend.answer("C1", shown.question.id, world.answer_to(shown, values=("red",)).answer)

    assert await world.kit.backend.pending("C1") == []
    # The model was not asked again: the stop cancelled the call that waited, and Docker Agent closed its
    # elicitation in the event log.
    assert len(asked(model, message.text)) == 1
    logged = await _logged(world.kit.backend, _session(stream), "elicitation_closed")
    assert logged[-1]["type"] == "elicitation_closed" and logged[-1]["elicitation_id"] == shown.question.id
    # The conversation's session is free: the next turn runs.
    await world.send(world.message("SAY=after", conversation="C1"))
    assert world.posts()[-1].text == "after"


async def test_a_subagents_question_is_asked_in_the_turn_and_its_answer_reaches_the_subagent(
    build_world, model, monkeypatch, stream
):
    monkeypatch.setattr(model, "script", helper_reads_its_task)
    world = await build_world(ANSWER_EVERYTHING)
    task = call("user_prompt", message="Which colour?", schema=CHOICE)
    text = call("transfer_task", agent=SUBAGENT, task=task, expected_output="a colour")
    await world.platform.inject(world.message(text, conversation="C1"))
    (shown,) = await world.wait_question(conversation="C1")

    await world.send(world.answer_to(shown, values=("red",)))
    await world.host.idle()

    helper = [entry["body"] for entry in list(model.log) if task in "\n".join(said(entry["body"], "system"))]
    assert json.loads(said(helper[-1], "tool")[-1]) == {"action": "accept", "content": {"value": "red"}}
    assert [post.text for post in world.posts()] == ["tool done"]
    request = next(event for event in stream if event.get("type") == "elicitation_request")
    print(f"a subagent's elicitation: {request}")
    assert request["agent_name"] == SUBAGENT and request["session_id"] != _session(stream)
