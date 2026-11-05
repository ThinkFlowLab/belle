"""Shared fixtures and the controls of the harness integration tests."""

import asyncio
import functools
import importlib.util
import inspect
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.scopes import capabilities as caps
from belle_agent.scopes import compile_scopes, register_channel
from belle_agent.tools.server import ToolServer

# The factory before anything imports JiuwenSwarm.
_RECORD_FACTORY = logging.getLogRecordFactory()
INTEGRATION = bool(os.environ.get("OCB_OPENCODE_URL"))
DOCKER_AGENT = bool(os.environ.get("DAB_AGENT_URL"))
PROVIDER = os.environ.get("OCB_PROVIDER") == "1"
PLAIN = os.environ.get("OCB_PLAIN") == "1"
PLUGIN = os.environ.get("OCB_PLUGIN") == "1"
PROXY = bool(os.environ.get("OCB_PROXY"))
STAGE = os.environ.get("OCB_STAGE", "")
MATTERMOST = bool(os.environ.get("MATTERMOST_TEST_URL") and os.environ.get("MATTERMOST_TEST_ADMIN_TOKEN"))
MATRIX = all(os.environ.get(name) for name in ("MATRIX_TEST_HOMESERVER", "MATRIX_TEST_REGISTRATION", "MATRIX_TEST_HELPER"))
TEST_TIMEOUT = float(os.environ.get("OCB_TEST_TIMEOUT") or 0)
#: The time after OCB_TEST_TIMEOUT at which the alarm stops an async test that blocks its event loop.
ALARM_BACKSTOP = 5.0
# The test directory named jiuwenswarm imports as a namespace package of that name: look for a module of the real one.
try:
    JIUWENSWARM = importlib.util.find_spec("jiuwenswarm.extensions") is not None
except ImportError:
    JIUWENSWARM = False
# Belle's tool server needs the MCP 2 SDK of belle-agent[mcp]. JiuwenSwarm pins MCP 1.x, which has no mcp_types.
TOOL_SERVER_SDK = importlib.util.find_spec("mcp_types") is not None
JIUWENSWARM_TESTS = {Path(__file__).parent / "jiuwenswarm", Path(__file__).parent / "jiuwenswarm_slack"}


class _NeedsJiuwenSwarm(pytest.Module):
    def collect(self):
        pytest.skip("needs the jiuwenswarm package, which the JiuwenSwarm test image holds: containers/test/README.md")


def pytest_pycollect_makemodule(module_path, parent):
    if not JIUWENSWARM and module_path.parent in JIUWENSWARM_TESTS:
        return _NeedsJiuwenSwarm.from_parent(parent, path=module_path)
    return None


@pytest.fixture(scope="session", autouse=True)
def _tool_server_sdk():
    """Skip each test that starts Belle's tool server where the SDK of the server is absent."""
    if TOOL_SERVER_SDK:
        yield
        return

    async def start(self):
        pytest.skip("Belle's tool server needs the mcp extra: belle-agent[mcp]")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ToolServer, "start", start)
        yield


def _logger_state(logger: logging.Logger) -> tuple:
    return logger.level, logger.propagate, logger.disabled, list(logger.handlers), list(logger.filters)


@pytest.fixture(autouse=True)
def _restore_logging():
    """Give the loggers back as each test found them, and give each test the standard record factory.

    Host extensions change loggers for the whole process. JiuwenSwarm's import replaces the record factory
    with one that masks text in each record. Belle's tests check the text that Belle logs.
    """
    logging.setLogRecordFactory(_RECORD_FACTORY)
    saved = {name: _logger_state(logger) for name, logger in _loggers()}
    yield
    for name, logger in _loggers():
        level, logger.propagate, logger.disabled, logger.handlers[:], logger.filters[:] = saved.get(
            name, (logging.NOTSET, True, False, [], [])
        )
        logger.setLevel(level)


def _loggers():
    yield "", logging.getLogger()
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger):
            yield name, logger


@pytest.fixture
def registry():
    snapshot = caps.snapshot_registry()
    yield register_channel
    caps.restore_registry(snapshot)


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def warn(warnings: list[str]) -> Callable[..., None]:
    def record(message: str, *args: Any) -> None:
        warnings.append(message % args if args else message)

    return record


@pytest.fixture
def refusal(warn: Callable[..., None]) -> Callable[..., str]:
    """Return the error that ``compile_scopes`` raises for these rules."""

    def refuse(entries: Any, **kwargs: Any) -> str:
        with pytest.raises(BelleConfigError) as caught:
            compile_scopes(entries, **{"warn": warn, **kwargs})
        return str(caught.value)

    return refuse


@pytest.fixture
def _delegate_to_the_manager(monkeypatch):
    async def search(manager, query, *, limit, min_score, **_):
        return await manager.search(query, opts={"maxResults": limit, "minScore": min_score})

    monkeypatch.setattr("belle_agent.second_brain.recall.search_corpus", search)


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs the OpenCode container that loads the relay plugin")
    config.addinivalue_line("markers", "adapter: needs an OpenCode container the adapter drives, with or without the plugin")
    config.addinivalue_line("markers", "opencode: checks the OpenCode backend without a running server")
    config.addinivalue_line("markers", "no_plugin: an adapter check of what holds only when no plugin is loaded")
    config.addinivalue_line("markers", "plugin: an adapter check of what holds only behind the relay plugin")
    config.addinivalue_line("markers", "provider: needs the real model behind OpenCode (OCB_PROVIDER=1)")
    config.addinivalue_line("markers", "validation: needs the provider behind the recording proxy (OCB_PROXY)")
    config.addinivalue_line("markers", "docker_agent: checks the Docker Agent backend without a running server")
    config.addinivalue_line("markers", "docker_agent_server: needs a Docker Agent server (DAB_AGENT_URL)")
    config.addinivalue_line(
        "markers", "mattermost_server: needs a Mattermost test server (MATTERMOST_TEST_URL, MATTERMOST_TEST_ADMIN_TOKEN)"
    )
    config.addinivalue_line(
        "markers", "matrix_server: needs a Matrix server (MATRIX_TEST_HOMESERVER, MATRIX_TEST_REGISTRATION, MATRIX_TEST_HELPER)"
    )
    config.addinivalue_line("markers", "stage(name): one stage of the restart checks (OCB_STAGE)")


def pytest_collection_modifyitems(config, items):
    for item in items:
        stage = item.get_closest_marker("stage")
        if (stage.args[0] if stage else "") != STAGE:
            item.add_marker(pytest.mark.skip(reason="the restart checks run one stage at a time: OCB_STAGE names the stage"))
            continue
        if item.get_closest_marker("docker_agent_server") and not DOCKER_AGENT:
            item.add_marker(pytest.mark.skip(reason="needs a Docker Agent server: DAB_AGENT_URL"))
            continue
        if item.get_closest_marker("mattermost_server") and not MATTERMOST:
            item.add_marker(
                pytest.mark.skip(reason="needs a Mattermost test server: MATTERMOST_TEST_URL and MATTERMOST_TEST_ADMIN_TOKEN")
            )
            continue
        if item.get_closest_marker("matrix_server") and not MATRIX:
            item.add_marker(
                pytest.mark.skip(
                    reason="needs a Matrix server: MATRIX_TEST_HOMESERVER, MATRIX_TEST_REGISTRATION and MATRIX_TEST_HELPER"
                )
            )
            continue
        marks = {"integration", "adapter", "provider"} & set(item.keywords)
        if not marks:
            continue
        if not INTEGRATION:
            item.add_marker(pytest.mark.skip(reason="needs an OpenCode server: OCB_OPENCODE_URL"))
        elif ("provider" in marks) != PROVIDER:
            item.add_marker(pytest.mark.skip(reason="stand-in checks and provider checks run in separate runs"))
        elif bool(item.get_closest_marker("validation")) != PROXY:
            item.add_marker(pytest.mark.skip(reason="the provider validation runs behind the recording proxy, alone"))
        elif ("adapter" in marks) != (PLAIN or PLUGIN):
            item.add_marker(pytest.mark.skip(reason="relay checks and adapter checks run in separate passes"))
        elif item.get_closest_marker("plugin" if PLAIN else "no_plugin"):
            item.add_marker(pytest.mark.skip(reason="this check belongs to the other adapter mode"))


def _expired(item) -> TimeoutError:
    return TimeoutError(f"{item.nodeid} took longer than {TEST_TIMEOUT:g}s (OCB_TEST_TIMEOUT)")


def _timed(item, limit=TEST_TIMEOUT):
    """Fail the part of a test that outlives ``limit``. A check whose guard is gone then fails, not hangs."""

    def expired(*_):
        raise _expired(item)

    if not TEST_TIMEOUT:
        yield
        return
    # ponytail: an alarm, which reaches the main thread only. pytest-timeout when a test blocks in C or off it.
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _cancelled_on_timeout(item, test):
    """Cancel the coroutine of an async test after OCB_TEST_TIMEOUT. Its fixtures then tear down in the same loop."""

    @functools.wraps(test)
    async def timed(*args, **kwargs):
        limit = asyncio.timeout(TEST_TIMEOUT)
        try:
            async with limit:
                return await test(*args, **kwargs)
        except TimeoutError:
            if limit.expired():
                raise _expired(item) from None
            raise

    return timed


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield from _timed(item)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    test = getattr(item, "obj", None)
    if not (TEST_TIMEOUT and inspect.iscoroutinefunction(test)):
        yield from _timed(item)
        return
    # The cancellation comes first, so that the fixtures can close what the test holds.
    # The alarm comes later. It stops a test that blocks the event loop, which the cancellation cannot reach.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(item, "obj", _cancelled_on_timeout(item, test))
        yield from _timed(item, TEST_TIMEOUT + ALARM_BACKSTOP)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    yield from _timed(item)
