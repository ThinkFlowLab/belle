"""Drive OpenCode turns over its HTTP wire and translate what it reports."""

import asyncio
import contextlib
import itertools
import logging
import time
import urllib.parse
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from belle_agent.backend import (
    APPROVAL,
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
    OP_ACTIVE,
    OP_ANSWER,
    OP_ASK,
    OP_CANCEL,
    OP_CATALOG,
    OP_CONTINUE,
    OP_FORGET,
    OP_HEALTH,
    OP_PEEK_HISTORY,
    OP_PENDING,
    OP_RECOVER,
    OP_REFUSE_QUESTION,
    PROGRESS_EVENTS,
    QUESTION,
    RECOVERY_NOT_SUBMITTED,
    RECOVERY_RUNNING,
    RECOVERY_TERMINAL,
    SUBAGENT,
    Answer,
    BackendCapabilities,
    BackendError,
    BackendHealth,
    Catalog,
    McpHealth,
    PeekHistoryPart,
    Question,
    Recovery,
    ToolHost,
    TurnEvent,
    TurnRequest,
    TurnScope,
    still_running,
)
from belle_agent.integrations.opencode.permissions import TOOL_LEVELS, permission_rules
from belle_agent.integrations.opencode.pin import OPENCODE_VERSION
from belle_agent.integrations.opencode.relay import RelayEndpoint
from belle_agent.integrations.opencode.sessions import message_id, session_id
from belle_agent.integrations.opencode.wire import Wire, WireError
from belle_agent.models import ConfiguredModels
from belle_agent.scopes import LEVEL_DENY
from belle_agent.tools.host import APPROVE, REJECT, approves
from belle_agent.tools.server import PATH as TOOLS_PATH
from belle_agent.tools.server import ToolServer

logger = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 120.0
READY_POLL_SECONDS = 0.5
HEALTH_TIMEOUT_SECONDS = 0.75
RECONNECT_SECONDS = 1.0
# How long a turn that was left waits for OpenCode to report it stopped.
STOP_TIMEOUT_SECONDS = 10.0

# The name of Belle's MCP server in OpenCode. OpenCode advertises its tools as `<server>_<tool>`.
MCP_SERVER = "belle"
#: The id `plugin/relay.ts` registers under.
RELAY_PLUGIN = "belle.relay"
MOUNT_TIMEOUT_SECONDS = 15.0
# No event says the tools of a mount are advertised. A turn sent 0.3 to 0.4 s after the mount
# connected had them, and one sent at once did not. Turns wait this long after each mount.
MOUNT_SETTLE_SECONDS = 1.0
# The key of the session instruction entry that carries a turn's `TurnScope.system_additions`.
INSTRUCTION_KEY = "belle"
# The ids that tell a tool approval from an invented form.
PERMISSION_ID_PREFIX = "per"
FORM_ID_PREFIX = "frm_"
# A subagent's call carries its own session id. Its conversation is that of the session it descends from.
MAX_SESSION_DEPTH = 4
# OpenCode's built-in tool that runs a subagent in a child session, and the argument that names the subagent's type.
SUBAGENT_TOOL = "subagent"
SUBAGENT_TYPE_ARGUMENT = "agent"
# The status of a `subagent` call that returned while its child runs on: the call was made with `background`.
CHILD_RUNNING = "running"
# The inbox item OpenCode makes of a child's report to the session that started it, and the state of a child that completed.
CHILD_REPORT = "synthetic"
CHILD_COMPLETED = "completed"
# How a step ends that called a tool. Its text tells of the call and is no answer.
STEP_CALLED_TOOLS = "tool-calls"
# How a step ends that hit the output limit. If the step called a tool, the next step answers the result or the
# failure of the call, as after a tool-calls step. OpenCode may not run the calls of a cut response, and with an
# OpenAI-compatible provider it runs none of them. If the step called no tool, OpenCode tells the model to go on
# without repeating itself. The text of the next step then joins the cut text with no separator, because a cut can
# fall in the middle of a word. As in OpenCode, a call that the provider runs itself (``executed``) does not count.
STEP_CUT = "length"
# OpenCode continues a step whose response broke off after it began to answer, such as a stream that ended with no
# finish. It fails the step and keeps its text. It schedules a retry of the failed step, and when the wait ends it
# prompts the model with this text (``CONTINUE_AFTER_INCOMPLETE_STREAM``, ``packages/core/src/session/runner/llm.ts``).
# The adapter then reads the failed step as a cut step. In stored messages the failed step keeps its ``retry``. So
# does a failed step whose wait a shutdown ended, and OpenCode resumes that turn with a new step and no prompt. So
# once a next step exists, only this prompt marks the continuation. Other messages, such as a model switch, can come
# between the failed step and the prompt.
CONTINUE_BROKEN = (
    "The previous response was interrupted. Continue from where you left off without repeating completed content."
)
#: How the final of a turn that failed after it wrote text tells of the failure, on a line of its own.
RESPONSE_STOPPED = "The model's response stopped: {reason}."

# How many of a session's newest messages are read to learn how a turn ended.
RECONCILE_PAGE = 50
# An execution can leave the active list a moment before its end is stored.
RECONCILE_SETTLE_SECONDS = 0.5

ALREADY_RUNNING = "A turn is already running in this conversation."
ALREADY_RAN = "This request has already run in this conversation."
EXECUTION_LOST = "OpenCode stopped running the turn before it ended."

# In a turn's queue: events were read again after a time in which none could be. OpenCode replays
# nothing, so what the turn did meanwhile is read from its session.
_RESUBSCRIBED: dict[str, Any] = {}
# In a turn's queue: the turn is to stop while children it started run. Its reader stops them with it.
_CANCELLED: dict[str, Any] = {}


#: The kinds of input OpenCode says a model takes, and the media types of tool-result content each lets
#: through. Since 2.1.3 (OpenCode #57640), ``toContent`` of ``packages/core/src/tool/mcp.ts`` hands a model
#: only the media types of ``MEDIA_MIMES``, the list of its read tool, as a message of its own after the
#: tool's. It gives a textual type as text, and saves any other binary type to a file on its server and
#: tells the model the path, so audio reaches no model.
INPUT_CONTENT: Mapping[str, frozenset[str]] = {
    "image": frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
    "pdf": frozenset({"application/pdf"}),
}


def _content_types(kinds: Any) -> frozenset[str]:
    if not isinstance(kinds, (list, frozenset)):
        return frozenset()
    return frozenset().union(*(INPUT_CONTENT[kind] for kind in kinds if kind in INPUT_CONTENT))


class OpenCodeError(BackendError):
    """OpenCode cannot serve Belle as it stands."""


@dataclass
class _Turn:
    """What the adapter holds for one running turn."""

    conversation: str
    prompt_id: str
    #: The turn's settled scope, for the relay endpoint to answer hooks from.
    scope: TurnScope = field(default_factory=TurnScope)
    #: The prompt has been sent, or is being sent.
    posted: bool = False
    #: OpenCode has handed this turn's prompt to the session's execution.
    delivered: bool = False
    #: Events of this turn may have been missed. Its end is then read from the session.
    gapped: bool = False
    #: What the session had cost before this turn. OpenCode reports the cost of a session.
    cost_before: float = 0.0
    #: Session token totals before this turn; USAGE events subtract these so counts are turn-scoped.
    tokens_in_before: int = 0
    tokens_out_before: int = 0
    events: asyncio.Queue[Mapping[str, Any]] = field(default_factory=asyncio.Queue)
    #: Tool call id to tool name: `session.tool.called` carries the id only.
    tools: dict[str, str] = field(default_factory=dict)
    #: The name OpenCode advertises each of Belle's own tools under, to the name the core knows it by.
    belle_tools: Mapping[str, str] = field(default_factory=dict)
    #: Tool call id to the type of subagent the call runs, for a call of OpenCode's subagent tool.
    subagents: dict[str, str] = field(default_factory=dict)
    #: Text parts of the step in progress.
    final: list[str] = field(default_factory=list)
    #: Whether the next step continues the text of the steps before it, which ``unfinished`` holds. Those steps were
    #: cut at the output limit or broke off, and called no tool that the provider does not run itself.
    continuing: bool = False
    unfinished: str = ""
    #: Whether the step in progress called a tool that the provider does not run itself.
    step_called: bool = False
    #: Whether the step in progress failed.
    step_failed: bool = False
    #: The answers given so far, in order: the text of each step that called no tool. An execution gives one,
    #: and one more for each child's report that reaches it while it runs.
    answers: list[str] = field(default_factory=list)
    #: Child session to the call that started it, for each subagent that runs in the background and has not reported.
    children: dict[str, str] = field(default_factory=dict)
    #: The children's reports the session holds and has handed to no execution yet.
    inbox: set[str] = field(default_factory=set)
    #: When the turn began, by the monotonic clock: a refusal of the relay endpoint since then is this turn's to name.
    began: float = field(default_factory=time.monotonic)


def _text(parts: Any) -> str:
    return "".join(part.get("text", "") for part in parts or () if part.get("type") == "text")


def _session_token_totals(raw: Any) -> tuple[int | None, int | None]:
    """Read nonnegative input/output counts from an OpenCode session or usage payload."""
    if not isinstance(raw, Mapping):
        return None, None
    raw_in, raw_out = raw.get("input"), raw.get("output")
    tokens_in = raw_in if type(raw_in) is int and raw_in >= 0 else None
    tokens_out = raw_out if type(raw_out) is int and raw_out >= 0 else None
    return tokens_in, tokens_out


def _turn_tokens(raw: Any, turn: _Turn) -> dict[str, int]:
    """Session token totals minus the baseline at turn start (turn-cumulative deltas)."""
    raw_in, raw_out = _session_token_totals(raw)
    tokens: dict[str, int] = {}
    if raw_in is not None:
        tokens["input"] = max(0, raw_in - turn.tokens_in_before)
    if raw_out is not None:
        tokens["output"] = max(0, raw_out - turn.tokens_out_before)
    return tokens


def translate(event: Mapping[str, Any], turn: _Turn) -> TurnEvent | None:
    """Turn one OpenCode event of a turn's session into what the core reads, if it reads it."""
    data = event.get("data") or {}
    if not turn.delivered:
        # Until then the session reports earlier work: an execution that was stopped, for one.
        turn.delivered = event.get("type") == "session.inbox.delivered" and data.get("inboxID") == turn.prompt_id
        return None
    session = event_session(event)
    if session and session in turn.children:
        # Child-session SSE shares the global stream. Forward text/tools under the parent subagent call id.
        return _translate_child(event, turn, turn.children[session])
    call_id = data.get("id", "")
    error = data.get("error") or {}
    match event.get("type"):
        case "session.step.started":
            # One per request to the model: a request that is retried starts a step again. A request that
            # continues a cut or broken text says so.
            _step_started(turn)
            return TurnEvent(EVENT_STEP, data={"continues": True} if turn.continuing else {})
        case "session.text.delta":
            # OpenCode replays nothing. Once events were missed the text of the reply has a hole, and the
            # deltas that follow would be shown as if they continued what came before. The final is whole.
            return None if turn.gapped else TurnEvent(EVENT_TEXT_DELTA, text=data.get("delta", ""))
        case "session.text.ended":
            turn.final.append(data.get("text", ""))
        case "session.step.ended":
            _answered(turn, data.get("finish"))
        case "session.step.failed":
            turn.step_failed = True
        case "session.retry.scheduled":
            # A step retried from its start has not failed. A failed step is retried only to continue its text.
            if turn.step_failed:
                _answered(turn, STEP_CUT)
        case "session.tool.input.started":
            # One of Belle's own tools is advertised as `belle_<name>`. The core and its scopes know it as `<name>`.
            name = data.get("name", "")
            turn.tools[call_id] = turn.belle_tools.get(name, name)
        case "session.tool.called":
            turn.step_called |= not data.get("executed")
            arguments = data.get("input") or {}
            if turn.tools.get(call_id) == SUBAGENT_TOOL:
                # The call runs a child session. Its type is all this event says of it.
                turn.subagents[call_id] = str(arguments.get(SUBAGENT_TYPE_ARGUMENT) or "")
            return TurnEvent(EVENT_TOOL_CALLED, data={**_called(turn, call_id), "arguments": arguments})
        case "session.tool.progress":
            # Progress can name the child session before tool.success; routing is recorded in the pump too.
            _remember_child(turn, call_id, data.get("metadata"))
        case "session.tool.success":
            child = _background_child(data.get("metadata")) if call_id in turn.subagents else ""
            if child:
                # The call returned and its subagent runs on. The call's result is the child's report.
                turn.children[child] = call_id
                return None
            if call_id in turn.subagents:
                # A waiting subagent finished: stop routing its session onto this turn.
                _forget_child(turn, call_id)
            return TurnEvent(EVENT_TOOL_RESULT, text=_text(data.get("content")), data=_called(turn, call_id))
        case "session.tool.failed":
            # A call that OpenCode fails before it runs, such as one that the output limit cut, has no `called` event.
            turn.step_called |= not data.get("executed")
            if call_id in turn.subagents:
                _forget_child(turn, call_id)
            failed = {**_called(turn, call_id), "failed": True}
            return TurnEvent(EVENT_TOOL_RESULT, text=str(error.get("message", "")), data=failed)
        case "session.inbox.enqueued":
            return _reported(data, turn)
        case "session.inbox.delivered":
            turn.inbox.discard(data.get("inboxID"))
        case "session.usage.updated":
            cost = data.get("cost")
            spent = cost - turn.cost_before if isinstance(cost, (int, float)) else None
            usage: dict[str, Any] = {"cost": spent}
            if "tokens" in data:
                usage["tokens"] = _turn_tokens(data.get("tokens"), turn)
            return TurnEvent(EVENT_USAGE, data=usage)
        case "session.execution.succeeded":
            _answered(turn)
            # OpenCode runs the session once more for each child that reports. The turn ends with the last of them.
            if turn.children or turn.inbox:
                return None
            return TurnEvent(EVENT_SUCCEEDED, text=_final(turn.answers))
        case "session.execution.failed":
            # The provider's answer body travels in `error.response`. It stays behind.
            return _execution_failed(turn, error)
        case "session.execution.interrupted":
            return TurnEvent(EVENT_INTERRUPTED, text=str(data.get("reason", "")))
        case "permission.asked":
            # OpenCode's permission engine raised an `ask` from the session ruleset. A person decides;
            # the wait is server state, not a held tool call. The action is the tool, built-in or `belle_*`.
            return _question_from_permission(data)
        case "form.created":
            # A question on the wire: the model's native `question` tool, or one Belle invented.
            return _question_from_form(data.get("form") or {})
    return None


def _translate_child(event: Mapping[str, Any], turn: _Turn, attribution: str) -> TurnEvent | None:
    """Forward one child-session event as parent-turn progress attributed to the subagent call.

    ``attribution`` is the parent ``subagent`` tool call id. Host peek maps that id to the child
    Activity row. The child's own execution lifecycle does not drive the parent turn.
    """
    data = event.get("data") or {}
    call_id = data.get("id", "")
    match event.get("type"):
        case "session.text.delta":
            # Same no-replay rule as the parent: a gap would weld later deltas onto earlier text.
            # ``child`` keeps drafts off ordinary platform answer delivery; peek still tees them.
            return None if turn.gapped else TurnEvent(
                EVENT_TEXT_DELTA,
                text=data.get("delta", ""),
                data={"call_id": attribution, "child": True},
            )
        case "session.tool.input.started":
            name = data.get("name", "")
            turn.tools[call_id] = turn.belle_tools.get(name, name)
        case "session.tool.called":
            arguments = data.get("input") or {}
            return TurnEvent(
                EVENT_TOOL_CALLED,
                data={
                    "name": turn.tools.get(call_id, ""), "call_id": attribution,
                    "arguments": arguments, "child": True,
                },
            )
        case "session.tool.success":
            return TurnEvent(
                EVENT_TOOL_RESULT,
                text=_text(data.get("content")),
                data={"name": turn.tools.get(call_id, ""), "call_id": attribution, "child": True},
            )
        case "session.tool.failed":
            error = data.get("error") or {}
            return TurnEvent(
                EVENT_TOOL_RESULT,
                text=str(error.get("message", "")),
                data={
                    "name": turn.tools.get(call_id, ""), "call_id": attribution,
                    "failed": True, "child": True,
                },
            )
    return None


def _remember_child(turn: _Turn, call_id: str, metadata: Any) -> None:
    """Map a child session onto its parent subagent call when the wire names the child."""
    if call_id not in turn.subagents:
        return
    metadata = metadata or {}
    child = str(metadata.get("sessionID") or "")
    if child and metadata.get("status") == CHILD_RUNNING:
        turn.children[child] = call_id


def _forget_child(turn: _Turn, call_id: str) -> None:
    """Stop routing sessions that belonged to a finished subagent call."""
    for child, owner in list(turn.children.items()):
        if owner == call_id:
            del turn.children[child]


def _called(turn: _Turn, call_id: str) -> dict[str, Any]:
    """Name one tool call as the core reads it: its tool, its id, and its subagent's type where it runs one."""
    called: dict[str, Any] = {"name": turn.tools.get(call_id, ""), "call_id": call_id}
    if call_id in turn.subagents:
        called[SUBAGENT] = turn.subagents[call_id]
    return called


def _step_started(turn: _Turn) -> None:
    turn.final.clear()
    turn.step_called = turn.step_failed = False


def _answered(turn: _Turn, finish: str | None = None) -> None:
    """Take the text of the step that ended with ``finish`` into the turn's answers.

    The text joins the text of the cut steps that it continues. A cut step that called no tool is continued by the
    next step. Its text and a cut step's that called a tool are no answer, nor is a tool-calls step's. With no
    ``finish`` the step ended with its execution, as a failed step that OpenCode does not continue does. Its text is
    then an answer unless the step called a tool.
    """
    text = turn.unfinished + "".join(turn.final)
    turn.final.clear()
    turn.continuing = finish == STEP_CUT and not turn.step_called
    turn.unfinished = text if turn.continuing else ""
    called = finish == STEP_CALLED_TOOLS or (finish is None and turn.step_called)
    if finish != STEP_CUT and not called and text.strip():
        turn.answers.append(text)


def _execution_failed(turn: _Turn, error: Mapping[str, Any]) -> TurnEvent:
    """Build the terminal event of a turn whose execution failed, from what the turn wrote before it."""
    _answered(turn)
    return _failed(turn.answers, error)


def _replay(messages: Sequence[Mapping[str, Any]], turn: _Turn | None = None) -> _Turn:
    """Read a session's stored steps, oldest first, into ``turn`` or a new one, as their events are read."""
    turn = turn or _Turn(conversation="", prompt_id="")
    turn.answers.clear()
    turn.unfinished = ""
    turn.continuing = False
    _step_started(turn)
    for index, message in enumerate(messages):
        if message.get("type") != "assistant":
            continue
        _step_started(turn)
        content = message.get("content") or ()
        turn.final.append(_text(content))
        turn.step_called = any(part.get("type") == "tool" and not part.get("executed") for part in content)
        turn.step_failed = bool(message.get("error"))
        if turn.step_failed:
            # OpenCode continues a failed step while it waits to retry it, and once it has prompted the model to go
            # on. Its next step clears the text of a failed step that it does not continue.
            following = list(itertools.takewhile(lambda later: later.get("type") != "assistant", messages[index + 1 :]))
            waiting = message.get("retry") and index + len(following) + 1 == len(messages)
            if waiting or any(_prompts_to_go_on(later) for later in following):
                _answered(turn, STEP_CUT)
        elif message.get("finish"):
            _answered(turn, message["finish"])
    return turn


def _prompts_to_go_on(message: Mapping[str, Any]) -> bool:
    return message.get("type") == "synthetic" and message.get("text") == CONTINUE_BROKEN


def _written(turn: _Turn) -> list[str]:
    """What a turn wrote so far: its answers, and last the text of the step in progress and the steps it continues."""
    return [*turn.answers, turn.unfinished + "".join(turn.final)]


def _final(answers: Sequence[str]) -> str:
    """Join a turn's answers into its final: each one once, in order, a blank line apart."""
    answers = [answer for answer in answers if answer.strip()]
    return "\n\n".join(answer.strip() for answer in answers) if len(answers) > 1 else "".join(answers)


def _failed(answers: Sequence[str], error: Mapping[str, Any]) -> TurnEvent:
    """Build the terminal event of an execution that failed. The text written before it is kept."""
    reason = str(error.get("message") or error.get("type") or "OpenCode reported no reason.")
    failure_kind = "provider" if str(error.get("type") or "").startswith("provider.") else "backend"
    written = _final(answers).strip()
    stopped = RESPONSE_STOPPED.format(reason=reason.rstrip("."))
    return TurnEvent(
        EVENT_FAILED, text=f"{written}\n\n{stopped}" if written else reason,
        data={"failure_kind": failure_kind, "status": error.get("status"), "error_type": error.get("type")},
    )


def _in_session(session: str, ended: TurnEvent | None) -> TurnEvent | None:
    """Name the session of a failed turn: OpenCode keeps its record of the failure there."""
    if ended is None or ended.kind != EVENT_FAILED:
        return ended
    return replace(ended, data={**ended.data, "backend_session": session})


def _background_child(metadata: Any) -> str:
    """The child session a ``subagent`` call left running in the background, from the call's metadata, or nothing."""
    metadata = metadata or {}
    return str(metadata.get("sessionID") or "") if metadata.get("status") == CHILD_RUNNING else ""


def _reported(data: Mapping[str, Any], turn: _Turn) -> TurnEvent | None:
    """Take a child's report, which OpenCode puts to the session as a message of its own making.

    The report is the result of the call that started the child. The session
    answers it: in the execution in progress, or in one OpenCode starts for it.
    """
    item = data.get("item") or {}
    payload = item.get("payload") or {}
    report = payload.get("metadata") or {}
    if item.get("type") != CHILD_REPORT or report.get("source") != SUBAGENT_TOOL:
        return None
    turn.inbox.add(str(data.get("inboxID") or ""))
    call_id = turn.children.pop(str(report.get("childID") or ""), None)
    if call_id is None:
        # A child of an earlier turn. Its report is answered in this one all the same.
        return None
    failed = {} if report.get("state") == CHILD_COMPLETED else {"failed": True}
    return TurnEvent(EVENT_TOOL_RESULT, text=str(payload.get("text") or ""), data={**_called(turn, call_id), **failed})


def _unreported(messages: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Read from a session's messages the children started in the background that have not reported, each with its call."""
    children: dict[str, str] = {}
    for message in messages:
        if message.get("type") == CHILD_REPORT:
            children.pop((message.get("metadata") or {}).get("childID"), None)
        for part in (message.get("content") or ()) if message.get("type") == "assistant" else ():
            state = part.get("state") or {}
            child = _background_child(state.get("metadata")) if part.get("name") == SUBAGENT_TOOL else ""
            # A call that waits for its child reports it running too, until the call itself has ended.
            if child and state.get("status") == CHILD_COMPLETED:
                children[child] = str(part.get("id") or "")
    return children


def event_session(event: Mapping[str, Any]) -> str | None:
    """The session an event belongs to. Most carry it at ``data.sessionID``; a form nests it."""
    data = event.get("data") or {}
    return data.get("sessionID") or (data.get("form") or {}).get("sessionID")


def _question_from_permission(data: Mapping[str, Any]) -> TurnEvent:
    resources = ", ".join(str(resource) for resource in data.get("resources") or ())
    action = str(data.get("action") or "")
    return TurnEvent(
        EVENT_QUESTION,
        question=Question(
            id=str(data.get("id") or ""),
            text=f"Allow {action}: {resources}".rstrip(": ").strip() or f"Allow {action}",
            options=(APPROVE, REJECT),
            kind=APPROVAL,
            tool=action,
        ),
    )


def _question_from_form(form: Mapping[str, Any]) -> TurnEvent:
    fields = form.get("fields") or []
    first = fields[0] if fields else {}
    options = tuple(str(option.get("value")) for option in first.get("options") or ())
    prompt = str(first.get("description") or first.get("title") or "")
    title = str(form.get("title") or "")
    text = f"{title}: {prompt}".strip(": ").strip() if title and prompt else (title or prompt)
    return TurnEvent(EVENT_QUESTION, question=Question(id=str(form.get("id") or ""), text=text, options=options, kind=QUESTION))


def stored(messages: Sequence[Mapping[str, Any]], outcome: str) -> TurnEvent:
    """Build a turn's terminal event from what its session stored: its messages, and how its last execution ended.

    The reason of a failure is the one that the ``idle`` message at the end of ``messages`` stores.
    """
    turn = _replay(messages)
    if outcome == "succeeded":
        _answered(turn)
        return TurnEvent(EVENT_SUCCEEDED, text=_final(turn.answers))
    if outcome == "interrupted":
        return TurnEvent(EVENT_INTERRUPTED)
    idle = messages[-1] if messages and messages[-1].get("type") == "idle" else {}
    return _execution_failed(turn, idle.get("error") or {})


def attachments(files: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Shape a request's files for a prompt. Only a ``data:`` URI travels.

    OpenCode reads a ``file://`` URI from its own filesystem, where none of
    Belle's files are and where a path from a platform would read the server's.
    """
    shaped = []
    for entry in files:
        uri = str(entry.get("uri") or "")
        if not uri.startswith("data:"):
            raise ValueError("a file reaches OpenCode as a data: URI and in no other form")
        shaped.append({"uri": uri, **({"name": str(entry["name"])} if entry.get("name") else {})})
    return shaped


def _lists_relay(plugins: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a plugin list holds the relay plugin: a source of its id that is not one of OpenCode's own."""
    return any(
        p.get("id") == RELAY_PLUGIN and (p.get("source") or {}).get("type") not in (None, "builtin") for p in plugins
    )


class OpenCodeBackend:
    """``belle_agent.backend.Backend`` on one OpenCode server.

    ``namespace`` keeps two deployments that share a server out of each
    other's sessions.
    """

    def __init__(
        self,
        url: str,
        password: str,
        *,
        username: str = "opencode",
        namespace: str = "",
        tools_bind: str = "127.0.0.1",
        tools_port: int = 0,
        tools_url: str = "",
        relay_bind: str = "127.0.0.1",
        relay_port: int = 0,
        relay_token: str = "",
        deny_tools: frozenset[str] = frozenset(),
        ready_timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._wire = Wire(url, password, username=username, transport=transport)
        self._url = url
        #: How long a start waits for OpenCode. ``None`` waits ``READY_TIMEOUT_SECONDS``.
        self._ready_timeout = ready_timeout
        # Why the event stream last failed. A start that runs out of time names it.
        self._stream_failure = ""
        # What a start waits on besides the model and the event stream, while it waits.
        self._waiting_on = ""
        self._tools: ToolHost | None = None
        # The first subscription has mounted the tools. Until then a mount that fails ends the start.
        self._started = False
        self._namespace = namespace
        self._tools_address = (tools_bind, tools_port)
        #: Where OpenCode reaches the tool server. The default suits an OpenCode on this host.
        self._tools_url = tools_url
        self._relay_address = (relay_bind, relay_port)
        self._relay_token = relay_token
        self._deny_tools = deny_tools
        #: The relay endpoint, started only when the relay plugin is loaded.
        self._relay: RelayEndpoint | None = None
        self._plugin = False
        self._tool_server: ToolServer | None = None
        # What serves a turn whose scope names no model. Read again at each subscription.
        self._default_model: tuple[str, str] = ("", "")
        self._model_content: dict[str, frozenset[str]] = {}
        # Belle's own tool names, to spell a scoped tool level as its advertised permission action.
        self._belle_tools: frozenset[str] = frozenset()
        # Session id to the turn that runs in it.
        self._turns: dict[str, _Turn] = {}
        # Set while events are read and the tools are mounted. A prompt waits for it.
        self._subscribed = asyncio.Event()
        self._mount_lock = asyncio.Lock()
        self._pump_task: asyncio.Task[None] | None = None
        # The host stops. A turn whose reader leaves now keeps running, and the next start recovers it.
        self._shutting_down = False

    def capabilities(self) -> BackendCapabilities:
        """Report the operations, scope fields and content types this adapter supports.

        Tool levels become session permissions. Required skills and the model
        are selected per turn. Progress comes from the turn's session; a child
        session is represented by its subagent call. Unsupported scope fields
        remain undeclared so the host can warn. An unknown model gets text only.
        """
        return BackendCapabilities(
            backend="opencode",
            operations=frozenset({OP_CANCEL, OP_ACTIVE, OP_RECOVER, OP_PENDING, OP_ASK, OP_ANSWER, OP_REFUSE_QUESTION,
                                  OP_CATALOG, OP_CONTINUE, OP_FORGET, OP_PEEK_HISTORY, OP_HEALTH}),
            scope_fields=frozenset(
                {"system_additions", "tools", "skills_required", "skills_available", "skills_unavailable", "model"}
            ),
            tool_levels=TOOL_LEVELS,
            tool_name_format=f"{MCP_SERVER}_{{name}}",
            reports=PROGRESS_EVENTS,
            tool_content=_content_types(self._wire.model_input),
            tool_content_by_model=self._model_content,
            catalog=frozenset({CATALOG_MODELS, CATALOG_SKILLS, CATALOG_SUBAGENTS}),
            version=OPENCODE_VERSION,
        )

    async def _loaded_plugins(self) -> Sequence[Mapping[str, Any]]:
        """List OpenCode's plugins, once it has loaded them.

        OpenCode loads a location's plugins when it is first asked for them, and
        answers that first call before they are loaded: an empty list. A loaded
        list is never empty, because it names OpenCode's own plugins. So an
        empty answer is asked again, within the time a start may take, and
        says nothing about the relay plugin. This holds with and without it:
        beside a server that loads no plugin of Belle's the wait ends with the
        server's own list.
        """
        while not (plugins := await self._wire.call("GET", "/api/plugin") or ()):
            self._waiting_on = "it lists no plugin yet, not even its own"
            await asyncio.sleep(READY_POLL_SECONDS)
        self._waiting_on = ""
        return plugins

    async def _relay_trouble(self, session: str, began: float) -> str:
        """Say what the relay plugin has to do with a prompt OpenCode answered with a server error, or nothing.

        A hook of the plugin that fails fails the prompt, and OpenCode's answer
        does not say why. A refusal of the relay endpoint since ``began`` is
        named. Without one, the cause is in OpenCode's log.
        """
        try:
            loaded = _lists_relay(await self._wire.call("GET", "/api/plugin") or ())
        except (WireError, httpx.HTTPError):
            return ""
        if not loaded:
            return ""
        started = self._relay is None and self._tools is not None
        if started:
            # The plugin was not in the list this adapter read at start. Its endpoint is started now.
            await self._start_relay(self._tools)
        # The port it listens on, which a configured port 0 leaves to the system.
        bind, port = self._relay_address[0], self._relay.port if self._relay is not None else self._relay_address[1]
        if started:
            return (
                "OpenCode loaded the relay plugin after this adapter had started, so nothing answered its hooks."
                f" The relay endpoint now listens on {bind}:{port}. Send the message again"
            )
        refused = self._relay.session_refusal(session) if self._relay is not None else None
        if refused is not None and refused[0] >= began:
            return f"The relay endpoint on {bind}:{port} refused a hook while this turn ran: {refused[1]}"
        return (
            f"The relay endpoint on {bind}:{port} refused no hook while this turn ran. OpenCode's log holds the cause."
            " If the relay plugin failed, it did not reach the endpoint, whose address it reads from BELLE_RELAY_URL"
        )

    async def _start_relay(self, tools: ToolHost) -> None:
        """Start the relay endpoint when the relay plugin is loaded, else stay on the wire.

        The plugin is a non-builtin source with the relay's id. With it loaded,
        a turn's system additions travel through the ``context`` hook; without
        it, through the session instruction entry. Everything else is the same
        on both paths, so no scope field needs the plugin and the declaration
        does not change between modes.
        """
        self._tools = tools
        self._plugin = _lists_relay(await self._loaded_plugins())
        if not self._plugin:
            return
        bind, port = self._relay_address
        if not self._relay_token:
            logger.warning("opencode: the relay plugin is loaded and no relay token is set; %s:%s answers any caller", bind, port)
        self._relay = RelayEndpoint(self._turn_of, tools, token=self._relay_token, host=bind, port=port)
        try:
            await self._relay.start()
        except OSError as exc:
            raise OpenCodeError(f"the relay endpoint cannot listen on {bind}:{port}: {exc}") from exc

    def _turn_of(self, session: str) -> "tuple[str, TurnScope] | None":
        """The conversation and scope of the turn running in a session, for the relay endpoint."""
        turn = self._turns.get(session)
        return (turn.conversation, turn.scope) if turn is not None else None

    async def start(self, tools: ToolHost) -> None:
        """Wait for the model catalog, check the pin, serve the tools and subscribe to events.

        Each way this can fail raises an :class:`OpenCodeError` that names it: a server that cannot be
        reached or refuses the password, another version than the pin, a listener that cannot bind,
        and a tool server OpenCode cannot connect to.
        """
        timeout = self._ready_timeout or READY_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(timeout):
                await self._ready()
                version = (await self._wire.call("GET", "/api/info") or {}).get("version")
                if version != OPENCODE_VERSION:
                    raise OpenCodeError(
                        f"OpenCode reports version {version}; this adapter is pinned to {OPENCODE_VERSION}"
                    )
                host, port = self._tools_address
                self._belle_tools = frozenset(d["name"] for d in tools.definitions(""))
                self._tool_server = ToolServer(
                    tools, self._conversation_of, host=host, port=port, advertised_url=self._tools_url
                )
                try:
                    await self._tool_server.start()
                except OSError as exc:
                    raise OpenCodeError(f"the tool server cannot listen on {host}:{port}: {exc}") from exc
                await self._start_relay(tools)
                self._pump_task = asyncio.create_task(self._pump())
                subscribed = asyncio.create_task(self._subscribed.wait())
                try:
                    await asyncio.wait({subscribed, self._pump_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    subscribed.cancel()
                if self._pump_task.done():
                    # Only a tool server OpenCode cannot connect to ends the pump. Its reason is the start's.
                    self._pump_task.result()
                self._started = True
        except BaseException as exc:
            await self.close()
            if isinstance(exc, TimeoutError):
                waited_on = self._waiting_on or self._stream_failure or self._wire.unready
                raise OpenCodeError(
                    f"OpenCode at {self._url} was not ready to run turns within {timeout:g}s: {waited_on}"
                ) from None
            if isinstance(exc, (WireError, httpx.HTTPError)):
                raise OpenCodeError(f"OpenCode at {self._url} refused the start: {exc}") from exc
            raise

    async def health(self) -> BackendHealth:
        if not self._started:
            return BackendHealth("disconnected", "not started", McpHealth("unknown", "not_started"))
        deadline = asyncio.get_running_loop().time() + HEALTH_TIMEOUT_SECONDS
        # Probe default model before subscription state (no readiness side effects).
        try:
            async with asyncio.timeout_at(deadline):
                default = await self._wire.probe_default_model()
        except WireError:
            return BackendHealth("disconnected", "authentication refused", McpHealth("unknown", "backend_unreachable"))
        except (TimeoutError, httpx.HTTPError):
            return BackendHealth("disconnected", "unreachable", McpHealth("unknown", "backend_unreachable"))
        mcp = await self._probe_mcp(deadline)
        if default is None:
            return BackendHealth("degraded", "default model not ready", mcp)
        if not self._subscribed.is_set():
            return BackendHealth("degraded", "event stream reconnecting", mcp)
        if mcp.state in {"missing", "failed"}:
            return BackendHealth("degraded", mcp.reason, mcp)
        return BackendHealth("ready", mcp=mcp)

    async def _probe_mcp(self, deadline: float) -> McpHealth:
        """Read Belle's mount from ``GET /api/mcp``. Never mounts or reconnects."""
        try:
            async with asyncio.timeout_at(deadline):
                entry = await self._belle_mcp_entry()
        except TimeoutError:
            return McpHealth("unknown", "mcp_probe_timeout")
        except (WireError, httpx.HTTPError, TypeError, ValueError):
            # ValueError covers JSONDecodeError from a non-JSON 200 body.
            return McpHealth("unknown", "mcp_probe_failed")
        if entry is None:
            return McpHealth("missing", "mcp_mount_missing")
        status = entry.get("status")
        if not isinstance(status, Mapping):
            return McpHealth("unknown", "mcp_probe_failed")
        state = status.get("status")
        if not isinstance(state, str):
            return McpHealth("unknown", "mcp_probe_failed")
        if state == "connected":
            return McpHealth("ready")
        if state == "failed":
            return McpHealth("failed", "mcp_mount_failed")
        if state in {"disconnected", "disabled"}:
            return McpHealth("failed", f"mcp_mount_{state}")
        if state in {"needs_auth", "needs_client_registration"}:
            return McpHealth("failed", "mcp_mount_needs_auth")
        return McpHealth("unknown", "mcp_mount_not_connected")

    async def _ready(self) -> None:
        """Wait for the default model and keep each declared model's input types."""
        while (default := await self._wire.default_model()) is None:
            await asyncio.sleep(READY_POLL_SECONDS)
        self._default_model = default
        models = await self._wire.listing("/api/model", "providerID", "id", "capabilities")
        self._model_content = {
            f"{model['providerID']}/{model['id']}": _content_types((model.get("capabilities") or {}).get("input"))
            for model in models
            if model["providerID"] and model["id"] and isinstance(model.get("capabilities"), Mapping)
        }
        self._model_content["/".join(default)] = _content_types(self._wire.model_input)

    async def _pump(self) -> None:
        """Hand each event to the turn of its session, for as long as the adapter runs."""
        while True:
            try:
                # After a restart OpenCode listens before it can run a turn, as at the start.
                await self._ready()
                async with self._wire.subscribe() as events:
                    # From here on nothing more is missed, so a waiting turn can read back what was.
                    for turn in self._turns.values():
                        if turn.posted:
                            turn.events.put_nowait(_RESUBSCRIBED)
                    # A restart of OpenCode drops both the stream and the mount, so each subscription mounts.
                    async with self._mount_lock:
                        await self._mount()
                    self._subscribed.set()
                    async for event in events:
                        # Record child session ids in the pump so the next events on this stream
                        # route before translate has run (one SSE; no second subscription).
                        self._note_child_route(event)
                        turn = self._turn_for(event_session(event))
                        if turn is not None:
                            turn.events.put_nowait(event)
                logger.warning("opencode: the event stream ended; subscribing again")
            except (WireError, httpx.HTTPError, OpenCodeError) as exc:
                if isinstance(exc, OpenCodeError) and not self._started:
                    raise
                self._stream_failure = f"the event stream failed ({exc})"
                logger.warning("opencode: the event stream failed (%s); subscribing again", exc)
            self._subscribed.clear()
            await asyncio.sleep(RECONNECT_SECONDS)

    def _turn_for(self, session: str | None) -> _Turn | None:
        """The running turn that owns ``session``, or the parent turn of a routed child session."""
        if not session:
            return None
        turn = self._turns.get(session)
        if turn is not None:
            return turn
        for candidate in self._turns.values():
            if session in candidate.children:
                return candidate
        return None

    def _note_child_route(self, event: Mapping[str, Any]) -> None:
        """Record child session → parent subagent call as soon as the wire names the child.

        Must run in the pump before routing: translate has not yet consumed the parent
        event when the child's following events arrive on the same stream. Gated on
        ``posted`` only — not ``delivered`` — so the pump never advances delivery
        ahead of events already queued on the turn.
        """
        session = event_session(event)
        turn = self._turns.get(session) if session else None
        if turn is None or not turn.posted:
            return
        data = event.get("data") or {}
        call_id = str(data.get("id") or "")
        kind = event.get("type")
        if kind == "session.tool.input.started":
            name = data.get("name", "")
            turn.tools[call_id] = turn.belle_tools.get(name, name)
        elif kind == "session.tool.called" and turn.tools.get(call_id) == SUBAGENT_TOOL:
            arguments = data.get("input") or {}
            turn.subagents[call_id] = str(arguments.get(SUBAGENT_TYPE_ARGUMENT) or "")
        elif kind == "session.tool.progress":
            _remember_child(turn, call_id, data.get("metadata"))
        elif kind == "session.tool.success":
            child = _background_child(data.get("metadata")) if call_id in turn.subagents else ""
            if child:
                turn.children[child] = call_id

    async def _mount(self) -> None:
        """Mount the tool server in OpenCode and wait until its tools are advertised.

        The mount is the wire one. It does not survive a restart of OpenCode,
        and a mount made by the server configuration would: but that one stays
        failed when Belle starts second, needs the same experimental path to
        connect, and cannot carry a token this process makes. A new mount
        replaces the last and makes OpenCode read the tool list again.
        """
        assert self._tool_server is not None
        config = {
            "type": "remote",
            "url": self._tools_url or f"http://127.0.0.1:{self._tool_server.port}{TOOLS_PATH}",
            "oauth": False,
            # Advertise each tool by name. The default reaches them through one `execute` tool.
            "codemode": False,
            "headers": {"Authorization": f"Bearer {self._tool_server.token}"},
        }
        await self._wire.call("PUT", f"/api/experimental/mcp/{MCP_SERVER}", {"config": config})
        asked_again = False
        try:
            async with asyncio.timeout(MOUNT_TIMEOUT_SECONDS):
                while (status := await self._mount_status()).get("status") != "connected":
                    if status.get("status") != "failed":
                        await asyncio.sleep(READY_POLL_SECONDS)
                    elif asked_again:
                        reason = status.get("error")
                        raise OpenCodeError(f"OpenCode cannot reach the tool server at {config['url']}: {reason}")
                    else:
                        # A mount that failed stays failed, and the configuration it failed with changes
                        # nothing when it is put again. OpenCode tries once more when asked to connect.
                        asked_again = True
                        await self._wire.call("POST", f"/api/experimental/mcp/{MCP_SERVER}/connect")
        except TimeoutError:
            raise OpenCodeError(f"OpenCode did not connect to the tool server at {config['url']}") from None
        await asyncio.sleep(MOUNT_SETTLE_SECONDS)

    async def _belle_mcp_entry(self) -> Mapping[str, Any] | None:
        """Return Belle's MCP server entry for health probes.

        Raises TypeError when the body is not a list of objects with string
        names. Returns None when the listing is valid and Belle is absent.
        """
        servers = await self._wire.call("GET", "/api/mcp")
        if not isinstance(servers, list):
            raise TypeError("GET /api/mcp must return a list")
        for server in servers:
            if not isinstance(server, Mapping):
                raise TypeError("GET /api/mcp list items must be objects")
            name = server.get("name")
            if not isinstance(name, str):
                raise TypeError("GET /api/mcp server name must be a string")
            if name == MCP_SERVER:
                return server
        return None

    async def _mount_status(self) -> Mapping[str, Any]:
        for server in await self._wire.call("GET", "/api/mcp") or ():
            if server.get("name") == MCP_SERVER:
                return server.get("status") or {}
        return {}

    async def _ensure_mount(self) -> None:
        async with self._mount_lock:
            if (await self._mount_status()).get("status") != "connected":
                logger.warning("opencode: Belle tools disappeared; mounting them again")
                await self._mount()

    async def _conversation_of(self, meta: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        """Name the conversation of a tool call from the session OpenCode names in ``_meta``. No header names it."""
        session = str(meta.get("ai.opencode/sessionID") or "")
        for _ in range(MAX_SESSION_DEPTH):
            turn = self._turns.get(session)
            if turn is not None:
                return turn.conversation
            if not session:
                break
            try:
                parent = await self._wire.call("GET", f"/api/session/{urllib.parse.quote(session, safe='')}")
            except (WireError, httpx.HTTPError):
                break
            session = str((parent or {}).get("parentID") or "")
        return ""

    async def _instruct(self, session: str, additions: Sequence[str]) -> None:
        """Make a turn's system additions the session's instruction entry, or remove the entry.

        No plugin is loaded, so nothing edits the system message of one request.
        An entry set before a session's first turn is in the system message of
        every request of the session. One set or changed later reaches the model
        as a ``user`` role ``<system-update>`` message stored in the history, and
        a removed one as a stored notice that it no longer applies: the earlier
        text stays in the history before that notice. A value equal to the
        entry's adds nothing, so a conversation whose additions do not change
        pays for them once.
        """
        path = f"/api/experimental/session/{session}/instructions/entries/{INSTRUCTION_KEY}"
        if additions:
            await self._wire.call("PUT", path, {"value": "\n\n".join(additions)})
        else:
            await self._wire.call("DELETE", path)

    def _advertised(self, name: str) -> str | None:
        """The name OpenCode advertises one of Belle's own tools under, ``belle_<name>``, and nothing for another."""
        return self.capabilities().tool_name(name) if name in self._belle_tools else None

    async def _set_permissions(self, session: str, scope: TurnScope) -> None:
        """Set the session's permission ruleset for this turn. An empty ruleset restricts nothing.

        ``deny`` denies the tool and drops it from the roster. ``ask`` makes OpenCode's permission
        engine raise a ``permission.asked`` a person decides, so no decision waits inside a tool call.
        A skill the scope does not allow is denied: it leaves the model's list of skills, and loading it fails.
        """
        tools = scope.tools
        levels = {name: LEVEL_DENY for name in self._deny_tools if name not in tools}
        rules = permission_rules(
            {**levels, **tools}, self._advertised, scope.skills_available, scope.skills_unavailable
        )
        await self._wire.call("PATCH", f"/api/session/{session}", {"permissions": rules})

    async def _serve_with(self, session: str, current: Mapping[str, Any], model: str) -> None:
        """Make the session's model this turn's: the scope's ``providerID/id`` name, or the server's default.

        OpenCode keeps a switch for every later turn of the session, so a turn
        whose scope names no model switches back. ``current`` is the model the
        session reports, which is nothing until its first switch. The session
        switch is used, not a raw request field, so OpenCode's own accounting
        and limits follow the turn's model. A name with no provider names no
        model OpenCode knows, and the default serves the turn.
        """
        provider, _, identifier = model.partition("/")
        wanted = (provider, identifier) if provider and identifier else self._default_model
        serving = (current.get("providerID"), current.get("id")) if current else self._default_model
        if wanted != serving:
            body = {"model": {"providerID": wanted[0], "id": wanted[1]}}
            await self._wire.call("POST", f"/api/session/{session}/model", body)

    async def _ran(self, session: str, prompt_id: str) -> bool:
        """Say whether the session already holds this prompt.

        OpenCode answers 200 to a second prompt with a known message id and runs nothing for it,
        so nothing in its answer or its events tells the two apart.
        """
        try:
            await self._wire.call("GET", f"/api/session/{session}/message/{prompt_id}")
        except WireError as exc:
            if exc.status != 404:
                raise
            return False
        return True

    async def peek_history(self, conversation: str, request_id: str) -> Sequence[PeekHistoryPart]:
        """Seed a mid-turn peek from in-memory answers and stored session messages.

        Live ``session.text.delta`` events are not replayed by OpenCode; completed
        step text and whatever the session has stored for this prompt are what
        catch-up can offer. Active child sessions are read the same way: their
        deltas share the no-replay stream, so stored child assistant text is the
        catch-up for a subagent that was already writing when peek opened.
        """
        session = session_id(self._namespace, conversation)
        prompt_id = message_id(self._namespace, conversation, request_id)
        turn = self._turns.get(session)
        parts: list[PeekHistoryPart] = []
        if turn is not None and turn.prompt_id == prompt_id:
            for text in _written(turn):
                if text.strip():
                    parts.append(PeekHistoryPart(kind="text", text=text))
            await self._append_child_peek_history(turn, parts)
            if parts:
                return tuple(parts)
        try:
            newer = await self._messages_since_prompt(session, prompt_id)
        except (WireError, ValueError, TypeError):
            if turn is not None and turn.prompt_id == prompt_id:
                await self._append_child_peek_history(turn, parts)
            return tuple(parts)
        for text in _written(_replay(newer)):
            if text.strip():
                parts.append(PeekHistoryPart(kind="text", text=text.strip()))
        if turn is not None and turn.prompt_id == prompt_id:
            await self._append_child_peek_history(turn, parts)
        return tuple(parts)

    async def _append_child_peek_history(self, turn: _Turn, parts: list[PeekHistoryPart]) -> None:
        """Append stored assistant text from each routed child session, attributed by parent call id."""
        for child, call_id in list(turn.children.items()):
            try:
                for text in await self._child_assistant_texts(child):
                    parts.append(PeekHistoryPart(kind="text", text=text, call_id=call_id))
            except (WireError, ValueError, TypeError, httpx.HTTPError):
                continue

    async def _child_assistant_texts(self, child: str) -> list[str]:
        """Assistant step text stored on a child session (oldest first), for peek catch-up."""
        path = f"/api/session/{urllib.parse.quote(child, safe='')}/message?limit={RECONCILE_PAGE}&order=desc"
        response = await self._wire.call("GET", path, unwrap=False)
        batch = response.get("data") or []
        return [text.strip() for text in _written(_replay(batch[::-1])) if text.strip()]

    async def _messages_since_prompt(self, session: str, prompt_id: str) -> list[Mapping[str, Any]]:
        """List session messages after the turn's prompt, oldest first."""
        try:
            prompt = await self._wire.call("GET", f"/api/session/{session}/message/{prompt_id}")
        except WireError as exc:
            if exc.status == 404:
                return []
            raise
        since = (prompt.get("time") or {}).get("created", 0)
        page: list[Mapping[str, Any]] = []
        cursor = ""
        while True:
            path = f"/api/session/{session}/message?limit={RECONCILE_PAGE}"
            path += f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else "&order=desc"
            response = await self._wire.call("GET", path, unwrap=False)
            batch = response.get("data") or []
            page.extend(batch)
            if any(message.get("id") == prompt_id for message in batch):
                break
            if len(batch) < RECONCILE_PAGE:
                break
            cursor = (response.get("cursor") or {}).get("next") or ""
            if not cursor:
                break
        return [
            message
            for message in reversed(page or ())
            if message.get("id") != prompt_id and (message.get("time") or {}).get("created", 0) >= since
        ]

    async def _reconcile(self, session: str, turn: _Turn, *, terminal_seen: bool = False) -> TurnEvent | None:
        """Read from a session what its turn did while no event could be read.

        Return the turn's terminal event if it ended, and nothing if it still
        runs. An execution that is neither running nor ended was lost with the
        OpenCode process that ran it: OpenCode does not take it up again. So
        was a child that neither runs nor has reported, and the turn does not
        wait for it. A terminal event can arrive before the active list clears.
        """
        turn.gapped = True
        for attempt in range(2):
            active = await self._wire.call("GET", "/api/session/active") or {}
            try:
                prompt = await self._wire.call("GET", f"/api/session/{session}/message/{turn.prompt_id}")
            except WireError as exc:
                if exc.status != 404:
                    raise
                # The prompt waits in the session's inbox. Its delivery will be seen.
                return None
            turn.delivered = True
            since = (prompt.get("time") or {}).get("created", 0)
            page: list[Mapping[str, Any]] = []
            cursor = ""
            while True:
                path = f"/api/session/{session}/message?limit={RECONCILE_PAGE}"
                path += f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else "&order=desc"
                response = await self._wire.call("GET", path, unwrap=False)
                batch = response.get("data") or []
                page.extend(batch)
                if any(message.get("id") == turn.prompt_id for message in batch):
                    break
                if len(batch) < RECONCILE_PAGE:
                    raise ValueError("OpenCode did not list the turn's prompt")
                cursor = (response.get("cursor") or {}).get("next") or ""
                if not cursor:
                    raise ValueError("OpenCode omitted the cursor before the turn's prompt")
            newer = [
                message
                for message in reversed(page or ())
                if message.get("id") != turn.prompt_id and (message.get("time") or {}).get("created", 0) >= since
            ]
            # The turn goes on from what its session stored, and the events that follow continue it.
            _replay(newer, turn)
            unreported = _unreported(newer)
            turn.children = {child: call for child, call in unreported.items() if child in active}
            turn.inbox.clear()
            # An execution ends with an `idle` message that says how. One that failed or was stopped ends the turn.
            for index, message in enumerate(newer):
                if message.get("type") == "idle" and message.get("outcome") != "succeeded":
                    return _in_session(session, stored(newer[: index + 1], str(message.get("outcome") or "")))
            if turn.children:
                return None
            # The session runs once more for each child that reports, so its last message tells whether it is done.
            done = bool(newer) and newer[-1].get("type") == "idle"
            if session in active and not terminal_seen:
                return None
            if done and len(turn.children) == len(unreported):
                return _in_session(session, stored(newer, "succeeded"))
            # A child that neither runs nor has reported may be about to report: it is given a moment.
            if done and attempt:
                return _in_session(session, stored(newer, "succeeded"))
            if not attempt:
                await asyncio.sleep(RECONCILE_SETTLE_SECONDS)
        return _in_session(session, TurnEvent(EVENT_FAILED, text=EXECUTION_LOST))

    async def _existing(self, conversation: str) -> dict[str, Any] | None:
        session = session_id(self._namespace, conversation)
        try:
            found = await self._wire.call("GET", f"/api/session/{session}")
        except WireError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(found, dict) or found.get("id") != session:
            return None
        metadata = found.get("metadata")
        belle = metadata.get("belle") if isinstance(metadata, Mapping) else None
        if not isinstance(belle, Mapping) or belle.get("conversation") != conversation:
            return None
        return found

    async def retained(self, conversation: str) -> bool:
        return await self._existing(conversation) is not None

    async def forget(self, conversation: str) -> None:
        """Delete the conversation's OpenCode session, its child sessions with it. A missing one counts as deleted."""
        session = session_id(self._namespace, conversation)
        if session in self._turns or session in (await self._wire.call("GET", "/api/session/active") or {}):
            raise still_running(conversation)
        try:
            await self._wire.call("DELETE", f"/api/session/{session}")
        except WireError as exc:
            if exc.status != 404:
                raise

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Run one turn: one prompt, and every execution of the session until its work is complete.

        OpenCode ends an execution while subagents started with ``background``
        still run, and runs the session once more for the report of each. The
        turn ends with the execution that answers the last report, and its
        final is every answer in order.
        """
        session = session_id(self._namespace, request.conversation)
        if session in self._turns:
            # The host queues. A second prompt here would join the first one's execution.
            yield TurnEvent(EVENT_FAILED, text=ALREADY_RUNNING)
            return
        prompt: dict[str, Any] = {
            "id": message_id(self._namespace, request.conversation, request.request_id),
            "text": request.text,
        }
        turn = self._turns[session] = _Turn(
            conversation=request.conversation,
            prompt_id=prompt["id"],
            scope=request.scope,
            belle_tools={self.capabilities().tool_name(name): name for name in self._belle_tools},
        )
        if self._relay is not None:
            self._relay.watch_session(session)
        # The execution lives in the server: a reader that leaves does not stop it.
        # Once the prompt is sent, only a terminal event from OpenCode says nothing is left to stop.
        stop = left = opened = False
        try:
            if request.files:
                prompt["files"] = attachments(request.files)
            await self._subscribed.wait()
            if request.existing_conversation_only:
                created = await self._existing(request.conversation)
                if created is None:
                    yield TurnEvent(EVENT_FAILED, text=CONVERSATION_UNAVAILABLE)
                    return
            else:
                # A second create with a known id answers with the session it names.
                metadata = {"belle": {"conversation": request.conversation}}
                created = await self._wire.call("POST", "/api/session", {"id": session, "metadata": metadata})
            opened = True
            turn.cost_before = float((created or {}).get("cost") or 0)
            before_in, before_out = _session_token_totals((created or {}).get("tokens"))
            turn.tokens_in_before = before_in or 0
            turn.tokens_out_before = before_out or 0
            if await self._ran(session, prompt["id"]):
                ended = TurnEvent(EVENT_FAILED, text=ALREADY_RAN, data={DUPLICATE: True})
            else:
                await self._set_permissions(session, request.scope)
                # With the plugin loaded the system additions travel through the context hook; without it,
                # through the session instruction entry. One path per mode, so the model never sees them twice.
                if not self._plugin:
                    await self._instruct(session, request.scope.system_additions)
                if request.scope.skills_required:
                    prompt["skills"] = [{"id": name} for name in request.scope.skills_required]
                await self._serve_with(session, (created or {}).get("model") or {}, request.scope.model)
                await self._ensure_mount()
                stop = turn.posted = True
                await self._wire.call("POST", f"/api/session/{session}/prompt", prompt)
                yield TurnEvent(EVENT_ACCEPTED)
            while stop:
                event = await turn.events.get()
                if event is _CANCELLED:
                    ended = TurnEvent(EVENT_INTERRUPTED)
                    break
                ended = await self._reconcile(session, turn) if event is _RESUBSCRIBED else _in_session(session, translate(event, turn))
                if ended is None:
                    if event is _RESUBSCRIBED:
                        # The ask events are not replayed. The open ones are read back and re-relayed; the
                        # host does not ask the platform twice for a question it already relayed.
                        for question in await self.pending(request.conversation):
                            yield TurnEvent(EVENT_QUESTION, question=question)
                    continue
                if ended.terminal:
                    if turn.gapped and event is not _RESUBSCRIBED:
                        # The text of a step that ended unseen is not in what was read. The session has it.
                        ended = await self._reconcile(session, turn, terminal_seen=True) or ended
                    # An execution that failed or was stopped ends the turn while its children run. They are stopped.
                    stop = bool(turn.children)
                    break
                yield ended
        except (GeneratorExit, asyncio.CancelledError):
            left = True
            raise
        except (WireError, httpx.HTTPError, OpenCodeError, ValueError) as exc:
            # A server error on the prompt is how a relay hook that failed shows. The cause is named, not the status alone.
            trouble = await self._relay_trouble(session, turn.began) if isinstance(exc, WireError) and exc.status >= 500 and turn.posted else ""
            logger.warning(
                "opencode: turn could not run: conversation=%s reason=%s%s",
                request.conversation, exc, f". {trouble}" if trouble else "",
            )
            ended = TurnEvent(EVENT_FAILED, text=f"The turn could not run on OpenCode: {exc}" + (f". {trouble}" if trouble else ""))
            if opened:
                ended = _in_session(session, ended)
        finally:
            try:
                # A reader that leaves as the host stops leaves the turn running. The children of an ended turn still stop.
                if stop and not (left and self._shutting_down):
                    await self._stop(session, turn)
            finally:
                if self._relay is not None:
                    self._relay.unwatch_session(session)
                del self._turns[session]
        yield ended

    async def _stop(self, session: str, turn: _Turn) -> None:
        """Interrupt a session and the children its turn left running, and wait until OpenCode says all of it ended.

        An interrupt of the session does not reach a child that runs in the
        background. A child that is interrupted reports it to the session, and
        OpenCode runs the session once more for that report: that execution is
        interrupted too.
        """
        interrupted = [await self._interrupt(running) for running in (session, *turn.children)]
        if not any(interrupted) or not self._subscribed.is_set():
            return
        try:
            async with asyncio.timeout(STOP_TIMEOUT_SECONDS):
                while True:
                    event = await turn.events.get()
                    if event is _RESUBSCRIBED:
                        return
                    kind = str(event.get("type", ""))
                    translate(event, turn)
                    if kind == "session.execution.started":
                        await self._interrupt(session)
                    elif kind.startswith("session.execution.") and not (turn.children or turn.inbox):
                        return
        except TimeoutError:
            logger.warning("opencode: session %s did not report that all its interrupted work ended", session)

    async def _interrupt(self, session: str) -> bool:
        try:
            answer = await self._wire.call("POST", f"/api/session/{session}/interrupt", {})
        except (WireError, httpx.HTTPError) as exc:
            logger.warning("opencode: interrupt failed: session=%s reason=%s", session, exc)
            return False
        return bool(isinstance(answer, Mapping) and answer.get("interrupted"))

    def shutting_down(self) -> None:
        self._shutting_down = True

    async def cancel(self, conversation: str) -> bool:
        session = session_id(self._namespace, conversation)
        turn = self._turns.get(session)
        if turn is not None and turn.children:
            # The session may be idle while a child runs, and an interrupt of it stops no child. The turn's
            # reader stops all of it.
            turn.events.put_nowait(_CANCELLED)
            return True
        return await self._interrupt(session)

    async def ask(self, conversation: str, question: Question) -> str:
        """Put a question Belle invents, as a form on the wire. Return its id.

        A form holds no provider request of its own. The reply reaches the turn
        through :meth:`answer`.
        """
        session = session_id(self._namespace, conversation)
        field: dict[str, Any] = {"key": "answer", "type": "string", "title": question.text or "Answer"}
        if question.options:
            field["options"] = [{"value": option, "label": option} for option in question.options]
        form = await self._wire.call(
            "POST", f"/api/session/{session}/form", {"title": question.text or "Question", "fields": [field]}
        )
        return str((form or {}).get("id") or "")

    async def answer(self, conversation: str, question_id: str, answer: Answer) -> None:
        """Deliver a person's answer: a decision on a permission, or a reply to a form."""
        session = session_id(self._namespace, conversation)
        if question_id.startswith(PERMISSION_ID_PREFIX):
            decision = "once" if approves(answer) else "reject"
            await self._wire.call("POST", f"/api/session/{session}/permission/{question_id}/reply", {"decision": decision})
        elif question_id.startswith(FORM_ID_PREFIX):
            value = answer.values[0] if answer.values else ("yes" if approves(answer) else "no")
            await self._wire.call(
                "POST", f"/api/session/{session}/form/{question_id}/reply",
                {"answer": {await self._form_key(session, question_id): value}},
            )

    async def refuse_question(self, conversation: str, question_id: str, reason: str) -> None:
        """Cancel a form, or reject a permission, with ``reason`` as the message its asker reads."""
        session = session_id(self._namespace, conversation)
        if question_id.startswith(PERMISSION_ID_PREFIX):
            await self._wire.call(
                "POST", f"/api/session/{session}/permission/{question_id}/reply", {"decision": "reject", "message": reason}
            )
        elif question_id.startswith(FORM_ID_PREFIX):
            query = urllib.parse.urlencode({"message": reason})
            await self._wire.call("DELETE", f"/api/session/{session}/form/{question_id}?{query}")

    async def _form_key(self, session: str, form_id: str) -> str:
        """The key of a form's first field, or ``answer`` for a form this adapter built."""
        for form in await self._wire.call("GET", f"/api/session/{session}/form") or ():
            if form.get("id") == form_id:
                fields = form.get("fields") or []
                return str(fields[0].get("key")) if fields else "answer"
        return "answer"

    async def pending(self, conversation: str) -> list[Question]:
        """List what a conversation's session waits on a person for.

        The events that announce an ask are not replayed, so after a time
        with no event stream this is the only way to learn of one.
        """
        session = session_id(self._namespace, conversation)
        try:
            permissions = await self._wire.call("GET", f"/api/session/{session}/permission")
            forms = await self._wire.call("GET", f"/api/session/{session}/form")
        except WireError as exc:
            if exc.status != 404:
                raise
            return []
        questions = [
            Question(
                id=str(request.get("id") or ""),
                text=f"Allow {request.get('action')}: {', '.join(map(str, request.get('resources') or ()))}",
                options=(APPROVE, REJECT),
                kind=APPROVAL,
                tool=str(request.get("action") or ""),
            )
            for request in permissions or ()
        ]
        questions += [Question(id=str(form.get("id") or ""), text=str(form.get("title") or "")) for form in forms or ()]
        return questions

    async def active(self) -> list[str]:
        """List this deployment's conversations whose session OpenCode is executing, whoever started it."""
        conversations = []
        for session in await self._wire.call("GET", "/api/session/active") or {}:
            try:
                found = await self._wire.call("GET", f"/api/session/{urllib.parse.quote(session, safe='')}")
            except WireError:
                continue
            conversation = (((found or {}).get("metadata") or {}).get("belle") or {}).get("conversation")
            # The id is derived from the namespace, so the session of another deployment does not pass.
            if isinstance(conversation, str) and session_id(self._namespace, conversation) == session:
                conversations.append(conversation)
        return conversations

    async def recover(self, request: TurnRequest) -> Recovery:
        """Read a past turn from OpenCode without sending its prompt again."""
        session = session_id(self._namespace, request.conversation)
        prompt_id = message_id(self._namespace, request.conversation, request.request_id)
        turn = self._turns.get(session)
        if turn is not None and turn.prompt_id != prompt_id:
            raise OpenCodeError(f"another request is bound to conversation {request.conversation!r}")
        active = session in (await self._wire.call("GET", "/api/session/active") or {})
        if not await self._ran(session, prompt_id):
            if active:
                raise OpenCodeError(
                    f"cannot recover request {request.request_id!r}: the conversation is active, but its prompt is absent"
                )
            self._turns.pop(session, None)
            return Recovery(RECOVERY_NOT_SUBMITTED)
        if turn is None:
            turn = _Turn(
                conversation=request.conversation,
                prompt_id=prompt_id,
                scope=request.scope,
                posted=True,
                delivered=True,
                belle_tools={self.capabilities().tool_name(name): name for name in self._belle_tools},
            )
            self._turns[session] = turn
        else:
            turn.scope = request.scope
        if active:
            await self._set_permissions(session, request.scope)
        result = await self._reconcile(session, turn)
        if result is None:
            # The event stream is also reading this session. Recovery uses the
            # stored messages, so its queued progress events are not replayed.
            while not turn.events.empty():
                turn.events.get_nowait()
            return Recovery(RECOVERY_RUNNING)
        self._turns.pop(session, None)
        return Recovery(RECOVERY_TERMINAL, result)

    async def catalog(self) -> Catalog:
        """List what OpenCode offers: its enabled models, its skills and its subagents.

        A model is named ``provider/id``, which is the name a scope gives and
        ``_serve_with`` reads. The default model comes first. A skill is named
        by its id, which is what a prompt attaches. The skills are those of
        OpenCode's own skill directories, its two built-in ones included. A
        subagent is an agent of mode ``subagent`` that is not hidden: the model
        reaches one through the ``subagent`` tool, as a child session.
        """
        default = "/".join(self._default_model) if all(self._default_model) else ""
        models = [
            f"{model['providerID']}/{model['id']}"
            for model in await self._wire.listing("/api/model", "providerID", "id", "enabled")
            if model["providerID"] and model["id"] and model["enabled"] is not False
        ]
        skills = await self._wire.listing("/api/skill", "id", "description")
        agents = await self._wire.listing("/api/agent", "id", "mode", "hidden")
        return Catalog(
            skills=tuple(
                {"name": str(skill["id"]), "description": str(skill["description"] or ""), "installed": True}
                for skill in skills
                if skill["id"]
            ),
            subagents=tuple(str(agent["id"]) for agent in agents if agent["id"] and agent["mode"] == "subagent" and not agent["hidden"]),
            models=ConfiguredModels(names=tuple(dict.fromkeys(([default] if default else []) + models))),
        )

    async def close(self) -> None:
        """Stop reading events, unmount the tools and close the listeners. A second call does nothing."""
        pump, self._pump_task = self._pump_task, None
        if pump is not None:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        server, self._tool_server = self._tool_server, None
        if server is not None:
            # Tools left advertised would be called and fail. The mount is by name, so it is removed by name.
            with contextlib.suppress(WireError, httpx.HTTPError):
                await self._wire.call("DELETE", f"/api/experimental/mcp/{MCP_SERVER}")
            await server.close()
        relay, self._relay = self._relay, None
        if relay is not None:
            await relay.close()
        await self._wire.close()
