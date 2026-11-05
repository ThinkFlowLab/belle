"""Outbound replies for the JiuwenSwarm Slack channel."""

import logging
from pathlib import Path

from jiuwenswarm.extensions.sdk.channel_runtime import RoutingTarget
from jiuwenswarm.extensions.sdk.host_types import EventType, Message

from belle_agent.platforms.base import MARK_DONE, MARK_FAILED
from belle_agent.platforms.slack.activity import tool_result_event
from belle_agent.platforms.slack.cron import cron_status_record
from belle_agent.platforms.slack.delivery import SlackDeliveryError
from belle_agent.platforms.slack.file_transfer import outgoing_files, upload_file
from belle_agent.platforms.slack.reply_delivery import SlackReplyDelivery
from belle_agent.platforms.slack.streaming import extract_block_request, split_threaded_report
from belle_agent.platforms.slack.text import MAX_SLACK_UPDATE_TEXT_LENGTH as _MAX_SLACK_UPDATE_TEXT_LENGTH
from belle_agent.platforms.slack.text import split_text

logger = logging.getLogger("belle_agent.integrations.jiuwenswarm.slack_channel")

# Progress events feed the activity card but do not become Slack messages.
_INTERMEDIATE_STREAM_EVENTS = frozenset(
    {
        EventType.CHAT_REASONING,
        EventType.CHAT_TOOL_CALL,
        EventType.CHAT_TOOL_UPDATE,
        EventType.CHAT_TOOL_RESULT,
        EventType.CHAT_SUBTASK_UPDATE,
        EventType.CHAT_PROCESSING_STATUS,
        EventType.CHAT_USAGE_METADATA,
        EventType.CHAT_USAGE_SUMMARY,
        EventType.CHAT_SYMPHONY_STATUS,
        EventType.CHAT_EVOLUTION_STATUS,
        EventType.CHAT_RETRACT,
        EventType.CONTEXT_USAGE,
        EventType.TODO_UPDATED,
    }
)


class SlackOutboundMixin(SlackReplyDelivery):
    async def send(
        self, msg: Message, *, routing_target: RoutingTarget | None = None
    ) -> None:
        """Deliver terminal events to Slack and consume progress events."""
        if self._settle_agent_reply(msg):
            return
        if msg.event_type == EventType.CHAT_DELTA:
            delta = self._extract_delta_text(msg)
            if delta:
                await self._stream_delta(msg, routing_target, delta, stepped=self._step_text(msg).add(delta))
            return
        if msg.event_type == EventType.CHAT_TOOL_RESULT and tool_result_event(msg.payload) is not None:
            self._step_text(msg).step()
        if msg.event_type in (EventType.CHAT_FINAL, EventType.CHAT_ERROR):
            msg = self._with_step_breaks(msg)

        if msg.event_type == EventType.CHAT_FILE:
            await self._send_files(msg, routing_target)
            return

        if msg.event_type == EventType.CHAT_ASK_USER_QUESTION:
            await self._send_question(msg, routing_target)
            return

        if self._is_runtime_accepted(msg):
            session_id = str(msg.session_id or "")
            request_id = str(msg.id or "").strip()
            initiator = self.turn_initiator(session_id)
            if initiator is None or initiator.request_id != request_id:
                # A steer acknowledgment must not close the active turn.
                self._note_ack_only_request(request_id)
            return

        if await self._track_activity_event(msg, routing_target):
            return

        card_carried_error = False
        if msg.event_type in (EventType.CHAT_FINAL, EventType.CHAT_ERROR):
            session_id = str(msg.session_id or "")
            request_id = str(msg.id or "").strip()
            if not self._is_ack_only_request(request_id):
                ended = self._admission.turn_initiators.get(session_id)
                if ended is not None and ended.request_id != request_id:
                    ended = None
                self._forget_turn_initiator(session_id, request_id)
                withheld = self._turn_says_nothing(msg, routing_target)
                card_carried_error = await self._close_activity_card(
                    msg, routing_target, withheld=withheld
                )
                if ended is not None:
                    await self._mark_turn_ended(
                        ended,
                        MARK_FAILED
                        if msg.event_type == EventType.CHAT_ERROR
                        else MARK_DONE,
                    )
                    await self._clear_thinking_status(ended.origin)
                await self._drain_queued_messages(session_id)

        if msg.event_type in _INTERMEDIATE_STREAM_EVENTS:
            return

        content = self._extract_outgoing_text(msg)
        if not content:
            return

        content, block_request = extract_block_request(content)
        if not content:
            return

        if self._withhold_on_no_reply(msg, content, routing_target):
            await self._discard_streamed_preview(msg, routing_target)
            return

        channel_id, thread_ts = self._require_delivery(msg, routing_target)

        stream_ts, streaming = await self._close_stream(msg, channel_id, thread_ts)

        cron = cron_status_record(
            getattr(msg, "payload", None), channel_id, content, self._cron_records
        )

        if card_carried_error and not stream_ts and cron is None:
            logger.info(
                "[SlackChannel] turn failure delivered on the activity card: "
                "channel=%s",
                channel_id,
            )
            return

        root_update_ts = (
            ""
            if streaming is not None
            else stream_ts or (cron.anchor_ts if cron is not None else "")
        )
        first_limit = _MAX_SLACK_UPDATE_TEXT_LENGTH if root_update_ts else 0

        parts = [
            self._normalize_slack_mrkdwn(part)
            for part in split_threaded_report(content)
        ]
        parts = [part for part in parts if part]

        if streaming is not None and await self._finish_streamed_reply(
            streaming,
            handle=stream_ts,
            channel_id=channel_id,
            thread_ts=thread_ts,
            content=content,
            block_request=block_request,
        ):
            return

        report = len(parts) > 1 and not thread_ts
        if report:
            # Keep report sections separate before threading continuations.
            chunks = split_text(parts[0], first_limit)
            for part in parts[1:]:
                chunks.extend(split_text(part))
        else:
            chunks = split_text("\n\n".join(parts), first_limit)
        if not chunks:
            return
        root_blocks, root_kind = self._root_blocks_and_kind_for(
            chunks[0], block_request, cron
        )
        sent, root_ts, error = await self._post_root(
            channel_id=channel_id,
            text=chunks[0],
            thread_ts=thread_ts,
            update_ts=root_update_ts,
            chunk_index=1,
            chunk_total=len(chunks),
            blocks=root_blocks,
            block_kind=root_kind,
            cron=cron,
        )
        if not sent:
            raise SlackDeliveryError(
                error,
                channel_id=channel_id,
                chunks_sent=0,
                chunks_total=len(chunks),
            )
        continuation_ts = root_ts if report or (cron and not thread_ts) else thread_ts
        await self._post_chunks(
            chunks, channel_id=channel_id, thread_ts=continuation_ts,
            block_request=block_request, start=1,
        )

    async def _send_files(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> None:
        """Upload files from a chat.file event."""
        files = outgoing_files(msg.payload)
        if not files:
            logger.warning("Slack chat.file event carried no usable file entries")
            return

        channel_id, thread_ts = self._require_delivery(msg, routing_target)

        total = len(files)
        for index, (path, filename) in enumerate(files):
            if Path(path).is_file():
                sent, error = await upload_file(
                    self._client,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    path=path,
                    filename=filename,
                    file_index=index + 1,
                    file_total=total,
                    retry_limit=3,
                    logger=logger,
                )
            else:
                sent, error = False, f"file no longer exists: {path}"
            if not sent:
                raise SlackDeliveryError(
                    error,
                    channel_id=channel_id,
                    chunks_sent=index,
                    chunks_total=total,
                    unit="files",
                )
