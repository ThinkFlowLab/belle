"""A child hook keeps the parent turn's scope when its stream arrives first or later."""

import asyncio
import threading
from types import SimpleNamespace

from belle_agent.backend import TurnScope
from belle_agent.integrations.docker_agent import adapter, hooks


def _backend(monkeypatch):
    backend = object.__new__(adapter.DockerAgentBackend)
    scope = TurnScope()
    turn = adapter._Turn("source", scope, {}, session="parent")
    backend._turns = {"source": turn}
    backend._child_sessions_ready = threading.Condition()
    backend._hooks = SimpleNamespace(run=lambda value: value)
    backend._tools = SimpleNamespace(authorize=lambda call: call)
    backend._belle_tools = {}
    monkeypatch.setattr(hooks, "answer", lambda _event, active, *_rest: active)
    return backend, turn, scope


async def test_child_hook_waits_for_its_stream_session(monkeypatch):
    backend, turn, scope = _backend(monkeypatch)
    turn.tools["transfer"] = hooks.TRANSFER_TOOL
    waiting = threading.Event()
    wait_for = backend._child_sessions_ready.wait_for

    def signal_wait(predicate, timeout):
        waiting.set()
        return wait_for(predicate, timeout)

    monkeypatch.setattr(backend._child_sessions_ready, "wait_for", signal_wait)
    hook = asyncio.create_task(asyncio.to_thread(
        backend._hook, hooks.TOOL_GUARD, {"session_id": "child"}, {},
    ))
    assert await asyncio.to_thread(waiting.wait, 1)
    with backend._child_sessions_ready:
        turn.subsessions.add("child")
        backend._child_sessions_ready.notify_all()
    assert await asyncio.wait_for(hook, 1) == ("source", scope)


async def test_unknown_child_hook_receives_no_turn(monkeypatch):
    backend, turn, _ = _backend(monkeypatch)
    turn.tools["transfer"] = hooks.TRANSFER_TOOL
    monkeypatch.setattr(adapter, "CHILD_SESSION_WAIT_SECONDS", 0.01)
    assert await asyncio.to_thread(backend._hook, hooks.TOOL_GUARD, {"session_id": "foreign"}, {}) is None


def test_foreign_hook_does_not_wait_without_a_transfer(monkeypatch):
    backend, _, _ = _backend(monkeypatch)

    def unexpected_wait(*_args):
        raise AssertionError("no child session is expected")

    monkeypatch.setattr(backend._child_sessions_ready, "wait_for", unexpected_wait)
    assert backend._hook(hooks.TOOL_GUARD, {"session_id": "foreign"}, {}) is None
