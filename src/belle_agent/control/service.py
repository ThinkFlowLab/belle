"""Opt-in local snapshot collection with operator resource budgets."""

import asyncio
import errno
import fcntl
import json
import logging
import math
import os
import socket
import stat
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from belle_agent.control.peek import (
    PEEK_DEFAULT_MAX_BYTES,
    PEEK_DEFAULT_MAX_FRAMES,
    PEEK_DEFAULT_MAX_SUBSCRIBERS,
    PEEK_DEFAULT_SUBSCRIBER_IDLE,
)
from belle_agent.control.protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    VERSION,
    Build,
    Component,
    ControlError,
    HarnessModelRow,
    McpCheck,
    Metric,
    Snapshot,
    decode_line,
    encode_line,
    utc_now,
    validate_component,
    validate_request,
    validate_snapshot,
)
from belle_agent.models import TaskModelRow

logger = logging.getLogger(__name__)
Provider = Callable[[], Awaitable[Component]]
ModelProvider = Callable[[], Awaitable[list[HarnessModelRow] | None]]


@dataclass(frozen=True)
class ControlSettings:
    """Operator limits protect the host's memory, descriptors and event loop."""

    enabled: bool = False
    # Content-bearing peek of live turn text; off keeps status mounts metadata-only.
    peek: bool = False
    # Operator resource limits for retained peek windows and concurrent peeks.
    peek_max_frames: int = PEEK_DEFAULT_MAX_FRAMES
    peek_max_bytes: int = PEEK_DEFAULT_MAX_BYTES
    peek_max_subscribers: int = PEEK_DEFAULT_MAX_SUBSCRIBERS
    #: Close a peek with no open/poll for this many seconds (slot budget).
    peek_subscriber_idle_seconds: float = PEEK_DEFAULT_SUBSCRIBER_IDLE
    max_clients: int = 8
    max_request_bytes: int = MAX_REQUEST_BYTES
    max_response_bytes: int = MAX_RESPONSE_BYTES
    request_timeout: float = 2.0
    provider_timeout: float = 0.25

    def __post_init__(self) -> None:
        """Validate service budgets before the service can start."""
        if type(self.enabled) is not bool:
            raise ValueError('control.enabled must be a boolean')
        if type(self.peek) is not bool:
            raise ValueError('control.peek must be a boolean')
        for field in ('max_clients', 'max_request_bytes', 'max_response_bytes',
                      'peek_max_frames', 'peek_max_bytes', 'peek_max_subscribers'):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ValueError(f'control.{field} must be a positive integer')
        for field in ('request_timeout', 'provider_timeout', 'peek_subscriber_idle_seconds'):
            value = getattr(self, field)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'control.{field} must be a positive finite number of seconds')

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'ControlSettings':
        """Read control flags and resource limits; reject unknown keys."""
        section = config.get('control', {})
        allowed = {
            'enabled', 'peek', 'peek_max_frames', 'peek_max_bytes', 'peek_max_subscribers',
            'peek_subscriber_idle_seconds',
        }
        if not isinstance(section, Mapping) or set(section) - allowed:
            raise ValueError(
                'control must be an object with only enabled, peek, peek_max_frames, '
                'peek_max_bytes, peek_max_subscribers, and peek_subscriber_idle_seconds',
            )
        return cls(**section)


def unavailable(identity: str, reason: str, *, state: str = 'unavailable') -> Component:
    """Return explicit missing measurements without stale values."""
    names = {'scheduling': ('due_runs', 'running_runs'),
             'work': ('active_turns', 'queued_messages', 'queued_events')}.get(identity, ())
    metrics: dict[str, Metric] = {name: {'value': None, 'reason': reason} for name in names}
    return {'id': identity, 'state': state, 'reason': reason, 'observed_at': None, 'metrics': metrics}


def mcp_check(state: str, reason: str | None = None, observed_at: str | None = None) -> McpCheck:
    """Build the harness mcp field for a status component."""
    return {'state': state, 'reason': reason, 'observed_at': observed_at}


def server_build() -> Build:
    """Read installed distribution metadata once, without invoking git."""
    try:
        package = distribution('belle-agent')
    except PackageNotFoundError:
        return {'version': None, 'revision': None}
    revision = None
    direct = package.read_text('direct_url.json')
    if direct:
        try:
            vcs = json.loads(direct).get('vcs_info', {})
            if vcs.get('vcs') == 'git':
                revision = vcs.get('commit_id')
        except (ValueError, TypeError, AttributeError):
            pass
    return {'version': package.version, 'revision': revision}


class SnapshotCollector:
    """Read independent provider values without retaining their mutable state."""

    def __init__(self, providers: Mapping[str, Provider], *, timeout: float = 0.25,
                 task_models: Sequence[TaskModelRow] = (), harness_models: ModelProvider | None = None) -> None:
        # Keys identify protocol slots, never executable methods.
        for name in providers:
            if name not in {'work', 'scheduling'} and not name.startswith(('platforms/', 'harnesses/')):
                raise ValueError('provider name must be work, scheduling, platforms/<id>, or harnesses/<id>')
        self.build = server_build()
        self.providers = dict(providers)
        self.task_models = deepcopy(list(task_models))
        self.harness_models = harness_models
        self.timeout = timeout
        self.instance_id = uuid4().hex

    async def _read(self, name: str, provider: Provider) -> Component:
        try:
            async with asyncio.timeout(self.timeout):
                value = await provider()
                validate_component(value)
                harness = name.startswith('harnesses/')
                if harness != ('mcp' in value):
                    raise ValueError('mcp required on harnesses and absent elsewhere')
                # Detach nested dictionaries before another task can change them.
                return deepcopy(value)
        except TimeoutError:
            return self._failed(name, 'provider_timeout')
        except Exception:  # noqa: BLE001 - provider failure must not stop other providers.
            # Provider exception messages can contain paths, credentials or message content.
            return self._failed(name, 'provider_failed')

    @staticmethod
    def _failed(name: str, reason: str) -> Component:
        component = unavailable(name.rsplit('/', 1)[-1], reason)
        if name.startswith('harnesses/'):
            component['mcp'] = mcp_check('unknown', reason)
        return component

    async def collect(self) -> Snapshot:
        """Collect providers concurrently and isolate their failures."""
        reads: list[Awaitable[Any]] = [self._read(name, provider)
                                      for name, provider in self.providers.items()]
        if self.harness_models is not None:
            reads.append(self._read_models())
        values = await asyncio.gather(*reads)
        result: Snapshot = {
            'build': self.build, 'instance_id': self.instance_id, 'generated_at': utc_now(), 'platforms': [], 'harnesses': [],
            'work': unavailable('work', 'no_provider', state='unsupported'),
            'scheduling': unavailable('scheduling', 'no_provider', state='unsupported'),
            'task_models': deepcopy(self.task_models),
        }
        if self.harness_models is not None:
            result['harness_models'] = values[-1]
            values = values[:-1]
        else:
            result['harness_models'] = None
        for name, value in zip(self.providers, values, strict=True):
            if name.startswith('platforms/'):
                result['platforms'].append(value)
            elif name.startswith('harnesses/'):
                result['harnesses'].append(value)
            elif name == 'work':
                result['work'] = value
            else:
                result['scheduling'] = value
        return validate_snapshot(result)

    async def _read_models(self) -> list[HarnessModelRow] | None:
        """Read backend model names within the same operator-set provider budget."""
        assert self.harness_models is not None
        if isinstance(self.harness_models, CachedModelProvider):
            return await self.harness_models()
        try:
            async with asyncio.timeout(self.timeout):
                rows = await self.harness_models()
                return deepcopy(rows)
        except Exception:  # noqa: BLE001 - unavailable catalogues do not stop the status snapshot.
            return None


class CachedModelProvider:
    """Cache backend model names to keep status polling from querying the catalog each time."""

    def __init__(self, provider: ModelProvider, *, ttl: float = 30.0, timeout: float = 0.25,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('model catalogue cache ttl must be a positive finite number of seconds')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('model catalogue provider timeout must be a positive finite number of seconds')
        self.provider = provider
        self.ttl = float(ttl)
        self.timeout = timeout
        self.clock = clock
        self._expires = 0.0
        self._rows: list[HarnessModelRow] | None = None
        self._lock = asyncio.Lock()

    async def __call__(self) -> list[HarnessModelRow] | None:
        async with self._lock:
            now = self.clock()
            if now < self._expires:
                return deepcopy(self._rows)
            try:
                async with asyncio.timeout(self.timeout):
                    rows = await self.provider()
            except Exception:  # noqa: BLE001 - failed refresh is unavailable, not stale data.
                rows = None
            self._rows = deepcopy(rows)
            self._expires = self.clock() + self.ttl
            return deepcopy(self._rows)


class StatusService:
    """Serve local status and operator actions inside a private same-UID directory."""

    def __init__(self, collector: SnapshotCollector, runtime_dir: Path | None,
                 settings: ControlSettings | None = None, *,
                 toggle_schedule: Callable[[str, str], Awaitable[None]] | None = None,
                 delete_schedule: Callable[[str, str], Awaitable[None]] | None = None,
                 stop_work: Callable[[str, str], Awaitable[None]] | None = None,
                 drop_work: Callable[[str, str], Awaitable[None]] | None = None,
                 peek_open: Callable[[str], Awaitable[Any]] | None = None,
                 peek_poll: Callable[[str, int], Awaitable[Any]] | None = None,
                 peek_close: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.toggle_schedule = toggle_schedule
        self.delete_schedule = delete_schedule
        self.stop_work = stop_work
        self.drop_work = drop_work
        self.peek_open = peek_open
        self.peek_poll = peek_poll
        self.peek_close = peek_close
        self.collector = collector
        self.runtime_dir = runtime_dir
        self.settings = settings or ControlSettings()
        self._server: asyncio.Server | None = None
        self._lock: BinaryIO | None = None
        self._inode: int | None = None
        self._clients: set[asyncio.Task[Any]] = set()

    @property
    def socket_path(self) -> Path | None:
        """Return the local socket path when a runtime directory exists."""
        return self.runtime_dir / 'control.sock' if self.runtime_dir is not None else None

    async def start(self) -> bool:
        """Bind the private socket if enabled and return whether it started."""
        if not self.settings.enabled:
            return False
        if self._server is not None:
            return True
        if self.runtime_dir is None:
            logger.warning('control disabled: set BELLE_AGENT_RUNTIME_DIR or XDG_RUNTIME_DIR')
            return False
        try:
            self._bind_directory()
            path = self.socket_path
            assert path is not None
            self._server = await asyncio.start_unix_server(self._connected, path=path,
                                                          limit=self.settings.max_request_bytes)
            path.chmod(0o600)
            self._inode = path.lstat().st_ino
            self.collector.instance_id = uuid4().hex
            return True
        except (OSError, ValueError) as exc:
            logger.warning('control disabled: runtime directory unavailable: %s', exc)
            await self.stop()
            return False

    def _bind_directory(self) -> None:
        assert self.runtime_dir is not None
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.runtime_dir.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
            raise ValueError('runtime directory must be a real directory owned by this UID with mode 0700')
        # A persistent lock closes the bind/unlink race between cooperative Belle instances.
        fd = os.open(self.runtime_dir / 'control.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._lock = os.fdopen(fd, 'rb+')
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = self.socket_path
        assert path is not None
        try:
            existing = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            raise ValueError('control.sock must be a socket owned by this UID')
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(self.settings.request_timeout)
            try:
                probe.connect(str(path))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise
            else:
                raise ValueError('control.sock belongs to a running server')
        if path.lstat().st_ino != existing.st_ino:
            raise ValueError('control.sock changed during stale socket check')
        path.unlink()

    def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Admission happens before allocating a handler task or reading bytes.
        if len(self._clients) >= self.settings.max_clients:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._clients.add(task)
        def finished(done: asyncio.Task[None]) -> None:
            writer.close()
            self._clients.discard(done)
        task.add_done_callback(finished)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        identity: str | None = None
        #: peek.open succeeded but the client never received peek_id (encode/write fail).
        undelivered_peek_id: str | None = None
        try:
            async with asyncio.timeout(self.settings.request_timeout):
                try:
                    try:
                        data = await reader.readline()
                    except ValueError:
                        raise ControlError('message_too_large', 'request exceeds control.max_request_bytes') from None
                    value = decode_line(data, self.settings.max_request_bytes)
                    if isinstance(value, dict) and isinstance(value.get('id'), str):
                        identity = value['id']
                    validate_request(value)
                    method = value['method']
                    if method == 'snapshot.get':
                        result: Any = await self.collector.collect()
                    elif method.startswith('peek.'):
                        result = await self._peek(method, value['params'])
                        if method == 'peek.open' and isinstance(result, Mapping):
                            peek_id = result.get('peek_id')
                            if isinstance(peek_id, str) and peek_id:
                                undelivered_peek_id = peek_id
                    else:
                        params = value['params']
                        view = 'Jobs' if method.startswith('schedule.') else 'Activity'
                        if params['instance_id'] != self.collector.instance_id:
                            raise ControlError('stale_selection', f'Belle restarted; refresh {view} and retry')
                        action = {'schedule.toggle': self.toggle_schedule, 'schedule.delete': self.delete_schedule,
                                  'work.stop': self.stop_work, 'work.drop': self.drop_work}[method]
                        if action is None:
                            raise ControlError('unsupported', f'This host does not support {method}')
                        identity_field = 'job_id' if method.startswith('schedule.') else 'work_id'
                        try:
                            await action(params[identity_field], params['expected_state'])
                        except ControlError:
                            raise
                        except Exception:  # noqa: BLE001 - never expose store paths or provider exceptions.
                            raise ControlError('action_failed', f'Action failed; refresh {view} before retrying') from None
                        result = await self.collector.collect()
                    response = {'version': VERSION, 'id': identity, 'result': result}
                    payload = encode_line(response, self.settings.max_response_bytes)
                except ControlError as exc:
                    await self._close_undelivered_peek(undelivered_peek_id)
                    undelivered_peek_id = None
                    payload = encode_line({'version': VERSION, 'id': identity,
                                           'error': {'code': exc.code, 'message': exc.message}},
                                          self.settings.max_response_bytes)
                writer.write(payload)
                await writer.drain()
                # Client received the open result (or an error after reclaim).
                undelivered_peek_id = None
        except (TimeoutError, ConnectionError, ControlError, OSError):
            await self._close_undelivered_peek(undelivered_peek_id)
            undelivered_peek_id = None
        finally:
            writer.close()
            try:
                async with asyncio.timeout(self.settings.request_timeout):
                    await writer.wait_closed()
            except (TimeoutError, ConnectionError, OSError):
                writer.transport.abort()

    async def _close_undelivered_peek(self, peek_id: str | None) -> None:
        """Drop a peek the client never learned about (encode/write failure after open)."""
        if peek_id is None or self.peek_close is None:
            return
        try:
            await self.peek_close(peek_id)
        except Exception:
            # Reclaim must not fail the socket handler.
            logger.warning('undelivered peek close failed: peek_id=%s', peek_id, exc_info=True)

    async def _peek(self, method: str, params: Mapping[str, Any]) -> Any:
        """Dispatch peek methods; refuse when disabled or the host has no handler."""
        if not self.settings.peek:
            raise ControlError('peek_disabled', 'set control.peek: true to allow live turn peeks')
        if method == 'peek.open':
            if params['instance_id'] != self.collector.instance_id:
                raise ControlError('stale_selection', 'Belle restarted; refresh Activity and retry')
            if self.peek_open is None:
                raise ControlError('unsupported', 'This host does not support peek.open')
            return await self.peek_open(params['work_id'])
        if method == 'peek.poll':
            if self.peek_poll is None:
                raise ControlError('unsupported', 'This host does not support peek.poll')
            return await self.peek_poll(params['peek_id'], params['after_seq'])
        if method == 'peek.close':
            if self.peek_close is None:
                raise ControlError('unsupported', 'This host does not support peek.close')
            await self.peek_close(params['peek_id'])
            return {'closed': True}
        raise ControlError('unknown_method', f'unknown peek method {method}')

    async def stop(self) -> None:
        """Cancel readers and remove only the socket this service bound."""
        if self._server is not None:
            self._server.close()
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if self._server is not None:
            # From Python 3.12 this waits for open connections, so the readers are cancelled first.
            await self._server.wait_closed()
            self._server = None
        path = self.socket_path
        if path is not None and self._inode is not None:
            try:
                if path.lstat().st_ino == self._inode:
                    path.unlink()
            except FileNotFoundError:
                pass
        self._inode = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None
