"""How a reply shown while it was written becomes the reply that was owed.

A connector that streams a reply opens one of the surfaces of
``streaming.py`` and writes to it as text arrives. This mixin holds what the
surface is given to call Slack with, and how the reply ends: the stream is
closed with the rest of the text, and a stream that will take no more is
rewritten whole or continued in a message of its own.
"""

import logging
from typing import Any

from belle_agent.platforms.slack import streaming as slack_streaming
from belle_agent.platforms.slack.activity import _ACTIVITY_TITLE_STOPPED
from belle_agent.platforms.slack.streaming import (
    SlackSurfaceIO,
    _SlackMessageSurface,
    _SlackStream,
    _SlackStreamingSurface,
    call_stream_method,
    extract_block_request,
    split_threaded_report,
)
from belle_agent.platforms.slack.text import (
    MAX_SLACK_UPDATE_TEXT_LENGTH,
    normalize_slack_mrkdwn,
    preferred_split_index,
    split_text,
)

logger = logging.getLogger(__name__)

# Slack reports this when chat.update targets a message still owned by streaming.
_STREAM_STATE_CONFLICT = "streaming_state_conflict"
# Marks a preview as still being written, so a cut prefix does not read as a
# finished but oddly abrupt answer.
STREAM_TRUNCATION_SUFFIX = "…"
# What a reply that was being written when its turn was stopped ends with.
STREAM_STOPPED_TAIL = f"\n\n_{_ACTIVITY_TITLE_STOPPED}_"


class SlackStreamDelivery:
    #: Where the class that uses this mixin logs, and how its lines are tagged. A connector sets its own.
    _log: logging.Logger = logger
    _log_tag: str = "[slack]"

    async def _finish_streamed_reply(
        self,
        surface: _SlackStreamingSurface,
        *,
        handle: str,
        channel_id: str,
        thread_ts: str,
        content: str,
        block_request: bool | None,
    ) -> bool:
        """Finish the surface, retaining host control of failed-close recovery."""
        return await surface.finish_reply(
            handle, content,
            lambda tail: self._blocks_for(
                normalize_slack_mrkdwn(tail), block_request
            ),
            lambda remainder: self._recover_streamed_reply(
                surface, handle=handle, channel_id=channel_id,
                thread_ts=thread_ts, remainder=remainder,
                block_request=block_request,
            ),
        )

    async def _recover_streamed_reply(
        self,
        surface: _SlackStreamingSurface,
        *,
        handle: str,
        channel_id: str,
        thread_ts: str,
        remainder: str,
        block_request: bool | None,
    ) -> bool:
        """Try fallbacks for a stream that could not close."""
        if not remainder.strip():
            self._log.warning(
                "%s the stream could not be stopped but the reply "
                "is complete on screen: channel=%s reason=%s",
                self._log_tag,
                channel_id,
                surface.error or "-",
            )
            return False

        # Preserve the partial message when the reader stopped the stream.
        if not surface.stopped_by_user and await self._rewrite_streamed_message(
            surface,
            handle=handle,
            channel_id=channel_id,
            remainder=remainder,
            block_request=block_request,
        ):
            return True

        chunks = split_text(normalize_slack_mrkdwn(remainder))
        if not chunks:
            return False
        self._log.warning(
            "%s the stream would take no more (%s); posting the "
            "rest of the reply as a message of its own: channel=%s chunks=%d",
            self._log_tag,
            surface.error or "-",
            channel_id,
            len(chunks),
        )
        await self._post_chunks(
            chunks, channel_id=channel_id, thread_ts=thread_ts,
            block_request=block_request,
        )
        return False

    async def _rewrite_streamed_message(
        self,
        surface: _SlackStreamingSurface,
        *,
        handle: str,
        channel_id: str,
        remainder: str,
        block_request: bool | None,
    ) -> bool:
        """Rewrite a failed stream when the reply fits in one message."""
        whole = normalize_slack_mrkdwn(
            f"{surface.sent_text}{remainder}"
        ).strip()
        if len(whole) > MAX_SLACK_UPDATE_TEXT_LENGTH:
            return False

        whole_blocks, whole_kind = self._blocks_and_kind_for(whole, block_request)
        sent, error = await surface.rewrite_message(
            handle, whole, whole_blocks or [], whole_kind
        )
        if sent:
            return True

        if _STREAM_STATE_CONFLICT not in error:
            return False

        # The message is still streaming; resume decides whether close can retry.
        cleared = surface.resume()
        if not cleared:
            self._log.warning(
                "%s the stream is still open but will not take this "
                "close (%s); not asking it twice: channel=%s ts=%s",
                self._log_tag,
                surface.error or "-",
                channel_id,
                handle,
            )
            return False
        self._log.info(
            "%s the stream is still open after all (%s); closing it "
            "again: channel=%s ts=%s",
            self._log_tag,
            cleared,
            channel_id,
            handle,
        )
        return await surface.stop_with_tail(
            handle, remainder,
            lambda tail: self._blocks_for(
                normalize_slack_mrkdwn(tail), block_request
            ),
        )

    async def _start_stream(self, stream: _SlackStream) -> None:
        """Open a stream, falling back to an editable preview."""
        if stream.failed or not stream.surface.ready_to_open(stream.text):
            return
        if await self._open_session(stream):
            return
        if isinstance(stream.surface, _SlackStreamingSurface):
            stream.surface = _SlackMessageSurface(
                self._stream_surface_io(), stream.channel_id, stream.thread_ts
            )
            if await self._open_session(stream):
                self._log.info(
                    "%s the stream could not be opened; showing "
                    "this reply with edits instead: channel=%s",
                    self._log_tag,
                    stream.channel_id,
                )
                return
        # Keep the failed stream so later deltas do not reopen it.
        stream.failed = True

    async def _finish_stopped_stream(
        self, stream: _SlackStream, channel_id: str
    ) -> None:
        """Close an abandoned stream and mark its visible result."""
        if stream.session is None:
            # Never opened: nothing on screen, and nothing to take back.
            return
        try:
            handle = await stream.session.stop()
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Slack streaming could not be closed after a stop: %s", exc
            )
            return
        surface = stream.surface
        if isinstance(surface, _SlackMessageSurface) and handle:
            preview = self._stream_snapshot(stream.text).rstrip()
            # Slack's edit limit must leave room for the stopped marker.
            budget = MAX_SLACK_UPDATE_TEXT_LENGTH - len(STREAM_STOPPED_TAIL)
            if len(preview) > budget:
                preview = preview[:preferred_split_index(preview, budget)].rstrip()
            await surface.write(handle, preview + STREAM_STOPPED_TAIL, 0)
            return
        if not isinstance(surface, _SlackStreamingSurface) or not handle:
            return
        chunks = (
            []
            if surface.failed
            else surface._markdown_chunks(STREAM_STOPPED_TAIL)
        )
        stopped = await surface.stop_stream(handle, chunks=chunks)
        self._log.info(
            "%s streaming stopped with the turn: channel=%s ts=%s "
            "appends=%d streamed_chars=%d closed=%s marked=%s",
            self._log_tag,
            channel_id,
            surface.opened_ts or handle or "-",
            surface.edits,
            len(surface.sent_text),
            "yes" if stopped else "no",
            "yes" if stopped and chunks else "no",
        )

    def _stream_surface_io(self) -> SlackSurfaceIO:
        return SlackSurfaceIO(
            post_text=self._post_text,
            call_stream_method=self._call_stream_method,
            wait_for_slot=self._wait_for_stream_slot,
            render_snapshot=self._stream_snapshot,
            logger=self._log,
            tag=self._log_tag,
        )

    async def _wait_for_stream_slot(
        self, channel_id: str, interval: float | None = None
    ) -> None:
        """Wait for the next channel write slot shared by all streams."""
        await self._stream_state.wait_for_slot(
            channel_id,
            slack_streaming._STREAM_MIN_UPDATE_INTERVAL_SECONDS if interval is None else interval,
        )

    async def _call_stream_method(self, method: str, **kwargs: Any) -> Any:
        return await call_stream_method(
            self._client,
            method,
            logger=self._log,
            retry_limit=3,
            **kwargs,
        )

    def _stream_snapshot(self, text: str) -> str:
        """Render the first text chunk within the chat.update limit.

        The terminal send renders blocks and all remaining chunks."""
        text, _ = extract_block_request(text)
        # The first piece only: the preview is the message the deltas are being
        # written into, and that message is the root. Every further piece becomes
        # a reply the closing rewrite posts, however many there are.
        summary = split_threaded_report(text)[0]
        return self._clamp_stream_preview(normalize_slack_mrkdwn(summary))

    @classmethod
    def _clamp_stream_preview(cls, text: str) -> str:
        """Trim a preview at a safe split point. The final reply contains all text."""
        text = text.strip()
        if len(text) <= MAX_SLACK_UPDATE_TEXT_LENGTH:
            return text
        budget = MAX_SLACK_UPDATE_TEXT_LENGTH - len(STREAM_TRUNCATION_SUFFIX)
        split_at = preferred_split_index(text, budget)
        return text[:split_at].rstrip() + STREAM_TRUNCATION_SUFFIX
