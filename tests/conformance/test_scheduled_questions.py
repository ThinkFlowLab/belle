"""Questions from scheduled turns never wait indefinitely or release a late write."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conformance_kit import LoopbackKit, worlds
from scripted_backend import ScriptedBackend

from belle_agent.backend import EVENT_SUCCEEDED, Question, ToolCall, TurnEvent
from belle_agent.host import OUTCOME_POSTED_FINAL, _HeldCall


async def test_refuse_mode_answers_backend_questions_with_reject(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn = world.host._turn(world.direct("scheduled"), "schedule_job_due", {}, reply_optional=False)
        turn.session_run = SimpleNamespace(questions="refuse")

        await world.host._ask(turn, Question(id="q1", text="Continue?", options=("yes", "no")))

        assert world.kit.answers[-1][2].values == ("reject",)
        assert world.questions() == []


async def test_ask_target_backend_question_expires_and_is_rejected(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        world.host.scheduler = SimpleNamespace(settings=SimpleNamespace(question_timeout_seconds=1), stop=AsyncMock())
        withdrawn = []

        async def withdraw_question(question_id, *, workspace):
            withdrawn.append(question_id)

        world.platform.withdraw_question = withdraw_question
        turn = world.host._turn(world.direct("scheduled"), "schedule_job_due", {}, reply_optional=False)
        turn.session_run = SimpleNamespace(questions="ask_target")
        question = Question(id="q2", text="Continue?", options=("yes", "no"))

        await world.host._ask(turn, question)
        await asyncio.sleep(1.05)

        assert world.kit.answers[-1][2].values == ("reject",)
        assert ("loopback", "q2") not in world.host._questions
        assert withdrawn == ["q2"]


async def test_refuse_mode_does_not_prompt_for_a_held_write(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn = world.host._turn(world.direct("scheduled"), "schedule_job_due", {}, reply_optional=False)
        turn.session_run = SimpleNamespace(questions="refuse")
        world.host._admission.remember_turn(
            turn.conversation, turn, cap=256, timeout=3600, warn=lambda *_: None
        )
        call = ToolCall(conversation=turn.conversation, name="post_message", arguments={"text": "private"})

        asked = await world.host._hold_call(call, turn.bound, Question(id="q2", text="Allow?"))

        assert not asked
        assert world.questions() == []
        assert world.host._held == {}


async def test_held_scheduled_write_expires_when_its_run_ends(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn = world.host._turn(world.direct("scheduled"), "schedule_job_due", {}, reply_optional=False)
        turn.session_run = SimpleNamespace(questions="ask_target")
        call = ToolCall(conversation=turn.conversation, name="post_message", arguments={"text": "private"})
        held = _HeldCall(turn=turn, call=call, asked_at=world.host.clock(), deadline=world.host.clock() + 1)
        world.host._held["q3"] = held

        withdrawn = []

        async def withdraw_question(question_id, *, workspace):
            withdrawn.append(question_id)

        world.platform.withdraw_question = withdraw_question
        await world.host._expire_held_at_turn_end(turn)
        result = await world.host._receive_answer(
            world.answer_to(
                SimpleNamespace(conversation="D-U1", question=SimpleNamespace(id="q3")),
                approved=True,
            )
        )

        assert result == "refused:question-expired"
        assert world.posts("C2") == []
        assert withdrawn == ["q3"]


async def test_held_destructive_call_expires_when_its_ordinary_turn_ends(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn = world.host._turn(world.direct("ordinary"), "ordinary", {}, reply_optional=False)
        call = ToolCall(conversation=turn.conversation, name="erase", arguments={})
        world.host._tools.requires_active_turn = lambda platform, name: name == "erase"
        world.host._held["q3"] = _HeldCall(
            turn=turn, call=call, asked_at=world.host.clock(), deadline=world.host.clock() + 1
        )
        withdrawn = []

        async def withdraw_question(question_id, *, workspace):
            withdrawn.append(question_id)

        world.platform.withdraw_question = withdraw_question
        await world.host._expire_held_at_turn_end(turn)
        result = await world.host._receive_answer(
            world.answer_to(SimpleNamespace(conversation="D-U1", question=SimpleNamespace(id="q3")), approved=True)
        )

        assert result == "refused:question-expired"
        assert withdrawn == ["q3"]
        assert not world.host._held


async def test_an_expired_answer_whose_notice_is_refused_is_still_refused(tmp_path, caplog):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn = world.host._turn(world.direct("ordinary"), "ordinary", {}, reply_optional=False)
        world.host._remember_expired((turn.inbound.platform, "q4"), turn)
        world.platform.post = AsyncMock(side_effect=RuntimeError("a post for one person needs a permission"))

        with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
            result = await world.host._receive_answer(
                world.answer_to(SimpleNamespace(conversation="D-U1", question=SimpleNamespace(id="q4")), approved=True)
            )

        assert result == "refused:question-expired"
        assert "needs a permission" in caplog.text


async def test_held_scheduled_write_expires_at_its_timeout(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        world.host.scheduler = SimpleNamespace(settings=SimpleNamespace(question_timeout_seconds=1), stop=AsyncMock())
        turn = world.host._turn(world.direct("scheduled"), "schedule_job_due", {}, reply_optional=False)
        turn.session_run = SimpleNamespace(questions="ask_target")
        call = ToolCall(conversation=turn.conversation, name="post_message", arguments={"text": "private"})
        held = _HeldCall(turn=turn, call=call, asked_at=world.host.clock(), deadline=world.host.clock() + 1)
        world.host._held["q4"] = held
        world.host._held_timeouts["q4"] = asyncio.create_task(world.host._expire_held_call("q4", held))
        withdrawn = []

        async def withdraw_question(question_id, *, workspace):
            withdrawn.append(question_id)

        world.platform.withdraw_question = withdraw_question

        await asyncio.sleep(1.05)
        result = await world.host._receive_answer(
            world.answer_to(
                SimpleNamespace(conversation="D-U1", question=SimpleNamespace(id="q4")),
                approved=True,
            )
        )

        assert result == "refused:question-expired"
        assert world.posts("C2") == []
        assert withdrawn == ["q4"]


async def test_an_ordinary_held_call_timeout_withdraws_its_question(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        turn = world.host._turn(world.direct("ordinary"), "ordinary", {}, reply_optional=False)
        call = ToolCall(conversation=turn.conversation, name="post_message", arguments={"text": "private"})
        held = _HeldCall(turn=turn, call=call, asked_at=world.host.clock(), deadline=world.host.clock() + 0.01)
        world.host._held["q5"] = held
        withdrawn = []

        async def withdraw_question(question_id, *, workspace):
            withdrawn.append(question_id)

        world.platform.withdraw_question = withdraw_question
        await world.host._expire_held_call("q5", held)

        assert "q5" not in world.host._held
        assert withdrawn == ["q5"]


async def test_expired_click_records_are_pruned_by_age(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        world.host.scheduler = SimpleNamespace(settings=SimpleNamespace(question_timeout_seconds=1), stop=AsyncMock())
        turn = world.host._turn(world.direct("scheduled"), "schedule_job_due", {}, reply_optional=False)
        now = [100.0]
        world.host.clock = lambda: now[0]

        world.host._remember_expired(("loopback", "old"), turn)
        now[0] += 1
        world.host._remember_expired(("loopback", "new"), turn)

        assert set(world.host._expired_questions) == {("loopback", "new")}


async def test_ended_scheduled_turn_expires_its_pending_question(tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, LoopbackKit()) as build:
        world = await build()
        host = world.host
        turn = host._turn(world.direct("scheduled"), "schedule_job_due", {}, reply_optional=False)
        turn.session_run = SimpleNamespace(questions="ask_target", turn={}, session_id="ses-1234567890",
                                           run_id="run-1234567890", delivery="post_final")
        host.scheduler.ended_run = AsyncMock()
        key = ("loopback", "q-end")
        host._questions[key] = turn
        timer = asyncio.create_task(asyncio.sleep(3600))
        host._question_timeouts[key] = timer
        host._terminal_event = AsyncMock(return_value=TurnEvent(EVENT_SUCCEEDED, text="done"))
        host._deliver = AsyncMock(return_value=OUTCOME_POSTED_FINAL)

        await host._run(turn)

        assert key not in host._questions
        assert key in host._expired_questions
        assert key not in host._question_timeouts
        await asyncio.gather(timer, return_exceptions=True)
        assert timer.cancelled()
        answer = world.answer_to(
            SimpleNamespace(conversation="D-U1", question=SimpleNamespace(id="q-end")), approved=True
        )
        assert await host._receive_answer(answer) == "refused:question-expired"
