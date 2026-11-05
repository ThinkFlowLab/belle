"""Scheduled runs cross the host and platform boundary."""

import json

import pytest
from conformance_kit import ModelTurn, worlds
from scripted_backend import ScriptedBackend

from belle_agent.control.protocol import ControlError
from belle_agent.platforms.base import InboundMessage
from belle_agent.scheduling.address import Address


async def _runs(toolkit, job_id):  # type: ignore[no-untyped-def]
    async with toolkit.store.transaction() as snapshot:
        return [run for session in snapshot.job_sessions(job_id) for run in session["runs"]]


async def test_manual_run_posts_in_its_target_and_records_completion(kit, tmp_path):
    async with worlds(kit, tmp_path) as build:
        world = await build()
        direct = await world.platform.open_direct_message("U1")
        at = InboundMessage("loopback", "", direct, "m1", "U1", "schedule this", chat_type="dm")
        toolkit = world.host.schedule_toolkit
        assert toolkit is not None
        assert "schedule_create" in {item["name"] for item in world.host.tools.definitions("schedule_test")}

        made = json.loads(await toolkit.invoke_at("schedule_create", {
            "name": "Reminder", "prompt": "Say hello", "when": {"kind": "once", "after_seconds": 600},
        }, at=at))
        assert made["ok"], made
        kit.play(ModelTurn(final="hello"))
        started = json.loads(await toolkit.invoke_at("schedule_run_now", {"job_id": made["job"]["job_id"]}, at=at))
        assert started["ok"]
        await world.host.idle()

        assert [post.text for post in world.posts(direct)] == ["hello"]
        assert world.requests[0].metadata["belle_schedule"]["job_id"] == made["job"]["job_id"]
        (record,) = await _runs(toolkit, made["job"]["job_id"])
        assert record["state"] == "succeeded"
        assert record["outcome"] == "posted-final"
        assert record["posted"][0]["conversation"] == direct


async def test_scheduled_silence_records_success_without_a_post(tmp_path):
    backend = ScriptedBackend()
    async with worlds(backend, tmp_path) as build:
        world = await build()
        direct = await world.platform.open_direct_message("U1")
        at = InboundMessage("loopback", "", direct, "m1", "U1", "schedule this", chat_type="dm")
        toolkit = world.host.schedule_toolkit
        assert toolkit is not None
        made = json.loads(await toolkit.invoke_at("schedule_create", {
            "name": "Quiet", "prompt": "Check quietly", "when": {"kind": "once", "after_seconds": 600},
        }, at=at))
        backend.play(ModelTurn(final="NO_REPLY"))
        assert json.loads(await toolkit.invoke_at("schedule_run_now", {"job_id": made["job"]["job_id"]}, at=at))["ok"]
        await world.host.idle()

        assert world.posts(direct) == []
        assert world.requests[0].scope.system_additions[0].startswith("[trigger: schedule]")
        (record,) = await _runs(toolkit, made["job"]["job_id"])
        assert record["state"] == "succeeded"
        assert record["outcome"] == "withheld"


async def test_scheduler_disabled_does_not_register_tools_or_create_state(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path) as build:
        world = await build("scheduling: {engine: disabled}")
        assert world.host.schedule_toolkit is None
        assert world.host.scheduler is None
        assert "schedule_create" not in {item["name"] for item in world.host.tools.definitions("plain")}
        assert not (tmp_path / "state" / "sessions.json").exists()


async def test_model_choice_is_not_a_scheduled_scope_limit(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path) as build:
        world = await build()
        direct = await world.platform.open_direct_message("U1")
        world.host.model_choices[("", direct)] = "personal-preference"
        limits = world.host.scope_limits(Address("loopback", "", direct, ""), "U1", "dm")
        assert limits.scope.model == ""


async def test_stopping_a_scheduled_turn_marks_its_run_cancelled(tmp_path):
    backend = ScriptedBackend()
    async with worlds(backend, tmp_path) as build:
        world = await build()
        direct = await world.platform.open_direct_message("U1")
        at = InboundMessage("loopback", "", direct, "m1", "U1", "schedule this", chat_type="dm")
        toolkit = world.host.schedule_toolkit
        assert toolkit is not None
        made = json.loads(await toolkit.invoke_at("schedule_create", {
            "name": "Long run", "prompt": "Wait", "when": {"kind": "once", "after_seconds": 600},
        }, at=at))
        backend.play(ModelTurn(final="should not post", hold=True))
        assert json.loads(await toolkit.invoke_at("schedule_run_now", {"job_id": made["job"]["job_id"]}, at=at))["ok"]
        await backend.holding()

        # A scheduled run posts no card and has no message id, so an operator stops it as running work.
        (work,) = [turn.status_id for turn in world.host._active_work.values()]
        await world.host.stop_work(work, "running")
        await world.host.idle()

        assert world.posts(direct) == []
        (record,) = await _runs(toolkit, made["job"]["job_id"])
        assert record["state"] == "cancelled"
        assert record["stop_requested"]


@pytest.mark.parametrize("fails", ["returns false", "raises"])
async def test_a_stop_that_fails_leaves_a_scheduled_run_unmarked(tmp_path, fails):
    backend = ScriptedBackend()

    async def cancel(conversation: str) -> bool:
        if fails == "raises":
            raise RuntimeError("the backend refused the stop")
        return False

    backend.cancel = cancel  # type: ignore[method-assign]
    async with worlds(backend, tmp_path) as build:
        world = await build()
        direct = await world.platform.open_direct_message("U1")
        at = InboundMessage("loopback", "", direct, "m1", "U1", "schedule this", chat_type="dm")
        toolkit = world.host.schedule_toolkit
        assert toolkit is not None
        made = json.loads(await toolkit.invoke_at("schedule_create", {
            "name": "Long run", "prompt": "Wait", "when": {"kind": "once", "after_seconds": 600},
        }, at=at))
        backend.play(ModelTurn(final="posted after all", hold=True))
        assert json.loads(await toolkit.invoke_at("schedule_run_now", {"job_id": made["job"]["job_id"]}, at=at))["ok"]
        await backend.holding()

        (turn,) = world.host._active_work.values()
        with pytest.raises(RuntimeError if fails == "raises" else ControlError):
            await world.host.stop_work(turn.status_id, "running")
        (record,) = await _runs(toolkit, made["job"]["job_id"])
        assert (record["state"], record["stop_requested"], turn.bound.stopped) == ("running", False, False)
        backend.release()
        await world.host.idle()

        (record,) = await _runs(toolkit, made["job"]["job_id"])
        assert record["state"] == "succeeded"
        assert [post.text for post in world.posts(direct)] == ["posted after all"]


async def test_parallel_shared_runs_keep_both_records_but_serialize_backend_turns(tmp_path):
    backend = ScriptedBackend()
    async with worlds(backend, tmp_path) as build:
        world = await build()
        direct = await world.platform.open_direct_message("U1")
        at = InboundMessage("loopback", "", direct, "m1", "U1", "schedule this", chat_type="dm")
        toolkit = world.host.schedule_toolkit
        assert toolkit is not None
        made = json.loads(await toolkit.invoke_at("schedule_create", {
            "name": "Shared", "prompt": "One turn at a time", "when": {"kind": "once", "after_seconds": 600},
            "session": "shared", "overlap": "parallel",
        }, at=at))
        job_id = made["job"]["job_id"]
        backend.play(ModelTurn(final="first", hold=True), ModelTurn(final="second"))
        for _ in range(2):
            assert json.loads(await toolkit.invoke_at("schedule_run_now", {"job_id": job_id}, at=at))["ok"]
        await backend.holding()
        assert len(world.requests) == 1
        backend.release()
        await world.host.idle()

        assert [post.text for post in world.posts(direct)] == ["first", "second"]
        assert world.requests[0].conversation == world.requests[1].conversation
        records = await _runs(toolkit, job_id)
        assert [record["state"] for record in records] == ["succeeded", "succeeded"]


async def test_delete_stops_a_shared_run_while_it_waits_for_its_turn(tmp_path):
    backend = ScriptedBackend()
    async with worlds(backend, tmp_path) as build:
        world = await build()
        direct = await world.platform.open_direct_message("U1")
        at = InboundMessage("loopback", "", direct, "m1", "U1", "schedule this", chat_type="dm")
        toolkit = world.host.schedule_toolkit
        assert toolkit is not None
        made = json.loads(await toolkit.invoke_at("schedule_create", {
            "name": "Shared", "prompt": "Wait", "when": {"kind": "once", "after_seconds": 600},
            "session": "shared", "overlap": "parallel",
        }, at=at))
        job_id = made["job"]["job_id"]
        backend.play(ModelTurn(final="first", hold=True), ModelTurn(final="must not start"))
        for _ in range(2):
            assert json.loads(await toolkit.invoke_at("schedule_run_now", {"job_id": job_id}, at=at))["ok"]
        await backend.holding()
        deleted = json.loads(await toolkit.invoke_at("schedule_delete", {
            "job_id": job_id, "keep_history": True,
        }, at=at))
        assert deleted["ok"]
        await world.host.idle()

        assert len(world.requests) == 1
        assert world.posts(direct) == []
        records = await _runs(toolkit, job_id)
        assert [record["state"] for record in records] == ["cancelled", "cancelled"]
