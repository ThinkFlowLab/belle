"""A detached session's conversation lives on the harness until it expires. An attached session stays.

Every case runs on the scripted backend and on the OpenCode and Docker Agent containers with the stand-in model.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from conformance_kit import ModelTurn, worlds
from scripted_backend import ScriptedBackend

from belle_agent.backend import (
    CONVERSATION_UNAVAILABLE,
    EVENT_FAILED,
    OP_CONTINUE,
    OP_FORGET,
    OP_RECOVER,
    TurnEvent,
    TurnRequest,
    still_running,
)
from belle_agent.host import RESTART_LOST_NOTICE
from belle_agent.host.attached_sessions import AttachedSessionStore
from belle_agent.platforms.base import TRIGGER_DM, InboundMessage
from belle_agent.platforms.loopback import LoopbackPlatform
from belle_agent.tools.host import ToolHost
from tests.scheduling.fake_clock import FakeClock


class KeepingBackend(ScriptedBackend):
    """The scripted backend with conversations that it keeps, continues and deletes, as a harness does."""

    def __init__(self) -> None:
        super().__init__()
        self._capabilities = replace(self._capabilities,
                                     operations=self._capabilities.operations | {OP_CONTINUE, OP_FORGET})
        self.kept: set[str] = set()

    async def run_turn(self, request: TurnRequest):  # type: ignore[no-untyped-def]
        if request.existing_conversation_only and request.conversation not in self.kept:
            yield TurnEvent(EVENT_FAILED, text=CONVERSATION_UNAVAILABLE)
            return
        self.kept.add(request.conversation)
        async for event in super().run_turn(request):
            yield event

    async def retained(self, conversation: str) -> bool:
        return conversation in self.kept

    async def forget(self, conversation: str) -> None:
        if conversation in self._running:
            raise still_running(conversation)
        self.kept.discard(conversation)


def _kit(kit: Any) -> Any:
    return KeepingBackend() if isinstance(kit, ScriptedBackend) else kit


def where(kit: Any, conversation: str) -> str:
    """The server path of a conversation's harness session, read while Belle still names it."""
    backend = kit.backend
    if isinstance(backend, KeepingBackend):
        return conversation
    if hasattr(backend, "_sessions"):
        return f"/sessions/{backend._sessions[conversation]}"
    from belle_agent.integrations.opencode.sessions import session_id

    return f"/api/session/{session_id(backend._namespace, conversation)}"


async def on_harness(kit: Any, path: str) -> bool:
    """Whether the harness itself still has the session at ``path``. A harness answers 404 for one it lost."""
    backend = kit.backend
    if isinstance(backend, KeepingBackend):
        return path in backend.kept
    try:
        await backend._wire.call("GET", path)
    except Exception as exc:  # noqa: BLE001 - each wire names its own error, and both have the status
        assert getattr(exc, "status", None) == 404, exc
        return False
    return True


def later(host: Any, hours: int) -> None:
    clock = FakeClock(datetime.now(UTC) + timedelta(hours=hours))
    host.scheduler.clock = host.scheduler.store._clock = host.scheduler.expiry.clock = clock


async def test_expiry_keeps_attached_session(kit, tmp_path):  # type: ignore[no-untyped-def]
    kit = _kit(kit)
    async with worlds(kit, tmp_path) as build:
        world = await build()
        host, sessions, schedule = world.host, world.host.session_toolkit, world.host.schedule_toolkit
        direct = await world.platform.open_direct_message("U1")
        at = InboundMessage("loopback", "", direct, "m0", "U1", "", event_id="e0", chat_type="dm", trigger=TRIGGER_DM)

        def event(name: str) -> InboundMessage:
            return replace(at, message_id=name, event_id=name)

        kit.play(*(ModelTurn(final=f"done {number}") for number in range(4)))
        await world.send(world.direct("Say done."))
        chat = world.requests[-1].conversation
        started = json.loads(await sessions.invoke_at("session_start", {"prompt": "Say done.", "notify": False},
                                                       at=event("start")))
        assert started["ok"], started
        await host.idle()
        for mode in ("fresh", "shared"):
            made = json.loads(await schedule.invoke_at("schedule_create", {
                "name": mode, "prompt": "Say done.", "when": {"kind": "once", "after_seconds": 600}, "session": mode,
            }, at=event(f"create-{mode}")))
            assert json.loads(await schedule.invoke_at("schedule_run_now", {"job_id": made["job"]["job_id"]},
                                                       at=event(f"run-{mode}")))["ok"]
            await host.idle()
            # A deleted job no longer holds its shared session: the session follows the time rule.
            assert json.loads(await schedule.invoke_at(
                "schedule_delete", {"job_id": made["job"]["job_id"], "keep_history": True}, at=event(f"delete-{mode}"),
            ))["ok"]
        async with host.scheduler.store.transaction() as snapshot:
            owned = [f"belle_session_{session_id}" for session_id in snapshot.sessions]
        assert len(owned) == 3
        paths = [where(kit, conversation) for conversation in owned]
        chat_path = where(kit, chat)
        assert all([await on_harness(kit, path) for path in paths]) and await on_harness(kit, chat_path)

        later(host, 71)
        await host.scheduler.expiry.tick()
        assert all([await on_harness(kit, path) for path in paths])
        later(host, 73)
        await host.scheduler.expiry.tick()
        assert not any([await on_harness(kit, path) for path in paths])
        assert await on_harness(kit, chat_path)
        async with host.scheduler.store.transaction() as snapshot:
            assert snapshot.sessions == {} and snapshot.deleted == {}


async def _detached_sessions(world: Any) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Start a detached session and a schedule run in one direct message, and wait for both.

    Return the function that calls a session tool there with a message key, the started session and the run.
    """
    host, sessions, schedule = world.host, world.host.session_toolkit, world.host.schedule_toolkit
    direct = await world.platform.open_direct_message("U1")
    at = InboundMessage("loopback", "", direct, "m0", "U1", "", event_id="e0", chat_type="dm", trigger=TRIGGER_DM)

    async def call(name: str, arguments: dict[str, Any], key: str) -> dict[str, Any]:
        return json.loads(await sessions.invoke_at(name, arguments, at=replace(at, message_id=key, event_id=key)))

    first = await call("session_start", {"prompt": "Remember the word MARKER-ONE.", "notify": False}, "s1")
    made = json.loads(await schedule.invoke_at("schedule_create", {
        "name": "Fresh", "prompt": "Say done.", "when": {"kind": "once", "after_seconds": 600},
    }, at=at))
    run = json.loads(await schedule.invoke_at("schedule_run_now", {"job_id": made["job"]["job_id"]}, at=at))["run"]
    await host.idle()
    return call, first, run


async def test_continuation_across_retention(kit, tmp_path):  # type: ignore[no-untyped-def]
    kit = _kit(kit)
    async with worlds(kit, tmp_path) as build:
        world = await build()
        host = world.host
        kit.play(*(ModelTurn(final=f"done {number}") for number in range(4)))
        call, first, run = await _detached_sessions(world)
        for session_id in (first["session_id"], run["session_id"]):
            again = await call("session_continue", {"session_id": session_id, "prompt": "Which word? MARKER-TWO."},
                               f"c-{session_id}")
            assert again["ok"], again
            await host.idle()
            async with host.scheduler.store.transaction() as snapshot:
                runs = snapshot.sessions[session_id]["runs"]
            assert [item["state"] for item in runs] == ["succeeded", "succeeded"], runs
        if hasattr(kit, "model"):
            asked = [json.dumps(entry["body"]) for entry in list(kit.model.log) if "MARKER-TWO" in json.dumps(entry["body"])]
            assert any("MARKER-ONE" in body for body in asked)

        later(host, 73)
        expired = await call("session_continue", {"session_id": run["session_id"], "prompt": "Too late."}, "c3")
        assert expired["error"] == "session_expired", expired


async def test_a_session_gone_from_the_harness_or_the_store_is_not_continued(kit, tmp_path):  # type: ignore[no-untyped-def]
    kit = _kit(kit)
    async with worlds(kit, tmp_path) as build:
        world = await build()
        host = world.host
        kit.play(*(ModelTurn(final=f"done {number}") for number in range(2)))
        call, first, run = await _detached_sessions(world)

        # Deleted on the harness by other means: a continuation is refused, and no new conversation is made.
        await host._backend.forget(f"belle_session_{first['session_id']}")
        missing = await call("session_continue", {"session_id": first["session_id"], "prompt": "Again."}, "c2")
        assert missing["error"] == "backend_conversation_unavailable", missing

        later(host, 73)
        await host.scheduler.expiry.tick()
        gone = await call("session_continue", {"session_id": run["session_id"], "prompt": "Too late."}, "c4")
        assert gone["error"] == "session_not_found", gone


async def test_chat_turn_of_a_stopped_host(kit, tmp_path):  # type: ignore[no-untyped-def]
    """A host stops while a chat turn runs, and keeps the turn's record.

    OpenCode runs the turn on, and the next host delivers its answer once. A Docker Agent run ends with its stream,
    and the scripted backend ends a turn whose reader leaves: the next host tells the message that the answer is lost.
    """
    kit = _kit(kit)
    async with worlds(kit, tmp_path) as build:
        kit.play(ModelTurn(final="answer after restart", hold=True))
        first = await build()
        await first.platform.inject(first.direct("hello"))
        await kit.holding()
        key = first.requests[-1].conversation
        await first.host.stop()
        async with AttachedSessionStore(tmp_path / "state").transaction() as data:
            assert data["sessions"][key]["running"]["request_id"] == first.requests[-1].request_id
        recovers = OP_RECOVER in kit.backend.capabilities().operations
        kit.restart()
        second = await build()
        kit.release()
        # The host watches a recovered turn until it ends.
        await second.host.idle()
        assert first.posts() == [] and second.requests == []
        assert [post.text for post in second.posts()] == (["answer after restart"] if recovers else [RESTART_LOST_NOTICE])
        async with AttachedSessionStore(tmp_path / "state").transaction() as data:
            assert data["sessions"] == {}


async def test_session_run_of_a_stopped_host(kit, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A host stops while a detached session runs. The run follows the rule of a chat turn."""
    kit = _kit(kit)
    # A real platform still has the direct message after a restart. A loopback platform opens it again.
    started = LoopbackPlatform.start

    async def start(platform: LoopbackPlatform, *args: Any) -> None:
        await platform.open_direct_message("U1")
        await started(platform, *args)

    monkeypatch.setattr(LoopbackPlatform, "start", start)
    async with worlds(kit, tmp_path) as build:
        kit.play(ModelTurn(final="result after restart", hold=True))
        first = await build()
        at = InboundMessage("loopback", "", "dm:U1", "m0", "U1", "", event_id="e0", chat_type="dm", trigger=TRIGGER_DM)
        begun = json.loads(await first.host.session_toolkit.invoke_at(
            "session_start", {"prompt": "Say the result.", "notify": False}, at=at))
        assert begun["ok"], begun
        await kit.holding()
        await first.host.stop()
        recovers = OP_RECOVER in kit.backend.capabilities().operations
        kit.restart()
        second = await build()
        kit.release()
        await second.host.idle()
        async with second.host.scheduler.store.transaction() as snapshot:
            ended = snapshot.sessions[begun["session_id"]]["runs"][-1]
        assert (ended["state"], ended["result"]) == (("succeeded", "result after restart") if recovers
                                                     else ("interrupted", "")), ended
        assert second.requests == []


async def test_chat_turn_outlives_the_host_that_started_it(kit, tmp_path):  # type: ignore[no-untyped-def]
    """A process started a chat turn and crashed. The next host delivers the answer once, or stops the turn and says so.

    A crash cannot go through a stop, so the case writes the record that the crashed process left.
    """
    kit = _kit(kit)
    key = "loopback_W1_D-U1_U1"
    async with worlds(kit, tmp_path) as build:
        kit.play(ModelTurn(final="answer after restart", hold=True))
        # The earlier process: its own backend object starts the turn, and nothing delivers it.
        backend = kit.backend
        if not isinstance(backend, KeepingBackend):
            await backend.start(ToolHost([]))
        earlier = asyncio.create_task(_drain(backend.run_turn(TurnRequest(conversation=key, request_id="e1", text="hello"))))
        await kit.holding()
        recovers = OP_RECOVER in backend.capabilities().operations
        if recovers:
            # The earlier process stops without a word to OpenCode: only its listeners go, to free their ports.
            for name in ("_tool_server", "_relay"):
                listener = getattr(backend, name)
                setattr(backend, name, None)
                if listener is not None:
                    await listener.close()
        else:
            # Docker Agent ends a run with the request that streams it, so a stopped process ends its run.
            earlier.cancel()
            await asyncio.gather(earlier, return_exceptions=True)
            if not isinstance(backend, KeepingBackend):
                await backend.close()
        kit.restart()
        inbound = {"platform": "loopback", "workspace": "W1", "conversation": "D-U1", "message_id": "m1",
                   "sender": "U1", "text": "hello", "event_id": "e1", "thread": "", "chat_type": "dm",
                   "trigger": TRIGGER_DM, "reply_thread": None, "envelope": "", "side": "", "skills_required": [],
                   "reply_optional": False}
        # The turn started within the turn timeout, which counts from its start.
        started = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        async with AttachedSessionStore(tmp_path / "state").transaction() as data:
            data["sessions"][key] = {
                "key": key, "backend": backend.capabilities().backend, "last_turn_ended_iso_utc": "",
                "deletion": {"state": "none", "claimed_by": "", "attempts": 0, "last_error": "", "last_attempt_iso_utc": ""},
                "running": {"request_id": "e1", "inbound": inbound, "started_iso_utc": started,
                            "owner": "a stopped process", "delivering": False},
            }
        world = await build()
        try:
            if recovers:
                kit.release()
                await world.host.idle()
                assert [post.text for post in world.posts()] == ["answer after restart"]
                assert world.requests == []
            else:
                # Docker Agent and the scripted backend cannot recover a turn, so the host stops it and says so.
                kit.release()
                await world.host.idle()
                assert [post.text for post in world.posts()] == [RESTART_LOST_NOTICE]
            async with AttachedSessionStore(tmp_path / "state").transaction() as data:
                assert data["sessions"] == {}
        finally:
            earlier.cancel()
            await asyncio.gather(earlier, return_exceptions=True)
            if backend is not kit.backend:
                await backend.close()  # A second call does nothing, and the first one closes the earlier wire.


async def _drain(events: Any) -> None:
    async for _ in events:
        pass


async def test_idle_attached_session_expiry(kit, tmp_path):  # type: ignore[no-untyped-def]
    """attached_sessions.expire_after_idle_hours is off by default. Set, it deletes an idle attached session."""
    kit = _kit(kit)
    async with worlds(kit, tmp_path) as build:
        kit.play(ModelTurn(final="noted"), ModelTurn(final="noted again"), ModelTurn(final="fresh start"))
        plain = await build()
        await plain.send(plain.direct("Remember the word MARKER-IDLE."))
        async with AttachedSessionStore(tmp_path / "state").transaction() as data:
            assert data["sessions"] == {}
        await plain.host.stop()
        kit.restart()

        world = await build("attached_sessions: {expire_after_idle_hours: 1}")
        # The handled message ids outlive the restart, so the new messages take new ones.
        world._ids = plain._ids
        await world.send(world.direct("Remember the word MARKER-IDLE again."))
        chat = world.requests[-1].conversation
        path = where(kit, chat)
        assert await on_harness(kit, path)
        await world.host.expire_attached_sessions(datetime.now(UTC) + timedelta(minutes=59))
        assert await on_harness(kit, path)
        await world.host.expire_attached_sessions(datetime.now(UTC) + timedelta(hours=1, minutes=1))
        assert not await on_harness(kit, path)
        async with AttachedSessionStore(tmp_path / "state").transaction() as data:
            assert data["sessions"] == {}
        # The next message in the place starts with an empty conversation.
        await world.send(world.direct("Which word?"))
        assert world.posts()[-1].text == "fresh start"
        if hasattr(kit, "model"):
            last = json.dumps(list(kit.model.log)[-1]["body"])
            assert "Which word?" in last and "MARKER-IDLE" not in last
