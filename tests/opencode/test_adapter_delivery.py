"""What reaches the model and what OpenCode keeps, for delivery and silence. No plugin is loaded."""

import asyncio

import pytest
from drive import Client
from standin import call, said

from belle_agent.backend import TurnRequest, TurnScope
from belle_agent.host import OUTCOME_POSTED_BY_TOOL, OUTCOME_WITHHELD
from belle_agent.silence import post_tool_instruction

pytestmark = pytest.mark.adapter

POST = "belle_post_message"

OPTIONAL_REPLY_IN_C1 = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], reply: optional, prompt: Answer in French.}
"""


def _session(model, text: str) -> str:
    """The OpenCode session of the latest turn whose message was ``text``."""
    return next(entry["session"] for entry in reversed(model.log) if said(entry["body"], "user")[-1:] == [text])


async def _turn(backend, text: str, request_id: str, *additions: str) -> None:
    request = TurnRequest(
        conversation="c1", request_id=request_id, text=text, scope=TurnScope(system_additions=additions)
    )
    async for _ in backend.run_turn(request):
        pass


@pytest.mark.no_plugin
async def test_the_scopes_prompt_additions_reach_the_model_with_no_plugin(build_world, model):
    world = await build_world(OPTIONAL_REPLY_IN_C1)

    await world.send(world.message("one SAY=un", thread="root"))
    await world.send(world.message("two SAY=deux", thread="root"))
    await world.send(world.direct("three SAY=three"))

    scoped, direct = model.requests(_session(model, "two SAY=deux")), model.requests(_session(model, "three SAY=three"))
    instruction = post_tool_instruction(POST)
    # An entry set before the session's first turn sits in the system message of each of its requests.
    for request in scoped:
        (system,) = said(request, "system")
        assert "Answer in French." in system and instruction in system
    # The same additions on the next turn add nothing to the history.
    assert said(scoped[-1], "user") == ["one SAY=un", "two SAY=deux"]
    # Control: a conversation whose scope adds nothing is told nothing.
    (system,) = said(direct[-1], "system")
    assert "Answer in French." not in system and instruction not in system
    assert said(direct[-1], "user") == ["three SAY=three"]


@pytest.mark.no_plugin
async def test_changed_and_removed_additions_are_told_to_the_model_and_stay_in_the_history(world, model):
    backend = world.kit.backend

    await _turn(backend, "one SAY=a", "r1")
    await _turn(backend, "two SAY=b", "r2", "FIRST_ADDITION")
    await _turn(backend, "three SAY=c", "r3", "SECOND_ADDITION")
    await _turn(backend, "four SAY=d", "r4")

    users = [said(request, "user") for request in model.requests(_session(model, "four SAY=d"))]
    # A turn with no additions in a session that never had any is told nothing.
    assert users[0] == ["one SAY=a"]
    # An addition that arrives later is a stored `user` role update, not system text.
    assert "FIRST_ADDITION" in users[1][1] and users[1][1].startswith("<system-update>")
    assert "SECOND_ADDITION" in users[2][3] and "supersedes" in users[2][3]
    # The cost: the removed text stays in the history, followed by OpenCode's notice that it no longer applies.
    assert [text for text in users[3] if "ADDITION" in text] == [users[1][1], users[2][3]]
    assert "no longer applies" in users[3][5]
    assert users[3][-1] == "four SAY=d"


async def test_an_unposted_final_is_stored_and_sent_to_the_model_again(build_world, model):
    """A record of an effect of tool-only silence on this route. It is not a requirement."""
    world = await build_world(OPTIONAL_REPLY_IN_C1)

    await world.send(world.message("one SAY=UNSENT_THOUGHT", thread="root"))

    assert world.platform.log == []
    assert world.outcomes() == [OUTCOME_WITHHELD]
    session = _session(model, "one SAY=UNSENT_THOUGHT")
    stored = await asyncio.to_thread(Client().messages, session)
    assert [part["text"] for message in stored if message["type"] == "assistant" for part in message["content"]] == [
        "UNSENT_THOUGHT"
    ]

    # Control: the next turn of the session posts through the tool. Its request holds the final nobody read.
    text = "two " + call(POST, text="worth saying")
    await world.send(world.message(text, thread="root"))

    assert [post.text for post in world.posts("C1")] == ["worth saying"]
    assert world.outcomes() == [OUTCOME_WITHHELD, OUTCOME_POSTED_BY_TOOL]
    assert said(model.requests(session)[-1], "assistant")[0] == "UNSENT_THOUGHT"
