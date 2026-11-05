"""Drive Docker Agent turns over its HTTP wire and translate what it reports."""

import asyncio
import base64
import json
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import httpx

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.backend import (
    APPROVAL,
    CATALOG_EXPERTS,
    CATALOG_MODELS,
    CATALOG_SKILLS,
    CATALOG_SUBAGENTS,
    CONVERSATION_UNAVAILABLE,
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
    OP_CANCEL,
    OP_CATALOG,
    OP_CONTINUE,
    OP_FORGET,
    OP_HEALTH,
    OP_PENDING,
    OP_STEER,
    PROGRESS_EVENTS,
    SUBAGENT,
    Answer,
    BackendCapabilities,
    BackendError,
    BackendHealth,
    Catalog,
    McpHealth,
    PeekHistoryPart,
    Question,
    ToolHost,
    TurnEvent,
    TurnRequest,
    TurnScope,
    read_data_uri,
    still_running,
)
from belle_agent.hook_server import HookServer
from belle_agent.integrations.docker_agent import hooks
from belle_agent.integrations.docker_agent.permissions import session_permissions
from belle_agent.integrations.docker_agent.wire import Wire, WireError
from belle_agent.models import ConfiguredModels
from belle_agent.skills import packaged_skills
from belle_agent.tools.host import APPROVE, REJECT, approves
from belle_agent.tools.server import ToolServer

logger = logging.getLogger(__name__)

#: The agent of Belle's YAML: its file name in the image, without `.yaml`.
AGENT = "belle"
#: The agent of that YAML that each turn starts on.
ENTRY_AGENT = "root"
#: The MCP toolset that serves Belle's tools. Docker Agent advertises each as `belle_<name>`.
TOOLSET = "belle"
#: Nothing that no rule names asks a person. Session rules still deny and ask.
SAFETY_POLICY = "autonomous"
#: The headers a turn request carries, which the YAML forwards on each call of Belle's tools.
TOKEN_HEADER = "X-Belle-Tools-Token"
TURN_HEADER = "X-Belle-Turn"
#: How `stream_stopped` names a turn the model finished. A turn that stops early can carry no reason.
ENDED = "normal"
#: How Docker Agent's warning begins when a toolset did not start. Belle's tools are then missing from the turn.
TOOLSETS_FAILED = "Some toolsets failed to initialize"
TRANSFER_TOOL = hooks.TRANSFER_TOOL

READY_TIMEOUT_SECONDS = 120.0
READY_POLL_SECONDS = 0.5
HEALTH_TIMEOUT_SECONDS = 0.75
STOP_TIMEOUT_SECONDS = 10.0
STOP_POLL_SECONDS = 0.1
# Docker Agent's relay hook has a 55-second HTTP limit. Wait for the child stream
# to name its session before deciding a child hook's scope.
CHILD_SESSION_WAIT_SECONDS = 10.0
#: How many request ids the adapter remembers, to run each one once.
REQUEST_WINDOW = 1024
#: In the state directory: the request ids that ran, and each conversation's session.
REQUESTS_FILE = "docker_agent_requests.json"
SESSIONS_FILE = "docker_agent_sessions.json"

ALREADY_RUNNING = "A turn is already running in this conversation."
ALREADY_RAN = "This request has already run in this conversation."
NOT_PASSED = "These files came with the message and were not passed to you, as the model takes none of their types"
#: The largest turn request Docker Agent takes unless its `--max-request-size` says otherwise.
MAX_REQUEST_BYTES = 1 << 20
STREAM_LOST = "Docker Agent ended the turn's stream before the turn ended."
#: The answer Belle gives in the session to the message of a turn that ended before its own answer.
INTERRUPTED = "This turn was interrupted before it gave an answer."
TOOLS_MISSING = "Belle's tools did not start in Docker Agent, so the turn was stopped"
#: Docker Agent tells the model this after its own words for a refused call.
REFUSED = "A person in the conversation refused it."
#: The code of Docker Agent's ``error`` event when a hook ended the run.
HOOK_BLOCKED = "hook_blocked"
#: Told after the reason a hook ended the run, where Belle's hook endpoint refused a hook while the turn ran.
HOOK_REFUSED = "Belle's hook endpoint refused a hook while this turn ran"

#: Text reaches every model. The other media types reach a model whose `capabilities` in the agent YAML set their flag.
TEXT = "text/*"
CAPABILITY_TYPES = {"image": "image/*", "pdf": "application/pdf"}

# In a turn's queue: the stream ended. A person's stop is the other end.
_END = object()
_CANCELLED = object()


class DockerAgentError(BackendError):
    """Docker Agent cannot serve Belle as it stands."""


@dataclass
class _Turn:
    """What the adapter holds for one running turn."""

    conversation: str
    scope: TurnScope
    #: The name Docker Agent advertises each of Belle's tools under, to the name the core knows it by.
    belle_tools: Mapping[str, str]
    session: str = ""
    events: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    cancelled: bool = False
    #: Docker Agent ended the stream: nothing is left to stop.
    ended: bool = False
    #: Text of the model call in progress.
    text: list[str] = field(default_factory=list)
    #: Tool call id to the tool's name as the core knows it.
    tools: dict[str, str] = field(default_factory=dict)
    #: The sessions of the subagents this turn started. Their hooks answer from this turn.
    subsessions: set[str] = field(default_factory=set)
    #: The tool call Docker Agent holds until a person answers. One waits at a time.
    question: Question | None = None
    error: str = ""
    #: The code of Docker Agent's ``error``, such as ``hook_blocked`` when a hook ended the run. Empty without one.
    error_code: str = ""
    began: float = field(default_factory=time.monotonic)
    cost: float = 0.0
    #: The request ids of the messages steered into the session during this turn, in order.
    steered: list[str] = field(default_factory=list)


def _arguments(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _content_types(model: Any) -> frozenset[str]:
    """The media types a model of the agent YAML takes, beside plain text."""
    declared = (model.get("capabilities") if isinstance(model, Mapping) else None) or {}
    return frozenset({TEXT, *(kind for flag, kind in CAPABILITY_TYPES.items() if declared.get(flag))})


def message(text: str, files: tuple[Mapping[str, Any], ...], takes: frozenset[str]) -> dict[str, Any]:
    """The user message of a turn, its files as document parts with their content inline.

    A file whose type the model does not take is left out, and the message names it.
    """
    documents, left_out = [], []
    for entry in files:
        name = str(entry.get("name") or "file")
        mime, content = read_data_uri(str(entry.get("uri") or ""))
        if not any(fnmatchcase(mime, kind) for kind in takes):
            left_out.append(f"{name} ({mime})")
            continue
        source = (
            {"inline_text": content.decode("utf-8", "replace")}
            if fnmatchcase(mime, TEXT)
            else {"inline_data": base64.b64encode(content).decode("ascii")}
        )
        documents.append({"type": "document", "document": {"name": name, "mime_type": mime, "source": source}})
    if left_out:
        text = f"{text}\n\n{NOT_PASSED}: {', '.join(left_out)}."
    if not files:
        return {"role": "user", "content": text}
    # With parts, the model reads the parts only.
    return {"role": "user", "content": text, "multi_content": [{"type": "text", "text": text}, *documents]}


def _entry(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """The entry agent of an agent configuration as `GET /api/agents/:id` returns it."""
    agents = [agent for agent in config.get("agents") or () if isinstance(agent, dict)]
    return next((agent for agent in agents if agent.get("Name") == ENTRY_AGENT), agents[0] if agents else {})


def translate(event: Mapping[str, Any], turn: _Turn) -> TurnEvent | None:
    """Turn one event of a turn's stream into what the core reads, if it reads it."""
    session = event.get("session_id")
    if session and session != turn.session and event.get("type") not in ("token_usage", "tool_call_confirmation"):
        # A subagent's session reports its own text and end. What it costs, and what it asks a person, is the turn's.
        return None
    match event.get("type"):
        case "stream_started":
            return TurnEvent(EVENT_ACCEPTED)
        case "agent_info":
            # Sent before each model call of the turn: the text written so far was no answer.
            turn.text.clear()
            return TurnEvent(EVENT_STEP)
        case "agent_choice":
            delta = str(event.get("content") or "")
            turn.text.append(delta)
            return TurnEvent(EVENT_TEXT_DELTA, text=delta)
        case "tool_call":
            call = event.get("tool_call") or {}
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            name = turn.tools[str(call.get("id") or "")] = turn.belle_tools.get(name, name)
            arguments = _arguments(function.get("arguments"))
            data: dict[str, Any] = {"name": name, "call_id": str(call.get("id") or ""), "arguments": arguments}
            if name == TRANSFER_TOOL:
                data[SUBAGENT] = str(arguments.get("agent") or "")
            return TurnEvent(EVENT_TOOL_CALLED, data=data)
        case "tool_call_response":
            call_id = str(event.get("tool_call_id") or "")
            name = str((event.get("tool_definition") or {}).get("name") or "")
            data = {"name": turn.tools.get(call_id) or turn.belle_tools.get(name, name), "call_id": call_id}
            if (event.get("result") or {}).get("isError"):
                data["failed"] = True
            return TurnEvent(EVENT_TOOL_RESULT, text=str(event.get("response") or ""), data=data)
        case "token_usage":
            usage = event.get("usage") or {}
            # `cost` is the session's. The cost of the model call that just ended is its last message's.
            turn.cost += float((usage.get("last_message") or {}).get("Cost") or 0)
            tokens = {"input": usage.get("input_tokens"), "output": usage.get("output_tokens")}
            return TurnEvent(EVENT_USAGE, data={"cost": turn.cost, "tokens": tokens})
        case "error":
            turn.error = str(event.get("error") or "")
            turn.error_code = str(event.get("code") or "")
        case "warning" if str(event.get("message") or "").startswith(TOOLSETS_FAILED):
            return TurnEvent(EVENT_FAILED, text=f"{TOOLS_MISSING}: {event.get('message')}")
        case "tool_call_confirmation":
            call = event.get("tool_call") or {}
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            tool = turn.belle_tools.get(name, name)
            shown = json.dumps(_arguments(function.get("arguments")), ensure_ascii=False)[:200]
            turn.question = Question(
                id=str(call.get("id") or ""),
                text=f"Allow {tool}: {shown}",
                options=(APPROVE, REJECT),
                kind=APPROVAL,
                tool=tool,
            )
            return TurnEvent(EVENT_QUESTION, question=turn.question)
        case "stream_stopped":
            turn.ended = True
            reason = str(event.get("reason") or "")
            if reason == ENDED or not (reason or turn.error):
                return TurnEvent(EVENT_SUCCEEDED, text="".join(turn.text))
            text = turn.error or f"Docker Agent ended the turn: {reason}"
            # The error's message is the reader's. Its code names it in the log.
            return TurnEvent(EVENT_FAILED, text=text, data={"reason": reason, "error_type": turn.error_code or None})
    return None


class DockerAgentBackend:
    """``belle_agent.backend.Backend`` on one ``docker-agent serve api``.

    Each conversation is one Docker Agent session, titled from ``namespace``
    and the conversation key. ``namespace`` keeps two deployments that share
    a server out of each other's sessions. ``state_dir`` keeps each
    conversation's session and the request ids that ran across restarts.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        relay_token: str,
        state_dir: Path,
        namespace: str = "",
        tools_bind: str = "127.0.0.1",
        tools_port: int = 0,
        tools_url: str = "",
        relay_bind: str = "127.0.0.1",
        relay_port: int = 0,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        ready_timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._wire = Wire(url, token, transport=transport)
        self._url = url
        self._ready_timeout = ready_timeout
        self._max_request_bytes = max_request_bytes
        # Why the server could not be reached, for a start that runs out of time.
        self._unready = "it has not been asked yet"
        self._prefix = f"belle:{namespace}:"
        self._tools_address = (tools_bind, tools_port)
        #: Where Docker Agent reaches the tool server. The YAML reads it from BELLE_TOOLS_URL; this is for the log.
        self._tools_url = tools_url
        self._relay_address = (relay_bind, relay_port)
        self._relay_token = relay_token
        self._tool_server: ToolServer | None = None
        self._hooks: HookServer | None = None
        self._tools: ToolHost | None = None
        self._belle_tools: Mapping[str, str] = {}
        #: The entry agent's model, which serves a turn whose scope names none, and its experts. Read at start.
        self._default_model = ""
        #: Each model of the agent YAML, to the media types it takes. Read at start.
        self._content: dict[str, frozenset[str]] = {}
        self._experts: tuple[str, ...] = ()
        self._subagents: tuple[str, ...] = ()
        #: The entry agent and its experts relay the hook that follows a steered message. Read at start.
        self._steers = False
        # Session to the permissions last set on it.
        self._permissions: dict[str, dict[str, list[str]]] = {}
        # Conversation to its session id, kept in a file, and to the turn that runs in it.
        self._sessions_file = state_dir / SESSIONS_FILE
        self._sessions: dict[str, str] = {}
        #: The conversations whose last run did not succeed, kept in the same file. Their session may end on a
        #: message with no answer.
        self._unsettled: set[str] = set()
        self._turns: dict[str, _Turn] = {}
        self._child_sessions_ready = threading.Condition()
        #: Conversation to the steered request ids its session holds unread, for the turn the host runs for them.
        self._unread: dict[str, list[str]] = {}
        # The turn header's value of each running turn, to its conversation.
        self._bindings: dict[str, str] = {}
        self._ran = EventDedupStore(state_dir / REQUESTS_FILE, max_entries=REQUEST_WINDOW)

    def capabilities(self) -> BackendCapabilities:
        """Report supported operations, scope fields and content types.

        Scope permissions are checked by ``tool_guard``. Hooks add instructions
        to each model call and filter skills and subagents. Progress comes from
        the turn stream. Steering is available only when the agent relays its
        hook. Image and PDF support follows the model capabilities in agent YAML.
        """
        experts = frozenset({"expert"} if self._experts else ())
        listed = {CATALOG_EXPERTS: self._experts, CATALOG_SUBAGENTS: self._subagents}
        return BackendCapabilities(
            backend="docker_agent",
            operations=frozenset({OP_CANCEL, OP_ANSWER, OP_PENDING, OP_ACTIVE, OP_CATALOG, OP_CONTINUE, OP_FORGET, OP_HEALTH})
            | ({OP_STEER} if self._steers else set()),
            scope_fields=frozenset(
                {"system_additions", "tools", "skills_available", "skills_unavailable", "subagents_available", "model"}
            )
            | experts,
            tool_name_format=f"{TOOLSET}_{{name}}",
            reports=PROGRESS_EVENTS,
            tool_content=self._content.get(self._default_model, frozenset()),
            tool_content_by_model=self._content,
            catalog=frozenset({CATALOG_MODELS, CATALOG_SKILLS} | {kind for kind, names in listed.items() if names}),
        )

    async def start(self, tools: ToolHost) -> None:
        """Wait for the server and its agent, read back the sessions, and serve the tools and the hooks.

        Each way this can fail raises a :class:`DockerAgentError` that names it.
        """
        timeout = self._ready_timeout or READY_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(timeout):
                while not await self._ping():
                    await asyncio.sleep(READY_POLL_SECONDS)
            self._read_agent(await self._wire.call("GET", f"/agents/{AGENT}") or {})
            await self._read_sessions()
            self._tools = tools
            self._belle_tools = {self.capabilities().tool_name(d["name"]): d["name"] for d in tools.definitions("")}
            (tools_host, tools_port), (relay_host, relay_port) = self._tools_address, self._relay_address
            self._tool_server = ToolServer(
                tools, self._conversation_of, host=tools_host, port=tools_port, advertised_url=self._tools_url
            )
            self._hooks = HookServer(self._hook, token=self._relay_token, host=relay_host, port=relay_port)
            for name, server, (host, port) in (
                ("tool server", self._tool_server, self._tools_address),
                ("hook endpoint", self._hooks, self._relay_address),
            ):
                try:
                    await server.start()
                except OSError as exc:
                    raise DockerAgentError(f"the {name} cannot listen on {host}:{port}: {exc}") from exc
            logger.info("docker_agent: serving tools at %s for %s", self._tool_server.url, self._url)
        except BaseException as exc:
            await self.close()
            if isinstance(exc, TimeoutError):
                reason = f"Docker Agent at {self._url} was not ready within {timeout:g}s: {self._unready}"
                raise DockerAgentError(reason) from None
            if isinstance(exc, (WireError, httpx.HTTPError)):
                raise DockerAgentError(f"Docker Agent at {self._url} refused the start: {exc}") from exc
            raise

    async def _ping(self, *, record: bool = True) -> bool:
        """Say whether the server answers. A refused token is not a wait: it raises."""
        try:
            await self._wire.call("GET", "/ping")
        except httpx.HTTPError as exc:
            if record:
                self._unready = f"it cannot be reached ({type(exc).__name__}: {exc})"
            return False
        except WireError as exc:
            if exc.status in (401, 403):
                raise
            if record:
                self._unready = str(exc)
            return False
        return True

    async def health(self) -> BackendHealth:
        # No read-only toolset status API, so the Belle toolset is never reported ready or missing.
        mcp = McpHealth("unknown", "mcp_status_unavailable")
        if self._tool_server is None:
            return BackendHealth("disconnected", "not started", McpHealth("unknown", "not_started"))
        try:
            async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
                if await self._ping(record=False):
                    return BackendHealth("ready", mcp=mcp)
        except WireError:
            return BackendHealth("disconnected", "authentication refused", mcp)
        except TimeoutError:
            return BackendHealth("disconnected", "unreachable", mcp)
        return BackendHealth("disconnected", "unreachable", mcp)

    def _read_agent(self, config: Mapping[str, Any]) -> None:
        """Keep the entry agent's model, experts and subagents. Refuse a YAML where one of them does not relay Belle's hooks."""
        entry = _entry(config)
        experts = tuple(map(str, (entry.get("routing") or {}).get("allowed_agents") or ()))
        subagents = tuple(map(str, entry.get("sub_agents") or ()))
        named = {str(agent.get("Name")): agent for agent in config.get("agents") or () if isinstance(agent, dict)}
        needs = [(entry, hooks.RELAYED | ({hooks.BEFORE_AGENT_RUN} if experts else set()))]
        needs += [(named.get(name) or {"Name": name}, hooks.RELAYED) for name in experts + subagents]
        for agent, needed in needs:
            if missing := sorted(needed - set(agent.get("hooks") or ())):
                raise DockerAgentError(
                    f"the agent {agent.get('Name')} of {AGENT} on Docker Agent at {self._url} does not relay these"
                    f" hooks to Belle: {', '.join(missing)}"
                )
        self._default_model = str(entry.get("model") or "")
        self._content = {str(name): _content_types(model) for name, model in (config.get("models") or {}).items()}
        self._experts = experts
        self._subagents = subagents
        self._steers = all(hooks.USER_STEERING in (agent.get("hooks") or ()) for agent, _ in needs[: 1 + len(experts)])

    async def _read_sessions(self) -> None:
        """Read the conversation-to-session map, and the unsettled conversations, from its file.

        Without a file of this namespace, rebuild the map from the titles the server lists, oldest first. The
        server reads every message of every session for that list, so it is asked only then. A map without a
        record of its unsettled conversations, rebuilt or written before that record, counts every one.
        """
        try:
            stored = json.loads(self._sessions_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            stored = {}
        except ValueError as exc:
            logger.warning(
                "docker_agent: %s is unreadable, so the sessions are read from the server: %s", self._sessions_file, exc
            )
            stored = {}
        if (
            isinstance(stored, dict)
            and stored.get("prefix") == self._prefix
            and isinstance(stored.get("sessions"), dict)
        ):
            self._sessions = {str(key): str(value) for key, value in stored["sessions"].items()}
            unsettled = stored.get("unsettled")
            self._unsettled = set(map(str, unsettled)) if isinstance(unsettled, list) else set(self._sessions)
            return
        listed = await self._wire.call("GET", "/sessions") or []
        for entry in sorted(listed, key=lambda entry: str(entry.get("created_at") or "")):
            title = str(entry.get("title") or "")
            if title.startswith(self._prefix) and entry.get("id"):
                self._sessions[title[len(self._prefix) :]] = str(entry["id"])
        self._unsettled = set(self._sessions)
        self._write_sessions()

    def _write_sessions(self) -> None:
        self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
        written = self._sessions_file.with_suffix(".tmp")
        stored = {"prefix": self._prefix, "sessions": self._sessions, "unsettled": sorted(self._unsettled)}
        written.write_text(json.dumps(stored), encoding="utf-8")
        written.replace(self._sessions_file)

    def _settled(self, conversation: str, settled: bool) -> None:
        """Record whether the conversation's last run succeeded, where that changes."""
        if (conversation not in self._unsettled) != settled:
            (self._unsettled.discard if settled else self._unsettled.add)(conversation)
            self._write_sessions()

    async def _answer_interrupted(self, conversation: str, session: str) -> None:
        """Answer the message a run that did not succeed left last in its session, if one is left there.

        Docker Agent keeps a turn's message when the turn stops before its answer. Without an answer after it,
        the next message follows it directly, and Docker Agent may join the two into one for the model.
        """
        if conversation not in self._unsettled:
            return
        messages = (await self._wire.call("GET", f"/sessions/{session}") or {}).get("messages") or []
        last = messages[-1] if messages else {}
        # An implicit message is Docker Agent's own, such as the task that starts a subagent's session.
        if (last.get("message") or {}).get("role") == "user" and not last.get("implicit"):
            answer = {"agent_name": ENTRY_AGENT, "message": {"role": "assistant", "content": INTERRUPTED}}
            await self._wire.call("POST", f"/sessions/{session}/messages", {"message": answer})

    async def _conversation_of(self, meta: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        """Name the conversation of a tool call from the turn header Docker Agent forwards."""
        return self._bindings.get(headers.get(TURN_HEADER.lower(), ""), "")

    def _hook(self, name: str, event: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        """Answer one relay hook from the turn that runs in its session. It runs in a thread of the hook endpoint."""
        session = event.get("session_id")
        with self._child_sessions_ready:
            def matched() -> _Turn | None:
                return next(
                    (turn for turn in self._turns.values() if session and session in (turn.session, *turn.subsessions)),
                    None,
                )

            turn = matched()
            if turn is None and session and any(TRANSFER_TOOL in active.tools.values() for active in self._turns.values()):
                self._child_sessions_ready.wait_for(lambda: matched() is not None, CHILD_SESSION_WAIT_SECONDS)
                turn = matched()
        assert self._hooks is not None and self._tools is not None, "the backend is not started"
        authorize, run = self._tools.authorize, self._hooks.run
        return hooks.answer(
            event,
            (turn.conversation, turn.scope) if turn else None,
            lambda call: run(authorize(call)),
            lambda name: self._belle_tools.get(name, name),
            ENTRY_AGENT,
        )

    async def _session(self, conversation: str) -> str:
        session = self._sessions.get(conversation)
        if session is None:
            body = {"title": self._prefix + conversation, "safety_policy": SAFETY_POLICY}
            session = self._sessions[conversation] = str((await self._wire.call("POST", "/sessions", body))["id"])
            self._write_sessions()
        return session

    async def _permit(self, session: str, tools: Mapping[str, str]) -> None:
        """Set the session's permissions to a scope's tool levels, where they differ from those last set."""
        advertised = {core: name for name, core in self._belle_tools.items()}
        permissions = session_permissions(tools, lambda name: advertised.get(name, name))
        if self._permissions.get(session) != permissions:
            await self._wire.call("PATCH", f"/sessions/{session}/permissions", {"permissions": permissions})
            self._permissions[session] = permissions

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Run one turn: one request on the conversation's session, and its event stream to its end."""
        conversation = request.conversation
        if conversation in self._turns:
            # The host queues. Docker Agent refuses a second run on a busy session.
            yield TurnEvent(EVENT_FAILED, text=ALREADY_RUNNING)
            return
        if request.existing_conversation_only and not await self.retained(conversation):
            # Checked before ``_session``, which would make an empty session in its place.
            yield TurnEvent(EVENT_FAILED, text=CONVERSATION_UNAVAILABLE)
            return
        # A request id names a request in one conversation: two platforms may give the same one. An entry of
        # the request id alone was written before the conversation was part of the key, and still counts.
        if await self._ran.holds(request.request_id) or not await self._ran.remember(
            f"{conversation} {request.request_id}"
        ):
            yield TurnEvent(EVENT_FAILED, text=ALREADY_RAN, data={DUPLICATE: True})
            return
        assert self._tool_server is not None, "the backend is not started"
        turn = _Turn(conversation, request.scope, self._belle_tools)
        with self._child_sessions_ready:
            self._turns[conversation] = turn
        binding = secrets.token_urlsafe(16)
        self._bindings[binding] = conversation
        try:
            turn.session = await self._session(conversation)
            headers = {TOKEN_HEADER: self._tool_server.token, TURN_HEADER: binding}
            # A turn stopped before it started has no stream to end it.
            ended = TurnEvent(EVENT_INTERRUPTED)
            try:
                item = await self._open(turn, request, headers)
            except WireError:
                if not await self._lost(conversation, turn.session):
                    raise
                if request.existing_conversation_only:
                    # A continuation runs only in the session it continues.
                    item, ended = None, TurnEvent(EVENT_FAILED, text=CONVERSATION_UNAVAILABLE)
                else:
                    lost, turn.session = turn.session, await self._session(conversation)
                    logger.warning(
                        "docker_agent: the server no longer has session %s, so conversation %s continues in session"
                        " %s without its earlier messages",
                        lost,
                        conversation,
                        turn.session,
                    )
                    item = await self._open(turn, request, headers)
            while item is not None:
                if item is _CANCELLED:
                    ended = TurnEvent(EVENT_INTERRUPTED)
                    break
                if item is _END:
                    ended = TurnEvent(EVENT_FAILED, text=STREAM_LOST)
                    break
                event = translate(item, turn)
                if event is not None and event.terminal:
                    ended = event
                    break
                if event is not None:
                    yield event
                item = await turn.events.get()
        except (WireError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            reason = str(exc) or type(exc).__name__
            logger.warning("docker_agent: turn could not run: conversation=%s reason=%s", conversation, reason)
            ended = TurnEvent(EVENT_FAILED, text=f"The turn could not run on Docker Agent: {reason}")
        finally:
            try:
                await self._stop(turn)
            finally:
                with self._child_sessions_ready:
                    del self._turns[conversation]
                    self._child_sessions_ready.notify_all()
                del self._bindings[binding]
        # The relay gives only curl's status for a refusal. The endpoint knows why it refused.
        refused = self._hooks.refusal_since(turn.began) if turn.error_code == HOOK_BLOCKED and self._hooks is not None else ""
        if ended.kind == EVENT_FAILED and refused:
            ended = replace(ended, text=f"{ended.text}\n{HOOK_REFUSED}: {refused}")
        if ended.kind == EVENT_FAILED and turn.session:
            # The session leads to Docker Agent's own record of the failure.
            ended = replace(ended, data={**ended.data, "backend_session": turn.session})
        if ended.kind == EVENT_SUCCEEDED:
            self._settled(conversation, True)
        if turn.steered and not turn.cancelled:
            # The stream is over. The messages still in the steer queue are the last ones: it is first in, first out.
            unread = turn.steered[len(turn.steered) - min(await self._unread_count(turn), len(turn.steered)) :]
            if unread:
                self._unread[conversation] = unread
                ended = replace(ended, data={**ended.data, NOT_STEERED: unread})
        yield ended

    async def _open(self, turn: _Turn, request: TurnRequest, headers: Mapping[str, str]) -> Any:
        """Prepare the turn's session and start its stream. Return the stream's first item, or None for a turn
        stopped before it started.

        A call the server refuses before the stream opens raises its :class:`WireError`, and nothing has run.
        """
        conversation = request.conversation
        await self._permit(turn.session, request.scope.tools)
        await self._answer_interrupted(conversation, turn.session)
        if turn.cancelled:
            return None
        # A model named for one turn stays on the session, so every turn names the one it runs on.
        model = request.scope.model or self._default_model
        takes = self._content.get(model, frozenset({TEXT}))
        # Messages steered too late wait in the session, which gives them to the model before its first call. A
        # session made in place of a lost one holds none, so its turn sends the message.
        waiting = request.request_id in self._unread.pop(conversation, ()) and await self._unread_count(turn)
        body = {"messages": [] if waiting else [message(request.text, request.files, takes)], "model": model}
        size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
        if size > self._max_request_bytes:
            raise ValueError(
                f"Turn request is {size} bytes, exceeding backends.docker_agent.max_request_bytes"
                f" ({self._max_request_bytes}). Reduce the message or files, or increase this setting"
                " and Docker Agent's --max-request-size. Nothing was submitted."
            )
        # Recorded before the run, so that a run cut short by the end of this process counts too.
        self._settled(conversation, False)
        turn.task = asyncio.create_task(self._pump(turn, body, headers))
        item = await turn.events.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def _lost(self, conversation: str, session: str) -> bool:
        """Whether the server no longer has the conversation's session, as after a reset of its database. A lost
        session leaves the session map.

        Docker Agent 1.152 answers most calls on a missing session with 500, so the session is read back.
        """
        if await self._exists(session):
            return False
        self._sessions.pop(conversation, None)
        self._unsettled.discard(conversation)
        self._permissions.pop(session, None)
        self._write_sessions()
        return True

    async def _unread_count(self, turn: _Turn) -> int:
        """Count the messages that wait in the session's steer queue. A count that cannot be read is 0."""
        try:
            queue = await self._wire.call("GET", f"/sessions/{turn.session}/queue") or {}
        except (WireError, httpx.HTTPError) as exc:
            logger.warning("docker_agent: cannot read the steer queue of session %s: %s", turn.session, exc)
            return 0
        return int((queue.get("steer") or {}).get("depth") or 0)

    async def _pump(self, turn: _Turn, body: Any, headers: Mapping[str, str]) -> None:
        """Read the turn's stream into its queue, and mark its end. A stream that breaks ends like one cut short."""
        opened = False
        try:
            async with self._wire.turn(turn.session, AGENT, body, headers) as events:
                opened = True
                async for event in events:
                    # A subagent's session shows in its first event, which comes before the subagent's first model
                    # call and so before any hook of that session. It is noted here, as it arrives.
                    if (session := event.get("session_id")) and session != turn.session:
                        with self._child_sessions_ready:
                            turn.subsessions.add(str(session))
                            self._child_sessions_ready.notify_all()
                    turn.events.put_nowait(event)
        except (WireError, httpx.HTTPError, ValueError) as exc:
            if not opened:
                turn.events.put_nowait(exc)
                return
            logger.warning("docker_agent: the stream of session %s broke: %r", turn.session, exc)
        turn.events.put_nowait(_END)

    async def _stop(self, turn: _Turn) -> None:
        """Close the turn's stream unless Docker Agent ended it, and wait until the server lets the session go.

        A closed stream cancels the turn in Docker Agent, which then ends the work it started.
        """
        task = turn.task
        if task is None:
            return
        if not turn.ended:
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=STOP_TIMEOUT_SECONDS)
        if not done:
            task.cancel()
            await asyncio.wait({task})
        try:
            async with asyncio.timeout(STOP_TIMEOUT_SECONDS):
                while (await self._wire.call("GET", f"/sessions/{turn.session}/status") or {}).get("streaming"):
                    await asyncio.sleep(STOP_POLL_SECONDS)
        except WireError as exc:
            if exc.status != 404:  # No runtime holds the session: nothing runs in it.
                logger.warning("docker_agent: cannot read whether session %s still runs: %s", turn.session, exc)
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.warning("docker_agent: session %s did not report that its turn ended: %r", turn.session, exc)

    async def cancel(self, conversation: str) -> bool:
        """Stop the conversation's running turn. Return whether one was stopped."""
        turn = self._turns.get(conversation)
        if turn is None:
            return False
        turn.cancelled = True
        await self._stop(turn)
        turn.events.put_nowait(_CANCELLED)
        return True

    async def peek_history(self, conversation: str, request_id: str) -> Sequence[PeekHistoryPart]:
        """Docker Agent exposes no on-demand turn transcript for peek catch-up."""
        return ()

    async def forget(self, conversation: str) -> None:
        """Delete the conversation's Docker Agent session and its entry in the session map. A missing one counts as deleted."""
        session = self._sessions.get(conversation)
        if conversation in self._turns or (session is not None and await self._streaming(session)):
            raise still_running(conversation)
        if session is None:
            return
        try:
            await self._wire.call("DELETE", f"/sessions/{session}")
        except WireError as exc:
            # Docker Agent 1.152 answers the delete of a missing session with 500, so whether it is gone is read back.
            if exc.status != 404 and await self._exists(session):
                raise
        del self._sessions[conversation]
        self._unsettled.discard(conversation)
        self._permissions.pop(session, None)
        self._unread.pop(conversation, None)
        self._write_sessions()

    async def _streaming(self, session: str) -> bool:
        """Whether Docker Agent runs a turn in the session, whoever started it. A missing session runs none."""
        try:
            return bool((await self._wire.call("GET", f"/sessions/{session}/status") or {}).get("streaming"))
        except WireError as exc:
            if exc.status == 404:
                return False
            raise

    async def retained(self, conversation: str) -> bool:
        """Whether the session map names the conversation and Docker Agent still has that session."""
        session = self._sessions.get(conversation)
        return session is not None and await self._exists(session)

    async def _exists(self, session: str) -> bool:
        try:
            await self._wire.call("GET", f"/sessions/{session}")
        except WireError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def steer(self, request: TurnRequest) -> bool:
        """Queue a message for the running turn of its conversation. Docker Agent reads it before the next model call.

        A message that arrives after the turn's last model call stays in the session's queue. The turn's terminal
        event names it, and the session gives it to the model in the next turn.
        """
        turn = self._turns.get(request.conversation)
        if turn is None or turn.task is None or turn.ended or turn.cancelled:
            return False
        body = {"messages": [message(request.text, (), frozenset())]}
        try:
            await self._wire.call("POST", f"/sessions/{turn.session}/steer", body)
        except (WireError, httpx.HTTPError) as exc:
            logger.warning("docker_agent: session %s did not take a steered message: %s", turn.session, exc)
            return False
        turn.steered.append(request.request_id)
        return True

    async def answer(self, conversation: str, question_id: str, answer: Answer) -> None:
        """Resume the tool call a turn holds for a person: run it on approval, or refuse it and go on."""
        turn = self._turns.get(conversation)
        if turn is None or turn.question is None or turn.question.id != question_id:
            logger.warning(
                "docker_agent: no tool call waits for this answer: conversation=%s question=%s",
                conversation,
                question_id,
            )
            return
        turn.question = None
        body = {"confirmation": "approve"} if approves(answer) else {"confirmation": "reject", "reason": REFUSED}
        await self._wire.call("POST", f"/sessions/{turn.session}/resume", body)

    async def pending(self, conversation: str) -> list[Question]:
        """List the tool call the conversation's turn holds for a person, if any."""
        turn = self._turns.get(conversation)
        return [turn.question] if turn is not None and turn.question is not None else []

    async def active(self) -> list[str]:
        """List this deployment's conversations whose session Docker Agent is running, whoever started it."""
        listed = await self._wire.call("GET", "/sessions?active=true") or []
        titles = (str(entry.get("title") or "") for entry in listed if entry.get("streaming"))
        return [title[len(self._prefix) :] for title in titles if title.startswith(self._prefix)]

    async def catalog(self) -> Catalog:
        """List the agent YAML's models, the entry agent's first, Belle's packaged skills, its subagents and experts."""
        config = await self._wire.call("GET", f"/agents/{AGENT}") or {}
        entry = _entry(config)
        models = [str(entry.get("model") or ""), *map(str, config.get("models") or {})]
        return Catalog(
            skills=tuple(packaged_skills()),
            experts=self._experts,
            subagents=self._subagents,
            models=ConfiguredModels(names=tuple(dict.fromkeys(name for name in models if name))),
        )

    def shutting_down(self) -> None:
        """Nothing to do: Docker Agent ends a run when the request that streams it closes."""

    async def close(self) -> None:
        """Close the listeners and the wire. A second call does nothing."""
        for server in (self._tool_server, self._hooks):
            if server is not None:
                await server.close()
        self._tool_server = self._hooks = None
        await self._wire.close()
