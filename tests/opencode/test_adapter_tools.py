"""Belle's tools served to OpenCode over MCP, with no plugin loaded and the stand-in model."""

import asyncio
import secrets

import pytest
from drive import Client, tool_result
from standin import call, said

from belle_agent.host import OUTCOME_POSTED_BY_TOOL

pytestmark = pytest.mark.adapter

POST = "belle_post_message"


def _session(model, text: str) -> str:
    """The OpenCode session of the latest turn whose message was ``text``."""
    return next(entry["session"] for entry in reversed(model.log) if said(entry["body"], "user")[-1:] == [text])


def _tool_results(model, session: str) -> list[dict]:
    """What the model read back from the tools, in the session's latest provider request."""
    return [tool_result(result) for result in said(model.requests(session)[-1], "tool")]


def _posted(model, session: str) -> list[tuple]:
    """The success, conversation and first message ID of each tool result in the session's latest request."""
    return [(result["ok"], result["chat_id"], result["message_id"]) for result in _tool_results(model, session)]


async def _on_the_wire(model, session: str, text: str) -> None:
    """Prompt a session directly, as something that is not Belle would, and wait for its tool call to be answered."""
    client = Client()
    await asyncio.to_thread(client.api, "POST", "/api/session", {"id": session})
    assert (await asyncio.to_thread(client.prompt, session, text))[0] == 200
    async with asyncio.timeout(30):
        # The request that follows a tool call holds the tool's answer.
        while not any(said(request, "user")[-1] == text and said(request, "tool") for request in model.requests(session)):
            await asyncio.sleep(0.05)


async def test_the_model_posts_through_the_posting_tool(world, model):
    text = "go " + call(POST, text="said through the tool")

    await world.send(world.direct(text))

    assert [(post.conversation, post.text) for post in world.posts()] == [("D-U1", "said through the tool")]
    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]
    # The roster names the tool as the adapter said it would, and the next request holds the result.
    session = _session(model, text)
    names = [tool["function"]["name"] for tool in model.requests(session)[0]["tools"]]
    assert {POST, "belle_react_to_message"} <= set(names)
    assert _posted(model, session) == [(True, "D-U1", "out-1")]


async def test_two_overlapping_turns_each_post_in_their_own_conversation(world, model):
    first = "WAIT=1 " + call(POST, text="from the first")
    second = "WAIT=1 " + call(POST, text="from the second")

    await asyncio.gather(world.send(world.direct(first, sender="U1")), world.send(world.direct(second, sender="U2")))

    assert sorted((post.conversation, post.text) for post in world.posts()) == [
        ("D-U1", "from the first"),
        ("D-U2", "from the second"),
    ]
    # Both turns were running when either called the tool.
    opened = [min(e["at"] for e in model.log if e["session"] == _session(model, text)) for text in (first, second)]
    called = [max(e["at"] for e in model.log if e["session"] == _session(model, text)) for text in (first, second)]
    assert max(opened) < min(called)


async def test_a_call_from_a_session_that_runs_no_turn_of_belles_makes_no_post(world, model):
    await world.send(world.direct("SAY=first"))
    idle = _session(model, "SAY=first")
    foreign = f"ses_foreign_{secrets.token_hex(4)}"

    # A session OpenCode runs for someone else, and Belle's own session of a conversation whose turn
    # has ended, each prompted behind Belle's back. The model in them calls the posting tool.
    for session in (foreign, idle):
        text = f"go {secrets.token_hex(4)} " + call(POST, text="not mine to post")
        await _on_the_wire(model, session, text)
        assert [result["error"] for result in _tool_results(model, session)] == ["tool_denied"]
    assert [post.text for post in world.posts()] == ["first"]

    # Control: the same call in a turn Belle started posts.
    await world.send(world.direct("go " + call(POST, text="mine to post")))
    assert [post.text for post in world.posts()] == ["first", "mine to post"]


async def test_a_subagents_call_lands_in_its_parents_conversation(world, model):
    child = "child task " + call(POST, text="from the child")

    parent = "go " + call("subagent", agent="general", description="a child", prompt=child)

    await world.send(world.direct(parent))

    assert [(post.conversation, post.text) for post in world.posts()] == [("D-U1", "from the child")]
    # The call came from the child's own session, which no turn of Belle's runs in.
    callers = {e["session"] for e in model.log if said(e["body"], "tool") and child in said(e["body"], "user")[-1]}
    assert len(callers) == 1 and callers != {_session(model, parent)}
    assert _posted(model, callers.pop()) == [(True, "D-U1", "out-1")]


@pytest.mark.provider
async def test_a_real_model_told_to_answer_through_the_posting_tool_calls_it(world):
    token = "SMOKE_" + secrets.token_hex(4)

    async with asyncio.timeout(240):
        await world.send(
            world.direct(
                f"Answer by calling the tool {POST} once, with the text {token}."
                " Nobody reads anything you write outside that tool call."
            )
        )

    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]
    assert [post.conversation for post in world.posts()] == ["D-U1"]
    assert token in world.posts()[0].text
