"""Run Belle conversation turns through JiuwenSwarm's Gateway.

The host's backend port, over the Gateway's message bus. A turn is one
``chat.send`` request routed to the AgentServer, and the Gateway's events for
that request are the turn's events. The AgentServer holds the agent, its
tools and its permission rail, so nothing here serves a tool.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.extensions.sdk.host_types import EventType, Message, ReqMethod
from jiuwenswarm.extensions.sdk.workspace_paths import get_agent_sessions_dir

from belle_agent.backend import (
    CATALOG_MODELS,
    DUPLICATE,
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_QUESTION,
    EVENT_STEP,
    EVENT_SUBAGENT_UPDATE,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    OP_ANSWER,
    OP_CANCEL,
    OP_CATALOG,
    Answer,
    BackendCapabilities,
    Catalog,
    PeekHistoryPart,
    Question,
    ToolHost,
    TurnEvent,
    TurnRequest,
    TurnScope,
    read_data_uri,
)
from belle_agent.integrations.jiuwenswarm.step_text import StepText
from belle_agent.models import ConfiguredModels
from belle_agent.platforms.slack.activity import tool_call_event, tool_result_event
from belle_agent.platforms.slack.file_transfer import (
    attachment_params,
    describe_downloaded_attachments,
    safe_path_component,
    unique_path,
)
from belle_agent.platforms.slack.questions import QuestionPresenter, extract_request
from belle_agent.silence import REPLY_OPTIONAL_KEY, SILENCE_UNSTORED_KEY

logger = logging.getLogger(__name__)

#: The question sources whose answer resumes a paused turn as a new request.
RESUME_SOURCES = frozenset({"ask_user_interrupt", "confirm_interrupt", "permission_interrupt", "evolution_interrupt"})
#: How many request ids each process remembers, to refuse a second run of one.
MAX_SEEN_REQUESTS = 4096

Route = Callable[[Message], Awaitable[None]]
#: Reads a JiuwenSwarm question's options: each label and the value its answer sends.
_PRESENTER = QuestionPresenter(action_id_prefix="", option_style=lambda intent, value: "", logger=logger)


def turn_params(scope: TurnScope, *, default_model: str = "") -> dict[str, Any]:
    """The request parameters that apply a turn's scope, as the AgentServer reads them."""
    params: dict[str, Any] = {"agent_template_name": scope.expert}
    if scope.subagents_available is not None:
        params["agent_subagents_available"] = sorted(scope.subagents_available)
    if scope.skills_available is not None:
        params["agent_skills_available"] = sorted(scope.skills_available)
    if scope.skills_unavailable:
        params["agent_skills_unavailable"] = sorted(scope.skills_unavailable)
    if scope.skills_required:
        params["agent_skills_required"] = list(scope.skills_required)
    if scope.subagent_required is not None:
        name, task = scope.subagent_required
        params["agent_subagent_required"] = {"name": name, "task": task}
    # The AgentServer keeps the last model of a session otherwise.
    if model := scope.model or default_model:
        params["model_name"] = model
    return params


def save_files(request: TurnRequest) -> list[dict[str, Any]]:
    """Write a turn's files where the AgentServer reads uploads, as the records the Slack channel sends."""
    if not request.files:
        return []
    folder = get_agent_sessions_dir() / safe_path_component(request.conversation, "default") / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    for entry in request.files:
        mime, data = read_data_uri(str(entry.get("uri") or ""))
        name = str(entry.get("name") or "attachment")
        path = unique_path(folder / safe_path_component(name, "attachment"))
        path.write_bytes(data)
        records.append({
            "type": "image" if mime.startswith("image/") else "document",
            "filename": path.name,
            "original_filename": name,
            "path": str(path),
            "mime_type": mime,
            "size_bytes": len(data),
        })
    return records


def _content(msg: Message) -> str:
    payload = msg.payload if isinstance(msg.payload, Mapping) else {}
    content = payload.get("content")
    if isinstance(content, Mapping):
        content = content.get("output", "")
    if content is None and isinstance(msg.params, Mapping):
        content = msg.params.get("content")
    return str(content or "")


@dataclass
class _Pending:
    """A question of the AgentServer, as the person sees it."""

    source: str
    text: str
    #: Each option's value, by the label its button shows.
    values: Mapping[str, str]


@dataclass
class _Running:
    request: TurnRequest
    events: asyncio.Queue[TurnEvent] = field(default_factory=asyncio.Queue)
    #: The ids whose events belong to this turn: its own, then each resume of it.
    ids: set[str] = field(default_factory=set)
    #: The request whose final ends the turn: the latest resume, or the turn's own.
    current: str = ""
    questions: dict[str, _Pending] = field(default_factory=dict)
    #: A tool ran since the last text: the next text is a new model call.
    stepped: bool = False
    #: The answer text of each of the turn's requests, by request id.
    texts: dict[str, StepText] = field(default_factory=dict)


class JiuwenSwarmTurns:
    """The backend port over JiuwenSwarm's Gateway. ``route`` hands a request to the Gateway."""

    def __init__(
        self,
        route: Route,
        *,
        channel: str,
        chat_key: str,
        user_key: str,
        models: Callable[[], ConfiguredModels] = ConfiguredModels,
        session_id: Callable[[str], str] = str,
    ) -> None:
        self._route = route
        self._channel = channel
        self._chat_key = chat_key
        self._user_key = user_key
        self._models = models
        self._session_id = session_id
        self._running: dict[str, _Running] = {}
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def capabilities(self) -> BackendCapabilities:
        """What a turn here applies and reports."""
        return BackendCapabilities(
            backend="jiuwenswarm",
            operations=frozenset({OP_CANCEL, OP_ANSWER, OP_CATALOG}),
            # Not "tools": a request of a channel extension carries no tool levels. The AgentServer settles
            # its own from Belle's scopes, where its permission engine is on.
            scope_fields=frozenset({
                "skills_available", "skills_unavailable", "skills_required", "expert",
                "subagents_available", "subagent_required", "model", "system_additions", "withhold_final",
            }),
            reports=frozenset({EVENT_TEXT_DELTA, EVENT_STEP, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT,
                               EVENT_SUBAGENT_UPDATE}),
            catalog=frozenset({CATALOG_MODELS}),
            scheduler="jiuwenswarm",
        )

    async def catalog(self) -> Catalog:
        """The models JiuwenSwarm's configuration names. Its skills and experts are the AgentServer's to list."""
        return Catalog(models=self._models())

    async def start(self, tools: ToolHost) -> None:
        """Nothing to start: the AgentServer serves the tools, and the Gateway is running."""

    def shutting_down(self) -> None:
        """Nothing to do: a reader that leaves sends no cancel, so the AgentServer continues the turn unread."""

    async def close(self) -> None:
        """End each running turn: the channel stopped."""
        for running in self._running.values():
            running.events.put_nowait(TurnEvent(EVENT_FAILED, text="The channel stopped before the turn ended."))

    def _message(self, request: TurnRequest, request_id: str, method: ReqMethod, params: dict[str, Any], *, stream: bool) -> Message:
        metadata = dict(request.metadata)
        return Message(
            id=request_id,
            type="req",
            channel_id=self._channel,
            session_id=self._session_id(request.conversation),
            params=params,
            timestamp=time.time(),
            ok=True,
            chat_id=str(metadata.get(self._chat_key) or ""),
            user_id=str(metadata.get(self._user_key) or ""),
            req_method=method,
            is_stream=stream,
            metadata=metadata,
        )

    def request_message(self, request: TurnRequest) -> Message:
        """The ``chat.send`` request of one turn."""
        text = "\n\n".join(part for part in (request.text, *request.scope.system_additions) if part.strip())
        saved = save_files(request)
        if saved:
            text = describe_downloaded_attachments(text, saved)
        params: dict[str, Any] = {"content": text, "query": text}
        params.update(turn_params(request.scope, default_model=self._models().default))
        if saved:
            params["files"] = attachment_params(saved)
            params["media_items"] = saved
        message = self._message(request, request.request_id, ReqMethod.CHAT_SEND, params, stream=True)
        if request.scope.withhold_final:
            message.metadata[REPLY_OPTIONAL_KEY] = True
            message.metadata[SILENCE_UNSTORED_KEY] = True
        return message

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Route the turn's request, then yield its events up to the terminal one."""
        key = (request.conversation, request.request_id)
        if key in self._seen:
            yield TurnEvent(EVENT_FAILED, data={DUPLICATE: True})
            return
        self._seen[key] = None
        while len(self._seen) > MAX_SEEN_REQUESTS:
            self._seen.popitem(last=False)
        running = _Running(request, ids={request.request_id}, current=request.request_id)
        self._running[request.conversation] = running
        try:
            await self._route(self.request_message(request))
            while True:
                event = await running.events.get()
                yield event
                if event.terminal:
                    return
        finally:
            if self._running.get(request.conversation) is running:
                del self._running[request.conversation]

    def deliver(self, msg: Message) -> bool:
        """Take an event of a running turn. Return whether one of them owns it."""
        request_id = str(msg.id or "")
        running = next((turn for turn in self._running.values()
                        if request_id in turn.ids and msg.session_id == self._session_id(turn.request.conversation)), None)
        if running is None:
            return False
        for event in self._events(running, msg):
            running.events.put_nowait(event)
        return True

    def _events(self, running: _Running, msg: Message) -> list[TurnEvent]:
        kind, payload = msg.event_type, msg.payload if isinstance(msg.payload, Mapping) else {}
        if kind == EventType.CHAT_DELTA:
            if not _content(msg):
                return []
            if str(payload.get("source_chunk_type") or "").lower() == "llm_reasoning":
                # Distinct from answer text so peeks (and platforms) can style thinking separately.
                return [TurnEvent(EVENT_TEXT_DELTA, text=_content(msg), data={"thinking": True})]
            # A tool ran since the last text, so this text is the next model call's.
            stepped, running.stepped = running.stepped, False
            running.texts.setdefault(str(msg.id or ""), StepText()).add(_content(msg))
            return [TurnEvent(EVENT_STEP)] * stepped + [TurnEvent(EVENT_TEXT_DELTA, text=_content(msg))]
        if kind == EventType.CHAT_TOOL_CALL and (called := tool_call_event(payload)):
            name, call_id, _ = called
            return [TurnEvent(EVENT_TOOL_CALLED, data={"name": name, "call_id": call_id})]
        if kind == EventType.CHAT_TOOL_RESULT and (result := tool_result_event(payload)):
            name, call_id, state = result
            running.stepped = True
            running.texts.setdefault(str(msg.id or ""), StepText()).step()
            return [TurnEvent(EVENT_TOOL_RESULT, data={"name": name, "call_id": call_id, "failed": state == "failed"})]
        if kind == EventType.CHAT_SUBTASK_UPDATE and (subagent_id := payload.get("subagent_id")):
            outcome = str(payload.get("turn_outcome") or "").lower()
            status = str(payload.get("status") or "").lower()
            state = (outcome if outcome in {"completed", "failed", "cancelled"} else
                     "running" if status == "running" else "unreported" if status == "closed" else "")
            if state:
                return [TurnEvent(EVENT_SUBAGENT_UPDATE, data={"id": str(subagent_id),
                                                            "name": payload.get("subagent_type"), "state": state})]
        if kind == EventType.CHAT_ASK_USER_QUESTION:
            return self._question(running, msg)
        if kind == EventType.CHAT_ERROR:
            return [TurnEvent(EVENT_FAILED, text=_content(msg).strip() or "The turn failed in JiuwenSwarm.")]
        paused = any(pending.source in RESUME_SOURCES for pending in running.questions.values())
        if kind == EventType.CHAT_FINAL and str(msg.id or "") == running.current and not paused:
            # A request that paused for an answer ends too. Its resume carries the turn on.
            text = _content(msg)
            if (written := running.texts.get(running.current)) is not None:
                text = written.final(text)
            return [TurnEvent(EVENT_SUCCEEDED, text=text)]
        return []

    def _question(self, running: _Running, msg: Message) -> list[TurnEvent]:
        parsed = extract_request(msg.payload)
        if parsed is None:
            logger.warning("jiuwenswarm question carried nothing to answer: id=%s", msg.id)
            return []
        question_id, source, question, dropped = parsed
        if dropped:
            logger.warning("%s shows question 1 of %d: request_id=%s", self._channel, len(dropped) + 1, question_id)
        values = {option.label: option.value for option in _PRESENTER.options(question)}
        running.questions[question_id] = _Pending(source, _PRESENTER.prompt_text(question), values)
        text = _PRESENTER.fallback_text(question)
        return [TurnEvent(EVENT_QUESTION, question=Question(id=question_id, text=text, options=tuple(values)))]

    async def answer(self, conversation: str, question_id: str, answer: Answer) -> None:
        """Route a person's answer: a resume of a paused request, or an answer to the question of a tool."""
        running = self._running.get(conversation)
        pending = running.questions.pop(question_id, None) if running is not None else None
        if running is None or pending is None:
            logger.warning("jiuwenswarm answer of no waiting question: session=%s question=%s", conversation, question_id)
            return
        values = [pending.values.get(value, value) for value in answer.values]
        params: dict[str, Any] = {
            "request_id": question_id,
            "answers": [{"question": pending.text, "selected_options": values, "custom_input": ""}],
            "source": pending.source,
        }
        resumes = pending.source in RESUME_SOURCES
        request_id = f"{question_id}-answer-{int(time.time() * 1000)}"
        if resumes:
            params.update(query="", supports_user_interaction=True)
            params.update(turn_params(running.request.scope, default_model=self._models().default))
            running.ids.add(request_id)
            running.current = request_id
        method = ReqMethod.CHAT_SEND if resumes else ReqMethod.CHAT_ANSWER
        await self._route(self._message(running.request, request_id, method, params, stream=resumes))

    async def cancel(self, conversation: str) -> bool:
        """Cancel the conversation's running request. Its turn ends interrupted."""
        running = self._running.get(conversation)
        if running is None:
            return False
        await self._route(self._message(
            running.request, f"{running.request.request_id}-cancel", ReqMethod.CHAT_CANCEL,
            {"intent": "cancel", "session_id": self._session_id(conversation)}, stream=False,
        ))
        running.events.put_nowait(TurnEvent(EVENT_INTERRUPTED))
        return True

    async def peek_history(self, conversation: str, request_id: str) -> Sequence[PeekHistoryPart]:
        """JiuwenSwarm exposes no on-demand turn transcript for peek catch-up."""
        return ()

