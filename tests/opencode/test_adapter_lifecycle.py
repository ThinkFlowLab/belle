"""Stop, duplicates, bounds and a lost event stream, against the OpenCode container with no plugin loaded."""

import asyncio
import os
import secrets
from dataclasses import replace

import pytest
import pytest_asyncio
from conformance_kit import worlds
from drive import Client
from opencode_kit import OpenCodeKit
from standin import call, said
from tcp_proxy import Proxy

from belle_agent import host
from belle_agent.host import (
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_POSTED_BY_TOOL,
    OUTCOME_POSTED_FINAL,
)

pytestmark = pytest.mark.adapter

POST = "belle_post_message"


def _session(model, text: str) -> str:
    """The OpenCode session of the latest turn whose message begins with ``text``.

    The host may add lines after the message, such as the note on a stopped turn.
    """
    return next(entry["session"] for entry in reversed(model.log) if (said(entry["body"], "user") or [""])[-1].startswith(text))


async def _asked(model, text: str, requests: int = 1) -> str:
    """Wait until the model has been asked ``requests`` times in the turn of ``text``. Return its session."""
    async with asyncio.timeout(30):
        while True:
            asked = [entry for entry in model.log if said(entry["body"], "user")[-1:] == [text]]
            if len(asked) >= requests:
                return asked[0]["session"]
            await asyncio.sleep(0.05)


def _shell(command: str) -> str:
    return call("shell", command=command, description="a lifecycle check")


async def test_a_stop_during_a_slow_tool_leaves_no_marker(world, model):
    mark = secrets.token_hex(4)
    slow = "go " + _shell(f"sleep 4; touch STOPPED_{mark}")
    message = world.direct(slow)
    await world.platform.inject(message)
    await _asked(model, slow)
    await asyncio.sleep(1)  # the tool is running

    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    await world.host.idle()

    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert world.platform.log == []
    await asyncio.sleep(5)  # past the moment the stopped command would have written its marker

    # Control: a turn nobody stops writes its marker, and lists the directory both would be in.
    listing = "go " + _shell(f"touch CONTROL_{mark}; ls")
    await world.send(world.direct(listing))

    assert world.outcomes() == [OUTCOME_INTERRUPTED, OUTCOME_POSTED_FINAL]
    # The request after the listing holds two tool results: the stopped call's, then the listing.
    request = model.requests(_session(model, listing))[-1]
    listed = said(request, "tool")[-1]
    assert f"CONTROL_{mark}" in listed
    assert f"STOPPED_{mark}" not in listed
    # The turn after the stop is told which message the person stopped.
    assert message.message_id in said(request, "user")[-1]


async def test_a_request_that_comes_twice_makes_one_provider_request(world, model):
    first = world.direct("once SAY=ONCE")
    await world.send(first)

    async with asyncio.timeout(10):
        await world.send(replace(first, message_id="seen-as-new"))

    session = _session(model, "once SAY=ONCE")
    assert len(model.requests(session)) == 1
    assert [post.text for post in world.posts()] == ["ONCE"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL, OUTCOME_DUPLICATE]

    # Control: a new request id with the same text makes a second.
    await world.send(world.direct("once SAY=ONCE"))
    assert len(model.requests(session)) == 2
    assert [post.text for post in world.posts()] == ["ONCE", "ONCE"]


async def test_a_provider_that_fails_every_request_is_stopped_at_the_step_bound(world, model, monkeypatch):
    monkeypatch.setattr(host, "MAX_TURN_STEPS", 2)

    async with asyncio.timeout(60):
        await world.send(world.direct("FAIL=500"))

    # OpenCode retries a 5xx without end and reports no failure. Each retry is a step of the turn.
    assert [post.text for post in world.posts()] == [host.TURN_STEPS_NOTICE]
    assert world.outcomes() == [OUTCOME_FAILED]
    session = _session(model, "FAIL=500")
    asked = len(model.requests(session))
    assert 2 <= asked <= 3

    # Control: the conversation is free, and nothing retries behind its next turn.
    await world.send(world.direct("SAY=fine"))
    assert [post.text for post in world.posts()] == [host.TURN_STEPS_NOTICE, "fine"]
    assert sum(said(request, "user")[-1] == "FAIL=500" for request in model.requests(session)) == asked


@pytest_asyncio.fixture
async def cut_build(model, tmp_path):
    """A builder for a host whose adapter reaches OpenCode through a relay the check can cut, and that relay."""
    proxy = await Proxy(os.environ["OCB_OPENCODE_URL"]).start()
    kit = OpenCodeKit(url=proxy.url, namespace=secrets.token_hex(8))
    async with worlds(kit, tmp_path) as build:
        yield build, proxy
    await proxy.close()


@pytest_asyncio.fixture
async def cut(cut_build):
    build, proxy = cut_build
    return await build(), proxy


SHELL_ASKS = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
    permissions: {tools: {shell: ask}}
"""


async def test_a_turn_that_outlasts_a_lost_event_stream_is_resumed_and_posted_once(cut, model):
    world, proxy = cut
    slow = "SLOW TICKS=10"
    sending = asyncio.create_task(world.send(world.direct(slow)))
    await _asked(model, slow)
    await asyncio.sleep(1)

    proxy.drop()
    await sending

    # The deltas sent meanwhile are gone. The final is read from the session, whole.
    assert [post.text for post in world.posts()] == [" ".join(f"t{number}" for number in range(10))]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    # Nothing stopped the turn: OpenCode ran it to its end.
    stored = await asyncio.to_thread(Client().api, "GET", f"/api/session/{_session(model, slow)}")
    assert stored["outcome"] == "succeeded"


async def test_a_turn_that_ends_while_no_event_can_be_read_is_posted_once(cut, model):
    world, proxy = cut
    quick = "SLOW TICKS=4"
    sending = asyncio.create_task(world.send(world.direct(quick)))
    await _asked(model, quick)

    # The turn takes two seconds. Nothing reaches OpenCode for four.
    proxy.drop(4)
    await sending

    assert [post.text for post in world.posts()] == ["t0 t1 t2 t3"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]

    # Control: the conversation's next turn is its own, and holds the first.
    await world.send(world.direct("SAY=next"))
    assert [post.text for post in world.posts()] == ["t0 t1 t2 t3", "next"]
    assert said(model.requests(_session(model, "SAY=next"))[-1], "assistant") == ["t0 t1 t2 t3 "]


async def test_a_post_made_through_the_tool_is_not_repeated_after_a_lost_event_stream(cut, model):
    world, proxy = cut
    text = "WAIT=3 " + call(POST, text="said through the tool")
    sending = asyncio.create_task(world.send(world.direct(text)))
    await asyncio.sleep(1)

    proxy.drop(4)
    await sending

    # The tool call and the end of the turn both fell in the gap. The turn is found ended, and its final stays unposted.
    assert [post.text for post in world.posts()] == ["said through the tool"]
    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]


async def test_a_waiting_approval_is_found_again_after_a_lost_event_stream(cut_build, model):
    build, proxy = cut_build
    world = await build(SHELL_ASKS)
    conversation = "loopback_W1_D-U1_U1"
    text = "go " + _shell("echo approved")
    await world.platform.inject(world.direct(text))

    # The scope makes shell ask; OpenCode's permission engine raises it, and the host shows it once.
    (shown,) = await world.wait_question(conversation="D-U1")
    assert shown.question.tool == "shell" and "echo approved" in shown.question.text
    before = len(world.questions("D-U1"))

    proxy.drop(2)
    await asyncio.sleep(6)

    # The ask event is not replayed. The adapter re-reads it and the host re-relays it, but not twice.
    assert len(world.questions("D-U1")) == before
    assert world.outcomes() == []
    assert await world.kit.backend.active() == [conversation]

    # A reject releases the call: the turn ends (a reject interrupts in OpenCode; a turn whose
    # stream was cut may instead be reconciled as failed) and no question is left waiting.
    await world.send(world.answer_to(shown, approved=False))
    assert world.outcomes() and world.outcomes()[-1] in (OUTCOME_INTERRUPTED, OUTCOME_FAILED)
    assert await world.kit.backend.pending(conversation) == []
