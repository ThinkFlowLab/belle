"""Boundary checks for the independent local status protocol and service."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from belle_agent.control.client import get_snapshot
from belle_agent.control.protocol import (
    ControlError,
    decode_line,
    decode_response,
    encode_line,
    encode_request,
    utc_now,
    validate_component,
    validate_snapshot,
)
from belle_agent.control.service import CachedModelProvider, ControlSettings, SnapshotCollector, StatusService
from belle_agent.models import task_model_rows


async def measured():
    return {'id': 'work', 'state': 'ready', 'reason': None, 'observed_at': utc_now(),
            'metrics': {'queued_messages': {'value': 0, 'reason': None},
                        'active_turns': {'value': None, 'reason': 'not_measured'}}}


@pytest.mark.asyncio
async def test_round_trip_and_unknown_zero():
    result = await SnapshotCollector({'work': measured}).collect()
    assert validate_snapshot(result) == result
    assert result['work']['metrics']['queued_messages']['value'] == 0
    assert result['work']['metrics']['active_turns']['value'] is None
    response = encode_line({'version': 4, 'id': 'x', 'result': result}, 262144)
    assert decode_response(response, 'x') == result
    result['work']['metrics']['active_turns']['reason'] = None
    with pytest.raises(ControlError, match='reason'):
        validate_snapshot(result)


@pytest.mark.asyncio
async def test_task_model_catalog_shows_selection_without_secrets():
    rows = task_model_rows({'models': {'decision': {
        'default': 'review', 'entries': [
            {'id': 'quick', 'api': 'openrouter', 'model_name': 'fast', 'api_key': 'PRIVATE'},
            {'id': 'review', 'api': 'openai', 'model_name': 'careful', 'api_base': 'https://private.example'},
        ]}, 'summarizer': {'entries': [{'id': 'brief', 'api': 'scaledown', 'model_name': 'summarize'}]}}})
    result = await SnapshotCollector({}, task_models=rows).collect()
    assert [(row['role'], row['id'], row['selected']) for row in result['task_models']] == [
        ('decision', 'quick', False), ('decision', 'review', True), ('summarizer', 'brief', True)]
    assert 'PRIVATE' not in json.dumps(result)
    assert 'private.example' not in json.dumps(result)
    assert validate_snapshot(result) == result
    result['task_models'].append(dict(rows[0]))
    with pytest.raises(ControlError, match='unique role and id'):
        validate_snapshot(result)


@pytest.mark.asyncio
async def test_harness_model_catalog_is_reported_or_explicitly_unavailable():
    async def listed():
        return [{'owner': 'opencode', 'model_name': 'provider/model'}]

    result = await SnapshotCollector({}, harness_models=listed).collect()
    assert result['harness_models'] == [{'owner': 'opencode', 'model_name': 'provider/model'}]
    assert validate_snapshot(result) == result

    result = await SnapshotCollector({}).collect()
    assert result['harness_models'] is None
    assert validate_snapshot(result) == result
    result['harness_models'] = [{'owner': 'opencode', 'model_name': 'provider/model'},
                                {'owner': 'opencode', 'model_name': 'provider/model'}]
    with pytest.raises(ControlError, match='unique owner and model name'):
        validate_snapshot(result)


@pytest.mark.asyncio
async def test_harness_model_catalog_cache_limits_refreshes_and_drops_failed_rows():
    now = 0.0
    calls = 0

    async def listed():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('catalog unavailable')
        return [{'owner': 'opencode', 'model_name': f'model-{calls}'}]

    cache = CachedModelProvider(listed, ttl=30, clock=lambda: now)
    assert await cache() == [{'owner': 'opencode', 'model_name': 'model-1'}]
    now = 10
    assert await cache() == [{'owner': 'opencode', 'model_name': 'model-1'}]
    assert calls == 1
    now = 31
    assert await cache() is None
    now = 32
    assert await cache() is None
    assert calls == 2


def test_task_model_catalog_uses_second_brain_selection():
    rows = task_model_rows({'models': {'summarizer': {'default': 'first', 'entries': [
        {'id': 'first', 'api': 'openrouter'}, {'id': 'second', 'api': 'scaledown'}]}},
        'second_brain': {'ingest': {'summarizer': {'model': 'second'}}}})
    assert [(row['id'], row['selected']) for row in rows] == [('first', False), ('second', True)]


def test_task_model_catalog_marks_the_outline_entry_selected():
    rows = task_model_rows({'models': {'summarizer': {'default': 'first', 'entries': [
        {'id': 'first', 'api': 'chat_completions'}, {'id': 'second', 'api': 'scaledown'},
        {'id': 'third', 'api': 'openrouter'}]}},
        'second_brain': {'ingest': {'summarizer': {'model': 'second', 'outline_model': 'first'}}}})
    assert [(row['id'], row['selected']) for row in rows] == [('first', True), ('second', True), ('third', False)]


def test_component_reason_optional_on_degraded_and_disconnected():
    stamp = utc_now()
    for state, reason in (
        ('degraded', 'event stream reconnecting'),
        ('disconnected', 'unreachable'),
        ('degraded', None),
        ('disconnected', None),
    ):
        validate_component({
            'id': 'opencode', 'state': state, 'reason': reason,
            'observed_at': stamp, 'metrics': {},
        })
    with pytest.raises(ControlError, match='reason'):
        validate_component({
            'id': 'opencode', 'state': 'ready', 'reason': 'unexpected',
            'observed_at': stamp, 'metrics': {},
        })
    with pytest.raises(ControlError, match='reason'):
        validate_component({
            'id': 'opencode', 'state': 'unavailable', 'reason': None,
            'observed_at': None, 'metrics': {},
        })


@pytest.mark.parametrize('data,code', [
    (b'broken\n', 'bad_json'), (b'\xff\n', 'bad_json'),
    (b'{"version":3,"id":"x","method":"snapshot.get"}\n', 'unsupported_version'),
    (b'{"version":4,"id":"x","method":"action.stop_turn"}\n', 'unknown_method'),
    (b'{"version":4,"id":"x"}\n', 'invalid_request'),
    (b'{"version":4,"version":4}\n', 'invalid_request'),
])
@pytest.mark.asyncio
async def test_precise_socket_errors(tmp_path, data, code):
    service = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True))
    assert await service.start()
    try:
        reader, writer = await asyncio.open_unix_connection(service.socket_path)
        writer.write(data)
        await writer.drain()
        response = json.loads(await reader.readline())
        assert response['error']['code'] == code
        if '"id":"x"' in data.decode(errors='ignore'):
            assert response['id'] == 'x'
        writer.close()
        await writer.wait_closed()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_multiple_clients_restart_and_live_socket(tmp_path):
    service = StatusService(SnapshotCollector({'work': measured}), tmp_path, ControlSettings(enabled=True))
    assert await service.start()
    try:
        snapshots = await asyncio.gather(*(get_snapshot(service.socket_path) for _ in range(5)))
        assert len({value['instance_id'] for value in snapshots}) == 1
        assert service.socket_path.stat().st_mode & 0o777 == 0o600
        competitor = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True))
        assert not await competitor.start()
        assert await get_snapshot(service.socket_path)
        previous = snapshots[0]['instance_id']
        await service.stop()
        assert await service.start()
        assert (await get_snapshot(service.socket_path))['instance_id'] != previous
    finally:
        await service.stop()
    assert not service.socket_path.exists()


@pytest.mark.asyncio
async def test_stale_socket_and_refuse_other_files(tmp_path):
    path = tmp_path / 'control.sock'
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind(str(path))
    service = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True))
    assert await service.start()
    await service.stop()
    path.write_text('keep')
    assert not await service.start()
    assert path.read_text() == 'keep'


@pytest.mark.asyncio
async def test_disabled_and_unsafe_directory(tmp_path):
    path = tmp_path / 'absent'
    service = StatusService(SnapshotCollector({}), path)
    assert not await service.start()
    assert not path.exists()
    service = StatusService(SnapshotCollector({}), None, ControlSettings(enabled=True))
    assert not await service.start()
    tmp_path.chmod(0o755)
    service = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True))
    assert not await service.start()
    assert tmp_path.stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_provider_timeout_failure_and_shutdown(tmp_path):
    async def slow():
        await asyncio.sleep(60)
    async def failed():
        raise ValueError('PRIVATE message/token/path')
    collector = SnapshotCollector({'work': measured, 'platforms/slow': slow, 'harnesses/broken': failed}, timeout=0.01)
    result = await collector.collect()
    assert result['platforms'][0]['reason'] == 'provider_timeout'
    assert result['harnesses'][0]['reason'] == 'provider_failed'
    assert result['harnesses'][0]['mcp']['state'] == 'unknown'
    assert 'PRIVATE' not in json.dumps(result)
    assert result['work']['state'] == 'ready'
    service = StatusService(collector, tmp_path, ControlSettings(enabled=True))
    assert await service.start()
    reader, writer = await asyncio.open_unix_connection(service.socket_path)
    await asyncio.sleep(0)
    await service.stop()
    assert await reader.read() == b''
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_client_and_request_limits(tmp_path):
    service = StatusService(SnapshotCollector({}), tmp_path,
                            ControlSettings(enabled=True, max_clients=1, max_request_bytes=64, request_timeout=0.1))
    assert await service.start()
    try:
        reader, writer = await asyncio.open_unix_connection(service.socket_path)
        await asyncio.sleep(0)
        other, second = await asyncio.open_unix_connection(service.socket_path)
        assert await other.read() == b''
        second.close()
        await second.wait_closed()
        assert await reader.read() == b''  # Idle connection times out.
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)
        reader, writer = await asyncio.open_unix_connection(service.socket_path)
        writer.write(b'x' * 65 + b'\n')
        await writer.drain()
        assert json.loads(await reader.readline())['error']['code'] == 'message_too_large'
        writer.close()
        await writer.wait_closed()
    finally:
        await service.stop()


def test_protocol_import_has_no_optional_dependencies():
    code = """
import sys
import belle_agent.control.protocol
assert not any(name.startswith(('belle_agent.platforms', 'belle_agent.integrations', 'textual', 'openjiuwen')) for name in sys.modules)
"""
    subprocess.run([sys.executable, '-S', '-c', code], check=True,
                   env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})


def test_limits_and_config_validation():
    with pytest.raises(ControlError) as exc:
        decode_line(b'{}\n', 2)
    assert exc.value.code == 'message_too_large'
    with pytest.raises(ControlError):
        encode_request('')
    for values in ({'enabled': 'true'}, {'max_clients': 0}, {'provider_timeout': float('nan')}):
        with pytest.raises(ValueError):
            ControlSettings(**values)
    with pytest.raises(ValueError, match='enabled'):
        ControlSettings.from_config({'control': {'max_clients': 20}})
    assert ControlSettings.from_config({}).enabled is False


def test_committed_wire_fixtures():
    directory = Path(__file__).parent / 'fixtures' / 'control'
    assert encode_request('probe-1') == (directory / 'request.json').read_bytes()
    assert decode_response((directory / 'response.json').read_bytes(), 'probe-1')['work']['state'] == 'ready'


def _harness(**mcp):
    now = utc_now()
    return {'id': 'h', 'state': 'ready', 'reason': None, 'observed_at': now, 'metrics': {},
            'mcp': {'state': 'ready', 'reason': None, 'observed_at': now, **mcp}}


@pytest.mark.parametrize('mcp', [
    {}, {'state': 'missing', 'reason': 'mcp_mount_missing'}, {'state': 'unknown', 'reason': 'x', 'observed_at': None},
    {'state': 'not_applicable', 'observed_at': None},
])
def test_component_accepts_mcp_check(mcp):
    validate_component(_harness(**mcp))


@pytest.mark.parametrize('mcp', [
    {'state': 'up'}, {'reason': 'x'}, {'state': 'failed', 'reason': None}, {'state': 'failed', 'reason': 'a\nb'},
    {'observed_at': '2026-10-18T12:00:00+02:00'}, {'observed_at': 'now'},
])
def test_component_rejects_bad_mcp_check(mcp):
    with pytest.raises(ControlError):
        validate_component(_harness(**mcp))
    extra = _harness()
    extra['mcp']['extra'] = 1
    with pytest.raises(ControlError, match='exactly'):
        validate_component(extra)


@pytest.mark.asyncio
async def test_snapshot_requires_mcp_on_harnesses_only():
    async def harness():
        return _harness()

    async def harness_without_mcp():
        value = _harness()
        del value['mcp']
        return value

    async def platform_with_mcp():
        return _harness()

    result = await SnapshotCollector({'work': measured, 'harnesses/h': harness}).collect()
    assert result['harnesses'][0]['mcp']['state'] == 'ready'
    del result['harnesses'][0]['mcp']
    with pytest.raises(ControlError, match='harnesses.mcp'):
        validate_snapshot(result)
    bad_harness = await SnapshotCollector({'work': measured, 'harnesses/h': harness_without_mcp}).collect()
    assert bad_harness['harnesses'][0]['reason'] == 'provider_failed'
    assert bad_harness['harnesses'][0]['mcp']['state'] == 'unknown'
    bad_platform = await SnapshotCollector({'work': measured, 'platforms/p': platform_with_mcp}).collect()
    assert bad_platform['platforms'][0]['reason'] == 'provider_failed'
    assert 'mcp' not in bad_platform['platforms'][0]
    work = await measured()
    work['mcp'] = _harness()['mcp']
    scheduling = {
        'id': 'scheduling', 'state': 'ready', 'reason': None, 'observed_at': utc_now(),
        'metrics': {'due_runs': {'value': 0, 'reason': None}, 'running_runs': {'value': 0, 'reason': None}},
    }
    with pytest.raises(ControlError, match='work.mcp'):
        validate_snapshot({
            'instance_id': 'x', 'generated_at': utc_now(), 'platforms': [], 'harnesses': [_harness()],
            'work': work, 'scheduling': scheduling, 'task_models': [], 'harness_models': None,
        })


@pytest.mark.asyncio
async def test_response_budget_returns_error_without_truncation(tmp_path):
    async def large():
        value = await measured()
        value['id'] = 'x' * 1000
        return value
    service = StatusService(SnapshotCollector({'work': large}), tmp_path,
                            ControlSettings(enabled=True, max_response_bytes=512))
    assert await service.start()
    try:
        with pytest.raises(ControlError) as error:
            await get_snapshot(service.socket_path)
        assert error.value.code == 'message_too_large'
        assert 'max_response_bytes' in str(error.value)
        assert 'bytes' in str(error.value)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_external_live_socket_is_preserved(tmp_path):
    path = tmp_path / 'control.sock'
    external = await asyncio.start_unix_server(lambda reader, writer: writer.close(), path=path)
    inode = path.stat().st_ino
    service = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True))
    try:
        assert not await service.start()
        assert path.stat().st_ino == inode
    finally:
        external.close()
        await external.wait_closed()


@pytest.mark.asyncio
async def test_shutdown_cancels_provider_during_active_request(tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def pending():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    collector = SnapshotCollector({'work': pending}, timeout=1)
    service = StatusService(collector, tmp_path, ControlSettings(enabled=True))
    assert await service.start()
    reader, writer = await asyncio.open_unix_connection(service.socket_path)
    writer.write(encode_request('shutdown'))
    await writer.drain()
    await started.wait()
    await service.stop()
    assert cancelled.is_set()
    assert await reader.read() == b''
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_shutdown_closes_a_client_whose_handler_has_not_started(tmp_path):
    service = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True))
    original = service._connected
    accepted = asyncio.Event()
    stopping = None

    def connected(reader, writer):
        nonlocal stopping
        stopping = asyncio.create_task(service.stop())
        original(reader, writer)
        accepted.set()

    service._connected = connected
    assert await service.start()
    reader, writer = await asyncio.open_unix_connection(service.socket_path)
    try:
        await accepted.wait()
        async with asyncio.timeout(1):
            await stopping
            assert await reader.read() == b''
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_stalled_reader_cannot_hold_client_slot(tmp_path):
    started = asyncio.Event()
    async def large():
        value = await measured()
        value['id'] = 'x' * 1000000
        started.set()
        return value
    settings = ControlSettings(enabled=True, max_response_bytes=2000000, request_timeout=0.5)
    service = StatusService(SnapshotCollector({'work': large}), tmp_path, settings)
    assert await service.start()
    reader, writer = await asyncio.open_unix_connection(service.socket_path, limit=1024)
    try:
        writer.write(encode_request('stalled'))
        await writer.drain()
        await started.wait()
        async with asyncio.timeout(2):
            while service._clients:
                await asyncio.sleep(0.01)
        assert await reader.read(1) == b'{'  # The client did not consume bytes until its slot closed.
    finally:
        writer.transport.abort()
        await service.stop()


@pytest.mark.asyncio
async def test_work_rows_validate_without_silent_truncation():
    result = await SnapshotCollector({'work': measured}).collect()
    row = {'id': 'local-only', 'state': 'queued', 'type': 'main', 'agent': None, 'parent': None,
           'harness': 'scripted', 'platform': 'loopback', 'workspace': 'W1', 'conversation': 'C1',
           'started_at': None, 'model': None, 'tokens_in': None, 'tokens_out': None}
    result['work']['rows'] = [row]
    assert validate_snapshot(result)['work']['rows'] == [row]
    row['state'] = 'running'
    with pytest.raises(ControlError, match='timestamp for running'):
        validate_snapshot(result)
    row['state'] = 'queued'
    row['tokens_in'] = -1
    with pytest.raises(ControlError, match='tokens_in'):
        validate_snapshot(result)
    row['tokens_in'] = None
    row['started_at'] = utc_now()
    with pytest.raises(ControlError, match='null for queued'):
        validate_snapshot(result)
    row['started_at'] = None
    result['work']['rows'].append(dict(row))
    with pytest.raises(ControlError, match='unique'):
        validate_snapshot(result)


async def test_subagent_rows_require_a_main_parent():
    result = await SnapshotCollector({'work': measured}).collect()
    parent = {'id': 'turn', 'state': 'running', 'type': 'main', 'agent': None, 'parent': None,
              'harness': 'scripted', 'platform': None, 'workspace': None, 'conversation': None,
              'started_at': utc_now(), 'model': None, 'tokens_in': None, 'tokens_out': None}
    child = {**parent, 'id': 'child', 'state': 'completed', 'type': 'subagent',
             'agent': 'research', 'parent': 'turn'}
    result['work']['rows'] = [child, parent]
    assert validate_snapshot(result)['work']['rows'] == [child, parent]
    child['parent'] = 'missing'
    with pytest.raises(ControlError, match='rows.parent'):
        validate_snapshot(result)
    child['parent'] = 'turn'
    child['agent'] = 'invalid\nname'
    with pytest.raises(ControlError, match='rows.agent'):
        validate_snapshot(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['platform', 'workspace', 'conversation'])
@pytest.mark.parametrize('value', ['', 'bad\nvalue', 42, False])
async def test_work_row_origins_reject_invalid_values(field, value):
    result = await SnapshotCollector({'work': measured}).collect()
    row = {'id': 'local-only', 'state': 'queued', 'type': 'main', 'agent': None, 'parent': None,
           'harness': 'scripted',
           'platform': None, 'workspace': None, 'conversation': None, 'started_at': None,
           'model': None, 'tokens_in': None, 'tokens_out': None}
    result['work']['rows'] = [row]
    assert validate_snapshot(result)['work']['rows'] == [row]
    row[field] = value
    with pytest.raises(ControlError, match=field):
        validate_snapshot(result)


async def test_schedule_rows_validate_and_build_metadata_is_optional():
    result = await SnapshotCollector({}).collect()
    result["scheduling"] = {"id": "scheduling", "state": "ready", "reason": None,
                            "observed_at": utc_now(), "metrics": {}, "rows": [
        {"job_id": "job-123", "name": "Daily job", "state": "paused",
         "when": "every seconds=300", "next_due": None}]}
    assert validate_snapshot(result) is result
    result.pop("build")
    assert validate_snapshot(result) is result
    result["build"] = {"version": "0.0.0", "revision": None}
    assert validate_snapshot(result) is result
    result["scheduling"]["rows"][0]["prompt"] = "must never cross this boundary"
    with pytest.raises(ControlError, match="rows"):
        validate_snapshot(result)


async def test_schedule_row_rejects_invalid_state_duplicate_id_and_timestamp():
    from copy import deepcopy

    baseline = await SnapshotCollector({}).collect()
    baseline["scheduling"] = {"id": "scheduling", "state": "ready", "reason": None,
                              "observed_at": utc_now(), "metrics": {}, "rows": [
        {"job_id": "job-123", "name": "Daily job", "state": "active",
         "when": "every seconds=300", "next_due": utc_now()}]}
    for field, value in (("state", "broken"), ("next_due", "tomorrow"), ("name", "")):
        data = deepcopy(baseline)
        data["scheduling"]["rows"][0][field] = value
        with pytest.raises(ControlError, match=field):
            validate_snapshot(data)
    baseline["scheduling"]["rows"].append(deepcopy(baseline["scheduling"]["rows"][0]))
    with pytest.raises(ControlError, match="unique"):
        validate_snapshot(baseline)


def test_server_build_reads_installed_direct_url_without_git(monkeypatch):
    from belle_agent.control import service

    class Package:
        version = "1.2.3"

        def read_text(self, name):
            assert name == "direct_url.json"
            return '{"vcs_info":{"vcs":"git","commit_id":"0123456789abcdef"}}'

    monkeypatch.setattr(service, "distribution", lambda _: Package())
    assert service.server_build() == {"version": "1.2.3", "revision": "0123456789abcdef"}


def test_server_build_leaves_revision_unknown_for_non_git_install(monkeypatch):
    from belle_agent.control import service

    class Package:
        version = "0.0.0"

        def read_text(self, _):
            return '{"dir_info":{"editable":true},"url":"file:///src"}'

    monkeypatch.setattr(service, "distribution", lambda _: Package())
    assert service.server_build() == {"version": "0.0.0", "revision": None}


@pytest.mark.parametrize('failure', [None, 'unsupported', 'stale_selection', 'action_failed'])
async def test_schedule_toggle_socket_boundary(tmp_path, failure):
    from belle_agent.control.client import toggle_schedule

    calls = []

    async def toggle(job_id, expected_state):
        calls.append((job_id, expected_state))
        if failure == 'action_failed':
            raise OSError('/private/state/path must stay private')

    service = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True),
                            toggle_schedule=None if failure == 'unsupported' else toggle)
    assert await service.start()
    try:
        sample = await get_snapshot(service.socket_path)
        instance = 'old-instance' if failure == 'stale_selection' else sample['instance_id']
        if failure:
            with pytest.raises(ControlError) as error:
                await toggle_schedule(service.socket_path, 'job-1234567890', 'active', instance)
            assert error.value.code == failure
            assert '/private' not in error.value.message
        else:
            assert (await toggle_schedule(service.socket_path, 'job-1234567890', 'active', instance))['instance_id'] == instance
        assert calls == ([] if failure in {'unsupported', 'stale_selection'} else [('job-1234567890', 'active')])
    finally:
        await service.stop()


@pytest.mark.parametrize('params', [None, {}, {'job_id': '', 'expected_state': 'active', 'instance_id': 'x'},
                                     {'job_id': 'job-1234567890', 'expected_state': 'ended', 'instance_id': 'x'},
                                     {'job_id': 'job-1234567890', 'expected_state': 'active', 'instance_id': 'x', 'enabled': True}])
def test_schedule_toggle_validates_parameters(params):
    from belle_agent.control.protocol import validate_request

    with pytest.raises(ControlError, match='request.params'):
        validate_request({'version': 4, 'id': 'x', 'method': 'schedule.toggle', 'params': params})


@pytest.mark.parametrize('method,state,key', [('work.stop', 'running', 'work_id'),
                                              ('work.drop', 'queued', 'work_id'),
                                              ('schedule.delete', 'ended', 'job_id')])
@pytest.mark.parametrize('failure', [None, 'unsupported', 'stale_selection', 'action_failed'])
async def test_operator_actions_socket_boundary(tmp_path, method, state, key, failure):
    from belle_agent.control.client import _request
    from belle_agent.control.protocol import MAX_REQUEST_BYTES, validate_request

    calls = []

    async def action(identity, expected):
        calls.append((identity, expected))
        if failure == 'action_failed':
            raise OSError('/private/store')

    callback = {'work.stop': 'stop_work', 'work.drop': 'drop_work', 'schedule.delete': 'delete_schedule'}[method]
    service = StatusService(SnapshotCollector({}), tmp_path, ControlSettings(enabled=True),
                            **{callback: None if failure == 'unsupported' else action})
    assert await service.start()
    try:
        instance = 'old' if failure == 'stale_selection' else service.collector.instance_id
        request = encode_line(validate_request({'version': 4, 'id': 'action', 'method': method,
                              'params': {key: 'selected', 'expected_state': state, 'instance_id': instance}}),
                              MAX_REQUEST_BYTES)
        if failure:
            with pytest.raises(ControlError) as error:
                await _request(service.socket_path, request, 'action', 2)
            assert error.value.code == failure
            assert '/private' not in error.value.message
        else:
            result = await _request(service.socket_path, request, 'action', 2)
            assert result['instance_id'] == instance
        assert calls == ([] if failure in {'unsupported', 'stale_selection'} else [('selected', state)])
    finally:
        await service.stop()


@pytest.mark.parametrize('method,state,key', [('work.stop', 'queued', 'work_id'),
                                              ('work.drop', 'running', 'work_id'),
                                              ('schedule.delete', 'running', 'job_id')])
def test_operator_actions_reject_wrong_selected_state(method, state, key):
    from belle_agent.control.protocol import validate_request

    with pytest.raises(ControlError, match='expected_state'):
        validate_request({'version': 4, 'id': 'x', 'method': method,
                          'params': {key: 'selected', 'expected_state': state, 'instance_id': 'instance'}})
