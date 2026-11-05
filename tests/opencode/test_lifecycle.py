"""The restart checks, one stage at a time: OCB_STAGE names the stage, OCB_RUN the run.

A stage is one Belle process. Between two stages, or in the middle of one that
raises a flag for it in the state directory, the runner (a person or a tool)
restarts the OpenCode container or kills the Belle one. Every stage uses the
same namespace and the same state directory, as one deployment does across its
restarts.
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
from drive import Client
from opencode_kit import ARTEFACTS, TOOLS_PORT, OpenCodeKit
from standin import call, said

from belle_agent.host import (
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_POSTED_BY_TOOL,
    OUTCOME_POSTED_FINAL,
)
from belle_agent.integrations.opencode import adapter
from belle_agent.integrations.opencode.sessions import session_id

pytestmark = pytest.mark.adapter

RUN = os.environ.get("OCB_RUN", "")
NAMESPACE = f"lifecycle-{RUN}"
DIRECTORY = pathlib.Path(os.path.dirname(ARTEFACTS)) / f"lifecycle-{RUN}"
OLD_TOKEN = DIRECTORY / "tool-token-of-the-belle-that-died"
POST = "belle_post_message"
#: The turn that is running when Belle is killed. It would take two minutes. The stand-in model dies with
#: Belle, so what OpenCode still runs afterwards is this turn's retries of a provider that is gone. For the
#: same reason no turn can be made to end while no Belle runs.
ORPHAN = "SLOW TICKS=240"


def _conversation(sender: str) -> str:
    return f"loopback_W1_D-{sender}_{sender}"


def _flag(name: str) -> None:
    """Ask the runner for its next step."""
    (DIRECTORY / name).touch()


async def _asked(model, text: str) -> None:
    async with asyncio.timeout(30):
        while not any(said(entry["body"], "user")[-1:] == [text] for entry in model.log):
            await asyncio.sleep(0.05)


def _requests(model, sender: str) -> list[dict]:
    return model.requests(session_id(NAMESPACE, _conversation(sender)))


@pytest_asyncio.fixture
async def start(model):
    """Return a function that starts this deployment's host. Message ids go on from ``first``."""
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    async with worlds(OpenCodeKit(namespace=NAMESPACE), DIRECTORY) as build:

        async def started(first: int):
            world = await build()
            world._ids = itertools.count(first)
            return world

        yield started


@pytest.mark.stage("opencode-restarts")
async def test_opencode_restarts_under_a_running_belle(start, model):
    world = await start(100)
    await world.send(world.direct("one SAY=FIRST", sender="U1"))
    await world.send(world.direct("go " + call(POST, text="before the restart"), sender="U3"))
    slow = "SLOW TICKS=40"
    in_flight = asyncio.create_task(world.send(world.direct(slow, sender="U2")))
    await _asked(model, slow)
    await asyncio.sleep(1)

    _flag("restart-opencode")
    async with asyncio.timeout(90):
        await in_flight

    # The turn in flight ended once, as a failure its reader was told of. OpenCode does not take it up again.
    assert world.outcomes() == [OUTCOME_POSTED_FINAL, OUTCOME_POSTED_BY_TOOL, OUTCOME_FAILED]
    assert [post.text for post in world.posts("D-U2")] == [adapter.EXECUTION_LOST]

    # The sessions survived: the next turn of a conversation holds its earlier one.
    await world.send(world.direct("two SAY=SECOND", sender="U1"))
    assert said(_requests(model, "U1")[-1], "user") == ["one SAY=FIRST", "two SAY=SECOND"]
    assert said(_requests(model, "U1")[-1], "assistant") == ["FIRST"]
    # The tool mount did not survive, and was made again with the event stream.
    await world.send(world.direct("go " + call(POST, text="after the restart"), sender="U3"))
    assert [post.text for post in world.posts("D-U3")] == ["before the restart", "after the restart"]
    # The conversation of the lost turn takes its next one, and nothing of the lost one arrives late.
    await world.send(world.direct("SAY=again", sender="U2"))
    assert [post.text for post in world.posts("D-U2")] == [adapter.EXECUTION_LOST, "again"]


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
    orphan = session_id(NAMESPACE, _conversation("U2"))
    client = Client()
    # OpenCode went on running the turn of the Belle that died.
    assert orphan in await asyncio.to_thread(client.api, "GET", "/api/session/active")

    world = await start(300)

    # The turn was stopped at the start: nothing can deliver it.
    assert orphan not in await asyncio.to_thread(client.api, "GET", "/api/session/active")
    assert (await asyncio.to_thread(client.api, "GET", f"/api/session/{orphan}"))["outcome"] == "interrupted"

    # The event id window was read back: the message the dead Belle answered is not answered again.
    third = replace(world.direct("three SAY=THIRD", sender="U1"), message_id="m200", event_id="e200")
    assert await world.send(third) == "ignored:already-handled (dedupe)"
    # Past the window, the session itself still holds the request.
    await world.send(replace(third, message_id="seen-as-new"))
    assert world.outcomes() == [OUTCOME_DUPLICATE]
    assert world.posts() == []

    # Session ids are derived, so the conversation goes on with its history and no stored map.
    await world.send(world.direct("four SAY=FOURTH", sender="U1"))
    assert said(_requests(model, "U1")[-1], "user") == [
        "one SAY=FIRST",
        "two SAY=SECOND",
        "three SAY=THIRD",
        "four SAY=FOURTH",
    ]
    assert [post.text for post in world.posts("D-U1")] == ["FOURTH"]

    # The tools are mounted with this process's token. The dead one's is refused.
    assert await _tools_list(OLD_TOKEN.read_text(encoding="utf-8")) == 401
    assert await _tools_list(world.kit.backend._tool_server.token) == 200
    await world.send(world.direct("go " + call(POST, text="from the new belle"), sender="U3"))
    assert [post.text for post in world.posts("D-U3")] == ["from the new belle"]
    assert world.outcomes()[-1] == OUTCOME_POSTED_BY_TOOL

    # The conversation of the stopped turn takes its next one, and the stopped one posts nothing.
    await world.send(world.direct("SAY=back", sender="U2"))
    assert [post.text for post in world.posts("D-U2")] == ["back"]
