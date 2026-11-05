"""Fixtures of the ACP backend checks: a scripted agent on a Unix socket, and the backend in front of it."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("acp", reason="the ACP checks need the acp extra")
sys.path += [str(Path(__file__).resolve().parents[1] / name) for name in ("acp", "conformance")]

from acp_kit import Toolkit, settings
from scripted_agent import ScriptedAgent

from belle_agent.integrations.acp import AcpBackend
from belle_agent.tools.host import ToolHost


@pytest.fixture
def socket_dir():
    # A Unix socket path has a short limit, so the directory is short too.
    directory = tempfile.mkdtemp(prefix="acp", dir="/tmp")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def toolkit() -> Toolkit:
    return Toolkit()


@pytest.fixture
def tools(toolkit) -> ToolHost:
    return ToolHost([toolkit], posting_tool="post")


@pytest.fixture
async def agent(socket_dir):
    server = ScriptedAgent(f"{socket_dir}/agent.sock")
    await server.start()
    yield server
    await server.close()


@pytest.fixture
async def make_backend(tmp_path, tools):
    """Build and start a backend on a socket, with its state in ``tmp_path`` unless named. Each is closed at the end."""
    built: list[AcpBackend] = []

    async def make(socket: str, *, state: Path | None = None, **options) -> AcpBackend:
        backend = AcpBackend(settings(socket, **options), state or tmp_path / "state")
        await backend.start(tools)
        built.append(backend)
        return backend

    yield make
    for each in built:
        await each.close()


@pytest.fixture
async def backend(agent, make_backend):
    return await make_backend(agent.path)
