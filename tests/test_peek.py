"""Live peek buffer and control method checks."""

from pathlib import Path

import pytest

from belle_agent.backend import PeekHistoryPart
from belle_agent.control.client import peek_close, peek_open, peek_poll
from belle_agent.control.peek import PeekRegistry, validate_peek_frame
from belle_agent.control.protocol import ControlError, utc_now, validate_request
from belle_agent.control.service import ControlSettings, SnapshotCollector, StatusService


def test_registry_seeds_only_on_fresh_buffer():
    peeks = PeekRegistry(max_frames=10, max_bytes=4096, max_subscribers=2)
    seed = (PeekHistoryPart(kind='text', text='before'),)
    first = peeks.open('work-1', seed=seed)
    assert [frame['payload']['text'] for frame in first['frames']] == ['before']
    second = peeks.open('work-1', seed=(PeekHistoryPart(kind='text', text='again'),))
    assert [frame['payload']['text'] for frame in second['frames']] == ['before']


def test_registry_tees_text_only_while_subscribed():
    peeks = PeekRegistry(max_frames=10, max_bytes=4096, max_subscribers=2)
    peeks.observe_text('work-1', 'ignored')
    opened = peeks.open('work-1')
    assert opened['frames'] == []
    peeks.observe_text('work-1', 'hello')
    peeks.observe_text('work-1', 'think', kind='thinking')
    peeks.observe_text('work-1', ' world')
    polled = peeks.poll(opened['peek_id'], 0)
    assert [(frame['kind'], frame['payload'].get('text')) for frame in polled['frames']] == [
        ('text', 'hello'), ('thinking', 'think'), ('text', ' world')]
    assert polled['live'] is True
    peeks.observe_outcome(
        'work-1', 'succeeded', duration_seconds=65.2, ended_at='2026-10-20T14:32:01Z',
        extra='posted-final',
    )
    ended = peeks.poll(opened['peek_id'], polled['frames'][-1]['seq'])
    outcome = ended['frames'][-1]
    assert outcome['kind'] == 'outcome'
    assert outcome['payload'] == {
        'state': 'succeeded',
        'duration_seconds': 65.2,
        'ended_at': '2026-10-20T14:32:01Z',
        'extra': 'posted-final',
    }
    assert ended['live'] is False
    peeks.close(opened['peek_id'])
    with pytest.raises(ControlError, match='peek_ended|closed'):
        peeks.poll(opened['peek_id'], 0)


def test_peek_outcome_frame_rejects_bad_duration():
    frame = {
        'seq': 1, 'at': utc_now(), 'work_id': 'w', 'source_work_id': 'w',
        'kind': 'outcome',
        'payload': {
            'state': 'succeeded', 'duration_seconds': -1, 'ended_at': utc_now(), 'extra': None,
        },
    }
    with pytest.raises(ControlError, match='duration_seconds'):
        validate_peek_frame(frame)
    frame['payload']['duration_seconds'] = float('nan')
    with pytest.raises(ControlError, match='duration_seconds'):
        validate_peek_frame(frame)


def test_registry_evicts_old_frames_under_frame_cap():
    peeks = PeekRegistry(max_frames=2, max_bytes=4096, max_subscribers=1)
    opened = peeks.open('work-1')
    for text in ('a', 'b', 'c'):
        peeks.observe_text('work-1', text)
    frames = peeks.poll(opened['peek_id'], 0)['frames']
    assert [frame['payload']['text'] for frame in frames] == ['b', 'c']


def test_peek_request_shapes():
    validate_request({'version': 4, 'id': '1', 'method': 'peek.open',
                      'params': {'work_id': 'abc', 'instance_id': 'inst'}})
    validate_request({'version': 4, 'id': '1', 'method': 'peek.poll',
                      'params': {'peek_id': 'p1', 'after_seq': 0}})
    validate_request({'version': 4, 'id': '1', 'method': 'peek.close',
                      'params': {'peek_id': 'p1'}})
    with pytest.raises(ControlError, match='after_seq'):
        validate_request({'version': 4, 'id': '1', 'method': 'peek.poll',
                          'params': {'peek_id': 'p1', 'after_seq': -1}})


def test_peek_frame_validation():
    frame = {
        'seq': 1, 'at': utc_now(), 'work_id': 'w', 'source_work_id': 'w',
        'kind': 'text', 'payload': {'text': 'hi'},
    }
    assert validate_peek_frame(frame) == frame
    bad = dict(frame, kind='diff')
    with pytest.raises(ControlError, match='kind'):
        validate_peek_frame(bad)


def test_registry_tees_steps_tools_and_questions():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=2)
    opened = peeks.open('work-1')
    peeks.observe_step('work-1', 'Plan')
    peeks.observe_tool_call(
        'work-1', 'c1', 'shell', detail='list files', args_text='{"command":"ls"}',
    )
    peeks.observe_tool_result(
        'work-1', 'c1', ok=True, name='shell', result_text='a.txt\n',
    )
    peeks.observe_question('work-1', 'Allow shell to run?', 'approval')
    frames = peeks.poll(opened['peek_id'], 0)['frames']
    assert [(frame['kind'], frame['payload']) for frame in frames] == [
        ('step', {'label': 'Plan'}),
        (
            'tool_call',
            {
                'call_id': 'c1', 'name': 'shell', 'detail': 'list files',
                'args_text': '{"command":"ls"}',
            },
        ),
        (
            'tool_result',
            {
                'call_id': 'c1', 'name': 'shell', 'ok': True, 'summary': None,
                'result_text': 'a.txt\n',
            },
        ),
        ('question', {'prompt_summary': 'Allow shell to run?', 'kind': 'approval'}),
    ]
    for frame in frames:
        validate_peek_frame(frame)
        assert frame['source_work_id'] == 'work-1'


def test_tool_body_fields_optional_and_refuse_oversize_frame():
    peeks = PeekRegistry(max_frames=10, max_bytes=200, max_subscribers=1)
    opened = peeks.open('work-1')
    peeks.observe_tool_call('work-1', 'c1', 'shell', detail=None, args_text=None)
    frame = peeks.poll(opened['peek_id'], 0)['frames'][0]
    assert frame['payload']['args_text'] is None
    assert validate_peek_frame(frame) == frame
    huge = 'x' * 500
    with pytest.raises(ControlError, match='peek_max_bytes') as refused:
        peeks.observe_tool_call('work-1', 'c2', 'shell', args_text=huge)
    assert refused.value.code == 'message_too_large'
    bad = {
        'seq': 1, 'at': utc_now(), 'work_id': 'w', 'source_work_id': 'w',
        'kind': 'tool_call',
        'payload': {
            'call_id': 'c1', 'name': 'shell', 'detail': None, 'args_text': 'bad\x00',
        },
    }
    with pytest.raises(ControlError, match='args_text'):
        validate_peek_frame(bad)


def test_registry_filters_subagent_source_and_keeps_outcome():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=2)
    main = peeks.open('main-1')
    child = peeks.open(
        'main-1', source_filter='child-1', opened_work_id='child-1',
    )
    assert child['work_id'] == 'child-1'
    peeks.observe_text('main-1', 'parent text')
    peeks.observe_tool_call('main-1', 'c1', 'research', source_work_id='child-1')
    peeks.observe_text('main-1', 'child notes', source_work_id='child-1')
    peeks.observe_outcome('main-1', 'succeeded', duration_seconds=1.0, ended_at='2026-10-20T14:32:01Z')
    parent_kinds = [frame['kind'] for frame in peeks.poll(main['peek_id'], 0)['frames']]
    assert parent_kinds == ['text', 'tool_call', 'text', 'outcome']
    filtered = peeks.poll(child['peek_id'], 0)['frames']
    assert [(frame['kind'], frame['source_work_id']) for frame in filtered] == [
        ('tool_call', 'child-1'),
        ('text', 'child-1'),
        ('outcome', 'main-1'),
    ]


def test_peek_tool_helpers_use_description_only():
    from belle_agent.backend import EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent
    from belle_agent.progress import (
        MAX_PEEK_TOOL_BODY_BYTES,
        peek_question_text,
        peek_tool_args_text,
        peek_tool_called_fields,
        peek_tool_result_fields,
        peek_tool_result_text,
    )

    called = TurnEvent(
        EVENT_TOOL_CALLED,
        data={
            "call_id": "c1",
            "name": "shell",
            "arguments": {
                "description": "list files",
                "command": "ls",
                "path": "/tmp/secret/dir/file.txt",
                "token": "super-secret",
            },
        },
    )
    assert peek_tool_called_fields(called) == ("c1", "shell", "list files")
    args_text = peek_tool_args_text(called)
    assert args_text is not None
    assert "list files" in args_text and "ls" in args_text
    assert "/tmp/" not in args_text
    assert "file.txt" in args_text
    assert "super-secret" not in args_text
    assert "[redacted]" in args_text
    # Oversize body is omitted whole — never mid-string trimmed.
    fat = TurnEvent(
        EVENT_TOOL_CALLED,
        data={"call_id": "c2", "name": "shell", "arguments": {"blob": "y" * (MAX_PEEK_TOOL_BODY_BYTES + 8)}},
    )
    assert peek_tool_args_text(fat) is None
    result = TurnEvent(
        EVENT_TOOL_RESULT,
        text='{"ok": false, "error": "denied", "path": "/var/log/app.log"}',
        data={"call_id": "c1", "name": "shell", "failed": True},
    )
    call_id, name, ok, summary = peek_tool_result_fields(result)
    assert (call_id, name, ok) == ("c1", "shell", False)
    assert summary is not None and "denied" in summary
    result_text = peek_tool_result_text(result)
    assert result_text is not None and "denied" in result_text
    assert "/var/" not in result_text and "app.log" in result_text
    assert "\n" not in peek_question_text("Line one\nLine two\x00")


def test_peek_tool_and_question_frames_reject_control_chars():
    base = {
        'seq': 1, 'at': utc_now(), 'work_id': 'w', 'source_work_id': 'w',
        'kind': 'tool_call',
        'payload': {'call_id': 'c1', 'name': 'shell', 'detail': 'bad\ndetail'},
    }
    with pytest.raises(ControlError, match='detail'):
        validate_peek_frame(base)
    question = {
        **base,
        'kind': 'question',
        'payload': {'prompt_summary': 'line\ntwo', 'kind': 'question'},
    }
    with pytest.raises(ControlError, match='prompt_summary'):
        validate_peek_frame(question)


def test_peek_usage_frame_validation_and_observe():
    peeks = PeekRegistry(max_frames=10, max_bytes=4096, max_subscribers=1)
    opened = peeks.open('work-1')
    peeks.observe_usage('work-1')  # nothing to report: no frame
    peeks.observe_usage('work-1', tokens_in=10, tokens_out=4, cost=0.25)
    polled = peeks.poll(opened['peek_id'], 0)
    assert len(polled['frames']) == 1
    frame = polled['frames'][0]
    assert frame['kind'] == 'usage'
    assert frame['payload'] == {'tokens_in': 10, 'tokens_out': 4, 'cost': 0.25}
    assert validate_peek_frame(frame) == frame
    bad = dict(frame, payload={'tokens_in': -1, 'tokens_out': None, 'cost': None})
    with pytest.raises(ControlError, match='tokens_in'):
        validate_peek_frame(bad)


@pytest.mark.asyncio
async def test_status_service_peek_disabled_and_open(tmp_path: Path):
    runtime = tmp_path / 'run'
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    peeks = PeekRegistry()

    async def measured():
        return {'id': 'work', 'state': 'ready', 'reason': None, 'observed_at': utc_now(),
                'metrics': {'queued_messages': {'value': 0, 'reason': None},
                            'active_turns': {'value': 0, 'reason': None},
                            'queued_events': {'value': 0, 'reason': None}}}

    async def open_work(work_id: str):
        return peeks.open(work_id)

    async def poll_work(peek_id: str, after_seq: int):
        return peeks.poll(peek_id, after_seq)

    async def close_work(peek_id: str):
        peeks.close(peek_id)

    off = StatusService(SnapshotCollector({'work': measured}), runtime,
                        ControlSettings(enabled=True, peek=False),
                        peek_open=open_work, peek_poll=poll_work, peek_close=close_work)
    assert await off.start()
    try:
        with pytest.raises(ControlError, match='control.peek') as refused:
            await peek_open(off.socket_path, 'work-1', off.collector.instance_id)
        assert refused.value.code == 'peek_disabled'
    finally:
        await off.stop()

    on = StatusService(SnapshotCollector({'work': measured}), runtime,
                       ControlSettings(enabled=True, peek=True),
                       peek_open=open_work, peek_poll=poll_work, peek_close=close_work)
    assert await on.start()
    try:
        opened = await peek_open(on.socket_path, 'work-1', on.collector.instance_id)
        peeks.observe_text('work-1', 'streamed')
        polled = await peek_poll(on.socket_path, opened['peek_id'], 0)
        assert polled['frames'][0]['payload']['text'] == 'streamed'
        await peek_close(on.socket_path, opened['peek_id'])
    finally:
        await on.stop()


def test_control_settings_accept_peek_keys():
    settings = ControlSettings.from_config({
        'control': {
            'enabled': True, 'peek': True, 'peek_max_frames': 10,
            'peek_subscriber_idle_seconds': 12.5,
        },
    })
    assert settings.peek is True
    assert settings.peek_max_frames == 10
    assert settings.peek_subscriber_idle_seconds == 12.5
    with pytest.raises(ValueError, match='peek_max'):
        ControlSettings.from_config({'control': {'enabled': True, 'mystery': True}})
    with pytest.raises(ValueError, match='peek_subscriber_idle_seconds'):
        ControlSettings.from_config({
            'control': {'enabled': True, 'peek_subscriber_idle_seconds': 0},
        })


def test_registry_splits_oversized_text_instead_of_refusing():
    peeks = PeekRegistry(max_frames=50, max_bytes=200, max_subscribers=1)
    opened = peeks.open('work-1')
    peeks.observe_text('work-1', 'x' * 500)
    frames = peeks.poll(opened['peek_id'], 0)['frames']
    assert frames
    assert all(frame['kind'] == 'text' for frame in frames)
    assert ''.join(frame['payload']['text'] for frame in frames).count('x') >= 1


def test_registry_open_reports_lost_from_seed_eviction():
    from belle_agent.backend import PeekHistoryPart

    peeks = PeekRegistry(max_frames=2, max_bytes=4096, max_subscribers=1)
    seed = tuple(PeekHistoryPart(kind='text', text=letter) for letter in ('a', 'b', 'c'))
    opened = peeks.open('work-1', seed=seed)
    assert opened['lost'] == 1
    assert [frame['payload']['text'] for frame in opened['frames']] == ['b', 'c']


def test_registry_seed_prepends_before_live_frames():
    from belle_agent.backend import PeekHistoryPart

    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=2)
    peeks.ensure_buffer('work-1')
    peeks.observe_text('work-1', 'live')
    seed = (PeekHistoryPart(kind='text', text='early'),)
    opened = peeks.open('work-1', seed=seed)
    assert [frame['payload']['text'] for frame in opened['frames']] == ['early', 'live']
    # Second open must not re-seed.
    again = peeks.open('work-1', seed=(PeekHistoryPart(kind='text', text='again'),))
    assert [frame['payload']['text'] for frame in again['frames']] == ['early', 'live']


def test_registry_empty_seed_without_seed_done_does_not_block_later_seed():
    from belle_agent.backend import PeekHistoryPart

    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=2)
    peeks.ensure_buffer('work-1')
    peeks.observe_text('work-1', 'live')
    peeks.open('work-1', seed=())
    assert peeks._buffers['work-1'].seeded is False
    opened = peeks.open('work-1', seed=(PeekHistoryPart(kind='text', text='early'),))
    assert [frame['payload']['text'] for frame in opened['frames']] == ['early', 'live']


def test_registry_seed_after_mark_ended_keeps_history():
    from belle_agent.backend import PeekHistoryPart

    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=1)
    peeks.ensure_buffer('work-1')
    peeks.observe_text('work-1', 'live')
    peeks.mark_ended('work-1')
    opened = peeks.open(
        'work-1', seed=(PeekHistoryPart(kind='text', text='early'),), seed_done=True,
    )
    assert [frame['payload']['text'] for frame in opened['frames']] == ['early', 'live']
    assert opened['live'] is False


def test_registry_lost_zero_when_subscriber_keeps_up():
    peeks = PeekRegistry(max_frames=3, max_bytes=4096, max_subscribers=1)
    opened = peeks.open('work-1')
    after = 0
    for letter in 'abcdef':
        peeks.observe_text('work-1', letter)
        polled = peeks.poll(opened['peek_id'], after)
        assert polled['lost'] == 0
        assert [frame['payload']['text'] for frame in polled['frames']] == [letter]
        after = polled['frames'][-1]['seq']


def test_registry_discard_if_unsubscribed_drops_orphan():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=1)
    peeks.ensure_buffer('work-1')
    peeks.observe_text('work-1', 'x')
    peeks.discard_if_unsubscribed('work-1')
    assert peeks.has_buffer('work-1') is False


def test_registry_refuses_unknown_seed_kind():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=1)
    peeks.ensure_buffer('work-1')
    peeks.observe_text('work-1', 'live')
    with pytest.raises(ControlError, match='seed.kind'):
        peeks.open(
            'work-1',
            seed=(
                PeekHistoryPart(kind='text', text='ok'),
                PeekHistoryPart(kind='step', text='bad'),
            ),
        )
    assert peeks.subscriber_count() == 0
    assert [frame['payload']['text'] for frame in peeks._buffers['work-1'].frames] == ['live']


def test_registry_refuses_non_utf8_text():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=1)
    opened = peeks.open('work-1')
    with pytest.raises(ControlError, match='UTF-8'):
        peeks.observe_text('work-1', 'a\ud800b')
    assert peeks.poll(opened['peek_id'], 0)['frames'] == []


def test_registry_seed_utf8_names_seed_text_field():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=1)
    with pytest.raises(ControlError, match='seed.text') as caught:
        peeks.open('work-1', seed=(PeekHistoryPart(kind='text', text='a\ud800b'),))
    assert caught.value.code == 'invalid_request'
    assert peeks.subscriber_count() == 0


def test_registry_tool_utf8_names_field():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=1)
    peeks.open('work-1')
    with pytest.raises(ControlError, match='name') as caught:
        peeks.observe_tool_call('work-1', 'c1', 'a\ud800b')
    assert caught.value.code == 'invalid_request'
    with pytest.raises(ControlError, match='args_text') as caught_body:
        peeks.observe_tool_call('work-1', 'c1', 'shell', args_text='a\ud800b')
    assert caught_body.value.code == 'invalid_request'


def test_registry_reaps_idle_subscribers():
    peeks = PeekRegistry(
        max_frames=20, max_bytes=4096, max_subscribers=1, subscriber_idle=0.01,
    )
    opened = peeks.open('work-1')
    old_peek_id = opened['peek_id']
    peeks._peeks[old_peek_id].last_active -= 1.0
    peeks.require_subscriber_slot()
    assert peeks.subscriber_count() == 0
    assert not peeks.has_buffer('work-1')
    with pytest.raises(ControlError, match='closed or unknown') as caught:
        peeks.poll(old_peek_id, 0)
    assert caught.value.code == 'peek_ended'
    again = peeks.open('work-2')
    assert again['peek_id'] != old_peek_id


def test_registry_refresh_open_includes_new_frames():
    peeks = PeekRegistry(max_frames=20, max_bytes=4096, max_subscribers=2)
    opened = peeks.open('work-1')
    peeks.observe_text('work-1', 'before')
    first = peeks.refresh_open(opened['peek_id'])
    assert [frame['payload']['text'] for frame in first['frames']] == ['before']
    peeks.observe_text('work-1', 'after')
    second = peeks.refresh_open(opened['peek_id'])
    assert [frame['payload']['text'] for frame in second['frames']] == ['before', 'after']
    assert peeks.poll(opened['peek_id'], second['frames'][-1]['seq'])['frames'] == []


def test_registry_text_split_raises_before_partial_append():
    peeks = PeekRegistry(max_frames=20, max_bytes=96 + 3, max_subscribers=1)
    opened = peeks.open('work-1')
    with pytest.raises(ControlError, match='peek frame is') as caught:
        peeks.observe_text('work-1', 'aaaaaa😀')
    assert caught.value.code == 'message_too_large'
    assert not peeks._buffers['work-1'].frames
    assert peeks.poll(opened['peek_id'], 0)['lost'] == 0


def test_registry_reports_lost_on_eviction_including_filtered():
    peeks = PeekRegistry(max_frames=2, max_bytes=4096, max_subscribers=2)
    main = peeks.open('main-1')
    child = peeks.open('main-1', source_filter='child-1', opened_work_id='child-1')
    peeks.observe_text('main-1', 'parent-a')
    peeks.observe_text('main-1', 'child-a', source_work_id='child-1')
    peeks.observe_text('main-1', 'child-b', source_work_id='child-1')
    peeks.observe_text('main-1', 'child-c', source_work_id='child-1')
    # Ring keeps child-b/child-c. Main lost parent-a+child-a; child filter lost child-a only.
    main_poll = peeks.poll(main['peek_id'], 0)
    assert main_poll['lost'] == 2
    child_poll = peeks.poll(child['peek_id'], 0)
    assert child_poll['lost'] == 1
    assert [frame['payload']['text'] for frame in child_poll['frames']] == ['child-b', 'child-c']


def test_peek_tool_result_plain_path_is_basenamed():
    from belle_agent.backend import EVENT_TOOL_RESULT, TurnEvent
    from belle_agent.progress import peek_tool_result_text

    plain = TurnEvent(
        EVENT_TOOL_RESULT,
        text='/home/example/.config/app/private.json',
        data={'call_id': 'c1', 'name': 'read'},
    )
    result_text = peek_tool_result_text(plain)
    assert result_text == 'private.json'
    assert '/home/' not in (result_text or '')
