"""Slack preview and native streaming message surfaces."""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack.delivery import CHANNEL_LOG_TAG, response_ts, slack_failure_reason
from belle_agent.platforms.slack.retry import retry_after_seconds
from belle_agent.platforms.slack.text import preferred_split_index
from belle_agent.streaming import streamed_remainder

_MAX_SLACK_APPEND_TEXT_LENGTH = 12000
_STREAM_MIN_UPDATE_INTERVAL_SECONDS = 1.0
_STREAM_APPEND_MIN_INTERVAL_SECONDS = 0.6
# Producers such as cron pushes and skill reports can request rendering in text.
_SLACK_THREAD_DETAILS_MARKER = "<!-- belle:slack-thread-details -->"
_SLACK_BLOCKS_MARKER = "<!-- belle:slack-blocks -->"
_SLACK_BLOCKS_OFF_MARKER = "<!-- belle:slack-blocks:off -->"
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_STREAM_BLOCK_REJECTION_ERRORS = frozenset(
    {"invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long", "internal_error"}
)
_STREAM_STOPPED_BY_USER = "stopped_by_user"
_STREAM_UNUSABLE_ERRORS = frozenset(
    {_STREAM_STOPPED_BY_USER, "message_not_in_streaming_state", "message_not_owned_by_app"}
)
_STREAM_RECIPIENT_ERRORS = frozenset({"missing_recipient_user_id", "missing_recipient_team_id"})
_STREAM_PAYLOAD_ERRORS = frozenset({"streaming_mode_mismatch"})


def extract_block_request(content: str) -> tuple[str, bool | None]:
    """Remove Block Kit markers; an off marker overrides an on marker."""
    requested: bool | None = None
    cleaned = content
    if _SLACK_BLOCKS_OFF_MARKER in cleaned:
        requested = False
        cleaned = cleaned.replace(_SLACK_BLOCKS_OFF_MARKER, "")
    if _SLACK_BLOCKS_MARKER in cleaned:
        if requested is None:
            requested = True
        cleaned = cleaned.replace(_SLACK_BLOCKS_MARKER, "")
    if requested is None:
        return content, None
    return _BLANK_RUN_RE.sub("\n\n", cleaned).strip(), requested


def split_threaded_report(content: str) -> list[str]:
    """Split a reply at thread markers and discard empty sections."""
    if _SLACK_THREAD_DETAILS_MARKER not in content:
        return [content]
    pieces = [piece.strip() for piece in content.split(_SLACK_THREAD_DETAILS_MARKER)]
    pieces = [piece for piece in pieces if piece]
    if len(pieces) < 2:
        return [content.replace(_SLACK_THREAD_DETAILS_MARKER, "").strip()]
    return pieces


@dataclass(frozen=True)
class SlackSurfaceIO:
    """Connector calls a message surface needs."""

    post_text: Callable[..., Awaitable[tuple[bool, str, str]]]
    call_stream_method: Callable[..., Awaitable[Any]]
    wait_for_slot: Callable[[str, float], Awaitable[None]]
    render_snapshot: Callable[[str], str]
    logger: logging.Logger
    #: How the lines of a surface are tagged. A caller that is not the JiuwenSwarm channel passes its own.
    tag: str = CHANNEL_LOG_TAG


class SlackStreamError(RuntimeError):
    """The opening message could not be created."""


class _SlackMessageSurface:
    """Post one Slack message, then edit it in place as the reply arrives.

    A terminal edit error latches so later deltas do not keep retrying. The
    connector uses the error and counters when it closes the turn.
    """

    def __init__(self, io: SlackSurfaceIO, channel_id: str, thread_ts: str) -> None:
        self._io = io
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self.failed = False
        self.error = ""
        self.opened_ts = ""
        self.edits = 0
        self.edit_failures = 0

    def ready_to_open(self, text: str) -> bool:
        """An edit preview may open on the first delta."""
        del text
        return True

    def render(self, text: str) -> str:
        """Render the full snapshot for chat.update."""
        return self._io.render_snapshot(text)

    async def open(self, text: str) -> str:
        sent, message_ts, error = await self._io.post_text(
            channel_id=self._channel_id,
            text=self.render(text),
            thread_ts=self._thread_ts,
        )
        if not sent or not message_ts:
            raise SlackStreamError(error or "chat.postMessage returned no message ts")
        self.opened_ts = message_ts
        self._io.logger.info(
            "%s streaming opened: channel=%s ts=%s preview_chars=%d",
            self._io.tag,
            self._channel_id,
            message_ts,
            len(text),
        )
        return message_ts

    async def write(self, handle: str, text: str, sequence: int) -> None:
        del sequence  # Slack orders edits by arrival; there is no sequence field.
        await self._io.wait_for_slot(self._channel_id, _STREAM_MIN_UPDATE_INTERVAL_SECONDS)
        self.edits += 1
        sent, _, error = await self._io.post_text(
            channel_id=self._channel_id,
            text=self.render(text),
            thread_ts="",
            update_ts=handle,
        )
        if not sent:
            self.failed = True
            self.edit_failures += 1
            self.error = error or "chat.update failed without an error"
            self._io.logger.warning("Slack streaming update abandoned: %s", error)

    async def close(self, handle: str, text: str, sequence: int) -> None:
        # send() makes the final edit and owns its delivery error.
        del handle, text, sequence


class _SlackStreamingSurface:
    """Manage a streamed Slack message and its fallback state."""

    def __init__(
        self,
        io: SlackSurfaceIO,
        channel_id: str,
        thread_ts: str,
        *,
        recipient_user_id: str = "",
        recipient_team_id: str = "",
    ) -> None:
        self._io = io
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._recipient_user_id = recipient_user_id
        self._recipient_team_id = recipient_team_id
        self.failed = False
        self.error = ""
        self.opened_ts = ""
        self.edits = 0
        self.edit_failures = 0
        self.stopped_by_user = False
        # Advance only after a successful append; close uses this to find the tail.
        self.sent_text = ""

    def ready_to_open(self, text: str) -> bool:
        """Open only after a complete line; empty startStream is undocumented."""
        return bool(self._sendable(text))

    def _sendable(self, text: str) -> str:
        """Return complete Markdown lines before a thread marker, fence, or table."""
        stable = text.lstrip()
        cut = stable.rfind("\n")
        stable = stable[: cut + 1] if cut >= 0 else ""
        if not stable:
            return ""
        marker_at = stable.find(_SLACK_THREAD_DETAILS_MARKER)
        if marker_at >= 0:
            stable = stable[:marker_at]
        stable = self._strip_block_markers(stable)
        return stable[: slack_blocks.streamable_prose_prefix(stable)]

    def _strip_block_markers(self, text: str) -> str:
        if _SLACK_BLOCKS_MARKER not in text and _SLACK_BLOCKS_OFF_MARKER not in text:
            return text
        kept: list[str] = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            if _SLACK_BLOCKS_MARKER not in body and _SLACK_BLOCKS_OFF_MARKER not in body:
                kept.append(line)
                continue
            cleaned = body.replace(_SLACK_BLOCKS_MARKER, "").replace(_SLACK_BLOCKS_OFF_MARKER, "")
            if cleaned.strip():
                kept.append(cleaned + line[len(body) :])
        return "".join(kept)

    async def open(self, text: str) -> str:
        """Open in chunks mode; Slack rejects later chunks after a text-mode start."""
        opening = self._sendable(text)
        response = await self._io.call_stream_method(
            "chat_startStream",
            channel=self._channel_id,
            thread_ts=self._thread_ts,
            chunks=[{"type": "markdown_text", "text": opening}],
            recipient_user_id=self._recipient_user_id or None,
            recipient_team_id=self._recipient_team_id or None,
        )
        message_ts = response_ts(response)
        if not message_ts:
            raise SlackStreamError("chat.startStream returned no message ts")
        self.opened_ts = message_ts
        self.sent_text = opening
        self._io.logger.info(
            "%s streaming opened: channel=%s ts=%s mode=stream recipient=%s opening_chars=%d",
            self._io.tag,
            self._channel_id,
            message_ts,
            self._recipient_user_id or "-",
            len(opening),
        )
        return message_ts

    async def write(self, handle: str, text: str, sequence: int) -> None:
        del sequence  # Appends land in the order they are made.
        await self.append(handle, self._sendable(text))

    async def append(self, handle: str, sendable: str) -> None:
        """Append new text; failed calls leave sent_text unchanged for close."""
        if self.failed:
            return
        if not sendable.startswith(self.sent_text):
            # A changed prefix cannot be appended without corrupting the reply.
            self._latch("the streamed prefix stopped matching what was sent")
            return
        pending = sendable[len(self.sent_text) :]
        if not pending:
            return
        for piece in self._split_appends(pending):
            await self._io.wait_for_slot(self._channel_id, _STREAM_APPEND_MIN_INTERVAL_SECONDS)
            self.edits += 1
            try:
                await self._io.call_stream_method(
                    "chat_appendStream",
                    channel=self._channel_id,
                    ts=handle,
                    chunks=[{"type": "markdown_text", "text": piece}],
                )
            except Exception as exc:  # noqa: BLE001
                self.edit_failures += 1
                self._latch(slack_failure_reason(exc))
                return
            self.sent_text += piece

    async def close(self, handle: str, text: str, sequence: int) -> None:
        """Stop the stream when its session closes outside send()."""
        del sequence
        chunks = self._markdown_chunks(text) if text else []
        await self.stop_stream(handle, chunks=chunks)

    async def stop_stream(
        self,
        handle: str,
        *,
        chunks: list[dict[str, Any]] | None = None,
        plain_chunks: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Stop streaming, retrying plain chunks if Slack rejects rendered blocks.

        Return whether stop succeeded. The caller decides final delivery.
        """
        payload: dict[str, Any] = {"channel": self._channel_id, "ts": handle}
        if chunks:
            payload["chunks"] = chunks
        try:
            await self._io.call_stream_method("chat_stopStream", **payload)
        except Exception as exc:  # noqa: BLE001
            reason = slack_failure_reason(exc)
            rendered = any(chunk.get("type") == "blocks" for chunk in chunks or [])
            if rendered and any(code in reason for code in _STREAM_BLOCK_REJECTION_ERRORS):
                self._io.logger.warning(
                    "Slack rejected the Block Kit rendering at the close (%s); finishing this stream as plain text",
                    reason,
                )
                return await self.stop_stream(handle, chunks=plain_chunks)
            self._latch(reason)
            return False
        for chunk in chunks or []:
            if chunk.get("type") == "markdown_text":
                self.sent_text += str(chunk.get("text") or "")
        return True

    def tail_chunks(
        self, remainder: str,
        render_blocks: Callable[[str], list[dict[str, Any]] | None],
    ) -> list[dict[str, Any]]:
        """Render the reply tail as blocks or Markdown chunks."""
        if not remainder.strip():
            return []
        blocks = render_blocks(remainder)
        if blocks is not None:
            return [{"type": "blocks", "blocks": blocks}]
        return self._markdown_chunks(remainder)

    async def stop_with_tail(
        self, handle: str, remainder: str,
        render_blocks: Callable[[str], list[dict[str, Any]] | None],
    ) -> bool:
        """Close a failed surface without entrusting more text to it."""
        chunks = self.tail_chunks(remainder, render_blocks)
        if self.failed:
            await self.stop_stream(handle)
            return False
        return await self.stop_stream(
            handle, chunks=chunks, plain_chunks=self._markdown_chunks(remainder),
        )

    async def finish_reply(
        self, handle: str, content: str,
        render_blocks: Callable[[str], list[dict[str, Any]] | None],
        recover: Callable[[str], Awaitable[bool]],
    ) -> bool:
        """Close the native stream, asking its host to recover a failed close."""
        root = "\n\n".join(
            piece.strip() for piece in split_threaded_report(content) if piece.strip()
        )
        remainder = streamed_remainder(self.sent_text, root)
        if remainder is None:
            self._io.logger.warning(
                "%s the finished reply does not continue what was "
                "streamed: channel=%s ts=%s streamed_chars=%d final_chars=%d",
                self._io.tag,
                self._channel_id, handle or "-", len(self.sent_text), len(root),
            )
            await self.stop_stream(handle)
            return False

        stopped = await self.stop_with_tail(handle, remainder, render_blocks)
        if not stopped:
            stopped = await recover(remainder)
        self._io.logger.info(
            "%s streaming finished: channel=%s ts=%s appends=%d "
            "failed=%d tail_chars=%d one_message=%s",
            self._io.tag,
            self._channel_id, handle or "-", self.edits, self.edit_failures,
            len(remainder), "yes" if stopped else "no",
        )
        return True

    async def rewrite_message(
        self, handle: str, whole: str, blocks: list[dict[str, Any]], kind: str,
    ) -> tuple[bool, str]:
        """Replace an expired stream with a complete Slack message."""
        sent, _, error = await self._io.post_text(
            channel_id=self._channel_id, text=whole, thread_ts="",
            update_ts=handle, blocks=blocks, block_kind=kind,
        )
        if sent:
            self._io.logger.info(
                "%s the expired stream was rewritten whole: "
                "channel=%s ts=%s chars=%d",
                self._io.tag,
                self._channel_id, handle, len(whole),
            )
        return sent, error

    def _latch(self, error: str) -> None:
        self.failed = True
        self.error = error
        if _STREAM_STOPPED_BY_USER in error:
            self.stopped_by_user = True
            self._io.logger.info(
                "%s streaming stopped by the reader: channel=%s ts=%s",
                self._io.tag,
                self._channel_id,
                self.opened_ts or "-",
            )
            return
        if any(code in error for code in _STREAM_RECIPIENT_ERRORS):
            self._io.logger.error(
                "%s streaming refused for want of a recipient: channel=%s ts=%s user=%s team=%s error=%s",
                self._io.tag,
                self._channel_id,
                self.opened_ts or "-",
                self._recipient_user_id or "-",
                self._recipient_team_id or "-",
                error,
            )
            return
        if any(code in error for code in _STREAM_UNUSABLE_ERRORS):
            self._io.logger.warning(
                "Slack streaming will take no more (%s); what is left of the reply is posted separately",
                error,
            )
            return
        self._io.logger.warning("Slack streaming append abandoned: %s", error)

    def resume(self) -> str:
        """Clear a recoverable failure; a payload rejection cannot be retried.

        Slack can report streaming_state_conflict for a live stream while also
        rejecting its payload with streaming_mode_mismatch.
        """
        if not self.failed:
            return ""
        if any(code in self.error for code in _STREAM_PAYLOAD_ERRORS):
            return ""
        cleared, self.error, self.failed = self.error, "", False
        return cleared

    @staticmethod
    def _split_appends(text: str) -> list[str]:
        """Split long appends on preferred text boundaries."""
        if len(text) <= _MAX_SLACK_APPEND_TEXT_LENGTH:
            return [text] if text else []
        pieces: list[str] = []
        remaining = text
        while len(remaining) > _MAX_SLACK_APPEND_TEXT_LENGTH:
            split_at = preferred_split_index(remaining, _MAX_SLACK_APPEND_TEXT_LENGTH)
            pieces.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            pieces.append(remaining)
        return pieces

    @classmethod
    def _markdown_chunks(cls, text: str) -> list[dict[str, Any]]:
        return [{"type": "markdown_text", "text": piece} for piece in cls._split_appends(text)]


@dataclass
class _SlackStream:
    """One reply being rendered into one Slack message."""

    surface: _SlackMessageSurface | _SlackStreamingSurface
    channel_id: str = ""
    thread_ts: str = ""
    session: Any | None = None
    text: str = ""
    # Opening failed; surface.failed covers failures after opening.
    failed: bool = False
    touched_at: float = field(default_factory=time.monotonic)


class SlackStreamState:
    """Own the maps and lock shared by one connector's active streams."""

    def __init__(self) -> None:
        self.streams: dict[tuple[str, str, str], _SlackStream] = {}
        self.next_update_at: dict[str, float] = {}
        self.recipients: dict[str, tuple[str, str]] = {}
        self.lock = asyncio.Lock()

    def clear(self) -> None:
        self.streams.clear()
        self.next_update_at.clear()
        self.recipients.clear()

    async def wait_for_slot(self, channel_id: str, interval: float) -> None:
        while True:
            now = time.monotonic()
            ready_at = self.next_update_at.get(channel_id, 0.0)
            if now >= ready_at:
                self.next_update_at[channel_id] = now + interval
                return
            await asyncio.sleep(ready_at - now)

    def remember_recipient(self, session_id: str, user_id: str, team_id: str, cap: int) -> None:
        if not session_id or not user_id:
            return
        if len(self.recipients) >= cap:
            for stale in list(self.recipients)[: len(self.recipients) - cap + 1]:
                del self.recipients[stale]
        self.recipients[session_id] = (user_id, team_id)

    def recipient(self, session_id: str, metadata: dict[str, Any]) -> tuple[str, str]:
        remembered = self.recipients.get(session_id)
        if remembered is not None:
            return remembered
        user_id = str(metadata.get("slack_user_id") or "").strip()
        team_id = str(metadata.get("slack_team_id") or "").strip()
        if user_id and team_id:
            return user_id, team_id
        if session_id.startswith("slack_"):
            parts = session_id.split("_", 4)
            if len(parts) >= 3:
                candidate = parts[1].strip()
                if not team_id and candidate != "default":
                    team_id = candidate
            if len(parts) >= 4:
                target = parts[3].strip()
                if not user_id and target and "." not in target:
                    user_id = target
        return user_id, team_id

    def new_stream(
        self,
        key: tuple[str, str, str],
        channel_id: str,
        thread_ts: str,
        *,
        io: SlackSurfaceIO,
        native: bool,
        recipient: tuple[str, str],
        logger: logging.Logger,
        tag: str = CHANNEL_LOG_TAG,
    ) -> _SlackStream:
        """Choose a native stream or an editable preview for one reply."""
        surface: _SlackMessageSurface | _SlackStreamingSurface | None = None
        if native:
            user_id, team_id = recipient
            refusal = cannot_stream(channel_id, thread_ts, user_id, team_id)
            if refusal:
                logger.info(
                    "%s streaming unavailable for this reply (%s); "
                    "showing it with edits instead: channel=%s thread=%s",
                    tag,
                    refusal,
                    channel_id,
                    thread_ts or "-",
                )
            else:
                surface = _SlackStreamingSurface(
                    io, channel_id, thread_ts,
                    recipient_user_id=user_id,
                    recipient_team_id=team_id,
                )
        if surface is None:
            surface = _SlackMessageSurface(io, channel_id, thread_ts)
        stream = _SlackStream(surface=surface, channel_id=channel_id, thread_ts=thread_ts)
        self.streams[key] = stream
        return stream

    async def close_stream(
        self, key: tuple[str, str, str], channel_id: str, *, logger: logging.Logger, tag: str = CHANNEL_LOG_TAG
    ) -> tuple[str, _SlackStreamingSurface | None]:
        """Stop one preview and return its handle and native surface, if any."""
        stream = self.streams.pop(key, None)
        if stream is None or stream.session is None or stream.failed:
            return "", None
        try:
            handle = await stream.session.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack streaming could not be closed: %s", exc)
            return "", None
        surface = stream.surface
        if surface.failed:
            logger.warning(
                "%s streaming abandoned, %s: "
                "channel=%s ts=%s edits=%d failed=%d reason=%s",
                tag,
                "the rest of the reply is posted separately"
                if isinstance(surface, _SlackStreamingSurface)
                else "reply posts fresh",
                channel_id,
                surface.opened_ts or "-",
                surface.edits,
                surface.edit_failures,
                surface.error or "-",
            )
            if isinstance(surface, _SlackStreamingSurface):
                return handle, surface
            return "", None
        logger.info(
            "%s streaming closed: channel=%s ts=%s edits=%d failed=%d",
            tag,
            channel_id,
            surface.opened_ts or handle or "-",
            surface.edits,
            surface.edit_failures,
        )
        return handle, surface if isinstance(surface, _SlackStreamingSurface) else None


def cannot_stream(channel_id: str, thread_ts: str, user_id: str, team_id: str) -> str:
    """Return why this destination cannot use Slack's native streaming API."""
    if not thread_ts:
        return "the reply is not in a thread and a stream has to be one"
    if not channel_id.startswith("D") and not (user_id and team_id):
        return "a channel stream needs both recipient ids and one is unknown"
    return ""


async def call_stream_method(
    client: Any,
    method: str,
    *,
    logger: logging.Logger,
    retry_limit: int,
    **kwargs: Any,
) -> Any:
    """Call a Slack streaming method, retrying rate limits."""
    payload = {key: value for key, value in kwargs.items() if value is not None}
    call = getattr(client, method)
    retries = 0
    while True:
        try:
            return await call(**payload)
        except Exception as exc:
            delay = retry_after_seconds(exc)
            if delay is None or retries >= retry_limit:
                raise
            retries += 1
            logger.warning(
                "Slack rate limited on %s; retrying in %.1fs (%d/%d)",
                method,
                delay,
                retries,
                retry_limit,
            )
            await asyncio.sleep(delay)
