"""A reply shown in Slack while it is written, for a connector on the conversation host.

The host hands the connector the deltas of a reply whose final it will post,
and then the final. The reply is shown in Slack's own stream, or in a message
that is edited where a stream cannot be had, and it ends as the final: the
stream is closed with the rest of the text, or the message is rewritten to
read as the final, or it is taken down and the final is posted whole.
"""

import logging

from belle_agent.backend import EVENT_STEP, TurnEvent
from belle_agent.platforms.base import MARK_DONE, MARK_STOPPED, InboundMessage
from belle_agent.platforms.slack.config import STREAMING_OFF, STREAMING_STREAM, resolve_streaming_mode
from belle_agent.platforms.slack.delivery import SlackDeliveryError, slack_failure_reason
from belle_agent.platforms.slack.stream_delivery import SlackStreamDelivery
from belle_agent.platforms.slack.streaming import (
    SlackStreamState,
    _SlackStream,
    _SlackStreamingSurface,
    extract_block_request,
)
from belle_agent.platforms.slack.text import MAX_SLACK_UPDATE_TEXT_LENGTH, normalize_slack_mrkdwn, split_text
from belle_agent.streaming import SnapshotWriter

#: The setting that turns the surface on, as the host names it at start.
STREAMING_SETTING = "platforms.slack.enable_streaming"

logger = logging.getLogger(__name__)


class SlackTurnStream(SlackStreamDelivery):
    """Show the reply of a turn as it is written, and end it as one message."""

    def _init_streams(self) -> None:
        self._stream_state = SlackStreamState()

    def _clear_streams(self) -> None:
        for stream in self._stream_state.streams.values():
            if stream.session is not None:
                stream.session.cancel()
        self._stream_state.clear()

    def _streaming_mode(self) -> str:
        return resolve_streaming_mode(self.config.enable_streaming)

    def _streams_replies(self) -> bool:
        return self._streaming_mode() != STREAMING_OFF

    @staticmethod
    def _stream_key(inbound: InboundMessage) -> tuple[str, str, str]:
        return inbound.message_id, inbound.conversation, inbound.reply_thread or ""

    async def _open_session(self, stream: _SlackStream) -> bool:
        """Open the surface and give it a writer. ``False`` if it would not open."""
        try:
            handle = await stream.surface.open(stream.text)
        except Exception as exc:  # noqa: BLE001 - whatever stops a reply being shown, it is still posted whole
            logger.warning("Slack streaming could not start: %s", slack_failure_reason(exc))
            return False
        stream.session = SnapshotWriter(stream.surface, handle)
        return True

    async def _show_reply(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Take one delta of a reply, or the step that starts the reply again."""
        if self._client is None:
            return
        key = self._stream_key(inbound)
        stream = self._stream_state.streams.get(key)
        if event.kind == EVENT_STEP:
            # Slack's stream only appends. What an earlier step wrote stays on screen until the reply ends,
            # a paragraph away from what the next one writes, unless the next one continues it.
            if stream is not None and stream.text.strip() and not event.data.get("continues"):
                stream.text = stream.text.rstrip() + "\n\n"
            return
        if not event.text:
            return
        if stream is None:
            # Slack rejects an empty opening message.
            if not event.text.strip():
                return
            native = self._streaming_mode() == STREAMING_STREAM
            stream = self._stream_state.new_stream(
                key, inbound.conversation, inbound.reply_thread or "",
                io=self._stream_surface_io(), native=native,
                recipient=(inbound.sender, inbound.workspace) if native else ("", ""),
                logger=logger, tag=self._log_tag,
            )
        if stream.failed or stream.surface.failed:
            return
        stream.text += event.text
        if stream.session is None:
            # Slack's stream waits for a complete line.
            await self._start_stream(stream)
        else:
            stream.session.replace(stream.text)

    async def _end_reply(self, inbound: InboundMessage, state: str, final: str) -> bool:
        """End the reply that was shown for a message. Return whether ``final`` is now what is on screen.

        With no final, what was shown was no part of what the turn said, and it
        is taken down. A turn that was stopped keeps what it had written, marked
        as stopped.
        """
        stream = self._stream_state.streams.pop(self._stream_key(inbound), None)
        if stream is None:
            return False
        if state == MARK_STOPPED:
            await self._finish_stopped_stream(stream, stream.channel_id)
            return False
        if stream.session is None:
            # Never opened: nothing is on screen.
            return False
        handle = await stream.session.stop()
        surface = stream.surface
        native = isinstance(surface, _SlackStreamingSurface)
        content, block_request = extract_block_request(final) if state == MARK_DONE else ("", None)
        content = content.strip()
        if content and native and content.startswith(surface.sent_text.rstrip()):
            # What was streamed is the start of the final: the stream is closed with the rest. Where that
            # cannot be done the reply is rewritten whole, or its rest is posted beside it.
            if await self._finish_streamed_reply(
                surface, handle=handle, channel_id=stream.channel_id, thread_ts=stream.thread_ts,
                content=content, block_request=block_request,
            ):
                return True
        else:
            # What is on screen is not the start of the final: an earlier step wrote some of it, a part of it
            # was lost, or it is an edited message. A stream is closed as it is, and the message rewritten.
            if native:
                await surface.stop_stream(handle)
            try:
                if content and await self._rewrite_as_final(stream, handle, content, block_request):
                    return True
            except SlackDeliveryError as exc:
                # The message already reads as the start of the final. Posting the final again would say it twice.
                logger.error(
                    "%s a reply was rewritten as its final and the rest of it could not be posted:"
                    " channel=%s ts=%s reason=%s",
                    self._log_tag, stream.channel_id, handle, exc,
                )
                return True
        await self._take_down_reply(stream.channel_id, handle)
        return False

    async def _rewrite_as_final(self, stream: _SlackStream, handle: str, final: str, block_request: bool | None) -> bool:
        """Make the message that was shown read as the final, with what does not fit posted after it."""
        chunks = split_text(normalize_slack_mrkdwn(final), MAX_SLACK_UPDATE_TEXT_LENGTH)
        if not chunks:
            return False
        blocks, kind = self._blocks_and_kind_for(chunks[0], block_request)
        sent, _, error = await self._post_text(
            channel_id=stream.channel_id, text=chunks[0], thread_ts="", update_ts=handle,
            chunk_index=1, chunk_total=len(chunks), blocks=blocks, block_kind=kind,
        )
        if not sent:
            logger.warning(
                "%s a reply shown while it was written could not be rewritten as its final (%s);"
                " it is taken down and the final is posted whole: channel=%s ts=%s",
                self._log_tag, error or "-", stream.channel_id, handle,
            )
            return False
        await self._post_chunks(
            chunks, channel_id=stream.channel_id, thread_ts=stream.thread_ts, block_request=block_request, start=1
        )
        return True

    async def _take_down_reply(self, channel_id: str, handle: str) -> None:
        """Delete a reply that was shown and is no part of what the turn said."""
        if self._client is None or not handle:
            return
        try:
            await self._client.chat_delete(channel=channel_id, ts=handle)
        except Exception as exc:  # noqa: BLE001 - the SDK's errors vary, and the outcome is the same
            logger.warning(
                "%s a reply shown while it was written could not be taken down and is still on screen:"
                " channel=%s ts=%s reason=%s",
                self._log_tag, channel_id, handle, slack_failure_reason(exc),
            )
            return
        logger.info("%s a reply shown while it was written was taken down: channel=%s ts=%s", self._log_tag, channel_id, handle)
