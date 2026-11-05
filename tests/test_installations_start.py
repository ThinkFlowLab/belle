"""Installations that do not answer at start: the others are served, and they are started again in the background."""

import asyncio
import logging

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.platforms import installations
from belle_agent.platforms.base import PlatformUnreachable
from belle_agent.platforms.installations import Installations


class Member:
    """An installation that answers once ``reachable`` is set, and stops for good once ``down`` is set."""

    name, posting_tool, capabilities, ephemeral_posts = "fake", "post", None, False
    public_chat_types = direct_chat_types = frozenset()

    def __init__(self, *, reachable: bool = True) -> None:
        self.event_kinds: dict = {}
        self.reachable = reachable
        self.refusal = ""
        self.down = asyncio.Event()
        self.starts = self.stops = 0

    async def start(self, on_inbound) -> None:
        self.starts += 1
        if self.refusal:
            raise BelleConfigError(self.refusal)
        if not self.reachable:
            raise PlatformUnreachable("no answer")

    async def stop(self) -> None:
        self.stops += 1

    async def wait_down(self) -> str:
        await self.down.wait()
        return "gone"


def _port(**members: Member) -> Installations:
    return Installations(
        members, noun="server", where="servers", installation_of=lambda workspace: workspace, metadata_key="server",
        unknown_tool=lambda name, server: None, logger=logging.getLogger("test"),
    )


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(installations, "RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(installations, "RETRY_MAX_SECONDS", 0.01)


async def _until(condition) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


async def _receive(record) -> str:
    return "answered"


async def test_the_platform_is_down_only_once_an_installation_that_connected_later_is_down_too():
    east, west = Member(), Member(reachable=False)
    port = _port(east=east, west=west)
    await port.start(_receive)
    down = asyncio.ensure_future(port.wait_down())

    east.down.set()
    await asyncio.sleep(0.05)
    assert not down.done()

    west.reachable = True
    await _until(lambda: "west" in port.serving)
    await asyncio.sleep(0.05)
    assert not down.done()

    west.down.set()
    assert await asyncio.wait_for(down, 5) == "east: gone; west: gone"
    await port.stop()


async def test_the_platform_is_down_once_an_installation_that_was_connecting_is_refused():
    east, west = Member(), Member(reachable=False)
    port = _port(east=east, west=west)
    await port.start(_receive)
    down = asyncio.ensure_future(port.wait_down())
    east.down.set()

    west.refusal = "the token is refused"
    assert await asyncio.wait_for(down, 5) == "east: gone"
    assert west.stops == 1 and not port.connecting
    await port.stop()


async def test_an_installation_started_again_is_stopped_once_when_the_platform_stops():
    east, west = Member(), Member(reachable=False)
    port = _port(east=east, west=west)
    await port.start(_receive)
    await _until(lambda: west.starts > 2)

    await port.stop()
    starts = west.starts
    await asyncio.sleep(0.05)

    assert west.starts == starts and (east.stops, west.stops) == (1, 1)
    assert not port.connecting and not port.serving
