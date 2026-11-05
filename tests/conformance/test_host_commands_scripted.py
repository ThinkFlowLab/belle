"""The host's command operations, on what only the scripted backend can be told to do."""

import asyncio

import pytest
import pytest_asyncio
from conformance_kit import WORKSPACE, ModelTurn, worlds
from scripted_backend import ScriptedBackend

from belle_agent.backend import SCOPE_FIELDS, BackendCapabilities, Catalog
from belle_agent.platforms.base import MARK_DROPPED, MARK_QUEUED, InboundDrop


@pytest_asyncio.fixture
async def build_world(tmp_path, request):
    backend = ScriptedBackend(getattr(request, "param", None))
    async with worlds(backend, tmp_path) as build:
        yield build


async def test_a_catalog_that_cannot_be_read_is_no_catalog_and_the_host_goes_on(build_world, caplog):
    world = await build_world()
    world.kit.listed = RuntimeError("the secret a backend error can quote")

    assert await world.host.catalog() is None
    assert "secret" not in caplog.text and "RuntimeError" in caplog.text
    # Control: the host still runs turns.
    assert (await world.send(world.direct("hello"))).startswith("dispatched")


async def test_a_catalog_that_does_not_answer_in_time_is_no_catalog(build_world, monkeypatch):
    world = await build_world()

    async def never() -> Catalog:
        await asyncio.sleep(3600)

    monkeypatch.setattr(world.kit, "catalog", never)
    assert await world.host.catalog(timeout=0.01) is None


@pytest.mark.parametrize(
    "build_world", [BackendCapabilities(backend="bare", scope_fields=SCOPE_FIELDS)], indirect=True
)
async def test_a_backend_that_lists_nothing_is_not_asked(build_world, monkeypatch):
    world = await build_world()
    monkeypatch.setattr(world.kit, "catalog", None)  # calling it would raise

    assert await world.host.catalog() is None


async def test_the_platform_is_told_that_a_removed_message_no_longer_waits(build_world):
    world = await build_world("scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n")
    world.kit.play(ModelTurn(final="first", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    waiting = world.message("two", thread="t")
    await world.platform.inject(waiting)

    drop = InboundDrop(platform="loopback", workspace=WORKSPACE, conversation="C1", message_id=waiting.message_id, sender="U1")
    assert (await world.platform.inject(drop)).startswith("removed:")
    world.kit.release()
    await world.host.idle()

    assert [state for message_id, state in world.platform.marks if message_id == waiting.message_id] == [MARK_QUEUED, MARK_DROPPED]


async def test_local_operator_drops_selected_message_and_stops_selected_turn(build_world, tmp_path):
    from belle_agent.control.protocol import ControlError

    world = await build_world("scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n")
    world.kit.play(ModelTurn(final="first", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    waiting = world.message("two", thread="t")
    await world.platform.inject(waiting)
    rows = (await world.host.status_work())['rows']
    running = next(row for row in rows if row['state'] == 'running')
    queued = next(row for row in rows if row['state'] == 'queued')
    from belle_agent.control.client import _request
    from belle_agent.control.protocol import encode_line
    from belle_agent.control.service import ControlSettings, SnapshotCollector, StatusService

    service = StatusService(SnapshotCollector({'work': world.host.status_work}), tmp_path / 'control',
                            ControlSettings(enabled=True), stop_work=world.host.stop_work,
                            drop_work=world.host.drop_work)
    assert await service.start()

    async def act(method, row):
        return await _request(service.socket_path, encode_line({
            'version': 4, 'id': 'selected', 'method': method,
            'params': {'work_id': row['id'], 'expected_state': row['state'],
                       'instance_id': service.collector.instance_id}}, 4096), 'selected', 2)

    result = await act('work.drop', queued)
    assert queued['id'] not in [row['id'] for row in result['work']['rows']]
    with pytest.raises(ControlError, match='no longer exists'):
        await world.host.drop_work(queued['id'], 'queued')
    assert [state for message_id, state in world.platform.marks if message_id == waiting.message_id] == [MARK_QUEUED, MARK_DROPPED]
    await act('work.stop', running)
    await service.stop()
    world.kit.release()
    await world.host.idle()
    with pytest.raises(ControlError, match='no longer exists'):
        await world.host.stop_work(running['id'], 'running')
