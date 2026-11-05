"""Private Discord threads keep their audience separate at host boundaries."""

import pytest
from conformance_kit import ModelTurn
from discord_kit import DiscordKit
from scripted_backend import ScriptedBackend

from belle_agent.platforms.base import InboundEvent


@pytest.fixture
def kit():
    return ScriptedBackend()


@pytest.fixture
def platform_kit(monkeypatch):
    return DiscordKit(monkeypatch)


def private_threads(platform_kit):
    for thread in ('T-PRIVATE', 'T-OTHER'):
        platform_kit.rest.channels[thread] = {
            'id': thread, 'type': 12, 'guild_id': 'W1', 'parent_id': 'C1',
        }


def settings(session='channel', events='context'):
    return f"""
scopes:
  - match: {{channel: loopback}}
    delivery: {{mode: [all], session: {session}, reply: optional, events: {{reaction: {events}}}}}
"""


@pytest.mark.parametrize('session', ['channel', 'thread'])
async def test_private_sessions_retain_only_their_own_history(build_world, platform_kit, session):
    private_threads(platform_kit)
    world = await build_world(settings(session) + """
  - match: {channel: loopback, chat: T-PRIVATE}
    agent: {model_name: other-model}
""")
    world.kit.play(ModelTurn(final='private-secret'), ModelTurn(), ModelTurn(), ModelTurn(), ModelTurn())
    for text, thread, sender in (
        ('private one', 'T-PRIVATE', 'U1'), ('public', '', 'U1'),
        ('public thread', 'T-PUBLIC', 'U1'), ('other private', 'T-OTHER', 'U1'),
        ('private two', 'T-PRIVATE', 'U2'),
    ):
        await world.send(world.message(text, thread=thread, sender=sender))

    first, public, public_thread, other, again = world.requests
    assert first.conversation == again.conversation
    assert first.conversation not in {public.conversation, public_thread.conversation, other.conversation}
    assert (public.conversation == public_thread.conversation) is (session == 'channel')
    assert first.scope.model == again.scope.model == 'other-model'
    assert public.scope.model == ''
    assert first.metadata['belle_place']['conversation'] == 'T-PRIVATE'
    assert first.metadata['belle_place']['thread'] == 'T-PRIVATE'
    for request in (public, public_thread, other):
        history = await world.kit.peek_history(request.conversation, request.request_id)
        assert 'private-secret' not in ''.join(part.text for part in history)
    history = await world.kit.peek_history(again.conversation, again.request_id)
    assert 'private-secret' in ''.join(part.text for part in history)


@pytest.mark.parametrize('disposition', ['context', 'turn'])
async def test_private_reaction_context_and_turns_stay_in_the_private_conversation(
    build_world, platform_kit, disposition,
):
    private_threads(platform_kit)
    world = await build_world(settings(events=disposition))
    event = InboundEvent(
        platform='discord', workspace='W1', conversation='C1', kind='reaction', actor='PRIVATE-ACTOR',
        anchor='private-anchor', anchor_thread='T-PRIVATE', event_id='reaction-private',
    )
    outcome = await world.send(event)
    assert outcome.startswith('dispatched:event:reaction' if disposition == 'turn' else 'buffered:reaction')
    for thread in ('', 'T-PUBLIC', 'T-OTHER', 'T-PRIVATE'):
        await world.send(world.message('next message', thread=thread))

    public, public_thread, other, private = world.requests[-4:]
    assert all('PRIVATE-ACTOR' not in request.text for request in (public, public_thread, other))
    if disposition == 'context':
        assert 'PRIVATE-ACTOR' in private.text
    else:
        event_request = world.requests[0]
        assert 'PRIVATE-ACTOR' in event_request.text
        assert event_request.conversation == private.conversation != public.conversation
        assert event_request.metadata['belle_place']['conversation'] == 'T-PRIVATE'


async def test_commands_cannot_read_or_stop_a_private_queue_from_the_parent(build_world, platform_kit):
    private_threads(platform_kit)
    world = await build_world(settings())
    world.kit.play(ModelTurn(hold=True))
    first = world.message('private running', thread='T-PRIVATE')
    await world.platform.inject(first)
    await world.kit.holding()
    await world.platform.inject(world.message('private-queue-secret', thread='T-PRIVATE'))
    commands = world.host.platforms['discord'].commands

    async def command(channel, text):
        return await commands._command({
            'id': 'command', 'channel_id': channel, 'guild_id': 'W1',
            'member': {'user': {'id': 'U1'}},
        }, text)

    parent_queue, _ = await command('C1', 'queue list')
    private_queue, _ = await command('T-PRIVATE', 'queue list')
    assert 'private-queue-secret' not in parent_queue
    assert 'private-queue-secret' in private_queue
    assert (await command('C1', 'stop'))[1] == 'ignored:no-turn-running'
    assert (await command('T-PRIVATE', 'stop'))[1].startswith('stopped conversation=')
    await world.host.idle()
    assert len(world.kit.stopped) == 1


async def test_parent_allowlist_does_not_admit_private_thread_messages_or_reactions(build_world, platform_kit):
    private_threads(platform_kit)
    world = await build_world('platforms: {discord: {allowed_channel_ids: [C1]}}\n' + settings(events='turn'))
    outcome = await world.send(world.message('private', thread='T-PRIVATE'))
    assert outcome == 'ignored:channel-not-in-allowed_channel_ids'
    outcome = await world.send(InboundEvent(
        platform='discord', workspace='W1', conversation='C1', kind='reaction', actor='U1',
        anchor='private-anchor', anchor_thread='T-PRIVATE', event_id='private-reaction',
    ))
    assert outcome == 'ignored:channel-not-in-allowed_channel_ids'
    assert not world.requests
    assert (await world.send(world.message('public', thread='T-PUBLIC'))).startswith('dispatched:')
