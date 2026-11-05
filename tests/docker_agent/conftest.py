"""Fixtures for the checks that need the Docker Agent container.

`tests/conftest.py` skips them unless DAB_AGENT_URL names a Docker Agent server.
"""

import sys
from pathlib import Path

import pytest
import pytest_asyncio

# The adapter checks run a host on the loopback platform, as the conformance suite does.
sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import worlds
from docker_agent_kit import DockerAgentKit, standin


@pytest.fixture(scope="session")
def model():
    """The stand-in model. It answers the directives in the message text."""
    return standin()


@pytest.fixture
def kit(model) -> DockerAgentKit:
    return DockerAgentKit()


@pytest_asyncio.fixture
async def build_world(kit, tmp_path):
    async with worlds(kit, tmp_path) as build:
        yield build


@pytest_asyncio.fixture
async def world(build_world):
    """A host on the loopback platform with the adapter behind it."""
    return await build_world()
