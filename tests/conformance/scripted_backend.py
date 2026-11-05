"""A backend that plays scripted model turns against the tool host.

It runs no model and no harness. It is the first parameter of the conformance
suite, and what proves the host and a platform before any adapter exists.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from conformance_kit import ModelTurn

from belle_agent.backend import (
    APPROVAL,
    CATALOG_KINDS,
    DUPLICATE,
    EVENT_ACCEPTED,
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_QUESTION,
    EVENT_STEP,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    EVENT_USAGE,
    NOT_STEERED,
    OP_ACTIVE,
    OP_ANSWER,
    OP_ASK,
    OP_CANCEL,
    OP_CATALOG,
    OP_PEEK_HISTORY,
    PROGRESS_EVENTS,
    SCOPE_FIELDS,
    Answer,
    BackendCapabilities,
    Catalog,
    PeekHistoryPart,
    Question,
    ToolCall,
    ToolHost,
    TurnEvent,
    TurnRequest,
)
from belle_agent.models import ConfiguredModels
from belle_agent.scopes import LEVEL_ASK
from belle_agent.tools.host import result_text

#: The option values a scripted approval offers, matching the loopback platform.
APPROVE = "approve"
REJECT = "reject"


@dataclass(frozen=True)
class ScriptedTurn(ModelTurn):
    """A model turn, plus what only this backend can be told to do."""

    #: Fail the turn with this reason, as a provider error would.
    error: str = ""
    #: Raise from the event stream.
    crash: bool = False
    #: Ask this and wait for the answer before going on.
    question: Question | None = None
    #: Make the tool calls after a stop all the same, as work that has not noticed it yet.
    ignores_stop: bool = False
    #: Report this as what the turn has cost, before its first step.
    cost: float | None = None
    #: Optional turn-cumulative token counts on the same USAGE event as ``cost``.
    tokens_in: int | None = None
    tokens_out: int | None = None
    #: Write this before the tool calls, in a step that is not the last: text that is no part of the final.
    narration: str = ""
    #: Raise from the event stream once half of the final has been written, as a backend that is lost mid-reply.
    crash_mid_reply: bool = False
    #: Wait for the kit's ``release()`` once the tool calls are made, before any of the final is written.
    hold_before_final: bool = False


class ScriptedBackend:
    other_model = "other-model"

    def __init__(self, capabilities: BackendCapabilities | None = None) -> None:
        self._capabilities = capabilities or BackendCapabilities(
            backend="scripted",
            operations=frozenset({OP_CANCEL, OP_ANSWER, OP_ASK, OP_ACTIVE, OP_CATALOG, OP_PEEK_HISTORY}),
            scope_fields=SCOPE_FIELDS,
            # What a model that sees pictures takes. Any other content of a tool result is held back.
            tool_content=frozenset({"image/*"}),
            reports=PROGRESS_EVENTS,
            catalog=CATALOG_KINDS,
            version="1",
        )
        #: Text deltas seen so far per conversation, for peek catch-up.
        self._peek_history: dict[str, list[str]] = {}
        #: What ``catalog`` answers. A check replaces it, or sets it to an exception to raise.
        self.listed: Catalog | Exception = Catalog(
            skills=(
                {"name": "notes", "description": "Keep notes of a conversation.", "installed": True},
                {"name": "digest", "description": "Summarize a span of messages.", "installed": True},
            ),
            experts=("reviewer",),
            subagents=("researcher", "writer"),
            models=ConfiguredModels(names=("default-model", self.other_model)),
        )
        self._script: deque[ModelTurn] = deque()
        self._tools: ToolHost | None = None
        self._released = asyncio.Event()
        self._holding = asyncio.Event()
        self._cancelled: set[str] = set()
        self._running: set[str] = set()
        #: The host said that it stops.
        self.shut_down = False
        #: Each request that ran, as ``(conversation, request id)``: a request id names a request in one conversation.
        self._ran: set[tuple[str, str]] = set()
        #: Per-question approval: its answer event, and whether it was approved.
        #: Both by ``(conversation, question id)``: a question id names a question in one conversation.
        self._answered: dict[tuple[str, str], asyncio.Event] = {}
        self._verdicts: dict[tuple[str, str], bool] = {}
        #: Conversation to the prefix of the question ids its running turn asks under.
        self._asking: dict[str, str] = {}
        #: Conversations whose turn was stopped, in order.
        self.stopped: list[str] = []
        #: The requests steered into a running turn, in order. Only where the capabilities declare ``steer``.
        self.steered: list[TurnRequest] = []
        #: How many of the last messages steered into a turn the turn does not read.
        self.unread_steers = 0
        #: Tool results in call order, as the model would read them.
        self.results: list[str] = []
        self.answers: list[tuple[str, str, Answer]] = []
        #: Each question the host refused to the turn, as ``(conversation, question id, reason)``.
        self.refused: list[tuple[str, str, str]] = []
        self.closed = False
        self._models: list[str] = []

    def models(self) -> list[str]:
        return list(self._models)

    @property
    def backend(self) -> "ScriptedBackend":
        return self

    def restart(self) -> None:
        """Keep this backend: it holds its sessions in memory, which a restart in the same test keeps."""

    def play(self, *turns: ModelTurn) -> None:
        self._script.extend(turns)

    async def holding(self) -> None:
        await self._holding.wait()
        self._holding.clear()

    def release(self) -> None:
        self._released.set()

    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    async def start(self, tools: ToolHost) -> None:
        self._tools = tools

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        key = request.conversation
        if (key, request.request_id) in self._ran:
            yield TurnEvent(EVENT_FAILED, text="This request ran before.", data={DUPLICATE: True})
            return
        turn = self._script.popleft() if self._script else ModelTurn(final="ok")
        if not isinstance(turn, ScriptedTurn):
            turn = ScriptedTurn(
                final=turn.final, calls=turn.calls, hold=turn.hold, ask=turn.ask, background=turn.background
            )
        self._ran.add((key, request.request_id))
        self._running.add(key)
        self._asking[key] = f"{request.request_id}:"
        self._models.append(request.scope.model or "default-model")
        reached_its_end = False
        steered_before = len(self.steered)
        try:
            yield TurnEvent(EVENT_ACCEPTED)
            if turn.crash:
                raise RuntimeError("scripted crash")
            if turn.question is not None:
                yield TurnEvent(EVENT_QUESTION, question=turn.question)
            if turn.hold or turn.question is not None:
                self._holding.set()
                await self._released.wait()
                self._released.clear()
            stopped = key in self._cancelled
            self._cancelled.discard(key)
            if stopped and not turn.ignores_stop:
                yield TurnEvent(EVENT_INTERRUPTED)
                return
            yield TurnEvent(EVENT_STEP)
            if turn.ask is not None:
                # A question Belle invents: the turn pauses here, with no further work, until it is answered.
                prompt, options = turn.ask
                question_id = f"{request.request_id}:ask"
                yield TurnEvent(EVENT_QUESTION, question=Question(id=question_id, text=prompt, options=tuple(options)))
                await self._await_answer(key, question_id)
            if turn.cost is not None or turn.tokens_in is not None or turn.tokens_out is not None:
                usage: dict = {}
                if turn.cost is not None:
                    usage["cost"] = turn.cost
                if turn.tokens_in is not None or turn.tokens_out is not None:
                    usage["tokens"] = {"input": turn.tokens_in, "output": turn.tokens_out}
                yield TurnEvent(EVENT_USAGE, data=usage)
            if turn.error:
                yield TurnEvent(EVENT_FAILED, text=turn.error)
                return
            if turn.narration:
                self._peek_history.setdefault(key, []).append(turn.narration)
                yield TurnEvent(EVENT_TEXT_DELTA, text=turn.narration)
            for number, (name, arguments) in enumerate(turn.calls):
                call_id = f"{request.request_id}-{number}"
                # Realize a scope `ask` as a person's approval, as OpenCode does on its own permission path.
                if request.scope.tools.get(name) == LEVEL_ASK:
                    question_id = f"{request.request_id}:{call_id}"
                    yield TurnEvent(
                        EVENT_QUESTION,
                        question=Question(
                            id=question_id, text=f"Allow {name} to run?",
                            options=(APPROVE, REJECT), kind=APPROVAL, tool=name,
                        ),
                    )
                    if not await self._await_answer(key, question_id):
                        if key in self._cancelled:
                            # Stopped while no person had answered: the turn ends there, as OpenCode's does.
                            self._cancelled.discard(key)
                            yield TurnEvent(EVENT_INTERRUPTED)
                            return
                        self.results.append('{"ok": false, "error": "rejected"}')
                        yield TurnEvent(EVENT_TOOL_RESULT, text=self.results[-1], data={"name": name, "failed": True})
                        yield TurnEvent(EVENT_STEP)
                        continue
                yield TurnEvent(EVENT_TOOL_CALLED, data={"name": name, "call_id": call_id, "arguments": arguments})
                call = ToolCall(conversation=key, name=name, arguments=arguments, call_id=call_id)
                self.results.append(await self._tools.call(call))
                yield TurnEvent(EVENT_TOOL_RESULT, text=result_text(self.results[-1]), data={"name": name, "call_id": call_id})
                yield TurnEvent(EVENT_STEP)
            if turn.hold_before_final:
                self._holding.set()
                await self._released.wait()
                self._released.clear()
                stopped = stopped or key in self._cancelled
                self._cancelled.discard(key)
            if not stopped:
                # The final is written in two pieces, so a reader of the deltas has to join them.
                half = len(turn.final) // 2
                for piece in filter(None, (turn.final[:half], turn.final[half:])):
                    self._peek_history.setdefault(key, []).append(piece)
                    yield TurnEvent(EVENT_TEXT_DELTA, text=piece)
                    if turn.crash_mid_reply:
                        raise RuntimeError("scripted crash mid-reply")
            final = turn.final
            if turn.background and not stopped:
                # The work the turn started has ended, and the model answers it: a second answer of the same turn.
                yield TurnEvent(EVENT_STEP)
                self._peek_history.setdefault(key, []).append(turn.background)
                yield TurnEvent(EVENT_TEXT_DELTA, text=turn.background)
                final = f"{turn.final.strip()}\n\n{turn.background.strip()}"
            reached_its_end = True
            unread = [steered.request_id for steered in self.steered[steered_before:]][-self.unread_steers :]
            data = {NOT_STEERED: unread} if self.unread_steers and unread else {}
            yield TurnEvent(EVENT_INTERRUPTED if stopped else EVENT_SUCCEEDED, text=final, data=data)
        finally:
            self._running.discard(key)
            if turn.ignores_stop and not reached_its_end:
                # The reader left. The work it was reading gets its calls out before it notices.
                for name, arguments in turn.calls:
                    self.results.append(await self._tools.call(ToolCall(conversation=key, name=name, arguments=arguments)))

    async def peek_history(self, conversation: str, request_id: str) -> Sequence[PeekHistoryPart]:
        text = "".join(self._peek_history.get(conversation, ()))
        return (PeekHistoryPart(kind="text", text=text),) if text else ()

    async def cancel(self, conversation: str) -> bool:
        if conversation not in self._running:
            return False
        self._cancelled.add(conversation)
        self.stopped.append(conversation)
        self._released.set()
        for (asked_in, question_id), event in self._answered.items():
            # A question of this conversation's turn waits for nobody once the turn is stopped.
            if asked_in == conversation and question_id.startswith(self._asking.get(conversation, "\0")):
                event.set()
        return True

    async def steer(self, request: TurnRequest) -> bool:
        if request.conversation not in self._running:
            return False
        self.steered.append(request)
        return True

    async def _await_answer(self, conversation: str, question_id: str) -> bool:
        """Wait for a person's verdict on one tool-approval question."""
        event = self._answered.setdefault((conversation, question_id), asyncio.Event())
        self._holding.set()
        await event.wait()
        return self._verdicts.get((conversation, question_id), False)

    async def ask(self, conversation: str, question: Question) -> str:
        raise NotImplementedError

    async def answer(self, conversation: str, question_id: str, answer: Answer) -> None:
        self.answers.append((conversation, question_id, answer))
        approved = answer.approved if answer.approved is not None else (APPROVE in answer.values)
        self._verdicts[(conversation, question_id)] = approved
        self._answered.setdefault((conversation, question_id), asyncio.Event()).set()
        self._released.set()

    async def refuse_question(self, conversation: str, question_id: str, reason: str) -> None:
        """A refused question goes on as an answered one does, with no verdict, as OpenCode's cancelled form does."""
        self.refused.append((conversation, question_id, reason))
        self._answered.setdefault((conversation, question_id), asyncio.Event()).set()
        self._released.set()

    async def pending(self, conversation: str) -> list[Question]:
        return []

    async def active(self) -> list[str]:
        return sorted(self._running)

    async def catalog(self) -> Catalog:
        if isinstance(self.listed, Exception):
            raise self.listed
        return self.listed

    def shutting_down(self) -> None:
        self.shut_down = True

    async def close(self) -> None:
        self.closed = True
