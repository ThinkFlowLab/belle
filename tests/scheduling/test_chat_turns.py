"""A running chat turn is recorded while it runs, so that a restarted host can deliver it or stop it."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conformance_kit import ModelTurn
from scripted_backend import ScriptedBackend

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.backend import (
    EVENT_SUCCEEDED,
    OP_ACTIVE,
    OP_RECOVER,
    RECOVERY_RUNNING,
    RECOVERY_TERMINAL,
    Recovery,
    TurnEvent,
)
from belle_agent.host import RESTART_DELIVERING_NOTICE, RESTART_LOST_NOTICE, ConversationHost, HostedPlatform
from belle_agent.host.attached_sessions import AttachedSession, AttachedSessionStore
from belle_agent.platforms.base import MARK_FAILED, TRIGGER_DM, InboundMessage
from belle_agent.platforms.loopback import LoopbackPlatform
from belle_agent.scheduling.records import RecordInvalid
from belle_agent.scheduling.settings import SchedulingSettings

KEY = "loopback_local_D-U1_U1"
MESSAGE = InboundMessage(platform="loopback", workspace="local", conversation="D-U1", message_id="m1", event_id="e1",
                         sender="U1", text="hello", chat_type="dm", trigger=TRIGGER_DM)


def host_of(tmp_path, backend, platform=None):  # type: ignore[no-untyped-def]
    platform = platform or LoopbackPlatform()
    host = ConversationHost([HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], backend,
                            scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path)
    return host, platform


async def orphan(tmp_path, *, delivering: bool = False, request_id: str = "e1", key: str = KEY,  # type: ignore[no-untyped-def]
                 started: str = "", reply_optional: bool = False, event: bool = False) -> None:
    """Store the running record of a turn that a stopped process started, by default a minute ago."""
    ids = {"message_id": "m1", "event_id": "e1"} if request_id == "e1" else {"message_id": "", "event_id": ""}
    inbound = {"platform": "loopback", "workspace": "local", "conversation": "D-U1", **ids,
               "sender": "U1", "text": "hello", "thread": "", "chat_type": "dm",
               "trigger": TRIGGER_DM, "reply_thread": None, "envelope": "", "side": "", "skills_required": [],
               "reply_optional": reply_optional}
    started = started or (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    async with AttachedSessionStore(tmp_path).transaction() as data:
        data["sessions"][key] = {"key": key, "backend": "scripted", "last_turn_ended_iso_utc": "", "deletion": {"state": "none", "claimed_by": "", "attempts": 0, "last_error": "", "last_attempt_iso_utc": ""}, "running": {
            "request_id": request_id, "inbound": None if event else inbound, "started_iso_utc": started, "owner": "gone",
            "delivering": delivering}}


async def live_elsewhere(tmp_path, owner: str = "gone") -> None:  # type: ignore[no-untyped-def]
    """Record a heartbeat of ``owner`` on another host: its turns count as live for 90 s."""
    store = AttachedSessionStore(tmp_path)
    await store.beat(owner, datetime.now(UTC))
    async with store.transaction() as data:
        data["owners"][owner]["host"] = "another-host"


async def gone(tmp_path, owner: str = "gone") -> None:  # type: ignore[no-untyped-def]
    """Age the heartbeat of ``owner``, as when its container went away."""
    async with AttachedSessionStore(tmp_path).transaction() as data:
        data["owners"][owner]["seen_iso_utc"] = "2026-10-18T00:00:00Z"


def posts(platform) -> list[str]:  # type: ignore[no-untyped-def]
    return [entry.text for entry in platform.log if entry.kind == "post"]


async def records(tmp_path) -> dict:  # type: ignore[no-untyped-def,type-arg]
    async with AttachedSessionStore(tmp_path).transaction() as data:
        return dict(data["sessions"])


def test_chat_record_refuses_session_fields() -> None:
    record = {"key": KEY, "backend": "scripted", "running": None, "last_turn_ended_iso_utc": "", "deletion": {"state": "none", "claimed_by": "", "attempts": 0, "last_error": "", "last_attempt_iso_utc": ""}}
    assert AttachedSession.from_json(record, "c").key == KEY
    for field in ("owned_conversation", "session_id"):
        with pytest.raises(RecordInvalid, match=f"unknown keys {field}"):
            AttachedSession.from_json({**record, field: "belle_session_ses-0000000001"}, "c")
    with pytest.raises(RecordInvalid, match="c.key must not start with belle_session_"):
        AttachedSession.from_json({**record, "key": "belle_session_ses-0000000001"}, "c")


async def test_running_turn_record_lives_only_while_running(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = ScriptedBackend()
    backend.play(ModelTurn(final="answer", hold=True))
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await platform.inject(MESSAGE)
        await backend.holding()
        running = (await records(tmp_path))[KEY]["running"]
        assert running["request_id"] == "e1" and running["inbound"]["text"] == "hello"
        assert running["delivering"] is False
        backend.release()
        await host.idle()
        assert await records(tmp_path) == {}
        assert [entry.text for entry in platform.log if entry.kind == "post"] == ["answer"]
    finally:
        await host.stop()


class Recovering(ScriptedBackend):
    def __init__(self, *operations: str) -> None:
        super().__init__()
        # The scripted backend lists its running turns. These cases name OP_ACTIVE where they need it.
        base = self._capabilities.operations - {OP_ACTIVE}
        self._capabilities = replace(self._capabilities, operations=base | set(operations))
        self.recovered: list[str] = []
        self.cancelled: list[str] = []

    async def active(self) -> list[str]:
        return [KEY]

    async def cancel(self, conversation: str) -> bool:
        self.cancelled.append(conversation)
        return True

    async def recover(self, request):  # type: ignore[no-untyped-def]
        self.recovered.append(request.request_id)
        return Recovery(RECOVERY_TERMINAL, TurnEvent(EVENT_SUCCEEDED, text="answer after restart"))


async def test_the_next_host_delivers_a_turn_that_a_stopped_host_left(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = Recovering(OP_RECOVER, OP_ACTIVE)
    backend.play(ModelTurn(final="answer", hold=True))
    host, platform = host_of(tmp_path, backend)
    await host.start()
    await platform.inject(MESSAGE)
    await backend.holding()
    await host.stop()
    assert (await records(tmp_path))[KEY]["running"]["request_id"] == "e1"
    restarted = Recovering(OP_RECOVER, OP_ACTIVE)
    host, platform = host_of(tmp_path, restarted)
    await host.start()
    try:
        await host.idle()
        assert restarted.recovered == ["e1"] and restarted.cancelled == []
        assert [entry.text for entry in platform.log if entry.kind == "post"] == ["answer after restart"]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_a_host_stop_tells_the_backend_before_it_leaves_a_turn(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Leaving(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__()
            self.left_after_shutdown: list[bool] = []

        async def run_turn(self, request):  # type: ignore[no-untyped-def]
            try:
                async for event in super().run_turn(request):
                    yield event
            finally:
                self.left_after_shutdown.append(self.shut_down)

    backend = Leaving()
    backend.play(ModelTurn(final="answer", hold=True))
    host, platform = host_of(tmp_path, backend)
    await host.start()
    await platform.inject(MESSAGE)
    await backend.holding()
    await host.stop()
    assert backend.left_after_shutdown == [True] and backend.stopped == []


async def test_a_turn_that_ended_with_its_process_is_not_stopped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path)

    class Ended(Recovering):
        async def active(self) -> list[str]:
            return []

    backend = Ended(OP_ACTIVE)
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert backend.cancelled == [] and await records(tmp_path) == {}
        assert posts(platform) == [RESTART_LOST_NOTICE] and platform.marks == [("m1", MARK_FAILED)]
    finally:
        await host.stop()


async def test_the_next_host_tells_a_message_whose_turn_a_restart_lost(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Ended(Recovering):
        async def active(self) -> list[str]:
            return []

    backend = Ended(OP_ACTIVE)
    backend.play(ModelTurn(final="answer", hold=True))
    host, platform = host_of(tmp_path, backend)
    await host.start()
    await platform.inject(MESSAGE)
    await backend.holding()
    await host.stop()
    assert posts(platform) == [] and platform.marks[-1] != ("m1", MARK_FAILED)
    host, platform = host_of(tmp_path, Ended(OP_ACTIVE))
    await host.start()
    try:
        await host.idle()
        assert posts(platform) == [RESTART_LOST_NOTICE] and platform.marks == [("m1", MARK_FAILED)]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_a_lost_turn_under_an_optional_reply_is_only_marked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path, reply_optional=True)
    backend = Recovering()
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert posts(platform) == [] and platform.marks == [("m1", MARK_FAILED)]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_a_lost_event_turn_tells_nobody(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path, event=True)
    backend = Recovering(OP_RECOVER, OP_ACTIVE)
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert backend.recovered == [] and backend.cancelled == [KEY]
        assert posts(platform) == [] and platform.marks == []
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_a_turn_with_a_request_id_from_the_clock_recovers_by_that_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path, request_id="loopback-1760745600000")
    backend = Recovering(OP_RECOVER, OP_ACTIVE)
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert backend.recovered == ["loopback-1760745600000"] and backend.cancelled == []
        assert [entry.text for entry in platform.log if entry.kind == "post"] == ["answer after restart"]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_a_listed_orphan_without_recovery_is_stopped_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path)
    backend = Recovering(OP_ACTIVE)
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host._attached_sessions_pass()
        await host.idle()
        assert backend.cancelled == [KEY]
        assert await records(tmp_path) == {}
        assert posts(platform) == [RESTART_LOST_NOTICE] and platform.marks == [("m1", MARK_FAILED)]
    finally:
        await host.stop()


async def test_a_turn_that_was_delivering_is_not_delivered_again(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path, delivering=True)
    backend = Recovering(OP_RECOVER, OP_ACTIVE)
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert backend.recovered == [] and posts(platform) == [RESTART_DELIVERING_NOTICE]
        assert platform.marks == [("m1", MARK_FAILED)]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_orphan_chat_turn_is_stopped_where_the_backend_lists_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path)
    backend = Recovering()
    assert OP_ACTIVE not in backend.capabilities().operations
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert backend.cancelled == [KEY]
        assert await records(tmp_path) == {}
        assert posts(platform) == [RESTART_LOST_NOTICE] and platform.marks == [("m1", MARK_FAILED)]
    finally:
        await host.stop()


async def test_a_live_owner_keeps_its_turn(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path)
    await live_elsewhere(tmp_path)
    backend = Recovering(OP_ACTIVE)
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        assert backend.cancelled == [] and KEY in await records(tmp_path)
        # The owner's container went away: its heartbeat grows old, and a later pass takes its turn.
        await gone(tmp_path)
        await host._attached_sessions_pass()
        await host.idle()
        assert backend.cancelled == [KEY] and await records(tmp_path) == {}
        assert posts(platform) == [RESTART_LOST_NOTICE]
    finally:
        await host.stop()


async def test_a_message_waits_for_the_turn_of_a_live_owner_and_keeps_its_record(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path)
    await live_elsewhere(tmp_path)
    backend = Recovering(OP_RECOVER, OP_ACTIVE)
    backend.play(ModelTurn(final="second answer"))
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        outcome = await platform.inject(replace(MESSAGE, message_id="m2", event_id="e2", text="are you there?"))
        assert outcome.startswith("queued:")
        assert (await records(tmp_path))[KEY]["running"]["request_id"] == "e1"
        await gone(tmp_path)
        await host._attached_sessions_pass()
        await host.idle()
        assert backend.recovered == ["e1"] and backend.cancelled == []
        assert posts(platform) == ["answer after restart", "second answer"]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_a_message_at_the_platform_start_waits_for_the_recovered_turn(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Busy(LoopbackPlatform):
        """A platform whose first message arrives as it connects."""

        async def start(self, on_inbound) -> None:  # type: ignore[no-untyped-def]
            await super().start(on_inbound)
            self.first = await self.inject(replace(MESSAGE, message_id="m2", event_id="e2", text="are you there?"))

    await orphan(tmp_path)
    backend = Recovering(OP_RECOVER, OP_ACTIVE)
    backend.play(ModelTurn(final="second answer"))
    host, platform = host_of(tmp_path, backend, Busy())
    await host.start()
    try:
        await host.idle()
        assert platform.first.startswith("queued:")
        assert backend.recovered == ["e1"] and posts(platform) == ["answer after restart", "second answer"]
    finally:
        await host.stop()


async def test_a_record_holding_another_processs_turn_is_not_replaced(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.host.attached_sessions import RunningTurn

    await orphan(tmp_path)
    store = AttachedSessionStore(tmp_path)
    mine = RunningTurn("e2", None, "2026-10-18T00:00:01Z", "me", False)
    assert not await store.started(KEY, "scripted", mine, datetime.now(UTC))
    assert (await records(tmp_path))[KEY]["running"]["request_id"] == "e1"
    # A stopped process of another backend left it: no host of this backend can recover it.
    assert await store.started(KEY, "opencode", mine, datetime.now(UTC))
    assert (await records(tmp_path))[KEY]["running"]["request_id"] == "e2"


async def test_a_record_with_a_malformed_message_is_refused_by_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.scheduling.store import StoreUnreadable

    await orphan(tmp_path)
    data = json.loads((tmp_path / "attached_sessions.json").read_text(encoding="utf-8"))
    del data["sessions"][KEY]["running"]["inbound"]["side"]
    (tmp_path / "attached_sessions.json").write_text(json.dumps(data), encoding="utf-8")
    host, _ = host_of(tmp_path, Recovering(OP_RECOVER, OP_ACTIVE))
    with pytest.raises(StoreUnreadable, match=f"sessions.{KEY}.running.inbound misses side"):
        await host.start()


async def test_a_turn_that_cannot_be_settled_goes_back_and_the_others_recover(tmp_path) -> None:  # type: ignore[no-untyped-def]
    other = "loopback_local_D-U2_U2"
    await orphan(tmp_path)
    await orphan(tmp_path, key=other)

    class Refusing(Recovering):
        def __init__(self) -> None:
            super().__init__(OP_ACTIVE)
            self.failures = 1

        async def active(self) -> list[str]:
            return [KEY, other]

        async def cancel(self, conversation: str) -> bool:
            if conversation == KEY and self.failures:
                self.failures -= 1
                raise RuntimeError("the harness answered 503")
            return await super().cancel(conversation)

    backend = Refusing()
    backend.play(ModelTurn(final="second answer"))
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert backend.cancelled == [other] and list(await records(tmp_path)) == [KEY]
        assert (await records(tmp_path))[KEY]["running"]["owner"] == "gone"
        outcome = await platform.inject(replace(MESSAGE, message_id="m2", event_id="e2", text="are you there?"))
        assert outcome.startswith("queued:")
        await host._attached_sessions_pass()
        await host.idle()
        # The message that waited is answered after the notice of the turn it waited for.
        assert backend.cancelled == [other, KEY]
        assert posts(platform) == [RESTART_LOST_NOTICE, RESTART_LOST_NOTICE, "second answer"]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_a_recovered_turn_past_the_turn_timeout_is_stopped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.host import TURN_TIMEOUT_NOTICE

    class Running(Recovering):
        async def recover(self, request):  # type: ignore[no-untyped-def]
            self.recovered.append(request.request_id)
            return Recovery(RECOVERY_RUNNING)

    await orphan(tmp_path, started=(datetime.now(UTC) - timedelta(minutes=61)).isoformat())
    backend = Running(OP_RECOVER, OP_ACTIVE)
    host, platform = host_of(tmp_path, backend)
    await host.start()
    try:
        await host.idle()
        assert backend.recovered == ["e1"] and backend.cancelled == [KEY]
        assert TURN_TIMEOUT_NOTICE in "".join(posts(platform))
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


def test_store_refuses_a_malformed_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "attached_sessions.json").write_text(json.dumps({"version": 1, "owners": {}, "sessions": {
        KEY: {"key": "other"}}}), encoding="utf-8")
    from belle_agent.scheduling.store import StoreUnreadable

    with pytest.raises(StoreUnreadable, match="sessions"):
        AttachedSessionStore(tmp_path)._read()
    (tmp_path / "attached_sessions.json").write_text(json.dumps({"version": 1, "owners": {"one": {"pid": "1"}},
                                                                 "sessions": {}}), encoding="utf-8")
    with pytest.raises(StoreUnreadable, match="owners.one must have exactly the integer pid"):
        AttachedSessionStore(tmp_path)._read()


class Forgetting(Recovering):
    """A harness that deletes conversations."""

    def __init__(self) -> None:
        from belle_agent.backend import OP_FORGET

        super().__init__(OP_FORGET)
        self.forgotten: list[str] = []
        self.failures: list[Exception] = []

    async def forget(self, conversation: str) -> None:
        self.forgotten.append(conversation)
        if self.failures:
            raise self.failures.pop(0)


def idle_host(tmp_path, backend, hours=None):  # type: ignore[no-untyped-def]
    from belle_agent.host.attached_sessions import AttachedSessionSettings

    platform = LoopbackPlatform()
    host = ConversationHost([HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], backend,
                            scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
                            attached_sessions=AttachedSessionSettings(hours))
    return host, platform


async def test_idle_attached_session_expires(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    backend = Forgetting()
    backend.play(ModelTurn(final="answer"))
    backend.failures.append(RuntimeError("the harness answered 503"))
    host, platform = idle_host(tmp_path, backend, 24)
    await host.start()
    try:
        await platform.inject(MESSAGE)
        await host.idle()
        record = (await records(tmp_path))[KEY]
        assert record["running"] is None and record["last_turn_ended_iso_utc"]
        now = datetime.now(UTC)
        await host.expire_attached_sessions(now + timedelta(hours=23))
        assert backend.forgotten == []
        await host.expire_attached_sessions(now + timedelta(hours=25))
        deletion = (await records(tmp_path))[KEY]["deletion"]
        assert deletion["attempts"] == 1 and "503" in deletion["last_error"] and deletion["claimed_by"] == ""
        await host.expire_attached_sessions(now + timedelta(hours=25))
        assert backend.forgotten == [KEY, KEY]
        assert await records(tmp_path) == {}
    finally:
        await host.stop()


async def test_disabled_keeps_all(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = Forgetting()
    backend.play(ModelTurn(final="answer"))
    host, platform = idle_host(tmp_path, backend)
    await host.start()
    try:
        await platform.inject(MESSAGE)
        await host.idle()
        assert await records(tmp_path) == {}
        assert backend.forgotten == []
    finally:
        await host.stop()


def test_unquoted_off_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.config import BelleConfigError, load_config
    from belle_agent.host.attached_sessions import attached_session_settings

    path = tmp_path / "config.yaml"
    for value in ("off", "no", "0", "1.5"):
        path.write_text(f"attached_sessions:\n  expire_after_idle_hours: {value}\n", encoding="utf-8")
        with pytest.raises(BelleConfigError, match="expire_after_idle_hours must be disabled or a whole number"):
            load_config(path)
    assert attached_session_settings({}).expire_after_idle_hours is None
    assert attached_session_settings({"attached_sessions": {"expire_after_idle_hours": "disabled"}}).expire_after_idle_hours is None
    assert attached_session_settings({"attached_sessions": {"expire_after_idle_hours": 72}}).expire_after_idle_hours == 72


async def test_a_backend_that_cannot_delete_refuses_the_setting(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.config import BelleConfigError

    host, _ = idle_host(tmp_path, ScriptedBackend(), 24)
    with pytest.raises(BelleConfigError, match="backend scripted does not. Set it to disabled"):
        await host.start()


async def test_a_store_error_in_a_pass_keeps_the_heartbeat(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = Forgetting()
    host, _ = idle_host(tmp_path, backend, 1)
    await host.start()
    try:
        path = tmp_path / "attached_sessions.json"
        kept = path.read_text(encoding="utf-8")
        path.write_text("{", encoding="utf-8")
        await host._attached_sessions_pass()
        path.write_text(kept, encoding="utf-8")
        await host._attached_sessions_pass()
        async with AttachedSessionStore(tmp_path).transaction() as data:
            assert host._owner in data["owners"]
    finally:
        await host.stop()


async def test_a_recovery_keeps_the_record_for_idle_expiry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    await orphan(tmp_path)
    backend = Forgetting()
    host, _ = idle_host(tmp_path, backend, 24)
    await host.start()
    try:
        assert backend.cancelled == [KEY]
        record = (await records(tmp_path))[KEY]
        assert record["running"] is None and record["last_turn_ended_iso_utc"]
    finally:
        await host.stop()


async def idle_record(tmp_path, backend: str = "scripted") -> None:  # type: ignore[no-untyped-def]
    """Store the record of an attached session whose last turn ended long ago."""
    async with AttachedSessionStore(tmp_path).transaction() as data:
        data["sessions"][KEY] = {"key": KEY, "backend": backend, "running": None,
                                 "last_turn_ended_iso_utc": "2026-10-18T00:00:00Z", "deletion": {
                                     "state": "none", "claimed_by": "", "attempts": 0, "last_error": "",
                                     "last_attempt_iso_utc": ""}}


async def test_another_backends_attached_session_is_claimed_and_logged_once(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    await idle_record(tmp_path, "docker_agent")
    backend = Forgetting()
    first, _ = idle_host(tmp_path, backend, 1)
    second, _ = idle_host(tmp_path, backend, 1)
    with caplog.at_level("WARNING", logger="belle_agent.scheduling.expiry"):
        await first.start()
        await second.start()
        try:
            await first.expire_attached_sessions()
            await first.expire_attached_sessions()
            await second.expire_attached_sessions()
        finally:
            await first.stop()
            await second.stop()
    deletion = (await records(tmp_path))[KEY]["deletion"]
    assert backend.forgotten == [] and deletion["state"] == "blocked" and deletion["attempts"] == 2
    assert "on backend docker_agent, and Belle runs on scripted" in deletion["last_error"]
    assert caplog.text.count("stays in the store") == 1


async def test_an_attached_session_whose_turn_runs_here_stays(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = Forgetting()
    backend.play(ModelTurn(final="answer", hold=True))
    host, platform = idle_host(tmp_path, backend, 1)
    await host.start()
    try:
        await platform.inject(MESSAGE)
        await backend.holding()
        # The record says idle while this process runs a turn on the key, as after a record that could not be written.
        await idle_record(tmp_path)
        await host.expire_attached_sessions()
        assert backend.forgotten == []
        assert (await records(tmp_path))[KEY]["deletion"]["state"] == "none"
        backend.release()
        await host.idle()
    finally:
        await host.stop()


async def test_a_message_during_a_deletion_waits_for_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    class Slow(Forgetting):
        def __init__(self) -> None:
            super().__init__()
            self.entered, self.leave = asyncio.Event(), asyncio.Event()

        async def forget(self, conversation: str) -> None:
            self.entered.set()
            await self.leave.wait()
            await super().forget(conversation)

    await idle_record(tmp_path)
    backend = Slow()
    backend.play(ModelTurn(final="answer"))
    host, platform = idle_host(tmp_path, backend, 1)
    await host.start()
    try:
        deleting = asyncio.create_task(host.expire_attached_sessions())
        await backend.entered.wait()
        assert (await platform.inject(MESSAGE)).startswith("queued:")
        backend.leave.set()
        await deleting
        await host.idle()
        assert backend.forgotten == [KEY] and posts(platform) == ["answer"]
        record = (await records(tmp_path))[KEY]
        assert record["running"] is None and record["deletion"]["state"] == "none"
    finally:
        await host.stop()
