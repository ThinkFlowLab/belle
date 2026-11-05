"""The relay plugin in a real OpenCode, answered by the adapter's own relay endpoint.

These run in the third OpenCode pass (OCB_PLUGIN=1), with the conformance
suite and the other adapter checks. What a turn was sent
is read from the stand-in's captured provider requests.
"""

import asyncio
import base64
import json

import pytest
from drive import Client
from standin import call, said

from belle_agent import hook_server
from belle_agent.backend import TurnRequest, TurnScope
from belle_agent.host import OUTCOME_FAILED, OUTCOME_POSTED_BY_TOOL, OUTCOME_POSTED_FINAL
from belle_agent.silence import post_tool_instruction

pytestmark = [pytest.mark.adapter, pytest.mark.plugin]

POST = "belle_post_message"

OPTIONAL_REPLY_IN_C1 = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], reply: optional, prompt: Answer in French.}
"""


def _session(model, text: str) -> str:
    """The OpenCode session of the latest turn whose message was ``text``."""
    return next(entry["session"] for entry in reversed(model.log) if said(entry["body"], "user")[-1:] == [text])


async def _turn(backend, text: str, request_id: str, *additions: str) -> list:
    request = TurnRequest(conversation="c1", request_id=request_id, text=text, scope=TurnScope(system_additions=additions))
    return [event async for event in backend.run_turn(request)]


async def test_the_relay_plugin_is_loaded_and_the_adapter_answers_it(world):
    sources = {plugin["id"]: plugin["source"]["type"] for plugin in Client().api("GET", "/api/plugin")}
    assert sources.get("belle.relay") not in (None, "builtin"), sources
    assert world.kit.backend._relay is not None


async def test_the_scopes_prompt_additions_reach_the_system_message_of_each_request(build_world, model):
    world = await build_world(OPTIONAL_REPLY_IN_C1)

    await world.send(world.message("one SAY=un", thread="root"))
    await world.send(world.message("two SAY=deux", thread="root"))
    await world.send(world.direct("three SAY=three"))

    scoped, direct = model.requests(_session(model, "two SAY=deux")), model.requests(_session(model, "three SAY=three"))
    instruction = post_tool_instruction(POST)
    assert len(scoped) == 2
    for request in scoped:
        system = "\n".join(said(request, "system"))
        assert "Answer in French." in system and instruction in system
    # Nothing of them is stored: the history holds the two messages and no update notice.
    assert said(scoped[-1], "user") == ["one SAY=un", "two SAY=deux"]
    # Control: a conversation whose scope adds nothing is told nothing.
    system = "\n".join(said(direct[-1], "system"))
    assert "Answer in French." not in system and instruction not in system


async def test_a_system_addition_is_in_its_own_turns_request_and_in_no_other(world, model):
    """What only the context hook can do: the instruction entry of the other mode stores a later addition as a
    ``user`` role update and keeps it in the history."""
    backend = world.kit.backend

    await _turn(backend, "one SAY=a", "r1")
    await _turn(backend, "two SAY=b", "r2", "ONLY_THIS_TURN")
    await _turn(backend, "three SAY=c", "r3")

    first, second, third = model.requests(_session(model, "three SAY=c"))
    assert "ONLY_THIS_TURN" in "\n".join(said(second, "system"))
    assert "ONLY_THIS_TURN" not in "\n".join(said(first, "system") + said(third, "system"))
    assert [said(request, "user") for request in (first, second, third)] == [
        ["one SAY=a"],
        ["one SAY=a", "two SAY=b"],
        ["one SAY=a", "two SAY=b", "three SAY=c"],
    ]
    # No instruction entry was written for the session.
    session = _session(model, "three SAY=c")
    assert not Client().api("GET", f"/api/experimental/session/{session}/instructions/entries")


async def test_files_of_a_thread_over_the_relay_endpoints_limit_reach_the_model(world, model, monkeypatch):
    """A file's contents do not cross the relay, so the endpoint's limit does not bound the files of a thread.

    OpenCode repeats every file of the session in each context event: the
    third turn's carries both files, together over the limit. Each file's own
    prompt event, as base64, stays under it. The limit is lowered so that the
    files stay small for the stand-in model.
    """
    monkeypatch.setattr(hook_server, "MAX_REQUEST_BYTES", 96 * 1024)
    notes = ["x" * 56 * 1024, "y" * 56 * 1024]
    for number, note in enumerate(notes):
        uri = "data:text/plain;base64," + base64.b64encode(note.encode()).decode()
        await world.send(world.direct(f"read SAY=read{number}", files=({"name": f"{number}.txt", "uri": uri},)))
    await world.send(world.direct("again SAY=again"))

    assert world.outcomes() == [OUTCOME_POSTED_FINAL] * 3
    assert world.kit.backend._relay.refused == {}
    request = json.dumps(model.requests(_session(model, "again SAY=again"))[-1])
    assert all(note in request for note in notes)


async def test_with_the_relay_endpoint_stopped_a_turn_fails_closed(world, model):
    relay = world.kit.backend._relay
    await world.send(world.direct("before SAY=answered"))
    asked = len(model.log)

    await relay.close()
    await world.send(world.direct("during " + call(POST, text="must not be posted")))

    # The prompt hook cannot be answered: no provider request, no post but the host's failure notice.
    assert len(model.log) == asked
    assert world.outcomes() == [OUTCOME_POSTED_FINAL, OUTCOME_FAILED]
    assert "must not be posted" not in "".join(post.text for post in world.posts())

    # Control: the endpoint is back and the conversation runs again, through the tool.
    await relay.start()
    await world.send(world.direct("after " + call(POST, text="posted again")))
    assert world.outcomes()[-1] == OUTCOME_POSTED_BY_TOOL
    assert world.posts()[-1].text == "posted again"


async def test_a_relay_endpoint_that_stops_during_a_turn_lets_no_tool_call_through(world, model):
    relay = world.kit.backend._relay
    text = "WAIT=3 " + call(POST, text="must not be posted")

    sent = asyncio.create_task(world.send(world.direct(text)))
    async with asyncio.timeout(30):
        # The prompt and context hooks were answered: the model's request is out, and held.
        while not any(said(entry["body"], "user")[-1:] == [text] for entry in model.log):
            await asyncio.sleep(0.05)
    await relay.close()
    await sent

    # The tool call met a hook nobody answers, and so did the request that would have followed it.
    assert world.outcomes() == [OUTCOME_FAILED]
    assert "must not be posted" not in "".join(post.text for post in world.posts())
    assert len(model.requests(_session(model, text))) == 1

    # Control: the same call with the endpoint back.
    await relay.start()
    await world.send(world.direct("again " + call(POST, text="posted")))
    assert world.outcomes()[-1] == OUTCOME_POSTED_BY_TOOL
