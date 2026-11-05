"""Docker Agent adapter health checks. No container and no network."""

from unittest.mock import AsyncMock

import httpx
import pytest

from belle_agent.backend import BackendHealth, McpHealth, OP_HEALTH
from belle_agent.integrations.docker_agent import DockerAgentBackend
from belle_agent.integrations.docker_agent.wire import WireError
from test_docker_agent_wire import FakeDockerAgent, TOKEN, _started_backend

pytestmark = pytest.mark.asyncio

MCP = McpHealth("unknown", "mcp_status_unavailable")


async def test_health_before_start_is_not_started(tmp_path):
    backend = DockerAgentBackend(
        "http://docker-agent.test:8080",
        TOKEN,
        relay_token="relay",
        state_dir=tmp_path,
        transport=httpx.MockTransport(FakeDockerAgent()),
    )
    assert await backend.health() == BackendHealth("disconnected", "not started", McpHealth("unknown", "not_started"))


async def test_health_after_start_ping_ok_is_ready():
    backend = await _started_backend(FakeDockerAgent())
    try:
        assert OP_HEALTH in backend.capabilities().operations
        assert await backend.health() == BackendHealth("ready", mcp=MCP)
    finally:
        await backend.close()


async def test_health_after_start_ping_fail_is_unreachable():
    backend = await _started_backend(FakeDockerAgent())
    try:
        backend._ping = AsyncMock(return_value=False)
        assert await backend.health() == BackendHealth("disconnected", "unreachable", MCP)
    finally:
        await backend.close()


async def test_health_probe_does_not_update_unready():
    backend = await _started_backend(FakeDockerAgent())
    backend._unready = "prior"

    async def fail_ping(*, record: bool = True) -> bool:
        assert record is False
        return False

    try:
        backend._ping = fail_ping  # type: ignore[method-assign]
        assert await backend.health() == BackendHealth("disconnected", "unreachable", MCP)
        assert backend._unready == "prior"
    finally:
        await backend.close()


async def test_health_when_authentication_is_refused():
    backend = await _started_backend(FakeDockerAgent())

    async def refuse(*, record: bool = True) -> bool:
        raise WireError("GET", "/ping", 401, "unauthorized")

    try:
        backend._ping = refuse  # type: ignore[method-assign]
        assert await backend.health() == BackendHealth("disconnected", "authentication refused", MCP)
    finally:
        await backend.close()
