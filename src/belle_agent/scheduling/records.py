"""Stored jobs, sessions and runs of Belle's scheduler.

A session owns one conversation, ``belle_session_<session_id>``. A run is one
turn in that conversation. A job starts runs on a clock. Each record type reads
its stored JSON with ``from_json``, which refuses a missing key, an unknown key
and a value of the wrong type.
"""

import hashlib
import json
import re
import secrets
import string
import types
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any, ClassVar, Self, Union, get_args, get_origin, get_type_hints

SESSION_ID = re.compile(r"ses-[a-z0-9]{10}\Z")
RUN_ID = re.compile(r"run-[a-z0-9]{10}\Z")
#: The prefix of the conversation key of every detached session. No attached session's key starts with it.
SESSION_CONVERSATION_PREFIX = "belle_session_"
RUN_STATES = frozenset({"pending", "running", "succeeded", "failed", "interrupted", "cancelled"})
ENDED_STATES = RUN_STATES - {"pending", "running"}
UNRUN_STATES = frozenset({"skipped", "missed"})
DELIVERIES = frozenset({"post_final", "notice", "store"})
#: ``pending``: Belle owes the notice. ``uncertain``: Belle began to post it and has no receipt, so it posts no copy.
REPORT_STATES = frozenset({"none", "pending", "sent", "refused", "uncertain"})
_ADDRESS = ("platform", "workspace", "conversation", "thread")
_POSTED = (*_ADDRESS, "message_id")
_INBOUND_TEXT = ("message_id", "envelope", "trigger")


class RecordInvalid(ValueError):
    """A stored record does not have the form of its type. The text names the JSON path and the form."""


def _id(prefix: str) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(10))


def new_job_id() -> str:
    """Make a job id. The store checks that it is unused before insertion."""
    return _id("job-")


def new_session_id() -> str:
    """Make a session id."""
    return _id("ses-")


def new_run_id() -> str:
    """Make a run id."""
    return _id("run-")


def owned_conversation(session_id: str) -> str:
    """Return the key of the conversation that a session owns."""
    return SESSION_CONVERSATION_PREFIX + session_id


def utc_time(value: str) -> datetime:
    """Read a stored UTC instant. Refuse one without a Z or +00:00 offset."""
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError("UTC time needs a Z or +00:00 offset")
    return instant


def owes_delivery(run: dict[str, Any]) -> bool:
    """Whether a run still owes a post: its notice, or the final of a post_final run that recovery stored.

    A final with an error is one that Belle could not post, and the error says why.
    """
    return bool(run["report"]["state"] == "pending"
                or run["delivery"] == "post_final" and run["outcome"] == "stored-final" and not run["error"])


def fingerprint(*parts: Any) -> str:
    """Return a stable hash of a request, to refuse a reused idempotency key with another request."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


_UNIONS = (Union, types.UnionType)
_FORMS = {str: "a string", int: "an integer", bool: "true or false"}


def _form(hint: Any) -> str:
    if get_origin(hint) in _UNIONS:
        return " or ".join("null" if item is type(None) else _form(item) for item in get_args(hint))
    if get_origin(hint) is list:
        return "a list"
    return _FORMS.get(hint, "an object")


def _value(hint: Any, value: Any, path: str) -> Any:
    origin = get_origin(hint)
    if origin in _UNIONS:
        if value is None and type(None) in get_args(hint):
            return None
        records = [option for option in get_args(hint) if isinstance(option, type) and issubclass(option, Record)]
        if isinstance(value, dict) and len(records) == 1:
            # The error names the field of the record that is wrong.
            return records[0].from_json(value, path)
        for option in get_args(hint):
            if option is not type(None):
                try:
                    return _value(option, value, path)
                except RecordInvalid:
                    pass
        raise RecordInvalid(f"{path} must be {_form(hint)}")
    if isinstance(hint, type) and issubclass(hint, Record):
        return hint.from_json(value, path)
    if origin is list:
        if not isinstance(value, list):
            raise RecordInvalid(f"{path} must be a list")
        (item,) = get_args(hint)
        return [_value(item, entry, f"{path}[{index}]") for index, entry in enumerate(value)]
    if origin is dict:
        _, item = get_args(hint)
        if (not isinstance(value, dict) or any(not isinstance(key, str) for key in value)
                or item is str and any(not isinstance(entry, str) for entry in value.values())):
            raise RecordInvalid(f"{path} must be an object" + (" of strings" if item is str else ""))
        return value
    if type(value) is not hint:
        raise RecordInvalid(f"{path} must be {_form(hint)}")
    return value


def _check_address(value: dict[str, str], path: str, keys: tuple[str, ...] = _ADDRESS) -> None:
    if set(value) != set(keys):
        raise RecordInvalid(f"{path} must have exactly the strings {', '.join(keys)}")


class Record:
    """A stored record. Each subclass is a frozen dataclass."""

    #: Field name to the values that it can hold.
    _CHOICES: ClassVar[dict[str, frozenset[str]]] = {}
    #: Fields that must not be empty.
    _NONEMPTY: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_json(cls, data: Any, path: str) -> Self:
        """Read one stored record. Raise :class:`RecordInvalid` with the path and the expected form."""
        if not isinstance(data, dict):
            raise RecordInvalid(f"{path} must be an object")
        names = [item.name for item in fields(cls)]  # type: ignore[arg-type]
        missing = [name for name in names if name not in data]
        unknown = sorted(set(data) - set(names))
        if missing or unknown:
            problem = f"misses {', '.join(missing)}" if missing else f"has unknown keys {', '.join(unknown)}"
            raise RecordInvalid(f"{path} {problem}. It must have exactly: {', '.join(names)}")
        hints = get_type_hints(cls)
        values: dict[str, Any] = {name: _value(hints[name], data[name], f"{path}.{name}") for name in names}
        for name, choices in cls._CHOICES.items():
            if values[name] not in choices:
                raise RecordInvalid(f"{path}.{name} must be one of: {', '.join(sorted(choices))}")
        for name in cls._NONEMPTY:
            if not values[name]:
                raise RecordInvalid(f"{path}.{name} must not be empty")
        for name in names:
            if name.endswith("_iso_utc") and values[name]:
                try:
                    utc_time(values[name])
                except ValueError as exc:
                    raise RecordInvalid(
                        f"{path}.{name} must be a UTC time, such as 2026-10-23T08:00:00Z: {exc}"
                    ) from None
        record = cls(**values)
        record._check(path)
        return record

    def _check(self, path: str) -> None:
        """Check the rules between the fields of one record."""

    def to_json(self) -> dict[str, Any]:
        """Return the stored form."""
        return asdict(self)  # type: ignore[call-overload,no-any-return]


@dataclass(frozen=True)
class ClockStart(Record):
    """How the clock started a run."""

    due_iso_utc: str
    late_seconds: int
    #: Due times before this one that passed with no run, under ``catch_up: latest``.
    missed_runs: int
    #: Unique in the store, so that two processes start one due time once.
    claim_key: str

    _NONEMPTY: ClassVar[tuple[str, ...]] = ("due_iso_utc", "claim_key")


@dataclass(frozen=True)
class RequestStart(Record):
    """Who asked for a run, and where."""

    #: Empty when the request has no idempotency, as for ``schedule_run_now``.
    idempotency_key: str
    fingerprint: str
    #: The context of the message that asked, for the run's delivery.
    inbound: dict[str, Any]
    requested_iso_utc: str
    #: The readers that the requester approved for this run's place, or null for the job's own.
    approved_readers: list[str] | str | None
    #: A session_continue run. Its turn must find the session's conversation on the backend.
    continues: bool = False

    _NONEMPTY: ClassVar[tuple[str, ...]] = ("requested_iso_utc",)

    def _check(self, path: str) -> None:
        context = self.inbound
        if (set(context) - {*_INBOUND_TEXT, "reply_thread", "reply_optional"}
                or any(not isinstance(context.get(key, ""), str) for key in _INBOUND_TEXT)
                or context.get("reply_thread") is not None and not isinstance(context["reply_thread"], str)
                or type(context.get("reply_optional", False)) is not bool):
            raise RecordInvalid(f"{path}.inbound must hold the strings message_id, envelope, trigger, "
                                "reply_thread (or null) and the boolean reply_optional")
        if isinstance(self.approved_readers, str) and self.approved_readers != "workspace":
            raise RecordInvalid(f"{path}.approved_readers must be a list of user ids, workspace or null")


@dataclass(frozen=True)
class ReportRecord(Record):
    """The short notice of a ``notice`` run's end."""

    state: str
    message_id: str
    error: str

    _CHOICES: ClassVar[dict[str, frozenset[str]]] = {"state": REPORT_STATES}


@dataclass(frozen=True)
class SessionRun(Record):
    """One run of a detached session: one turn in its conversation, with its result and its delivery."""

    run_id: str
    started_by: str
    state: str
    outcome: str
    #: Cleared when the run ends.
    prompt: str
    turn: dict[str, Any]
    questions: str
    place: dict[str, str]
    delivery: str
    started_iso_utc: str
    ended_iso_utc: str
    result: str
    result_available: bool
    error: str
    owner: str
    #: A stop is in progress. Belle sets it before it sends the stop, and clears it when it sent none. An
    #: ``interrupted`` end with it set is ``cancelled``.
    stop_requested: bool
    posted: list[dict[str, str]]
    report: ReportRecord
    clock: ClockStart | None
    request: RequestStart | None

    _CHOICES: ClassVar[dict[str, frozenset[str]]] = {
        "started_by": frozenset({"clock", "request"}), "state": RUN_STATES, "delivery": DELIVERIES,
        "questions": frozenset({"refuse", "ask_target"}),
    }

    def _check(self, path: str) -> None:
        if not RUN_ID.fullmatch(self.run_id):
            raise RecordInvalid(f"{path}.run_id must be run- and 10 lowercase letters and digits")
        if (self.clock is None) != (self.started_by == "request") or (self.request is None) != (self.started_by == "clock"):
            raise RecordInvalid(f"{path} must have clock for started_by clock, request for started_by request, "
                                "and null for the other one")
        _check_address(self.place, f"{path}.place")
        for index, posted in enumerate(self.posted):
            _check_address(posted, f"{path}.posted[{index}]", _POSTED)
        if self.report.state != "none" and self.delivery != "notice":
            raise RecordInvalid(f"{path}.report.state must be none when delivery is not notice")

    @property
    def queued_iso_utc(self) -> str:
        """When the run fell due or was asked for."""
        if self.clock is not None:
            return self.clock.due_iso_utc
        assert self.request is not None
        return self.request.requested_iso_utc


@dataclass(frozen=True)
class Deletion(Record):
    """The deletion of a session's conversation, then of its record."""

    state: str
    #: The owner that deletes now. Empty when no process holds the deletion.
    claimed_by: str
    attempts: int
    last_error: str
    last_attempt_iso_utc: str

    _CHOICES: ClassVar[dict[str, frozenset[str]]] = {"state": frozenset({"none", "deleting", "blocked"})}


@dataclass(frozen=True)
class DetachedSession(Record):
    """Work that Belle starts on its own. It owns one conversation."""

    session_id: str
    kind: str
    #: The job of a scheduled session, else empty. It names a job or a deleted job.
    job_id: str
    #: fresh or shared for a scheduled session, else empty.
    job_mode: str
    backend: str
    origin: dict[str, str]
    origin_chat_type: str
    requested_by_user_id: str
    created_iso_utc: str
    deletion: Deletion
    runs: list[SessionRun]

    _CHOICES: ClassVar[dict[str, frozenset[str]]] = {"kind": frozenset({"standalone", "scheduled"}), "job_mode": frozenset({"", "fresh", "shared"})}
    _NONEMPTY: ClassVar[tuple[str, ...]] = ("created_iso_utc",)

    @property
    def owned_conversation(self) -> str:
        """The key of the conversation that this session owns."""
        return owned_conversation(self.session_id)

    @classmethod
    def from_json(cls, data: Any, path: str) -> "DetachedSession":
        """Read one stored session. Its ``owned_conversation`` must be ``belle_session_`` and its id."""
        if not isinstance(data, dict):
            raise RecordInvalid(f"{path} must be an object")
        values: dict[str, Any] = dict(data)
        stored = values.pop("owned_conversation", None)
        session = super().from_json(values, path)
        if stored != session.owned_conversation:
            raise RecordInvalid(f"{path}.owned_conversation must be {session.owned_conversation}")
        return session

    def _check(self, path: str) -> None:
        if not SESSION_ID.fullmatch(self.session_id):
            raise RecordInvalid(f"{path}.session_id must be ses- and 10 lowercase letters and digits")
        if (self.kind == "scheduled") != bool(self.job_id) or bool(self.job_id) != bool(self.job_mode):
            raise RecordInvalid(f"{path} must have job_id and job_mode for kind scheduled, and both empty for kind standalone")
        if not self.runs and self.job_mode != "shared":
            raise RecordInvalid(f"{path}.runs must not be empty, except in the session of a shared job")
        if len({run.run_id for run in self.runs}) != len(self.runs):
            raise RecordInvalid(f"{path}.runs must have distinct run_id values")
        _check_address(self.origin, f"{path}.origin")

    def to_json(self) -> dict[str, Any]:
        """Return the stored form, with the derived ``owned_conversation``."""
        return {**asdict(self), "owned_conversation": self.owned_conversation}


@dataclass(frozen=True)
class UnrunDue(Record):
    """A due time that started no run."""

    due_iso_utc: str
    state: str
    reason: str
    claim_key: str

    _CHOICES: ClassVar[dict[str, frozenset[str]]] = {"state": UNRUN_STATES}
    _NONEMPTY: ClassVar[tuple[str, ...]] = ("due_iso_utc", "claim_key")


@dataclass(frozen=True)
class Job(Record):
    """One scheduled job, with the fields that only the store reads."""

    job_id: str
    name: str
    prompt: str
    when: dict[str, Any]
    enabled: bool
    ended: bool
    disabled_reason: str
    deliver_to: dict[str, str]
    session: str
    overlap: str
    queue_max: int
    catch_up: str
    questions: str
    turn: dict[str, Any]
    origin: dict[str, str]
    origin_chat_type: str
    created_by_user_id: str
    parent_job_id: str
    created_iso_utc: str
    updated_by_user_id: str
    updated_iso_utc: str
    runs_started: int
    consecutive_failures: int
    backend: str
    revision: int = 1
    watermark_iso_utc: str = ""
    approved_readers: list[str] | str = field(default_factory=list)
    #: The session of a shared job, else empty.
    shared_session_id: str = ""
    unrun: list[UnrunDue] = field(default_factory=list)
    #: The latest due time of a run or an unrun due time that Belle dropped from the history, else empty.
    dropped_through_iso_utc: str = ""

    _CHOICES: ClassVar[dict[str, frozenset[str]]] = {"session": frozenset({"fresh", "shared"})}
    _NONEMPTY: ClassVar[tuple[str, ...]] = ("created_iso_utc", "updated_iso_utc")

    def _check(self, path: str) -> None:
        _check_address(self.origin, f"{path}.origin")
        _check_address(self.deliver_to, f"{path}.deliver_to")
        if isinstance(self.approved_readers, str) and self.approved_readers != "workspace":
            raise RecordInvalid(f"{path}.approved_readers must be a list of user ids or workspace")
        if (self.session == "shared") != bool(self.shared_session_id):
            raise RecordInvalid(f"{path}.shared_session_id must name a session for session shared, and be empty for fresh")


@dataclass(frozen=True)
class DeletedJob(Record):
    """What stays of a deleted job while a session names it."""

    origin: dict[str, str]
    deliver_to: dict[str, str]
    created_by_user_id: str
    #: Whether schedule_runs still shows the job's runs. Without history, its sessions expire when they end.
    history_kept: bool
    unrun: list[UnrunDue]
    #: See ``Job.dropped_through_iso_utc``.
    dropped_through_iso_utc: str
    #: The backend that the job ran on, for the rows of its unrun due times.
    backend: str

    def _check(self, path: str) -> None:
        _check_address(self.origin, f"{path}.origin")
        _check_address(self.deliver_to, f"{path}.deliver_to")


NO_DELETION = Deletion("none", "", 0, "", "")


def request_id_of(run: SessionRun) -> str:
    """Return the backend request id of a run. It is the run id."""
    return run.run_id


def new_run(
    *, started_by: str, prompt: str, turn: dict[str, Any], questions: str, place: dict[str, str],
    delivery: str, owner: str, clock: ClockStart | None = None, request: RequestStart | None = None,
) -> SessionRun:
    """Make a pending run."""
    return SessionRun(
        run_id=new_run_id(), started_by=started_by, state="pending", outcome="", prompt=prompt, turn=turn,
        questions=questions, place=place, delivery=delivery, started_iso_utc="", ended_iso_utc="", result="",
        result_available=False, error="", owner=owner, stop_requested=False, posted=[],
        report=ReportRecord("pending" if delivery == "notice" else "none", "", ""), clock=clock, request=request,
    )


#: The ``job_json`` fields, in order. The fields that only the store reads are left out.
JOB_PUBLIC = (
    "job_id", "name", "prompt", "when", "enabled", "ended", "disabled_reason", "deliver_to", "session",
    "overlap", "queue_max", "catch_up", "questions", "turn", "origin", "origin_chat_type",
    "created_by_user_id", "parent_job_id", "created_iso_utc", "updated_by_user_id", "updated_iso_utc",
    "runs_started", "consecutive_failures", "backend",
)
#: The fields of one ``schedule_runs`` row, in order.
RUN_ROW = (
    "run_id", "session_id", "job_id", "started_by", "state", "outcome", "due_iso_utc", "started_iso_utc",
    "ended_iso_utc", "late_seconds", "missed_runs", "place", "posted", "turn", "result", "result_chars",
    "result_truncated", "error", "reason", "backend",
)


def job_json(job: Job, *, next_runs: list[dict[str, str]] | None = None, last_run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the public job record in field order."""
    data = job.to_json()
    return {**{key: data[key] for key in JOB_PUBLIC}, "next_runs": next_runs or [], "last_run": last_run}


def run_row(session: DetachedSession, run: SessionRun) -> dict[str, Any]:
    """Write one run of a job as a ``schedule_runs`` row."""
    clock = run.clock
    values = {
        "run_id": run.run_id, "session_id": session.session_id, "job_id": session.job_id,
        "started_by": run.started_by, "state": run.state, "outcome": run.outcome,
        "due_iso_utc": run.queued_iso_utc, "started_iso_utc": run.started_iso_utc,
        "ended_iso_utc": run.ended_iso_utc, "late_seconds": clock.late_seconds if clock else 0,
        "missed_runs": clock.missed_runs if clock else 0, "place": run.place, "posted": run.posted,
        "turn": run.turn, "result": run.result, "result_chars": len(run.result), "result_truncated": False,
        "error": run.error, "reason": "", "backend": session.backend,
    }
    return {key: values[key] for key in RUN_ROW}


def unrun_row(job_id: str, unrun: UnrunDue, place: dict[str, str], backend: str) -> dict[str, Any]:
    """Write one due time that started no run as a ``schedule_runs`` row."""
    values: dict[str, Any] = {
        "run_id": None, "session_id": None, "job_id": job_id, "started_by": "clock", "state": unrun.state,
        "outcome": "", "due_iso_utc": unrun.due_iso_utc, "started_iso_utc": "", "ended_iso_utc": "",
        "late_seconds": 0, "missed_runs": 0, "place": place, "posted": [], "turn": {}, "result": "",
        "result_chars": 0, "result_truncated": False, "error": "", "reason": unrun.reason, "backend": backend,
    }
    return {key: values[key] for key in RUN_ROW}
