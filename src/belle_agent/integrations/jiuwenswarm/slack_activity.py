"""Turn activity cards for JiuwenSwarm's Slack channel."""

import time
from collections.abc import Mapping

from jiuwenswarm.extensions.sdk.channel_runtime import RoutingTarget
from jiuwenswarm.extensions.sdk.host_types import EventType, Message

from belle_agent.platforms.slack.activity import (
    _COMPRESSION_STATES,
    _RUN_DONE,
    _RUN_FAILED,
    _RUN_RUNNING,
    _RUN_UNREPORTED,
    ActivityCardRenderer,
    _SlackActivityRecord,
    compression_event,
    subagent_type_label,
    todo_snapshot,
    tool_call_event,
    tool_result_event,
)
from belle_agent.platforms.slack.config import OPTIONAL_REPLY_POST_TOOL
from belle_agent.platforms.slack.streaming import extract_block_request
from belle_agent.silence import is_no_reply_sentinel

_SUBAGENT_TOOL_NAMES = frozenset({"task_tool", "subagent_spawn", "subagent_wait"})


class SlackActivityMixin(ActivityCardRenderer):
    # ------------------------------------------------------------------
    # Turn activity card
    # ------------------------------------------------------------------

    # -- Rendering ------------------------------------------------------

    def _activity_key(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> tuple[str, str] | None:
        request_id = str(msg.id or "").strip() or str(msg.session_id or "").strip()
        if not request_id:
            return None
        channel_id, _ = self._extract_delivery(msg, routing_target)
        return self._activity_state.key(request_id, channel_id)

    def _activity_record(
        self, key: tuple[str, str], msg: Message, routing_target: RoutingTarget | None
    ) -> _SlackActivityRecord:
        thread_ts = (
            self._extract_delivery(msg, routing_target)[1]
            if key not in self._activity_records else ""
        )
        session_id = str(getattr(msg, "session_id", "") or "").strip()
        return self._activity_state.record(
            key, thread_ts=thread_ts, session_id=session_id,
        )

    async def _begin_activity_card(self, session_id: str) -> None:
        """Start the card of a turn owed a reply, so that a turn that calls no tool can be stopped by its button too.

        A turn whose reply is optional may stay silent, and its card comes with its first tool call.
        """
        entry = self.turn_initiator(session_id)
        if entry is None or entry.silence_is_honoured or not self.config.activity_card or self._client is None:
            return
        key = self._activity_state.key(entry.request_id, entry.channel_id)
        if key is None:
            return
        async with self._activity_lock:
            if key in self._activity_records:
                return
            record = self._activity_state.record(key, thread_ts=entry.reply_thread_ts, session_id=session_id)
            await self._arm_or_refresh(key, record, force=False)

    async def _track_activity_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Fold progress events into the turn card."""
        if msg.event_type == EventType.CHAT_SUBTASK_UPDATE:
            return await self._track_subagent_event(msg, routing_target)
        if msg.event_type == EventType.TODO_UPDATED:
            return await self._track_todo_event(msg, routing_target)
        if msg.event_type == EventType.CHAT_REASONING:
            return await self._track_reasoning_event(msg, routing_target)
        if compression_event(msg.payload) is not None:
            return await self._track_compression_event(msg, routing_target)
        if msg.event_type not in (
            EventType.CHAT_TOOL_CALL,
            EventType.CHAT_TOOL_RESULT,
        ):
            return False

        is_call = msg.event_type == EventType.CHAT_TOOL_CALL
        event = tool_call_event(msg.payload) if is_call else tool_result_event(msg.payload)
        if event is None:
            return False
        name, call_id, detail = event
        # Posting tool results affect delivery even when cards are disabled.
        if is_call:
            self._note_posting_tool_call(msg, name)
        else:
            self._note_posting_tool_result(msg, name, detail)
        if name in _SUBAGENT_TOOL_NAMES:
            # The roster carries the subagent state; calls do not count runs.
            return True

        if not self.config.activity_card or self._client is None:
            return False

        key = self._activity_key(msg, routing_target)
        if key is None:
            return False

        async with self._activity_lock:
            if is_call:
                if not call_id:
                    # An unpaired call cannot leave the running state.
                    return True
                record = self._activity_record(key, msg, routing_target)
                await self._track_tool_call(key, record, call_id, name)
                return True

            state = detail
            record = self._activity_records.get(key)
            if record is None or not call_id:
                # No matching run remains on screen.
                return True
            await self._track_tool_result(record, call_id, state)
        return True

    async def _track_subagent_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Count subagent instances from their roster."""
        payload = msg.payload if isinstance(msg.payload, Mapping) else None
        if payload is None:
            return True
        subagent_id = str(payload.get("subagent_id") or "").strip()
        if not subagent_id or not self.config.activity_card or self._client is None:
            return True
        outcome = str(payload.get("turn_outcome") or "").strip().lower()
        status = str(payload.get("status") or "").strip().lower()
        if outcome == "completed":
            state = _RUN_DONE
        elif outcome == "failed":
            state = _RUN_FAILED
        elif outcome == "cancelled":
            state = _RUN_UNREPORTED
        elif status == "running":
            state = _RUN_RUNNING
        elif status == "closed":
            state = _RUN_UNREPORTED
        else:
            # Idle without a reported outcome does not establish a result.
            return True
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_record(key, msg, routing_target)
            await self._track_subagent_run(
                key, record, subagent_id, subagent_type_label(payload), state, closed=status == "closed"
            )
        return True

    async def _track_reasoning_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Record thinking time without reading the reasoning payload."""
        if not self.config.activity_card or self._client is None:
            return False
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_record(key, msg, routing_target)
            # Thinking is working: a request whose card had settled is live
            # again, the same as it would be for a replayed tool call.
            record.reopen()
            record.note_reasoning()
            record.touched_at = time.monotonic()
            await self._arm_or_refresh(key, record, force=False)
        return True

    async def _track_compression_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Track compaction without relaying its error text."""
        body = compression_event(msg.payload)
        if body is None:
            return False
        status = str(body.get("status") or "").strip().lower()
        if status not in _COMPRESSION_STATES:
            return True
        operation_id = str(body.get("operation_id") or "").strip()
        if not operation_id:
            return True
        if not self.config.activity_card or self._client is None:
            return True
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_record(key, msg, routing_target)
            record.reopen()
            # Compaction ends the current thinking stretch.
            record.end_reasoning()
            record.note_compression(operation_id, status, body)
            record.touched_at = time.monotonic()
            await self._arm_or_refresh(key, record, force=True)
        return True

    async def _track_todo_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Replace the turn's todo counts from one ``todo.updated`` snapshot."""
        counts = todo_snapshot(msg.payload)
        if counts is None:
            return False
        if not self.config.activity_card or self._client is None:
            return False
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_records.get(key)
            if record is None:
                if not counts:
                    return True
                record = self._activity_record(key, msg, routing_target)
            record.reopen()
            record.todo_counts = counts
            record.touched_at = time.monotonic()
            await self._arm_or_refresh(key, record, force=False)
        return True

    async def _close_activity_card(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
        *,
        withheld: bool = False,
    ) -> bool:
        """Settle the activity card at the end of a turn, or remove an empty card."""
        failed = msg.event_type == EventType.CHAT_ERROR
        error_text = self._extract_outgoing_text(msg) if failed else ""
        if self._client is None:
            return False
        key = self._activity_key(msg, routing_target)
        if key is None:
            return False
        async with self._activity_lock:
            if not self.config.activity_card:
                # With the card off, only a stop makes a record, and a stop
                # that failed to send leaves it open.
                self._activity_records.pop(key, None)
                return False
            record = self._activity_records.get(key)
            if record is not None and record.closed:
                return False
            if record is None:
                if not failed:
                    return False
                record = self._activity_record(key, msg, routing_target)
            if failed:
                record.failed = True
                record.harness_error = error_text
            record.settle()
            if not record.message_ts:
                if not failed:
                    self._activity_records.pop(key, None)
                    return False
                return await self._post_activity_card(key, record)
            reason = self._discard_reason(record, withheld=withheld)
            if reason and await self._discard_activity_card(
                key, record, reason=reason
            ):
                return False
            written = await self._refresh_activity_card(record, force=True)
            return failed and written

    def _turn_says_nothing(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Check whether an optional turn delivered no visible message."""
        if msg.event_type != EventType.CHAT_FINAL:
            return False
        channel_id, _ = self._extract_delivery(msg, routing_target)
        if not self._reply_was_optional(msg, channel_id):
            return False
        if self._optional_reply_mode() == OPTIONAL_REPLY_POST_TOOL:
            calls = self._posting_tool_record(msg)
            return calls is None or (bool(calls.refused) and not calls.delivered)
        content, _ = extract_block_request(self._extract_outgoing_text(msg))
        return not content or is_no_reply_sentinel(content)
