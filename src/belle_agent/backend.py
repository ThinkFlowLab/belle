"""Declare what runs model turns for Belle, and what Belle serves it.

A backend implements :class:`Backend`. The core implements :class:`ToolHost`
and hands it to the backend at start. Everything a turn needs is settled by
the core before the request crosses, so a backend translates and decides
nothing.

``conversation`` is the conversation key throughout: the key of the
conversation that one turn runs in. The conversation key of an attached session
comes from :func:`belle_agent.turn_policy.conversation_key`. The conversation
key of a detached session is ``belle_session_`` and the session id. The key never comes from tool
arguments, because the model writes those.
"""

import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Protocol
from urllib.parse import unquote_to_bytes

from belle_agent.models import ConfiguredModels
from belle_agent.scopes import LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY

EVENT_ACCEPTED = "accepted"
EVENT_TEXT_DELTA = "text_delta"
#: The model is asked once more: one request to it, a retried one included. When ``data["continues"]`` is true,
#: the request continues the text of the step before it, and a platform shows that text with no paragraph break.
EVENT_STEP = "step"
EVENT_TOOL_CALLED = "tool_called"
EVENT_TOOL_RESULT = "tool_result"
EVENT_SUBAGENT_UPDATE = "subagent_update"
EVENT_QUESTION = "question"
EVENT_USAGE = "usage"
EVENT_SUCCEEDED = "succeeded"
EVENT_FAILED = "failed"
EVENT_INTERRUPTED = "interrupted"

# Failure categories a backend may report without copying error text into logs.
FAILURE_KINDS: frozenset[str] = frozenset({"backend", "provider"})

#: A turn ends with exactly one of these.
TERMINAL_EVENTS: frozenset[str] = frozenset({EVENT_SUCCEEDED, EVENT_FAILED, EVENT_INTERRUPTED})

#: What a turn reports of its own work while it runs. A backend declares the kinds it yields, and a
#: platform shows the kinds it has a surface for.
PROGRESS_EVENTS: frozenset[str] = frozenset(
    {EVENT_TEXT_DELTA, EVENT_STEP, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, EVENT_SUBAGENT_UPDATE, EVENT_USAGE}
)

#: ``TurnEvent.data`` key of the subagent's type. A backend sets it on the ``tool_called`` event of a call that
#: runs a subagent, and tells no more than it knows. It may also set it on that call's ``tool_result``.
#: A ``tool_result`` on the subagent's ``call_id`` ends the subagent, with or without this key, unless the
#: result has ``child``. A tool event that a backend routes from a child session onto that ``call_id`` has
#: ``child`` and no ``SUBAGENT``.
SUBAGENT = "subagent"

#: ``TurnEvent.data`` key of the ``failed`` event that answers a request id the backend ran before.
#: Nothing ran for it, so nothing is owed to anyone.
DUPLICATE = "duplicate"

#: ``TurnEvent.data`` key of a terminal event: the request ids of the messages steered into the turn that the
#: turn did not read. The host runs them, as one turn, before any other message of the conversation.
NOT_STEERED = "not_steered"

# Every backend starts, runs turns and closes. These it may lack.
OP_CANCEL = "cancel"
OP_ASK = "ask"
OP_ANSWER = "answer"
OP_PENDING = "pending"
OP_ACTIVE = "active"
OP_RECOVER = "recover"
OP_CATALOG = "catalog"
OP_STEER = "steer"
#: Delete a conversation's harness session. Belle deletes the conversation of an expired detached session,
#: and an idle attached session where its operator turns that on.
OP_FORGET = "forget"
OP_CONTINUE = "continue"
#: Prior assistant text for a live peek opened mid-turn, when the harness still has it.
OP_PEEK_HISTORY = "peek_history"
OP_HEALTH = "health"
#: Tell a turn why its question reached nobody, so that the turn goes on and can put it another way.
OP_REFUSE_QUESTION = "refuse_question"
OPTIONAL_OPERATIONS: frozenset[str] = frozenset(
    {OP_CANCEL, OP_ASK, OP_ANSWER, OP_PENDING, OP_ACTIVE, OP_RECOVER, OP_CATALOG, OP_STEER, OP_FORGET,
     OP_CONTINUE, OP_PEEK_HISTORY, OP_HEALTH, OP_REFUSE_QUESTION}
)

RECOVERY_NOT_SUBMITTED = "not_submitted"
RECOVERY_RUNNING = "running"
RECOVERY_TERMINAL = "terminal"

# What a backend's catalog can list. A backend names the kinds it lists in ``BackendCapabilities.catalog``.
CATALOG_SKILLS = "skills"
CATALOG_EXPERTS = "experts"
CATALOG_SUBAGENTS = "subagents"
CATALOG_MODELS = "models"
CATALOG_KINDS: frozenset[str] = frozenset({CATALOG_SKILLS, CATALOG_EXPERTS, CATALOG_SUBAGENTS, CATALOG_MODELS})

QUESTION = "question"
APPROVAL = "approval"

VERDICT_ALLOW = LEVEL_ALLOW
VERDICT_DENY = LEVEL_DENY
VERDICT_ASK = LEVEL_ASK
VERDICT_REWRITE = "rewrite"


@dataclass(frozen=True)
class TurnScope:
    """What one turn may use, settled by the core at admission."""

    #: Tool names to ``ask`` or ``deny``. A tool not named is allowed.
    tools: Mapping[str, str] = field(default_factory=dict)
    #: ``None`` leaves the backend's whole catalog available.
    skills_available: frozenset[str] | None = None
    #: Skills a turn may not use, though ``skills_available`` lists them or is ``None``.
    skills_unavailable: frozenset[str] = frozenset()
    skills_required: tuple[str, ...] = ()
    #: Empty runs the turn with no expert, clearing any earlier one.
    expert: str = ""
    subagents_available: frozenset[str] | None = None
    #: A subagent name and its task, when the turn must delegate.
    subagent_required: tuple[str, str] | None = None
    #: Empty runs the turn on the backend's default model.
    model: str = ""
    #: Instructions for this turn only, in order.
    system_additions: tuple[str, ...] = ()
    #: The final text reaches no reader. A backend may keep it out of storage.
    withhold_final: bool = False


#: The names a backend lists in ``BackendCapabilities.scope_fields``.
SCOPE_FIELDS: frozenset[str] = frozenset(item.name for item in fields(TurnScope))


@dataclass(frozen=True)
class TurnRequest:
    """One admitted message, ready to run."""

    conversation: str
    #: Idempotency key. A backend runs one turn for one ``request_id``.
    request_id: str
    text: str
    scope: TurnScope = field(default_factory=TurnScope)
    #: Each as ``{"uri": <data: URI>, "name": <file name>}``. See :func:`read_data_uri`.
    files: tuple[Mapping[str, Any], ...] = ()
    #: The platform's request metadata. Opaque to a backend.
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: The conversation must exist. A backend that cannot find it fails the turn with
    #: :data:`CONVERSATION_UNAVAILABLE` and makes none.
    existing_conversation_only: bool = False


def read_data_uri(uri: str) -> tuple[str, bytes]:
    """Return the media type and the bytes of a ``data:`` URI. Any other URI raises ``ValueError``."""
    header, comma, payload = uri.partition(",")
    if not (header.startswith("data:") and comma):
        raise ValueError("a file is read only from a data: URI")
    content = base64.b64decode(payload) if header.endswith(";base64") else unquote_to_bytes(payload)
    return header[5:].split(";")[0], content


@dataclass(frozen=True)
class Question:
    """Something a person must answer before a turn or a tool call goes on."""

    id: str
    text: str
    options: tuple[str, ...] = ()
    kind: str = QUESTION
    #: The tool whose call waits, for an approval.
    tool: str = ""


@dataclass(frozen=True)
class Answer:
    """A person's answer: the options or text given, or an approval verdict."""

    values: tuple[str, ...] = ()
    approved: bool | None = None


@dataclass(frozen=True)
class TurnEvent:
    """One thing a running turn reports."""

    kind: str
    #: A delta, the whole final text on ``succeeded``, or the reason on ``failed``. The deltas since
    #: the last ``step`` are the reply as far as it is written. A ``step`` starts the reply again:
    #: the text before it was no part of the final, or was an earlier answer of the turn. A turn that
    #: answers more than once has all its answers as its final, in order, a blank line apart.
    text: str = ""
    question: Question | None = None
    #: On ``tool_called`` and ``tool_result``: ``name``, the tool as the core and its scopes name it,
    #: ``call_id``, which pairs a result with its call, and ``failed`` on a result that is an error.
    #: On ``tool_called``, ``arguments``, the call's arguments as the model gave them, where the backend has them.
    #: On ``subagent_update``: ``id`` identifies a subagent within the turn, ``name`` is its type,
    #: and ``state`` is running, completed, failed, cancelled, or unreported.
    #: On ``usage``, ``cost`` is what the turn has cost so far, in the backend's own unit, beside the
    #: counts the backend keeps.
    #: On ``failed``, ``failure_kind`` is in ``FAILURE_KINDS`` and ``status`` is an HTTP status code.
    #: ``error_type`` is the harness's own name for the error, such as its error code or stop reason, or
    #: ``None`` where it names none. ``backend_session`` is the harness's id of the session the turn ran in, on
    #: every failure after the turn had one. Each is letters, digits, ``.``, ``_`` and ``-``: at most 64
    #: characters for ``error_type`` and 128 for ``backend_session``. The host logs neither in another form. No
    #: field holds the error's message, which can hold model output or credentials.
    #: On ``text_delta``, ``thinking`` marks internal draft text; ``child`` marks subagent-session
    #: draft text. Both are peek-only: the host does not hand them to platform answer progress.
    #: A backend sets ``child`` on every tool event that it routes from a child session onto the
    #: parent subagent call. ``SUBAGENT`` says how such events and the subagent's own result differ.
    #: Platforms may still show the child's tools.
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_EVENTS


@dataclass(frozen=True)
class PeekHistoryPart:
    """One piece of prior turn text a backend still has when a peek opens mid-turn.

    ``kind`` is ``text`` or ``thinking``. Belle keeps no turn transcript of its own;
    this is harness catch-up for an ephemeral peek ring only. ``call_id`` is the
    parent subagent tool call when the text came from a routed child session; the
    host maps it to ``source_work_id`` on seed.
    """

    kind: str
    text: str
    call_id: str | None = None
    #: Set by the host when seeding: Activity id for child-attributed catch-up text.
    source_work_id: str | None = None


HEALTH_STATES: frozenset[str] = frozenset({"ready", "degraded", "disconnected"})


MCP_STATES: frozenset[str] = frozenset({"ready", "missing", "failed", "unknown", "not_applicable"})


@dataclass(frozen=True)
class McpHealth:
    """Whether the harness can use Belle's MCP tools."""

    state: str  # ready | missing | failed | unknown | not_applicable
    reason: str = ""  # safe code such as mcp_mount_missing; required unless ready or not_applicable

    def __post_init__(self) -> None:
        if self.state not in MCP_STATES:
            raise ValueError(f"unknown mcp state: {self.state}")
        if self.state in {"ready", "not_applicable"}:
            if self.reason:
                raise ValueError(f"{self.state} mcp has no reason")
        elif not self.reason or any(ord(char) < 32 for char in self.reason):
            raise ValueError(f"{self.state} mcp needs a reason without control characters")


@dataclass(frozen=True)
class BackendHealth:
    """Readiness sample from a cheap harness probe."""

    state: str  # ready | degraded | disconnected
    detail: str = ""  # short label; required when not ready
    # Default not_applicable for backends that do not use Belle MCP tools.
    mcp: McpHealth = field(default_factory=lambda: McpHealth("not_applicable"))

    def __post_init__(self) -> None:
        if self.state not in HEALTH_STATES:
            raise ValueError(f"unknown health state: {self.state}")
        if self.state == "ready" and self.detail:
            raise ValueError("ready health has no detail")
        if self.state != "ready" and not self.detail.strip():
            raise ValueError(f"{self.state} health needs a detail")
        if not isinstance(self.mcp, McpHealth):
            raise TypeError("mcp must be McpHealth")


@dataclass(frozen=True)
class Recovery:
    """A backend's observation of one previously admitted request."""

    state: str
    terminal: TurnEvent | None = None

    def __post_init__(self) -> None:
        if self.state not in {RECOVERY_NOT_SUBMITTED, RECOVERY_RUNNING, RECOVERY_TERMINAL}:
            raise ValueError(f"unknown recovery state: {self.state}")
        if (self.terminal is not None) != (self.state == RECOVERY_TERMINAL):
            raise ValueError("only terminal recovery has a terminal event")
        if self.terminal is not None and not self.terminal.terminal:
            raise ValueError("recovery terminal must be a terminal turn event")


@dataclass(frozen=True)
class BackendCapabilities:
    """The optional operations and scope fields a backend applies."""

    backend: str
    operations: frozenset[str] = frozenset()
    scope_fields: frozenset[str] = frozenset()
    #: How the backend spells a core tool name to the model.
    tool_name_format: str = "{name}"
    #: The backend's own tools that a scope's ``permissions.tools`` may name, each with the levels the
    #: backend applies to it. ``None``: the backend does not say, and the host checks no tool name.
    tool_levels: Mapping[str, frozenset[str]] | None = None
    #: The ``PROGRESS_EVENTS`` a turn of this backend yields. A kind left out is shown by no platform:
    #: a backend that yields no ``text_delta`` has no reply shown as it is written.
    reports: frozenset[str] = frozenset()
    #: The media types of tool-result content, beside text, that this backend hands to the model: a type
    #: such as ``image/png`` or a family such as ``image/*``. A backend may know them only once it has
    #: started. A result with a part of any other type is refused by the tool host, which says so.
    tool_content: frozenset[str] = frozenset()
    #: Content types of named models. A named model absent here receives text only.
    tool_content_by_model: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: The kinds its catalog lists, among ``CATALOG_KINDS``. A command that reads another kind says so.
    catalog: frozenset[str] = frozenset()
    #: The version of what runs the turns, where the backend knows it, for a status a person reads.
    version: str = ""
    #: Name of a scheduler and its tools owned by the backend, or empty when it has none.
    scheduler: str = ""
    #: Maximum tool-result characters the backend accepts. Zero means that it declares no limit.
    tool_result_max_chars: int = 0

    def __post_init__(self) -> None:
        for label, given, known in (
            ("operations", self.operations, OPTIONAL_OPERATIONS),
            ("scope fields", self.scope_fields, SCOPE_FIELDS),
            ("progress events", self.reports, PROGRESS_EVENTS),
            ("catalog kinds", self.catalog, CATALOG_KINDS),
        ):
            unknown = sorted(frozenset(given) - known)
            if unknown:
                raise ValueError(f"backend {self.backend} declares unknown {label}: {', '.join(unknown)}")
        object.__setattr__(self, "operations", frozenset(self.operations))
        object.__setattr__(self, "scope_fields", frozenset(self.scope_fields))
        object.__setattr__(self, "reports", frozenset(self.reports))
        object.__setattr__(self, "tool_content", frozenset(self.tool_content))
        object.__setattr__(self, "tool_content_by_model", {
            name: frozenset(types) for name, types in self.tool_content_by_model.items()
        })
        object.__setattr__(self, "catalog", frozenset(self.catalog))

    def tool_name(self, name: str) -> str:
        return self.tool_name_format.format(name=name)

    def content_for(self, model: str) -> frozenset[str]:
        return self.tool_content_by_model.get(model, frozenset()) if model else self.tool_content


@dataclass(frozen=True)
class Catalog:
    """What a backend can offer a turn, in the shapes the core already filters.

    A kind the backend does not name in ``BackendCapabilities.catalog`` stays empty here.
    """

    #: Entries as ``capability_catalog.usable_skills`` reads them.
    skills: tuple[Mapping[str, Any], ...] = ()
    experts: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    models: ConfiguredModels = field(default_factory=ConfiguredModels)


@dataclass(frozen=True)
class ContentPart:
    """Content of a tool result that is not text: its bytes and their media type."""

    mime: str
    data: bytes
    name: str = ""


@dataclass(frozen=True)
class ToolContent:
    """A tool result that carries content beside its text.

    ``text`` is what a result has always been, and says what the parts are. A
    backend hands each part to the model in its own form, where it passes
    content of that type: see ``BackendCapabilities.tool_content``.
    """

    text: str
    parts: tuple[ContentPart, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    """One tool call, bound to its conversation by the backend's own records."""

    conversation: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class ToolVerdict:
    """Whether a tool call runs, and with which arguments."""

    action: str
    message: str = ""
    question: Question | None = None
    arguments: Mapping[str, Any] | None = None

    @classmethod
    def allow(cls) -> "ToolVerdict":
        return cls(VERDICT_ALLOW)

    @classmethod
    def deny(cls, message: str) -> "ToolVerdict":
        return cls(VERDICT_DENY, message=message)

    @classmethod
    def ask(cls, question: Question) -> "ToolVerdict":
        return cls(VERDICT_ASK, message=question.text, question=question)

    @classmethod
    def rewrite(cls, arguments: Mapping[str, Any]) -> "ToolVerdict":
        return cls(VERDICT_REWRITE, arguments=dict(arguments))


class ToolHost(Protocol):
    """What the core serves a backend: tools, their gate and their execution."""

    def definitions(self, conversation: str) -> list[dict[str, Any]]:
        """List ``{name, description, input_params}`` for a conversation's turn."""

    async def authorize(self, call: ToolCall) -> ToolVerdict:
        """Decide one call. A backend asks before any tool runs, its own included."""

    async def call(self, call: ToolCall) -> "str | ToolContent":
        """Run one of the core's tools and return its result: text, or text with content parts."""


class BackendError(Exception):
    """A backend cannot serve as it stands. The message names the cause, for whoever started it."""


#: The text of a turn that failed because its conversation must exist and the harness no longer has it.
CONVERSATION_UNAVAILABLE = "backend_conversation_unavailable: the harness no longer has this conversation"


def still_running(conversation: str) -> BackendError:
    """The refusal of ``forget`` while a turn runs in the conversation."""
    return BackendError(f"conversation {conversation!r} still has a running turn")


class Backend(Protocol):
    """What runs model turns for the core."""

    def capabilities(self) -> BackendCapabilities:
        """Declare the optional operations and scope fields this backend applies."""

    async def start(self, tools: ToolHost) -> None:
        """Become ready to run turns, serving ``tools`` to the model.

        A backend that cannot become ready raises :class:`BackendError`, and holds nothing open after it.
        """

    def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Run one turn. The stream ends with exactly one terminal event.

        Work the turn starts and does not wait for is part of the turn. The
        terminal event comes when that work has ended and the model has
        answered what it reported.

        A reader that leaves the stream before that event stops the turn: the
        backend ends the work it started and frees the conversation. After
        :meth:`shutting_down`, the backend may leave the turn running instead.

        A request id the backend ran before in the same conversation starts
        nothing. Its stream is one ``failed`` event whose data holds
        ``DUPLICATE``, at once. The same request id in another conversation is
        another request: two platforms may give the same one.

        When the harness no longer has the conversation's session, as after a
        reset of its storage, the turn runs in a new session, and the backend
        logs a warning that the earlier context is gone. A request with
        ``existing_conversation_only`` fails with :data:`CONVERSATION_UNAVAILABLE`
        instead.
        """

    def shutting_down(self) -> None:
        """Hear that the host stops, and not its turns.

        The host calls this before it leaves the streams of its running turns. A backend whose harness runs a turn
        without a reader then leaves each turn running, so that the next start can recover it with ``OP_RECOVER``.
        A backend whose harness ends a turn with its stream changes nothing. :meth:`cancel` still stops a turn.
        """

    async def cancel(self, conversation: str) -> bool:
        """Stop the conversation's running turn. Return whether one was stopped."""

    async def steer(self, request: TurnRequest) -> bool:
        """Give the running turn of ``request.conversation`` the text of ``request`` before its next model call.

        Return whether the turn took it. A turn that did not take it is left as it was. A message the turn
        took but did not read is named under ``NOT_STEERED`` in its terminal event.
        """

    async def ask(self, conversation: str, question: Question) -> str:
        """Put a question the core invents. Return its id."""

    async def answer(self, conversation: str, question_id: str, answer: Answer) -> None:
        """Deliver a person's answer to a waiting question."""

    async def refuse_question(self, conversation: str, question_id: str, reason: str) -> None:
        """Tell the turn that its waiting question reached nobody, and why. Only with ``OP_REFUSE_QUESTION``.

        The turn goes on without an answer, and its model reads ``reason``. An approval it refuses is a no.
        """

    async def pending(self, conversation: str) -> list[Question]:
        """List the questions still waiting, for use after a reconnect."""

    async def active(self) -> list[str]:
        """List conversations with a running turn, for recovery after a restart."""

    async def recover(self, request: TurnRequest) -> Recovery:
        """Inspect a past request without submitting it again.

        For a running request, restore the backend's per-turn tool binding from
        the trusted, current ``request.scope``. Only a backend that advertises
        ``OP_RECOVER`` may be called.
        """

    async def peek_history(self, conversation: str, request_id: str) -> Sequence[PeekHistoryPart]:
        """Prior assistant text for a live peek opened mid-turn.

        Only a backend that advertises ``OP_PEEK_HISTORY`` is called. Return what
        the harness still has for this request; empty when it has nothing. Belle
        seeds the ephemeral peek ring once and does not store turn history.
        """

    async def forget(self, conversation: str) -> None:
        """Delete the conversation's harness session. Only with ``OP_FORGET``.

        Only :mod:`belle_agent.host.expiry` calls it. A session already gone counts as
        deleted. Raise :func:`still_running` while a turn runs in it, started
        by this process or not.
        """

    async def retained(self, conversation: str) -> bool:
        """Whether a later turn can use this conversation's stored context."""

    async def catalog(self) -> Catalog:
        """List what this backend has, of the kinds it declares. The first model is the one that serves by default."""

    async def health(self) -> BackendHealth:
        """Return a :class:`BackendHealth` sample. Only with ``OP_HEALTH``."""

    async def close(self) -> None:
        """Release connections and listeners."""
