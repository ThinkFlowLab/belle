"""Read the events of the Mattermost WebSocket, and resume the connection after a drop.

A dropped connection is opened again with its ``connection_id`` and the next
sequence number. The server either resumes the connection, and sends the
events the drop missed, or starts a new one. The server resumes only a
connection that it has already found dead, so a short drop usually gets a new
one. A new connection never sends the events of the gap: the reader logs the
gap with its span and hands the span to ``on_gap``.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from belle_agent.platforms.mattermost.client import EventStream, MattermostClient, MattermostError

logger = logging.getLogger(__name__)

#: The first event of every connection.
EVENT_HELLO = "hello"
#: The wait before the first new attempt after a drop, doubled after each failure up to the maximum.
RETRY_FIRST_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0

#: Called with the start and the end of a gap that a new connection did not send, as seconds since the epoch.
OnGap = Callable[[float, float], Awaitable[None]]


class NoHello(RuntimeError):
    """A new connection did not start with ``hello``, so the server did not accept it."""


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class MattermostSocket:
    """The WebSocket of one bot account, read as one stream of events across drops."""

    def __init__(
        self,
        client: MattermostClient,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
        on_gap: OnGap | None = None,
    ) -> None:
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._on_gap = on_gap
        self._stream: EventStream | None = None
        #: The first event a resumed connection sent, which ``events`` yields next.
        self._pending: Mapping[str, Any] | None = None
        self._closed = False
        #: The id the server gave the connection in ``hello``, and the last sequence number read.
        self.connection_id = ""
        self.sequence = -1
        #: When the last event arrived, as seconds since the epoch.
        self.last_event_at = 0.0

    async def connect(self) -> None:
        """Open a new connection and read its ``hello``. Raise ``MattermostError`` or ``NoHello``."""
        self._stream = await self._client.events()
        hello = await self._stream.receive()
        if not hello or hello.get("event") != EVENT_HELLO:
            raise NoHello("the Mattermost WebSocket did not start with hello")
        self._keep(hello)

    def _keep(self, event: Mapping[str, Any]) -> None:
        """Keep what an event says of the connection: its id, in ``hello``, and the sequence number."""
        self.last_event_at = self._clock()
        if event.get("event") == EVENT_HELLO:
            data = event.get("data")
            self.connection_id = str((data if isinstance(data, Mapping) else {}).get("connection_id") or "")
        if isinstance(event.get("seq"), int):
            self.sequence = event["seq"]

    async def events(self) -> AsyncIterator[Mapping[str, Any]]:
        """Yield each event after ``hello``, across drops, until ``close``."""
        while not self._closed:
            event, self._pending = self._pending, None
            if event is None and self._stream is not None:
                event = await self._stream.receive()
            if event is None:
                if self._closed:
                    return
                await self._resume()
                continue
            previous = self.connection_id
            self._keep(event)
            if event.get("event") != EVENT_HELLO:
                yield event
            elif previous and self.connection_id != previous:
                # A hello on a connection that was open says the server started it again.
                logger.warning("mattermost websocket restarted its connection: %s -> %s", previous, self.connection_id)

    async def _resume(self) -> None:
        """Open the connection again, waiting longer after each failure, and hand a gap it did not resume to ``on_gap``."""
        dropped_at = self.last_event_at or self._clock()
        logger.warning(
            "mattermost websocket closed: connection=%s sequence=%d; resuming", self.connection_id, self.sequence
        )
        if self._stream is not None:
            await self._stream.close()
        delay = RETRY_FIRST_SECONDS
        while not self._closed:
            try:
                stream = await self._client.events(self.connection_id, self.sequence + 1)
                first = await stream.receive()
            except MattermostError as exc:
                logger.warning("mattermost websocket did not reopen: %s; next attempt in %.0f s", exc, delay)
            else:
                if first is not None:
                    break
                await stream.close()
                logger.warning("mattermost websocket closed on reopening; next attempt in %.0f s", delay)
            await self._sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
        else:
            return
        self._stream = stream
        previous = self.connection_id
        if first.get("event") == EVENT_HELLO:
            self._keep(first)
        else:
            self._pending = first
        if self.connection_id != previous:
            now = self._clock()
            logger.warning(
                "mattermost websocket opened a new connection %s in place of %s: the server does not send the events"
                " from %s to %s (%.0f s)",
                self.connection_id, previous, _iso(dropped_at), _iso(now), now - dropped_at,
            )
            if self._on_gap is not None:
                try:
                    await self._on_gap(dropped_at, now)
                except Exception:
                    logger.exception("mattermost websocket gap from %s to %s was not handled", _iso(dropped_at), _iso(now))
            return
        logger.info("mattermost websocket resumed connection=%s at sequence %d", self.connection_id, self.sequence)

    async def close(self) -> None:
        """Close the connection and end ``events``."""
        self._closed = True
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.close()

