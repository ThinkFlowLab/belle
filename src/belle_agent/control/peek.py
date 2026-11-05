"""Ephemeral live-peek buffers for the control socket.

Peek content is operator-facing turn output. ``snapshot.get`` stays content-free.
A buffer may be reserved before the first subscriber for ``peek_history`` catch-up,
then discarded when the last subscriber leaves (no grace). Frame and subscriber
caps are operator resource limits from control settings.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast
from uuid import uuid4

from belle_agent.backend import PeekHistoryPart
from belle_agent.control.protocol import ControlError, _fail, _keys, _text, _timestamp, utc_now

PEEK_KIND_TEXT = 'text'
PEEK_KIND_THINKING = 'thinking'
PEEK_KIND_STEP = 'step'
PEEK_KIND_TOOL_CALL = 'tool_call'
PEEK_KIND_TOOL_RESULT = 'tool_result'
PEEK_KIND_QUESTION = 'question'
PEEK_KIND_USAGE = 'usage'
PEEK_KIND_OUTCOME = 'outcome'
#: Defaults shared by PeekRegistry and ControlSettings.
PEEK_DEFAULT_MAX_FRAMES = 200
PEEK_DEFAULT_MAX_BYTES = 65536
PEEK_DEFAULT_MAX_SUBSCRIBERS = 4
#: Drop a peek subscriber idle this long (no open/poll). Protects slot budget.
PEEK_DEFAULT_SUBSCRIBER_IDLE = 300.0
PEEK_KINDS = frozenset({
    PEEK_KIND_TEXT, PEEK_KIND_THINKING, PEEK_KIND_STEP, PEEK_KIND_TOOL_CALL,
    PEEK_KIND_TOOL_RESULT, PEEK_KIND_QUESTION, PEEK_KIND_USAGE, PEEK_KIND_OUTCOME,
})
_OUTCOME_STATES = frozenset({'succeeded', 'failed', 'interrupted'})
_TEXT_KINDS = frozenset({PEEK_KIND_TEXT, PEEK_KIND_THINKING})


class PeekFrame(TypedDict):
    seq: int
    at: str
    work_id: str
    source_work_id: str
    kind: str
    payload: dict[str, Any]


class PeekOpenResult(TypedDict):
    peek_id: str
    work_id: str
    live: bool
    frames: list[PeekFrame]
    #: Matching frames dropped by ring eviction before this open/poll delivered them.
    lost: int


class PeekPollResult(TypedDict):
    peek_id: str
    work_id: str
    live: bool
    frames: list[PeekFrame]
    lost: int


def _utf8_str(value: str, field: str) -> None:
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        _fail(field, 'a UTF-8 string')


def _optional_plain(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        _fail(field, 'null or a string without control characters')
    _utf8_str(value, field)
    return value


def _optional_body(value: Any, field: str) -> str | None:
    """Optional multiline tool body: null, or a string allowing tab/newline only among C0 controls."""
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(char) < 32 and char not in '\t\n' for char in value):
        _fail(field, 'null or a string without control characters (tab and newline allowed)')
    _utf8_str(value, field)
    return value


def validate_peek_frame(value: Any, field: str = 'frame') -> PeekFrame:
    """Validate one peek frame without rewriting it."""
    entry = _keys(value, {'seq', 'at', 'work_id', 'source_work_id', 'kind', 'payload'}, field)
    if type(entry['seq']) is not int or entry['seq'] < 1:
        _fail(f'{field}.seq', 'a positive integer')
    _timestamp(entry['at'], f'{field}.at')
    _text(entry['work_id'], f'{field}.work_id')
    _text(entry['source_work_id'], f'{field}.source_work_id')
    if entry['kind'] not in PEEK_KINDS:
        _fail(f'{field}.kind', f'one of {", ".join(sorted(PEEK_KINDS))}')
    if not isinstance(entry['payload'], dict):
        _fail(f'{field}.payload', 'an object')
    kind = entry['kind']
    if kind in _TEXT_KINDS:
        payload = _keys(entry['payload'], {'text'}, f'{field}.payload')
        if not isinstance(payload['text'], str):
            _fail(f'{field}.payload.text', 'a string')
    elif kind == PEEK_KIND_STEP:
        payload = _keys(entry['payload'], {'label'}, f'{field}.payload')
        _optional_plain(payload['label'], f'{field}.payload.label')
    elif kind == PEEK_KIND_TOOL_CALL:
        payload = _keys(
            entry['payload'], {'call_id', 'name', 'detail', 'args_text'}, f'{field}.payload',
        )
        _text(payload['call_id'], f'{field}.payload.call_id')
        _text(payload['name'], f'{field}.payload.name')
        _optional_plain(payload['detail'], f'{field}.payload.detail')
        _optional_body(payload['args_text'], f'{field}.payload.args_text')
    elif kind == PEEK_KIND_TOOL_RESULT:
        payload = _keys(
            entry['payload'], {'call_id', 'name', 'ok', 'summary', 'result_text'}, f'{field}.payload',
        )
        _text(payload['call_id'], f'{field}.payload.call_id')
        _optional_plain(payload['name'], f'{field}.payload.name')
        if type(payload['ok']) is not bool:
            _fail(f'{field}.payload.ok', 'a boolean')
        _optional_plain(payload['summary'], f'{field}.payload.summary')
        _optional_body(payload['result_text'], f'{field}.payload.result_text')
    elif kind == PEEK_KIND_QUESTION:
        payload = _keys(entry['payload'], {'prompt_summary', 'kind'}, f'{field}.payload')
        _text(payload['prompt_summary'], f'{field}.payload.prompt_summary')
        if any(ord(char) < 32 for char in payload['prompt_summary']):
            _fail(f'{field}.payload.prompt_summary', 'a string without control characters')
        _text(payload['kind'], f'{field}.payload.kind')
    elif kind == PEEK_KIND_USAGE:
        payload = _keys(entry['payload'], {'tokens_in', 'tokens_out', 'cost'}, f'{field}.payload')
        _optional_nonneg_int(payload['tokens_in'], f'{field}.payload.tokens_in')
        _optional_nonneg_int(payload['tokens_out'], f'{field}.payload.tokens_out')
        _optional_nonneg_float(payload['cost'], f'{field}.payload.cost')
    elif kind == PEEK_KIND_OUTCOME:
        payload = _keys(
            entry['payload'], {'state', 'duration_seconds', 'ended_at', 'extra'}, f'{field}.payload',
        )
        if payload['state'] not in _OUTCOME_STATES:
            _fail(f'{field}.payload.state', f'one of {", ".join(sorted(_OUTCOME_STATES))}')
        duration = payload['duration_seconds']
        if duration is not None and (
            type(duration) not in (int, float) or isinstance(duration, bool)
            or not math.isfinite(duration) or duration < 0
        ):
            _fail(f'{field}.payload.duration_seconds', 'null or a nonnegative finite number')
        if payload['ended_at'] is not None:
            _timestamp(payload['ended_at'], f'{field}.payload.ended_at')
        _optional_plain(payload['extra'], f'{field}.payload.extra')
    return cast(PeekFrame, entry)


def validate_peek_open_result(value: Any) -> PeekOpenResult:
    entry = _keys(value, {'peek_id', 'work_id', 'live', 'frames', 'lost'}, 'result')
    _text(entry['peek_id'], 'result.peek_id')
    _text(entry['work_id'], 'result.work_id')
    if type(entry['live']) is not bool:
        _fail('result.live', 'a boolean')
    if type(entry['lost']) is not int or entry['lost'] < 0:
        _fail('result.lost', 'a nonnegative integer')
    if not isinstance(entry['frames'], list):
        _fail('result.frames', 'an array')
    for index, frame in enumerate(entry['frames']):
        validate_peek_frame(frame, f'result.frames[{index}]')
    return cast(PeekOpenResult, entry)


def validate_peek_poll_result(value: Any) -> PeekPollResult:
    return cast(PeekPollResult, validate_peek_open_result(value))


@dataclass
class _Buffer:
    work_id: str
    max_frames: int
    max_bytes: int
    live: bool = True
    next_seq: int = 1
    frames: deque[PeekFrame] = field(default_factory=deque)
    byte_count: int = 0
    subscribers: set[str] = field(default_factory=set)
    #: True after a completed history seed attempt (including an empty result).
    seeded: bool = False

    def append_text(
        self, text: str, *, source_work_id: str | None = None,
        kind: str = PEEK_KIND_TEXT, force: bool = False, field: str = 'text',
    ) -> list[PeekFrame]:
        if (not self.live and not force) or not text:
            return []
        if kind not in _TEXT_KINDS:
            _fail('kind', 'text or thinking')
        try:
            char_lens = [len(char.encode('utf-8')) for char in text]
        except UnicodeEncodeError:
            _fail(field, 'a UTF-8 string')
        # Frame size is fixed overhead plus UTF-8 payload; plan chunks, then append.
        overhead = _frame_bytes(_frame_stub(
            self, {'text': ''}, kind=kind, source_work_id=source_work_id,
        ))
        budget = self.max_bytes - overhead
        if budget <= 0:
            raise ControlError(
                'message_too_large',
                f'peek frame overhead is {overhead} bytes; '
                f'increase control.peek_max_bytes ({self.max_bytes})',
            )
        chunks: list[str] = []
        offset = 0
        length = len(text)
        while offset < length:
            end = offset
            size = 0
            while end < length:
                ch_len = char_lens[end]
                if size + ch_len > budget:
                    break
                size += ch_len
                end += 1
            if end == offset:
                need = overhead + char_lens[offset]
                raise ControlError(
                    'message_too_large',
                    f'peek frame is {need} bytes; increase control.peek_max_bytes ({self.max_bytes})',
                )
            chunks.append(text[offset:end])
            offset = end
        evicted: list[PeekFrame] = []
        for chunk in chunks:
            evicted.extend(self._append(
                {'text': chunk}, kind=kind, source_work_id=source_work_id, force=force,
            ))
        return evicted

    def append_step(self, label: str | None = None, *, source_work_id: str | None = None) -> list[PeekFrame]:
        if not self.live:
            return []
        if label is not None and (not isinstance(label, str) or any(ord(char) < 32 for char in label)):
            _fail('label', 'null or a string without control characters')
        if label is not None:
            _utf8_str(label, 'label')
        return self._append({'label': label}, kind=PEEK_KIND_STEP, source_work_id=source_work_id)

    def append_tool_call(
        self, call_id: str, name: str, *, detail: str | None = None, args_text: str | None = None,
        source_work_id: str | None = None,
    ) -> list[PeekFrame]:
        if not self.live:
            return []
        _text(call_id, 'call_id')
        _text(name, 'name')
        _utf8_str(name, 'name')
        if detail is not None and (not isinstance(detail, str) or any(ord(char) < 32 for char in detail)):
            _fail('detail', 'null or a string without control characters')
        _optional_body(args_text, 'args_text')
        return self._append(
            {'call_id': call_id, 'name': name, 'detail': detail, 'args_text': args_text},
            kind=PEEK_KIND_TOOL_CALL, source_work_id=source_work_id,
        )

    def append_tool_result(
        self, call_id: str, *, ok: bool, name: str | None = None, summary: str | None = None,
        result_text: str | None = None, source_work_id: str | None = None,
    ) -> list[PeekFrame]:
        if not self.live:
            return []
        _text(call_id, 'call_id')
        if type(ok) is not bool:
            _fail('ok', 'a boolean')
        if name is not None and (not isinstance(name, str) or any(ord(char) < 32 for char in name)):
            _fail('name', 'null or a string without control characters')
        if name is not None:
            _utf8_str(name, 'name')
        if summary is not None and (not isinstance(summary, str) or any(ord(char) < 32 for char in summary)):
            _fail('summary', 'null or a string without control characters')
        if summary is not None:
            _utf8_str(summary, 'summary')
        _optional_body(result_text, 'result_text')
        return self._append(
            {
                'call_id': call_id, 'name': name, 'ok': ok, 'summary': summary,
                'result_text': result_text,
            },
            kind=PEEK_KIND_TOOL_RESULT, source_work_id=source_work_id,
        )

    def append_question(
        self, prompt_summary: str, kind: str, *, source_work_id: str | None = None,
    ) -> list[PeekFrame]:
        if not self.live:
            return []
        _text(prompt_summary, 'prompt_summary')
        if any(ord(char) < 32 for char in prompt_summary):
            _fail('prompt_summary', 'a string without control characters')
        _text(kind, 'kind')
        return self._append(
            {'prompt_summary': prompt_summary, 'kind': kind},
            kind=PEEK_KIND_QUESTION, source_work_id=source_work_id,
        )

    def append_usage(
        self, *, tokens_in: int | None = None, tokens_out: int | None = None, cost: float | None = None,
        source_work_id: str | None = None,
    ) -> list[PeekFrame]:
        if not self.live:
            return []
        _optional_nonneg_int(tokens_in, 'tokens_in')
        _optional_nonneg_int(tokens_out, 'tokens_out')
        _optional_nonneg_float(cost, 'cost')
        return self._append(
            {
                'tokens_in': tokens_in,
                'tokens_out': tokens_out,
                'cost': None if cost is None else float(cost),
            },
            kind=PEEK_KIND_USAGE, source_work_id=source_work_id,
        )

    def append_outcome(
        self, state: str, *, duration_seconds: float | None = None, ended_at: str | None = None,
        extra: str | None = None, source_work_id: str | None = None,
    ) -> list[PeekFrame]:
        if state not in _OUTCOME_STATES:
            _fail('state', f'one of {", ".join(sorted(_OUTCOME_STATES))}')
        if duration_seconds is not None and (
            type(duration_seconds) not in (int, float) or isinstance(duration_seconds, bool)
            or not math.isfinite(duration_seconds) or duration_seconds < 0
        ):
            _fail('duration_seconds', 'null or a nonnegative finite number')
        if ended_at is not None:
            _timestamp(ended_at, 'ended_at')
        if extra is not None:
            if not isinstance(extra, str) or any(ord(char) < 32 for char in extra):
                _fail('extra', 'null or a string without control characters')
            try:
                extra.encode('utf-8')
            except UnicodeEncodeError:
                _fail('extra', 'a UTF-8 string')
        dropped = self._append(
            {
                'state': state,
                'duration_seconds': None if duration_seconds is None else float(duration_seconds),
                'ended_at': ended_at or utc_now(),
                'extra': extra,
            },
            kind=PEEK_KIND_OUTCOME,
            source_work_id=source_work_id,
        )
        self.live = False
        return dropped

    def _append(
        self, payload: dict[str, Any], *, kind: str, source_work_id: str | None,
        force: bool = False, at: str | None = None,
    ) -> list[PeekFrame]:
        if kind != PEEK_KIND_OUTCOME and not self.live and not force:
            return []
        frame: PeekFrame = {
            'seq': self.next_seq,
            'at': at or utc_now(),
            'work_id': self.work_id,
            'source_work_id': source_work_id or self.work_id,
            'kind': kind,
            'payload': payload,
        }
        size = _frame_bytes(frame)
        # Operator resource limit: refuse a single frame larger than the buffer budget.
        if size > self.max_bytes:
            raise ControlError(
                'message_too_large',
                f'peek frame is {size} bytes; increase control.peek_max_bytes ({self.max_bytes})',
            )
        dropped: list[PeekFrame] = []
        while self.frames and (len(self.frames) >= self.max_frames or self.byte_count + size > self.max_bytes):
            old = self.frames.popleft()
            self.byte_count -= _frame_bytes(old)
            dropped.append(old)
        self.frames.append(frame)
        self.byte_count += size
        self.next_seq += 1
        return dropped

    def since(self, after_seq: int) -> list[PeekFrame]:
        if type(after_seq) is not int or after_seq < 0:
            _fail('after_seq', 'a nonnegative integer')
        return [frame for frame in self.frames if frame['seq'] > after_seq]


def _frame_stub(
    buffer: _Buffer, payload: dict[str, Any], *, kind: str, source_work_id: str | None,
) -> PeekFrame:
    """Build a frame-shaped value for size checks without allocating a sequence number."""
    return {
        'seq': buffer.next_seq,
        'at': '',
        'work_id': buffer.work_id,
        'source_work_id': source_work_id or buffer.work_id,
        'kind': kind,
        'payload': payload,
    }


def _optional_nonneg_int(value: Any, field: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        _fail(field, 'null or a nonnegative integer')


def _optional_nonneg_float(value: Any, field: str) -> None:
    if value is not None and (
        type(value) not in (int, float) or isinstance(value, bool)
        or not math.isfinite(value) or value < 0
    ):
        _fail(field, 'null or a nonnegative finite number')


def _frame_bytes(frame: PeekFrame) -> int:
    payload = frame['payload']
    try:
        return sum(
            len(value.encode('utf-8')) for value in payload.values() if isinstance(value, str)
        ) + 96
    except UnicodeEncodeError:
        raise ControlError('invalid_request', 'peek frame string is not UTF-8') from None


@dataclass
class _PeekSub:
    """One peek subscriber: buffer key, Activity row opened, optional source filter."""

    buffer_id: str
    opened_work_id: str
    source_filter: str | None = None
    #: Matching frames ring-evicted before this subscriber received them.
    lost: int = 0
    #: Highest ``seq`` already returned on open or poll.
    delivered: int = 0
    last_active: float = field(default_factory=time.monotonic)


@dataclass
class PeekRegistry:
    """Lazy per-work buffers and peek subscriber slots."""

    max_frames: int = PEEK_DEFAULT_MAX_FRAMES
    max_bytes: int = PEEK_DEFAULT_MAX_BYTES
    max_subscribers: int = PEEK_DEFAULT_MAX_SUBSCRIBERS
    subscriber_idle: float = PEEK_DEFAULT_SUBSCRIBER_IDLE
    _buffers: dict[str, _Buffer] = field(default_factory=dict)
    _peeks: dict[str, _PeekSub] = field(default_factory=dict)

    def subscriber_count(self) -> int:
        return len(self._peeks)

    def ensure_buffer(self, work_id: str) -> None:
        """Create an empty live buffer before async history catch-up."""
        _text(work_id, 'work_id')
        if work_id not in self._buffers:
            self._buffers[work_id] = _Buffer(
                work_id, max_frames=self.max_frames, max_bytes=self.max_bytes,
            )

    def discard_if_unsubscribed(self, work_id: str) -> None:
        """Drop a reserved buffer that never gained a subscriber."""
        buffer = self._buffers.get(work_id)
        if buffer is not None and not buffer.subscribers:
            self._buffers.pop(work_id, None)

    def require_subscriber_slot(self) -> None:
        self._reap_idle_subscribers()
        if len(self._peeks) >= self.max_subscribers:
            raise ControlError(
                'peek_subscriber_limit',
                f'at most {self.max_subscribers} peeks; close one or raise control.peek_max_subscribers',
            )

    def _reap_idle_subscribers(self) -> None:
        if self.subscriber_idle <= 0:
            return
        now = time.monotonic()
        for peek_id, sub in list(self._peeks.items()):
            if now - sub.last_active >= self.subscriber_idle:
                self.close(peek_id)

    def apply_seed(self, work_id: str, seed: Sequence[PeekHistoryPart]) -> None:
        """Install history catch-up once. Restores prior frames if seeding fails."""
        buffer = self._buffers.get(work_id)
        if buffer is None or buffer.seeded:
            return
        for part in seed:
            if part.kind not in _TEXT_KINDS:
                _fail('seed.kind', 'text or thinking')
        prior = list(buffer.frames)
        if not seed:
            buffer.seeded = True
            return
        buffer.frames.clear()
        buffer.byte_count = 0
        buffer.next_seq = 1
        try:
            for part in seed:
                if part.text:
                    self._note_drops(work_id, buffer.append_text(
                        part.text, kind=part.kind, source_work_id=part.source_work_id,
                        force=True, field='seed.text',
                    ))
            for frame in prior:
                self._note_drops(work_id, buffer._append(
                    dict(frame['payload']),
                    kind=frame['kind'],
                    source_work_id=frame['source_work_id'],
                    force=True,
                    at=frame['at'],
                ))
        except BaseException:
            buffer.frames.clear()
            buffer.byte_count = 0
            buffer.next_seq = 1
            for frame in prior:
                buffer._append(
                    dict(frame['payload']),
                    kind=frame['kind'],
                    source_work_id=frame['source_work_id'],
                    force=True,
                    at=frame['at'],
                )
            raise
        buffer.seeded = True

    def observe_text(self, work_id: str, text: str, *, source_work_id: str | None = None,
                     kind: str = PEEK_KIND_TEXT) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        self._note_drops(work_id, buffer.append_text(text, source_work_id=source_work_id, kind=kind))

    def observe_step(self, work_id: str, label: str | None = None, *,
                     source_work_id: str | None = None) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        self._note_drops(work_id, buffer.append_step(label, source_work_id=source_work_id))

    def observe_tool_call(
        self, work_id: str, call_id: str, name: str, *, detail: str | None = None,
        args_text: str | None = None, source_work_id: str | None = None,
    ) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        self._note_drops(work_id, buffer.append_tool_call(
            call_id, name, detail=detail, args_text=args_text, source_work_id=source_work_id,
        ))

    def observe_tool_result(
        self, work_id: str, call_id: str, *, ok: bool, name: str | None = None,
        summary: str | None = None, result_text: str | None = None,
        source_work_id: str | None = None,
    ) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        self._note_drops(work_id, buffer.append_tool_result(
            call_id, ok=ok, name=name, summary=summary, result_text=result_text,
            source_work_id=source_work_id,
        ))

    def observe_question(
        self, work_id: str, prompt_summary: str, kind: str, *,
        source_work_id: str | None = None,
    ) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        self._note_drops(
            work_id, buffer.append_question(prompt_summary, kind, source_work_id=source_work_id),
        )

    def observe_usage(
        self, work_id: str, *, tokens_in: int | None = None, tokens_out: int | None = None,
        cost: float | None = None, source_work_id: str | None = None,
    ) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        if tokens_in is None and tokens_out is None and cost is None:
            return
        self._note_drops(work_id, buffer.append_usage(
            tokens_in=tokens_in, tokens_out=tokens_out, cost=cost, source_work_id=source_work_id,
        ))

    def observe_outcome(
        self, work_id: str, state: str, *, duration_seconds: float | None = None,
        ended_at: str | None = None, extra: str | None = None,
    ) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        self._note_drops(work_id, buffer.append_outcome(
            state, duration_seconds=duration_seconds, ended_at=ended_at, extra=extra,
        ))

    def mark_ended(self, work_id: str) -> None:
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        buffer.live = False
        # Keep frames until a subscriber opens or the buffer is empty.
        if not buffer.subscribers and not buffer.frames:
            self._buffers.pop(work_id, None)

    def has_buffer(self, work_id: str) -> bool:
        return work_id in self._buffers

    def is_seeded(self, work_id: str) -> bool:
        buffer = self._buffers.get(work_id)
        return buffer is not None and buffer.seeded

    def open(
        self, work_id: str, *, seed: Sequence[PeekHistoryPart] = (),
        seed_done: bool = False, source_filter: str | None = None,
        opened_work_id: str | None = None,
    ) -> PeekOpenResult:
        """Open a peek subscriber. ``seed`` fills a new buffer before live tee continues.

        ``work_id`` is the buffer key (main turn Activity id). ``opened_work_id`` is
        the Activity row the operator selected (main or subagent). When
        ``source_filter`` is set, open/poll return only frames from that source
        plus terminal ``outcome`` frames. ``seed_done`` marks that history catch-up
        finished (even when empty) so a later open does not re-seed.
        """
        _text(work_id, 'work_id')
        selected = opened_work_id or work_id
        _text(selected, 'opened_work_id')
        if source_filter is not None:
            _text(source_filter, 'source_filter')
        self.require_subscriber_slot()
        buffer = self._buffers.get(work_id)
        if buffer is None:
            buffer = self._buffers[work_id] = _Buffer(
                work_id, max_frames=self.max_frames, max_bytes=self.max_bytes,
            )
        # Subscribe before seeding so seed eviction counts in lost.
        peek_id = uuid4().hex
        sub = _PeekSub(work_id, selected, source_filter)
        self._peeks[peek_id] = sub
        buffer.subscribers.add(peek_id)
        try:
            if not buffer.seeded and (seed or seed_done):
                self.apply_seed(work_id, seed)
        except BaseException:
            buffer.subscribers.discard(peek_id)
            self._peeks.pop(peek_id, None)
            if not buffer.subscribers and not buffer.frames and not buffer.seeded:
                self._buffers.pop(work_id, None)
            raise
        frames = _filter_frames(list(buffer.frames), source_filter)
        if frames:
            sub.delivered = frames[-1]['seq']
        lost = sub.lost
        sub.lost = 0
        sub.last_active = time.monotonic()
        return {
            'peek_id': peek_id,
            'work_id': selected,
            'live': buffer.live,
            'frames': frames,
            'lost': lost,
        }

    def refresh_open(self, peek_id: str) -> PeekOpenResult:
        """Rebuild an open result after frames were appended post-subscribe."""
        _text(peek_id, 'peek_id')
        sub = self._peeks.get(peek_id)
        if sub is None:
            raise ControlError('peek_ended', 'peek is closed or unknown; open again')
        buffer = self._buffers.get(sub.buffer_id)
        if buffer is None:
            self._peeks.pop(peek_id, None)
            raise ControlError('peek_ended', 'peek buffer was discarded; open again')
        frames = _filter_frames(list(buffer.frames), sub.source_filter)
        if frames:
            sub.delivered = frames[-1]['seq']
        lost = sub.lost
        sub.lost = 0
        sub.last_active = time.monotonic()
        return {
            'peek_id': peek_id,
            'work_id': sub.opened_work_id,
            'live': buffer.live,
            'frames': frames,
            'lost': lost,
        }

    def poll(self, peek_id: str, after_seq: int) -> PeekPollResult:
        _text(peek_id, 'peek_id')
        self._reap_idle_subscribers()
        sub = self._peeks.get(peek_id)
        if sub is None:
            raise ControlError('peek_ended', 'peek is closed or unknown; open again')
        buffer = self._buffers.get(sub.buffer_id)
        if buffer is None:
            self._peeks.pop(peek_id, None)
            raise ControlError('peek_ended', 'peek buffer was discarded; open again')
        sub.last_active = time.monotonic()
        frames = _filter_frames(buffer.since(after_seq), sub.source_filter)
        live = buffer.live
        lost = sub.lost
        sub.lost = 0
        if frames:
            sub.delivered = max(sub.delivered, frames[-1]['seq'])
        if not live and not frames and lost == 0:
            buffer.subscribers.discard(peek_id)
            self._peeks.pop(peek_id, None)
            if not buffer.subscribers:
                self._buffers.pop(sub.buffer_id, None)
            raise ControlError('peek_ended', 'turn ended and peek window drained')
        return {
            'peek_id': peek_id,
            'work_id': sub.opened_work_id,
            'live': live,
            'frames': frames,
            'lost': lost,
        }

    def close(self, peek_id: str) -> None:
        _text(peek_id, 'peek_id')
        sub = self._peeks.pop(peek_id, None)
        if sub is None:
            return
        buffer = self._buffers.get(sub.buffer_id)
        if buffer is None:
            return
        buffer.subscribers.discard(peek_id)
        if not buffer.subscribers:
            self._buffers.pop(sub.buffer_id, None)

    def _note_drops(self, work_id: str, dropped: Sequence[PeekFrame]) -> None:
        if not dropped:
            return
        buffer = self._buffers.get(work_id)
        if buffer is None:
            return
        for peek_id in buffer.subscribers:
            sub = self._peeks.get(peek_id)
            if sub is None:
                continue
            sub.lost += sum(
                1 for frame in dropped
                if frame['seq'] > sub.delivered and _frame_visible(frame, sub.source_filter)
            )


def _frame_visible(frame: PeekFrame, source_filter: str | None) -> bool:
    if source_filter is None:
        return True
    return frame['source_work_id'] == source_filter or frame['kind'] == PEEK_KIND_OUTCOME


def _filter_frames(frames: list[PeekFrame], source_filter: str | None) -> list[PeekFrame]:
    if source_filter is None:
        return frames
    return [frame for frame in frames if _frame_visible(frame, source_filter)]
