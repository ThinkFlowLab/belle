"""Detached turns store their result and report through their origin platform."""

import json
from dataclasses import replace

import pytest
from conformance_kit import ModelTurn, RecordedBackend
from scripted_backend import ScriptedBackend

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.backend import (
    EVENT_SUCCEEDED,
    OP_CONTINUE,
    OP_FORGET,
    OP_RECOVER,
    RECOVERY_RUNNING,
    RECOVERY_TERMINAL,
    Recovery,
    TurnEvent,
)
from belle_agent.host import ConversationHost, HostedPlatform
from belle_agent.platforms.base import TRIGGER_DM, InboundMessage
from belle_agent.platforms.loopback import LoopbackPlatform
from belle_agent.scheduling.records import NO_DELETION, DetachedSession, RequestStart, new_run
from belle_agent.platforms.loopback.platform import LoopbackToolkit
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore
from belle_agent.scopes import LEVEL_DENY


class RecoverBackend(ScriptedBackend):
    def __init__(self, conversation: str, *, running_first: bool = False) -> None:
        super().__init__()
        self.conversation = conversation
        self._capabilities = replace(self._capabilities, operations=self._capabilities.operations | {OP_RECOVER})
        self.cancelled: list[str] = []
        self.running_first = running_first
        self.recoveries = 0

    async def active(self) -> list[str]:
        return [self.conversation]

    async def cancel(self, conversation: str) -> bool:
        self.cancelled.append(conversation)
        return True

    async def recover(self, request) -> Recovery:  # type: ignore[no-untyped-def]
        assert request.metadata["belle_inbound"]["message_id"] == "original-recovery-message"
        assert request.request_id != "original-recovery-message"
        assert "recovery envelope" in request.text
        assert request.conversation == self.conversation
        self.recoveries += 1
        if self.running_first and self.recoveries == 1:
            return Recovery(RECOVERY_RUNNING)
        return Recovery(RECOVERY_TERMINAL, TurnEvent(EVENT_SUCCEEDED, text="stored final"))


class RetainedBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__()
        self._capabilities = replace(self._capabilities, operations=self._capabilities.operations | {OP_CONTINUE})
        self.retained_conversations: set[str] = set()

    async def retained(self, conversation: str) -> bool:
        return conversation in self.retained_conversations


def detached(conversation: str = "D-U1", *, thread: str = "", notify: bool = True, inbound: dict | None = None,
             chat_type: str = "dm", **run: object) -> DetachedSession:  # type: ignore[type-arg]
    origin = {"platform": "loopback", "workspace": "local", "conversation": conversation, "thread": thread}
    request = RequestStart("trusted-event", "fingerprint", inbound or {}, "2026-10-18T00:00:00Z", None)
    first = new_run(started_by="request", prompt="work", turn={}, questions="refuse", place=origin,
                    delivery="notice" if notify else "store", owner="", request=request)
    return DetachedSession("ses-1234567890", "standalone", "", "", "scripted", origin, chat_type, "U1",
                   "2026-10-18T00:00:00Z", NO_DELETION, [replace(first, **run)])  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("notify", [True, False])
async def test_detached_session_stores_result_and_reports_when_requested(tmp_path, notify: bool) -> None:  # type: ignore[no-untyped-def]
    kit = ScriptedBackend()
    kit.play(ModelTurn(final="the result"))
    platform = LoopbackPlatform()
    backend = RecordedBackend(kit.backend)
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], backend,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert host.scheduler is not None
        record = detached(notify=notify)
        await host.scheduler.request_session(record)
        await host.idle()
        async with host.scheduler.store.transaction() as snapshot:
            stored = snapshot.sessions[record.session_id]["runs"][0]
            assert stored["state"] == "succeeded", stored["error"]
            assert stored["result"] == "the result"
            assert stored["report"]["state"] == ("sent" if notify else "none")
        posts = [entry.text for entry in platform.log if entry.kind == "post"]
        assert len(posts) == int(notify)
        if notify:
            assert "the result" not in posts[0] and record.session_id in posts[0]
        assert backend.requests[0].conversation == record.owned_conversation
        assert backend.requests[0].request_id == record.runs[0].run_id
        # The session's lock goes with its last run: the host keeps no lock for each session it ever ran.
        assert host._session_locks == {}
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_a_notice_whose_post_fails_lets_its_session_expire(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.scheduling.expiry import expires_iso_utc

    kit = ScriptedBackend()
    kit.play(ModelTurn(final="the result"))
    platform = LoopbackPlatform()

    async def refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("the platform is down")

    platform.post = refuse  # type: ignore[method-assign]
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], RecordedBackend(kit.backend),
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert host.scheduler is not None
        record = detached()
        await host.scheduler.request_session(record)
        await host.idle()
        async with host.scheduler.store.transaction() as snapshot:
            stored = snapshot.sessions[record.session_id]
            assert stored["runs"][0]["report"]["state"] == "uncertain"
            assert expires_iso_utc(stored, snapshot, host.scheduler.settings) is not None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_continuation_uses_same_backend_conversation_and_reports_again(tmp_path) -> None:  # type: ignore[no-untyped-def]
    kit = RetainedBackend()
    kit.play(ModelTurn(final="first"), ModelTurn(final="second"))
    backend = RecordedBackend(kit)
    platform = LoopbackPlatform()
    conversation = await platform.open_direct_message("U1", workspace="local")
    platform.threads.add(("local", conversation, "thread-one"))
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], backend,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert host.scheduler is not None and host.session_toolkit is not None
        record = detached(conversation, thread="thread-one", inbound={
            "message_id": "original-start", "envelope": "original task context", "reply_thread": "thread-one",
        }, prompt="first task")
        await host.scheduler.request_session(record)
        await host.idle()
        kit.retained_conversations.add(record.owned_conversation)
        continued = json.loads(await host.session_toolkit.invoke_at(
            "session_continue", {"session_id": record.session_id, "prompt": "second task"},
            at=InboundMessage("loopback", "local", conversation, "m2", "U1", "", event_id="e2",
                              thread="thread-one", chat_type="dm", trigger=TRIGGER_DM),
        ))
        assert continued["ok"], continued
        assert "first" not in json.dumps(continued)
        await host.idle()
        assert len(backend.requests) == 2
        assert backend.requests[0].conversation == backend.requests[1].conversation
        assert backend.requests[0].existing_conversation_only is False
        assert backend.requests[1].existing_conversation_only is True
        assert backend.requests[1].metadata["belle_inbound"]["message_id"] == "original-start"
        assert "original task context" in backend.requests[1].text
        async with host.scheduler.store.transaction() as snapshot:
            runs = snapshot.sessions[record.session_id]["runs"]
            assert [run["result"] for run in runs] == ["first", "second"]
            assert len({run["run_id"] for run in runs}) == 2
        posts = [entry for entry in platform.log if entry.kind == "post"]
        assert len(posts) == 2 and all(entry.thread == "thread-one" for entry in posts)
    finally:
        await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("origin_type", "current_type", "command"), [
    ("dm", "channel", "/belle"),
    ("direct", "direct", "!belle"),
    ("D", "D", "/belle"),
])
async def test_report_never_shows_result_in_any_chat(
    tmp_path, monkeypatch, origin_type: str, current_type: str, command: str,
) -> None:  # type: ignore[no-untyped-def]
    platform = LoopbackPlatform()
    platform.direct_chat_types = frozenset({origin_type})
    monkeypatch.setattr(platform, "command_prefix", lambda workspace: command)
    conversation = await platform.open_direct_message("U1", workspace="local")
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], ScriptedBackend(),
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert host.scheduler is not None
        record = detached(conversation, chat_type=origin_type, state="succeeded", result="private result",
                          result_available=True, ended_iso_utc="2026-10-18T00:00:01Z")
        async with host.scheduler.store.transaction() as snapshot:
            snapshot.sessions[record.session_id] = record.to_json()
        platform.chat_types[("local", conversation)] = current_type
        await host.report_run(record.session_id, record.runs[0].run_id)
        posts = [entry.text for entry in platform.log if entry.kind == "post"]
        assert len(posts) == 1 and "private result" not in posts[0]
        assert "succeeded" in posts[0] and record.session_id in posts[0]
        assert f"run `{command}` with `sessions result {record.session_id}`" in posts[0]
    finally:
        await host.stop()


async def test_chat_turn_writes_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import hashlib

    kit = ScriptedBackend()
    kit.play(ModelTurn(final="ordinary answer"))
    platform = LoopbackPlatform()
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], RecordedBackend(kit.backend),
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert host.scheduler is not None
        path = host.scheduler.store.path
        before = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        await platform.inject(InboundMessage(
            platform="loopback", workspace="local", conversation="D-U1", message_id="m1", event_id="e1",
            sender="U1", text="hello", chat_type="dm", trigger=TRIGGER_DM,
        ))
        await host.idle()
        assert (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) == before
        async with host.scheduler.store.transaction() as snapshot:
            assert snapshot.sessions == {}
        assert [entry.text for entry in platform.log if entry.kind == "post"] == ["ordinary answer"]
    finally:
        await host.stop()


def minutes_ago(minutes: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")


@pytest.mark.asyncio
@pytest.mark.parametrize("running_first", [False, True])
async def test_host_recovers_stored_final_without_cancelling_or_resubmitting(tmp_path, running_first) -> None:  # type: ignore[no-untyped-def]
    record = detached(inbound={"message_id": "original-recovery-message", "envelope": "recovery envelope"},
                      state="running", owner="old", started_iso_utc=minutes_ago(1))
    async with JobStore(tmp_path).transaction() as snapshot:
        snapshot.sessions[record.session_id] = record.to_json()
    backend = RecoverBackend(record.owned_conversation, running_first=running_first)
    recorded = RecordedBackend(backend)
    platform = LoopbackPlatform()
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], recorded,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        await host.idle()
        assert backend.cancelled == [] and recorded.requests == []
        assert backend.recoveries == (2 if running_first else 1)
        async with host.scheduler.store.transaction() as snapshot:  # type: ignore[union-attr]
            run = snapshot.sessions[record.session_id]["runs"][0]
            assert run["state"] == "succeeded" and run["result"] == "stored final"
        assert [entry.text for entry in platform.log if entry.kind == "post"] == [
            ("Session `ses-1234567890` succeeded.\n"
             "In a DM, run `/belle` with `sessions result ses-1234567890` as its command text to inspect the stored result.")
        ]
    finally:
        await host.stop()


async def test_a_recovered_run_past_the_turn_timeout_is_stopped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.host import TURN_TIMEOUT_NOTICE

    record = detached(inbound={"message_id": "original-recovery-message", "envelope": "recovery envelope"},
                      state="running", owner="old", started_iso_utc=minutes_ago(61))
    async with JobStore(tmp_path).transaction() as snapshot:
        snapshot.sessions[record.session_id] = record.to_json()

    class Running(RecoverBackend):
        async def recover(self, request) -> Recovery:  # type: ignore[no-untyped-def]
            await super().recover(request)
            return Recovery(RECOVERY_RUNNING)

    backend = Running(record.owned_conversation)
    host = ConversationHost([HostedPlatform(LoopbackPlatform(), EventDedupStore(tmp_path / "seen.json"))], backend,
                            scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path)
    await host.start()
    try:
        await host.idle()
        # The first check takes the run, and the watch stops it at its first check: the run started 61 minutes ago.
        assert backend.cancelled == [record.owned_conversation] and backend.recoveries == 2
        async with host.scheduler.store.transaction() as snapshot:  # type: ignore[union-attr]
            run = snapshot.sessions[record.session_id]["runs"][0]
        assert run["state"] == "failed" and run["error"] == TURN_TIMEOUT_NOTICE
    finally:
        await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("post", [True, False])
@pytest.mark.parametrize("post_tool", ["post_message", "upload"])
async def test_detached_post_or_silence_keeps_final_private(
    tmp_path, monkeypatch, notify: bool, post: bool, post_tool: str,
) -> None:  # type: ignore[no-untyped-def]
    kit = ScriptedBackend()
    kit.play(ModelTurn(calls=((post_tool, {"text": "substantive answer"}),) if post else (),
                       final="private audit final"))
    platform = LoopbackPlatform()
    class UploadToolkit(LoopbackToolkit):
        def get_tool_definitions(self):
            return [{**super().get_tool_definitions()[0], "name": "upload"}]

        upload = LoopbackToolkit.post_message

    original = platform.toolkits
    monkeypatch.setattr(platform, "posting_tools", ("upload",), raising=False)
    monkeypatch.setattr(platform, "toolkits", lambda metadata: [*original(metadata), UploadToolkit(platform, metadata)])
    backend = RecordedBackend(kit.backend)
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], backend,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert host.session_toolkit is not None and host.scheduler is not None
        response = json.loads(await host.session_toolkit.invoke_at(
            "session_start", {"prompt": "work", "notify": notify},
            at=InboundMessage("loopback", "local", "D-U1", "source-message", "U1", "",
                              event_id="command-event", chat_type="dm", trigger=TRIGGER_DM,
                              envelope="Trusted original message context"),
        ))
        assert response["ok"], response
        await host.idle()
        request = backend.requests[0]
        assert request.request_id not in {"source-message", "command-event"}
        assert "Trusted original message context" in request.text
        assert request.metadata["belle_inbound"]["message_id"] == "source-message"
        guidance = " ".join(request.scope.system_additions)
        assert "only through post_message" in guidance and "never automatically posted" in guidance
        posts = [entry.text for entry in platform.log if entry.kind == "post"]
        assert posts.count("substantive answer") == int(post)
        results = [json.loads(result) for result in kit.results]
        assert len(results) == int(post)
        if post:
            assert results[0]["ok"] is True and results[0]["chat_id"] == "D-U1"
            assert results[0]["message_id"]
        assert len(posts) == int(post) + int(notify)
        assert all("private audit final" not in text for text in posts)
        async with host.scheduler.store.transaction() as snapshot:
            run = snapshot.sessions[response["session_id"]]["runs"][0]
        await host.report_run(response["session_id"], run["run_id"])
        assert len([entry for entry in platform.log if entry.kind == "post"]) == len(posts)
        assert run["result"] == "private audit final"
        assert run["outcome"] == ("posted-by-tool" if post else "stored-final")
    finally:
        await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("optional", [True, False])
@pytest.mark.parametrize("tool_name_format", ["{name}", "belle_{name}"])
async def test_detached_posting_policy_and_missing_capability(tmp_path, monkeypatch, optional: bool, tool_name_format: str) -> None:  # type: ignore[no-untyped-def]
    platform = LoopbackPlatform()
    backend = ScriptedBackend()
    backend._capabilities = replace(backend._capabilities, tool_name_format=tool_name_format)
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], backend,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        record = detached(notify=False, inbound={"message_id": "original", "reply_thread": "reply-place",
                                                 "reply_optional": optional})
        turn = await host._session_run_turn(None, record, record.runs[0])
        assert turn.inbound.reply_thread == "reply-place" and turn.inbound.message_id == "original"
        assert turn.reply_optional is optional
        guidance = " ".join(turn.request.scope.system_additions)
        advertised = tool_name_format.format(name="post_message")
        assert f"only through {advertised}," in guidance
        assert (f"Before your final response, call {advertised}" in guidance) is (not optional)
        assert ("choose silence" in " ".join(turn.request.scope.system_additions)) is optional
        original_limits = host.scope_limits
        monkeypatch.setattr(host, "scope_limits", lambda *args: replace(
            original_limits(*args), scope=replace(original_limits(*args).scope, tools={"post_message": LEVEL_DENY})
        ))
        if optional:
            denied = await host._session_run_turn(None, record, record.runs[0])
            assert denied.bound.tools["post_message"] == LEVEL_DENY
        else:
            with pytest.raises(Refused, match="current tool scope denies"):
                await host._session_run_turn(None, record, record.runs[0])
        monkeypatch.setattr(platform, "posting_tool", "")
        with pytest.raises(Refused, match="platform posting tool"):
            await host._session_run_turn(None, record, record.runs[0])
    finally:
        await host.stop()


class ForgettingBackend(RetainedBackend):
    """A scripted harness that keeps, continues and deletes conversations, and records each deletion."""

    def __init__(self) -> None:
        super().__init__()
        self._capabilities = replace(self._capabilities, operations=self._capabilities.operations | {OP_FORGET})
        self.forgotten: list[tuple[str, frozenset[str]]] = []
        self.host: ConversationHost | None = None

    async def run_turn(self, request):  # type: ignore[no-untyped-def]
        self.retained_conversations.add(request.conversation)
        async for event in super().run_turn(request):
            yield event

    async def forget(self, conversation: str) -> None:
        assert self.host is not None and self.host.scheduler is not None
        async with self.host.scheduler.store.transaction() as snapshot:
            owned = frozenset(f"belle_session_{session_id}" for session_id in snapshot.sessions)
        self.forgotten.append((conversation, owned))
        self.retained_conversations.discard(conversation)


async def test_forget_spy_sees_only_owned_conversations(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from tests.scheduling.fake_clock import FakeClock

    kit = ForgettingBackend()
    kit.play(*(ModelTurn(final=f"answer {number}") for number in range(6)))
    platform = LoopbackPlatform()
    direct = await platform.open_direct_message("U1", workspace="local")
    recorded = RecordedBackend(kit)
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], recorded,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    kit.host = host
    await host.start()
    try:
        assert host.scheduler is not None and host.session_toolkit is not None and host.schedule_toolkit is not None

        def at(event: str) -> InboundMessage:
            return InboundMessage("loopback", "local", direct, event, "U1", "", event_id=event, chat_type="dm",
                                  trigger=TRIGGER_DM)

        await platform.inject(replace(at("m1"), text="hello"))
        await host.idle()
        started = json.loads(await host.session_toolkit.invoke_at("session_start", {"prompt": "work"}, at=at("m2")))
        await host.idle()
        for mode in ("fresh", "shared"):
            made = json.loads(await host.schedule_toolkit.invoke_at("schedule_create", {
                "name": mode, "prompt": "Say done.", "when": {"kind": "once", "after_seconds": 600}, "session": mode,
            }, at=at(f"c-{mode}")))
            assert json.loads(await host.schedule_toolkit.invoke_at(
                "schedule_run_now", {"job_id": made["job"]["job_id"]}, at=at(f"r-{mode}")))["ok"]
            await host.idle()
            if mode == "shared":
                assert json.loads(await host.schedule_toolkit.invoke_at(
                    "schedule_delete", {"job_id": made["job"]["job_id"], "keep_history": True}, at=at("d")))["ok"]
        continued = json.loads(await host.session_toolkit.invoke_at(
            "session_continue", {"session_id": started["session_id"], "prompt": "more"}, at=at("m3")))
        assert continued["ok"], continued
        await host.idle()
        chat = next(request.conversation for request in recorded.requests if not request.conversation.startswith("belle_"))
        async with host.scheduler.store.transaction() as snapshot:
            expected = {f"belle_session_{session_id}" for session_id in snapshot.sessions}
        assert len(expected) == 3
        later = FakeClock(datetime.now(UTC) + timedelta(hours=100))
        host.scheduler.expiry.clock = host.scheduler.store._clock = later
        await host.scheduler.expiry.tick()
        assert {conversation for conversation, _ in kit.forgotten} == expected
        assert all(conversation in owned for conversation, owned in kit.forgotten)
        assert chat in kit.retained_conversations
        async with host.scheduler.store.transaction() as snapshot:
            assert snapshot.sessions == {}
    finally:
        await host.stop()


async def test_a_shared_job_runs_after_a_first_run_that_never_reached_the_harness(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.scheduling.records import ClockStart

    kit = ScriptedBackend()
    kit.play(ModelTurn(final="second"))
    platform = LoopbackPlatform()
    direct = await platform.open_direct_message("U1", workspace="local")
    recorded = RecordedBackend(kit)
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], recorded,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert host.scheduler is not None and host.schedule_toolkit is not None
        at = InboundMessage("loopback", "local", direct, "c", "U1", "", event_id="c", chat_type="dm", trigger=TRIGGER_DM)
        made = json.loads(await host.schedule_toolkit.invoke_at("schedule_create", {
            "name": "shared", "prompt": "Say done.", "when": {"kind": "once", "after_seconds": 600}, "session": "shared",
        }, at=at))
        job = made["job"]
        runs = [replace(new_run(started_by="clock", prompt="Say done.", turn={}, questions="refuse", place=job["deliver_to"],
                                delivery="post_final", owner=host.scheduler.owner,
                                clock=ClockStart(f"2026-10-18T0{number}:00:00Z", 0, 0, f"{job['job_id']}:{number}")),
                        **({"state": "failed", "outcome": "failed", "error": "turn_out_of_scope: refused",
                            "ended_iso_utc": "2026-10-18T01:00:01Z", "prompt": ""} if number == 1 else {}))
                for number in (1, 2)]
        async with host.scheduler.store.transaction() as snapshot:
            shared = snapshot.jobs[job["job_id"]]["shared_session_id"]
            snapshot.sessions[shared]["runs"] = [run.to_json() for run in runs]
        host.scheduler.changed([job["job_id"]])
        await host.scheduler._tick()
        await host.idle()
        (request,) = recorded.requests
        assert request.conversation == f"belle_session_{shared}" and request.existing_conversation_only is False
        async with host.scheduler.store.transaction() as snapshot:
            assert snapshot.sessions[shared]["runs"][1]["state"] == "succeeded"
    finally:
        await host.stop()


def test_platform_named_belle_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    platform = LoopbackPlatform()
    platform.name = "belle"
    with pytest.raises(ValueError, match="'belle' is reserved"):
        ConversationHost([HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], ScriptedBackend())


async def _running_clock_run(tmp_path, conversation: str):  # type: ignore[no-untyped-def]
    from belle_agent.scheduling.records import ClockStart
    from tests.scheduling.records_kit import job

    place = {"platform": "loopback", "workspace": "local", "conversation": conversation, "thread": ""}
    item = replace(job(1), origin=place, deliver_to=place, origin_chat_type="dm", backend="scripted",
                   created_by_user_id="U1", ended=True)
    run = replace(new_run(started_by="clock", prompt="Say hello.", turn={}, questions="refuse", place=place,
                          delivery="post_final", owner="old", clock=ClockStart("2026-10-18T00:00:00Z", 0, 0, "k")),
                  state="running", started_iso_utc="2026-10-18T00:00:01Z")
    session = DetachedSession("ses-sched00001", "scheduled", item.job_id, "fresh", "scripted", place, "dm", "U1",
                      "2026-10-18T00:00:00Z", NO_DELETION, [run])
    async with JobStore(tmp_path).transaction() as snapshot:
        snapshot.jobs[item.job_id] = item.to_json()
        snapshot.sessions[session.session_id] = session.to_json()
    return session


class RecoveringBackend(ScriptedBackend):
    """A harness that still runs a turn from before Belle's restart, and ends it with a stored final."""

    def __init__(self, conversation: str, *, recover: bool = True) -> None:
        super().__init__()
        if recover:
            self._capabilities = replace(self._capabilities, operations=self._capabilities.operations | {OP_RECOVER})
        self.conversation = conversation
        self.cancelled: list[str] = []
        self.recovered: list[str] = []

    async def active(self) -> list[str]:
        return [self.conversation]

    async def cancel(self, conversation: str) -> bool:
        self.cancelled.append(conversation)
        return True

    async def recover(self, request) -> Recovery:  # type: ignore[no-untyped-def]
        self.recovered.append(request.request_id)
        return Recovery(RECOVERY_TERMINAL, TurnEvent(EVENT_SUCCEEDED, text="hello after restart"))


@pytest.mark.parametrize("recover", [True, False])
async def test_clock_run_after_restart(tmp_path, recover: bool) -> None:  # type: ignore[no-untyped-def]
    platform = LoopbackPlatform()
    conversation = await platform.open_direct_message("U1", workspace="local")
    session = await _running_clock_run(tmp_path, conversation)
    backend = RecoveringBackend(session.owned_conversation, recover=recover)
    recorded = RecordedBackend(backend)
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], recorded,
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        await host.idle()
        assert recorded.requests == []
        async with host.scheduler.store.transaction() as snapshot:  # type: ignore[union-attr]
            run = snapshot.sessions[session.session_id]["runs"][0]
        posts = [entry.text for entry in platform.log if entry.kind == "post"]
        if recover:
            assert backend.cancelled == [] and backend.recovered == [session.runs[0].run_id]
            assert posts == ["hello after restart"]
            assert (run["state"], run["outcome"]) == ("succeeded", "posted-final")
            assert run["posted"][0]["conversation"] == conversation
            await host.report_run(session.session_id, run["run_id"])
            assert len([entry for entry in platform.log if entry.kind == "post"]) == 1
        else:
            assert backend.cancelled == [session.owned_conversation] and posts == []
            assert run["state"] == "interrupted" and "final outcome unknown" in run["error"]
    finally:
        await host.stop()


async def test_recovered_final_posts_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    platform = LoopbackPlatform()
    conversation = await platform.open_direct_message("U1", workspace="local")
    session = await _running_clock_run(tmp_path, conversation)
    async with JobStore(tmp_path).transaction() as snapshot:
        # Belle stopped after the post and before it stored the receipt.
        snapshot.sessions[session.session_id]["runs"][0].update(
            state="succeeded", outcome="post-uncertain", result="hello", ended_iso_utc="2026-10-18T00:00:02Z")
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], ScriptedBackend(),
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        await host.report_run(session.session_id, session.runs[0].run_id)
        assert [entry for entry in platform.log if entry.kind == "post"] == []
    finally:
        await host.stop()


async def test_a_final_that_recovery_stored_before_a_stop_is_posted_at_start(tmp_path) -> None:  # type: ignore[no-untyped-def]
    platform = LoopbackPlatform()
    conversation = await platform.open_direct_message("U1", workspace="local")
    session = await _running_clock_run(tmp_path, conversation)
    async with JobStore(tmp_path).transaction() as snapshot:
        # Belle stopped after recovery stored the final and before it posted it.
        snapshot.sessions[session.session_id]["runs"][0].update(
            state="succeeded", outcome="stored-final", result="hello", ended_iso_utc="2026-10-18T00:00:02Z")
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], ScriptedBackend(),
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert [entry.text for entry in platform.log if entry.kind == "post"] == ["hello"]
        async with host.scheduler.store.transaction() as snapshot:  # type: ignore[union-attr]
            assert snapshot.sessions[session.session_id]["runs"][0]["outcome"] == "posted-final"
    finally:
        await host.stop()


async def test_a_delivery_to_a_platform_that_is_not_configured_is_marked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    gone = {"platform": "gone", "workspace": "local", "conversation": "D-U1", "thread": ""}
    session = await _running_clock_run(tmp_path, "D-U1")
    notice = detached()
    async with JobStore(tmp_path).transaction() as snapshot:
        # A stopped process owed a final and a notice on a platform that this Belle no longer serves.
        snapshot.sessions[session.session_id]["runs"][0].update(
            state="succeeded", outcome="stored-final", result="hello", ended_iso_utc="2026-10-18T00:00:02Z", place=gone)
        run = replace(notice.runs[0], state="succeeded", outcome="stored-final", owner="old",
                      ended_iso_utc="2026-10-18T00:00:02Z")
        snapshot.sessions[notice.session_id] = replace(notice, origin=gone, runs=[run]).to_json()
    platform = LoopbackPlatform()
    host = ConversationHost(
        [HostedPlatform(platform, EventDedupStore(tmp_path / "seen.json"))], ScriptedBackend(),
        scheduling=SchedulingSettings(engine="belle"), state_dir=tmp_path,
    )
    await host.start()
    try:
        assert [entry for entry in platform.log if entry.kind == "post"] == []
        async with host.scheduler.store.transaction() as snapshot:  # type: ignore[union-attr]
            final = snapshot.sessions[session.session_id]["runs"][0]
            report = snapshot.sessions[notice.session_id]["runs"][0]["report"]
        assert final["outcome"] == "stored-final" and "platform gone is not configured" in final["error"]
        assert report["state"] == "refused" and "platform gone is not configured" in report["error"]
    finally:
        await host.stop()
