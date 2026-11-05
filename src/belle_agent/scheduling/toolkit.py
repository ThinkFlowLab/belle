"""Six shared tools for creating and inspecting scheduled jobs."""

import asyncio
import inspect
import re
import weakref
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from belle_agent.backend import BackendCapabilities, Catalog, ToolCall, TurnScope
from belle_agent.platforms.base import InboundMessage
from belle_agent.scheduling.address import Address, address_from_json, expand_deliver_to, validate_address
from belle_agent.scheduling.cards import schedule_cards
from belle_agent.scheduling.clock import Clock
from belle_agent.scheduling.clock import iso_utc as _iso
from belle_agent.scheduling.expiry import expires_iso_utc
from belle_agent.scheduling.policy import allowed_by, delivery_refusal_detail, parse_delivery_rules
from belle_agent.scheduling.records import (
    RUN_STATES,
    UNRUN_STATES,
    DetachedSession,
    Job,
    UnrunDue,
    job_json,
    new_job_id,
    new_session_id,
    run_row,
    unrun_row,
)
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.runner import Runner, delete_job_records, scheduled_session
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore, Snapshot
from belle_agent.scheduling.tool_support import answer as _answer
from belle_agent.scheduling.tool_support import decode_cursor, encode_cursor
from belle_agent.scheduling.tool_support import positive as _positive
from belle_agent.scheduling.tool_support import success as _success
from belle_agent.scheduling.turnspec import ScopeLimits, check_catalog, parse_turn, settle_run_scope
from belle_agent.scheduling.when import Schedule, next_runs, parse_when
from belle_agent.scopes.people import PeopleDirectory
from belle_agent.skills import packaged_skill_text
from belle_agent.tools.host import active_call, refusal, request_metadata
from belle_agent.tools.inputs import checks_ids, iso_instant

JOB_ID_FORM = {"job": (re.compile(r"job-[a-z0-9]{10}\Z"), "job- and 10 lowercase letters and digits")}
_OPTIONS = {
    "session": frozenset({"fresh", "shared"}),
    "overlap": frozenset({"skip", "queue", "parallel"}),
    "catch_up": frozenset({"skip", "latest", "all"}),
    "questions": frozenset({"refuse", "ask_target"}),
}
_MISSING = object()
_STATES = RUN_STATES | UNRUN_STATES
_COMMAND_PLACE: ContextVar[dict[str, Any] | None] = ContextVar("belle_schedule_command_place", default=None)


class SchedulingHost(Protocol):
    """Host operations used to validate a scheduled turn."""

    directory: PeopleDirectory

    @property
    def platforms(self) -> Mapping[str, Any]: ...

    @property
    def capabilities(self) -> BackendCapabilities: ...

    async def catalog(self) -> Catalog | None:
        """Return the backend's current catalog."""
        ...

    def scope_limits(self, place: Address, user: str, chat_type: str) -> ScopeLimits:
        """Return the scope limits for a creator in one place."""
        ...

    def session_posting_tool(self, platform: str) -> str:
        """Return the registered posting tool for detached delivery."""
        ...

    def places(self, platform: str) -> Any:
        """Return the platform's optional place checks."""
        ...


@dataclass(frozen=True)
class Caller:
    """Trusted identity and place of one tool call."""

    place: Address
    user: str
    chat_type: str
    parent_job_id: str = ""


@dataclass(frozen=True)
class QuestionedAudience:
    """The audience named in one held tool call's question."""

    name: str
    origin: Address
    target: Address
    readers: tuple[str, ...] | str


def _option(value: Any, field: str) -> str:
    choices = _OPTIONS[field]
    if not isinstance(value, str) or value not in choices:
        raise Refused("invalid_arguments", f"{field} must be one of: {', '.join(sorted(choices))}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise Refused("invalid_arguments", f"{field} must be true or false")
    return value


def _required_job_id(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise Refused("invalid_arguments", "job_id must be job- and 10 lowercase letters and digits")


def _cursor_key(raw: Any) -> tuple[float, str]:
    form = "cursor must be a value from coverage.next_cursor"
    values = decode_cursor(raw, form=form, count=2)
    if (not all(isinstance(value, str) for value in values)
            or not values[1] or (instant := iso_instant(values[0])) is None):
        raise Refused("invalid_arguments", form)
    return instant, values[1]


def _row_key(row: Mapping[str, Any]) -> tuple[float, str]:
    """Order the rows of schedule_runs by due time, then by run id, or by claim key for a due time with no run."""
    due = iso_instant(row["due_iso_utc"])
    assert due is not None  # The store checked each due instant before this read.
    return due, str(row["run_id"] or row["claim_key"])


def _job_rows(snapshot: Snapshot, job_id: str, owner: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the schedule_runs rows of a job or a deleted job, each with the claim key of a due time with no run."""
    rows: list[dict[str, Any]] = []
    for stored in snapshot.job_sessions(job_id):
        session = DetachedSession.from_json(stored, f"sessions.{stored['session_id']}")
        rows.extend({**run_row(session, run), "claim_key": ""} for run in session.runs)
    for entry in owner["unrun"]:
        due = UnrunDue.from_json(entry, "unrun")
        rows.append({**unrun_row(job_id, due, owner["deliver_to"], owner["backend"]), "claim_key": due.claim_key})
    return rows


class ScheduleToolkit:
    """Serve scheduled jobs through one validated, platform-neutral boundary."""

    def __init__(
        self, store: JobStore, runner: Runner, settings: SchedulingSettings, host: SchedulingHost, clock: Clock
    ) -> None:
        self.store = store
        self.runner = runner
        self.settings = settings
        self.host = host
        self.clock = clock
        self._delivery = parse_delivery_rules(settings.delivery)
        self._questioned: dict[
            tuple[str, str], tuple[weakref.ReferenceType[ToolCall], QuestionedAudience]
        ] = {}

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Describe the six tools for this deployment."""
        return schedule_cards(self.settings)

    async def invoke_at(self, name: str, arguments: Mapping[str, Any], *, at: InboundMessage) -> str:
        """Run one schedule tool for a trusted inbound command and clear its place."""
        if name not in {"schedule_create", "schedule_list", "schedule_update", "schedule_delete", "schedule_run_now", "schedule_runs"}:
            return refusal("invalid_arguments", "name must be one of the six schedule tools")
        if not isinstance(arguments, Mapping) or not isinstance(at, InboundMessage):
            return refusal("invalid_arguments", "arguments must be an object and at must be an inbound message")
        method = cast(Callable[..., Awaitable[str]], getattr(self, name))
        try:
            inspect.signature(method).bind(**arguments)
        except TypeError as exc:
            return refusal("invalid_arguments", str(exc))
        token = _COMMAND_PLACE.set({"belle_place": {
            "platform": at.platform, "workspace": at.workspace, "conversation": at.conversation,
            "thread": at.thread, "sender": at.sender, "chat_type": at.chat_type,
        }})
        try:
            return await method(**arguments)
        finally:
            _COMMAND_PLACE.reset(token)

    def _caller(self) -> Caller:
        metadata = _COMMAND_PLACE.get() or request_metadata()
        place = metadata.get("belle_place")
        if not isinstance(place, Mapping) or any(not isinstance(place.get(key), str) for key in (
            "platform", "workspace", "conversation", "thread", "sender", "chat_type"
        )) or not place["platform"] or not place["conversation"]:
            raise Refused("trusted_conversation_context_required", "schedule tools need belle_place from a running turn")
        scheduled = metadata.get("belle_schedule")
        if isinstance(scheduled, Mapping):
            origin = address_from_json(scheduled.get("origin"))
            creator = scheduled.get("creator")
            parent = scheduled.get("job_id")
            origin_chat_type = scheduled.get("origin_chat_type")
            if not isinstance(creator, str) or not isinstance(parent, str) or not isinstance(origin_chat_type, str) or not origin_chat_type:
                raise Refused("trusted_conversation_context_required", "belle_schedule needs creator, job_id and origin_chat_type")
            return Caller(origin, creator, origin_chat_type, parent)
        return Caller(
            Address(place["platform"], place["workspace"], place["conversation"], place["thread"]),
            place["sender"], place["chat_type"],
        )

    def _visibility(self, all_conversations: Any) -> bool:
        if all_conversations is not None:
            _boolean(all_conversations, "all_conversations")
            if self.settings.job_visibility != "all":
                raise Refused("invalid_arguments", "all_conversations is not available: this conversation can see only its own jobs")
        return all_conversations is True

    @staticmethod
    def _same_conversation(left: Address, right: Address) -> bool:
        return (left.platform, left.workspace, left.conversation) == (
            right.platform, right.workspace, right.conversation
        )

    def _visible(self, entry: Mapping[str, Any], caller: Caller, all_conversations: bool) -> bool:
        # Only a trusted text command can use the operator role to cross conversations.
        operator_command = _COMMAND_PLACE.get() is not None and caller.user in self.host.directory.ids_for_role(
            self.settings.operator_role, channel=caller.place.platform
        )
        if all_conversations or operator_command:
            return True
        return any(self._same_conversation(address_from_json(entry[key]), caller.place) for key in ("origin", "deliver_to"))

    def _places(self, platform: str) -> Any:
        return self.host.places(platform)

    async def _target(self, raw: Any, origin: Address, creator: str) -> tuple[Address, str]:
        source_places = self._places(origin.platform)

        async def open_dm() -> Address:
            if not creator or source_places is None or not hasattr(source_places, "open_direct_message"):
                raise Refused("delivery_unavailable", "deliver_to creator_dm needs the creator's id and direct messages")
            try:
                conversation = await source_places.open_direct_message(creator, workspace=origin.workspace)
            except ValueError as exc:
                raise Refused("delivery_unavailable", str(exc)) from None
            if not isinstance(conversation, str) or not conversation:
                raise Refused("delivery_unavailable", "deliver_to creator_dm needs a direct message address")
            return Address(origin.platform, origin.workspace, conversation, "")

        target = await expand_deliver_to(raw, origin, open_dm=open_dm)
        workspaces = {
            name: places.workspaces if (places := self._places(name)) is not None else None
            for name in self.host.platforms
        }
        validate_address(target, workspaces)
        if allowed_by(self._delivery, origin, target) is None:
            raise Refused("delivery_not_allowed", delivery_refusal_detail(self._delivery, origin, target))
        target_places = self._places(target.platform)
        if target_places is None:
            if not self._same_conversation(origin, target):
                raise Refused("delivery_unavailable", "deliver_to needs a platform that can check the target conversation")
            return target, ""
        if (origin.platform == target.platform and origin.workspace == target.workspace and creator
                and not await target_places.can_read(creator, target.conversation, workspace=target.workspace)):
            raise Refused("target_not_readable", f"creator {creator} cannot read deliver_to.conversation {target.conversation}")
        checked = await target_places.check_place(target.conversation, target.thread, workspace=target.workspace)
        if not checked.exists or not checked.member:
            raise Refused("destination_unreachable", f"deliver_to {target.conversation} cannot receive a post: {checked.reason}")
        return target, str(checked.chat_type)

    async def _readers(self, origin: Address, target: Address) -> list[str] | str:
        if self._same_conversation(origin, target):
            return []
        if origin.platform != target.platform or origin.workspace != target.workspace:
            return "workspace"
        places = self._places(origin.platform)
        if places is None:
            return "workspace"
        readers = await places.newcomers(origin.conversation, target.conversation, workspace=origin.workspace)
        return "workspace" if readers is None else list(readers)

    @staticmethod
    def _reader_set(readers: list[str] | str) -> tuple[str, ...] | str:
        return readers if readers == "workspace" else tuple(sorted(set(readers)))

    def _remember_question(self, name: str, origin: Address, target: Address, readers: list[str] | str) -> None:
        current = active_call()
        if current is None:
            return
        key, call = current

        def forget(reference: weakref.ReferenceType[ToolCall]) -> None:
            if self._questioned.get(key, (None,))[0] is reference:
                self._questioned.pop(key, None)

        reference = weakref.ref(call, forget)
        self._questioned[key] = (
            reference, QuestionedAudience(name, origin, target, self._reader_set(readers)),
        )

    def _approved_readers(
        self, name: str, origin: Address, target: Address, readers: list[str] | str
    ) -> list[str] | str:
        current = active_call()
        asked = None
        if current is not None:
            key, call = current
            stored = self._questioned.pop(key, None)
            if stored is not None and stored[0]() is call:
                asked = stored[1]
        expected = QuestionedAudience(name, origin, target, self._reader_set(readers))
        if asked is not None and asked != expected:
            raise Refused("audience_widened", "deliver_to audience changed since its approval question; retry the call")
        if readers and asked is None:
            raise Refused("approval_required", "deliver_to has new readers; confirm the exact audience before this call runs")
        return readers

    async def widening_question(self, name: str, arguments: Mapping[str, Any]) -> str | None:
        """Ask the creator before a target gains readers outside the origin."""
        if name not in {"schedule_create", "schedule_update", "schedule_run_now"} or "deliver_to" not in arguments:
            return None
        caller = self._caller()
        origin = caller.place
        creator = caller.user
        if name != "schedule_create":
            job_id = arguments.get("job_id")
            if not isinstance(job_id, str):
                return None
            async with self.store.transaction() as snapshot:
                if job_id not in snapshot.jobs or not self._visible(snapshot.jobs[job_id], caller, False):
                    return None
                job = self._job(snapshot, job_id)
                self._owner(job, caller)
                origin = address_from_json(job.origin)
                creator = job.created_by_user_id
        target, _ = await self._target(arguments["deliver_to"], origin, creator)
        readers = await self._readers(origin, target)
        if not readers:
            return None
        self._remember_question(name, origin, target, readers)
        audience = "the whole workspace" if readers == "workspace" else ", ".join(readers)
        return f"Allow this scheduled job to send its result to {target.conversation}? New readers: {audience}."

    async def _turn(
        self, raw: Any, origin: Address, target: Address, creator: str, chat_type: str,
        *, target_chat_type: str = "", label: str = "", refuse_questions: bool = False,
    ) -> tuple[dict[str, Any], TurnScope]:
        selected = parse_turn(raw)
        capabilities = self.host.capabilities
        catalog = await self.host.catalog()
        supplies_skills = "skills_required" not in capabilities.scope_fields and "system_additions" in capabilities.scope_fields
        check_catalog(
            selected, catalog, capabilities,
            packaged=lambda name: packaged_skill_text(name) is not None,
            supplies_skills=supplies_skills,
        )
        scope = settle_run_scope(
            selected,
            self.host.scope_limits(origin, creator, chat_type),
            self.host.scope_limits(
                target,
                creator if (origin.platform, origin.workspace) == (target.platform, target.workspace) else "",
                chat_type if self._same_conversation(origin, target) else target_chat_type,
            ),
            refuse_questions=refuse_questions,
            system_additions=(label,) if label else (),
        )
        stored = {
            "model": selected.model, "skills": list(selected.skills), "expert": selected.expert,
            "subagents": None if selected.subagents is None else list(selected.subagents),
        }
        return stored, scope

    async def validate_run(
        self, job: Job, *, deliver_to: Address | None = None, turn: Mapping[str, Any] | None = None,
        approved_readers: list[str] | str | None = None, label: str = "",
    ) -> tuple[Address, TurnScope, str]:
        """Recheck one run's delivery, audience, catalog, and both scope limits."""
        origin = address_from_json(job.origin)
        target, target_chat_type = await self._target(
            (deliver_to or address_from_json(job.deliver_to)).json(), origin, job.created_by_user_id
        )
        readers = await self._readers(origin, target)
        approved = job.approved_readers if approved_readers is None else approved_readers
        if approved != "workspace" and (readers == "workspace" or set(readers) - set(approved)):
            new = "the whole workspace" if readers == "workspace" else ", ".join(sorted(set(readers) - set(approved)))
            raise Refused("audience_widened", f"deliver_to now reaches new readers: {new}")
        _, scope = await self._turn(
            turn if turn is not None else job.turn, origin, target, job.created_by_user_id, job.origin_chat_type,
            target_chat_type=target_chat_type, label=label, refuse_questions=job.questions == "refuse",
        )
        return target, scope, job.origin_chat_type if self._same_conversation(origin, target) else target_chat_type

    def _size(self, result: str, advice: str) -> str:
        """Refuse a result over the harness's tool result limit. ``advice`` says what to change."""
        limit = getattr(self.host.capabilities, "tool_result_max_chars", 0)
        if limit and len(result) > limit:
            raise Refused("result_too_large", f"the result has {len(result)} characters, and the limit is {limit}. {advice}")
        return result

    def _job_record(self, job: Job, rows: list[dict[str, Any]], count: int) -> dict[str, Any]:
        future = []
        if job.enabled and not job.ended:
            zone = ZoneInfo(str(job.when.get("zone") or "UTC"))
            for due in next_runs(Schedule(job.when), self.clock.now(), count):
                future.append({"due_iso_utc": _iso(due), "due_local": due.astimezone(zone).isoformat()})
        runs = [row for row in rows if row["run_id"] is not None]
        last = max(runs, key=_row_key, default=None)
        if last is not None:
            last = {key: value for key, value in last.items() if key not in {"result", "claim_key"}}
        return job_json(job, next_runs=future, last_run=last)

    async def _visible_job(self, job_id: str, caller: Caller) -> Job:
        async with self.store.transaction() as snapshot:
            if job_id not in snapshot.jobs or not self._visible(snapshot.jobs[job_id], caller, False):
                raise Refused("job_not_found", f"job_id {job_id!r} does not name a visible job")
            return self._job(snapshot, job_id)

    def _share(self, snapshot: Snapshot, job: dict[str, Any]) -> None:
        """Give a shared job its session: the one it had, while that one is not being deleted, or a new one."""
        kept = next((session for session in snapshot.job_sessions(job["job_id"])
                     if session["job_mode"] == "shared" and session["deletion"]["state"] == "none"), None)
        if kept is None:
            session_id = new_session_id()
            while session_id in snapshot.sessions:
                session_id = new_session_id()
            kept = snapshot.sessions[session_id] = scheduled_session(
                session_id, job, "shared", _iso(self.clock.now()), []
            ).to_json()
        job["shared_session_id"] = kept["session_id"]

    @staticmethod
    def _job(snapshot: Snapshot, job_id: str) -> Job:
        return Job.from_json(snapshot.jobs[job_id], f"jobs.{job_id}")

    @staticmethod
    def _validate_fields(fields: Mapping[str, Any]) -> None:
        for name in ("session", "overlap", "catch_up", "questions"):
            if name in fields and fields[name] is not _MISSING:
                _option(fields[name], name)
        if "queue_max" in fields and fields["queue_max"] is not _MISSING:
            _positive(fields["queue_max"], "queue_max")
        if "enabled" in fields and fields["enabled"] is not _MISSING:
            _boolean(fields["enabled"], "enabled")
        if "prompt" in fields and fields["prompt"] is not _MISSING and not isinstance(fields["prompt"], str):
            raise Refused("invalid_arguments", "prompt must be a string")
        if "name" in fields and fields["name"] is not _MISSING:
            name = fields["name"]
            if not isinstance(name, str) or not name or "\r" in name or "\n" in name:
                raise Refused("name_invalid", "name must be a nonempty string on one line")
        for name in ("when", "turn"):
            if name in fields and fields[name] is not _MISSING and not isinstance(fields[name], Mapping):
                raise Refused("invalid_arguments", f"{name} must be an object")
        if "deliver_to" in fields and fields["deliver_to"] is None:
            raise Refused("invalid_arguments", "deliver_to must be thread, conversation, creator_dm or an address object")

    def _owner(self, job: Job, caller: Caller, *, pause_only: bool = False) -> None:
        operators = self.host.directory.ids_for_role(self.settings.operator_role, channel=caller.place.platform)
        if caller.user == job.created_by_user_id or caller.user in operators:
            return
        if pause_only and self._same_conversation(caller.place, address_from_json(job.deliver_to)):
            return
        raise Refused("not_job_owner", f"job_id {job.job_id} was created by user {job.created_by_user_id}")

    @_answer
    async def schedule_create(
        self, name: Any, prompt: Any, when: Any, deliver_to: Any = _MISSING, session: Any = "fresh",
        overlap: Any = "skip", queue_max: Any = 1, catch_up: Any = _MISSING, questions: Any = "refuse",
        turn: Any = _MISSING, enabled: Any = True,
    ) -> str:
        """Create one job after validating its schedule, target, turn, and limit."""
        fields = locals().copy()
        self._validate_fields(fields)
        caller = self._caller()
        schedule, zone_source = await asyncio.to_thread(
            parse_when, when, now=self.clock.now(), default_zone=self.settings.default_zone or None,
            min_interval_seconds=self.settings.min_interval_seconds,
        )
        _positive(queue_max, "queue_max")
        target, target_chat_type = await self._target(
            None if deliver_to is _MISSING else deliver_to, caller.place, caller.user
        )
        selected_turn, _ = await self._turn(
            {} if turn is _MISSING else turn, caller.place, target, caller.user, caller.chat_type,
            target_chat_type=target_chat_type,
        )
        if not caller.user:
            raise Refused("not_job_owner", "created_by_user_id is empty; a schedule needs a requester")
        now = _iso(self.clock.now())
        job = Job(
            job_id="", name=name, prompt=prompt, when=schedule.when, enabled=enabled, ended=False,
            disabled_reason="", deliver_to=target.json(), session=session, overlap=overlap, queue_max=queue_max,
            catch_up=("latest" if schedule.when["kind"] == "once" else "skip") if catch_up is _MISSING else catch_up,
            questions=questions, turn=selected_turn, origin=caller.place.json(), origin_chat_type=caller.chat_type,
            created_by_user_id=caller.user, parent_job_id=caller.parent_job_id, created_iso_utc=now,
            updated_by_user_id=caller.user, updated_iso_utc=now, runs_started=0, consecutive_failures=0,
            backend=self.host.capabilities.backend, approved_readers=self._approved_readers(
                "schedule_create", caller.place, target, await self._readers(caller.place, target)
            ),
        )
        async with self.store.transaction() as snapshot:
            if len(snapshot.jobs) >= self.settings.max_jobs:
                raise Refused("job_limit_reached", f"this deployment holds at most {self.settings.max_jobs} jobs and has {len(snapshot.jobs)}")
            job_id = new_job_id()
            while job_id in snapshot.jobs or job_id in snapshot.deleted:
                job_id = new_job_id()
            job = replace(job, job_id=job_id)
            stored = snapshot.jobs[job_id] = job.to_json()
            if session == "shared":
                self._share(snapshot, stored)
            job = self._job(snapshot, job_id)
        self.runner.changed([job.job_id])
        result: dict[str, Any] = {"job": self._job_record(job, [], 3)}
        if zone_source:
            result["zone_from"] = zone_source
        return _success(**result)

    @checks_ids({"job_id": "job"}, JOB_ID_FORM, "Belle", refusal)
    @_answer
    async def schedule_list(self, job_id: Any = None, next_runs: Any = 3, all_conversations: Any = None) -> str:
        """Show visible jobs without cutting a result silently."""
        count = _positive(next_runs, "next_runs", minimum=0)
        wide = self._visibility(all_conversations)
        caller = self._caller()
        async with self.store.transaction() as snapshot:
            total = len(snapshot.jobs)
            entries = [
                (self._job(snapshot, current_id), _job_rows(snapshot, current_id, job))
                for current_id, job in snapshot.jobs.items()
                if (job_id is None or current_id == job_id) and self._visible(job, caller, wide)
            ]
        jobs = []
        for job, rows in entries:
            record = self._job_record(job, rows, count)
            if not self._same_conversation(caller.place, address_from_json(job.origin)):
                readers = await self._readers(address_from_json(job.origin), caller.place)
                if readers:
                    record.update(prompt=None, hidden=["prompt"], hidden_reason="caller conversation has readers outside job origin")
            jobs.append(record)
        if job_id is not None and not jobs:
            raise Refused("job_not_found", f"job_id {job_id!r} does not name a visible job")
        return self._size(_success(jobs=jobs, jobs_total=total), "Lower next_runs, or pass job_id.")

    @checks_ids({"job_id": "job"}, JOB_ID_FORM, "Belle", refusal)
    @_answer
    async def schedule_update(
        self, job_id: Any, name: Any = _MISSING, prompt: Any = _MISSING, when: Any = _MISSING,
        deliver_to: Any = _MISSING, session: Any = _MISSING, overlap: Any = _MISSING,
        queue_max: Any = _MISSING, catch_up: Any = _MISSING, questions: Any = _MISSING,
        turn: Any = _MISSING, enabled: Any = _MISSING,
    ) -> str:
        """Replace only supplied fields of a visible job."""
        _required_job_id(job_id)
        updates = {key: value for key, value in locals().items() if key not in {"self", "job_id"} and value is not _MISSING}
        self._validate_fields(updates)
        caller = self._caller()
        job = await self._visible_job(job_id, caller)
        self._owner(job, caller, pause_only=updates == {"enabled": False})
        if not updates:
            raise Refused("no_change", f"job_id {job_id!r} has no requested change")
        if updates.get("enabled") is True and job.ended and "when" not in updates:
            raise Refused("job_ended", "enabled true needs a new when for an ended job")
        zone_source = ""
        if "when" in updates:
            schedule, zone_source = await asyncio.to_thread(
                parse_when, updates["when"], now=self.clock.now(), default_zone=self.settings.default_zone or None,
                min_interval_seconds=self.settings.min_interval_seconds,
            )
            updates["when"] = schedule.when
        origin = address_from_json(job.origin)
        target = address_from_json(job.deliver_to)
        target_chat_type = job.origin_chat_type if self._same_conversation(origin, target) else ""
        if "deliver_to" in updates:
            target, target_chat_type = await self._target(updates["deliver_to"], origin, job.created_by_user_id)
            updates["deliver_to"] = target.json()
            updates["approved_readers"] = self._approved_readers(
                "schedule_update", origin, target, await self._readers(origin, target)
            )
        if "turn" in updates or "deliver_to" in updates:
            if "deliver_to" not in updates:
                target, target_chat_type = await self._target(job.deliver_to, origin, job.created_by_user_id)
            updates["turn"], _ = await self._turn(
                updates.get("turn", job.turn), origin, target, job.created_by_user_id, job.origin_chat_type,
                target_chat_type=target_chat_type,
            )
        changed = [key for key, value in updates.items() if key != "approved_readers" and getattr(job, key) != value]
        if "deliver_to" in updates and job.approved_readers != updates["approved_readers"] and "deliver_to" not in changed:
            changed.append("deliver_to")
        if not changed:
            raise Refused("no_change", f"job_id {job_id!r} already has the requested values")
        now = _iso(self.clock.now())
        async with self.store.transaction() as snapshot:
            current = snapshot.jobs.get(job_id)
            if current is None or current["revision"] != job.revision:
                raise Refused("no_change", f"job_id {job_id!r} changed while this request was checked; inspect and retry")
            current.update(updates)
            if "when" in updates:
                current.update(ended=False, runs_started=0, watermark_iso_utc="")
            if updates.get("session") == "shared" and not current["shared_session_id"]:
                self._share(snapshot, current)
            elif updates.get("session") == "fresh":
                current["shared_session_id"] = ""
            current["updated_by_user_id"] = caller.user
            current["updated_iso_utc"] = now
            if enabled is True:
                current["disabled_reason"] = ""
            recorded = self._job(snapshot, job_id)
            rows = _job_rows(snapshot, job_id, current)
        self.runner.changed([job_id])
        result: dict[str, Any] = {"job": self._job_record(recorded, rows, 3), "changed": changed}
        if zone_source:
            result["zone_from"] = zone_source
        return _success(**result)

    @checks_ids({"job_id": "job"}, JOB_ID_FORM, "Belle", refusal)
    @_answer
    async def schedule_delete(self, job_id: Any, keep_history: Any = False) -> str:
        """Delete one job, cancel each pending run of it and stop each running one."""
        _required_job_id(job_id)
        keep = _boolean(keep_history, "keep_history")
        caller = self._caller()
        async with self.store.transaction() as snapshot:
            if job_id not in snapshot.jobs or not self._visible(snapshot.jobs[job_id], caller, False):
                raise Refused("job_not_found", f"job_id {job_id!r} does not name a visible job")
            self._owner(self._job(snapshot, job_id), caller)
            # The caller reads the cancelled runs in the answer, so they owe no notice.
            ending = delete_job_records(snapshot, job_id, keep_history=keep, now=self.clock.now(), notify=False)
            ends = [expires_iso_utc(session, snapshot, self.settings) for session in snapshot.job_sessions(job_id)]
        self.runner.changed([job_id])
        runs = await self.runner.end_listed(ending)
        # The kept history goes with the last of the job's sessions. A held session has no expiry time yet.
        known = [end for end in ends if end is not None]
        kept_until = max(known) if keep and known and len(known) == len(ends) else None
        return _success(job_id=job_id, runs=runs, history_kept_until_iso_utc=kept_until)

    @checks_ids({"job_id": "job"}, JOB_ID_FORM, "Belle", refusal)
    @_answer
    async def schedule_run_now(self, job_id: Any, deliver_to: Any = _MISSING, turn: Any = _MISSING) -> str:
        """Start one manual run without changing its job or run count."""
        _required_job_id(job_id)
        if turn is not _MISSING and not isinstance(turn, Mapping):
            raise Refused("invalid_arguments", "turn must be an object")
        if deliver_to is None:
            raise Refused("invalid_arguments", "deliver_to must be thread, conversation, creator_dm or an address object")
        caller = self._caller()
        job = await self._visible_job(job_id, caller)
        self._owner(job, caller)
        origin = address_from_json(job.origin)
        target = address_from_json(job.deliver_to)
        target_chat_type = job.origin_chat_type if self._same_conversation(origin, target) else ""
        override_target = None
        if deliver_to is not _MISSING:
            target, target_chat_type = await self._target(deliver_to, origin, job.created_by_user_id)
            override_target = target.json()
        override_turn = None
        if turn is not _MISSING or deliver_to is not _MISSING:
            if deliver_to is _MISSING:
                target, target_chat_type = await self._target(job.deliver_to, origin, job.created_by_user_id)
            override_turn, _ = await self._turn(
                turn if turn is not _MISSING else job.turn, origin, target, job.created_by_user_id, job.origin_chat_type,
                target_chat_type=target_chat_type,
            )
        approved = self._approved_readers(
            "schedule_run_now", origin, target, await self._readers(origin, target)
        ) if deliver_to is not _MISSING else None
        session, run = await self.runner.manual(
            job_id, deliver_to=override_target, turn=override_turn, approved_readers=approved,
        )
        return _success(run=run_row(session, run))

    @checks_ids({"job_id": "job"}, JOB_ID_FORM, "Belle", refusal)
    @_answer
    async def schedule_runs(
        self, job_id: Any = None, states: Any = None, after_iso_utc: Any = None, before_iso_utc: Any = None,
        cursor: Any = None, max_runs: Any = 20, result_max_chars: Any = 2000, all_conversations: Any = None,
    ) -> str:
        """List visible session history with strict filters and a paging cursor."""
        maximum = _positive(max_runs, "max_runs")
        chars = _positive(result_max_chars, "result_max_chars", minimum=0)
        wide = self._visibility(all_conversations)
        if states is not None and (
            not isinstance(states, list) or not states or any(not isinstance(state, str) or state not in _STATES for state in states)
        ):
            raise Refused("invalid_arguments", f"states must be a nonempty list from: {', '.join(sorted(_STATES))}")
        windows = []
        for field, value in (("after_iso_utc", after_iso_utc), ("before_iso_utc", before_iso_utc)):
            if value is not None and (not isinstance(value, str) or (instant := iso_instant(value)) is None):
                raise Refused("time_malformed", f"{field} must be a UTC ISO instant, such as 2026-10-17T18:00:00Z")
            windows.append(None if value is None else instant)
        after, before = windows
        boundary = _cursor_key(cursor) if cursor is not None else None
        caller = self._caller()
        records: list[dict[str, Any]] = []
        kept: list[tuple[float, str]] = []
        #: Each row due after the latest due time that a visible job dropped is in the history.
        dropped = 0.0
        found = False
        async with self.store.transaction() as snapshot:
            owners = [*snapshot.jobs.items(), *((key, stub) for key, stub in snapshot.deleted.items() if stub["history_kept"])]
            for current_id, owner in owners:
                if (job_id is not None and current_id != job_id) or not self._visible(owner, caller, wide):
                    continue
                found = True
                if owner["dropped_through_iso_utc"]:
                    dropped = max(dropped, iso_instant(owner["dropped_through_iso_utc"]) or 0.0)
                for row in _job_rows(snapshot, current_id, owner):
                    due, key = _row_key(row)
                    kept.append((due, row["due_iso_utc"]))
                    if (states is not None and row["state"] not in states) or (after is not None and due <= after) or (
                        before is not None and due >= before
                    ) or (boundary is not None and (due, key) >= boundary):
                        continue
                    records.append(row)
        if job_id is not None and not found:
            raise Refused("job_not_found", f"job_id {job_id!r} does not name a visible job")
        records.sort(key=_row_key, reverse=True)
        partial = len(records) > maximum
        selected = []
        for row in records[:maximum]:
            record = {key: value for key, value in row.items() if key != "claim_key"}
            record["result"] = row["result"][:chars]
            record["result_truncated"] = len(row["result"]) > chars
            for posted in row["posted"]:
                address = address_from_json({key: posted[key] for key in ("platform", "workspace", "conversation", "thread")})
                if address.platform != caller.place.platform or address.workspace != caller.place.workspace:
                    record.update(result=None, hidden=["result"], hidden_reason=f"posted to {address.platform}/{address.workspace}/{address.conversation}")
                    break
                if await self._readers(address, caller.place):
                    record.update(result=None, hidden=["result"], hidden_reason=f"caller cannot read posted place {address.conversation}")
                    break
            selected.append(record)
        last = records[maximum - 1] if partial else None
        coverage = {
            "status": "partial" if partial else "complete",
            "next_cursor": encode_cursor(last["due_iso_utc"], _row_key(last)[1]) if last is not None else None,
            "kept_since_iso_utc": min((item for item in kept if item[0] > dropped), default=(0.0, None))[1],
        }
        return self._size(_success(runs=selected, coverage=coverage), "Lower max_runs or result_max_chars.")
