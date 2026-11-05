"""Fixtures for the checks that need the OpenCode container.

`tests/conftest.py` skips them unless OCB_OPENCODE_URL names an OpenCode server.
"""

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# The adapter checks run a host on the loopback platform, as the conformance suite does.
sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import worlds
from drive import Client, Events
from listeners import McpServer, Relay
from opencode_kit import OpenCodeKit, standin


class Policy:
    """The relay's answers, per hook and session: a dict, an HTTP status, or a function of the event."""

    def __init__(self):
        self.rules: dict[tuple[str, str], object] = {}

    def set(self, hook: str, session: str, answer) -> None:
        self.rules[(hook, session)] = answer

    def __call__(self, hook: str, event: dict):
        answer = self.rules.get((hook, event.get("sessionID")), {})
        return answer(event) if callable(answer) else answer


@pytest.fixture(scope="session")
def relay():
    """The relay endpoint. Every prompt needs it: the plugin's hooks throw when it is away."""
    relay = Relay(token=os.environ.get("BELLE_RELAY_TOKEN", ""))
    relay.answer = Policy()
    yield relay
    relay.close()


@pytest.fixture(scope="session")
def model():
    """The stand-in model. Its request log is stored with the authorization header removed."""
    return standin()


@pytest.fixture(scope="session")
def mcp():
    mcp = McpServer()
    mcp.add("echo", lambda arguments: "echo saw " + arguments.get("token", ""), "Echo a token back.")
    yield mcp
    mcp.close()


@pytest.fixture(scope="session")
def client(relay):
    client = Client()
    client.wait_ready()
    return client


@pytest.fixture(scope="session")
def events(client):
    events = Events(client)
    yield events
    events.close()


@pytest.fixture
def kit(model) -> OpenCodeKit:
    """The adapter beside the stand-in that reads its directives from the message text."""
    return OpenCodeKit()


@pytest_asyncio.fixture
async def build_world(kit, tmp_path):
    async with worlds(kit, tmp_path) as build:
        yield build


@pytest_asyncio.fixture
async def world(build_world):
    """A host on the loopback platform with the adapter behind it."""
    return await build_world()
