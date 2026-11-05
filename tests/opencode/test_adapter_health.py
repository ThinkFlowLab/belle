"""OpenCode adapter health(): cheap local and wire checks."""

import asyncio

import httpx
import pytest

from belle_agent.backend import OP_HEALTH, BackendHealth, McpHealth
from belle_agent.integrations.opencode import OpenCodeBackend

DEFAULT = {"providerID": "configured", "id": "vendor/served"}
BELLE_CONNECTED = [{"name": "belle", "status": {"status": "connected"}}]
UNREACHABLE = McpHealth("unknown", "backend_unreachable")
READY_MCP = McpHealth("ready")
PROBE_FAILED = McpHealth("unknown", "mcp_probe_failed")


def _backend(transport: httpx.MockTransport) -> OpenCodeBackend:
    return OpenCodeBackend("http://opencode.test", "password", transport=transport)


def _live(backend: OpenCodeBackend) -> OpenCodeBackend:
    backend._started = True
    backend._subscribed.set()
    return backend


def _answer(mcp=BELLE_CONNECTED, *, model=DEFAULT, model_status=200, calls=None, mcp_response=None):
    def answer(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path))
        if request.url.path == "/api/model/default":
            if isinstance(model, Exception):
                raise model
            return httpx.Response(model_status, json={"data": model} if model_status == 200 else {"message": "refused"})
        if request.url.path == "/api/mcp":
            if isinstance(mcp, Exception):
                raise mcp
            if mcp_response is not None:
                return mcp_response
            return httpx.Response(200, json={"data": mcp})
        return httpx.Response(404)

    return answer


async def test_health_before_start_is_disconnected():
    backend = _backend(httpx.MockTransport(_answer()))
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("disconnected", "not started", McpHealth("unknown", "not_started"))
    assert OP_HEALTH in backend.capabilities().operations


async def test_health_when_subscribed_and_default_model_is_ready():
    backend = _live(_backend(httpx.MockTransport(_answer())))
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("ready", mcp=READY_MCP)


async def test_health_while_event_stream_reconnects_is_degraded():
    backend = _backend(httpx.MockTransport(_answer()))
    backend._started = True
    backend._subscribed.clear()
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("degraded", "event stream reconnecting", READY_MCP)


async def test_health_when_unsubscribed_and_unreachable_is_disconnected():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    backend = _backend(httpx.MockTransport(fail))
    backend._started = True
    backend._subscribed.clear()
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("disconnected", "unreachable", UNREACHABLE)


async def test_health_when_default_model_is_not_ready_yet_is_degraded():
    backend = _live(_backend(httpx.MockTransport(_answer(model_status=503))))
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("degraded", "default model not ready", READY_MCP)


async def test_health_when_default_model_ids_are_empty_is_degraded():
    backend = _live(_backend(httpx.MockTransport(_answer(model={"providerID": "", "id": ""}))))
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("degraded", "default model not ready", READY_MCP)


async def test_health_when_default_model_times_out():
    async def boom() -> tuple[str, str] | None:
        raise TimeoutError

    backend = _live(_backend(httpx.MockTransport(_answer())))
    backend._wire.probe_default_model = boom  # type: ignore[method-assign]
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("disconnected", "unreachable", UNREACHABLE)


async def test_health_probe_does_not_update_wire_readiness():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    backend = _live(_backend(httpx.MockTransport(fail)))
    backend._wire.unready = "prior"
    backend._wire.model_input = frozenset({"text"})
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("disconnected", "unreachable", UNREACHABLE)
    assert backend._wire.unready == "prior"
    assert backend._wire.model_input == frozenset({"text"})


@pytest.mark.parametrize("status", (401, 403))
async def test_health_when_authentication_is_refused(status: int):
    backend = _live(_backend(httpx.MockTransport(_answer(model_status=status))))
    try:
        sample = await backend.health()
    finally:
        await backend.close()

    assert sample == BackendHealth("disconnected", "authentication refused", UNREACHABLE)


async def _sample(backend: OpenCodeBackend) -> BackendHealth:
    try:
        return await backend.health()
    finally:
        await backend.close()


async def test_health_reports_connected_belle_mount_ready():
    calls: list[tuple[str, str]] = []
    backend = _live(_backend(httpx.MockTransport(_answer(calls=calls))))

    assert await _sample(backend) == BackendHealth("ready", mcp=READY_MCP)
    assert {method for method, _ in calls} == {"GET"}
    assert ("GET", "/api/mcp") in calls


@pytest.mark.parametrize("servers", ([], [{"name": "other", "status": {"status": "connected"}}]))
async def test_health_when_belle_mount_is_absent_is_degraded(servers):
    sample = await _sample(_live(_backend(httpx.MockTransport(_answer(servers)))))

    assert sample == BackendHealth(
        "degraded", "mcp_mount_missing", McpHealth("missing", "mcp_mount_missing"),
    )


async def test_health_when_belle_mount_failed_is_degraded():
    backend = _live(_backend(httpx.MockTransport(
        _answer([{"name": "belle", "status": {"status": "failed", "error": "secret"}}]),
    )))
    sample = await _sample(backend)

    assert sample == BackendHealth("degraded", "mcp_mount_failed", McpHealth("failed", "mcp_mount_failed"))
    assert "secret" not in repr(sample)


@pytest.mark.parametrize("state,reason", [
    ("disconnected", "mcp_mount_disconnected"),
    ("disabled", "mcp_mount_disabled"),
    ("needs_auth", "mcp_mount_needs_auth"),
    ("needs_client_registration", "mcp_mount_needs_auth"),
])
async def test_health_when_belle_mount_is_unusable_is_degraded(state, reason):
    backend = _live(_backend(httpx.MockTransport(
        _answer([{"name": "belle", "status": {"status": state}}]),
    )))

    assert await _sample(backend) == BackendHealth("degraded", reason, McpHealth("failed", reason))


async def test_health_when_belle_mount_status_is_unknown_stays_ready():
    backend = _live(_backend(httpx.MockTransport(
        _answer([{"name": "belle", "status": {"status": "other"}}]),
    )))

    assert await _sample(backend) == BackendHealth(
        "ready", mcp=McpHealth("unknown", "mcp_mount_not_connected"),
    )


async def test_health_when_mcp_payload_is_not_a_list_keeps_model_ready():
    backend = _live(_backend(httpx.MockTransport(_answer({"belle": {"status": "connected"}}))))

    assert await _sample(backend) == BackendHealth("ready", mcp=PROBE_FAILED)


@pytest.mark.parametrize("mcp", (None, [None], [{"name": 1, "status": {"status": "connected"}}]))
async def test_health_when_mcp_listing_is_malformed_keeps_model_ready(mcp):
    assert await _sample(_live(_backend(httpx.MockTransport(_answer(mcp))))) == BackendHealth(
        "ready", mcp=PROBE_FAILED,
    )


async def test_health_when_mcp_json_is_invalid_keeps_model_ready():
    backend = _live(_backend(httpx.MockTransport(_answer(
        mcp_response=httpx.Response(200, content=b"not-json"),
    ))))

    assert await _sample(backend) == BackendHealth("ready", mcp=PROBE_FAILED)


async def test_health_when_belle_status_is_not_a_string_keeps_model_ready():
    backend = _live(_backend(httpx.MockTransport(_answer(
        [{"name": "belle", "status": {"status": []}}],
    ))))

    assert await _sample(backend) == BackendHealth("ready", mcp=PROBE_FAILED)


async def test_health_when_mcp_probe_errors_keeps_model_ready():
    backend = _live(_backend(httpx.MockTransport(_answer(httpx.ConnectError("boom")))))

    assert await _sample(backend) == BackendHealth("ready", mcp=PROBE_FAILED)


async def test_health_when_mcp_probe_times_out_keeps_model_ready(monkeypatch):
    monkeypatch.setattr("belle_agent.integrations.opencode.adapter.HEALTH_TIMEOUT_SECONDS", 0.2)
    backend = _live(_backend(httpx.MockTransport(lambda _: httpx.Response(404))))

    async def model():
        return ("configured", "vendor/served")

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(10)

    backend._wire.probe_default_model = model  # type: ignore[method-assign]
    backend._wire.call = hang  # type: ignore[method-assign]

    assert await _sample(backend) == BackendHealth("ready", mcp=McpHealth("unknown", "mcp_probe_timeout"))


async def test_health_keeps_model_detail_and_attaches_missing_mcp():
    backend = _live(_backend(httpx.MockTransport(_answer([]))))
    backend._subscribed.clear()

    assert await _sample(backend) == BackendHealth(
        "degraded", "event stream reconnecting", McpHealth("missing", "mcp_mount_missing"),
    )


async def test_health_rejects_non_object_mount_status():
    backend = _live(_backend(httpx.MockTransport(
        _answer([{"name": "belle", "status": "connected"}]),
    )))

    assert await _sample(backend) == BackendHealth("ready", mcp=PROBE_FAILED)
