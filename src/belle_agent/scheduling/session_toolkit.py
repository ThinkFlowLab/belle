"""Shared tools for detached sessions: standalone and scheduled."""

import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import Any, Protocol, cast

from belle_agent.backend import OP_CONTINUE, TurnScope
from belle_agent.platforms.base import TRIGGER_DM, InboundMessage
from belle_agent.scheduling.address import Address, address_from_json
from belle_agent.scheduling.clock import Clock
from belle_agent.scheduling.clock import iso_utc as _iso
from belle_agent.scheduling.expiry import expires_iso_utc
from belle_agent.scheduling.records import (
    NO_DELETION,
    RUN_STATES,
    SESSION_ID,
    DetachedSession,
    RequestStart,
    fingerprint,
    new_run,
    new_session_id,
)
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.session_cards import session_cards
from belle_agent.scheduling.store import JobStore
from belle_agent.scheduling.tool_support import answer as _answer
from belle_agent.scheduling.tool_support import decode_cursor
from belle_agent.scheduling.tool_support import encode_cursor as _encode_cursor
from belle_agent.scheduling.tool_support import positive as _positive
from belle_agent.scheduling.tool_support import success as _success
from belle_agent.scheduling.toolkit import Caller, ScheduleToolkit
from belle_agent.scopes import LEVEL_DENY
from belle_agent.tools.host import active_call, refusal, request_metadata

_KINDS = frozenset({"standalone", "scheduled"})
_STARTED_BY = frozenset({"request", "clock"})
_NOT_TURNS = " The session tools serve detached sessions only."
_COMMAND: ContextVar[tuple[Caller, str, bool, dict[str, Any]] | None] = ContextVar("belle_session_command", default=None)
logger = logging.getLogger(__name__)


class SessionRunner(Protocol):
    async def request_session(self, session: DetachedSession) -> DetachedSession: ...
    async def continue_session(
        self, session_id: str, prompt: str, turn: dict[str, Any], chat_type: str, key: str,
    ) -> tuple[DetachedSession, Any]: ...
    async def cancel_session(self, session_id: str) -> dict[str, Any]: ...


def check_session_posting(posting_tool: str, scope: TurnScope, *, optional: bool) -> None:
    """Refuse detached work whose required delivery capability is unavailable."""
    if not posting_tool:
        raise Refused("capability_unavailable", "detached sessions need a platform posting tool")
    if not optional and scope.tools.get(posting_tool) == LEVEL_DENY:
        raise Refused("turn_out_of_scope", f"detached reply needs {posting_tool}, which current tool scope denies")


def _id(value: Any, field: str = "session_id") -> str:
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise Refused("invalid_arguments", f"{field} must be ses- and 10 lowercase letters and digits")
    return value


def _choices(raw: Any, field: str, allowed: frozenset[str], note: str = "") -> frozenset[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or any(not isinstance(item, str) or item not in allowed for item in raw):
        raise Refused("invalid_arguments", f"{field} must be a list of: {', '.join(sorted(allowed))}.{note}")
    return frozenset(raw)


def _cursor(raw: Any, field: str, count: int) -> list[Any] | None:
    if raw is None:
        return None
    return decode_cursor(raw, form=f"{field} must be a next_cursor from this tool", count=count, max_chars=256)


def _latest(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    runs = entry["runs"]
    return cast(Mapping[str, Any], runs[-1]) if runs else None


def _summary(entry: Mapping[str, Any]) -> dict[str, Any]:
    run = _latest(entry) or {}
    return {
        "session_id": entry["session_id"], "kind": entry["kind"], "job_id": entry["job_id"] or None,
        "job_mode": entry["job_mode"] or None, "started_by": run.get("started_by"), "state": run.get("state"),
        "created_iso_utc": entry["created_iso_utc"],
        "started_iso_utc": run.get("started_iso_utc") or None,
        "ended_iso_utc": run.get("ended_iso_utc") or None,
        "backend": entry["backend"], "origin": entry["origin"],
        "run_id": run.get("run_id"), "outcome": run.get("outcome") or None,
        "report_state": run["report"]["state"] if run else None,
    }


class SessionToolkit:
    """Serve session operations through one trusted caller and result boundary."""

    def __init__(self, store: JobStore, runner: SessionRunner, schedule: ScheduleToolkit, clock: Clock) -> None:
        self.store = store
        self.runner = runner
        self.schedule = schedule
        self.clock = clock

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        cards = session_cards()
        if OP_CONTINUE not in self.schedule.host.capabilities.operations:
            cards = [card for card in cards if card["name"] != "session_continue"]
        return cards

    async def invoke_at(self, name: str, arguments: Mapping[str, Any], *, at: InboundMessage) -> str:
        """Run one session command with its authenticated sender and place."""
        methods = {"session_start", "session_continue", "session_list", "session_get", "session_result",
                   "session_cancel"}
        if name not in methods or not isinstance(arguments, Mapping) or not isinstance(at, InboundMessage):
            return refusal("invalid_arguments", "name, arguments, or at is invalid")
        method = cast(Callable[..., Awaitable[str]], getattr(self, name))
        try:
            inspect.signature(method).bind(**arguments)
        except TypeError as exc:
            return refusal("invalid_arguments", str(exc))
        caller = Caller(Address(at.platform, at.workspace, at.conversation, at.thread), at.sender, at.chat_type)
        token = _COMMAND.set((caller, at.event_id or at.message_id, at.trigger == TRIGGER_DM, {
            **{name: getattr(at, name) for name in ("message_id", "reply_thread", "envelope", "trigger")},
            "reply_optional": False,
        }))
        try:
            return await method(**arguments)
        finally:
            _COMMAND.reset(token)

    def _caller(self) -> Caller:
        command = _COMMAND.get()
        return command[0] if command else self.schedule._caller()

    def _idempotency_key(self) -> str:
        command = _COMMAND.get()
        identity: tuple[Any, ...]
        if command is not None:
            identity = ("command", command[0].place.json(), command[0].user, command[1])
        elif current := active_call():
            identity = ("tool", current[1].conversation, *current[0])
        else:
            identity = ()
        if not identity or (command is not None and not command[1]):
            raise Refused("capability_unavailable", "session operation needs a stable tool call or command ID")
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    async def _visible(self, entry: Mapping[str, Any], caller: Caller) -> bool:
        origin = address_from_json(entry["origin"])
        if not caller.user or not self.schedule._same_conversation(origin, caller.place):
            return False
        places = self.schedule._places(origin.platform)
        return places is None or await places.can_read(caller.user, origin.conversation, workspace=origin.workspace)

    async def _result_visible(self, entry: Mapping[str, Any], caller: Caller) -> bool:
        command = _COMMAND.get()
        place = request_metadata().get("belle_place")
        direct = command[2] if command is not None else isinstance(place, Mapping) and place.get("direct") is True
        origin = address_from_json(entry["origin"])
        if (not direct or not caller.user or caller.user != entry["requested_by_user_id"]
                or (caller.place.platform, caller.place.workspace) != (origin.platform, origin.workspace)):
            return False
        places = self.schedule._places(origin.platform)
        if places is None:
            return False
        try:
            current = await places.check_place(
                caller.place.conversation, caller.place.thread, workspace=caller.place.workspace
            )
            return bool(current.exists and current.member and current.chat_type == caller.chat_type
                        and await places.can_read(caller.user, caller.place.conversation,
                                                  workspace=caller.place.workspace)
                        and await places.can_read(caller.user, origin.conversation, workspace=origin.workspace))
        except Exception:
            logger.warning("session result place check failed", exc_info=True)
            return False

    async def _find(self, session_id: str, caller: Caller, *, result: bool = False) -> dict[str, Any]:
        async with self.store.transaction() as snapshot:
            entry = snapshot.sessions.get(session_id)
        if entry is None or not await (self._result_visible(entry, caller) if result else self._visible(entry, caller)):
            raise Refused("session_not_found", "session_id does not name a session visible here")
        return entry

    def _result(self, advice: str = "This tool has no page argument.", **fields: Any) -> str:
        return self.schedule._size(_success(**fields), advice)

    @_answer
    async def session_start(self, prompt: Any, turn: Any = None, notify: Any = True, questions: Any = "refuse") -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise Refused("invalid_arguments", "prompt must be nonempty task text")
        if type(notify) is not bool:
            raise Refused("invalid_arguments", "notify must be true or false")
        if not isinstance(questions, str) or questions not in {"refuse", "ask_target"}:
            raise Refused("invalid_arguments", "questions must be refuse or ask_target")
        if turn is not None and not isinstance(turn, Mapping):
            raise Refused("invalid_arguments", "turn must be an object")
        caller = self._caller()
        if not caller.user:
            raise Refused("trusted_conversation_context_required", "session_start needs a sender")
        command = _COMMAND.get()
        context = command[3] if command else request_metadata().get("belle_inbound", {})
        selected, scope = await self.schedule._turn(
            {} if turn is None else turn, caller.place, caller.place, caller.user, caller.chat_type,
            target_chat_type=caller.chat_type, refuse_questions=questions == "refuse",
        )
        check_session_posting(
            self.schedule.host.session_posting_tool(caller.place.platform), scope,
            optional=context.get("reply_optional", False),
        )
        backend = self.schedule.host.capabilities.backend
        now = _iso(self.clock.now())
        request = RequestStart(
            self._idempotency_key(),
            fingerprint("standalone", caller.user, caller.place.json(), caller.chat_type, backend, prompt, questions,
                        selected, notify),
            dict(context), now, None,
        )
        run = new_run(started_by="request", prompt=prompt, turn=selected, questions=questions,
                      place=caller.place.json(), delivery="notice" if notify else "store", owner="", request=request)
        stored = await self.runner.request_session(DetachedSession(
            session_id=new_session_id(), kind="standalone", job_id="", job_mode="", backend=backend,
            origin=caller.place.json(), origin_chat_type=caller.chat_type, requested_by_user_id=caller.user,
            created_iso_utc=now, deletion=NO_DELETION, runs=[run],
        ))
        return self._result(session_id=stored.session_id, state=stored.runs[-1].state,
                            origin=stored.origin, inspect_with="session_get")

    @_answer
    async def session_continue(self, session_id: Any, prompt: Any, turn: Any = None) -> str:
        wanted = _id(session_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise Refused("invalid_arguments", "prompt must be nonempty task text")
        if turn is not None and not isinstance(turn, Mapping):
            raise Refused("invalid_arguments", "turn must be an object")
        if OP_CONTINUE not in self.schedule.host.capabilities.operations:
            raise Refused("capability_unavailable", "session_continue needs a backend that retains conversations")
        caller = self._caller()
        async with self.store.transaction() as snapshot:
            entry = snapshot.sessions.get(wanted)
        if entry is None or not (await self._visible(entry, caller) or await self._result_visible(entry, caller)):
            raise Refused("session_not_found", "session_id does not name a session visible here")
        operators = self.schedule.host.directory.ids_for_role(
            self.schedule.settings.operator_role, channel=caller.place.platform
        )
        if caller.user != entry["requested_by_user_id"] and caller.user not in operators:
            raise Refused("session_not_found", "session_id does not name a session you can continue here")
        origin = address_from_json(entry["origin"])
        requester = entry["requested_by_user_id"]
        target, chat_type = await self.schedule._target(origin.json(), origin, requester)
        if not chat_type:
            raise Refused("destination_unreachable", "origin chat type is unavailable")
        selected, scope = await self.schedule._turn(
            {} if turn is None else turn, origin, target, requester, chat_type,
            target_chat_type=chat_type, refuse_questions=True,
        )
        first = entry["runs"][0] if entry["runs"] else {}
        check_session_posting(
            self.schedule.host.session_posting_tool(origin.platform), scope,
            optional=((first.get("request") or {}).get("inbound") or {}).get("reply_optional", False),
        )
        _, run = await self.runner.continue_session(
            wanted, prompt, selected, chat_type, f"continue:{self._idempotency_key()}"
        )
        return self._result(session_id=wanted, run_id=run.run_id, state=run.state, inspect_with="session_get")

    @_answer
    async def session_list(
        self, states: Any = None, kinds: Any = None, started_by: Any = None,
        cursor: Any = None, page_size: Any = 20,
    ) -> str:
        states_filter = _choices(states, "states", RUN_STATES)
        kinds_filter = _choices(kinds, "kinds", _KINDS, _NOT_TURNS)
        if started_by is not None and (not isinstance(started_by, str) or started_by not in _STARTED_BY):
            raise Refused("invalid_arguments", f"started_by must be one of: clock, request.{_NOT_TURNS}")
        page = _positive(page_size, "page_size")
        after = _cursor(cursor, "cursor", 2)
        if after is not None and (not isinstance(after[0], str) or not isinstance(after[1], str)):
            raise Refused("invalid_arguments", "cursor must be a next_cursor from session_list")
        caller = self._caller()
        async with self.store.transaction() as snapshot:
            records = [_summary(entry) for entry in snapshot.sessions.values()]
        visible = []
        for row in records:
            if ((states_filter is not None and row["state"] not in states_filter)
                    or (kinds_filter is not None and row["kind"] not in kinds_filter)
                    or (started_by is not None and row["started_by"] != started_by)
                    or not await self._visible(row, caller)):
                continue
            visible.append(row)
        visible.sort(key=lambda row: (row["created_iso_utc"], row["session_id"]), reverse=True)
        if after is not None:
            visible = [row for row in visible if (row["created_iso_utc"], row["session_id"]) < tuple(after)]
        listed = visible[:page]
        next_cursor = (_encode_cursor(listed[-1]["created_iso_utc"], listed[-1]["session_id"])
                       if len(visible) > page else None)
        return self._result(
            "Lower page_size.", sessions=listed, next_cursor=next_cursor,
            coverage={"source": "retained_sessions"},
        )

    @_answer
    async def session_get(self, session_id: Any, cursor: Any = None, page_size: Any = 20) -> str:
        wanted = _id(session_id)
        page = _positive(page_size, "page_size")
        after = _cursor(cursor, "cursor", 2)
        if after is not None and (after[0] != wanted or not isinstance(after[1], str)):
            raise Refused("invalid_arguments", "cursor must be a next_cursor for this session_get")
        entry = await self._find(wanted, self._caller())
        newest = entry["runs"][::-1]
        if after is not None:
            position = next((index for index, run in enumerate(newest) if run["run_id"] == after[1]), None)
            if position is None:
                raise Refused("invalid_arguments", "cursor names a run that the session no longer holds. "
                                                   "Call session_get without cursor")
            newest = newest[position + 1:]
        listed = newest[:page]
        next_cursor = _encode_cursor(wanted, listed[-1]["run_id"]) if len(newest) > page else None
        runs = [{
            "run_id": run["run_id"], "started_by": run["started_by"], "state": run["state"],
            "outcome": run["outcome"] or None, "started_iso_utc": run["started_iso_utc"] or None,
            "ended_iso_utc": run["ended_iso_utc"] or None, "report_state": run["report"]["state"],
            "stop_requested": run["stop_requested"],
        } for run in listed]
        async with self.store.transaction() as snapshot:
            current = snapshot.sessions.get(wanted)
            expires = expires_iso_utc(current, snapshot, self.schedule.settings) if current is not None else None
        # claimed_by names a Belle process, which tells the model nothing.
        deletion = {key: value for key, value in entry["deletion"].items() if key != "claimed_by"}
        session = {**_summary(entry), "deletion": deletion, "expires_iso_utc": expires, "runs": runs}
        return self._result("Lower page_size.", session=session, next_cursor=next_cursor)

    @_answer
    async def session_result(
        self, session_id: Any, run_id: Any = None, cursor: Any = None, result_max_chars: Any = 2000,
    ) -> str:
        wanted = _id(session_id)
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise Refused("invalid_arguments", "run_id must be a nonempty string")
        limit = _positive(result_max_chars, "result_max_chars")
        after = _cursor(cursor, "cursor", 3)
        if after is not None and (after[0] != wanted or not isinstance(after[1], str)
                                  or type(after[2]) is not int or after[2] < 0):
            raise Refused("invalid_arguments", "cursor must be a next_cursor for this session_result")
        entry = await self._find(wanted, self._caller(), result=True)
        runs = entry["runs"]
        run = next((item for item in runs if item["run_id"] == run_id), None) if run_id else _latest(entry)
        if run is None:
            raise Refused("invalid_arguments", "run_id must name a run of session_id")
        if after is not None and after[1] != run["run_id"]:
            raise Refused("invalid_arguments", "cursor must name this run")
        state = run["state"]
        if state in {"pending", "running"}:
            return self._result(session_id=wanted, run_id=run["run_id"],
                                state=state, available=False, reason="not_finished")
        if not run["result_available"]:
            return self._result(session_id=wanted, run_id=run["run_id"],
                                state=state, available=False, reason="not_retained",
                                error=run["error"] or None)
        text = run["result"]
        start = after[2] if after is not None else 0
        if start > len(text):
            raise Refused("invalid_arguments", "cursor is past the end of this result")
        end = min(start + limit, len(text))
        return self._result(
            "Lower result_max_chars.", session_id=wanted, run_id=run["run_id"], state=state,
            available=True, result=text[start:end], result_chars=len(text),
            result_truncated=end < len(text),
            next_cursor=_encode_cursor(wanted, run["run_id"], end) if end < len(text) else None,
        )

    @_answer
    async def session_cancel(self, session_id: Any) -> str:
        wanted = _id(session_id)
        caller = self._caller()
        entry = await self._find(wanted, caller)
        operators = self.schedule.host.directory.ids_for_role(
            self.schedule.settings.operator_role, channel=caller.place.platform
        )
        if caller.user != entry["requested_by_user_id"] and caller.user not in operators:
            raise Refused("session_not_found", "session_id does not name a session you can cancel here")
        return self._result(**await self.runner.cancel_session(wanted))
