"""Host caches harness health samples for the status snapshot."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from scripted_backend import ScriptedBackend

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.backend import OP_HEALTH, BackendCapabilities, BackendHealth, McpHealth
from belle_agent.control.protocol import validate_component
from belle_agent.control.service import ControlSettings
from belle_agent.host import (
    HARNESS_HEALTH_STALE_SECONDS,
    ConversationHost,
    HostedPlatform,
)
from belle_agent.integrations.opencode import OpenCodeBackend
from belle_agent.platforms.loopback import LoopbackPlatform


class HealthBackend(ScriptedBackend):
    """Scripted backend that advertises and returns a fixed health sample."""

    def __init__(self, sample: BackendHealth) -> None:
        super().__init__(
            BackendCapabilities(
                backend='scripted',
                operations=frozenset({OP_HEALTH}),
            ),
        )
        self.sample = sample
        self.health_calls = 0

    async def health(self) -> BackendHealth:
        self.health_calls += 1
        return self.sample


class GatedHealthBackend(HealthBackend):
    """Health probe that waits on a gate before returning the current sample."""

    def __init__(self, sample: BackendHealth, gate: asyncio.Event) -> None:
        super().__init__(sample)
        self.gate = gate

    async def health(self) -> BackendHealth:
        self.health_calls += 1
        await self.gate.wait()
        return self.sample


async def _started_host(tmp_path, backend):
    host = ConversationHost(
        [HostedPlatform(LoopbackPlatform(), EventDedupStore(tmp_path / 'seen.json'))],
        backend,
    )
    await host.start()
    return host


@pytest.fixture
async def host_with_health(tmp_path):
    backend = HealthBackend(BackendHealth('ready'))
    host = await _started_host(tmp_path, backend)
    yield host, backend
    await host.stop()


def test_backend_health_requires_detail_when_not_ready():
    with pytest.raises(ValueError, match='needs a detail'):
        BackendHealth('degraded')
    with pytest.raises(ValueError, match='needs a detail'):
        BackendHealth('disconnected', '  ')
    with pytest.raises(ValueError, match='ready health has no detail'):
        BackendHealth('ready', 'ok')


@pytest.mark.asyncio
async def test_harness_status_reports_ready_from_cached_sample(host_with_health):
    host, backend = host_with_health
    before = backend.health_calls
    sample = await host.status_harness()
    validate_component(sample)
    assert sample == {
        'id': 'scripted', 'state': 'ready', 'reason': None,
        'observed_at': sample['observed_at'], 'metrics': {},
        'mcp': {'state': 'not_applicable', 'reason': None, 'observed_at': sample['observed_at']},
    }
    assert backend.health_calls == before


def test_mcp_health_rules():
    assert BackendHealth('ready') == BackendHealth('ready', mcp=McpHealth('not_applicable'))
    assert BackendHealth('ready') != BackendHealth('ready', mcp=McpHealth('ready'))
    with pytest.raises(TypeError, match='mcp must be McpHealth'):
        BackendHealth('ready', mcp={'state': 'ready'})  # type: ignore[arg-type]
    for state in ('ready', 'not_applicable'):
        with pytest.raises(ValueError, match='no reason'):
            McpHealth(state, 'x')
    for state in ('missing', 'failed', 'unknown'):
        with pytest.raises(ValueError, match='needs a reason'):
            McpHealth(state)
        with pytest.raises(ValueError, match='control characters'):
            McpHealth(state, 'bad\n')
    with pytest.raises(ValueError, match='unknown mcp state'):
        McpHealth('up')


@pytest.mark.asyncio
async def test_harness_status_carries_mcp_sample(tmp_path):
    backend = HealthBackend(BackendHealth('degraded', 'belle tool server absent', McpHealth('missing', 'mcp_mount_missing')))
    host = await _started_host(tmp_path, backend)
    try:
        sample = await host.status_harness()
        validate_component(sample)
        assert sample['mcp'] == {
            'state': 'missing', 'reason': 'mcp_mount_missing', 'observed_at': sample['observed_at'],
        }
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_harness_status_surfaces_degraded_detail(tmp_path):
    backend = HealthBackend(BackendHealth('degraded', 'event stream reconnecting'))
    host = await _started_host(tmp_path, backend)
    try:
        sample = await host.status_harness()
        validate_component(sample)
        assert sample['state'] == 'degraded'
        assert sample['reason'] == 'event stream reconnecting'
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_harness_without_op_health_stays_unsupported(tmp_path):
    host = await _started_host(tmp_path, ScriptedBackend())
    try:
        sample = await host.status_harness()
        assert sample == {
            'id': 'scripted', 'state': 'unsupported', 'reason': 'remote_health_not_measured',
            'observed_at': None, 'metrics': {},
            'mcp': {'state': 'not_applicable', 'reason': None, 'observed_at': None},
        }
        validate_component(sample)
        assert host._harness_health_task is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_stale_harness_sample_becomes_unavailable(host_with_health):
    host, _backend = host_with_health
    stale_at = (datetime.now(UTC) - timedelta(seconds=HARNESS_HEALTH_STALE_SECONDS + 1)).isoformat().replace(
        '+00:00', 'Z',
    )
    host._harness_health = {
        'id': 'scripted', 'state': 'ready', 'reason': None,
        'observed_at': stale_at, 'metrics': {},
        'mcp': {'state': 'ready', 'reason': None, 'observed_at': stale_at},
    }
    sample = await host.status_harness()
    validate_component(sample)
    assert sample['state'] == 'unavailable'
    assert sample['reason'] == 'health_sample_stale'
    assert sample['mcp'] == {'state': 'unknown', 'reason': 'health_sample_stale', 'observed_at': None}


@pytest.mark.asyncio
async def test_probe_failure_stores_disconnected(tmp_path):
    backend = HealthBackend(BackendHealth('ready'))

    async def boom() -> BackendHealth:
        raise OSError('down')

    backend.health = boom  # type: ignore[method-assign]
    host = await _started_host(tmp_path, backend)
    try:
        sample = await host.status_harness()
        assert sample['state'] == 'disconnected'
        assert sample['reason'] == 'unreachable'
        validate_component(sample)
        assert sample['mcp'] == {'state': 'unknown', 'reason': 'unreachable', 'observed_at': None}
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_non_backend_health_return_is_invalid(tmp_path):
    backend = HealthBackend(BackendHealth('ready'))

    async def wrong() -> BackendHealth:
        return {'state': 'ready'}  # type: ignore[return-value]

    backend.health = wrong  # type: ignore[method-assign]
    host = await _started_host(tmp_path, backend)
    try:
        sample = await host.status_harness()
        assert sample['state'] == 'unavailable'
        assert sample['reason'] == 'health_sample_invalid'
        validate_component(sample)
        assert sample['mcp'] == {'state': 'unknown', 'reason': 'health_sample_invalid', 'observed_at': None}
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_hanging_health_probe_does_not_block_startup(tmp_path, monkeypatch):
    monkeypatch.setattr('belle_agent.host.HARNESS_HEALTH_PROBE_SECONDS', 0.05)
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv('BELLE_AGENT_RUNTIME_DIR', str(runtime))
    gate = asyncio.Event()
    backend = GatedHealthBackend(BackendHealth('ready'), gate)
    host = ConversationHost(
        [HostedPlatform(LoopbackPlatform(), EventDedupStore(tmp_path / 'seen.json'))],
        backend,
        control=ControlSettings(enabled=True),
    )
    await asyncio.wait_for(host.start(), timeout=2.0)
    try:
        assert host._running
        assert host._control.socket_path is not None
        assert host._control.socket_path.exists()
        sample = await host.status_harness()
        assert sample['state'] == 'disconnected'
        assert sample['reason'] == 'unreachable'

        gate.set()
        await host._refresh_harness_health()
        sample = await host.status_harness()
        assert sample['state'] == 'ready'
        assert sample['reason'] is None
    finally:
        await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('mcp_response', (
    httpx.Response(200, content=b'not-json'),
    httpx.Response(200, json={'data': [{'name': 'belle', 'status': {'status': []}}]}),
    httpx.Response(200, json={'data': None}),
    httpx.Response(200, json={'data': [None]}),
))
async def test_opencode_malformed_mcp_keeps_host_ready(tmp_path, mcp_response):
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/model/default':
            return httpx.Response(200, json={'data': {'providerID': 'configured', 'id': 'vendor/served'}})
        if request.url.path == '/api/mcp':
            return mcp_response
        return httpx.Response(404)

    placeholder = HealthBackend(BackendHealth('ready'))
    host = await _started_host(tmp_path, placeholder)
    opencode = OpenCodeBackend('http://opencode.test', 'password', transport=httpx.MockTransport(answer))
    opencode._started = True
    opencode._subscribed.set()
    host._backend = opencode
    host._capabilities = opencode.capabilities()
    try:
        await host._refresh_harness_health()
        sample = await host.status_harness()
        validate_component(sample)
        assert sample['state'] == 'ready'
        assert sample['reason'] is None
        assert sample['mcp'] == {
            'state': 'unknown', 'reason': 'mcp_probe_failed', 'observed_at': sample['observed_at'],
        }
    finally:
        await host.stop()
        await opencode.close()
