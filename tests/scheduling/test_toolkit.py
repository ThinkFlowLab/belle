"""Shared schedule tools use trusted places and durable store transactions."""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from belle_agent.backend import BackendCapabilities, ToolCall, TurnScope
from belle_agent.platforms.base import InboundMessage
from belle_agent.scheduling.address import Address
from belle_agent.scheduling.commands import SCHEDULE_SUBCOMMANDS, schedule_command
from belle_agent.scheduling.records import DetachedSession, Job, SessionRun
from belle_agent.scheduling.runner import RunEnd, Runner
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore
from belle_agent.scheduling.toolkit import ScheduleToolkit
from belle_agent.scheduling.turnspec import ScopeLimits
from belle_agent.scheduling.when import Schedule, next_runs
from belle_agent.scopes.people import EMPTY_DIRECTORY, PeopleDirectory
from belle_agent.tools.host import BoundTurn, ToolHost, active_call

from .fake_clock import FakeClock

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
ORIGIN = Address("loopback", "", "origin", "thread")


class Places:
    workspaces = frozenset({""})

    def __init__(self) -> None:
        self.readable = True
        self.reachable = True
        self.readers: dict[tuple[str, str], tuple[str, ...] | None] = {}

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        return self.readable

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> Any:
        return SimpleNamespace(exists=self.reachable, member=self.reachable, reason="not a member", chat_type="channel")

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None:
        return self.readers.get((origin, target), ())

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str:
        return f"dm-{user}"


class Host:
    def __init__(self, places: Places) -> None:
        self.platforms: Mapping[str, Any] = {"loopback": object()}
        self.capabilities = BackendCapabilities("opencode")
        self.directory = EMPTY_DIRECTORY
        self._places = places
        self.scope_calls: list[tuple[str, str]] = []

    async def catalog(self) -> None:
        return None

    def scope_limits(self, place: Address, user: str, chat_type: str) -> ScopeLimits:
        self.scope_calls.append((place.conversation, chat_type))
        return ScopeLimits(place, TurnScope())

    def places(self, platform: str) -> Places | None:
        return self._places if platform == "loopback" else None


class Recurrence:
    def next_after(self, when: dict[str, Any], after: datetime) -> datetime | None:
        runs = next_runs(Schedule(when), after, 1)
        return runs[0] if runs else None


class Starter:
    def __init__(self) -> None:
        self.started: list[SessionRun] = []
        self.stopped: list[SessionRun] = []

    async def start_run(self, job: Job | None, session: DetachedSession, run: SessionRun) -> None:
        self.started.append(run)

    async def stop_run(self, session: DetachedSession, run: SessionRun) -> bool:
        self.stopped.append(run)
        return True

    async def report_run(self, session_id: str, run_id: str) -> None:
        return None

    async def delete_owned_conversation(self, session: DetachedSession) -> None:
        return None


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ScheduleToolkit, JobStore, FakeClock, Places, Starter, dict[str, Any]]:
    clock = FakeClock(NOW)
    places = Places()
    starter = Starter()
    settings = SchedulingSettings()
    store = JobStore(tmp_path, clock=clock)
    runner = Runner(store, settings, starter, clock, Recurrence(), owner="toolkit-test")
    toolkit = ScheduleToolkit(store, runner, settings, Host(places), clock)
    metadata: dict[str, Any] = {"belle_place": {**ORIGIN.json(), "sender": "creator", "chat_type": "channel"}}
    monkeypatch.setattr("belle_agent.scheduling.toolkit.request_metadata", lambda: metadata)
    return toolkit, store, clock, places, starter, metadata


def read(result: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(result))


async def create(toolkit: ScheduleToolkit, **kwargs: Any) -> dict[str, Any]:
    return read(await toolkit.schedule_create("Reminder", "Post a reminder", {"kind": "once", "after_seconds": 600}, **kwargs))


async def confirmed_create(toolkit: ScheduleToolkit, metadata: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    held: list[tuple[ToolCall, BoundTurn]] = []

    async def confirm(call: ToolCall, turn: BoundTurn, question: Any) -> bool:
        held.append((call, turn))
        return True

    host = ToolHost([toolkit], confirm=confirm)
    host.bind("origin", BoundTurn("request-one", metadata=metadata))
    call = ToolCall(
        "origin", "schedule_create", {
            "name": "Reminder", "prompt": "Post a reminder", "when": {"kind": "once", "after_seconds": 600},
            **kwargs,
        }, "call-one",
    )
    assert read(cast(str, await host.call(call)))["error"] == "awaiting_confirmation"
    assert held and active_call() is None
    return read(cast(str, await host.run_confirmed(*held[0])))


async def test_command_uses_authenticated_sender_and_toolkit_permissions(rig: tuple[Any, ...]) -> None:
    toolkit = rig[0]
    job_id = (await create(toolkit))["job"]["job_id"]
    sub = next(item for item in SCHEDULE_SUBCOMMANDS if item.words == ("schedule", "pause"))
    at = InboundMessage("loopback", "", "origin", "", "creator", "", thread="thread", chat_type="channel")
    assert (await schedule_command(toolkit, at, sub, job_id, "/belle"))[1] == "answered:schedule-pause"
    at_other = InboundMessage("loopback", "", "origin", "", "other", "", thread="thread", chat_type="channel")
    sub = next(item for item in SCHEDULE_SUBCOMMANDS if item.words == ("schedule", "resume"))
    assert (await schedule_command(toolkit, at_other, sub, job_id, "/belle"))[1] == "refused:not_job_owner"


async def test_operator_commands_cross_conversations_without_widening_ordinary_calls(rig: tuple[Any, ...]) -> None:
    toolkit, store, _, _, _, metadata = rig
    toolkit.settings = replace(toolkit.settings, operator_role="operators")
    toolkit.host.directory = PeopleDirectory(
        {"admin": {"loopback": ("operator",)}, "elsewhere": {"slack": ("slack-only",)}},
        {"operators": ("admin", "elsewhere")},
    )
    first = (await create(toolkit))["job"]["job_id"]
    metadata["belle_place"].update(conversation="other", sender="creator")
    second = (await create(toolkit))["job"]["job_id"]
    from_third = InboundMessage("loopback", "", "third", "", "ordinary", "", chat_type="channel")
    operator = replace(from_third, sender="operator")

    async def command(at: InboundMessage, action: str, job_id: str = "") -> tuple[str, str]:
        sub = next(item for item in SCHEDULE_SUBCOMMANDS if item.words == ("schedule", action))
        return await schedule_command(toolkit, at, sub, job_id, "/belle")

    listed, outcome = await command(from_third, "list")
    assert outcome == "answered:schedule-list" and first not in listed and second not in listed
    for action in ("show", "pause", "resume", "delete", "run"):
        assert (await command(from_third, action, first))[1] == "refused:job_not_found"
    assert (await command(replace(from_third, sender="creator"), "pause", first))[1] == "refused:job_not_found"
    assert (await command(replace(from_third, sender="slack-only"), "show", first))[1] == "refused:job_not_found"
    listed, _ = await command(operator, "list")
    assert first in listed and second in listed
    shown, outcome = await command(operator, "show", first)
    assert outcome == "answered:schedule-show" and first in shown
    assert (await command(operator, "pause", first))[1] == "answered:schedule-pause"
    assert (await command(operator, "resume", first))[1] == "answered:schedule-resume"
    assert (await command(operator, "run", first))[1] == "answered:schedule-run"
    assert (await command(operator, "delete", second))[1] == "answered:schedule-delete"
    async with store.transaction() as snapshot:
        # A job with no session leaves no stub.
        assert second not in snapshot.deleted and second not in snapshot.jobs

    metadata["belle_place"].update(conversation="third", sender="operator")
    assert read(await toolkit.schedule_list())["jobs"] == []
    assert read(await toolkit.schedule_update(first, enabled=False))["error"] == "job_not_found"
    toolkit.settings = replace(toolkit.settings, operator_role="")
    assert (await command(operator, "show", first))[1] == "refused:job_not_found"


@pytest.mark.asyncio
async def test_create_list_update_and_target_pause(rig: tuple[Any, ...]) -> None:
    toolkit, store, _, _, _, metadata = rig
    made = await create(toolkit)
    assert made["ok"] and made["job"]["when"]["at"] == "2026-10-18T12:10:00Z"
    assert made["job"]["next_runs"][0]["due_iso_utc"] == "2026-10-18T12:10:00Z"
    job_id = made["job"]["job_id"]
    assert read(await toolkit.schedule_list())["jobs_total"] == 1
    changed = read(await toolkit.schedule_update(job_id, enabled=False))
    assert changed["changed"] == ["enabled"] and changed["job"]["next_runs"] == []
    assert read(await toolkit.schedule_update(job_id, enabled=False))["error"] == "no_change"
    assert read(await toolkit.schedule_update(job_id, prompt=None))["error"] == "invalid_arguments"
    assert read(await toolkit.schedule_update(job_id, deliver_to=None))["error"] == "invalid_arguments"
    metadata["belle_place"]["sender"] = "other"
    assert read(await toolkit.schedule_update(job_id, prompt="different"))["error"] == "not_job_owner"
    assert read(await toolkit.schedule_update(job_id, enabled=False))["error"] == "no_change"
    async with store.transaction() as snapshot:
        assert snapshot.jobs[job_id]["enabled"] is False


@pytest.mark.asyncio
async def test_widened_target_question_and_runtime_recheck(rig: tuple[Any, ...]) -> None:
    toolkit, _, _, places, _, metadata = rig
    places.readers[("origin", "target")] = ("new-reader",)
    args = {"deliver_to": {"conversation": "target", "thread": ""}}
    question = await toolkit.widening_question("schedule_create", args)
    assert question and "new-reader" in question
    assert (await create(toolkit, **args))["error"] == "approval_required"
    made = await confirmed_create(toolkit, metadata, **args)
    assert "approved_readers" not in made["job"]
    job = Job.from_json(await _stored_job(toolkit.store, made["job"]["job_id"]), "job")
    assert job.approved_readers == ["new-reader"]
    target, scope, chat_type = await toolkit.validate_run(job, label="scheduled run")
    assert target.conversation == "target" and scope.system_additions[0] == "scheduled run"
    assert chat_type == "channel"
    assert ("target", "channel") in cast(Host, toolkit.host).scope_calls
    places.readers[("origin", "target")] = ("new-reader", "later-reader")
    with pytest.raises(Exception, match="later-reader"):
        await toolkit.validate_run(job)


@pytest.mark.asyncio
async def test_confirmed_call_refuses_if_audience_changes_after_question(rig: tuple[Any, ...]) -> None:
    toolkit, store, _, places, _, metadata = rig
    places.readers[("origin", "target")] = ("asked-reader",)
    held: list[tuple[ToolCall, BoundTurn]] = []

    async def confirm(call: ToolCall, turn: BoundTurn, question: Any) -> bool:
        assert "asked-reader" in question.text
        held.append((call, turn))
        return True

    host = ToolHost([toolkit], confirm=confirm)
    host.bind("origin", BoundTurn("request-one", metadata=metadata))
    call = ToolCall(
        "origin", "schedule_create", {
            "name": "Reminder", "prompt": "Post a reminder", "when": {"kind": "once", "after_seconds": 600},
            "deliver_to": {"conversation": "target", "thread": ""},
        }, "call-one",
    )
    assert read(cast(str, await host.call(call)))["error"] == "awaiting_confirmation"
    places.readers[("origin", "target")] = ("asked-reader", "later-reader")
    refused = read(cast(str, await host.run_confirmed(*held[0])))
    assert refused["error"] == "audience_widened"
    assert "retry" in refused["detail"]
    assert active_call() is None
    async with store.transaction() as snapshot:
        assert snapshot.jobs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["schedule_update", "schedule_run_now"])
async def test_confirmed_change_and_manual_run_keep_the_questioned_audience(
    rig: tuple[Any, ...], name: str,
) -> None:
    toolkit, store, _, places, starter, metadata = rig
    made = await create(toolkit)
    job_id = made["job"]["job_id"]
    places.readers[("origin", "target")] = ("asked-reader",)
    held: list[tuple[ToolCall, BoundTurn]] = []

    async def confirm(call: ToolCall, turn: BoundTurn, question: Any) -> bool:
        held.append((call, turn))
        return True

    host = ToolHost([toolkit], confirm=confirm)
    host.bind("origin", BoundTurn("request-one", metadata=metadata))
    call = ToolCall(
        "origin", name, {"job_id": job_id, "deliver_to": {"conversation": "target", "thread": ""}}, "call-one",
    )
    assert read(cast(str, await host.call(call)))["error"] == "awaiting_confirmation"
    places.readers[("origin", "target")] = ("asked-reader", "later-reader")
    assert read(cast(str, await host.run_confirmed(*held[0])))["error"] == "audience_widened"
    assert not starter.started
    async with store.transaction() as snapshot:
        assert snapshot.jobs[job_id]["deliver_to"] == ORIGIN.json()
        assert snapshot.sessions == {}


async def _stored_job(store: JobStore, job_id: str) -> dict[str, Any]:
    async with store.transaction() as snapshot:
        return cast(dict[str, Any], snapshot.jobs[job_id].copy())


@pytest.mark.asyncio
async def test_manual_run_and_delete_kept_history(rig: tuple[Any, ...]) -> None:
    toolkit, store, _, _, starter, _ = rig
    made = await create(toolkit)
    job_id = made["job"]["job_id"]
    requested = read(await toolkit.schedule_run_now(job_id))
    assert requested["ok"] and requested["run"]["started_by"] == "request"
    assert starter.started and starter.started[0].request is not None
    async with store.transaction() as snapshot:
        assert snapshot.jobs[job_id]["runs_started"] == 0
    deleted = read(await toolkit.schedule_delete(job_id, keep_history=True))
    assert deleted["runs"] == [{"run_id": requested["run"]["run_id"], "state": "running", "stop_requested": True,
                                "stop_request_sent": True}]
    assert deleted["history_kept_until_iso_utc"] is None
    assert starter.stopped and starter.stopped[0].stop_requested
    history = read(await toolkit.schedule_runs(job_id=job_id))
    assert [run["run_id"] for run in history["runs"]] == [requested["run"]["run_id"]]
    assert read(await toolkit.schedule_list(job_id=job_id))["error"] == "job_not_found"


@pytest.mark.asyncio
async def test_visibility_filters_and_argument_order(rig: tuple[Any, ...]) -> None:
    toolkit, _, _, _, _, metadata = rig
    made = await create(toolkit)
    job_id = made["job"]["job_id"]
    assert read(await toolkit.schedule_list(job_id="bad", next_runs="bad"))["error"] == "job_id_malformed"
    assert read(await toolkit.schedule_list(job_id=job_id, next_runs="bad"))["error"] == "invalid_arguments"
    assert read(await toolkit.schedule_list(all_conversations=True))["error"] == "invalid_arguments"
    metadata["belle_place"]["conversation"] = "other"
    assert read(await toolkit.schedule_list())["jobs"] == []
    assert read(await toolkit.schedule_update(job_id, enabled=False))["error"] == "job_not_found"
    metadata["belle_schedule"] = {"job_id": job_id, "creator": "creator", "origin": ORIGIN.json()}
    assert read(await toolkit.schedule_update(job_id, enabled=False))["error"] == "trusted_conversation_context_required"
    metadata["belle_schedule"]["origin_chat_type"] = "channel"
    assert read(await toolkit.schedule_update(job_id, enabled=False))["ok"]


@pytest.mark.asyncio
async def test_target_can_pause_but_cannot_change_or_delete(rig: tuple[Any, ...]) -> None:
    toolkit, _, _, places, _, metadata = rig
    places.readers[("origin", "target")] = ("another-reader",)
    made = await confirmed_create(toolkit, metadata, deliver_to={"conversation": "target", "thread": ""})
    job_id = made["job"]["job_id"]
    metadata["belle_place"].update(conversation="target", sender="other")
    visible = read(await toolkit.schedule_list(job_id=job_id))["jobs"][0]
    assert visible["prompt"] is None and visible["hidden"] == ["prompt"]
    assert read(await toolkit.schedule_update(job_id, enabled=False))["changed"] == ["enabled"]
    assert read(await toolkit.schedule_update(job_id, prompt="changed"))["error"] == "not_job_owner"
    assert read(await toolkit.schedule_delete(job_id))["error"] == "not_job_owner"


@pytest.mark.asyncio
async def test_runs_page_and_hide_result_from_new_readers(rig: tuple[Any, ...]) -> None:
    toolkit, _, clock, places, _, metadata = rig
    places.readers[("origin", "target")] = ("another-reader",)
    made = await confirmed_create(toolkit, metadata, deliver_to={"conversation": "target", "thread": ""})
    job_id = made["job"]["job_id"]
    first = read(await toolkit.schedule_run_now(job_id))["run"]
    posted = [{**ORIGIN.json(), "message_id": "post-one"}]
    await toolkit.runner.ended_run(
        first["session_id"], first["run_id"], RunEnd("posted-final", "secret", posted, "", {}, clock.now().isoformat()),
    )
    clock.advance(10)
    second = read(await toolkit.schedule_run_now(job_id))["run"]
    assert second["state"] in {"pending", "running"}
    metadata["belle_place"].update(conversation="target", sender="other")
    page = read(await toolkit.schedule_runs(job_id=job_id, max_runs=1))
    assert page["coverage"]["status"] == "partial"
    assert isinstance(page["coverage"]["next_cursor"], str)
    assert read(await toolkit.schedule_runs(job_id=job_id, cursor=page["coverage"]["next_cursor"]))["runs"][0]["run_id"] == first["run_id"]
    earlier = read(await toolkit.schedule_runs(job_id=job_id, before_iso_utc=second["due_iso_utc"]))
    assert earlier["runs"][0]["result"] is None
    assert earlier["runs"][0]["hidden"] == ["result"]
    assert read(await toolkit.schedule_runs(job_id=job_id, states=["bad"]))["error"] == "invalid_arguments"
    assert read(await toolkit.schedule_runs(job_id=job_id, before_iso_utc="not-a-time"))["error"] == "time_malformed"


@pytest.mark.asyncio
async def test_cursor_pages_runs_with_the_same_due_second(rig: tuple[Any, ...]) -> None:
    toolkit, _, _, _, _, _ = rig
    first = await create(toolkit)
    second = await create(toolkit)
    runs = [
        read(await toolkit.schedule_run_now(job["job"]["job_id"]))["run"]
        for job in (first, second)
    ]
    assert runs[0]["due_iso_utc"] == runs[1]["due_iso_utc"]
    page = read(await toolkit.schedule_runs(max_runs=1))
    follow = read(await toolkit.schedule_runs(max_runs=1, cursor=page["coverage"]["next_cursor"]))
    assert {page["runs"][0]["run_id"], follow["runs"][0]["run_id"]} == {run["run_id"] for run in runs}
    assert follow["coverage"]["status"] == "complete"
    assert read(await toolkit.schedule_runs(cursor="broken"))["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_kept_since_starts_after_the_dropped_history(rig: tuple[Any, ...]) -> None:
    toolkit, store, clock, _, _, _ = rig
    job_id = (await create(toolkit))["job"]["job_id"]
    async with store.transaction() as snapshot:
        snapshot.jobs[job_id]["unrun"].append(
            {"due_iso_utc": "2026-10-18T11:00:00Z", "state": "skipped", "reason": "a test", "claim_key": f"{job_id}:1"})
    first = read(await toolkit.schedule_run_now(job_id))["run"]
    await toolkit.runner.ended_run(first["session_id"], first["run_id"], RunEnd(
        "posted-final", "", [], "", {}, clock.now().isoformat()))
    clock.advance(72 * 3600)
    second = read(await toolkit.schedule_run_now(job_id))["run"]
    await toolkit.runner.expiry.tick()
    page = read(await toolkit.schedule_runs(job_id=job_id))
    assert [row["run_id"] for row in page["runs"]] == [second["run_id"], None]
    assert page["coverage"] == {"status": "complete", "next_cursor": None, "kept_since_iso_utc": second["due_iso_utc"]}


@pytest.mark.asyncio
async def test_a_deleted_jobs_unrun_rows_keep_their_backend(rig: tuple[Any, ...]) -> None:
    toolkit, store, _, _, _, _ = rig
    job_id = (await create(toolkit))["job"]["job_id"]
    async with store.transaction() as snapshot:
        backend = snapshot.jobs[job_id]["backend"]
        snapshot.jobs[job_id]["unrun"].append(
            {"due_iso_utc": "2026-10-18T11:00:00Z", "state": "skipped", "reason": "a test", "claim_key": f"{job_id}:1"})
    # The run's session keeps the stub of the deleted job.
    assert read(await toolkit.schedule_run_now(job_id))["ok"]
    assert read(await toolkit.schedule_delete(job_id, keep_history=True))["ok"]
    rows = read(await toolkit.schedule_runs(job_id=job_id))["runs"]
    assert backend and [row["backend"] for row in rows if row["run_id"] is None] == [backend]


@pytest.mark.asyncio
async def test_result_size_and_unreadable_store_refuse(rig: tuple[Any, ...]) -> None:
    toolkit, store, _, _, _, metadata = rig
    assert (await create(toolkit))["ok"]
    object.__setattr__(toolkit.host.capabilities, "tool_result_max_chars", 50)
    too_large = read(await toolkit.schedule_list())
    assert too_large["error"] == "result_too_large" and "next_runs" in too_large["detail"]
    metadata.pop("belle_place")
    assert read(await toolkit.schedule_list())["error"] == "trusted_conversation_context_required"
    metadata["belle_place"] = {**ORIGIN.json(), "sender": "creator", "chat_type": "channel"}
    store.path.write_text("{broken")
    assert read(await toolkit.schedule_list())["error"] == "store_unreadable"


@pytest.mark.asyncio
async def test_limit_refusals_state_the_limit(rig: tuple[Any, ...]) -> None:
    toolkit = rig[0]
    toolkit.settings = replace(toolkit.settings, max_jobs=1)
    assert (await create(toolkit))["ok"]
    full = await create(toolkit)
    assert full["error"] == "job_limit_reached" and "at most 1 jobs" in full["detail"]
    hidden = read(await toolkit.schedule_list(all_conversations=True))
    assert hidden["error"] == "invalid_arguments" and "all_conversations" in hidden["detail"]
    assert "scheduling." not in full["detail"] + hidden["detail"]


@pytest.mark.asyncio
async def test_create_checks_name_schedule_and_target_in_order(rig: tuple[Any, ...]) -> None:
    toolkit, _, _, places, _, _ = rig
    invalid = read(await toolkit.schedule_create("bad\nname", "prompt", {"kind": "bad"}))
    assert invalid["error"] == "name_invalid"
    assert read(await toolkit.schedule_create("name", "prompt", {"kind": "bad"}))["error"] == "when_invalid"
    target = {"conversation": "elsewhere", "thread": ""}
    places.readable = False
    assert (await create(toolkit, deliver_to=target))["error"] == "target_not_readable"
    places.readable = True
    places.reachable = False
    assert (await create(toolkit, deliver_to=target))["error"] == "destination_unreachable"
    assert read(await toolkit.schedule_create("name", "prompt", {"kind": "once", "after_seconds": 600}, catch_up=None))["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_serving_shared_schedule_tools_preserves_platform_posting(rig: tuple[Any, ...]) -> None:
    toolkit, _, _, _, _, metadata = rig

    class Posting:
        def get_tool_definitions(self) -> list[dict[str, Any]]:
            return [{"name": "post", "description": "Post text.", "input_params": {"type": "object", "properties": {}}}]

        async def post(self) -> str:
            return '{"ok": true, "chat_id": "origin", "message_id": "m1"}'

    host = ToolHost([])
    host.serve("loopback", [Posting()], posting_tool="post")
    host.serve("", [toolkit])
    bound = BoundTurn("r1", metadata=metadata, platform="loopback")
    host.bind("origin", bound)
    result = await host.call(ToolCall(conversation="origin", name="post", arguments={}))
    assert read(cast(str, result))["ok"] and bound.delivered
    assert "schedule_create" in {item["name"] for item in host.definitions("origin")}


@pytest.mark.asyncio
async def test_command_place_exists_for_one_call_only(rig: tuple[Any, ...]) -> None:
    toolkit, _, _, _, _, metadata = rig
    assert (await create(toolkit))["ok"]
    metadata.pop("belle_place")
    at = InboundMessage("loopback", "", "origin", "message", "creator", "", thread="thread", chat_type="channel")
    assert read(await toolkit.invoke_at("schedule_list", {}, at=at))["jobs_total"] == 1
    assert read(await toolkit.schedule_list())["error"] == "trusted_conversation_context_required"
    assert read(await toolkit.invoke_at("other", {}, at=at))["error"] == "invalid_arguments"
    assert read(await toolkit.invoke_at("schedule_list", {"unknown": 1}, at=at))["error"] == "invalid_arguments"


async def test_schedule_runs_rows_name_run_and_session(rig: tuple[Any, ...]) -> None:
    toolkit, store, _, _, _, _ = rig
    job_id = (await create(toolkit, session="shared", overlap="parallel"))["job"]["job_id"]
    for _ in range(2):
        assert read(await toolkit.schedule_run_now(job_id))["ok"]
    async with store.transaction() as snapshot:
        shared = snapshot.jobs[job_id]["shared_session_id"]
        snapshot.jobs[job_id]["unrun"].append(
            {"due_iso_utc": "2026-10-18T11:00:00Z", "state": "skipped", "reason": "overlap", "claim_key": "k"})
    rows = read(await toolkit.schedule_runs(job_id=job_id))["runs"]
    runs = [row for row in rows if row["run_id"] is not None]
    assert [row["session_id"] for row in runs] == [shared, shared] and len({row["run_id"] for row in runs}) == 2
    (unrun,) = [row for row in rows if row["run_id"] is None]
    assert unrun["session_id"] is None and unrun["state"] == "skipped" and unrun["reason"] == "overlap"
