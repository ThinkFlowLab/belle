"""Host live peek tee against the scripted backend."""

import pytest
import pytest_asyncio
from conformance_kit import ModelTurn, worlds
from scripted_backend import ScriptedBackend, ScriptedTurn

from belle_agent.backend import (
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    SUBAGENT,
    Answer,
    TurnEvent,
)
from belle_agent.control.protocol import ControlError
from belle_agent.platforms.base import InboundAnswer


@pytest_asyncio.fixture
async def build_world(tmp_path, request):
    backend = ScriptedBackend(getattr(request, "param", None))
    async with worlds(backend, tmp_path) as build:
        yield build


async def test_peek_collects_text_while_subscribed(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    # Hold after narration so peek_history can seed text emitted before open.
    world.kit.play(ScriptedTurn(narration="partial-", final="done", hold_before_final=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    work_id = (await world.host.status_work())["rows"][0]["id"]
    opened = await world.host.peek_open(work_id)
    seeded = "".join(frame["payload"]["text"] for frame in opened["frames"] if frame["kind"] == "text")
    assert "partial-" in seeded
    world.kit.release()
    await world.host.idle()
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    texts = "".join(frame["payload"]["text"] for frame in polled["frames"] if frame["kind"] == "text")
    assert "done" in texts
    outcome = next(frame for frame in polled["frames"] if frame["kind"] == "outcome")
    assert outcome["payload"]["state"] == "succeeded"
    assert isinstance(outcome["payload"]["duration_seconds"], float)
    assert outcome["payload"]["duration_seconds"] >= 0
    assert outcome["payload"]["ended_at"]
    assert outcome["payload"]["extra"] is None
    await world.host.peek_close(opened["peek_id"])
    with pytest.raises(ControlError, match="peek_ended|closed"):
        await world.host.peek_poll(opened["peek_id"], 0)


async def test_peek_history_seeds_only_on_first_open(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ScriptedTurn(narration="early-", final="late", hold_before_final=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    work_id = (await world.host.status_work())["rows"][0]["id"]
    first = await world.host.peek_open(work_id)
    second = await world.host.peek_open(work_id)
    assert first["frames"]
    # Shared ring: second open sees the same frames, not a duplicated seed.
    assert [frame["seq"] for frame in second["frames"]] == [frame["seq"] for frame in first["frames"]]
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(first["peek_id"])
    await world.host.peek_close(second["peek_id"])


async def test_peek_tees_steps_tools_and_bounded_bodies(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    # Hold before the first step so peek can subscribe before structured events fire.
    world.kit.play(
        ScriptedTurn(
            hold=True,
            ask=("Ready to look?\nSay yes.", ("yes", "no")),
            calls=(("post_message", {"text": "noted"}),),
            final="done",
            hold_before_final=True,
        )
    )
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    work_id = (await world.host.status_work())["rows"][0]["id"]
    opened = await world.host.peek_open(work_id)
    world.kit.release()
    # Scripted ask pauses again until answered.
    await world.kit.holding()
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    question_frames = [frame for frame in polled["frames"] if frame["kind"] == "question"]
    assert len(question_frames) == 1
    assert question_frames[0]["payload"]["kind"] == "question"
    assert "Ready to look" in question_frames[0]["payload"]["prompt_summary"]
    assert "\n" not in question_frames[0]["payload"]["prompt_summary"]

    question_id = next(
        entry.question.id for entry in world.platform.log if getattr(entry, "question", None) is not None
    )
    assert (
        await world.platform.inject(
            InboundAnswer(
                platform="loopback",
                workspace="W1",
                conversation="C1",
                sender="U1",
                question_id=question_id,
                answer=Answer(values=("yes",)),
            )
        )
    ).startswith("answered")
    await world.kit.holding()
    after = await world.host.peek_poll(opened["peek_id"], 0)
    kinds = [frame["kind"] for frame in after["frames"]]
    # Scripted steps have no label; peek omits unlabeled step breaks.
    assert "step" not in kinds
    tool_calls = [frame for frame in after["frames"] if frame["kind"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["payload"]["name"] == "post_message"
    assert tool_calls[0]["payload"]["detail"] is None
    # Bounded args_text is optional expand content; compact detail stays empty here.
    assert tool_calls[0]["payload"]["args_text"] == '{"text": "noted"}'
    tool_results = [frame for frame in after["frames"] if frame["kind"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["payload"]["ok"] is True
    assert tool_results[0]["payload"].get("summary") is None
    # Scripted tool result text is present and bounded (not mid-string trimmed).
    assert isinstance(tool_results[0]["payload"].get("result_text"), str)
    assert tool_results[0]["payload"]["result_text"]
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])


async def test_peek_tool_body_falls_back_when_frame_exceeds_budget(build_world):
    """When args would blow control.peek_max_bytes, host tees meta-only (no trim)."""
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    work_id = turn.status_id
    # Tiny ring so a large args_text cannot fit as a single frame.
    world.host._peeks.max_bytes = 180
    opened = await world.host.peek_open(work_id)
    world.host._peek_event(
        turn,
        TurnEvent(
            EVENT_TOOL_CALLED,
            data={
                "call_id": "c-big",
                "name": "shell",
                "arguments": {"description": "big", "blob": "z" * 400},
            },
        ),
    )
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    calls = [frame for frame in polled["frames"] if frame["kind"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["payload"]["name"] == "shell"
    assert calls[0]["payload"]["detail"] == "big"
    # Body omitted (helper oversize or frame refuse fallback) — never a truncated blob.
    assert calls[0]["payload"]["args_text"] is None
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])


async def test_peek_on_subagent_filters_to_attributed_frames(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    main_id = turn.status_id
    opened_main = await world.host.peek_open(main_id)
    world.host._track_subagent(
        turn, TurnEvent(EVENT_TOOL_CALLED, data={"call_id": "c-sub", SUBAGENT: "research", "name": "task"}),
    )
    child_id = turn.subagents["c-sub"].id
    world.host._peek_event(
        turn, TurnEvent(EVENT_TOOL_CALLED, data={"call_id": "c-sub", SUBAGENT: "research", "name": "task"}),
    )
    world.host._peek_event(turn, TurnEvent(EVENT_TEXT_DELTA, text="main only"))
    world.host._peek_event(
        turn, TurnEvent(EVENT_TOOL_RESULT, data={"call_id": "c-sub", "name": "task"}, text='{"ok": true}'),
    )
    opened_child = await world.host.peek_open(child_id)
    assert opened_child["work_id"] == child_id
    child_frames = await world.host.peek_poll(opened_child["peek_id"], 0)
    assert [(frame["kind"], frame["source_work_id"]) for frame in child_frames["frames"]] == [
        ("tool_call", child_id),
        ("tool_result", child_id),
    ]
    main_frames = await world.host.peek_poll(opened_main["peek_id"], 0)
    kinds = [frame["kind"] for frame in main_frames["frames"]]
    assert kinds.count("tool_call") == 1 and kinds.count("text") == 1 and kinds.count("tool_result") == 1
    assert any(frame["source_work_id"] == child_id for frame in main_frames["frames"] if frame["kind"] == "tool_call")
    assert any(frame["source_work_id"] == main_id for frame in main_frames["frames"] if frame["kind"] == "text")
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened_main["peek_id"])
    await world.host.peek_close(opened_child["peek_id"])


async def test_oversized_peek_text_does_not_fail_the_turn(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    work_id = turn.status_id
    world.host._peeks.max_bytes = 256
    opened = await world.host.peek_open(work_id)
    # Direct tee path used by _read: must not raise into the turn.
    world.host._peek_event(turn, TurnEvent(EVENT_TEXT_DELTA, text="y" * 70_000))
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    assert polled["frames"]
    assert all(frame["kind"] == "text" for frame in polled["frames"])
    world.kit.release()
    await world.host.idle()
    assert world.outcomes()
    await world.host.peek_close(opened["peek_id"])


async def test_child_text_delta_is_excluded_from_platform_progress(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="parent-final", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    work_id = turn.status_id
    opened = await world.host.peek_open(work_id)
    world.host._track_subagent(
        turn, TurnEvent(EVENT_TOOL_CALLED, data={"call_id": "c-sub", SUBAGENT: "research", "name": "task"}),
    )
    await world.host._show(turn, TurnEvent(EVENT_TEXT_DELTA, text="parent-draft"))
    await world.host._show(
        turn, TurnEvent(EVENT_TEXT_DELTA, text="child-draft", data={"call_id": "c-sub", "child": True}),
    )
    world.host._peek_event(
        turn, TurnEvent(EVENT_TEXT_DELTA, text="child-draft", data={"call_id": "c-sub", "child": True}),
    )
    progressed = "".join(
        event.text for _, event in world.platform.progressed if event.kind == EVENT_TEXT_DELTA
    )
    assert "parent-draft" in progressed
    assert "child-draft" not in progressed
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    peek_text = "".join(frame["payload"]["text"] for frame in polled["frames"] if frame["kind"] == "text")
    assert "child-draft" in peek_text
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])


async def test_peek_open_during_blocked_history_does_not_stay_live(build_world, monkeypatch):
    import asyncio

    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    work_id = (await world.host.status_work())["rows"][0]["id"]
    gate = asyncio.Event()

    async def blocked_history(conversation, request_id):
        await gate.wait()
        return ()

    monkeypatch.setattr(world.host._backend, "peek_history", blocked_history)
    open_task = asyncio.create_task(world.host.peek_open(work_id))
    await asyncio.sleep(0)
    assert not open_task.done()
    world.kit.release()
    await world.host.idle()
    gate.set()
    opened = await open_task
    assert opened["live"] is False
    try:
        polled = await world.host.peek_poll(opened["peek_id"], 0)
        assert polled["live"] is False
    except ControlError as exc:
        assert exc.code == "peek_ended"


async def test_cancelled_peek_open_reclaims_seed_and_does_not_lose_later_text(build_world, monkeypatch):
    """Last cancelled opener drops the reserved buffer and seed task so a retry tees live text."""
    import asyncio

    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    work_id = turn.status_id
    gate = asyncio.Event()

    async def blocked_history(conversation, request_id):
        await gate.wait()
        return ()

    monkeypatch.setattr(world.host._backend, "peek_history", blocked_history)
    open_task = asyncio.create_task(world.host.peek_open(work_id))
    await asyncio.sleep(0)
    assert work_id in world.host._peek_seeding
    open_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await open_task
    assert work_id not in world.host._peek_open_waiters
    assert work_id not in world.host._peek_seeding
    assert world.host._peeks.has_buffer(work_id) is False
    gate.set()
    await asyncio.sleep(0)
    opened = await world.host.peek_open(work_id)
    world.host._peek_event(turn, TurnEvent(EVENT_TEXT_DELTA, text="MISSING-X"))
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    texts = "".join(frame["payload"]["text"] for frame in polled["frames"] if frame["kind"] == "text")
    assert "MISSING-X" in texts
    assert polled["lost"] == 0
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])


async def test_peek_open_replays_stashed_outcome(build_world):
    """Outcome teed with no buffer is stashed and applied on the next open."""
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    work_id = turn.status_id
    world.host._peek_event(turn, TurnEvent(EVENT_TEXT_DELTA, text="before-end"))
    world.host._peek_outcome(turn, "failed", extra="boom")
    assert turn.peek_outcome is not None
    assert world.host._peeks.has_buffer(work_id) is False
    opened = await world.host.peek_open(work_id)
    kinds = [frame["kind"] for frame in opened["frames"]]
    assert "outcome" in kinds
    outcome = next(frame for frame in opened["frames"] if frame["kind"] == "outcome")
    assert outcome["payload"]["state"] == "failed"
    assert outcome["payload"]["extra"] == "boom"
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])


async def test_peek_outcome_retries_without_huge_extra(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    work_id = turn.status_id
    world.host._peeks.max_bytes = 200
    opened = await world.host.peek_open(work_id)
    world.host._peek_outcome(turn, "failed", extra="x" * 70_000)
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    outcome = next(frame for frame in polled["frames"] if frame["kind"] == "outcome")
    assert outcome["payload"]["state"] == "failed"
    assert outcome["payload"]["extra"] is None
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])


async def test_forget_active_work_ends_peek_buffer(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n    delivery: {mode: [all]}\n"
    )
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    turn = next(iter(world.host._active_work.values()))
    work_id = turn.status_id
    opened = await world.host.peek_open(work_id)
    world.host._forget_active_work(work_id)
    assert work_id not in world.host._active_work
    assert world.host._peeks.has_buffer(work_id)
    assert world.host._peeks._buffers[work_id].live is False
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])


async def test_peek_and_work_rows_take_turn_usage_tokens(build_world):
    world = await build_world(
        "scopes:\n  - match: {channel: loopback, chat: C1}\n"
        "    agent: {model_name: default-model}\n"
        "    delivery: {mode: [all]}\n"
    )
    world.kit.play(ScriptedTurn(
        final="done", cost=0.4, tokens_in=100, tokens_out=20,
        hold=True, hold_before_final=True,
    ))
    await world.platform.inject(world.message("one", thread="t"))
    await world.kit.holding()
    row = (await world.host.status_work())["rows"][0]
    assert row["model"] == "default-model"
    assert row["tokens_in"] is row["tokens_out"] is None
    opened = await world.host.peek_open(row["id"])
    world.kit.release()
    await world.kit.holding()
    row = (await world.host.status_work())["rows"][0]
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 20
    polled = await world.host.peek_poll(opened["peek_id"], 0)
    usage = next(frame for frame in polled["frames"] if frame["kind"] == "usage")
    assert usage["payload"] == {"tokens_in": 100, "tokens_out": 20, "cost": 0.4}
    world.kit.release()
    await world.host.idle()
    await world.host.peek_close(opened["peek_id"])
