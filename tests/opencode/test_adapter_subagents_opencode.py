"""A turn whose subagents run in the background, on OpenCode itself with the stand-in model.

The model starts each child with ``background``, so the call returns at once
and the first execution of the session ends while the child runs. The child's
answer is ``SAY=<word>``, which is how its report makes the stand-in answer
``<word>`` when OpenCode runs the session again for it.
"""

import asyncio
import os

import pytest
from conformance_kit import LoopbackKit, worlds
from drive import Client
from slack_kit import SlackKit
from standin import call, said, tools

from belle_agent.backend import EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, SUBAGENT
from belle_agent.host import OUTCOME_INTERRUPTED, OUTCOME_POSTED_FINAL
from belle_agent.platforms.base import MARK_STOPPED

pytestmark = pytest.mark.adapter

DENY_SHELL = "scopes:\n  - match: {channel: loopback}\n    permissions: {tools: {shell: deny}}\n"


def background(word: str, seconds: float) -> str:
    """The directive that starts a child in the background which answers ``SAY=<word>`` after ``seconds``."""
    prompt = f"WAIT={seconds} SAY=SAY={word}"
    return call("subagent", agent="general", description=word, prompt=prompt, background=True)


@pytest.fixture(params=["loopback", "slack"])
async def world(request, kit, tmp_path, monkeypatch):
    """A host that shows all its platform can of a running turn, with the adapter behind it."""
    platform = LoopbackKit() if request.param == "loopback" else SlackKit(monkeypatch)
    async with worlds(kit, tmp_path, platform) as build:
        yield await build(platform.shows)


def subagents(world) -> list[tuple[str, str | None]]:
    """Each event of a subagent's call the platform was handed, in order: its kind and the subagent's type."""
    return [
        (event.kind, event.data.get(SUBAGENT))
        for _, event in world.platform.progressed
        if event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT)
    ]


def cards(world) -> list[str]:
    """The text of each activity card on screen. A platform with no card has none."""
    return [action.text for action in world.platform.log if action.kind == "activity"]


async def first_answer(world) -> None:
    """Wait until the turn has written the answer of its first execution."""
    async with asyncio.timeout(30):
        while "tool done" not in "".join(e.text for _, e in world.platform.progressed if e.kind == EVENT_TEXT_DELTA):
            await asyncio.sleep(0.05)
    # Time for an end of the turn to be delivered, were there one.
    await asyncio.sleep(1)


async def test_a_turn_with_one_background_subagent_ends_after_its_report_and_posts_both_answers_once(world):
    message = world.direct("go " + background("three", 3))
    await world.platform.inject(message)
    await first_answer(world)

    # The first execution has ended. The child runs, so the turn does, and its call has no result yet.
    assert world.outcomes() == [] and world.platform.concluded == []
    assert subagents(world) == [(EVENT_TOOL_CALLED, "general")]
    assert all(text.startswith("Working...") and "1 of 1 subagent running" in text for text in cards(world))

    await world.host.idle()

    assert [post.text for post in world.posts()] == ["tool done\n\nthree"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    # The call that started the child gets its result when the child reports.
    assert subagents(world) == [(EVENT_TOOL_CALLED, "general"), (EVENT_TOOL_RESULT, "general")]
    assert all(text.startswith("Turn finished") and "1 subagent finished" in text for text in cards(world))


async def test_two_background_subagents_of_different_durations_are_one_turn_and_a_message_meanwhile_waits(world):
    message = world.direct(f"go {background('short', 2)} {background('long', 5)}")
    await world.platform.inject(message)
    await first_answer(world)

    # A message that arrives while the children run waits, as behind any running turn.
    assert (await world.platform.inject(world.direct("SAY=afterwards"))).startswith("queued")
    assert world.outcomes() == [] and world.platform.concluded == []
    assert all(text.startswith("Working...") and "running" in text for text in cards(world))

    await world.host.idle()

    # Each answer once, in order: the first execution's, then one for each child as it reported.
    assert [post.text for post in world.posts()] == ["tool done\n\nshort\n\nlong", "afterwards"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL, OUTCOME_POSTED_FINAL]
    assert subagents(world) == [(EVENT_TOOL_CALLED, "general")] * 2 + [(EVENT_TOOL_RESULT, "general")] * 2
    assert all(text.startswith("Turn finished") and "2 subagents finished" in text for text in cards(world))


async def test_a_stop_while_a_background_subagent_runs_stops_the_child_and_what_its_end_would_start(world, model):
    message = world.direct("go " + background("never", 60))
    await world.platform.inject(message)
    await first_answer(world)

    # The session is idle and its child runs: an interrupt of the session alone would stop nothing.
    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    await world.host.idle()

    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert world.platform.concluded == [(message.message_id, MARK_STOPPED, "")]
    # Nothing of the turn runs on in OpenCode: not the child, and not the execution its end starts in the parent.
    await asyncio.sleep(2)
    client = Client()
    assert client.api("GET", "/api/session/active") == {}
    # The stand-in keeps the requests of every test. The session of this turn is the last that was sent its text.
    session = [entry["session"] for entry in model.log if any(message.text in user for user in said(entry["body"], "user"))][-1]
    stored = client.messages(session)
    report = next(index for index, entry in enumerate(stored) if entry["type"] == "synthetic")
    # The child reported that it was stopped, OpenCode ran the session for that report, and that execution was stopped too.
    assert stored[report]["metadata"]["state"] == "cancelled"
    assert [entry.get("outcome") for entry in stored[report + 1 :] if entry["type"] == "idle"] == ["interrupted"]

    # Control: the conversation's next turn is answered.
    await world.send(world.direct("SAY=the-next-turn"))
    assert [post.text for post in world.posts()][-1] == "the-next-turn"


@pytest.mark.parametrize("form", ["blocking", "background"])
@pytest.mark.parametrize(("rules", "allowed"), [pytest.param(DENY_SHELL, False, id="denied"), pytest.param("", True, id="no-rule")])
async def test_a_subagent_is_held_to_the_tool_levels_of_the_session_that_started_it(build_world, model, form, rules, allowed):
    """The measured answer: a child session inherits its parent's permission ruleset, in both forms of the call.

    The agents a child runs as, ``general`` and ``explore``, allow every tool in
    their own definition. The session's rules still apply: a tool the scope
    denies is not offered to the child and is refused when the child calls it.
    The run with no rule is the control: there the child is offered the shell and runs it.
    """
    world = await build_world(rules)
    child = f"child-{os.urandom(3).hex()} " + call("shell", command="echo ran-in-the-child", description="d")
    arguments = {"agent": "general", "description": "d", "prompt": child, **({"background": True} if form == "background" else {})}

    await world.send(world.direct("go " + call("subagent", **arguments)))

    requests = [entry["body"] for entry in model.log if any(child in user for user in said(entry["body"], "user"))]
    answered = "".join(said(requests[-1], "tool"))
    assert ("shell" in tools(requests[0])) is allowed
    assert ("ran-in-the-child" in answered) is allowed
    assert ("currently available" in answered) is not allowed
