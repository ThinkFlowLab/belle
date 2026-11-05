"""Slack reply streaming for the JiuwenSwarm channel."""

import logging
import time

from jiuwenswarm.extensions.sdk.channel_runtime import RoutingTarget
from jiuwenswarm.extensions.sdk.host_types import Message
from jiuwenswarm.extensions.sdk.streaming import StreamingSession

from belle_agent.integrations.jiuwenswarm.step_text import after_step
from belle_agent.platforms.slack.config import STREAMING_STREAM
from belle_agent.platforms.slack.stream_delivery import SlackStreamDelivery
from belle_agent.platforms.slack.streaming import (
    _SlackStream,
    _SlackStreamingSurface,
)

logger = logging.getLogger("belle_agent.integrations.jiuwenswarm.slack_channel")
_STREAM_DEBOUNCE_MS = 1000
_STREAM_APPEND_DEBOUNCE_MS = 600
_MAX_STREAM_RECIPIENTS = 256


class SlackStreamMixin(SlackStreamDelivery):
    async def _stream_delta(
        self, msg: Message, routing_target: RoutingTarget | None, delta: str, *, stepped: bool
    ) -> None:
        """Apply best-effort stream edits. The final event handles delivery errors.

        ``stepped`` says the delta starts another model call's text, which is shown a paragraph below.
        """
        if not self._streaming_enabled() or self._client is None:
            return
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id:
            return

        key = self._stream_key(msg, channel_id, thread_ts)
        async with self._stream_lock:
            stream = self._streams.get(key)
            if stream is None:
                # Slack rejects an empty opening message.
                if not delta.strip():
                    return
                stream = self._new_stream(key, msg, channel_id, thread_ts)
                stream.text = delta
                await self._start_stream(stream)
                return
            if stream.failed or stream.surface.failed:
                return
            if stepped:
                stream.text = after_step(stream.text)
            stream.text += delta
            stream.touched_at = time.monotonic()
            if stream.session is None:
                # Native streaming waits for a complete line.
                await self._start_stream(stream)
                return
            stream.session.replace(stream.text)

    def _new_stream(
        self,
        key: tuple[str, str, str],
        msg: Message,
        channel_id: str,
        thread_ts: str,
    ) -> _SlackStream:
        self._prune_streams()
        native = self._streaming_mode() == STREAMING_STREAM
        return self._stream_state.new_stream(
            key, channel_id, thread_ts,
            io=self._stream_surface_io(), native=native,
            recipient=self._stream_recipient(msg) if native else ("", ""),
            logger=logger,
        )

    async def _open_session(self, stream: _SlackStream) -> bool:
        """Open the surface and hand it a session. ``False`` if it would not open."""
        debounce = (
            _STREAM_APPEND_DEBOUNCE_MS
            if isinstance(stream.surface, _SlackStreamingSurface)
            else _STREAM_DEBOUNCE_MS
        )
        session = StreamingSession(stream.surface, debounce_ms=debounce)
        try:
            await session.start(stream.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack streaming could not start: %s", exc)
            return False
        stream.session = session
        return True

    async def _close_stream(
        self, msg: Message, channel_id: str, thread_ts: str
    ) -> tuple[str, _SlackStreamingSurface | None]:
        return await self._stream_state.close_stream(
            self._stream_key(msg, channel_id, thread_ts), channel_id, logger=logger
        )

    def _remember_stream_recipient(
        self, session_id: str, user_id: str, team_id: str
    ) -> None:
        self._stream_state.remember_recipient(
            session_id, user_id, team_id, _MAX_STREAM_RECIPIENTS
        )
