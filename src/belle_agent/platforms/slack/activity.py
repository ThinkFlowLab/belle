"""Slack activity records and pure Block Kit card rendering."""

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from belle_agent import progress
from belle_agent.progress import run_label

from . import blocks as slack_blocks
from .delivery import CHANNEL_LOG_TAG, slack_failure_reason
from .text import clamp as _clamp

logger = logging.getLogger(__name__)

_SlackActivityRun = progress.ActivityRun
_SlackCompressionRun = progress.CompressionRun
_RUN_RUNNING = progress.RUN_RUNNING
_RUN_DONE = progress.RUN_DONE
_RUN_FAILED = progress.RUN_FAILED
_RUN_UNREPORTED = progress.RUN_UNREPORTED
_COMPRESSION_STARTED = progress.COMPRESSION_STARTED

# Keep stop clicks separate from answers to pending questions.
STOP_ACTION_ID = "belle_stop"

_MAX_BUTTON_TEXT_LENGTH = 75
_STOP_BUTTON_LABEL = "Stop"
_CARD_STATUS_IN_PROGRESS = "in_progress"
_CARD_STATUS_COMPLETE = "complete"
_CARD_STATUS_ERROR = "error"
_MAX_CARD_TITLE_LENGTH = 150
_MAX_CARD_DETAILS_LENGTH = 2000
_CARD_STATUS_PENDING = "pending"
_RUN_STATE_EMOJI: tuple[tuple[str, str], ...] = (
    (_RUN_DONE, "white_check_mark"),
    (_RUN_RUNNING, "zap"),
    (_RUN_FAILED, "x"),
    (_RUN_UNREPORTED, "grey_question"),
)
_RUN_STATE_EMOJI_BY_STATE = dict(_RUN_STATE_EMOJI)
_TODO_COMPLETED = "completed"
_TODO_IN_PROGRESS = "in_progress"
_TODO_PENDING = "pending"
_TODO_STATE_EMOJI: tuple[tuple[str, str], ...] = (
    (_TODO_COMPLETED, "white_check_mark"),
    (_TODO_IN_PROGRESS, "zap"),
    (_TODO_PENDING, "hourglass_flowing_sand"),
)
_THINKING_CARD_TITLE = "Thinking"
_THINKING_TASK_ID_PREFIX = "thinking-"
_TODO_CARD_TITLE = "Todos"
_SUBAGENT_CARD_TITLE = "Subagents"
_TOOL_CARD_TITLE = "Tools"
_COMPRESSION_EVENT_TYPE = "context.compression_state"
_COMPRESSION_CARD_TITLE = "Context compression"
_COMPRESSION_TASK_ID_PREFIX = "compression-"
_COMPRESSION_COMPLETED = "completed"
_COMPRESSION_NOOP = "noop"
_COMPRESSION_SKIPPED = "skipped"
_COMPRESSION_FAILED = "failed"
_COMPRESSION_STATES = frozenset(
    {_COMPRESSION_STARTED, _COMPRESSION_COMPLETED, _COMPRESSION_NOOP, _COMPRESSION_SKIPPED, _COMPRESSION_FAILED}
)
_COMPRESSION_STATE_EMOJI = {
    _COMPRESSION_STARTED: "zap",
    _COMPRESSION_COMPLETED: "white_check_mark",
    _COMPRESSION_NOOP: "heavy_minus_sign",
    _COMPRESSION_SKIPPED: "heavy_minus_sign",
    _COMPRESSION_FAILED: "x",
    _RUN_UNREPORTED: _RUN_STATE_EMOJI_BY_STATE[_RUN_UNREPORTED],
}
_COMPRESSION_STATE_WORDING = {
    _COMPRESSION_NOOP: "saved nothing",
    _COMPRESSION_SKIPPED: "did not run",
    _COMPRESSION_FAILED: "failed",
    _RUN_UNREPORTED: "reported no result",
}
_COMPRESSION_CANCELLED_ERROR = "cancelled"
_MIN_COMPRESSION_ELAPSED_SECONDS = 1.0
_MAX_COMPRESSION_RUNS = 16
_THINKING_RUNNING_EMOJI = "zap"
_THINKING_ENDED_EMOJI = "hourglass"
_ACTIVITY_TITLE_WORKING = progress.TITLE_WORKING
_ACTIVITY_TITLE_DONE = progress.TITLE_DONE
_ACTIVITY_TITLE_FAILED = progress.TITLE_FAILED
_ACTIVITY_TITLE_STOPPED = progress.TITLE_STOPPED
_ERROR_CARD_TITLE = "Harness error"
_HARNESS_ERROR_LEAD = "The turn ended before it answered."
_HARNESS_ERROR_TRIM_MARKER = "\n[trimmed]"
_MAX_SUMMARY_ERROR_LENGTH = 200
_MAX_CARD_DETAIL_LINES = 8
_MAX_RUNNING_TOOL_LINES = 4
_MAX_CLOSED_ACTIVITY_RECORDS = 32
_ACTIVITY_RECORD_TIMEOUT_SECONDS = 3600.0
_MAX_ACTIVITY_REQUEST_ALIASES = 64
_TODO_STATES = frozenset(state for state, _ in _TODO_STATE_EMOJI)


def subagent_run_id(subagent_id: str) -> str:
    """The key of a subagent's run on a card, distinct from the keys of tool runs."""
    return f"subagent:{subagent_id}"


def subagent_type_label(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return "subagent"
    if not isinstance(arguments, Mapping):
        return "subagent"
    return run_label(arguments.get("subagent_type"), "subagent")


def tool_event_body(payload: Any, nested_key: str) -> Mapping[str, Any] | None:
    """Accept flattened and nested tool payloads."""
    if not isinstance(payload, dict):
        return None
    nested = payload.get(nested_key)
    return nested if isinstance(nested, Mapping) else payload


def posting_refusal_code(payload: Any) -> str:
    """Read the error code after the runtime classifies a posting call as failed."""
    body = tool_event_body(payload, "tool_result")
    if body is None:
        return ""
    for key in ("raw_output", "result"):
        result = body.get(key)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                continue
        if isinstance(result, Mapping) and (code := str(result.get("error") or "").strip()):
            return code
    return ""


def tool_call_event(payload: Any) -> tuple[str, str, Any] | None:
    body = tool_event_body(payload, "tool_call")
    if body is None:
        return None
    name = str(body.get("name") or body.get("tool_name") or "").strip()
    if not name:
        return None
    call_id = str(body.get("tool_call_id") or body.get("id") or "").strip()
    return name, call_id, body.get("arguments")


def tool_result_event(payload: Any) -> tuple[str, str, str] | None:
    body = tool_event_body(payload, "tool_result")
    if body is None:
        return None
    call_id = str(body.get("tool_call_id") or body.get("id") or "").strip()
    if not call_id:
        return None
    name = str(body.get("tool_name") or body.get("name") or "").strip()
    failed = (
        body.get("success") is False
        or bool(body.get("is_error"))
        or str(body.get("status") or "").strip().lower() == "error"
    )
    return name, call_id, _RUN_FAILED if failed else _RUN_DONE


def todo_snapshot(payload: Any) -> dict[str, int] | None:
    """Count statuses from a full todo snapshot without reading todo text."""
    if not isinstance(payload, dict) or not isinstance(payload.get("todos"), list):
        return None
    counts: dict[str, int] = {}
    for item in payload["todos"]:
        if not isinstance(item, Mapping):
            continue
        state = str(item.get("status") or "").strip().lower()
        state = state if state in _TODO_STATES else _TODO_PENDING
        counts[state] = counts.get(state, 0) + 1
    return counts


def compression_event(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return (
        payload
        if str(payload.get("event_type") or "") == _COMPRESSION_EVENT_TYPE
        else None
    )


def _compression_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _compression_metric(body: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = body.get(key)
    return value if isinstance(value, Mapping) else {}


def decode_stop_value(action: Mapping[str, Any], logger: logging.Logger) -> tuple[str, str, str] | None:
    """Decode the session, request and channel IDs from a stop button.

    Reject missing fields and malformed values."""
    raw = action.get("value")
    if not isinstance(raw, str) or not raw:
        logger.warning("Slack stop button carried no value")
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Slack stop button value was not valid JSON")
        return None
    if not isinstance(decoded, Mapping):
        logger.warning("Slack stop button value was not an object")
        return None
    session_id = str(decoded.get("session_id") or "").strip()
    request_id = str(decoded.get("request_id") or "").strip()
    channel_id = str(decoded.get("channel_id") or "").strip()
    if not session_id or not request_id or not channel_id:
        logger.warning("Slack stop button value identified no turn")
        return None
    return session_id, request_id, channel_id


@dataclass
class _SlackActivityRecord(progress.TurnProgress):
    request_id: str
    channel_id: str
    thread_ts: str
    session_id: str = ""
    stop_requested: bool = False
    message_ts: str = ""
    #: The delayed post of the card, or the edit that the edit interval held back.
    post_timer: asyncio.Task | None = None
    next_edit_at: float = 0.0
    subagent_card_status: str = ""
    #: The last edit failed. The next edit waits for the edit interval, also when it is forced.
    edit_failed: bool = False
    closed: bool = False
    failed: bool = False
    harness_error: str = ""
    stopped: bool = False
    touched_at: float = field(default_factory=time.monotonic)

    def reopen(self) -> None:
        self.closed = False
        self.stopped = False

    def settle(self, *, stopped: bool = False) -> None:
        if self.post_timer is not None:
            self.post_timer.cancel()
            self.post_timer = None
        self.settle_progress()
        self.stopped = stopped
        self.closed = True
        self.touched_at = time.monotonic()

    def remember_written(self, blocks: list[dict[str, Any]]) -> None:
        self.subagent_card_status = ""
        for block in blocks:
            for card in block.get("tasks") or []:
                if str(card.get("task_id") or "").startswith("subagents-"):
                    self.subagent_card_status = str(card.get("status") or "")

    def note_compression(
        self, operation_id: str, status: str, body: Mapping[str, Any]
    ) -> None:
        """Pair compression updates and retain only bounded recent passes."""
        run = self.compression_runs.get(operation_id)
        if run is None:
            while len(self.compression_runs) >= _MAX_COMPRESSION_RUNS:
                self.compression_runs.pop(next(iter(self.compression_runs)))
            run = _SlackCompressionRun(
                operation_id=operation_id,
                label=run_label(body.get("processor"), "processor"),
            )
            self.compression_runs[operation_id] = run
            before = _compression_metric(body, "before")
            run.before_messages = _compression_int(before.get("messages"))
            run.before_tokens = _compression_int(before.get("tokens"))
            percent = before.get("context_percent")
            if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                run.before_percent = int(percent)
        if status == _COMPRESSION_STARTED and run.state != _COMPRESSION_STARTED:
            return
        run.state = status
        if status == _COMPRESSION_STARTED:
            return
        saved = _compression_metric(body, "saved")
        run.saved_messages = _compression_int(saved.get("messages"))
        run.saved_tokens = _compression_int(saved.get("tokens"))
        percent_saved = saved.get("percent")
        run.saved_percent = (
            float(percent_saved)
            if isinstance(percent_saved, (int, float))
            and not isinstance(percent_saved, bool)
            else 0.0
        )
        duration = body.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            run.duration_ms = max(0, int(duration))
        run.cancelled = (
            str(body.get("error") or "").strip().lower()
            == _COMPRESSION_CANCELLED_ERROR
        )


class ActivityCardState:
    """Own activity records, resumed-request aliases, and their async lock."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], _SlackActivityRecord] = {}
        self.request_aliases: dict[str, str] = {}
        self.lock = asyncio.Lock()

    def clear(self) -> None:
        for record in self.records.values():
            if record.post_timer is not None:
                record.post_timer.cancel()
        self.records.clear()
        self.request_aliases.clear()

    def _prune_closed(self) -> None:
        cutoff = time.monotonic() - _ACTIVITY_RECORD_TIMEOUT_SECONDS
        closed = [key for key, record in self.records.items() if record.closed]
        for key in closed:
            if self.records[key].touched_at < cutoff:
                del self.records[key]
        closed = [key for key in closed if key in self.records]
        while len(closed) >= _MAX_CLOSED_ACTIVITY_RECORDS:
            oldest = min(closed, key=lambda key: self.records[key].touched_at)
            del self.records[oldest]
            closed.remove(oldest)

    def remember_alias(self, resumed_id: str, original_id: str) -> None:
        if not resumed_id or not original_id:
            return
        original_id = self.request_aliases.get(original_id, original_id)
        if resumed_id == original_id:
            return
        while len(self.request_aliases) >= _MAX_ACTIVITY_REQUEST_ALIASES:
            self.request_aliases.pop(next(iter(self.request_aliases)))
        self.request_aliases[resumed_id] = original_id

    def key(self, request_id: str, channel_id: str) -> tuple[str, str] | None:
        if not request_id or not channel_id:
            return None
        return self.request_aliases.get(request_id, request_id), channel_id

    def record(
        self, key: tuple[str, str], *, thread_ts: str, session_id: str
    ) -> _SlackActivityRecord:
        current = self.records.get(key)
        if current is None:
            self._prune_closed()
            current = _SlackActivityRecord(
                request_id=key[0], channel_id=key[1],
                thread_ts=thread_ts, session_id=session_id,
            )
            self.records[key] = current
        elif not current.session_id and session_id:
            current.session_id = session_id
        return current


class ActivityCardRenderer:
    """Render a turn record as Slack task cards."""

    stop_action_id = ""
    #: How the class that uses this mixin tags its log lines. A connector sets its own.
    _log_tag: str = CHANNEL_LOG_TAG

    @staticmethod
    def _task_card(
        task_id: str,
        title: str,
        status: str,
        *,
        details: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        card: dict[str, Any] = {"type": "task_card", "task_id": task_id, "title": title, "status": status}
        if details is not None:
            card["details"] = details
        if output is not None:
            card["output"] = output
        return card

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        whole = max(0, int(seconds))
        if whole < 60:
            return f"{whole}s"
        minutes, secs = divmod(whole, 60)
        if minutes < 60:
            return f"{minutes}m {secs:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"

    @staticmethod
    def _counts_line(
        counts: Mapping[str, int], states: tuple[tuple[str, str], ...], *, label: str = ""
    ) -> list[dict[str, Any]]:
        line: list[dict[str, Any]] = []
        if label:
            line.append({"type": "text", "text": label, "style": {"code": True}})
        for state, emoji in states:
            count = counts.get(state, 0)
            if not count:
                continue
            if line:
                line.append({"type": "text", "text": "  "})
            line.extend(({"type": "emoji", "name": emoji}, {"type": "text", "text": f" {count}"}))
        return line

    @staticmethod
    def _rich_text_lines(lines: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
        elements: list[dict[str, Any]] = []
        for line in lines:
            if not line:
                continue
            if elements:
                elements.append({"type": "text", "text": "\n"})
            elements.extend(line)
        if not elements:
            return None
        return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}

    @classmethod
    def _grouped_lines(
        cls, by_label: Mapping[str, Mapping[str, int]], states: tuple[tuple[str, str], ...]
    ) -> list[list[dict[str, Any]]]:
        lines = [cls._counts_line(by_label[label], states, label=label) for label in sorted(by_label)]
        if len(lines) > _MAX_CARD_DETAIL_LINES:
            hidden = len(lines) - _MAX_CARD_DETAIL_LINES
            lines = lines[:_MAX_CARD_DETAIL_LINES]
            lines.append([{"type": "text", "text": f"and {hidden} more"}])
        return lines

    @staticmethod
    def _runs_card_status(counts: Mapping[str, int]) -> str:
        if counts.get(_RUN_RUNNING):
            return _CARD_STATUS_IN_PROGRESS
        if counts.get(_RUN_FAILED) or counts.get(_RUN_UNREPORTED):
            return _CARD_STATUS_ERROR
        return _CARD_STATUS_COMPLETE

    @classmethod
    def _thinking_summary(cls, record: _SlackActivityRecord) -> str:
        """Return the running or settled thinking duration."""
        if not record.reasoning_seen:
            return ""
        elapsed = cls._format_elapsed(record.reasoning_seconds())
        if record.reasoning_since is not None:
            return f"thinking for {elapsed}"
        return f"thought for {elapsed}"

    @staticmethod
    def _format_count(value: int) -> str:
        """Format a count with ASCII thousands separators."""
        return f"{int(value):,}"

    @classmethod
    def _compression_notes(cls, run: _SlackCompressionRun) -> list[str]:
        """Describe one compaction pass with counts, savings, and elapsed time."""
        elapsed = run.elapsed_seconds()
        if run.state == _COMPRESSION_STARTED:
            notes: list[str] = []
            if run.before_messages:
                notes.append(f"{cls._format_count(run.before_messages)} messages")
            if run.before_tokens:
                notes.append(f"{cls._format_count(run.before_tokens)} tokens")
            if run.before_percent is not None:
                notes.append(f"{int(run.before_percent)}% of window")
            notes.append(cls._format_elapsed(elapsed))
            return notes
        tail = [cls._format_elapsed(elapsed)] if elapsed >= _MIN_COMPRESSION_ELAPSED_SECONDS else []
        if run.saved_anything():
            saved: list[str] = []
            if run.saved_messages:
                saved.append(f"{cls._format_count(run.saved_messages)} messages")
            if run.saved_tokens:
                saved.append(f"{cls._format_count(run.saved_tokens)} tokens")
            phrase = "saved " + ", ".join(saved)
            if run.saved_percent:
                phrase += f" ({round(run.saved_percent)}%)"
            return [phrase, *tail]
        if run.cancelled:
            return [_COMPRESSION_CANCELLED_ERROR, *tail]
        return [_COMPRESSION_STATE_WORDING.get(run.state, run.state), *tail]

    @classmethod
    def _compression_lines(cls, record: _SlackActivityRecord) -> list[list[dict[str, Any]]]:
        """Render compaction passes in execution order."""
        runs = list(record.compression_runs.values())
        lines: list[list[dict[str, Any]]] = []
        for run in runs[:_MAX_CARD_DETAIL_LINES]:
            line: list[dict[str, Any]] = [
                {
                    "type": "emoji",
                    "name": _COMPRESSION_STATE_EMOJI.get(run.state, _COMPRESSION_STATE_EMOJI[_RUN_UNREPORTED]),
                },
                {"type": "text", "text": " "},
                {"type": "text", "text": run.label, "style": {"code": True}},
            ]
            line.extend({"type": "text", "text": f"  {note}"} for note in cls._compression_notes(run))
            lines.append(line)
        hidden = len(runs) - _MAX_CARD_DETAIL_LINES
        if hidden > 0:
            lines.append([{"type": "text", "text": f"and {hidden} more"}])
        return lines

    @classmethod
    def _compression_card(cls, record: _SlackActivityRecord) -> dict[str, Any] | None:
        """Show compaction pass status without exposing its summary text."""
        runs = list(record.compression_runs.values())
        if not runs:
            return None
        if record.compression_running():
            status = _CARD_STATUS_IN_PROGRESS
        elif any(run.state in (_COMPRESSION_FAILED, _RUN_UNREPORTED) for run in runs):
            status = _CARD_STATUS_ERROR
        else:
            status = _CARD_STATUS_COMPLETE
        return cls._task_card(
            f"{_COMPRESSION_TASK_ID_PREFIX}{record.request_id}",
            _COMPRESSION_CARD_TITLE,
            status,
            details=cls._rich_text_lines(cls._compression_lines(record)),
        )

    @classmethod
    def _compression_summary(cls, record: _SlackActivityRecord) -> str:
        """Summarize running or completed compaction for the notification."""
        runs = list(record.compression_runs.values())
        if not runs:
            return ""
        running = [run for run in runs if run.state == _COMPRESSION_STARTED]
        if running:
            largest = max(running, key=lambda run: run.before_messages)
            if largest.before_messages:
                return f"compacting {cls._format_count(largest.before_messages)} messages of context"
            return "compacting context"
        if any(run.state == _COMPRESSION_FAILED for run in runs):
            if all(run.cancelled for run in runs if run.state == _COMPRESSION_FAILED):
                return "context compaction cancelled"
            return "context compaction failed"
        if any(run.state == _RUN_UNREPORTED for run in runs):
            return "context compaction reported no result"
        saved = record.compression_saved_tokens()
        if saved:
            return f"context compacted, {cls._format_count(saved)} tokens saved"
        return "context compaction saved nothing"

    @classmethod
    def _thinking_card(cls, record: _SlackActivityRecord) -> dict[str, Any] | None:
        """Show the thinking duration without exposing reasoning text."""
        if not record.reasoning_seen:
            return None
        thinking = record.reasoning_since is not None
        emoji = _THINKING_RUNNING_EMOJI if thinking else _THINKING_ENDED_EMOJI
        return cls._task_card(
            f"{_THINKING_TASK_ID_PREFIX}{record.request_id}",
            _THINKING_CARD_TITLE,
            _CARD_STATUS_IN_PROGRESS if thinking else _CARD_STATUS_COMPLETE,
            details=cls._rich_text_lines(
                [[{"type": "emoji", "name": emoji}, {"type": "text", "text": f" {cls._thinking_summary(record)}"}]]
            ),
        )

    @classmethod
    def _todo_card(cls, record: _SlackActivityRecord) -> dict[str, Any] | None:
        """Render nonempty todo counts."""
        counts = record.todo_counts
        total = sum(counts.values())
        if not total:
            return None
        done = counts.get(_TODO_COMPLETED, 0)
        if done >= total:
            status = _CARD_STATUS_COMPLETE
        elif done or counts.get(_TODO_IN_PROGRESS, 0):
            status = _CARD_STATUS_IN_PROGRESS
        else:
            status = _CARD_STATUS_PENDING
        return cls._task_card(
            f"todos-{record.request_id}",
            _TODO_CARD_TITLE,
            status,
            details=cls._rich_text_lines([cls._counts_line(counts, _TODO_STATE_EMOJI)]),
        )

    @classmethod
    def _subagent_card(cls, record: _SlackActivityRecord) -> dict[str, Any] | None:
        """Show subagent counts by outcome and type."""
        runs = list(record.subagent_runs.values())
        if not runs:
            return None
        totals = record.subagent_totals()
        return cls._task_card(
            f"subagents-{record.request_id}",
            _SUBAGENT_CARD_TITLE,
            cls._runs_card_status(totals),
            details=cls._rich_text_lines([cls._counts_line(totals, _RUN_STATE_EMOJI)]),
            output=cls._rich_text_lines(cls._grouped_lines(record.subagents_by_type(), _RUN_STATE_EMOJI)),
        )

    @classmethod
    def _tool_card(cls, record: _SlackActivityRecord) -> dict[str, Any] | None:
        """Show parent tool counts and currently running tools."""
        by_tool = record.tools_by_name()
        if not by_tool:
            return None
        return cls._task_card(
            f"tools-{record.request_id}",
            _TOOL_CARD_TITLE,
            cls._runs_card_status(record.tool_totals()),
            details=cls._rich_text_lines(cls._grouped_lines(by_tool, _RUN_STATE_EMOJI)),
            output=cls._rich_text_lines(cls._running_tool_lines(record)),
        )

    @classmethod
    def _running_tool_lines(cls, record: _SlackActivityRecord) -> list[list[dict[str, Any]]]:
        """Render running tools in start order."""
        running = sorted(
            (run for run in record.tool_runs.values() if run.state == _RUN_RUNNING), key=lambda run: run.started_at
        )
        if not running:
            return []
        now = time.monotonic()
        lines = [
            [
                {"type": "emoji", "name": _RUN_STATE_EMOJI_BY_STATE[_RUN_RUNNING]},
                {"type": "text", "text": " "},
                {"type": "text", "text": run.label, "style": {"code": True}},
                {"type": "text", "text": f"  {cls._format_elapsed(now - run.started_at)}"},
            ]
            for run in running[:_MAX_RUNNING_TOOL_LINES]
        ]
        hidden = len(running) - _MAX_RUNNING_TOOL_LINES
        if hidden > 0:
            lines.append([{"type": "text", "text": f"and {hidden} more"}])
        return lines

    @classmethod
    def _error_card(cls, record: _SlackActivityRecord) -> dict[str, Any] | None:
        """Show a turn failure separately from tool and subagent outcomes."""
        if not record.failed:
            return None
        return cls._task_card(
            f"error-{record.request_id}",
            _ERROR_CARD_TITLE,
            _CARD_STATUS_ERROR,
            details=cls._error_details(record.harness_error),
        )

    @staticmethod
    def _error_details(error: str) -> dict[str, Any]:
        text = (error or "").strip()
        elements: list[dict[str, Any]] = [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": _HARNESS_ERROR_LEAD}]}
        ]
        if text:
            elements.append(
                {"type": "rich_text_preformatted", "elements": [{"type": "text", "text": _clamp_harness_error(text)}]}
            )
        return {"type": "rich_text", "elements": elements}

    @classmethod
    def _activity_title(cls, record: _SlackActivityRecord) -> str:
        """Choose the activity card title from the current turn state."""
        return progress.turn_title(record, closed=record.closed, failed=record.failed)

    @classmethod
    def _activity_summary(cls, record: _SlackActivityRecord) -> str:
        parts = [cls._activity_title(record)]
        compression = cls._compression_summary(record)
        if compression:
            parts.append(compression)
        thinking = cls._thinking_summary(record)
        if thinking:
            parts.append(thinking)
        todo_total = sum(record.todo_counts.values())
        if todo_total:
            done = record.todo_counts.get(_TODO_COMPLETED, 0)
            noun = "todo" if todo_total == 1 else "todos"
            parts.append(f"{done} of {todo_total} {noun} done")
        parts.extend(
            summary
            for summary in (
                cls._runs_summary(record.subagent_totals(), "subagent"),
                cls._runs_summary(record.tool_totals(), "tool"),
            )
            if summary
        )
        if record.harness_error:
            parts.append(_clamp(" ".join(record.harness_error.split()), _MAX_SUMMARY_ERROR_LENGTH))
        return " - ".join(parts)

    _runs_summary = staticmethod(progress.runs_summary)

    @classmethod
    def _activity_blocks(cls, record: _SlackActivityRecord) -> list[dict[str, Any]] | None:
        """Build the activity card blocks from the turn record."""
        cards = [
            card
            for card in (
                cls._compression_card(record),
                cls._thinking_card(record),
                cls._todo_card(record),
                cls._subagent_card(record),
                cls._tool_card(record),
                cls._error_card(record),
            )
            if card is not None
        ]
        if not cards:
            # A running turn that has reported nothing yet shows its title and the stop button only.
            stop = cls._stop_blocks(record)
            if not stop or not record.shows_nothing():
                return None
            title = {"type": "mrkdwn", "text": f"*{_clamp(cls._activity_title(record), _MAX_CARD_TITLE_LENGTH)}*"}
            return [{"type": "section", "text": title}, *stop]
        blocks: list[dict[str, Any]] = [
            {"type": "plan", "title": _clamp(cls._activity_title(record), _MAX_CARD_TITLE_LENGTH), "tasks": cards}
        ]
        blocks.extend(cls._stop_blocks(record))
        return blocks

    @classmethod
    def _card_shows_only_thinking(cls, record: _SlackActivityRecord) -> bool:
        """Check whether the posted card contains only thinking state."""
        blocks = cls._activity_blocks(record)
        if blocks is None:
            return False
        tasks = blocks[0].get("tasks") or []
        if len(tasks) != 1:
            return False
        return str(tasks[0].get("task_id") or "").startswith(_THINKING_TASK_ID_PREFIX)

    @classmethod
    def _discard_reason(cls, record: _SlackActivityRecord, *, withheld: bool) -> str:
        """Return why a settled card should be removed, or an empty string."""
        if record.failed:
            # A failed turn's card keeps what it showed. One that showed nothing leaves the failure to its notice.
            return "nothing shown" if record.shows_nothing() and not record.harness_error else ""
        if reason := progress.card_discard_reason(silent=withheld, stopped=record.stopped, empty=record.shows_nothing()):
            return reason
        if cls._card_shows_only_thinking(record):
            return "thinking only"
        return ""

    @classmethod
    def _stop_blocks(cls, record: _SlackActivityRecord) -> list[dict[str, Any]]:
        """Build the stopped card without claiming unfinished work succeeded."""
        if record.closed or record.stop_requested or not record.session_id or not cls.stop_action_id:
            return []
        return [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": cls.stop_action_id,
                        "text": {"type": "plain_text", "text": _clamp(_STOP_BUTTON_LABEL, _MAX_BUTTON_TEXT_LENGTH)},
                        "style": "danger",
                        "value": cls._stop_button_value(record),
                    }
                ],
            }
        ]

    @staticmethod
    def _stop_button_value(record: _SlackActivityRecord) -> str:
        return json.dumps(
            {"session_id": record.session_id, "request_id": record.request_id, "channel_id": record.channel_id},
            separators=(",", ":"),
        )

    @classmethod
    def _activity_block_kind(cls, record: _SlackActivityRecord) -> str:
        return slack_blocks.BLOCK_KIND_INTERACTIVE if cls._stop_blocks(record) else slack_blocks.BLOCK_KIND_CHROME

    def _remember_activity_alias(self, resumed_id: str, original_id: str) -> None:
        self._activity_state.remember_alias(resumed_id, original_id)

    async def _track_tool_call(
        self, key: tuple[str, str], record: _SlackActivityRecord, call_id: str, name: str
    ) -> None:
        """Fold one tool call into the turn's card. Caller holds the lock."""
        record.reopen()
        record.end_reasoning()
        record.note_tool_call(call_id, run_label(name, "tool"))
        record.touched_at = time.monotonic()
        await self._arm_or_refresh(key, record, force=False)

    async def _track_tool_result(self, record: _SlackActivityRecord, call_id: str, state: str) -> None:
        """Fold how one tool call ended into the turn's card. Caller holds the lock."""
        run = record.tool_runs.get(call_id)
        if run is not None:
            run.state = state
            record.touched_at = time.monotonic()
            await self._refresh_activity_card(record, force=False)

    async def _track_subagent_run(
        self,
        key: tuple[str, str],
        record: _SlackActivityRecord,
        subagent_id: str,
        label: str,
        state: str,
        *,
        closed: bool = False,
    ) -> None:
        """Fold one subagent's state into the turn's card. Caller holds the lock."""
        run_id = subagent_run_id(subagent_id)
        run = record.subagent_runs.get(run_id)
        previous = run.state if run is not None else None
        if run is None:
            record.subagent_runs[run_id] = _SlackActivityRun(call_id=run_id, label=label, state=state)
        elif previous != state:
            # A closed instance must not erase an outcome already shown.
            if closed and previous in (_RUN_DONE, _RUN_FAILED):
                return
            run.state = state
        else:
            return
        if state == _RUN_RUNNING:
            record.reopen()
            record.end_reasoning()
        record.touched_at = time.monotonic()
        # A subagents section that appears or reopens shows at once. A run that joins a running section waits.
        force = state != _RUN_RUNNING or record.subagent_card_status != _CARD_STATUS_IN_PROGRESS
        await self._arm_or_refresh(key, record, force=force)

    async def _arm_or_refresh(
        self, key: tuple[str, str], record: _SlackActivityRecord, *, force: bool
    ) -> None:
        """Start the delayed post or rewrite the card. Caller holds the lock."""
        if not record.message_ts:
            if record.post_timer is None:
                record.post_timer = asyncio.create_task(
                    self._post_activity_card_later(key),
                    name="slack-activity-card",
                )
            return
        await self._refresh_activity_card(record, force=force)

    async def _post_activity_card_later(self, key: tuple[str, str]) -> None:
        """Post an activity card only if the turn remains active after the delay."""
        delay = max(0.0, float(self.config.activity_card_delay_seconds or 0.0))
        try:
            await asyncio.sleep(delay)
            async with self._activity_lock:
                record = self._activity_records.get(key)
                if record is None:
                    return
                record.post_timer = None
                if record.message_ts or record.closed:
                    return
                if self._client is None:
                    self._activity_records.pop(key, None)
                    return
                await self._post_activity_card(key, record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Slack turn activity card failed for request %s",
                key[0],
                exc_info=True,
            )

    async def _post_activity_card(
        self, key: tuple[str, str], record: _SlackActivityRecord
    ) -> bool:
        """Post the first activity card and report whether it landed."""
        blocks = self._activity_blocks(record)
        if blocks is None:
            return False
        sent, message_ts, error = await self._post_text(
            block_kind=self._activity_block_kind(record),
            channel_id=record.channel_id,
            text=self._activity_summary(record),
            thread_ts=record.thread_ts,
            blocks=blocks,
        )
        if not sent:
            logger.warning(
                "Slack could not post the turn activity card for request %s (%s)",
                record.request_id,
                error,
            )
            self._activity_records.pop(key, None)
            return False
        record.message_ts = message_ts
        record.remember_written(blocks)
        record.next_edit_at = time.monotonic() + max(
            0.0, float(self.config.activity_card_min_edit_seconds or 0.0)
        )
        return True

    async def _refresh_activity_card(
        self, record: _SlackActivityRecord, *, force: bool
    ) -> bool:
        """Rewrite a posted card when its edit interval permits."""
        if not record.message_ts:
            return False
        now = time.monotonic()
        # After a failed edit, Slack can be rate limiting, so a forced edit waits too.
        if (not force or record.edit_failed) and now < record.next_edit_at:
            self._hold_activity_edit(record)
            return False
        if record.post_timer is not None:
            # This edit shows the latest state that the held edit would show.
            record.post_timer.cancel()
            record.post_timer = None
        blocks = self._activity_blocks(record)
        if blocks is None:
            return False
        sent, _, error = await self._post_text(
            block_kind=self._activity_block_kind(record),
            channel_id=record.channel_id,
            text=self._activity_summary(record),
            thread_ts=record.thread_ts,
            update_ts=record.message_ts,
            blocks=blocks,
        )
        record.edit_failed = not sent
        if not sent:
            logger.warning(
                "Slack could not update the turn activity card for request "
                "%s (%s); leaving the card as it was",
                record.request_id,
                error,
            )
            if not record.closed:
                # The held edit tries again when the interval ends. A settled card is not written again.
                self._hold_activity_edit(record)
        else:
            record.remember_written(blocks)
        record.next_edit_at = now + max(
            0.0, float(self.config.activity_card_min_edit_seconds or 0.0)
        )
        return sent

    def _hold_activity_edit(self, record: _SlackActivityRecord) -> None:
        """Write the latest state of the card when the edit interval ends."""
        if record.post_timer is None:
            record.post_timer = asyncio.create_task(
                self._refresh_activity_card_later(record), name="slack-activity-card-edit"
            )

    async def _refresh_activity_card_later(self, record: _SlackActivityRecord) -> None:
        """Write the held edit when the edit interval ends."""
        await asyncio.sleep(max(0.0, record.next_edit_at - time.monotonic()))
        async with self._activity_lock:
            record.post_timer = None
            try:
                await self._refresh_activity_card(record, force=True)
            except Exception:
                logger.warning(
                    "Slack turn activity card edit failed for request %s",
                    record.request_id,
                    exc_info=True,
                )

    async def _discard_activity_card(
        self,
        key: tuple[str, str],
        record: _SlackActivityRecord,
        *,
        reason: str,
    ) -> bool:
        """Remove an activity card and report whether removal succeeded."""
        if record.harness_error or (record.failed and not record.shows_nothing()):
            logger.error(
                "%s refusing to delete the activity card of a"
                " failed turn: request=%s channel=%s",
                self._log_tag,
                record.request_id,
                record.channel_id,
            )
            return False
        if self._client is None or not record.message_ts:
            return False
        channel_id, message_ts = record.channel_id, record.message_ts
        try:
            await self._client.chat_delete(channel=channel_id, ts=message_ts)
        except Exception as exc:  # noqa: BLE001 - Slack SDK failures must preserve the card.
            logger.warning(
                "%s the turn's activity card could not be deleted"
                " and is still on screen: channel=%s ts=%s discard=%s reason=%s",
                self._log_tag,
                channel_id,
                message_ts,
                reason,
                slack_failure_reason(exc),
            )
            return False
        record.message_ts = ""
        self._activity_records.pop(key, None)
        logger.info(
            "%s activity card deleted (%s): channel=%s ts=%s",
            self._log_tag,
            reason,
            channel_id,
            message_ts,
        )
        return True


def _clamp_harness_error(text: str) -> str:
    """Trim a long error while marking that it was trimmed."""
    room = _MAX_CARD_DETAILS_LENGTH - len(_HARNESS_ERROR_LEAD)
    if len(text) <= room:
        return text
    keep = max(0, room - len(_HARNESS_ERROR_TRIM_MARKER))
    return text[:keep].rstrip() + _HARNESS_ERROR_TRIM_MARKER
