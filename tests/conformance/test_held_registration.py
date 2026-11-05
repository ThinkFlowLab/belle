"""Held approvals remain answerable while platform calls are in flight."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conformance_kit import LoopbackKit, worlds
from scripted_backend import ScriptedBackend

from belle_agent.backend import Question, ToolCall
from belle_agent.host import _HeldCall


def _pending(world):
    turn = world.host._turn(world.direct("ordinary"), "ordinary", {}, reply_optional=False)
    turn.started_at = world.host.clock()
    world.host._admission.remember_turn(turn.conversation, turn, cap=256, timeout=3600, warn=lambda *_: None)
    call = ToolCall(conversation=turn.conversation, name="post_message", arguments={"text": "new"})
    old = _HeldCall(
        turn=turn,
        call=ToolCall(conversation=turn.conversation, name="post_message", arguments={"text": "old"}),
        asked_at=world.host.clock(),
        deadline=world.host.clock() + 3600,
    )
    world.host._held["old"] = old
    timer = asyncio.create_task(world.host._expire_held_call("old", old))
    world.host._held_timeouts["old"] = timer
    return turn, call, old, timer


@pytest.mark.parametrize("pause", ["ask", "ask_failure", "eviction"])
async def test_approval_during_platform_ask_or_eviction_runs_once(tmp_path, monkeypatch, pause):
    monkeypatch.setattr("belle_agent.host.MAX_PENDING_QUESTIONS", 1)
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn, call, old, old_timer = _pending(world)
        entered, release = asyncio.Event(), asyncio.Event()
        shown = []
        platform_ask = world.platform.ask
        confirmed = AsyncMock(return_value='{"ok": true}')
        monkeypatch.setattr(world.host._tools, "run_confirmed", confirmed)

        async def ask(conversation, thread, question, **where):
            shown.append(SimpleNamespace(conversation=conversation, question=question))
            result = await platform_ask(conversation, thread, question, **where)
            if pause != "eviction":
                entered.set()
                await release.wait()
            if pause == "ask_failure":
                raise RuntimeError("question delivery failed after its answer")
            return result

        async def withdraw(question_id, *, workspace):
            assert question_id == "old"
            entered.set()
            await release.wait()

        world.platform.ask = ask
        world.platform.withdraw_question = withdraw
        pending = asyncio.create_task(world.host._hold_call(call, turn.bound, Question(id="new", text="Allow?")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            question_id = shown[0].question.id
            timer = world.host._held_timeouts[question_id]
            answer = world.answer_to(shown[0], approved=True)

            assert (await world.host._receive_answer(answer)).startswith("answered")
            assert await world.host._receive_answer(answer) == "ignored:question-not-waiting"
            release.set()
            assert await asyncio.wait_for(pending, 2)
            await asyncio.gather(timer, return_exceptions=True)

            confirmed.assert_awaited_once_with(call, turn.bound)
            assert question_id not in world.host._held
            assert question_id not in world.host._held_timeouts
            assert timer.cancelled()
            if pause != "eviction":
                assert world.host._held == {"old": old}
                assert not old_timer.cancelled()
            else:
                assert world.host._held == {}
                await asyncio.gather(old_timer, return_exceptions=True)
                assert old_timer.cancelled()
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_unsuccessful_ask_removes_only_its_pending_approval(tmp_path, monkeypatch, cancel):
    monkeypatch.setattr("belle_agent.host.MAX_PENDING_QUESTIONS", 1)
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn, call, old, old_timer = _pending(world)
        entered = asyncio.Event()
        shown = []
        withdrawn = []

        async def ask(conversation, thread, question, **where):
            shown.append(question.id)
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("question delivery failed")

        async def withdraw(question_id, *, workspace):
            withdrawn.append(question_id)

        world.platform.ask = ask
        world.platform.withdraw_question = withdraw
        pending = asyncio.create_task(world.host._hold_call(call, turn.bound, Question(id="new", text="Allow?")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if cancel:
                timer = world.host._held_timeouts[shown[0]]
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                await asyncio.gather(timer, return_exceptions=True)
                assert timer.cancelled()
            else:
                assert not await pending

            assert world.host._held == {"old": old}
            assert world.host._held_timeouts == {"old": old_timer}
            assert not old_timer.cancelled()
            assert withdrawn == shown
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_held_call_that_expires_during_platform_ask_is_refused(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn, call, old, old_timer = _pending(world)
        turn.session_run = SimpleNamespace(questions="ask_target")
        world.host.scheduler = SimpleNamespace(settings=SimpleNamespace(question_timeout_seconds=1), stop=AsyncMock())
        expired = asyncio.Event()
        shown = []
        timers = []

        async def ask(conversation, thread, question, **where):
            shown.append(question.id)
            timers.append(world.host._held_timeouts[question.id])
            await asyncio.wait_for(expired.wait(), 2)

        async def withdraw(question_id, *, workspace):
            assert question_id == shown[0]
            expired.set()

        world.platform.ask = ask
        world.platform.withdraw_question = withdraw

        assert not await world.host._hold_call(call, turn.bound, Question(id="new", text="Allow?"))

        assert world.host._held == {"old": old}
        assert world.host._held_timeouts == {"old": old_timer}
        assert ("loopback", shown[0]) in world.host._expired_questions
        assert timers[0].done()
