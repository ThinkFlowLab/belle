"""Turn progress state shared by platform renderers."""

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from belle_agent.backend import EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, SUBAGENT, TurnEvent
from belle_agent.tools.host import held, succeeded

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_UNREPORTED = "unreported"
COMPRESSION_STARTED = "started"
MAX_TOOL_RUNS = 200
#: The title of a turn's activity card, as the turn runs and as it ended.
TITLE_WORKING = "Working..."
TITLE_DONE = "Turn finished"
TITLE_FAILED = "Turn failed"
TITLE_STOPPED = "Turn stopped"
#: How many runs a card names, and how long the detail of a run can be.
MAX_RUN_LINES = 6
MAX_DETAIL_LENGTH = 80
_RUN_LABEL_ALLOWED = re.compile(r"[^0-9A-Za-z_.\- ]+")
MAX_RUN_LABEL_LENGTH = 48
#: Bound for optional peek tool body fields. Oversize bodies are omitted whole
#: (no mid-string trim); the frame still refuses above ``control.peek_max_bytes``.
MAX_PEEK_TOOL_BODY_BYTES = 8192
#: Argument / result keys whose values are treated as filesystem paths (basename only).
_PEEK_PATH_KEYS = frozenset({
    "path", "file", "filepath", "filename", "dir", "directory", "cwd", "workdir",
    "working_directory", "target", "source",
})
#: Argument / result keys whose values are redacted in peek bodies.
_PEEK_SECRET_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "access_token", "refresh_token",
    "authorization", "bot_token", "app_token", "private_key", "client_secret",
})
_ABS_UNIX_PATH = re.compile(r"^/[^\x00\n]+")
_ABS_WIN_PATH = re.compile(r"^[A-Za-z]:[\\/][^\x00\n]+")


def card_discard_reason(*, silent: bool, stopped: bool, empty: bool = False) -> str:
    """Name why a turn leaves no activity card: it was silent or stopped, or its card held only the stop."""
    if silent:
        return "silence"
    if stopped:
        return "stopped"
    return "nothing shown" if empty else ""


def run_label(raw: Any, fallback: str, limit: int = MAX_RUN_LABEL_LENGTH) -> str:
    """Limit a model-supplied name to a short, single-line card label of letters, digits, ``_.-`` and spaces."""
    if not isinstance(raw, str):
        return fallback
    label = " ".join(_RUN_LABEL_ALLOWED.sub(" ", raw).split())
    if len(label) > limit:
        label = label[: limit - 1].rstrip() + "…"
    return label or fallback


@dataclass
class ActivityRun:
    call_id: str
    label: str
    state: str = RUN_RUNNING
    started_at: float = field(default_factory=time.monotonic)
    #: What the call says it does, where it says so.
    detail: str = ""


@dataclass
class CompressionRun:
    operation_id: str
    label: str
    state: str = COMPRESSION_STARTED
    started_at: float = field(default_factory=time.monotonic)
    before_messages: int = 0
    before_tokens: int = 0
    before_percent: int | None = None
    saved_messages: int = 0
    saved_tokens: int = 0
    saved_percent: float = 0.0
    duration_ms: int | None = None
    cancelled: bool = False

    def elapsed_seconds(self) -> float:
        if self.duration_ms is not None:
            return max(0.0, self.duration_ms / 1000.0)
        return max(0.0, time.monotonic() - self.started_at)

    def saved_anything(self) -> bool:
        return bool(self.saved_messages or self.saved_tokens)


@dataclass(kw_only=True)
class TurnProgress:
    subagent_runs: dict[str, ActivityRun] = field(default_factory=dict)
    tool_runs: dict[str, ActivityRun] = field(default_factory=dict)
    retired_tools: dict[str, dict[str, int]] = field(default_factory=dict)
    todo_counts: dict[str, int] = field(default_factory=dict)
    compression_runs: dict[str, CompressionRun] = field(default_factory=dict)
    reasoning_seen: bool = False
    reasoning_since: float | None = None
    reasoning_elapsed: float = 0.0

    def settle_progress(self) -> None:
        for run in (*self.subagent_runs.values(), *self.tool_runs.values()):
            if run.state == RUN_RUNNING:
                run.state = RUN_UNREPORTED
        for run in self.compression_runs.values():
            if run.state == COMPRESSION_STARTED:
                run.state = RUN_UNREPORTED
        self.end_reasoning()

    def subagent_totals(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self.subagent_runs.values():
            counts[run.state] = counts.get(run.state, 0) + 1
        return counts

    def subagents_by_type(self) -> dict[str, dict[str, int]]:
        by_type: dict[str, dict[str, int]] = {}
        for run in self.subagent_runs.values():
            counts = by_type.setdefault(run.label, {})
            counts[run.state] = counts.get(run.state, 0) + 1
        return by_type

    def tools_by_name(self) -> dict[str, dict[str, int]]:
        merged = {label: dict(counts) for label, counts in self.retired_tools.items()}
        for run in self.tool_runs.values():
            counts = merged.setdefault(run.label, {})
            counts[run.state] = counts.get(run.state, 0) + 1
        return merged

    def tool_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for counts in self.tools_by_name().values():
            for state, count in counts.items():
                totals[state] = totals.get(state, 0) + count
        return totals

    def note_reasoning(self) -> None:
        self.reasoning_seen = True
        if self.reasoning_since is None:
            self.reasoning_since = time.monotonic()

    def end_reasoning(self) -> None:
        if self.reasoning_since is None:
            return
        self.reasoning_elapsed += max(0.0, time.monotonic() - self.reasoning_since)
        self.reasoning_since = None

    def shows_nothing(self) -> bool:
        """Whether the turn reported nothing a card shows: no run, todo, compaction or thinking."""
        return not (
            self.subagent_runs or self.tool_runs or self.retired_tools or self.todo_counts
            or self.compression_runs or self.reasoning_seen
        )

    def compression_running(self) -> bool:
        return any(run.state == COMPRESSION_STARTED for run in self.compression_runs.values())

    def compression_saved_tokens(self) -> int:
        return sum(run.saved_tokens for run in self.compression_runs.values())

    def reasoning_seconds(self) -> float:
        total = self.reasoning_elapsed
        if self.reasoning_since is not None:
            total += max(0.0, time.monotonic() - self.reasoning_since)
        return total

    def retire_tool_run(self, call_id: str) -> None:
        run = self.tool_runs.pop(call_id, None)
        if run is None:
            return
        counts = self.retired_tools.setdefault(run.label, {})
        counts[run.state] = counts.get(run.state, 0) + 1

    def note_tool_call(self, call_id: str, label: str, detail: str = "") -> None:
        existing = self.tool_runs.get(call_id)
        if existing is not None:
            if existing.state == RUN_UNREPORTED:
                existing.state = RUN_RUNNING
                existing.started_at = time.monotonic()
            return
        while self.tool_runs and len(self.tool_runs) >= MAX_TOOL_RUNS:
            finished = [key for key, run in self.tool_runs.items() if run.state != RUN_RUNNING]
            oldest = min(finished or self.tool_runs, key=lambda key: self.tool_runs[key].started_at)
            self.retire_tool_run(oldest)
        self.tool_runs[call_id] = ActivityRun(call_id=call_id, label=label, detail=detail)


def runs_summary(counts: Mapping[str, int], noun: str) -> str:
    """Say how the runs of one kind stand, by their counts by state, such as ``1 of 3 tools running``."""
    total = sum(counts.values())
    if not total:
        return ""
    plural = noun if total == 1 else f"{noun}s"
    running = counts.get(RUN_RUNNING, 0)
    if running:
        return f"{running} of {total} {plural} running"
    failed = counts.get(RUN_FAILED, 0)
    if failed:
        return f"{failed} of {total} {plural} failed"
    unreported = counts.get(RUN_UNREPORTED, 0)
    if unreported:
        return f"{unreported} of {total} {plural} reported no result"
    return f"{total} {plural} finished"


def turn_title(turn: TurnProgress, *, closed: bool, failed: bool) -> str:
    """The title of a turn's card: working while it runs, then how it ended, with the runs that failed or went unreported."""
    if not closed:
        return TITLE_WORKING
    totals: dict[str, int] = {}
    for counts in (turn.subagent_totals(), turn.tool_totals()):
        for state, count in counts.items():
            totals[state] = totals.get(state, 0) + count
    notes: list[str] = []
    if failed_runs := totals.get(RUN_FAILED, 0):
        notes.append(f"{failed_runs} failed")
    if unreported := totals.get(RUN_UNREPORTED, 0):
        notes.append(f"{unreported} reported no result")
    stem = TITLE_FAILED if failed else TITLE_DONE
    return f"{stem}, " + ", ".join(notes) if notes else stem


def tool_result_failed(event: TurnEvent) -> bool:
    """Whether a ``tool_result`` reports an error, or a refusal that does not hold the call for a person's answer."""
    # A tool that refuses reports it in its result. The backend reports a call that could not run.
    # A call held for a person's answer has not failed: it has ended, and may be made later.
    return bool(event.data.get("failed")) or (not succeeded(event.text) and not held(event.text))


def note_tool_event(turn: TurnProgress, event: TurnEvent) -> bool:
    """Apply a ``tool_called`` or ``tool_result`` event to the runs of a turn. Return whether it named a run."""
    call_id = str(event.data.get("call_id") or "")
    subagent = event.data.get(SUBAGENT)
    if not call_id:
        return False
    if event.kind == EVENT_TOOL_CALLED:
        arguments = event.data.get("arguments")
        detail = run_label(arguments.get("description"), "", MAX_DETAIL_LENGTH) if isinstance(arguments, Mapping) else ""
        if subagent is None:
            # A tool call routed from the child session reuses the subagent's call_id and has no SUBAGENT.
            # It does not start a run of its own.
            if call_id in turn.subagent_runs:
                return True
            turn.note_tool_call(call_id, run_label(event.data.get("name"), "tool"), detail)
        elif (run := turn.subagent_runs.get(call_id)) is not None:
            run.state = RUN_RUNNING
        else:
            turn.subagent_runs[call_id] = ActivityRun(call_id, run_label(subagent, "subagent"), detail=detail)
        return True
    run = turn.tool_runs.get(call_id) or turn.subagent_runs.get(call_id)
    if run is None:
        return False
    if event.data.get("child") and call_id in turn.subagent_runs and call_id not in turn.tool_runs:
        # A tool result routed from the child session does not end the subagent run.
        return True
    run.state = RUN_FAILED if tool_result_failed(event) else RUN_DONE
    return True


def peek_tool_called_fields(event: TurnEvent) -> tuple[str, str, str] | None:
    """``call_id``, display ``name``, and short ``detail`` for a peek ``tool_call`` frame.

    Detail is only ``arguments.description`` when present. Full arguments go in
    optional ``args_text`` via :func:`peek_tool_args_text`.
    """
    if event.kind != EVENT_TOOL_CALLED:
        return None
    call_id = str(event.data.get("call_id") or "")
    if not call_id:
        return None
    arguments = event.data.get("arguments")
    detail = run_label(arguments.get("description"), "", MAX_DETAIL_LENGTH) if isinstance(arguments, Mapping) else ""
    subagent = event.data.get(SUBAGENT)
    if isinstance(subagent, str) and subagent.strip():
        name = run_label(subagent, "subagent")
    else:
        name = run_label(event.data.get("name"), "tool")
    return call_id, name, detail


def peek_tool_result_fields(event: TurnEvent) -> tuple[str, str | None, bool, str | None] | None:
    """``call_id``, optional ``name``, ``ok``, and optional short ``summary`` for a peek frame."""
    if event.kind != EVENT_TOOL_RESULT:
        return None
    call_id = str(event.data.get("call_id") or "")
    if not call_id:
        return None
    ok = not tool_result_failed(event)
    name = run_label(event.data.get("name"), "")
    if held(event.text):
        summary: str | None = "held"
    elif not ok:
        summary = run_label(event.text, "failed", MAX_DETAIL_LENGTH) if event.text else "failed"
    else:
        summary = None
    return call_id, name or None, ok, summary


def peek_tool_args_text(
    event: TurnEvent, *, max_bytes: int = MAX_PEEK_TOOL_BODY_BYTES,
) -> str | None:
    """Bounded JSON for peek ``args_text``, or null when absent / oversize / unserializable.

    Path policy: absolute filesystem paths become basenames (or are omitted when
    empty after that). Secret-shaped keys become ``[redacted]``. Does not invent
    arguments the event lacks. Never mid-string trims: oversize → omit.
    """
    if event.kind != EVENT_TOOL_CALLED:
        return None
    arguments = event.data.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    return _peek_bound_body(_scrub_peek_value(arguments), max_bytes=max_bytes)


def peek_tool_result_text(
    event: TurnEvent, *, max_bytes: int = MAX_PEEK_TOOL_BODY_BYTES,
) -> str | None:
    """Bounded plain/JSON body for peek ``result_text``, or null when absent / oversize."""
    if event.kind != EVENT_TOOL_RESULT:
        return None
    text = event.text
    if not isinstance(text, str) or not text:
        return None
    cleaned = _peek_body_controls(text)
    # Prefer structured JSON when the harness already emitted it.
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _peek_bound_body(_scrub_peek_plain(cleaned), max_bytes=max_bytes)
    return _peek_bound_body(_scrub_peek_value(parsed), max_bytes=max_bytes)


def _peek_body_controls(text: str) -> str:
    """Drop C0 controls other than tab and newline (same rule as peek body validation)."""
    return "".join(char for char in text if ord(char) >= 32 or char in "\t\n")


def _peek_bound_body(value: Any, *, max_bytes: int) -> str | None:
    """Serialize ``value`` for a peek body, or null when it cannot fit ``max_bytes`` whole."""
    if max_bytes <= 0:
        return None
    if isinstance(value, str):
        text = _peek_body_controls(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=_peek_json_default)
        except (TypeError, ValueError):
            return None
        text = _peek_body_controls(text)
    if not text:
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if size > max_bytes:
        # Operator resource limit: omit the optional body rather than mid-string trim.
        return None
    return text


def _peek_json_default(value: Any) -> Any:
    """Refuse inventing representations for non-JSON values."""
    raise TypeError(f"peek tool body cannot serialize {type(value).__name__}")


def _scrub_peek_value(value: Any) -> Any:
    """Redact secrets and reduce absolute paths to basenames for peek tool bodies."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in _PEEK_SECRET_KEYS or any(
                part in lowered for part in ("password", "secret", "token", "api_key")
            ):
                out[key] = "[redacted]"
            elif lowered in _PEEK_PATH_KEYS:
                out[key] = _peek_path_display(item)
            else:
                out[key] = _scrub_peek_value(item)
        return out
    if isinstance(value, list):
        return [_scrub_peek_value(item) for item in value]
    if isinstance(value, str):
        return _scrub_peek_plain(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Non-JSON leaves fail at dump time (do not invent a string form).
    return value


def _scrub_peek_plain(text: str) -> str:
    """Basename-only treatment when a plain result is itself an absolute path."""
    if _looks_like_abs_path(text):
        return _peek_path_display(text) or ""
    return text


def _looks_like_abs_path(text: str) -> bool:
    return bool(_ABS_UNIX_PATH.match(text) or _ABS_WIN_PATH.match(text))


def _peek_path_display(value: Any) -> str | None:
    """Basename-only display for path-like values; null when nothing safe remains."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text:
        return None
    if _ABS_WIN_PATH.match(text):
        name = PureWindowsPath(text).name
    elif _ABS_UNIX_PATH.match(text) or "/" in text or "\\" in text:
        name = PurePosixPath(text.replace("\\", "/")).name
    else:
        return text
    return name or None


def peek_question_text(text: str, *, limit: int = MAX_DETAIL_LENGTH) -> str:
    """Single-line, control-char-free question summary for a peek frame."""
    cleaned = " ".join("".join(char for char in text if ord(char) >= 32).split())
    return run_label(cleaned, "Question", limit)


def peek_usage_fields(data: Mapping[str, Any]) -> tuple[int | None, int | None, float | None]:
    """Normalize ``EVENT_USAGE`` data to tokens in/out and cost for peek and work rows.

    Returns only values the event itself reports. Session totals that a backend
    has not already scoped to the turn must not be invented here.
    """
    tokens = data.get("tokens")
    tokens_in = tokens_out = None
    if isinstance(tokens, Mapping):
        raw_in, raw_out = tokens.get("input"), tokens.get("output")
        if type(raw_in) is int and raw_in >= 0:
            tokens_in = raw_in
        if type(raw_out) is int and raw_out >= 0:
            tokens_out = raw_out
    cost = data.get("cost")
    if type(cost) not in (int, float) or isinstance(cost, bool) or cost < 0:
        cost_value = None
    else:
        cost_value = float(cost)
    return tokens_in, tokens_out, cost_value


def card_summary(turn: TurnProgress) -> str:
    """How the subagents and the tools of a turn stand, in one line."""
    parts = (runs_summary(turn.subagent_totals(), "subagent"), runs_summary(turn.tool_totals(), "tool"))
    return " · ".join(part for part in parts if part)


def run_lines(turn: TurnProgress, emoji: Mapping[str, str], max_lines: int = MAX_RUN_LINES) -> list[str]:
    """One Markdown line for each run, in the order they started: the running ones, then the latest of the others.

    ``emoji`` gives the mark of each run state. A last line counts the runs left out.
    """
    runs = sorted((*turn.subagent_runs.values(), *turn.tool_runs.values()), key=lambda run: run.started_at)
    running = [run for run in runs if run.state == RUN_RUNNING][:max_lines]
    room = max_lines - len(running)
    others = [run for run in runs if run.state != RUN_RUNNING][-room:] if room else []
    shown = sorted((*running, *others), key=lambda run: run.started_at)
    lines = [f"{emoji.get(run.state, '')} **{run.label}**" + (f" · {run.detail}" if run.detail else "") for run in shown]
    hidden = len(runs) - len(shown) + sum(sum(counts.values()) for counts in turn.retired_tools.values())
    return [*lines, f"and {hidden} more"] if hidden else lines
