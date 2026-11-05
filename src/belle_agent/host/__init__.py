"""Run conversations between platforms and one backend.

The host owns the order of admission, the queue, delivery and silence. A
platform says what arrived and performs what the host decides. A backend runs
the turn the host hands it. Neither decides who is answered or how. Each
record names its platform, and every action that follows from it goes back to
that platform.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.admission.policy import selected_message_trigger
from belle_agent.admission.state import AdmissionState
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
    EVENT_USAGE,
    FAILURE_KINDS,
    NOT_STEERED,
    OP_ACTIVE,
    OP_ANSWER,
    OP_CANCEL,
    OP_FORGET,
    OP_HEALTH,
    OP_PEEK_HISTORY,
    OP_RECOVER,
    OP_REFUSE_QUESTION,
    OP_STEER,
    SUBAGENT,
    Answer,
    Backend,
    BackendCapabilities,
    BackendHealth,
    PeekHistoryPart,
    Question,
    ToolCall,
    TurnEvent,
    TurnRequest,
    TurnScope,
)
from belle_agent.config import BelleConfigError, platform_section
from belle_agent.control.peek import PEEK_KIND_TEXT, PEEK_KIND_THINKING, PeekRegistry
from belle_agent.control.protocol import Component, ControlError, HarnessModelRow, WorkRow, utc_now
from belle_agent.control.service import (
    CachedModelProvider,
    ControlSettings,
    Provider,
    SnapshotCollector,
    StatusService,
    mcp_check,
    unavailable,
)
from belle_agent.host.attached_sessions import (
    AttachedSessionSettings,
    AttachedSessionStore,
    RecordedMessage,
    RunningTurn,
    attached_session_settings,
)
from belle_agent.host.commands import SCOPE_FIELD_SKILLS_REQUIRED, HostCommands, PlatformCommands
from belle_agent.host.decisions import MessageDecisions, decisions_from_config
from belle_agent.host.events import EventAdmission
from belle_agent.host.expiry import delete_attached_session
from belle_agent.host.notes import MESSAGE, SYSTEM_ADDITIONS, TurnNotes
from belle_agent.host.schedule import ScheduledTurns, StoredRecurrence
from belle_agent.host.session import SessionRuns
from belle_agent.identifiers import id_list
from belle_agent.models import TaskModelRow, task_model_rows
from belle_agent.paths import resolve_paths
from belle_agent.platforms.base import (
    ENDED_SILENT,
    MARK_DONE,
    MARK_DROPPED,
    MARK_FAILED,
    MARK_INGESTING,
    MARK_LATE,
    MARK_QUEUED,
    MARK_RECEIVED,
    MARK_REFUSED,
    MARK_STARTED,
    MARK_STEERED,
    MARK_STOPPED,
    MODE_ALL,
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_REPLY,
    DeliveryUncertain,
    InboundAnswer,
    InboundDrop,
    InboundEvent,
    InboundMessage,
    InboundStop,
    Platform,
    QuestionRefused,
    affordance_contradictions,
    affordances,
    create_platform,
    enabled_platforms,
)
from belle_agent.progress import (
    peek_question_text,
    peek_tool_args_text,
    peek_tool_called_fields,
    peek_tool_result_fields,
    peek_tool_result_text,
    peek_usage_fields,
    tool_result_failed,
)
from belle_agent.scheduling.clock import SystemClock
from belle_agent.scheduling.engine import resolve_engine
from belle_agent.scheduling.expiry import Deletions
from belle_agent.scheduling.host_turn import SessionRunTurn
from belle_agent.scheduling.records import owned_conversation
from belle_agent.scheduling.runner import EXPIRY_INTERVAL, RunEnd, Runner
from belle_agent.scheduling.session_toolkit import SessionToolkit
from belle_agent.scheduling.settings import SchedulingSettings, scheduling_settings
from belle_agent.scheduling.store import JobStore
from belle_agent.scheduling.toolkit import ScheduleToolkit
from belle_agent.scopes import (
    CLICK_APPROVE,
    CLICK_STOP,
    KEY_MID_TURN,
    KEY_REPLY,
    KEY_SESSION,
    LEVEL_DENY,
    MID_TURN_CANCEL,
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    PEOPLE_KEY,
    REPLY_OPTIONAL,
    ROLES_KEY,
    SECTION_AGENT,
    SECTION_DELIVERY,
    SECTION_PERMISSIONS,
    SESSION_CHANNEL,
    PeopleDirectory,
    Scope,
    click_rule,
    compile_people,
    compile_scopes,
    compose_section,
    register_channel,
    scope_enables_chat,
    strictest_tool_levels,
)
from belle_agent.second_brain.backend import SecondBrainBackend
from belle_agent.second_brain.hosted import STATUS_FAILED, SecondBrain, ingest_mark, second_brain_from_config
from belle_agent.silence import REPLY_OPTIONAL_KEY, is_no_reply_sentinel, post_tool_instruction
from belle_agent.skills import command_help_instruction, packaged_skill_text, required_skills_instruction
from belle_agent.tools.content import ContentToolkit, content_toolkit_from_config
from belle_agent.tools.host import (
    BoundTurn,
    ToolHost,
    _posted_records,
    approves,
    request_metadata,
    result_text,
    succeeded,
)
from belle_agent.turn_policy import conversation_key, mid_turn_mode

logger = logging.getLogger(__name__)

# The bounds the JiuwenSwarm Slack channel applies to the same state.
MAX_QUEUED_KEYS = 64
MAX_QUEUED_PER_KEY = 8
QUEUED_MESSAGE_TIMEOUT_SECONDS = 3600.0
MAX_TRACKED_TURNS = 256
TURN_TIMEOUT_SECONDS = 3600.0
# What one turn may spend before it is stopped: requests to the model, and cost in the backend's unit.
# A provider that fails every request and a model that calls tools without end both stop here.
# ponytail: two constants. They become settings when a deployment needs other figures.
MAX_TURN_STEPS = 100
MAX_TURN_COST = 5.0
MAX_PENDING_QUESTIONS = 32
# A call held for a person's answer stays answerable for as long as a turn may run. Its turn has ended by
# then or soon will, so the wait is this host's and no backend's.
CONFIRMATION_TIMEOUT_SECONDS = TURN_TIMEOUT_SECONDS
MAX_TURN_RECORDS = 256
# ponytail: fixed sample cadence. Make them settings if a deployment needs other figures.
HARNESS_HEALTH_INTERVAL_SECONDS = 2.0
HARNESS_HEALTH_STALE_SECONDS = 4.0
HARNESS_HEALTH_PROBE_SECONDS = 1.5  # host ceiling; adapters may use a shorter probe timeout

# What a conversation's next turn is told of a turn that a person stopped. It goes into that turn's message, which
# the session keeps: the stopped message stays in the session too, and later turns must not answer it.
STOPPED_NOTE = (
    "A person stopped the turn of message {message} before it ended. Do not answer that message or do its work."
    " Talk about it only if a person asks about it."
)

QUEUE_FULL_NOTICE = (
    f"This conversation is already holding {MAX_QUEUED_PER_KEY} messages"
    " waiting on the turn in progress, which is as many as it holds. Your"
    " message was not accepted and will not be answered. Send it again once"
    " the current work finishes."
)

TURN_TIMEOUT_NOTICE = "The turn did not end within its time limit and was stopped."
TURN_STEPS_NOTICE = "The turn asked the model more times than one turn may and was stopped."
TURN_COST_NOTICE = "The turn cost more than one turn may and was stopped."
RESTART_LOST_NOTICE = "Belle restarted before this turn ended, and its answer is lost. Send the message again."
RESTART_DELIVERING_NOTICE = "Belle restarted while it posted the answer to this message. Part of the answer may be missing."

# How a turn ended, as the host records it.
OUTCOME_POSTED_BY_TOOL = "posted-by-tool"
OUTCOME_POSTED_FINAL = "posted-final"
OUTCOME_STORED_FINAL = "stored-final"
OUTCOME_WITHHELD = "withheld"
OUTCOME_FAILED = "failed"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_DUPLICATE = "duplicate"

# What the platform is told became of a message whose turn ended. A request that ran before settles
# the acknowledgement of its second delivery the way a finished turn does.
_END_MARKS: Mapping[str, str] = {
    OUTCOME_POSTED_BY_TOOL: MARK_DONE,
    OUTCOME_POSTED_FINAL: MARK_DONE,
    OUTCOME_STORED_FINAL: MARK_DONE,
    OUTCOME_WITHHELD: MARK_DONE,
    OUTCOME_DUPLICATE: MARK_DONE,
    OUTCOME_FAILED: MARK_FAILED,
    OUTCOME_INTERRUPTED: MARK_STOPPED,
}

#: The settings of a platform's own section that list who may ask and where, as the Slack channel names them.
KEY_ALLOW_FROM = "allow_from"
KEY_ALLOWED_CONVERSATIONS = "allowed_channel_ids"

#: A conversation with no ``delivery.mode`` answers mentions.
DEFAULT_TRIGGERS: frozenset[str] = frozenset({TRIGGER_MENTION})

#: The ``TurnScope`` field that carries per-tool ``ask``/``deny`` levels.
SCOPE_FIELD_TOOLS = "tools"
SCOPE_FIELD_SYSTEM_ADDITIONS = "system_additions"

# The ``agent`` keys a scope writes, and the scope fields each one needs.
_AGENT_SCOPE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "model_name": ("model",),
    "expert": ("expert",),
    "subagents": ("subagents_available",),
}


@dataclass
class HostedPlatform:
    """One platform a host serves, with what the host keeps for that platform alone."""

    platform: Platform
    dedup: EventDedupStore
    #: Who may ask for a turn, answer a question or stop a turn. Empty admits everyone.
    allow_from: frozenset[str] = frozenset()
    #: The shared conversations served. Empty serves every one. A direct message is not held to it.
    allowed_conversations: frozenset[str] = frozenset()
    #: The decision gate, where an endpoint is configured for it. Without one nothing asks.
    decisions: MessageDecisions | None = None
    #: The content tools, where a provider is configured, with this platform's attachment resolution.
    content_tools: ContentToolkit | None = None
    #: The model a person chose for a conversation's next turns, by ``(workspace, conversation)``. A scope
    #: that selects a model comes first. Held in memory: a restart returns every conversation to its default.
    model_choices: dict[tuple[str, str], str] = field(default_factory=dict)
    #: How many events each conversation lost for good since the start, by ``(workspace, conversation)``.
    lost_events: dict[tuple[str, str], int] = field(default_factory=dict)
    #: The kinds of progress event the platform shows and the backend reports. Settled at start.
    shown: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        """The platform's name, as its records and its scopes name it."""
        return self.platform.name


@dataclass(frozen=True)
class TurnRecord:
    """The host's own account of one ended turn."""

    conversation: str
    request_id: str
    outcome: str


@dataclass
class _SubagentWork:
    id: str
    agent: str | None
    state: str
    started_utc: str | None


#: Reads the files of one message. The platform is asked on the first call, and every call returns what it read.
_ReadFiles = Callable[[], Awaitable[tuple[Mapping[str, Any], ...]]]


@dataclass
class _Turn:
    """One admitted message, while it waits and while it runs."""

    #: The key of the conversation the turn runs in.
    conversation: str
    request: TurnRequest
    inbound: InboundMessage
    reply_optional: bool
    bound: BoundTurn
    queued_at: float = field(default_factory=time.monotonic)
    started_at: float = 0.0
    status_id: str = field(default_factory=lambda: uuid4().hex)
    started_utc: str | None = None
    #: The message waited for another turn of its conversation to end.
    waited: bool = False
    #: Held while the platform is told what became of the message, so it is told in order.
    marking: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The platform has been told to end what it showed of this turn.
    concluded: bool = False
    #: The event that started the turn, where no message did. ``inbound`` then stands where one would.
    event: InboundEvent | None = None
    #: The session run that this turn carries out. None for a turn that a message or an event started.
    session_run: SessionRunTurn | None = None
    question_error: str = ""
    session_run_ended: bool = False
    #: The backend ran this turn before the host started. The host watches it and delivers its answer.
    recovered: bool = False
    #: How a recovered turn that no backend can finish ends. The host delivers it instead of watching the turn.
    lost: TurnEvent | None = None
    reading: asyncio.Task[TurnEvent] | None = field(default=None, repr=False)
    #: The messages added to this turn while it ran. Each ends as the turn ends.
    steered: list["_Turn"] = field(default_factory=list)
    #: The tool events held back from a platform whose card waits for a post: see ``ShowsTurns``.
    unshown: list[TurnEvent] = field(default_factory=list)
    subagents: dict[str, _SubagentWork] = field(default_factory=dict)
    #: Latest turn-cumulative token totals from ``EVENT_USAGE``, when the backend reports them.
    tokens_in: int | None = None
    tokens_out: int | None = None
    #: Stashed peek terminal: ``(state, duration_seconds, ended_at, extra)``.
    peek_outcome: tuple[str, float | None, str, str | None] | None = None
    #: When true, ``_read`` cancel must not tee ``interrupted`` (timeout / stop as failed).
    peek_cancel_as_failed: bool = False
    #: Reads the message's files with the platform call that its ingest shares.
    read_files: _ReadFiles | None = field(default=None, repr=False)

    @property
    def request_id(self) -> str:
        return self.request.request_id


@dataclass
class _HeldCall:
    """One tool call that waits for a person's answer, with the turn that made it."""

    turn: _Turn
    call: ToolCall
    asked_at: float
    deadline: float | None = None
    answered: bool = False


class _QuestionCannotReject(Exception):
    """A scheduled question cannot be safely left open on this backend."""


class _QuestionNotShown(Exception):
    """The platform cannot show a question, and the backend cannot tell its turn why. The message is the reason."""


def _peek_extra(event: TurnEvent) -> str | None:
    """Operator metadata for a peek end line, not streamed answer text.

    Succeeded turns leave ``extra`` empty: the answer is already in text frames.
    Failed and interrupted turns may carry a short reason. Remaining-work notes
    (background shells, open subagents) belong here only when they outlive the
    turn; a clean end has none.
    """
    if event.kind == EVENT_SUCCEEDED:
        return None
    text = event.text
    if not text:
        return None
    cleaned = ''.join(char for char in text if ord(char) >= 32).strip()
    if not cleaned:
        return None
    try:
        cleaned.encode('utf-8')
    except UnicodeEncodeError:
        cleaned = cleaned.encode('utf-8', 'replace').decode('utf-8')
    return cleaned


#: How often a host records that it still runs.
HEARTBEAT_SECONDS = 30

#: No platform takes this id: see ``SESSION_CONVERSATION_PREFIX``.
RESERVED_PLATFORM = "belle"


def _delivers_by_tool(turn: _Turn) -> bool:
    """Whether the turn is a session run whose answer goes only through the platform posting tool."""
    return turn.session_run is not None and turn.session_run.delivery != "post_final"


def _answered_here(turn: _Turn) -> bool:
    """Whether the posting tool put a message in the conversation this turn's answer goes to.

    Threads are not compared. A post names the thread of the message it replied to, which may be a reply
    inside the reply thread rather than its root.
    """
    return any(record["conversation"] == turn.inbound.conversation for record in turn.bound.posted)


def _reply_thread(inbound: InboundMessage) -> str:
    """Name the thread an answer to this message goes in: the platform's placement, or the message's own thread."""
    return inbound.thread if inbound.reply_thread is None else inbound.reply_thread


# A harness's error name and session id, as the log takes them. A line break would forge a log line, and a long
# token is more likely a credential than a name.
_ERROR_TYPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_BACKEND_SESSION = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _failure_fields(terminal: TurnEvent) -> tuple[str, int | str, str, str]:
    """Keep only known, bounded failure metadata for the log."""
    kind = terminal.data.get("failure_kind")
    status = terminal.data.get("status")
    error_type = terminal.data.get("error_type")
    session = terminal.data.get("backend_session")
    return (
        kind if isinstance(kind, str) and kind in FAILURE_KINDS else "-",
        status if type(status) is int and 100 <= status <= 599 else "-",
        error_type if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type) else "-",
        session if isinstance(session, str) and _BACKEND_SESSION.fullmatch(session) else "-",
    )


def _ids(value: Any) -> frozenset[str]:
    """Read a configured list of ids. One id written alone is a list of one."""
    return frozenset(id_list(value))


def _names(value: Any) -> frozenset[str] | None:
    """Read a settled list of names. ``None`` means the scope does not restrict."""
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(item) for item in value)
    return None


class ConversationHost(SessionRuns, ScheduledTurns, HostCommands, EventAdmission):
    """Admit inbound messages, run their turns in order and deliver the result."""

    def __init__(
        self,
        platforms: Sequence[HostedPlatform],
        backend: Backend,
        *,
        scopes: Sequence[Scope] = (),
        second_brain: SecondBrain | None = None,
        notes_file: Path | None = None,
        scheduling: SchedulingSettings | None = None,
        attached_sessions: AttachedSessionSettings | None = None,
        state_dir: Path | None = None,
        directory: PeopleDirectory | None = None,
        control: ControlSettings | None = None,
        task_models: Sequence[TaskModelRow] = (),
    ) -> None:
        self._hosted = {hosted.name: hosted for hosted in platforms}
        if not self._hosted or len(self._hosted) != len(platforms):
            raise ValueError("a host serves one platform or more, each under its own name")
        if RESERVED_PLATFORM in self._hosted:
            # An attached session's key starts with the platform id, so this keeps it apart from a detached session's key.
            raise ValueError(f"the platform id {RESERVED_PLATFORM!r} is reserved for Belle's detached sessions")
        #: What the host's own log lines name: every platform it serves.
        self._label = ",".join(self._hosted)
        self._backend = backend
        self._scopes = tuple(scopes)
        self._second_brain = second_brain
        self.directory = directory or compile_people(None, None)
        self._scheduling = scheduling
        self._schedule_state_dir = state_dir
        #: This process, in the records that several processes of one state directory share.
        self._owner = secrets.token_hex(12)
        self._attached_sessions = AttachedSessionStore(state_dir) if state_dir is not None else None
        self._idle_hours = (attached_sessions or AttachedSessionSettings()).expire_after_idle_hours
        self._idle_deletions = Deletions("attached session")
        #: One idle pass runs at a time, as for detached sessions.
        self._idle_pass = asyncio.Lock()
        self._attached_sessions_task: asyncio.Task[None] | None = None
        #: When the heartbeat loop last deleted idle attached sessions, on the monotonic clock.
        self._idle_expired_at = 0.0
        #: Keys whose chat turn another live process runs, or a recovery pass has yet to settle. A message waits.
        self._held_keys: set[str] = set()
        #: Keys whose idle attached session Belle deletes now. A message waits until the deletion settles.
        self._deleting: set[str] = set()
        #: Set once every platform started: a recovered turn delivers through its platform.
        self._platforms_started = asyncio.Event()
        self._tools = ToolHost([second_brain.toolkit] if second_brain else [], confirm=self._hold_call)
        for name, hosted in self._hosted.items():
            toolkits = hosted.platform.toolkits(request_metadata)
            if hosted.content_tools is not None:
                toolkits = [*toolkits, hosted.content_tools]
            self._tools.serve(
                name, toolkits, posting_tool=hosted.platform.posting_tool,
                posting_tools=getattr(hosted.platform, "posting_tools", ()),
            )
            # What a command surface of the platform asks of this host. See ``belle_agent.host.commands``.
            hosted.platform.bind(PlatformCommands(self, name))
        self._capabilities: BackendCapabilities | None = None
        #: The host puts the text of a turn's required skills in its system additions. See ``_applied``.
        self._supplies_skills = False
        self._admission = AdmissionState(label=self._label)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._active_work: dict[str, _Turn] = {}
        # ``(platform, question id)`` to the turn that waits on it. A backend may derive a question id from a
        # request id, which names a request on one platform only.
        self._questions: dict[tuple[str, str], _Turn] = {}
        self._question_timeouts: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Question id to the tool call that runs if a person agrees. A held call outlives its turn.
        self._held: dict[str, _HeldCall] = {}
        self._held_timeouts: dict[str, asyncio.Task[None]] = {}
        self._expired_questions: dict[tuple[str, str], tuple[_Turn, float]] = {}
        self._held_ids = itertools.count(1)
        # What each conversation's next turn is told of its held calls and stopped turns.
        self._notes = TurnNotes(notes_file)
        #: Reads the time a held call is aged by.
        self.clock = time.monotonic
        self._running = False
        self.scheduler: Runner | None = None
        self.schedule_toolkit: ScheduleToolkit | None = None
        self.session_toolkit: SessionToolkit | None = None
        #: The conversation of each detached session takes one turn at a time. Each lock has the count of the runs that
        #: hold it or wait for it, and goes with the last one.
        self._session_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        #: Run id to the task that waits for its session's conversation.
        self._run_waiters: dict[str, asyncio.Task[None]] = {}
        #: Latest harness health sample; snapshot providers read this only (no remote I/O).
        self._harness_health: Component | None = None
        self._harness_health_task: asyncio.Task[None] | None = None
        control = control or ControlSettings()
        self._peeks = PeekRegistry(
            max_frames=control.peek_max_frames,
            max_bytes=control.peek_max_bytes,
            max_subscribers=control.peek_max_subscribers,
            subscriber_idle=float(control.peek_subscriber_idle_seconds),
        )
        #: In-flight or done ``peek_history`` loads keyed by main-turn buffer id.
        self._peek_seeding: dict[str, asyncio.Task[tuple[PeekHistoryPart, ...]]] = {}
        #: Openers awaiting history for a buffer (refcount; last abandon drops reservation).
        self._peek_open_waiters: dict[str, int] = {}

        providers: dict[str, Provider] = {'work': self.status_work, 'scheduling': self.status_scheduling}
        for name in self._hosted:
            async def platform_status(identity: str = name) -> Component:
                return unavailable(identity, 'remote_health_not_measured', state='unsupported')
            providers[f'platforms/{name}'] = platform_status
        providers['harnesses/backend'] = self.status_harness
        async def harness_models() -> list[HarnessModelRow] | None:
            capabilities = self._backend.capabilities()
            if CATALOG_MODELS not in capabilities.catalog:
                return None
            try:
                catalog = await self._backend.catalog()
            except Exception:  # noqa: BLE001 - a status snapshot reports unavailable instead of failing.
                return None
            if catalog is None:
                return None
            return [{'owner': capabilities.backend, 'model_name': name} for name in catalog.models.names]
        # TUI polls every two seconds; this avoids repeated remote catalogue requests per poll.
        cached_harness_models = CachedModelProvider(
            harness_models, ttl=30.0, timeout=control.provider_timeout,
        )
        self._control = StatusService(SnapshotCollector(providers, timeout=control.provider_timeout,
                                                        task_models=task_models,
                                                        harness_models=cached_harness_models),
                                      resolve_paths().runtime_dir if control.enabled else None, control,
                                      toggle_schedule=self.toggle_schedule, delete_schedule=self.delete_schedule,
                                      stop_work=self.stop_work, drop_work=self.drop_work,
                                      peek_open=self.peek_open, peek_poll=self.peek_poll, peek_close=self.peek_close)
        self._init_events()
        #: Ended turns, oldest first.
        self.ended: deque[TurnRecord] = deque(maxlen=MAX_TURN_RECORDS)

    def _on(self, platform: str | None = None) -> HostedPlatform:
        """The platform of that name, or with no name the one platform this host serves."""
        if platform is not None:
            return self._hosted[platform]
        if len(self._hosted) != 1:
            raise ValueError(f"this host serves the platforms {self._label}: name one")
        return next(iter(self._hosted.values()))

    @property
    def platform(self) -> Platform:
        """The one platform this host serves. A host of several platforms names them in ``platforms``."""
        return self._on().platform

    @property
    def platforms(self) -> Mapping[str, Platform]:
        """Each platform this host serves, by name, in the order they start."""
        return {name: hosted.platform for name, hosted in self._hosted.items()}

    async def wait_down(self) -> dict[str, str]:
        """Return why each platform stopped, by name, once every platform has stopped for good.

        A platform without ``wait_down`` never stops for good, so then this waits for ever.
        """
        waits = {name: getattr(hosted.platform, "wait_down", None) for name, hosted in self._hosted.items()}
        if not all(waits.values()):
            await asyncio.Event().wait()
        reasons = await asyncio.gather(*(wait() for wait in waits.values() if wait is not None))
        return dict(zip(waits, reasons, strict=True))

    @property
    def model_choices(self) -> dict[tuple[str, str], str]:
        """The models people chose on the one platform this host serves. See ``HostedPlatform.model_choices``."""
        return self._on().model_choices

    @property
    def decides(self) -> bool:
        """Whether a decision gate is asked about the messages this host admits."""
        return any(hosted.decisions is not None for hosted in self._hosted.values())

    @property
    def tools(self) -> ToolHost:
        return self._tools

    async def start(self) -> None:
        """Start the backend, then the platform."""
        for name, hosted in self._hosted.items():
            for detail in affordance_contradictions(hosted.platform):
                logger.warning("%s platform affordance contradiction: %s", name, detail)
        self._capabilities = self._applied(self._backend.capabilities())
        if self._scheduling is not None:
            engine = resolve_engine(self._scheduling.engine, self._capabilities)
            if engine == "belle":
                assert self._schedule_state_dir is not None
                clock = SystemClock()
                store = JobStore(self._schedule_state_dir, kept_runs_per_job=self._scheduling.kept_runs_per_job, clock=clock)
                self.scheduler = Runner(
                    store, self._scheduling, self, clock, StoredRecurrence(), owner=self._owner
                )
                self.schedule_toolkit = ScheduleToolkit(store, self.scheduler, self._scheduling, self, clock)
                self.session_toolkit = SessionToolkit(store, self.scheduler, self.schedule_toolkit, clock)
                self._tools.serve("", [self.schedule_toolkit, self.session_toolkit])
        for hosted in self._hosted.values():
            hosted.shown = self._settle_shown(hosted.platform, self._capabilities)
        if self._second_brain is not None:
            # One corpus serves every platform: a platform that names its public conversations gives ``public`` a meaning.
            public_known = any(hosted.platform.public_chat_types for hosted in self._hosted.values())
            for warning in self._second_brain.warnings(public_known=public_known):
                logger.warning("%s: %s", self._label, warning)
            await self._second_brain.start()
        await self._backend.start(self._tools)
        # A backend may learn what it applies, such as the experts and the content its model
        # takes, only once it has started.
        self._capabilities = self._applied(self._backend.capabilities())
        if self._scheduling is not None and resolve_engine(self._scheduling.engine, self._capabilities) != engine:
            raise RuntimeError("backend scheduler capability changed during startup")
        self._warn_about_unapplied_settings(self._capabilities)
        if self._idle_hours is not None and OP_FORGET not in self._capabilities.operations:
            raise BelleConfigError(
                f"Belle config attached_sessions.expire_after_idle_hours needs a backend that deletes a conversation, "
                f"and backend {self._capabilities.backend} does not. Set it to disabled"
            )
        self._tools.content_types = self._capabilities.tool_content
        await self._stop_orphans()
        self._running = True
        if self._attached_sessions is not None:
            # Before any message arrives: a message on the key of a turn that a stopped process left waits for it.
            try:
                await self._recover_chat_turns()
            except Exception:  # The heartbeat loop tries again.
                logger.exception("%s could not recover the turns of a stopped process", self._label)
        for name, hosted in self._hosted.items():
            try:
                await hosted.platform.start(self.receive)
            except Exception:
                logger.error("%s could not start, so the host does not start", name)
                raise
        self._platforms_started.set()
        if self._attached_sessions is not None:
            self._attached_sessions_task = asyncio.create_task(self._attached_sessions_loop(), name=f"{self._label}-attached-sessions")
            self._attached_sessions_task.add_done_callback(self._loop_ended)
        if self.scheduler is not None:
            await self.scheduler.start()

        await self._refresh_harness_health()
        assert self._capabilities is not None
        if OP_HEALTH in self._capabilities.operations:
            self._harness_health_task = asyncio.create_task(
                self._harness_health_loop(), name=f'{self._label}-harness-health',
            )
        await self._control.start()

    async def toggle_schedule(self, job_id: str, expected_state: str) -> None:
        """Apply the same-UID operator's scheduler state change."""
        from belle_agent.control.protocol import ControlError
        from belle_agent.scheduling.refusals import Refused

        if self.scheduler is None:
            raise ControlError('unsupported', 'This host has no Belle scheduler')
        try:
            await self.scheduler.toggle_job(job_id, expected_state)
        except Refused as exc:
            raise ControlError(exc.code, exc.detail) from None

    async def delete_schedule(self, job_id: str, expected_state: str) -> None:
        """Delete a job for the authenticated local socket operator."""
        from belle_agent.control.protocol import ControlError
        from belle_agent.scheduling.refusals import Refused

        if self.scheduler is None:
            raise ControlError('unsupported', 'This host has no Belle scheduler')
        try:
            await self.scheduler.delete_job(job_id, expected_state)
        except Refused as exc:
            raise ControlError(exc.code, exc.detail) from None

    def _abandon_peek_open(self, buffer_id: str) -> None:
        """Drop one opener's reservation; last waiter clears unseeded buffer and seed task."""
        waiters = self._peek_open_waiters.get(buffer_id, 0) - 1
        if waiters > 0:
            self._peek_open_waiters[buffer_id] = waiters
            return
        self._peek_open_waiters.pop(buffer_id, None)
        if self._peeks.is_seeded(buffer_id):
            return
        task = self._peek_seeding.pop(buffer_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._peeks.discard_if_unsubscribed(buffer_id)

    def _finish_peek_open_wait(self, buffer_id: str) -> None:
        """Release a waiter after a successful open without discarding the buffer."""
        waiters = self._peek_open_waiters.get(buffer_id, 0) - 1
        if waiters > 0:
            self._peek_open_waiters[buffer_id] = waiters
        else:
            self._peek_open_waiters.pop(buffer_id, None)
        if self._peeks.is_seeded(buffer_id):
            self._peek_seeding.pop(buffer_id, None)

    async def peek_open(self, work_id: str) -> dict:
        """Open a live peek on a running main or subagent Activity row.

        The ring is always keyed by the main turn. Opening a subagent row filters
        to that child's ``source_work_id`` (plus terminal ``outcome``). When the
        buffer is new and the backend advertises ``OP_PEEK_HISTORY``, seed it once
        with prior harness text (seed parts carry ``source_work_id`` for filters).
        Belle does not keep turn history itself.
        """
        turn = next((item for item in self._active_work.values() if item.status_id == work_id), None)
        source_filter: str | None = None
        buffer_id = work_id
        if turn is None:
            for item in self._active_work.values():
                child = next((c for c in item.subagents.values() if c.id == work_id), None)
                if child is None:
                    continue
                if child.state != 'running':
                    raise ControlError(
                        'peek_unavailable', 'work_id must name a running main or subagent turn',
                    )
                turn = item
                buffer_id = item.status_id
                source_filter = child.id
                break
        if turn is None:
            raise ControlError(
                'peek_unavailable', 'work_id must name a running main or subagent turn',
            )
        seed: tuple[PeekHistoryPart, ...] = ()
        seed_done = False
        reserved = False
        if self._supports(OP_PEEK_HISTORY) and not self._peeks.is_seeded(buffer_id):
            if not self._peeks.has_buffer(buffer_id):
                self._peeks.require_subscriber_slot()
                self._peeks.ensure_buffer(buffer_id)
            self._peek_open_waiters[buffer_id] = self._peek_open_waiters.get(buffer_id, 0) + 1
            reserved = True
            task = self._peek_seeding.get(buffer_id)
            if task is None:
                async def _load_seed() -> tuple[PeekHistoryPart, ...]:
                    try:
                        return tuple(
                            self._peek_seed_part(turn, part)
                            for part in await self._backend.peek_history(
                                turn.request.conversation, turn.request_id,
                            )
                        )
                    except Exception as exc:
                        self._peek_seeding.pop(buffer_id, None)
                        if isinstance(exc, ControlError):
                            raise
                        logger.warning(
                            'peek_history failed: conversation=%s exception_type=%s',
                            turn.conversation, type(exc).__name__,
                        )
                        raise ControlError(
                            'peek_unavailable', 'history unavailable; retry',
                        ) from exc

                task = asyncio.create_task(_load_seed())
                self._peek_seeding[buffer_id] = task
            try:
                seed = await asyncio.shield(task)
                seed_done = True
            except BaseException:
                self._abandon_peek_open(buffer_id)
                raise
        try:
            opened = self._peeks.open(
                buffer_id, seed=seed, seed_done=seed_done,
                source_filter=source_filter, opened_work_id=work_id,
            )
        except BaseException:
            if reserved:
                self._abandon_peek_open(buffer_id)
            else:
                self._peeks.discard_if_unsubscribed(buffer_id)
            raise
        if reserved:
            self._finish_peek_open_wait(buffer_id)
        if turn.peek_outcome is not None:
            self._apply_stashed_peek_outcome(turn)
            opened = self._peeks.refresh_open(opened['peek_id'])
        if not any(item is turn for item in self._active_work.values()):
            self._peeks.mark_ended(buffer_id)
            return {**opened, 'live': False}
        return opened

    async def peek_poll(self, peek_id: str, after_seq: int) -> dict:
        """Return peek frames after ``after_seq`` for an open peek."""
        return self._peeks.poll(peek_id, after_seq)

    async def peek_close(self, peek_id: str) -> None:
        """Drop a peek subscriber and discard the buffer when none remain."""
        self._peeks.close(peek_id)

    async def stop_work(self, work_id: str, expected_state: str) -> None:
        """Stop one main turn for the same-UID local operator."""
        from belle_agent.control.protocol import ControlError

        if expected_state != 'running':
            raise ControlError('invalid_request', 'expected_state must be running')
        if not self._running:
            raise ControlError('unavailable', 'Host is not running')
        turn = next((turn for turn in self._active_work.values() if turn.status_id == work_id), None)
        if turn is None:
            raise ControlError('stale_selection', 'Running work no longer exists; refresh Activity')
        if not self._supports(OP_CANCEL):
            raise ControlError('unsupported', 'This backend cannot stop running work')

        def active() -> bool:
            # A scheduler store write can yield while the selected turn finishes.
            return any(running is turn for running in self._active_work.values())

        if not await self.stop_turn(turn, active=active):
            if not active():
                raise ControlError('stale_selection', 'Running work ended; refresh Activity')
            raise ControlError('action_failed', 'Backend did not stop the selected work; refresh Activity')

    async def drop_work(self, work_id: str, expected_state: str) -> None:
        """Drop one queued main message for the same-UID local operator."""
        from belle_agent.control.protocol import ControlError
        from belle_agent.scheduling.refusals import Refused

        if expected_state != 'queued':
            raise ControlError('invalid_request', 'expected_state must be queued')
        if not self._running:
            raise ControlError('unavailable', 'Host is not running')
        for key, queue in self._admission.queued_messages.items():
            turn = next((turn for turn in queue if turn.status_id == work_id), None)
            if turn is None:
                continue
            queue.remove(turn)
            if not queue:
                del self._admission.queued_messages[key]
            await self._mark_turn(turn, MARK_DROPPED)
            return
        if self.scheduler is not None:
            try:
                await self.scheduler.drop_pending_run(work_id)
                return
            except Refused as exc:
                raise ControlError(exc.code, exc.detail) from None
        raise ControlError('stale_selection', 'Waiting work no longer exists; refresh Activity')

    async def status_scheduling(self) -> Component:
        """Read scheduler metadata from its last existing store transaction."""
        if self.scheduler is None:
            return unavailable('scheduling', 'no_belle_scheduler', state='unsupported')
        return self.scheduler.status()

    async def status_harness(self) -> Component:
        """Return the cached harness health sample without remote I/O."""
        sample = self._harness_health
        if sample is None:
            return {**unavailable(self._backend.capabilities().backend, 'health_sample_pending'),
                    'mcp': mcp_check('unknown', 'health_sample_pending')}
        observed = sample.get('observed_at')
        if observed is not None:
            stamp = datetime.fromisoformat(observed)
            if (datetime.now(UTC) - stamp).total_seconds() > HARNESS_HEALTH_STALE_SECONDS:
                return {**unavailable(sample['id'], 'health_sample_stale'),
                        'mcp': mcp_check('unknown', 'health_sample_stale')}
        return sample

    async def _harness_health_loop(self) -> None:
        while self._running:
            await asyncio.sleep(HARNESS_HEALTH_INTERVAL_SECONDS)
            if self._running:
                await self._refresh_harness_health()

    async def _refresh_harness_health(self) -> None:
        identity = self._backend.capabilities().backend
        assert self._capabilities is not None
        if OP_HEALTH not in self._capabilities.operations:
            self._harness_health = {
                **unavailable(identity, 'remote_health_not_measured', state='unsupported'),
                'mcp': mcp_check('not_applicable'),
            }
            return
        try:
            async with asyncio.timeout(HARNESS_HEALTH_PROBE_SECONDS):
                sample = await self._backend.health()
        except Exception:  # noqa: BLE001 - probe faults become disconnected, not a snapshot failure.
            observed = utc_now()
            self._harness_health = {
                'id': identity, 'state': 'disconnected', 'reason': 'unreachable',
                'observed_at': observed, 'metrics': {},
                'mcp': mcp_check('unknown', 'unreachable'),
            }
            return
        if not isinstance(sample, BackendHealth):
            self._harness_health = {
                **unavailable(identity, 'health_sample_invalid'),
                'mcp': mcp_check('unknown', 'health_sample_invalid'),
            }
            return
        detail = sample.detail.strip()
        reason = detail if sample.state in {'degraded', 'disconnected'} else None
        observed = utc_now()
        self._harness_health = {
            'id': identity, 'state': sample.state, 'reason': reason,
            'observed_at': observed, 'metrics': {},
            'mcp': mcp_check(sample.mcp.state, sample.mcp.reason or None, observed),
        }

    async def status_work(self) -> Component:
        """Sample admission-owned counts on the host event loop without yielding."""
        admission = self._admission
        harness = self._backend.capabilities().backend
        rows: list[WorkRow] = []
        for state, turns in (('running', self._active_work.values()),
                             ('queued', (turn for queue in admission.queued_messages.values() for turn in queue))):
            for turn in turns:
                model = str(turn.request.scope.model or '').strip() or None
                main: WorkRow = {
                    'id': turn.status_id, 'state': state, 'type': 'main', 'agent': None, 'parent': None,
                    'harness': harness, 'platform': turn.inbound.platform, 'workspace': turn.inbound.workspace,
                    'conversation': turn.inbound.conversation, 'started_at': turn.started_utc if state == 'running' else None,
                    'model': model,
                    'tokens_in': turn.tokens_in if state == 'running' else None,
                    'tokens_out': turn.tokens_out if state == 'running' else None,
                }
                rows.append(main)
                if state == 'running':
                    rows.extend({**main, 'id': child.id, 'state': child.state, 'type': 'subagent',
                                 'agent': child.agent, 'parent': turn.status_id, 'started_at': child.started_utc}
                                for child in turn.subagents.values())
        if self.scheduler is not None:
            rows.extend(self.scheduler.pending_work())
        return {
            'id': 'work', 'state': 'ready' if self._running else 'disabled', 'reason': None,
            'observed_at': utc_now(),
            'rows': rows,
            'metrics': {
                'active_turns': {'value': len(self._active_work), 'reason': None},
                'queued_messages': {'value': sum(len(queue) for queue in admission.queued_messages.values()),
                                    'reason': None},
                'queued_events': {'value': admission.pending_count(), 'reason': None},
            },
        }

    def _applied(self, capabilities: BackendCapabilities) -> BackendCapabilities:
        """What a turn on this host gets: what the backend applies, and what the host applies for it.

        A backend that cannot attach a skill to a turn and takes system additions gets the text of each
        required packaged skill in the turn's additions.
        """
        fields = capabilities.scope_fields
        self._supplies_skills = SCOPE_FIELD_SKILLS_REQUIRED not in fields and SCOPE_FIELD_SYSTEM_ADDITIONS in fields
        if not self._supplies_skills:
            return capabilities
        return replace(capabilities, scope_fields=fields | {SCOPE_FIELD_SKILLS_REQUIRED})

    async def _stop_orphans(self) -> None:
        """Stop the chat turns a backend still runs from before this host started.

        Such a turn outlived the process that started it. Where its answer
        goes, whether one is owed and what its tools may touch were held by
        that process and are gone, so it cannot be delivered. It is stopped,
        so that it spends nothing more and the conversation's next turn does
        not start beside it. A running session run is left to the scheduler's
        recovery, which reads its stored run. A chat turn with a record is left
        to ``_recover_chat_turns``, which takes it once its owner is gone.
        """
        if not (self._supports(OP_ACTIVE) and self._supports(OP_CANCEL)):
            return
        protected: set[str] = set()
        if self.scheduler is not None:
            async with self.scheduler.store.transaction() as snapshot:
                protected = {
                    owned_conversation(session["session_id"]) for session in snapshot.sessions.values()
                    if any(run["state"] == "running" for run in session["runs"])
                }
        if self._attached_sessions is not None:
            async with self._attached_sessions.transaction() as data:
                protected |= {key for key, record in data["sessions"].items() if record["running"] is not None}
        for conversation in await self._backend.active():
            if conversation in protected:
                continue
            stopped = await self._backend.cancel(conversation)
            logger.warning(
                "%s found a turn running from before this start and %s: conversation=%s",
                self._label,
                "stopped it" if stopped else "could not stop it",
                conversation,
            )

    async def stop(self) -> None:
        """Stop admitting, leave running turns and close the backend.

        A backend that can recover a turn keeps it running, and the next start delivers it.
        """
        self._backend.shutting_down()
        if self.scheduler is not None:
            await self.scheduler.stop()
        attached_sessions_task, self._attached_sessions_task = self._attached_sessions_task, None
        if attached_sessions_task is not None:
            attached_sessions_task.cancel()
            await asyncio.gather(attached_sessions_task, return_exceptions=True)
        self._running = False
        health_task = self._harness_health_task
        self._harness_health_task = None
        if health_task is not None:
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
        await self._control.stop()
        for name, hosted in self._hosted.items():
            try:
                await hosted.platform.stop()
            except Exception:  # The other platforms and the backend still stop.
                logger.exception("%s could not stop", name)
        held = sum(len(queue) for queue in self._admission.queued_messages.values())
        if held:
            logger.warning(
                "%s dropped %s queued message(s) across %s attached session(s): the host"
                " stopped before the turns they were waiting on ended",
                self._label,
                held,
                len(self._admission.queued_messages),
            )
        self._stop_events()
        self._admission.clear()
        self._questions.clear()
        self._held.clear()
        timers = [*self._question_timeouts.values(), *self._held_timeouts.values()]
        self._question_timeouts.clear()
        self._held_timeouts.clear()
        for task in timers:
            task.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for hosted in self._hosted.values():
            if hosted.decisions is not None:
                await hosted.decisions.close()
        await self._backend.close()

    async def idle(self) -> None:
        """Wait until no turn runs and none waits."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── admission ────────────────────────────────────────────────────────

    async def receive(self, inbound: InboundMessage | InboundAnswer | InboundStop | InboundDrop | InboundEvent) -> str:
        """Take one record from a platform and report what was done with it."""
        if inbound.platform not in self._hosted:
            logger.warning("%s handed a record of %s, a platform it does not serve", self._label, inbound.platform or "-")
            return "dropped:platform-not-served"
        if isinstance(inbound, InboundEvent):
            outcome = await self._receive_event(inbound)
            identity = f"event={inbound.event_id or '-'} kind={inbound.kind or '-'}"
        elif isinstance(inbound, InboundAnswer):
            outcome = await self._receive_answer(inbound)
            identity = f"question={inbound.question_id}"
        elif isinstance(inbound, InboundDrop):
            outcome = await self._receive_drop(inbound)
            identity = f"drop={inbound.message_id or '-'}"
        elif isinstance(inbound, InboundStop):
            outcome = await self._receive_stop(inbound)
            identity = f"stop={inbound.message_id or '-'}"
        else:
            outcome = await self._admit(inbound)
            identity = f"message={inbound.message_id or '-'} event={inbound.event_id or '-'}"
        logger.info(
            "%s inbound: conversation=%s %s sender=%s outcome=%s",
            inbound.platform,
            inbound.conversation or "-",
            identity,
            (inbound.actor if isinstance(inbound, InboundEvent) else inbound.sender) or "-",
            outcome,
        )
        return outcome

    def _settle(self, inbound: InboundMessage, section: str, layer0: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return compose_section(
            self._scopes,
            channel=inbound.platform,
            chat=inbound.conversation or None,
            chat_type=inbound.chat_type or None,
            workspace=inbound.workspace or None,
            user=inbound.sender,
            section=section,
            layer0=layer0,
        )

    @staticmethod
    def _trigger(inbound: InboundMessage, triggers: frozenset[str]) -> str:
        """Name what lets this message start a turn, or nothing."""
        if inbound.trigger == TRIGGER_DM:
            return TRIGGER_DM
        if inbound.trigger == TRIGGER_MENTION:
            # ``all`` covers a mention, as it covers every other message.
            return TRIGGER_MENTION if triggers & {TRIGGER_MENTION, MODE_ALL} else ""
        return selected_message_trigger(
            triggers, inbound.text, reply_to_bot=inbound.trigger == TRIGGER_REPLY,
            has_file=bool(inbound.files),
        )

    def _sender_allowed(self, sender: str, platform: str | None = None) -> bool:
        allow_from = self._on(platform).allow_from
        return not allow_from or sender in allow_from

    def _conversation_allowed(self, conversation: str, platform: str | None = None) -> bool:
        """Whether a shared conversation is served: no list, a listed one, or one a scope of its own opts in."""
        hosted = self._on(platform)
        return (
            not hosted.allowed_conversations
            or conversation in hosted.allowed_conversations
            or scope_enables_chat(self._scopes, channel=hosted.name, chat=conversation)
        )

    async def _post(self, inbound: InboundMessage, text: str) -> str:
        """Post where an answer to this message goes, in the workspace the message came from."""
        return await self._hosted[inbound.platform].platform.post(
            inbound.conversation, _reply_thread(inbound), text, workspace=inbound.workspace
        )

    async def _tell_privately(self, inbound: InboundMessage, person: str, text: str) -> None:
        """Post a notice for ``person`` alone where an answer to this message goes.

        A platform may refuse a post for one person, as a server does when the bot lacks a permission. The refusal is
        logged, and the caller goes on.
        """
        try:
            await self._hosted[inbound.platform].platform.post(
                inbound.conversation, _reply_thread(inbound), text, ephemeral_to=person, workspace=inbound.workspace
            )
        except Exception as exc:  # noqa: BLE001 - a notice is a courtesy: what became of the message stands without it.
            logger.warning(
                "%s could not post a notice for %s alone: %s: conversation=%s message=%s",
                inbound.platform, person, exc, inbound.conversation, inbound.message_id or "-",
            )

    async def _post_scheduled(self, turn: _Turn, text: str) -> None:
        """Post and record a scheduled reply, including the root of a new thread."""
        inbound = turn.inbound
        thread = _reply_thread(inbound)
        message_id = await self._hosted[inbound.platform].platform.post(
            inbound.conversation, "" if thread == "new" else thread, text, workspace=inbound.workspace
        )
        if thread == "new":
            thread = message_id
            turn.inbound = replace(inbound, thread=thread, reply_thread=thread)
        turn.bound.posted.append({"conversation": inbound.conversation, "thread": thread, "message_id": message_id})

    async def _ask_where_it_came_from(self, inbound: InboundMessage, question: Question) -> str:
        return await self._hosted[inbound.platform].platform.ask(
            inbound.conversation, _reply_thread(inbound), question, workspace=inbound.workspace
        )

    async def _mark(self, inbound: InboundMessage, state: str) -> None:
        """Tell the platform what became of a message. A mark that fails changes nothing else."""
        try:
            await self._hosted[inbound.platform].platform.mark(inbound, state)
        except Exception:  # A mark is a courtesy, and no turn depends on it.
            logger.warning(
                "%s could not mark a message as %s: conversation=%s message=%s",
                inbound.platform, state, inbound.conversation, inbound.message_id or "-", exc_info=True,
            )

    async def _mark_turn(self, turn: "_Turn", state: str) -> None:
        """Mark an admitted message. A platform call takes time, and a turn can end or start within it."""
        if turn.event is not None or turn.session_run is not None:
            # Nobody sent a message that could be marked: the one an event is about is somebody else's.
            return
        async with turn.marking:
            await self._mark(turn.inbound, state)

    def _turn_scope(self, inbound: InboundMessage, delivery: Mapping[str, Any], *, reply_optional: bool) -> TurnScope:
        agent = self._settle(inbound, SECTION_AGENT)
        skills = agent.get("skills")
        skills = skills if isinstance(skills, Mapping) else {}
        additions = [str(delivery["prompt"])] if delivery.get("prompt") else []
        if reply_optional:
            assert self._capabilities is not None
            posting_tool = self._hosted[inbound.platform].platform.posting_tool
            additions.append(post_tool_instruction(self._capabilities.tool_name(posting_tool)))
        if self._second_brain is not None and self._second_brain.available:
            additions.append(self._second_brain.instructions)
        # What the scopes require, then what a person's command requires of this one turn.
        required = tuple(dict.fromkeys((*(skills.get("required") or ()), *inbound.skills_required)))
        if "belle-command-help" in required:
            prefix = self._hosted[inbound.platform].platform.command_prefix(inbound.workspace)
            if instruction := command_help_instruction(prefix):
                additions.append(instruction)
        if required and self._supplies_skills and (instruction := required_skills_instruction(required)):
            additions.append(instruction)
        levels = strictest_tool_levels(self._scopes, channel=inbound.platform, chat=inbound.conversation or None)
        return TurnScope(
            # A tool of another platform is denied, so a backend that narrows its roster by scope does not offer it.
            tools={**levels, **dict.fromkeys(self._tools.foreign(inbound.platform), LEVEL_DENY)},
            skills_available=_names(skills.get("available")),
            skills_unavailable=_names(skills.get("unavailable")) or frozenset(),
            skills_required=required,
            expert=agent["expert"] if isinstance(agent.get("expert"), str) else "",
            subagents_available=_names(agent.get("subagents")),
            subagent_required=inbound.subagent_required,
            # A scope's model comes before the one a person chose for the conversation.
            model=str(agent.get("model_name") or "").strip()
            or self._hosted[inbound.platform].model_choices.get((inbound.workspace, inbound.conversation), ""),
            system_additions=tuple(additions),
            withhold_final=reply_optional,
        )

    async def _admit(self, inbound: InboundMessage) -> str:
        if not self._running:
            return "dropped:host-not-running"
        if inbound.sender_is_bot:
            return "ignored:posted-by-an-app"
        if inbound.edited:
            return "ignored:edit-is-not-new-content"
        if not (inbound.sender and inbound.conversation):
            return "dropped:no-sender-or-conversation"

        # A document is ingested whether or not its message starts a turn, unless it came too late for either.
        # Both read its files once.
        read_files = self._files_once(inbound)
        ingesting = not inbound.late and self._dispatch_ingest(inbound, read_files)

        delivery = self._settle(inbound, SECTION_DELIVERY, {"mode": DEFAULT_TRIGGERS})
        # Composition leaves ``mode`` absent where no scope sets it.
        trigger = self._trigger(inbound, _names(delivery.get("mode", DEFAULT_TRIGGERS)) or frozenset())
        if not trigger:
            return "ingested:second-brain" if ingesting else "ignored:no-trigger-matched"

        # The message id comes first: one message can arrive as two events.
        hosted = self._hosted[inbound.platform]
        identity = inbound.message_id or inbound.event_id
        if identity and not await hosted.dedup.remember(f"{inbound.workspace}:{inbound.conversation}:{identity}"):
            return "ignored:already-handled (dedupe)"

        direct = trigger == TRIGGER_DM
        if not direct and not self._conversation_allowed(inbound.conversation, inbound.platform):
            return f"ignored:channel-not-in-{KEY_ALLOWED_CONVERSATIONS}"
        if not self._sender_allowed(inbound.sender, inbound.platform):
            await self._mark(inbound, MARK_REFUSED)
            return f"refused:user-not-in-{KEY_ALLOW_FROM}"
        if inbound.late:
            # The mark tells a sender owed an answer that none comes. A message whose reply is optional owes none.
            if direct or delivery.get(KEY_REPLY) != REPLY_OPTIONAL:
                await self._mark(inbound, MARK_LATE)
                return "refused:reached-belle-late"
            return "ignored:reached-belle-late"
        if hosted.decisions is not None:
            hosted.decisions.arrived(inbound)

        if not inbound.text.strip() and not inbound.files:
            return "dropped:no-text-and-no-file"

        shared_session = not direct and delivery.get(KEY_SESSION) == SESSION_CHANNEL
        key = conversation_key(
            platform=inbound.platform,
            workspace=inbound.workspace,
            conversation=inbound.conversation,
            sender=inbound.sender,
            thread=inbound.thread or inbound.message_id,
            direct=direct,
            shared_session=shared_session,
        )
        if not key:
            logger.warning(
                "%s message carried no workspace; dropping it rather than sharing"
                " a session with another workspace: conversation=%s message=%s",
                inbound.platform,
                inbound.conversation,
                inbound.message_id,
            )
            return "dropped:no-workspace-on-message"
        if inbound.side:
            key = f"{key}_{inbound.side}"

        # A direct message is always owed an answer.
        reply_optional = not direct and delivery.get(KEY_REPLY) == REPLY_OPTIONAL
        turn = self._turn(inbound, key, delivery, reply_optional=reply_optional)
        turn.read_files = read_files
        unmet = self._unmet_requirement(inbound, turn.request.scope)
        if unmet:
            return unmet

        mid_turn = mid_turn_mode(
            delivery.get(KEY_MID_TURN),
            shared_session=shared_session,
            requires_separate_turn=not self._supports(OP_STEER),
        )
        superseded = self._admission.turn_initiator(key, timeout=TURN_TIMEOUT_SECONDS)
        # A message waits for the turn of an event, whatever ``mid_turn`` says.
        event_turn = key in self._admission.event_turn_sessions
        if superseded is not None and mid_turn == MID_TURN_CANCEL and not event_turn:
            # The new message then runs once the stopped turn has closed.
            await self._cancel(superseded)
        steering = mid_turn == MID_TURN_STEER and not event_turn
        if steering and superseded is not None and await self._steer(superseded, turn):
            return f"steered:{trigger} conversation={key}"
        if self._admission.must_queue_message(
            key, mid_turn=MID_TURN_QUEUE, turn_running=self._occupied(key), event_turn_active=event_turn
        ):
            return await self._queue(turn, trigger)
        self._start(turn)
        # Told after the turn has its place, so a slow platform cannot let a later message start first.
        await self._mark_turn(turn, MARK_RECEIVED)
        return f"dispatched:{trigger} conversation={key}"

    def _turn(
        self, inbound: InboundMessage, key: str, delivery: Mapping[str, Any], *, reply_optional: bool,
        event: InboundEvent | None = None,
    ) -> _Turn:
        """Build the turn of one message, or of the event ``inbound`` stands in for."""
        text = inbound.text.strip()
        metadata = self._hosted[inbound.platform].platform.request_metadata(inbound)
        metadata["belle_place"] = {
            "platform": inbound.platform,
            "workspace": inbound.workspace,
            "conversation": inbound.conversation,
            "thread": inbound.thread,
            "sender": inbound.sender,
            "chat_type": inbound.chat_type,
            "direct": inbound.trigger == TRIGGER_DM,
        }
        metadata["belle_inbound"] = {
            **{name: getattr(inbound, name) for name in
               ("message_id", "reply_thread", "envelope", "trigger")},
            "reply_optional": reply_optional,
        }
        if reply_optional:
            metadata[REPLY_OPTIONAL_KEY] = True
        request = TurnRequest(
            conversation=key,
            request_id=inbound.event_id or inbound.message_id or f"{inbound.platform}-{int(time.time() * 1000)}",
            text=f"{text}\n\n{inbound.envelope}".strip() if inbound.envelope else text,
            scope=self._turn_scope(inbound, delivery, reply_optional=reply_optional),
            files=inbound.files,
            metadata=metadata,
        )
        return _Turn(
            conversation=key,
            request=request,
            inbound=inbound,
            reply_optional=reply_optional,
            bound=BoundTurn(
                request_id=request.request_id,
                metadata=metadata,
                platform=inbound.platform,
                tools=self._bound_tools(request.scope.tools),
                content_types=self._backend.capabilities().content_for(request.scope.model),
            ),
            event=event,
        )

    # ── events ───────────────────────────────────────────────────────────

    async def _receive_event(self, event: InboundEvent) -> str:
        if not self._running:
            return "dropped:host-not-running"
        kind = self._hosted[event.platform].platform.event_kinds.get(event.kind)
        if kind is None:
            return "ignored:event-of-no-known-kind"
        if not event.actor:
            return "dropped:no-actor-on-event"
        if event.actor_is_bot:
            return "ignored:done-by-an-app"
        if not event.conversation:
            return "dropped:no-conversation-on-event"
        if not event.direct and not self._conversation_allowed(event.conversation, event.platform):
            return f"ignored:channel-not-in-{KEY_ALLOWED_CONVERSATIONS}"
        if not self._sender_allowed(event.actor, event.platform):
            return f"refused:actor-not-in-{KEY_ALLOW_FROM}"
        return await self._take_event(event, kind)

    def _occupied(self, key: str) -> bool:
        """Whether a new turn on ``key`` waits: a turn runs on it, here or in another process, or its deletion runs."""
        return self._live_turn(key) is not None or key in self._held_keys or key in self._deleting

    def _key_busy(self, key: str) -> bool:
        """Whether a turn runs on ``key`` in this process, or a message or an event waits for one."""
        admission = self._admission
        return bool(self._occupied(key) or key in admission.event_admissions
                    or admission.queued_messages.get(key) or admission.pending_events.get(key))

    def _start_event_turn(self, event: InboundEvent, key: str) -> tuple[str, str]:
        """Start the turn of an event: a turn like any other, of a message nobody sent.

        Return its request id, or the refusal of a turn whose scope cannot be met, which starts nothing.
        """
        # The one who did it stands where a sender would, and the message it was done to where theirs would.
        inbound = InboundMessage(
            platform=event.platform,
            workspace=event.workspace,
            conversation=event.conversation,
            message_id=event.anchor,
            sender=event.actor,
            text="",
            event_id=event.event_id,
            thread=event.anchor_thread,
            chat_type=event.chat_type,
            reply_thread=event.anchor_thread,
        )
        # An event starts a turn only where the reply is optional, so this one speaks through the posting tool.
        turn = self._turn(inbound, key, self._settle(inbound, SECTION_DELIVERY), reply_optional=True, event=event)
        if unmet := self._unmet_requirement(inbound, turn.request.scope):
            return "", unmet
        self._start(turn)
        return turn.request_id, ""

    async def _steer(self, running: _Turn, turn: _Turn) -> bool:
        """Add a message to the turn that runs in its conversation. Return whether the backend took it.

        A message joins only where its own turn would have the same scope, it carries no file, nothing waits
        before it, and its sender may stop the running turn. Any other message waits for a turn of its own.
        """
        key = turn.conversation
        if (
            turn.inbound.files
            or self._admission.queued_messages.get(key)
            or key in self._admission.event_admissions
            # The running turn's additions grew when it started. The rest of its scope is what admission settled.
            or replace(running.request.scope, system_additions=()) != replace(turn.request.scope, system_additions=())
            or not self._may_click(CLICK_STOP, running, turn.inbound.sender)[0]
            or not await self._backend.steer(turn.request)
        ):
            return False
        decisions = self._hosted[turn.inbound.platform].decisions
        if decisions is not None:
            decisions.queued(turn.inbound)
        async with running.marking:
            running.steered.append(turn)
            await self._mark(turn.inbound, MARK_STEERED)
        return True

    async def _hand_back(self, turn: _Turn, request_ids: Collection[str]) -> None:
        """Queue the steered messages the turn did not read as one turn, ahead of every message that waits.

        Each waited behind none, so they keep their order. The first message stands for the turn, and the rest end
        with it.
        """
        unread = [steered for steered in turn.steered if steered.request_id in request_ids]
        if not unread:
            return
        turn.steered = [steered for steered in turn.steered if steered.request_id not in request_ids]
        first, *rest = unread
        first.request = replace(first.request, text="\n\n".join(steered.request.text for steered in unread))
        first.steered, first.waited, first.queued_at = rest, True, time.monotonic()
        self._admission.queued_messages.setdefault(turn.conversation, []).insert(0, first)
        await self._mark_turn(first, MARK_QUEUED)

    async def _queue(self, turn: _Turn, trigger: str) -> str:
        self._admission.prune_messages(
            cutoff=time.monotonic() - QUEUED_MESSAGE_TIMEOUT_SECONDS,
            max_sessions=MAX_QUEUED_KEYS,
            warn=logger.warning,
        )
        if not self._admission.queue_message(turn.conversation, turn, max_per_session=MAX_QUEUED_PER_KEY, warn=logger.warning):
            inbound = turn.inbound
            await self._mark(inbound, MARK_REFUSED)
            platform = self._hosted[inbound.platform].platform
            # A notice that everybody in the conversation would see is no notice for its sender: the mark stands alone.
            if affordances(platform, workspace=inbound.workspace).private_aside != "absent":
                await self._tell_privately(inbound, inbound.sender, QUEUE_FULL_NOTICE)
            return f"refused:queue-full conversation={turn.conversation}"
        turn.waited = True
        decisions = self._hosted[turn.inbound.platform].decisions
        if decisions is not None:
            decisions.queued(turn.inbound)
        await self._mark_turn(turn, MARK_QUEUED)
        # Drain now in case the running turn closed while this message was admitted.
        self._drain(turn.conversation)
        return f"queued:{trigger} conversation={turn.conversation}"

    def _supports(self, operation: str) -> bool:
        return self._capabilities is not None and operation in self._capabilities.operations

    def _bound_tools(self, tools: Mapping[str, str]) -> dict[str, str]:
        """What the tool host enforces for a turn.

        A backend that applies the ``tools`` scope field realizes ``ask`` at its
        own layer (a permission the person decides on, held as the backend's own
        server state, not inside a tool call). The tool host then keeps only
        ``deny``, as defense in depth, and lets an approved call through. A
        backend that does not apply ``tools`` leaves the tool host the only gate,
        so it keeps every level.
        """
        if self._capabilities is not None and SCOPE_FIELD_TOOLS in self._capabilities.scope_fields:
            return {name: level for name, level in tools.items() if level == LEVEL_DENY}
        return dict(tools)

    # ── second brain ─────────────────────────────────────────────────────

    def _files_once(self, inbound: InboundMessage) -> _ReadFiles:
        """Return a reader of a message's files that asks its platform once, at the first call."""
        reading: asyncio.Task[tuple[Mapping[str, Any], ...]] | None = None

        async def read() -> tuple[Mapping[str, Any], ...]:
            nonlocal reading
            if reading is None:
                reading = asyncio.create_task(self._hosted[inbound.platform].platform.fetch_files(inbound))
                self._tasks.add(reading)
                reading.add_done_callback(self._tasks.discard)
            # A reader that is stopped, such as a turn, leaves the read to the other one.
            return await asyncio.shield(reading)

        return read

    def _dispatch_ingest(self, inbound: InboundMessage, read_files: _ReadFiles) -> bool:
        """Start ingesting what a message carries, where its conversation is selected for it."""
        brain = self._second_brain
        # A direct message is a request to the bot, not a document shared with a room.
        public = inbound.chat_type in self._hosted[inbound.platform].platform.public_chat_types
        if brain is None or inbound.trigger == TRIGGER_DM or not brain.ingests_from(
            inbound.conversation, public=public, encrypted=inbound.encrypted
        ):
            return False
        if not brain.carries_documents(inbound.text, inbound.files):
            return False
        task = asyncio.create_task(self._ingest(brain, inbound, read_files))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _ingest(self, brain: SecondBrain, inbound: InboundMessage, read_files: _ReadFiles) -> None:
        identity = inbound.message_id or inbound.event_id
        key = f"brain:{inbound.workspace}:{inbound.conversation}:{identity}"
        if identity and not await self._hosted[inbound.platform].dedup.remember(key):
            return
        await self._mark(inbound, MARK_INGESTING)
        # The corpus manifest names these fields after the first platform it was filled from.
        meta = {"slack_channel": inbound.conversation, "poster": inbound.sender, "comment": inbound.text.strip()}
        statuses: list[str] = []
        try:
            files = await read_files() if inbound.files else ()
            # A file the platform could not read is left out, and counts as a failure.
            statuses += [STATUS_FAILED] * (len(inbound.files) - len(files))
            for result in await brain.ingest(inbound.text, files, meta):
                logger.info(
                    "%s second brain: conversation=%s message=%s status=%s doc_id=%s reason=%s",
                    inbound.platform,
                    inbound.conversation,
                    inbound.message_id or "-",
                    result.get("status"),
                    result.get("doc_id") or "-",
                    result.get("reason") or "-",
                )
                statuses.append(str(result.get("status") or ""))
        except Exception:  # The message is still marked, and the next one is still ingested.
            logger.exception(
                "%s second brain: ingest failed: conversation=%s message=%s",
                inbound.platform, inbound.conversation, inbound.message_id or "-",
            )
            statuses.append(STATUS_FAILED)
        await self._mark(inbound, ingest_mark(statuses))

    async def _recall(self, turn: _Turn) -> None:
        """Add the documents that match a turn's message to what the turn is told."""
        if self._second_brain is None:
            return
        recalled = await self._second_brain.recall(turn.inbound.text)
        if recalled:
            scope = turn.request.scope
            turn.request = replace(
                turn.request, scope=replace(scope, system_additions=(*scope.system_additions, recalled))
            )

    # ── stop ─────────────────────────────────────────────────────────────

    async def _cancel(self, turn: _Turn) -> bool:
        """Ask the backend to stop a running turn. Return whether one was stopped."""
        if not self._supports(OP_CANCEL):
            return False
        # What the turn was running may get a tool call out before it notices. That call acts for nobody.
        turn.bound.stopped = True
        stopped = False
        try:
            stopped = await self._backend.cancel(turn.conversation)
        finally:
            turn.bound.stopped = stopped
        if stopped:
            message = turn.inbound.message_id or turn.request_id
            self._notes.add(MESSAGE, turn.conversation, STOPPED_NOTE.format(message=message))
        return stopped

    async def _receive_stop(self, stop: InboundStop) -> str:
        if not self._running:
            return "dropped:host-not-running"
        turn = next(
            (
                running
                for running in self._admission.turn_initiators.values()
                if (running.inbound.platform, running.inbound.workspace, running.inbound.conversation, running.inbound.message_id)
                == (stop.platform, stop.workspace, stop.conversation, stop.message_id)
            ),
            None,
        )
        if turn is None:
            return "ignored:no-turn-running"
        permitted, ground = self._may_click(CLICK_STOP, turn, stop.sender)
        if not permitted:
            return f"refused:{ground}"
        if not await self.stop_turn(turn):
            return "ignored:no-turn-running"
        return f"stopped conversation={turn.conversation}"

    # ── what the command operations read ─────────────────────────────────

    def _may_click(self, kind: str, turn: _Turn, sender: str, *, unruled: bool = True) -> tuple[bool, str]:
        """Whether a person may stop a turn or remove a waiting message, and on what ground.

        Whoever sent the message may. The allow list, then a clicks rule of
        that kind, decide for anyone else. ``unruled`` is the answer where no
        such rule is written.
        """
        if sender and sender == turn.inbound.sender:
            return True, "sender"
        if not self._sender_allowed(sender, turn.inbound.platform):
            return False, KEY_ALLOW_FROM
        rule = click_rule(self._scopes, channel=turn.inbound.platform, chat=turn.inbound.conversation, kind=kind)
        if rule is None:
            return unruled, KEY_ALLOW_FROM if unruled else f"clicks.{kind}"
        return rule.permits(sender), f"clicks.{kind}={rule.describe()}"

    def _live_turn(self, key: str) -> _Turn | None:
        return self._admission.turn_initiator(key, timeout=TURN_TIMEOUT_SECONDS)

    # ── the turn ─────────────────────────────────────────────────────────

    def _start(self, turn: _Turn) -> asyncio.Task[None]:
        turn.started_at = time.monotonic()
        # A recovered turn keeps the start of the stopped process's turn.
        turn.started_utc = turn.started_utc or utc_now()
        self._active_work[turn.status_id] = turn
        self._admission.remember_turn(
            turn.conversation, turn, cap=MAX_TRACKED_TURNS, timeout=TURN_TIMEOUT_SECONDS, warn=logger.warning
        )
        task = asyncio.create_task(self._run(turn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # A task cancelled before its first step never enters _run finally.
        task.add_done_callback(lambda _: self._forget_active_work(turn.status_id))
        return task

    def _forget_active_work(self, work_id: str) -> None:
        self._active_work.pop(work_id, None)
        task = self._peek_seeding.get(work_id)
        if task is not None and not task.done():
            # History still loading for an opener; keep reservation until finish/abandon.
            self._peeks.mark_ended(work_id)
            return
        self._peek_open_waiters.pop(work_id, None)
        self._peek_seeding.pop(work_id, None)
        self._peeks.mark_ended(work_id)
        # Drop a reserved buffer that never got a subscriber (cancelled open).
        self._peeks.discard_if_unsubscribed(work_id)

    def _remember_recovered(self, turn: _Turn) -> None:
        turn.started_at = time.monotonic()
        # A recovered turn keeps the start of the stopped process's turn.
        turn.started_utc = turn.started_utc or utc_now()
        self._active_work[turn.status_id] = turn
        self._admission.remember_turn(
            turn.conversation, turn, cap=MAX_TRACKED_TURNS, timeout=TURN_TIMEOUT_SECONDS, warn=logger.warning
        )

    def _drain(self, key: str) -> None:
        if not self._running or key in self._admission.event_admissions or self._occupied(key):
            return
        # A message that waits runs before an event that waits.
        waiting = self._admission.take_message(key)
        if waiting is not None:
            self._start(waiting)
        else:
            self._drain_events(key)

    async def _run(self, turn: _Turn) -> None:
        key = turn.conversation
        outcome = OUTCOME_FAILED
        cancelled = False
        terminal: TurnEvent | None = None
        self._tools.bind(key, turn.bound)
        chat = turn.session_run is None and self._attached_sessions is not None
        try:
            if turn.recovered:
                await self._platforms_started.wait()
                terminal = turn.lost or await self._await_recovery(turn, partial(datetime.now, UTC))
                if terminal is None:
                    raise asyncio.CancelledError
            else:
                if chat:
                    await self._record_running(turn)
                terminal = await self._prepared_terminal(turn, key)
            await self._hand_back(turn, terminal.data.get(NOT_STEERED) or ())
            if chat:
                await self._record_delivering(turn)
            outcome = await self._deliver(turn, terminal)
        except asyncio.CancelledError:
            outcome = OUTCOME_INTERRUPTED
            cancelled = True
            raise
        except Exception:
            logger.exception("%s turn could not be delivered: conversation=%s", turn.inbound.platform, key)
        finally:
            self._tools.release(key, turn.request_id)
            scheduled = turn.session_run is not None
            turn.session_run_ended = scheduled
            for question_id in [name for name, waiting in self._questions.items() if waiting is turn]:
                del self._questions[question_id]
                await self._withdraw_question(turn, question_id[1])
                if scheduled:
                    self._remember_expired(question_id, turn)
                self._cancel_question_timer(question_id)
            await self._expire_held_at_turn_end(turn)
            self._forget_active_work(turn.status_id)
            self._admission.forget_turn(key, turn.request_id)
            self.ended.append(TurnRecord(conversation=key, request_id=turn.request_id, outcome=outcome))
            logger.info(
                "%s turn ended: conversation=%s request_id=%s outcome=%s",
                turn.inbound.platform,
                key,
                turn.request_id,
                outcome,
            )
            marker = turn.session_run
            if marker is not None and self.scheduler is not None and self._running:
                end = RunEnd(
                    outcome=outcome,
                    final=terminal.text if terminal is not None else "",
                    posted=[
                        {"platform": turn.inbound.platform, "workspace": turn.inbound.workspace, **record}
                        for record in turn.bound.posted
                    ],
                    error=terminal.text if terminal is not None and terminal.kind == EVENT_FAILED else "",
                    turn=marker.turn,
                    ended_iso_utc=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                )
                try:
                    await self.scheduler.ended_run(marker.session_id, marker.run_id, end)
                    await self.report_run(marker.session_id, marker.run_id)
                except Exception:
                    logger.exception("scheduler could not record the end of run %s", marker.run_id)
            if chat and self._running:
                # A stopping host keeps the record, so that the next host recovers the turn or stops it.
                await self._record_ended(turn)
            self._drain(key)
            if not cancelled and not _delivers_by_tool(turn):
                # The host is not stopping, so the platform still shows what became of the message.
                await self._conclude(turn, ENDED_SILENT if outcome == OUTCOME_WITHHELD else _END_MARKS[outcome])
                await self._mark_turn(turn, _END_MARKS[outcome])
                async with turn.marking:
                    for steered in turn.steered:
                        await self._mark(steered.inbound, _END_MARKS[outcome])

    async def _prepared_terminal(self, turn: _Turn, key: str) -> TurnEvent:
        """Complete the turn's request as it starts, then run it to its terminal event."""
        if turn.waited:
            await self._mark_turn(turn, MARK_STARTED)
        await self._begin_showing(turn)
        # Content a platform holds by reference is read now that the message has its turn.
        if turn.inbound.files:
            read_files = turn.read_files or self._files_once(turn.inbound)
            turn.request = replace(turn.request, files=await read_files())
        # Recalled as the turn starts, not when its message arrived: a message that waited
        # in the queue is matched against the corpus as it is now, and keeps its place.
        await self._recall(turn)
        taken = self._notes.take(key)
        if notes := taken[SYSTEM_ADDITIONS]:
            scope = turn.request.scope
            turn.request = replace(
                turn.request, scope=replace(scope, system_additions=(*scope.system_additions, *notes))
            )
        # What the conversation kept is told to the turn that runs, not to a message that may never have one.
        told = "\n\n".join(
            part
            for part in (
                *taken[MESSAGE],
                self._events_text(turn.inbound.platform, turn.inbound.workspace, turn.inbound.conversation, turn.event),
            )
            if part
        )
        # A message that waited was put to the gate when it arrived, and an event is put to it never.
        decisions = self._hosted[turn.inbound.platform].decisions
        if decisions is not None and turn.event is None and turn.session_run is None and not turn.waited:
            told = "\n\n".join(part for part in (told, await decisions.advice(turn.inbound)) if part)
        if told:
            turn.request = replace(turn.request, text=f"{turn.request.text}\n\n{told}".strip())
        if turn.event is not None or turn.inbound.text.strip() or turn.request.files:
            return await self._terminal_event(turn)
        # The message was a file and nothing of it could be read. The platform has said so.
        return TurnEvent(EVENT_FAILED)

    # ── chat turns that outlive the host ─────────────────────────────────

    async def _record_running(self, turn: _Turn) -> None:
        """Record a running chat turn, with what a restarted host needs to build it again."""
        assert self._attached_sessions is not None and turn.started_utc is not None
        inbound = None if turn.event is not None else RecordedMessage.of(turn.inbound, reply_optional=turn.reply_optional)
        running = RunningTurn(turn.request_id, inbound, turn.started_utc, self._owner, False)
        try:
            recorded = await self._attached_sessions.started(turn.conversation, self.backend_capabilities.backend,
                                                             running, datetime.now(UTC))
        except Exception:  # The record serves a restart, and the turn runs without it.
            logger.exception("%s could not record the running turn: conversation=%s", self._label, turn.conversation)
            return
        if not recorded:
            logger.warning("%s runs a turn with no record: the record holds a turn that another process runs or left."
                           " A restart cannot recover this turn: conversation=%s", self._label, turn.conversation)

    async def _record_delivering(self, turn: _Turn) -> None:
        assert self._attached_sessions is not None
        try:
            await self._attached_sessions.delivering(turn.conversation, turn.request_id)
        except Exception:  # See _record_running.
            logger.exception("%s could not record a delivery: conversation=%s", self._label, turn.conversation)

    async def _record_ended(self, turn: _Turn) -> None:
        assert self._attached_sessions is not None
        try:
            await self._attached_sessions.ended(turn.conversation, turn.request_id, utc_now(),
                                            keep=self._idle_hours is not None)
        except Exception:  # See _record_running.
            logger.exception("%s could not record the end of a turn: conversation=%s", self._label, turn.conversation)

    async def _recover_chat_turns(self) -> None:
        """Deliver the answer of each chat turn that outlived the process that started it, or stop the turn.

        The host runs this at start and with each heartbeat. An owner on another host stays live until its heartbeat
        is 90 s old, so its turns wait for a later pass. A message on the key of such a turn waits for it. A turn that
        the pass cannot settle goes back to its owner, and the next pass tries again.
        """
        store = self._attached_sessions
        assert store is not None
        taken, elsewhere = await store.take_orphans(self.backend_capabilities.backend, self._owner, datetime.now(UTC))
        released = self._held_keys - elsewhere
        self._held_keys = elsewhere | {key for key, _ in taken}
        for key, running in taken:
            try:
                await self._recover_chat_turn(key, running)
            except Exception:  # The other turns still recover.
                logger.exception("%s could not recover a turn of a stopped process. The next pass tries again:"
                                 " conversation=%s request_id=%s", self._label, key, running.request_id)
                try:
                    await store.give_back(key, running, self._owner)
                except Exception:  # The turn stays held here until this process stops.
                    logger.exception("%s could not give back a turn: conversation=%s", self._label, key)
                continue
            self._held_keys.discard(key)
            released.add(key)
        for key in released:
            self._drain(key)

    async def _recover_chat_turn(self, key: str, running: RunningTurn) -> None:
        """Deliver one taken turn through a recovered turn, or tell its message that the answer is lost."""
        assert self._attached_sessions is not None
        inbound = running.inbound
        hosted = inbound is not None and inbound.platform in self._hosted
        if running.delivering:
            logger.warning("%s found a turn whose answer was being delivered when Belle stopped. Belle does"
                           " not deliver it again: conversation=%s request_id=%s", self._label, key,
                           running.request_id)
            notice = RESTART_DELIVERING_NOTICE
        elif self._supports(OP_RECOVER) and hosted:
            self._start(self._recovered_turn(key, running))
            return
        elif self._supports(OP_ACTIVE) and key not in await self._backend.active():
            logger.warning("%s found a turn that ended with the process that started it. Belle cannot deliver"
                           " its answer: conversation=%s request_id=%s", self._label, key, running.request_id)
            notice = RESTART_LOST_NOTICE
        else:
            stopped = self._supports(OP_CANCEL) and await self._backend.cancel(key)
            logger.warning("%s found a turn running from before this start and %s: conversation=%s",
                           self._label, "stopped it" if stopped else "could not stop it", key)
            notice = RESTART_LOST_NOTICE
        if hosted:
            # The message still shows the turn as running. The turn ends as failed once the platforms start.
            turn = self._recovered_turn(key, running)
            turn.lost = TurnEvent(EVENT_FAILED, text=notice)
            self._start(turn)
            return
        await self._attached_sessions.ended(key, running.request_id, utc_now(), keep=self._idle_hours is not None)

    def _recovered_turn(self, key: str, running: RunningTurn) -> _Turn:
        """Build again the turn of a stopped process, from the message that started it."""
        assert running.inbound is not None
        message = running.inbound.message(running.request_id)
        turn = self._turn(message, key, self._settle(message, SECTION_DELIVERY), reply_optional=running.inbound.reply_optional)
        turn.recovered = True
        # The turn timeout counts from the turn's start in the stopped process.
        turn.started_utc = running.started_iso_utc
        return turn

    def _loop_ended(self, task: asyncio.Task[None]) -> None:
        """Log a heartbeat loop that ended with an error: other processes then take this one's turns."""
        if not task.cancelled() and task.exception() is not None:
            logger.error("%s stopped its heartbeat", self._label, exc_info=task.exception())

    async def _attached_sessions_loop(self) -> None:
        # The start recorded the heartbeat and took the turns of stopped processes.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self._attached_sessions_pass()

    async def _attached_sessions_pass(self) -> None:
        """Record the heartbeat, recover the turns of stopped processes, and delete idle attached sessions when due.

        An error in one part is logged, and the other parts run.
        """
        assert self._attached_sessions is not None
        try:
            await self._attached_sessions.beat(self._owner, datetime.now(UTC))
        except Exception:  # A missed heartbeat lets another process take this one's turns.
            logger.exception("%s could not record its heartbeat in %s", self._label, self._attached_sessions.path)
        try:
            await self._recover_chat_turns()
        except Exception:  # The next pass tries again.
            logger.exception("%s could not recover the turns of a stopped process", self._label)
        if self._idle_hours is not None and time.monotonic() - self._idle_expired_at >= EXPIRY_INTERVAL.total_seconds():
            self._idle_expired_at = time.monotonic()
            try:
                await self.expire_attached_sessions()
            except Exception:  # The next pass tries again.
                logger.exception("%s could not delete idle attached sessions", self._label)

    async def expire_attached_sessions(self, now: datetime | None = None) -> None:
        """Delete each idle attached session on the harness, and then its record. A failure waits for a retry. One pass runs at a time."""
        assert self._attached_sessions is not None and self._idle_hours is not None
        now = now or datetime.now(UTC)
        store, deletions = self._attached_sessions, self._idle_deletions
        async with self._idle_pass:
            for record, claimed in await store.claim_idle(self._owner, now, self._idle_hours, deletions.blocked):
                if self._key_busy(record.key):
                    # A turn started after the claim, or a message waits for one: the attached session is active.
                    await store.release(record, self._owner)
                    continue
                # A turn must not run in a conversation that the harness deletes, so a message waits until the end.
                self._deleting.add(record.key)
                try:
                    await deletions.delete(record.key, claimed,
                                           partial(delete_attached_session, self._backend, self.backend_capabilities,
                                                   record),
                                           partial(store.settle, record, self._owner, now))
                finally:
                    self._deleting.discard(record.key)
                    self._drain(record.key)

    async def _terminal_event(self, turn: _Turn) -> TurnEvent:
        """Wait for a turn's terminal event, for as long as a turn may take.

        A turn that outlives ``TURN_TIMEOUT_SECONDS`` is left, which stops it:
        the bookkeeping forgets a turn at that age, and the next message would
        otherwise start beside it.
        """
        reading = asyncio.create_task(self._read(turn))
        turn.reading = reading
        try:
            done, _ = await asyncio.wait({reading}, timeout=TURN_TIMEOUT_SECONDS)
        finally:
            if not reading.done():
                # Whatever ended the wait, a tool call the turn still gets out acts for nobody.
                turn.bound.stopped = True
                turn.peek_cancel_as_failed = True
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
        if turn.question_error:
            failed = TurnEvent(EVENT_FAILED, text=turn.question_error)
            self._peek_outcome(turn, failed.kind, extra=_peek_extra(failed))
            return failed
        if done:
            return reading.result()
        logger.warning(
            "%s turn reached no end in %ss and was stopped: conversation=%s request_id=%s",
            turn.inbound.platform,
            TURN_TIMEOUT_SECONDS,
            turn.conversation,
            turn.request_id,
        )
        failed = TurnEvent(EVENT_FAILED, text=TURN_TIMEOUT_NOTICE)
        self._peek_outcome(turn, failed.kind, extra=_peek_extra(failed))
        return failed

    async def _read(self, turn: _Turn) -> TurnEvent:
        """Read a turn's events up to its terminal one, relaying questions and keeping its budget."""
        steps = 0
        try:
            # Leaving the stream stops the turn, so it is closed here and not when it is collected.
            async with contextlib.aclosing(self._backend.run_turn(turn.request)) as events:
                async for event in events:
                    if event.terminal:
                        self._peek_outcome(turn, event.kind, extra=_peek_extra(event))
                        return event
                    self._track_subagent(turn, event)
                    if event.kind == EVENT_QUESTION and event.question is not None:
                        await self._ask(turn, event.question)
                    await self._show(turn, event)
                    try:
                        self._peek_event(turn, event)
                    except Exception as exc:  # noqa: BLE001 - tee must not fail the turn.
                        code = getattr(exc, 'code', type(exc).__name__)
                        logger.warning(
                            "%s peek tee skipped %s: conversation=%s code=%s",
                            turn.inbound.platform, event.kind, turn.conversation, code,
                        )
                    steps += event.kind == EVENT_STEP
                    cost = event.data.get("cost") if event.kind == EVENT_USAGE else None
                    spent = isinstance(cost, (int, float)) and cost > MAX_TURN_COST
                    if steps > MAX_TURN_STEPS or spent:
                        turn.bound.stopped = True
                        logger.warning(
                            "%s turn went over its budget and was stopped: conversation=%s request_id=%s steps=%s cost=%s",
                            turn.inbound.platform,
                            turn.conversation,
                            turn.request_id,
                            steps,
                            cost,
                        )
                        failed = TurnEvent(EVENT_FAILED, text=TURN_COST_NOTICE if spent else TURN_STEPS_NOTICE)
                        self._peek_outcome(turn, failed.kind, extra=_peek_extra(failed))
                        return failed
        except asyncio.CancelledError:
            if not turn.peek_cancel_as_failed:
                self._peek_outcome(turn, EVENT_INTERRUPTED)
            raise
        except _QuestionCannotReject:
            failed = TurnEvent(EVENT_FAILED, text="The backend cannot be told no.")
            self._peek_outcome(turn, failed.kind, extra=_peek_extra(failed))
            return failed
        except _QuestionNotShown as exc:
            failed = TurnEvent(EVENT_FAILED, text=f"The turn asked a question that cannot be shown here: {exc}.")
            self._peek_outcome(turn, failed.kind, extra=_peek_extra(failed))
            return failed
        except Exception as exc:  # noqa: BLE001 - a backend failure ends one turn, not the host.
            logger.error(
                "%s backend raised during a turn: conversation=%s exception_type=%s",
                turn.inbound.platform, turn.conversation, type(exc).__name__,
            )
            failed = TurnEvent(
                EVENT_FAILED, text=f"The turn failed: {type(exc).__name__}", data={"failure_kind": "backend"}
            )
            self._peek_outcome(turn, failed.kind, extra=_peek_extra(failed))
            return failed
        failed = TurnEvent(EVENT_FAILED, text="The turn ended without a result.")
        self._peek_outcome(turn, failed.kind, extra=_peek_extra(failed))
        return failed

    def _peek_event(self, turn: _Turn, event: TurnEvent) -> None:
        """Tee one non-terminal turn event when a peek buffer exists for the turn."""
        work_id = turn.status_id
        source = self._peek_source_work_id(turn, event)
        if event.kind == EVENT_TEXT_DELTA and event.text:
            kind = PEEK_KIND_THINKING if event.data.get("thinking") else PEEK_KIND_TEXT
            self._peeks.observe_text(work_id, event.text, kind=kind, source_work_id=source)
            return
        if event.kind == EVENT_STEP:
            # Unlabeled steps are silent in peek: bare ── lines add noise when tools/usage
            # already structure the stream. Show a break only when the harness names it.
            label = event.data.get("label")
            if isinstance(label, str) and label.strip() and all(ord(char) >= 32 for char in label):
                self._peeks.observe_step(work_id, label.strip(), source_work_id=source)
            return
        if event.kind == EVENT_TOOL_CALLED:
            fields = peek_tool_called_fields(event)
            if fields is None:
                return
            call_id, name, detail = fields
            args_text = peek_tool_args_text(event)
            self._peek_tool_call(
                work_id, call_id, name, detail=detail or None, args_text=args_text,
                source_work_id=source,
            )
            return
        if event.kind == EVENT_TOOL_RESULT:
            fields = peek_tool_result_fields(event)
            if fields is None:
                return
            call_id, name, ok, summary = fields
            result_text = peek_tool_result_text(event)
            self._peek_tool_result(
                work_id, call_id, ok=ok, name=name, summary=summary, result_text=result_text,
                source_work_id=source,
            )
            return
        if event.kind == EVENT_QUESTION and event.question is not None:
            self._peeks.observe_question(
                work_id, peek_question_text(event.question.text), event.question.kind,
                source_work_id=source,
            )
            return
        if event.kind == EVENT_USAGE:
            tokens_in, tokens_out, cost = peek_usage_fields(event.data)
            # Work-row tokens come only from turn usage the backend reports — not session invents.
            if tokens_in is not None:
                turn.tokens_in = tokens_in
            if tokens_out is not None:
                turn.tokens_out = tokens_out
            self._peeks.observe_usage(
                work_id, tokens_in=tokens_in, tokens_out=tokens_out, cost=cost,
                source_work_id=source,
            )

    def _peek_with_optional_body(self, observe, body: str | None) -> None:
        """Call ``observe(body)``; retry with ``None`` when the body alone makes the frame too large."""
        try:
            observe(body)
        except ControlError as exc:
            if exc.code != 'message_too_large' or body is None:
                raise
            observe(None)

    def _peek_tool_call(
        self, work_id: str, call_id: str, name: str, *, detail: str | None,
        args_text: str | None, source_work_id: str,
    ) -> None:
        """Tee a tool_call frame; drop the optional body if the frame exceeds peek_max_bytes."""
        self._peek_with_optional_body(
            lambda body: self._peeks.observe_tool_call(
                work_id, call_id, name, detail=detail, args_text=body,
                source_work_id=source_work_id,
            ),
            args_text,
        )

    def _peek_tool_result(
        self, work_id: str, call_id: str, *, ok: bool, name: str | None, summary: str | None,
        result_text: str | None, source_work_id: str,
    ) -> None:
        """Tee a tool_result frame; drop the optional body if the frame exceeds peek_max_bytes."""
        self._peek_with_optional_body(
            lambda body: self._peeks.observe_tool_result(
                work_id, call_id, ok=ok, name=name, summary=summary, result_text=body,
                source_work_id=source_work_id,
            ),
            result_text,
        )

    def _peek_outcome(self, turn: _Turn, state: str, *, extra: str | None = None) -> None:
        """Stash a terminal peek frame; apply now when a buffer already exists."""
        if turn.peek_outcome is not None:
            return
        duration = max(0.0, time.monotonic() - turn.started_at) if turn.started_at else None
        turn.peek_outcome = (state, duration, utc_now(), extra)
        if self._peeks.has_buffer(turn.status_id):
            self._apply_stashed_peek_outcome(turn)

    def _apply_stashed_peek_outcome(self, turn: _Turn) -> None:
        """Write ``turn.peek_outcome`` into the ring; retry without ``extra`` if too large."""
        stashed = turn.peek_outcome
        if stashed is None or not self._peeks.has_buffer(turn.status_id):
            return
        state, duration, ended_at, extra = stashed
        try:
            self._peek_with_optional_body(
                lambda body: self._peeks.observe_outcome(
                    turn.status_id, state, duration_seconds=duration, ended_at=ended_at, extra=body,
                ),
                extra,
            )
        except Exception as exc:  # noqa: BLE001 - tee must not fail the turn.
            code = getattr(exc, 'code', type(exc).__name__)
            logger.warning(
                "%s peek outcome skipped: conversation=%s code=%s",
                turn.inbound.platform, turn.conversation, code,
            )
            self._peeks.mark_ended(turn.status_id)

    @staticmethod
    def _peek_seed_part(turn: _Turn, part: PeekHistoryPart) -> PeekHistoryPart:
        """Attribute harness catch-up text that names a parent subagent call id."""
        if not part.call_id or part.call_id not in turn.subagents:
            return part
        return PeekHistoryPart(
            kind=part.kind,
            text=part.text,
            call_id=part.call_id,
            source_work_id=turn.subagents[part.call_id].id,
        )

    @staticmethod
    def _peek_source_work_id(turn: _Turn, event: TurnEvent) -> str:
        """Activity id for ``source_work_id``: subagent child when tracked, else the main turn."""
        data = event.data
        if event.kind == EVENT_SUBAGENT_UPDATE:
            key = data.get('id')
        else:
            key = data.get('call_id')
        if isinstance(key, str) and key in turn.subagents:
            return turn.subagents[key].id
        return turn.status_id

    @staticmethod
    def _track_subagent(turn: _Turn, event: TurnEvent) -> None:
        """Keep backend-reported subagent state until its parent turn ends."""
        data = event.data
        source = data.get('id') if event.kind == EVENT_SUBAGENT_UPDATE else data.get('call_id')
        if not isinstance(source, str) or not source:
            return
        if event.kind == EVENT_SUBAGENT_UPDATE:
            state = data.get('state')
            if state not in {'running', 'completed', 'failed', 'cancelled', 'unreported'}:
                return
            name = data.get('name')
        elif event.kind == EVENT_TOOL_CALLED and SUBAGENT in data:
            state, name = 'running', data[SUBAGENT]
        elif event.kind == EVENT_TOOL_RESULT and source in turn.subagents and not data.get('child'):
            # Tools routed from a child session reuse the parent call_id and set ``child``.
            # Only the subagent tool's own result ends the child.
            state, name = ('failed' if tool_result_failed(event) else 'completed'), None
        else:
            return
        agent = name if isinstance(name, str) and name and all(ord(char) >= 32 for char in name) else None
        child = turn.subagents.get(source)
        if child is None:
            child = turn.subagents[source] = _SubagentWork(uuid4().hex, agent, state,
                                                          utc_now() if state == 'running' else None)
        else:
            child.state = state
            if agent is not None:
                child.agent = agent
            if state == 'running' and child.started_utc is None:
                child.started_utc = utc_now()

    async def _deliver(self, turn: _Turn, terminal: TurnEvent) -> str:
        """Post what the turn owes its conversation, and say how it ended."""
        inbound = turn.inbound
        if terminal.kind == EVENT_INTERRUPTED:
            return OUTCOME_INTERRUPTED
        if terminal.kind == EVENT_FAILED and terminal.data.get(DUPLICATE):
            # The turn of this request id ran before. What it owed was delivered then.
            return OUTCOME_DUPLICATE
        if terminal.kind == EVENT_FAILED:
            kind, status, error_type, backend_session = _failure_fields(terminal)
            logger.warning(
                "%s turn failed: conversation=%s request_id=%s backend=%s failure_kind=%s status=%s error_type=%s"
                " backend_session=%s",
                inbound.platform, turn.conversation, turn.request_id, self._capabilities.backend, kind, status,
                error_type, backend_session,
            )
        if _delivers_by_tool(turn):
            if terminal.kind == EVENT_FAILED:
                return OUTCOME_FAILED
            return OUTCOME_POSTED_BY_TOOL if turn.bound.delivered else OUTCOME_STORED_FINAL
        if terminal.kind == EVENT_FAILED:
            if not turn.reply_optional and terminal.text.strip():
                # A reply still on screen is taken down before the reader is told why none follows.
                await self._conclude(turn, MARK_FAILED)
                if turn.session_run is not None:
                    await self._post_scheduled(turn, terminal.text.strip())
                else:
                    await self._post(inbound, terminal.text.strip())
            return OUTCOME_FAILED
        if _answered_here(turn):
            return OUTCOME_POSTED_BY_TOOL
        # A post in another conversation is not the answer this one is owed, so the final is still posted here.
        unanswered = OUTCOME_POSTED_BY_TOOL if turn.bound.delivered else OUTCOME_WITHHELD
        if turn.session_run is not None:
            final = terminal.text.strip()
            if not final or is_no_reply_sentinel(final):
                return unanswered
            await self._post_scheduled(turn, final)
            return OUTCOME_POSTED_FINAL
        if turn.reply_optional:
            # Under an optional reply the posting tool is the only way to speak.
            return unanswered
        final = terminal.text.strip()
        if not final and turn.bound.delivered:
            # Failing the message would invite a retry, and with it a second post elsewhere.
            logger.warning(
                "%s turn posted elsewhere and gave no reply here: conversation=%s request_id=%s",
                inbound.platform,
                turn.conversation,
                turn.request_id,
            )
            return OUTCOME_POSTED_BY_TOOL
        if not final:
            logger.warning(
                "%s turn owed a reply and produced none: conversation=%s request_id=%s",
                inbound.platform,
                turn.conversation,
                turn.request_id,
            )
            return OUTCOME_FAILED
        # A platform that showed the reply as it was written ends it as the final. Any other is handed the final whole.
        delivered = await self._conclude(turn, MARK_DONE, final)
        if delivered is None:
            return OUTCOME_FAILED
        if not delivered:
            await self._post(inbound, final)
        return OUTCOME_POSTED_FINAL

    # ── what a running turn shows ────────────────────────────────────────

    @staticmethod
    def _settle_shown(platform: Platform, capabilities: BackendCapabilities) -> frozenset[str]:
        """Name each setting of a platform that shows a kind of event this backend does not report."""
        shows = getattr(platform, "shows", None)
        shows = shows if isinstance(shows, Mapping) else {}
        unfed: dict[str, list[str]] = {}
        for kind, setting in sorted(shows.items()):
            if kind not in capabilities.reports:
                unfed.setdefault(setting, []).append(kind)
        for setting, kinds in unfed.items():
            logger.warning(
                "%s is set for %s but backend %s reports no %s event. That setting shows nothing",
                setting,
                platform.name,
                capabilities.backend,
                " or ".join(kinds),
            )
        return frozenset(shows) & capabilities.reports

    async def _begin_showing(self, turn: _Turn) -> None:
        """Tell the platform that a turn owed a reply has started, so its card can offer a stop before any tool runs."""
        hosted = self._hosted[turn.inbound.platform]
        begin = getattr(hosted.platform, "begin", None)
        # An optional reply is posted only through a tool, whose call brings up the card; see ``ShowsTurns.begin``.
        if begin is None or not hosted.shown or turn.reply_optional or turn.event is not None or turn.session_run is not None:
            return
        try:
            await begin(turn.inbound)
        except Exception:  # What a turn shows is a courtesy, and no turn depends on it.
            logger.warning(
                "%s could not start showing a turn: conversation=%s message=%s",
                hosted.name, turn.inbound.conversation, turn.inbound.message_id or "-", exc_info=True,
            )

    async def _show(self, turn: _Turn, event: TurnEvent) -> None:
        """Hand the platform one event of a running turn, where it shows that kind. A failure changes nothing else."""
        # The turn of an event shows nothing: nobody is waiting on it.
        hosted = self._hosted[turn.inbound.platform]
        if event.kind not in hosted.shown or turn.event is not None or turn.session_run is not None:
            return
        # Thinking and child-session drafts feed peeks only; they are not answer text.
        if event.kind == EVENT_TEXT_DELTA and (event.data.get("thinking") or event.data.get("child")):
            return
        # A reply is shown as it is written only while its final is what will be posted. Under an optional
        # reply the final is withheld, and it is withheld too once the posting tool has posted in this conversation.
        if event.kind == EVENT_TEXT_DELTA and (turn.reply_optional or _answered_here(turn)):
            return
        events = [event]
        if (
            event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT) and turn.reply_optional
            and affordances(hosted.platform, workspace=turn.inbound.workspace).cards_wait_for_a_post
        ):
            # A turn that may stay silent shows its card only once it has posted here, with every tool event so far.
            turn.unshown.append(event)
            if not _answered_here(turn):
                return
            events, turn.unshown = turn.unshown, []
        for shown in events:
            try:
                await hosted.platform.progress(turn.inbound, shown)  # type: ignore[attr-defined]
            except Exception:  # What a turn shows is a courtesy, and no turn depends on it.
                logger.warning(
                    "%s could not show a %s event: conversation=%s message=%s",
                    hosted.name, shown.kind, turn.inbound.conversation, turn.inbound.message_id or "-", exc_info=True,
                )

    async def _conclude(self, turn: _Turn, state: str, final: str = "") -> bool | None:
        """End the surface once. Return delivery, or None when delivery is uncertain."""
        hosted = self._hosted[turn.inbound.platform]
        if turn.concluded or not hosted.shown or turn.event is not None or turn.session_run is not None:
            return False
        turn.concluded = True
        try:
            return bool(await hosted.platform.conclude(turn.inbound, state, final))  # type: ignore[attr-defined]
        except DeliveryUncertain:
            logger.warning(
                "%s reply delivery is uncertain; automatic final posting refused: conversation=%s message=%s",
                hosted.name, turn.inbound.conversation, turn.inbound.message_id or "-",
            )
            return None
        except Exception:  # The final is then posted whole, as it is on a platform that shows nothing.
            logger.warning(
                "%s could not end what it showed of a turn: conversation=%s message=%s",
                hosted.name, turn.inbound.conversation, turn.inbound.message_id or "-", exc_info=True,
            )
            return False

    # ── questions ────────────────────────────────────────────────────────

    def _question_timeout(self) -> int:
        scheduler = getattr(self, "scheduler", None)
        return scheduler.settings.question_timeout_seconds if scheduler is not None else 900

    def _remember_expired(self, key: tuple[str, str], turn: _Turn) -> None:
        now = self.clock()
        for expired, (_, deadline) in tuple(self._expired_questions.items()):
            if deadline <= now:
                del self._expired_questions[expired]
        self._expired_questions[key] = (turn, now + self._question_timeout())

    def _cancel_question_timer(self, key: tuple[str, str]) -> None:
        task = self._question_timeouts.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _cancel_held_timer(self, question_id: str) -> None:
        task = self._held_timeouts.pop(question_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _withdraw_question(self, turn: _Turn, question_id: str) -> None:
        platform = self._hosted[turn.inbound.platform].platform
        withdraw = getattr(platform, "withdraw_question", None)
        if withdraw is None:
            return
        try:
            await withdraw(question_id, workspace=turn.inbound.workspace)
        except Exception:
            logger.warning(
                "%s could not remove controls for question %s", turn.inbound.platform, question_id, exc_info=True
            )

    async def _expire_held_at_turn_end(self, turn: _Turn) -> None:
        for question_id, held in tuple(self._held.items()):
            if held.turn is turn and (
                turn.session_run is not None or self._tools.requires_active_turn(turn.bound.platform, held.call.name)
            ):
                del self._held[question_id]
                self._remember_expired((turn.inbound.platform, question_id), turn)
                self._cancel_held_timer(question_id)
                await self._withdraw_question(turn, question_id)

    async def _expire_backend_question(self, key: tuple[str, str], turn: _Turn, question_id: str) -> None:
        await asyncio.sleep(self._question_timeout())
        if self._questions.get(key) is not turn:
            return
        del self._questions[key]
        self._remember_expired(key, turn)
        self._question_timeouts.pop(key, None)
        await self._withdraw_question(turn, question_id)
        try:
            await self._backend.answer(turn.conversation, question_id, Answer(values=("reject",)))
        except Exception:
            logger.exception("%s could not reject expired scheduled question: conversation=%s", turn.inbound.platform, turn.conversation)
            turn.question_error = "The backend cannot be told no."
            reading = turn.reading
            if reading is not None and not reading.done():
                reading.cancel()

    async def _expire_held_call(self, question_id: str, held: _HeldCall) -> None:
        assert held.deadline is not None
        await asyncio.sleep(max(0.0, held.deadline - self.clock()))
        if self._held.get(question_id) is held:
            del self._held[question_id]
            self._remember_expired((held.turn.inbound.platform, question_id), held.turn)
            await self._withdraw_question(held.turn, question_id)
        self._held_timeouts.pop(question_id, None)

    async def _ask(self, turn: _Turn, question: Question) -> None:
        asked = (turn.inbound.platform, question.id)
        schedule = turn.session_run
        if schedule is not None:
            if not self._supports(OP_ANSWER):
                raise _QuestionCannotReject
            if schedule.questions == "refuse":
                await self._backend.answer(turn.conversation, question.id, Answer(values=("reject",)))
                return
        if self._questions.get(asked) is turn:
            # A backend re-yields its open questions after a reconnect, so the platform is not asked twice.
            return
        while len(self._questions) >= MAX_PENDING_QUESTIONS:
            dropped = next(iter(self._questions))
            logger.warning("%s dropped the oldest of too many waiting questions: %s", turn.inbound.platform, dropped)
            dropped_turn = self._questions.pop(dropped)
            self._cancel_question_timer(dropped)
            await self._withdraw_question(dropped_turn, dropped[1])
            if dropped_turn.session_run is not None:
                if not self._supports(OP_ANSWER):
                    raise _QuestionCannotReject
                await self._backend.answer(dropped_turn.conversation, dropped[1], Answer(values=("reject",)))
        self._questions[asked] = turn
        try:
            await self._ask_where_it_came_from(turn.inbound, question)
        except QuestionRefused as refused:
            del self._questions[asked]
            await self._refuse_question(turn, question, str(refused))
            return
        if schedule is not None:
            self._question_timeouts[asked] = asyncio.create_task(
                self._expire_backend_question(asked, turn, question.id)
            )

    async def _refuse_question(self, turn: _Turn, question: Question, reason: str) -> None:
        """Tell the turn why its question reached nobody, so that its model can put it another way.

        A backend that cannot be told ends the turn with the reason.
        """
        logger.warning(
            "%s cannot show a question of the turn: conversation=%s question=%s reason=%s",
            turn.inbound.platform, turn.conversation, question.id, reason,
        )
        if not self._supports(OP_REFUSE_QUESTION):
            raise _QuestionNotShown(reason)
        await self._backend.refuse_question(
            turn.conversation, question.id, f"The question was not shown, so nobody was asked: {reason}."
        )

    # ── held calls ───────────────────────────────────────────────────────

    async def _hold_call(self, call: ToolCall, bound: BoundTurn, question: Question) -> bool:
        """Put a toolkit's question about one call to the conversation its turn came from, and hold the call.

        The tool call itself answers at once, so nothing waits inside a backend: no backend is known to
        keep a tool call open for as long as a person may take. Return whether somebody was asked.
        """
        turn = self._admission.turn_initiator(call.conversation, timeout=TURN_TIMEOUT_SECONDS)
        # Nobody asked for the turn of an event, so nobody is asked to agree to what it would do: the call is refused.
        if turn is None or turn.bound is not bound or turn.event is not None:
            return False
        schedule = turn.session_run
        if schedule is not None and schedule.questions == "refuse":
            return False
        if any(held.turn is turn and (held.call.name, held.call.arguments) == (call.name, call.arguments) for held in self._held.values()):
            # The model made the same call again while its question waits. One question is enough.
            return True
        question = replace(question, id=f"{turn.request_id}:held-{next(self._held_ids)}")
        if affordances(self._hosted[turn.inbound.platform].platform, workspace=turn.inbound.workspace).questions == "absent":
            logger.warning(
                "%s shows no question, so the call to %s is refused: conversation=%s",
                turn.inbound.platform, call.name, call.conversation,
            )
            return False
        timeout = self._question_timeout() if schedule is not None else CONFIRMATION_TIMEOUT_SECONDS
        deadline = self.clock() + timeout
        held = _HeldCall(turn=turn, call=call, asked_at=self.clock(), deadline=deadline)
        # Controls can be answered before the platform finishes asking.
        self._held[question.id] = held
        self._held_timeouts[question.id] = asyncio.create_task(self._expire_held_call(question.id, held))
        asked = False
        try:
            await self._ask_where_it_came_from(turn.inbound, question)
            asked = True
        except Exception:  # A platform that cannot ask leaves an unanswered call refused.
            logger.warning(
                "%s could not ask about %s, so the call is refused: conversation=%s",
                turn.inbound.platform, call.name, call.conversation, exc_info=True,
            )
            return held.answered
        finally:
            if not asked and self._held.get(question.id) is held:
                del self._held[question.id]
                self._cancel_held_timer(question.id)
                await self._withdraw_question(turn, question.id)
        while len(self._held) > MAX_PENDING_QUESTIONS:
            dropped = next(iter(self._held))
            logger.warning("%s dropped the oldest of too many held calls: %s", turn.inbound.platform, dropped)
            dropped_held = self._held.pop(dropped)
            self._cancel_held_timer(dropped)
            if dropped_held.turn.session_run is not None:
                self._remember_expired((dropped_held.turn.inbound.platform, dropped), dropped_held.turn)
            await self._withdraw_question(dropped_held.turn, dropped)
        return held.answered or self._held.get(question.id) is held

    async def _settle_held(self, question_id: str, held: _HeldCall, answer: InboundAnswer) -> None:
        """Make a held call once if its answer is yes, and leave word of the decision for the next turn."""
        call, turn = held.call, held.turn
        if turn.session_run is not None and (
            turn.session_run_ended or turn.bound.stopped or held.deadline is None or self.clock() >= held.deadline
        ):
            self._remember_expired((turn.inbound.platform, question_id), turn)
            return
        arguments = json.dumps(dict(call.arguments), ensure_ascii=False, default=str)
        said = f"An earlier turn of this conversation called {call.name} with {arguments[:200]}, which a person had to agree to."
        if not approves(answer.answer):
            self._notes.add(SYSTEM_ADDITIONS, turn.conversation, f"{said} {answer.sender} refused, and nothing was sent. Do not make that call again.")
            return
        result = await self._tools.run_confirmed(call, turn.bound)
        logger.info(
            "%s made a held call a person agreed to: conversation=%s tool=%s question=%s ok=%s",
            turn.inbound.platform, turn.conversation, call.name, question_id, succeeded(result),
        )
        self._notes.add(SYSTEM_ADDITIONS, turn.conversation, f"{said} {answer.sender} agreed, and the call was then made once. Its result: {result_text(result)}")
        if not succeeded(result):
            try:
                failure = json.loads(result_text(result))
                error = failure.get("error", "") if isinstance(failure, Mapping) else ""
            except (ValueError, TypeError, AttributeError):
                error = ""
            posted = _posted_records(result, call, turn.bound)
            if posted:
                count = len(posted)
                ids = ", ".join(record["message_id"] for record in posted)
                destination = posted[0]["conversation"]
                retry_warning = (
                    " Discord may also have accepted the next part. Do not retry the whole call; it may duplicate confirmed or uncertain parts."
                    if error == "delivery_uncertain_do_not_retry"
                    else " Retrying the whole post can duplicate those parts."
                )
                await self._post(
                    turn.inbound,
                    f"{call.name} failed after {count} part(s) were sent to {destination}. "
                    f"Sent message IDs: {ids}.{retry_warning}",
                )
                return
            # Without a receipt, delivery may still be uncertain.
            outcome = (
                "Delivery is uncertain. Do not retry this call because it may duplicate the action."
                if error == "delivery_uncertain_do_not_retry" else "Nothing was sent."
            )
            await self._post(
                turn.inbound, f"{call.name} was agreed to and then failed: {error or 'no reason was given'}. {outcome}"
            )

    async def _expired_answer(self, answer: InboundAnswer, turn: _Turn) -> str:
        timeout = self._question_timeout() if turn.session_run is not None else CONFIRMATION_TIMEOUT_SECONDS
        text = (
            "This request expired, so nothing ran. The run that asked for it has ended, "
            f"or it waited longer than {timeout} s."
        )
        platform = self._hosted[turn.inbound.platform].platform
        if affordances(platform, workspace=turn.inbound.workspace).private_aside != "absent":
            await self._tell_privately(turn.inbound, answer.sender, text)
        else:
            await self._post(turn.inbound, text)
        return "refused:question-expired"

    async def _receive_answer(self, answer: InboundAnswer) -> str:
        if not self._running:
            return "dropped:host-not-running"
        if not self._sender_allowed(answer.sender, answer.platform):
            # Seeing a question does not grant the right to answer it.
            return f"refused:user-not-in-{KEY_ALLOW_FROM}"
        held = self._held.get(answer.question_id)
        expired = False
        if held is not None and held.turn.session_run is not None and (
            held.turn.bound.stopped or held.turn.session_run_ended or held.deadline is None or self.clock() >= held.deadline
        ):
            del self._held[answer.question_id]
            self._cancel_held_timer(answer.question_id)
            self._remember_expired((held.turn.inbound.platform, answer.question_id), held.turn)
            await self._withdraw_question(held.turn, answer.question_id)
            expired = True
            held = None
        elif held is not None and (held.turn.bound.stopped or self.clock() - held.asked_at > CONFIRMATION_TIMEOUT_SECONDS):
            # Its turn was stopped, or the answer came later than a decision is kept for. Nothing is sent.
            del self._held[answer.question_id]
            self._cancel_held_timer(answer.question_id)
            await self._withdraw_question(held.turn, answer.question_id)
            held = None
        turn = held.turn if held is not None else self._questions.get((answer.platform, answer.question_id))
        expired_entry = self._expired_questions.get((answer.platform, answer.question_id))
        if expired_entry is not None and expired_entry[1] <= self.clock():
            del self._expired_questions[(answer.platform, answer.question_id)]
            expired_entry = None
        if turn is None and expired_entry is not None:
            turn = expired_entry[0]
            expired = True
        asked_in = (turn.inbound.platform, turn.inbound.workspace, turn.inbound.conversation) if turn is not None else None
        # A conversation id names a conversation in one workspace of one platform. The same id elsewhere is another place.
        if turn is None or asked_in != (answer.platform, answer.workspace, answer.conversation):
            return "ignored:question-not-waiting"
        # Seeing a question does not grant the right to answer it.
        rule = click_rule(self._scopes, channel=answer.platform, chat=answer.conversation, kind=CLICK_APPROVE)
        if rule is not None and not rule.permits(answer.sender):
            return f"refused:clicks.{CLICK_APPROVE}={rule.describe()}"
        if expired:
            return await self._expired_answer(answer, turn)
        # Claim before awaiting, so two answers cannot both be delivered.
        if held is not None:
            del self._held[answer.question_id]
            held.answered = True
            self._cancel_held_timer(answer.question_id)
            await self._settle_held(answer.question_id, held, answer)
            return f"answered conversation={turn.conversation}"
        del self._questions[(answer.platform, answer.question_id)]
        self._cancel_question_timer((answer.platform, answer.question_id))
        await self._backend.answer(turn.conversation, answer.question_id, answer.answer)
        return f"answered conversation={turn.conversation}"

    # ── startup checks ───────────────────────────────────────────────────

    def _warn_about_unapplied_settings(self, capabilities: BackendCapabilities) -> None:
        """Name each scoped setting this backend does not apply."""
        reported: set[tuple[str, str]] = set()

        channel = self._label

        def report(setting: str, missing: str) -> None:
            if (setting, missing) in reported:
                return
            reported.add((setting, missing))
            logger.warning(
                "scopes: %s is set for %s but backend %s does not apply %s. That setting enforces nothing",
                setting,
                channel,
                capabilities.backend,
                missing,
            )

        def need(setting: str, *scope_fields: str) -> None:
            for name in scope_fields:
                if name not in capabilities.scope_fields:
                    report(setting, name)

        served = {definition["name"] for definition in self._tools.definitions("")}
        declared = capabilities.tool_levels if SCOPE_FIELD_TOOLS in capabilities.scope_fields else None

        def unheld(tools: Any) -> None:
            """Name each tool level that nothing is known to hold.

            A level on a tool this host serves is held by the tool host. A level on a tool of the
            backend's own is held where the backend declares it. Any other is a name nobody declares,
            or a level the backend cannot apply to that tool: a deny that denies nothing is said here.
            """
            for name, level in tools.items() if isinstance(tools, Mapping) and declared is not None else ():
                setting = f"{SECTION_PERMISSIONS}.tools.{name}={level}"
                if name in served or level in declared.get(name, ()):
                    continue
                if name in declared:
                    report(setting, f"{level} to {name}")
                elif (setting, "") not in reported:
                    reported.add((setting, ""))
                    logger.warning(
                        "scopes: %s is set for %s, and neither that platform nor backend %s declares a tool of"
                        " that name. Nothing confirms that setting is enforced",
                        setting,
                        channel,
                        capabilities.backend,
                    )

        for scope in self._scopes:
            if scope.match.channel is not None and scope.match.channel not in self._hosted:
                continue
            channel = scope.match.channel or self._label
            agent = scope.section(SECTION_AGENT)
            for key, scope_fields in _AGENT_SCOPE_FIELDS.items():
                if key in agent:
                    need(f"{SECTION_AGENT}.{key}", *scope_fields)
            skills = agent.get("skills")
            for key in skills if isinstance(skills, Mapping) else ():
                need(f"{SECTION_AGENT}.skills.{key}", f"skills_{key}")
            required = skills.get("required") if self._supplies_skills and isinstance(skills, Mapping) else None
            for name in required or ():
                if packaged_skill_text(name) is None:
                    report(f"{SECTION_AGENT}.skills.required", f"the skill {name}, which Belle does not package")
            delivery = scope.section(SECTION_DELIVERY)
            if "prompt" in delivery or delivery.get(KEY_REPLY) == REPLY_OPTIONAL:
                need(f"{SECTION_DELIVERY}.{'prompt' if 'prompt' in delivery else KEY_REPLY}", "system_additions")
            if delivery.get(KEY_MID_TURN) == MID_TURN_CANCEL and OP_CANCEL not in capabilities.operations:
                report(f"{SECTION_DELIVERY}.{KEY_MID_TURN}={MID_TURN_CANCEL}", f"the {OP_CANCEL} operation")
            if scope.section(SECTION_PERMISSIONS):
                # The tool host still gates the platform's tools. The backend's own are not narrowed.
                need(f"{SECTION_PERMISSIONS}.tools", "tools")
                unheld(scope.section(SECTION_PERMISSIONS).get("tools"))


def host_from_config(
    config: Mapping[str, Any],
    backend: Backend,
    *,
    state_dir: Path,
    second_brain: SecondBrainBackend | None = None,
) -> ConversationHost:
    """Build a host for the platforms the configuration turns on.

    ``second_brain`` is what the corpus reads, summarizes and indexes with. The
    core holds no implementation of it, so whoever starts the host loads one.
    """
    control = ControlSettings.from_config(config)
    hosted: list[HostedPlatform] = []
    sections: dict[str, Mapping[str, Any]] = {}
    for platform in (create_platform(config, name, state_dir=state_dir) for name in enabled_platforms(config)):
        register_channel(platform.capabilities)
        section = platform_section(config, platform.name)
        section = sections[platform.name] = section if isinstance(section, Mapping) else {}
        hosted.append(
            HostedPlatform(
                platform,
                # Each platform keeps its own window of the deliveries it handled.
                EventDedupStore(state_dir / f"{platform.name}_seen_events.json"),
                allow_from=_ids(section.get(KEY_ALLOW_FROM)),
                allowed_conversations=_ids(section.get(KEY_ALLOWED_CONVERSATIONS)),
                decisions=decisions_from_config(config, platform),
                content_tools=content_toolkit_from_config(config, platform),
            )
        )
    directory = compile_people(config.get(PEOPLE_KEY), config.get(ROLES_KEY))
    scopes = compile_scopes(
        config.get("scopes"),
        channels_config=sections,
        directory=directory,
    )
    return ConversationHost(
        hosted,
        backend,
        scopes=scopes,
        second_brain=second_brain_from_config(config, second_brain),
        notes_file=state_dir / "turn_notes.json",
        scheduling=scheduling_settings(config),
        attached_sessions=attached_session_settings(config),
        state_dir=state_dir,
        directory=directory,
        control=control,
        task_models=task_model_rows(config),
    )
