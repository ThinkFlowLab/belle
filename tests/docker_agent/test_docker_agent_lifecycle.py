"""The restart checks, one stage at a time: OCB_STAGE names the stage, DAB_RUN the run.

A stage is one Belle process. Between two stages, or in the middle of one that
raises a flag for it in the state directory, the runner (a person or a tool)
restarts the Docker Agent container or kills the Belle one. Every stage uses
the same namespace and the same state directories, as one deployment does
across its restarts.
"""

import asyncio
import itertools
import os
import pathlib
from dataclasses import replace

import httpx
import pytest
import pytest_asyncio
from conformance_kit import worlds
from docker_agent_kit import ARTEFACTS, TOOLS_PORT, DockerAgentKit
from standin import call, said

from belle_agent.host import (
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_POSTED_BY_TOOL,
    OUTCOME_POSTED_FINAL,
)
from belle_agent.integrations.docker_agent import adapter

pytestmark = pytest.mark.docker_agent_server

RUN = os.environ.get("DAB_RUN", "")
NAMESPACE = f"lifecycle-{RUN}"
DIRECTORY = pathlib.Path(os.path.dirname(ARTEFACTS)) / f"lifecycle-{RUN}"
OLD_TOKEN = DIRECTORY / "tool-token-of-the-belle-that-died"
POST = "belle_post_message"
#: The turn that is running when Belle is killed. It would take two minutes.
ORPHAN = "SLOW TICKS=240"


def _flag(name: str) -> None:
    """Ask the runner for its next step."""
    (DIRECTORY / name).touch()


def _asked_last(model, line: str) -> dict:
    """The last provider request whose last user message ends with ``line``."""
    return [entry["body"] for entry in list(model.log) if (said(entry["body"], "user") or [""])[-1].endswith(line)][-1]


async def _asked(model, text: str) -> None:
    async with asyncio.timeout(30):
        while not any(said(entry["body"], "user")[-1:] == [text] for entry in list(model.log)):
            await asyncio.sleep(0.05)


async def _restarted(backend) -> None:
    """Wait until the runner has started Docker Agent again, and it answers."""
    async with asyncio.timeout(90):
        while not (DIRECTORY / "agent-restarted").exists() or not await backend._ping():
            await asyncio.sleep(0.5)


@pytest_asyncio.fixture
async def start(model):
    """Return a function that starts this deployment's host. Message ids go on from ``first``."""
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    async with worlds(DockerAgentKit(namespace=NAMESPACE), DIRECTORY) as build:

        async def started(first: int):
            world = await build()
            world._ids = itertools.count(first)
            return world

        yield started


@pytest.mark.stage("agent-restarts")
async def test_docker_agent_restarts_under_a_running_belle(start, model):
    world = await start(100)
    await world.send(world.direct("one SAY=FIRST", sender="U1"))
    await world.send(world.direct("go " + call(POST, text="before the restart"), sender="U3"))
    slow = "SLOW TICKS=40"
    in_flight = asyncio.create_task(world.send(world.direct(slow, sender="U2")))
    await _asked(model, slow)
    await asyncio.sleep(1)

    _flag("restart-agent")
    async with asyncio.timeout(90):
        await in_flight
    await _restarted(world.kit.backend)

    # The turn in flight ended once, as a failure its reader was told of.
    assert world.outcomes() == [OUTCOME_POSTED_FINAL, OUTCOME_POSTED_BY_TOOL, OUTCOME_FAILED]
    lost = [post.text for post in world.posts("D-U2")]
    assert len(lost) == 1 and adapter.STREAM_LOST in lost[0], lost

    # The sessions survived: the next turn of a conversation holds its earlier one.
    await world.send(world.direct("two SAY=SECOND", sender="U1"))
    assert said(_asked_last(model, "two SAY=SECOND"), "user") == ["one SAY=FIRST", "two SAY=SECOND"]
    assert said(_asked_last(model, "two SAY=SECOND"), "assistant") == ["FIRST"]
    # Belle's tools are reached again.
    await world.send(world.direct("go " + call(POST, text="after the restart"), sender="U3"))
    assert [post.text for post in world.posts("D-U3")] == ["before the restart", "after the restart"]
    # The conversation of the lost turn takes its next one, and nothing of the lost one arrives late.
    await world.send(world.direct("SAY=again", sender="U2"))
    assert [post.text for post in world.posts("D-U2")] == [*lost, "again"]
    # Docker Agent, stopped on SIGTERM, kept the lost turn's message and the text written so far, which no one was shown.
    again = _asked_last(model, "SAY=again")
    assert said(again, "user")[-2:] == [slow, "SAY=again"]
    assert said(again, "assistant")[-1].startswith("t0 ")


@pytest.mark.stage("belle-dies")
async def test_belle_is_killed_with_a_turn_running(start, model):
    world = await start(200)
    await world.send(world.direct("three SAY=THIRD", sender="U1"))
    assert [post.text for post in world.posts("D-U1")] == ["THIRD"]
    OLD_TOKEN.write_text(world.kit.backend._tool_server.token, encoding="utf-8")
    await world.platform.inject(world.direct(ORPHAN, sender="U2"))
    await _asked(model, ORPHAN)
    await asyncio.sleep(1)

    _flag("kill-belle")
    await asyncio.sleep(3600)  # the runner kills this process here


async def _tools_list(token: str) -> int:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with httpx.AsyncClient() as client:
        url = f"http://127.0.0.1:{TOOLS_PORT}/mcp"
        return (await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})).status_code


@pytest.mark.stage("belle-restarted")
async def test_a_new_belle_takes_over(start, model):
    world = await start(300)

    # The turn of the Belle that died ended with its stream: nothing runs in this deployment's sessions.
    assert await world.kit.backend.active() == []

    # The event id window of the host was read back: the message the dead Belle answered is not answered again.
    third = replace(world.direct("three SAY=THIRD", sender="U1"), message_id="m200", event_id="e200")
    assert await world.send(third) == "ignored:already-handled (dedupe)"
    # Past that window, the adapter's own record of the request ids that ran still holds it.
    await world.send(replace(third, message_id="seen-as-new"))
    assert world.outcomes() == [OUTCOME_DUPLICATE]
    assert world.posts() == []

    # The conversation goes on in its session, found in the adapter's state.
    await world.send(world.direct("four SAY=FOURTH", sender="U1"))
    assert said(_asked_last(model, "four SAY=FOURTH"), "user") == [
        "one SAY=FIRST",
        "two SAY=SECOND",
        "three SAY=THIRD",
        "four SAY=FOURTH",
    ]
    assert [post.text for post in world.posts("D-U1")] == ["FOURTH"]

    # The tools are served with this process's token. The dead one's is refused.
    assert await _tools_list(OLD_TOKEN.read_text(encoding="utf-8")) == 401
    assert await _tools_list(world.kit.backend._tool_server.token) == 200
    await world.send(world.direct("go " + call(POST, text="from the new belle"), sender="U3"))
    assert [post.text for post in world.posts("D-U3")] == ["from the new belle"]
    assert world.outcomes()[-1] == OUTCOME_POSTED_BY_TOOL

    # The conversation of the stopped turn takes its next one, and the stopped one posts nothing.
    await world.send(world.direct("SAY=back", sender="U2"))
    assert [post.text for post in world.posts("D-U2")] == ["back"]
    # Belle answered the stopped turn's message in the session, so the model reads the new message apart from it.
    back = _asked_last(model, "SAY=back")
    assert said(back, "user")[-2:] == [ORPHAN, "SAY=back"]
    assert said(back, "assistant")[-1] == adapter.INTERRUPTED
