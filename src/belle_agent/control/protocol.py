"""Version 4 status values and strict JSON-line validation."""

import json
from datetime import UTC, datetime
from typing import Any, NoReturn, NotRequired, TypedDict, cast

from belle_agent.backend import MCP_STATES
from belle_agent.models import TaskModelRow

VERSION = 4
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 262144
STATES = frozenset({'ready', 'disabled', 'unsupported', 'unavailable', 'disconnected', 'degraded'})


class Metric(TypedDict):
    """A measured count or an explicit unavailable reason."""
    value: int | None
    reason: str | None


class McpCheck(TypedDict):
    """Whether a harness can use Belle's MCP tools, sampled at observed_at."""
    state: str
    reason: str | None
    observed_at: str | None


class WorkRow(TypedDict):
    """Content-free local execution or waiting-message identity."""
    id: str
    state: str
    type: str
    agent: str | None
    parent: str | None
    harness: str
    platform: str | None
    workspace: str | None
    conversation: str | None
    started_at: str | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None


class ScheduleRow(TypedDict):
    """One job summary without its prompt or delivery address."""
    job_id: str
    name: str
    state: str
    when: str
    next_due: str | None


class Component(TypedDict):
    """One provider sample with its measurement time."""
    id: str
    state: str
    reason: str | None
    observed_at: str | None
    metrics: dict[str, Metric]
    mcp: NotRequired[McpCheck]
    rows: NotRequired[list[WorkRow] | list[ScheduleRow]]


class Build(TypedDict):
    """Installed server package metadata; null when unknown."""
    version: str | None
    revision: str | None


class HarnessModelRow(TypedDict):
    owner: str
    model_name: str


class Snapshot(TypedDict):
    """A complete version 4 status result."""
    build: NotRequired[Build]
    instance_id: str
    generated_at: str
    platforms: list[Component]
    harnesses: list[Component]
    work: Component
    scheduling: Component
    task_models: list[TaskModelRow]
    harness_models: NotRequired[list[HarnessModelRow] | None]


class ControlError(ValueError):
    """A status protocol error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def utc_now() -> str:
    """Return the current UTC sample time."""
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def _fail(field: str, form: str) -> NoReturn:
    raise ControlError('invalid_request', f'{field} must be {form}')


def _keys(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(field, f'an object with exactly {", ".join(sorted(keys))}')
    return cast(dict[str, Any], value)


def _text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        _fail(field, 'a nonempty string without control characters')


def _timestamp(value: Any, field: str) -> None:
    _text(value, field)
    try:
        stamp = datetime.fromisoformat(value)
        offset = stamp.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError
    except ValueError:
        _fail(field, 'a UTC ISO 8601 timestamp')


def _reason(value: Any, field: str, missing: bool, *, when_present: str = 'null for a measured value') -> None:
    if missing:
        _text(value, field)
    elif value is not None:
        _fail(field, when_present)


def _component_reason(value: Any, state: str, field: str) -> None:
    if state in {'unsupported', 'unavailable'}:
        _text(value, field)
    elif state in {'degraded', 'disconnected'}:
        if value is not None:
            _text(value, field)
    elif value is not None:
        _fail(field, 'null for a measured value')


def validate_component(value: Any, field: str = 'component') -> Component:
    """Validate one provider value without rewriting its fields."""
    keys = {'id', 'state', 'reason', 'observed_at', 'metrics'}
    if isinstance(value, dict) and value.get('id') in {'work', 'scheduling'} and 'rows' in value:
        keys.add('rows')
    if isinstance(value, dict) and 'mcp' in value:
        keys.add('mcp')
    entry = _keys(value, keys, field)
    _text(entry['id'], f'{field}.id')
    if not isinstance(entry['state'], str) or entry['state'] not in STATES:
        _fail(f'{field}.state', f'one of {", ".join(sorted(STATES))}')
    _component_reason(entry['reason'], entry['state'], f'{field}.reason')
    if entry['observed_at'] is None and entry['state'] not in {'unsupported', 'unavailable'}:
        _fail(f'{field}.observed_at', 'a UTC ISO 8601 sample timestamp for this state')
    if entry['observed_at'] is not None:
        _timestamp(entry['observed_at'], f'{field}.observed_at')
    if 'mcp' in entry:
        mcp = _keys(entry['mcp'], {'state', 'reason', 'observed_at'}, f'{field}.mcp')
        if not isinstance(mcp['state'], str) or mcp['state'] not in MCP_STATES:
            _fail(f'{field}.mcp.state', f'one of {", ".join(sorted(MCP_STATES))}')
        _reason(
            mcp['reason'], f'{field}.mcp.reason', mcp['state'] not in {'ready', 'not_applicable'},
            when_present='null for ready or not_applicable',
        )
        if mcp['observed_at'] is not None:
            _timestamp(mcp['observed_at'], f'{field}.mcp.observed_at')
    if not isinstance(entry['metrics'], dict):
        _fail(f'{field}.metrics', 'an object of named measurements')
    for name, metric in entry['metrics'].items():
        _text(name, f'{field}.metrics key')
        metric = _keys(metric, {'value', 'reason'}, f'{field}.metrics.{name}')
        count = metric['value']
        if count is not None and (type(count) is not int or count < 0):
            _fail(f'{field}.metrics.{name}.value', 'a nonnegative integer or null')
        _reason(metric['reason'], f'{field}.metrics.{name}.reason', count is None)
    if 'rows' in entry:
        if not isinstance(entry['rows'], list):
            _fail(f'{field}.rows', 'an array of rows')
        identities: set[str] = set()
        parents: list[str] = []
        main_ids: set[str] = set()
        for row in entry['rows']:
            if entry['id'] == 'scheduling':
                row = _keys(row, {'job_id', 'name', 'state', 'when', 'next_due'}, f'{field}.rows')
                for key in ('job_id', 'name', 'when'):
                    _text(row[key], f'{field}.rows.{key}')
                if row['job_id'] in identities:
                    _fail(f'{field}.rows.job_id', 'unique within schedule rows')
                identities.add(row['job_id'])
                if row['state'] not in ('active', 'paused', 'ended'):
                    _fail(f'{field}.rows.state', 'active, paused, or ended')
                if row['next_due'] is not None:
                    _timestamp(row['next_due'], f'{field}.rows.next_due')
                continue
            row = _keys(row, {'id', 'state', 'type', 'agent', 'parent', 'harness', 'platform',
                              'workspace', 'conversation', 'started_at', 'model', 'tokens_in', 'tokens_out'},
                        f'{field}.rows')
            _text(row['id'], f'{field}.rows.id')
            if row['id'] in identities:
                _fail(f'{field}.rows.id', 'unique within work rows')
            identities.add(row['id'])
            _text(row['harness'], f'{field}.rows.harness')
            if row['type'] == 'main':
                main_ids.add(row['id'])
                if row['agent'] is not None or row['parent'] is not None:
                    _fail(f'{field}.rows', 'null agent and parent for a main row')
                if row['state'] not in ('running', 'queued'):
                    _fail(f'{field}.rows.state', 'running or queued for a main row')
            elif row['type'] == 'subagent':
                _text(row['parent'], f'{field}.rows.parent')
                parents.append(row['parent'])
                if row['agent'] is not None:
                    _text(row['agent'], f'{field}.rows.agent')
                if row['state'] not in ('running', 'completed', 'failed', 'cancelled', 'unreported'):
                    _fail(f'{field}.rows.state', 'running, completed, failed, cancelled, or unreported for a subagent')
            else:
                _fail(f'{field}.rows.type', 'main or subagent')
            if row['started_at'] is not None:
                _timestamp(row['started_at'], f'{field}.rows.started_at')
            if row['state'] == 'running' and row['started_at'] is None:
                _fail(f'{field}.rows.started_at', 'a UTC ISO 8601 timestamp for running work')
            if row['state'] == 'queued' and row['started_at'] is not None:
                _fail(f'{field}.rows.started_at', 'null for queued work')
            for name in ('platform', 'workspace', 'conversation', 'model'):
                if row[name] is not None:
                    _text(row[name], f'{field}.rows.{name}')
            for name in ('tokens_in', 'tokens_out'):
                if row[name] is not None and (type(row[name]) is not int or row[name] < 0):
                    _fail(f'{field}.rows.{name}', 'a nonnegative integer or null')
        if any(parent not in main_ids for parent in parents):
            _fail(f'{field}.rows.parent', 'the id of a main row in this snapshot')
    return entry  # type: ignore[return-value]


def validate_snapshot(value: Any) -> Snapshot:
    """Validate all version 4 fields and return the original dictionary."""
    keys = {'instance_id', 'generated_at', 'platforms', 'harnesses', 'work', 'scheduling', 'task_models'}
    if isinstance(value, dict) and 'harness_models' in value:
        keys.add('harness_models')
    if isinstance(value, dict) and 'build' in value:
        keys.add('build')
    entry = _keys(value, keys, 'result')
    if 'build' in entry:
        build = _keys(entry['build'], {'version', 'revision'}, 'result.build')
        for name in ('version', 'revision'):
            if build[name] is not None:
                _text(build[name], f'result.build.{name}')
    _text(entry['instance_id'], 'result.instance_id')
    _timestamp(entry['generated_at'], 'result.generated_at')
    for group in ('platforms', 'harnesses'):
        if not isinstance(entry[group], list):
            _fail(f'result.{group}', 'an array of components')
        names: set[str] = set()
        for component in entry[group]:
            validate_component(component, f'result.{group}')
            if group == 'harnesses' and 'mcp' not in component:
                _fail(f'result.{group}.mcp', 'present on every harness')
            if group == 'platforms' and 'mcp' in component:
                _fail(f'result.{group}.mcp', 'absent; only harnesses report mcp')
            if component['id'] in names:
                _fail(f'result.{group}.id', 'unique within its array')
            names.add(component['id'])
    for group in ('work', 'scheduling'):
        validate_component(entry[group], f'result.{group}')
        if 'mcp' in entry[group]:
            _fail(f'result.{group}.mcp', 'absent; only harnesses report mcp')
    if not isinstance(entry['task_models'], list):
        _fail('result.task_models', 'an array of task models')
    seen: set[tuple[str, str]] = set()
    for row in entry['task_models']:
        row = _keys(row, {'role', 'id', 'api', 'model_name', 'selected'}, 'result.task_models')
        if row['role'] not in ('decision', 'summarizer', 'extractor', 'classifier'):
            _fail('result.task_models.role', 'decision, summarizer, extractor, or classifier')
        for name in ('id', 'api'):
            _text(row[name], f'result.task_models.{name}')
        if row['model_name'] is not None:
            _text(row['model_name'], 'result.task_models.model_name')
        if type(row['selected']) is not bool:
            _fail('result.task_models.selected', 'a boolean')
        identity = (row['role'], row['id'])
        if identity in seen:
            _fail('result.task_models', 'unique role and id pairs')
        seen.add(identity)
    if entry.get('harness_models') is not None:
        if not isinstance(entry['harness_models'], list):
            _fail('result.harness_models', 'an array of harness models or null when unavailable')
        seen_models: set[tuple[str, str]] = set()
        for row in entry['harness_models']:
            row = _keys(row, {'owner', 'model_name'}, 'result.harness_models')
            _text(row['owner'], 'result.harness_models.owner')
            _text(row['model_name'], 'result.harness_models.model_name')
            identity = (row['owner'], row['model_name'])
            if identity in seen_models:
                _fail('result.harness_models', 'unique owner and model name pairs')
            seen_models.add(identity)
    return entry  # type: ignore[return-value]


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(key, 'present once')
        result[key] = value
    return result


def decode_line(data: bytes, limit: int) -> Any:
    """Decode one bounded UTF-8 JSON line and reject duplicate keys."""
    if len(data) > limit:
        raise ControlError('message_too_large', f'message is {len(data)} bytes; maximum is {limit} bytes')
    if not data.endswith(b'\n') or b'\n' in data[:-1]:
        _fail('message', 'one JSON object followed by one newline')
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda value: _fail('JSON number', 'finite'))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ControlError):
            raise
        raise ControlError('bad_json', 'message must contain valid UTF-8 JSON') from None


def encode_line(value: Any, limit: int) -> bytes:
    """Encode one JSON line or refuse its full size."""
    data = (json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    if len(data) > limit:
        raise ControlError('message_too_large', f'message is {len(data)} bytes; increase max_response_bytes ({limit})')
    return data


_WRITE_METHODS = frozenset({'schedule.toggle', 'schedule.delete', 'work.stop', 'work.drop'})
_PEEK_METHODS = frozenset({'peek.open', 'peek.poll', 'peek.close'})
_KNOWN_METHODS = frozenset({'snapshot.get'}) | _WRITE_METHODS | _PEEK_METHODS


def validate_request(value: Any) -> dict[str, Any]:
    """Validate the request shape before dispatching its method."""
    keys = {'version', 'id', 'method'}
    method_name = value.get('method') if isinstance(value, dict) else None
    if method_name in _WRITE_METHODS | _PEEK_METHODS:
        keys.add('params')
    entry = _keys(value, keys, 'request')
    _text(entry['id'], 'request.id')
    if type(entry['version']) is not int or entry['version'] != VERSION:
        raise ControlError('unsupported_version', f'request.version must be {VERSION}')
    _text(entry['method'], 'request.method')
    method = entry['method']
    if method in _WRITE_METHODS:
        identity = 'job_id' if method.startswith('schedule.') else 'work_id'
        params = _keys(entry['params'], {identity, 'expected_state', 'instance_id'}, 'request.params')
        for field in (identity, 'instance_id'):
            _text(params[field], f'request.params.{field}')
        states = {'schedule.toggle': ('active', 'paused'),
                  'schedule.delete': ('active', 'paused', 'ended'),
                  'work.stop': ('running',), 'work.drop': ('queued',)}[method]
        if params['expected_state'] not in states:
            _fail('request.params.expected_state', ' or '.join(states))
    elif method == 'peek.open':
        params = _keys(entry['params'], {'work_id', 'instance_id'}, 'request.params')
        _text(params['work_id'], 'request.params.work_id')
        _text(params['instance_id'], 'request.params.instance_id')
    elif method == 'peek.poll':
        params = _keys(entry['params'], {'peek_id', 'after_seq'}, 'request.params')
        _text(params['peek_id'], 'request.params.peek_id')
        if type(params['after_seq']) is not int or params['after_seq'] < 0:
            _fail('request.params.after_seq', 'a nonnegative integer')
    elif method == 'peek.close':
        params = _keys(entry['params'], {'peek_id'}, 'request.params')
        _text(params['peek_id'], 'request.params.peek_id')
    elif method not in _KNOWN_METHODS:
        raise ControlError(
            'unknown_method',
            'request.method must be snapshot.get, schedule.toggle, schedule.delete, '
            'work.stop, work.drop, peek.open, peek.poll, or peek.close',
        )
    return entry


def encode_request(request_id: str) -> bytes:
    """Encode one version 4 snapshot request."""
    return encode_line(validate_request({'version': VERSION, 'id': request_id, 'method': 'snapshot.get'}), MAX_REQUEST_BYTES)


def decode_response(data: bytes, expected_id: str) -> Snapshot:
    """Validate the response identity and return its snapshot or raise its error."""
    value = decode_line(data, MAX_RESPONSE_BYTES)
    if not isinstance(value, dict) or set(value) not in ({'version', 'id', 'result'}, {'version', 'id', 'error'}):
        _fail('response', 'an object with version, id, and exactly one of result or error')
    if type(value['version']) is not int or value['version'] != VERSION:
        raise ControlError('unsupported_version', f'response.version must be {VERSION}')
    if value['id'] != expected_id:
        _fail('response.id', 'the request id')
    if 'error' in value:
        error = _keys(value['error'], {'code', 'message'}, 'response.error')
        _text(error['code'], 'response.error.code')
        _text(error['message'], 'response.error.message')
        raise ControlError(error['code'], error['message'])
    return validate_snapshot(value['result'])
