"""Serve toolkits to a backend, each call bound to its conversation's turn."""

import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from belle_agent.backend import (
    APPROVAL,
    VERDICT_ALLOW,
    VERDICT_ASK,
    Answer,
    Question,
    ToolCall,
    ToolContent,
    ToolVerdict,
)
from belle_agent.scopes import LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY

logger = logging.getLogger(__name__)

_REQUEST_METADATA: ContextVar[Mapping[str, Any] | None] = ContextVar("belle_request_metadata", default=None)
_ACTIVE_TURN: ContextVar[Callable[[], bool] | None] = ContextVar("belle_active_turn", default=None)
_ACTIVE_CALL: ContextVar[tuple[tuple[str, str], ToolCall] | None] = ContextVar("belle_active_tool_call", default=None)

APPROVE = "approve"
REJECT = "reject"


def approves(answer: Answer) -> bool:
    """Whether a person's answer approves. An approval verdict wins; otherwise the option named."""
    return answer.approved if answer.approved is not None else APPROVE in answer.values

#: What a call answers with that names a tool of other platforms than its turn's.
TOOL_OF_ANOTHER_PLATFORM = "tool_of_another_platform"

#: What a held call answers with, in place of a result. The model reads the detail.
AWAITING_CONFIRMATION = "awaiting_confirmation"
HELD_CALL_DETAIL = (
    " A person in this conversation has been asked. If they agree, this call is made once, exactly as"
    " given, when they answer. If they refuse, nothing is sent. Nothing has been sent yet. Do not make"
    " this call again and do not wait for the answer: say that it awaits a person's confirmation. This"
    " conversation's next turn is told what was decided."
)


def request_metadata() -> dict[str, Any]:
    """Return the metadata of the tool call in flight, or nothing outside one."""
    return dict(_REQUEST_METADATA.get() or {})


def active_call() -> tuple[tuple[str, str], ToolCall] | None:
    """Return the active turn and call identity, or nothing outside a call with an ID."""
    return _ACTIVE_CALL.get()


def active_turn_alive() -> bool:
    """Check the active-only call's turn, or allow calls without an active-turn constraint."""
    check = _ACTIVE_TURN.get()
    return check is None or check()


@dataclass
class BoundTurn:
    """What the tool host holds for one running turn."""

    request_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Tool names to ``ask`` or ``deny``, as ``TurnScope.tools``.
    tools: Mapping[str, str] = field(default_factory=dict)
    #: The media types this turn's model accepts; None uses the backend default.
    content_types: frozenset[str] | None = None
    #: A posting-tool call returned an accepted message ID during this turn.
    delivered: bool = False
    #: Messages a posting tool delivered, in order.
    posted: list[dict[str, str]] = field(default_factory=list)
    #: The turn is being stopped. What it was running may not have noticed yet.
    stopped: bool = False
    #: The platform the turn came from. Its tools, and the tools every platform shares, are the turn's.
    platform: str = ""


#: Puts a toolkit's question about one call to a person, and holds the call until they answer.
#: Returns whether somebody was asked. The call then runs through ``ToolHost.run_confirmed``, or not at all.
Confirm = Callable[[ToolCall, "BoundTurn", Question], Awaitable[bool]]


def refusal(code: str, detail: str) -> str:
    """Report a refused call in the shape the platform toolkits use."""
    return json.dumps({"ok": False, "error": code, "detail": detail}, ensure_ascii=False)


def result_text(result: Any) -> Any:
    """The text of a tool result, which is the result itself unless it carries content parts."""
    return result.text if isinstance(result, ToolContent) else result


def _refused(result: Any) -> Mapping[str, Any] | None:
    """Read a tool result that is a refusal in the shape the toolkits report one, or nothing."""
    result = result_text(result)
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return None
    return parsed if isinstance(parsed, Mapping) and parsed.get("ok") is False else None


def succeeded(result: Any) -> bool:
    """Whether a tool result is not a refusal in the shape the toolkits report one."""
    return _refused(result) is None


def held(result: Any) -> bool:
    """Whether a tool result says its call waits for a person's answer. Such a call has not failed."""
    return (_refused(result) or {}).get("error") == AWAITING_CONFIRMATION


def _posted_records(result: Any, call: ToolCall, turn: BoundTurn) -> list[dict[str, str]]:
    """Read the message ids returned by a posting tool, in the form that ``Platform.posting_tool`` states."""
    try:
        data = json.loads(result_text(result))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, Mapping):
        return []
    conversation = data.get("chat_id")
    first = data.get("message_id")
    if not isinstance(conversation, str) or not isinstance(first, str) or not first:
        return []
    parts = data.get("part_message_ids")
    if parts is not None and (
        not isinstance(parts, list) or not parts or parts[0] != first
        or any(not isinstance(item, str) or not item for item in parts)
    ):
        return []
    ids = parts if parts is not None else [first]
    count = data.get("parts")
    if count is not None and (type(count) is not int or count != len(ids)):
        return []
    if "thread_id" in data and not isinstance(data["thread_id"], str):
        return []
    placements = data.get("part_placements")
    if placements is not None:
        if not isinstance(placements, list) or len(placements) != len(ids):
            return []
        records = []
        for message_id, placement in zip(ids, placements, strict=True):
            if not isinstance(placement, Mapping):
                return []
            chat_id, actual_id, thread_id = (placement.get(key) for key in ("chat_id", "message_id", "thread_id"))
            if not isinstance(chat_id, str) or not chat_id or actual_id != message_id or not isinstance(thread_id, str):
                return []
            records.append({"conversation": chat_id, "message_id": message_id, "thread": thread_id})
        if records[0]["conversation"] != conversation:
            return []
        return records
    place = turn.metadata.get("belle_place")
    origin_thread = place.get("thread", "") if isinstance(place, Mapping) else ""
    thread = (
        data["thread_id"] if isinstance(data.get("thread_id"), str)
        else call.arguments.get("reply_to_message_id") or origin_thread
    )
    return [
        {"conversation": conversation, "message_id": message_id, "thread": str(thread if index == 0 else thread or first)}
        for index, message_id in enumerate(ids)
    ]


class ToolHost:
    """The core's side of ``backend.ToolHost`` over any list of toolkits.

    ``toolkits`` serve every turn. ``serve`` adds the toolkits of one platform, which serve only the turns
    of that platform. Two platforms may each serve a tool of the same name: a call runs the one of its turn's platform.
    """

    def __init__(
        self, toolkits: Sequence[Any], *, posting_tool: str = "", posting_tools: Sequence[str] = (),
        confirm: Confirm | None = None,
    ) -> None:
        self._confirm = confirm
        self._active_only: dict[str, set[str]] = {}
        #: Platform name to each tool it serves, as ``(definition, method, asker)``. ``""`` holds the shared tools.
        self._served: dict[str, dict[str, tuple[dict[str, Any], Any, Any]]] = {}
        #: Platform name to the tools whose message receipts count as delivery.
        self._posting_tools: dict[str, frozenset[str]] = {}
        self._turns: dict[str, BoundTurn] = {}
        #: The media types of content the backend hands to the model, as its capabilities name them.
        self.content_types: frozenset[str] = frozenset()
        self.serve("", toolkits, posting_tool=posting_tool, posting_tools=posting_tools)

    def serve(
        self, platform: str, toolkits: Sequence[Any], *, posting_tool: str = "", posting_tools: Sequence[str] = (),
    ) -> None:
        """Serve the toolkits of one platform to the turns of that platform."""
        served = self._served.setdefault(platform, {})
        active_only = self._active_only.setdefault(platform, set())
        self._posting_tools[platform] = frozenset(name for name in (posting_tool, *posting_tools) if name)
        for toolkit in toolkits:
            asker = getattr(toolkit, "widening_question", None)
            for definition in toolkit.get_tool_definitions():
                name = definition["name"]
                if name in served:
                    logger.warning("tool host: %s is defined twice; keeping the first definition", name)
                    continue
                if platform and name in self._served[""]:
                    logger.warning("tool host: %s is defined twice; %s serves its own to its turns", name, platform)
                served[name] = (definition, getattr(toolkit, name), asker)
                if name in getattr(toolkit, "active_turn_only", ()):
                    active_only.add(name)

    def requires_active_turn(self, platform: str, name: str) -> bool:
        """Whether a tool must finish before its originating turn ends."""
        owner = platform if name in self._served.get(platform, {}) else ""
        return name in self._active_only.get(owner, set())

    def _tool(self, platform: str, name: str) -> tuple[dict[str, Any], Any, Any] | None:
        """Find the tool a turn of ``platform`` calls by ``name``: its platform's, or a shared one."""
        return self._served.get(platform, {}).get(name) or self._served[""].get(name)

    def foreign(self, platform: str) -> frozenset[str]:
        """Name each tool that only other platforms serve, so that a turn of ``platform`` cannot call it."""
        own = self._served.get(platform, {}).keys() | self._served[""].keys()
        return frozenset(name for served in self._served.values() for name in served) - own

    def _elsewhere(self, call: ToolCall, turn: BoundTurn) -> str:
        """Say why a call names a tool of other platforms only, or nothing."""
        if self._tool(turn.platform, call.name) is not None:
            return ""
        others = sorted(name for name, served in self._served.items() if name and call.name in served)
        if not others:
            return ""
        return (
            f"{call.name} is a tool of {', '.join(others)}, and this conversation's turn runs on"
            f" {turn.platform or 'no platform'}. Call the tools of this turn's own list"
        )

    def bind(self, conversation: str, turn: BoundTurn) -> None:
        """Open a conversation to tool calls for the length of one turn."""
        self._turns[conversation] = turn

    def release(self, conversation: str, request_id: str) -> None:
        """Close a conversation to tool calls, unless a later turn now holds it."""
        turn = self._turns.get(conversation)
        if turn is not None and turn.request_id == request_id:
            del self._turns[conversation]

    def definitions(self, conversation: str) -> list[dict[str, Any]]:
        """List the tools a conversation's turn may call: those of its platform. Without a turn, every tool, each name once.

        The list is presentation. ``authorize`` is what refuses a call.
        """
        turn = self._turns.get(conversation)
        if turn is None:
            platforms = [*(name for name in self._served if name), ""]
        else:
            platforms = [turn.platform, ""]
        denied = {name for name, level in turn.tools.items() if level == LEVEL_DENY} if turn else set()
        listed: dict[str, dict[str, Any]] = {}
        for platform in platforms:
            for name, (definition, _, _) in self._served.get(platform, {}).items():
                listed.setdefault(name, definition)
        return [definition for name, definition in listed.items() if name not in denied]

    async def _toolkit_question(self, turn: BoundTurn, call: ToolCall) -> str:
        """Ask a call's toolkit what a person must be asked before the call runs, with the turn's metadata bound."""
        tool = self._tool(turn.platform, call.name)
        asker = tool[2] if tool is not None else None
        if asker is None:
            return ""
        token = _REQUEST_METADATA.set(turn.metadata)
        call_token = _ACTIVE_CALL.set(((turn.request_id, call.call_id), call) if call.call_id else None)
        try:
            return str(await asker(call.name, call.arguments) or "")
        except Exception:  # The tool makes the same check when it runs, and reports what failed in its own shape.
            logger.exception("tool host: the question of %s raised: conversation=%s", call.name, call.conversation)
            return ""
        finally:
            _ACTIVE_CALL.reset(call_token)
            _REQUEST_METADATA.reset(token)

    async def authorize(self, call: ToolCall) -> ToolVerdict:
        """Refuse a call outside a running turn, from a turn being stopped, or denied by the turn's scope.

        A call its scope allows is still a question where its toolkit says a person must agree to it first.
        """
        return (await self._decide(call))[0]

    async def _decide(self, call: ToolCall) -> tuple[ToolVerdict, bool]:
        """Decide one call, and say whether the verdict is a question of the call's own toolkit."""
        turn = self._turns.get(call.conversation)
        if turn is None:
            return ToolVerdict.deny("no turn is running for this conversation, so no tool call can belong to it"), False
        if turn.stopped:
            return ToolVerdict.deny("this conversation's turn is being stopped, so it makes no further tool call"), False
        elsewhere = self._elsewhere(call, turn)
        if elsewhere:
            return ToolVerdict.deny(elsewhere), False
        level = turn.tools.get(call.name, LEVEL_ALLOW)
        if level == LEVEL_DENY:
            return ToolVerdict.deny(f"{call.name} is denied in this conversation by its scope"), False
        toolkit_text = await self._toolkit_question(turn, call)
        if self._turns.get(call.conversation) is not turn or turn.stopped:
            return ToolVerdict.deny("the turn that requested this call ended or is being stopped"), False
        text = toolkit_text or (f"Allow {call.name} to run?" if level == LEVEL_ASK else "")
        if not text:
            return ToolVerdict.allow(), False
        question = Question(
            id=f"{turn.request_id}:{call.call_id or call.name}",
            text=text,
            options=(APPROVE, REJECT),
            kind=APPROVAL,
            tool=call.name,
        )
        return ToolVerdict.ask(question), bool(toolkit_text)

    async def call(self, call: ToolCall) -> "str | ToolContent":
        """Run one tool with its turn's metadata bound, or report the refusal.

        A call its toolkit puts a question about is not run and not kept open: the question goes to a
        person through ``confirm`` and the call answers at once that it waits. Where nobody can be
        asked, it is refused with the toolkit's sentence.
        """
        verdict, of_toolkit = await self._decide(call)
        if verdict.action == VERDICT_ASK and of_toolkit and self._confirm is not None:
            assert verdict.question is not None
            if await self._confirm(call, self._turns[call.conversation], verdict.question):
                logger.info("tool host holds %s for a person's answer: conversation=%s", call.name, call.conversation)
                return refusal(AWAITING_CONFIRMATION, verdict.message + HELD_CALL_DETAIL)
        if verdict.action != VERDICT_ALLOW:
            code = "approval_required" if verdict.action == VERDICT_ASK else "tool_denied"
            if call.conversation in self._turns and self._elsewhere(call, self._turns[call.conversation]):
                code = TOOL_OF_ANOTHER_PLATFORM
            logger.warning("tool host refused %s: conversation=%s reason=%s", call.name, call.conversation, code)
            return refusal(code, verdict.message)
        turn = self._turns[call.conversation]
        result = await self.run_confirmed(call, turn)
        if call.name in self._posting_tools.get(turn.platform, self._posting_tools[""]):
            posted = _posted_records(result, call, turn)
            if posted:
                turn.delivered = True
                turn.posted.extend(posted)
        types = turn.content_types if turn.content_types is not None else self.content_types
        return self._passable(call, result, types)

    def _passable(self, call: ToolCall, result: Any, content_types: frozenset[str]) -> Any:
        """Refuse a result with content the backend does not hand to the model, where the model would never see it."""
        parts = result.parts if isinstance(result, ToolContent) else ()
        # A media type is read without its parameters and in any case.
        unpassed = sorted({
            part.mime or "untyped"
            for part in parts
            if not any(fnmatchcase(part.mime.split(";", 1)[0].strip().lower(), kind) for kind in content_types)
        })
        if not unpassed:
            return result
        logger.warning("tool host held back the content of %s: conversation=%s types=%s", call.name, call.conversation, unpassed)
        taken = ", ".join(sorted(content_types)) or "none"
        refused = json.loads(refusal(
            "tool_content_not_passed",
            f"{call.name} read content of type {', '.join(unpassed)}. The model of this deployment is handed these"
            f" types of content from a tool result beside text: {taken}. The content was therefore not returned."
            " What the tool said about it is in result_without_content",
        ))
        return json.dumps({**refused, "result_without_content": result.text}, ensure_ascii=False)

    async def run_confirmed(self, call: ToolCall, turn: BoundTurn) -> "str | ToolContent":
        """Run a call that is allowed or that a person agreed to, with its turn's metadata. That turn may have ended."""
        if self.requires_active_turn(turn.platform, call.name) and (
            self._turns.get(call.conversation) is not turn or turn.stopped
        ):
            return refusal("turn_ended", f"{call.name} requires the turn that requested it to remain active")
        tool = self._tool(turn.platform, call.name)
        if tool is None:
            return refusal("unknown_tool", f"{call.name} is not a tool this deployment serves")
        method = tool[1]
        try:
            inspect.signature(method).bind(**call.arguments)
        except TypeError as exc:
            return refusal("invalid_arguments", str(exc))
        token = _REQUEST_METADATA.set(turn.metadata)
        call_token = _ACTIVE_CALL.set(((turn.request_id, call.call_id), call) if call.call_id else None)
        turn_token = _ACTIVE_TURN.set(
            (lambda: self._turns.get(call.conversation) is turn and not turn.stopped)
            if self.requires_active_turn(turn.platform, call.name) else None
        )
        try:
            return await method(**call.arguments)
        except Exception as exc:  # A tool failure is a result the model reads.
            logger.exception("tool host: %s raised: conversation=%s", call.name, call.conversation)
            return refusal("tool_failed", type(exc).__name__)
        finally:
            _ACTIVE_TURN.reset(turn_token)
            _ACTIVE_CALL.reset(call_token)
            _REQUEST_METADATA.reset(token)
