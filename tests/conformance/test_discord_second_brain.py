"""Discord ingestion requires consent for each conversation audience."""

import pytest
from conformance_kit import as_file, scripted_second_brain
from discord_kit import DiscordKit
from scripted_backend import ScriptedBackend

from belle_agent.platforms.base import InboundEvent


@pytest.fixture
def kit():
    return ScriptedBackend()


@pytest.fixture
def platform_kit(monkeypatch):
    return DiscordKit(monkeypatch)


def settings(tmp_path, channels):
    # One-document BM25 ranks are near zero; this test checks delivery, not relevance tuning.
    return f"""
second_brain:
  enabled: true
  corpus_dir: {tmp_path / 'corpus'}
  ingest: {{channels: {channels}}}
  retrieval: {{min_score: 0.00000001}}
scopes:
  - match: {{channel: loopback, chat: C1}}
    delivery: {{mode: [all]}}
"""


@pytest.mark.parametrize('kind,parent,overwrites', [
    (0, '', []),
    (0, '', [{'id': 'W1', 'type': 0, 'deny': '1024', 'allow': '0'}]),
    (11, 'C-PUBLIC', []),
    (11, 'C-PRIVATE', []),
    (12, 'C-PUBLIC', []),
])
async def test_default_never_guesses_public_from_type_or_bot_access(build_world, platform_kit, tmp_path, kind, parent, overwrites):
    world = await build_world(settings(tmp_path, '[public]'), second_brain=scripted_second_brain())
    platform_kit.rest.channels['C-DOCS'] = {
        'id': 'C-DOCS', 'guild_id': 'W1', 'type': kind,
        'parent_id': parent, 'permission_overwrites': overwrites,
        'thread_metadata': {'archived': True},
    }
    assert await world.send(world.message('shared', conversation='C-DOCS', files=(as_file('kite-bridle.md'),))) == 'ignored:no-trigger-matched'
    assert not any('/attachments/' in request.path for request in platform_kit.rest.requests)
    assert not world.platform._platform.public_chat_types


async def test_explicit_parent_ingest_recall_and_restart(build_world, platform_kit, tmp_path):
    config = settings(tmp_path, '[C-DOCS, D-U1]') + '\nplatforms: {discord: {brain_react_pending: 📖, brain_react_success: 🧠}}\n'
    world = await build_world(config, second_brain=scripted_second_brain())
    platform_kit.rest.channels['T-PUBLIC'] = {
        'id': 'T-PUBLIC', 'type': 11, 'guild_id': 'W1', 'parent_id': 'C-DOCS',
        'thread_metadata': {'archived': True},
    }
    assert await world.send(world.message('shared', conversation='C-DOCS', thread='T-PUBLIC', files=(as_file('kite-bridle.md'),))) == 'ingested:second-brain'
    marks = [action for action in world.platform.log if action.kind == 'reaction']
    assert [(mark.name, mark.removed) for mark in marks] == [('📖', False), ('📖', True), ('🧠', False)]
    assert await world.send(world.message('private', conversation='C-PRIVATE', files=(as_file('hive-inspection.md'),))) == 'ignored:no-trigger-matched'
    await world.send(world.direct('personal', files=(as_file('harbour-tides.md'),)))
    listing = await world.host._second_brain.toolkit.brain_list()
    assert '1 document(s)' in listing and 'hive-inspection' not in listing and 'harbour-tides' not in listing
    await world.send(world.message('why does a delta kite stall when the bridle moves?', conversation='C1'))
    recalled = '\n'.join(world.requests[-1].scope.system_additions)
    assert 'kite-bridle-' in recalled and 'A delta kite stalls' in recalled
    assert 'hive-inspection-' not in recalled and 'harbour-tides-' not in recalled
    assert not any('/attachments/C-PRIVATE/' in request.path for request in platform_kit.rest.requests)

    await world.host.stop()
    restarted = await build_world(config, second_brain=scripted_second_brain())
    await restarted.send(restarted.message('why does a delta kite stall when the bridle moves?', conversation='C1'))
    recalled = '\n'.join(restarted.requests[-1].scope.system_additions)
    assert 'kite-bridle-' in recalled and 'hive-inspection-' not in recalled and 'harbour-tides-' not in recalled


@pytest.mark.parametrize('channels,expected', [('[C-DOCS]', False), ('[T-PRIVATE]', True)])
async def test_private_thread_ingest_requires_its_own_id_and_reactions_do_not_ingest(
    build_world, platform_kit, tmp_path, channels, expected,
):
    world = await build_world(settings(tmp_path, channels), second_brain=scripted_second_brain())
    platform_kit.rest.channels['T-PRIVATE'] = {
        'id': 'T-PRIVATE', 'type': 12, 'guild_id': 'W1', 'parent_id': 'C-DOCS',
    }
    await world.send(InboundEvent(
        platform='discord', workspace='W1', conversation='C-DOCS',
        kind='reaction', actor='U1', record={}, event_id='reaction-1',
        anchor='anchor', anchor_thread='T-PRIVATE',
    ))
    assert not any('/attachments/' in request.path for request in platform_kit.rest.requests)
    assert await world.host._second_brain.toolkit.brain_list() == 'The second brain is empty.'

    outcome = await world.send(world.message(
        'shared', conversation='C-DOCS', thread='T-PRIVATE', files=(as_file('kite-bridle.md'),),
    ))
    assert outcome == ('ingested:second-brain' if expected else 'ignored:no-trigger-matched')
    assert any('/attachments/' in request.path for request in platform_kit.rest.requests) is expected
    listing = await world.host._second_brain.toolkit.brain_list()
    assert ('1 document(s)' in listing) is expected
