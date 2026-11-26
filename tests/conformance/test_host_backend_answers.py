"""A backend question stays open until the backend takes its answer."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conformance_kit import LoopbackKit, worlds
from scripted_backend import ScriptedBackend

from belle_agent.backend import Answer, BackendError, Question

QUESTION = Question(id="q1", text="Colour?", options=("red", "blue"))
KEY = ("loopback", "q1")


class Refuses(ScriptedBackend):
    """Refuses the answers ``refusals`` names, as a backend does with one it cannot deliver. ``held`` holds each
    answer until it is set."""

    def __init__(self, *refusals: BackendError) -> None:
        super().__init__()
        self.refusals = list(refusals)
        self.held: asyncio.Event | None = None

    async def answer(self, conversation: str, question_id: str, answer: Answer) -> None:
        if self.held is not None:
            await self.held.wait()
        if self.refusals:
            raise self.refusals.pop(0)
        await super().answer(conversation, question_id, answer)


async def _asked(world, *, scheduled: bool = False):
    turn = world.host._turn(world.direct("ask"), "schedule_job_due", {}, reply_optional=False)
    if scheduled:
        world.host.scheduler = SimpleNamespace(settings=SimpleNamespace(question_timeout_seconds=1), stop=AsyncMock())
        turn.session_run = SimpleNamespace(questions="ask_target")
    await world.host._ask(turn, QUESTION)
    (shown,) = world.questions()
    return shown


async def test_an_answer_the_backend_does_not_take_leaves_the_question_open_for_another(tmp_path):
    async with worlds(Refuses(BackendError("the answer 'green' is none of the options")), tmp_path, LoopbackKit()) as build:
        world = await build()
        shown = await _asked(world)

        refused = await world.platform.inject(world.answer_to(shown, values=("green",)))
        assert refused.startswith("undelivered:backend") and "none of the options" in refused
        assert KEY in world.host._questions

        answered = await world.platform.inject(world.answer_to(shown, values=("red",)))
        assert answered.startswith("answered")
        assert world.kit.answers[-1][2] == Answer(values=("red",))
        assert KEY not in world.host._questions


async def test_a_second_answer_while_the_first_is_delivered_is_ignored(tmp_path):
    async with worlds(Refuses(), tmp_path, LoopbackKit()) as build:
        world = await build()
        world.kit.held = asyncio.Event()
        shown = await _asked(world)

        first = asyncio.create_task(world.platform.inject(world.answer_to(shown, values=("red",))))
        await asyncio.sleep(0.05)
        assert await world.platform.inject(world.answer_to(shown, values=("blue",))) == "ignored:question-being-answered"
        world.kit.held.set()

        assert (await first).startswith("answered")
        assert [answer for _, _, answer in world.kit.answers] == [Answer(values=("red",))]


async def test_a_question_expires_after_an_answer_held_across_its_timeout_is_refused(tmp_path):
    async with worlds(Refuses(BackendError("the server is away")), tmp_path, LoopbackKit()) as build:
        world = await build()
        world.kit.held = asyncio.Event()
        shown = await _asked(world, scheduled=True)

        answering = asyncio.create_task(world.platform.inject(world.answer_to(shown, values=("red",))))
        await asyncio.sleep(1.2)  # past the question's timeout, with the answer still held
        assert world.kit.answers == [] and KEY in world.host._questions
        world.kit.held.set()

        assert (await answering).startswith("undelivered")
        await asyncio.sleep(0.05)
        assert world.kit.answers[-1][2] == Answer(values=("reject",), approved=False)
        assert KEY not in world.host._questions


async def test_a_question_does_not_expire_after_an_answer_held_across_its_timeout_is_taken(tmp_path):
    async with worlds(Refuses(), tmp_path, LoopbackKit()) as build:
        world = await build()
        world.kit.held = asyncio.Event()
        shown = await _asked(world, scheduled=True)

        answering = asyncio.create_task(world.platform.inject(world.answer_to(shown, values=("red",))))
        await asyncio.sleep(1.2)
        world.kit.held.set()

        assert (await answering).startswith("answered")
        await asyncio.sleep(0.05)
        assert [answer for _, _, answer in world.kit.answers] == [Answer(values=("red",))]
