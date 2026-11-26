"""Talk to one ``docker-agent serve api``: JSON calls and the event stream of a turn."""

import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

# A turn's stream stays silent while a tool or a person is awaited.
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=None)


class WireError(Exception):
    """Docker Agent refused a call."""

    def __init__(self, method: str, path: str, status: int, reason: str = "") -> None:
        super().__init__(f"{method} {path}: HTTP {status}{f' {reason}' if reason else ''}")
        self.status = status


def _reason(body: bytes) -> str:
    """Read the message of an error answer. A successful body is never quoted."""
    try:
        answer = json.loads(body)
    except ValueError:
        return ""
    return str(answer.get("message") or "")[:200] if isinstance(answer, dict) else ""


class Wire:
    """A client of one Docker Agent server, with its bearer token."""

    def __init__(self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            transport=transport,
        )

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        """Make one call and return its decoded answer."""
        response = await self._client.request(method, path, json=body)
        if response.status_code >= 400:
            raise WireError(method, path, response.status_code, _reason(response.content))
        return response.json() if response.content else None

    @contextlib.asynccontextmanager
    async def turn(
        self, session: str, agent: str, body: Any, headers: Mapping[str, str]
    ) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """Run a turn and yield its events. Leaving the block closes the stream, which stops the turn."""
        path = f"/sessions/{session}/agent/{agent}"
        async with self._stream("POST", path, json=body, headers=dict(headers)) as events:
            yield (event async for _, event in events)

    @contextlib.asynccontextmanager
    async def session_events(
        self, session: str, since: int
    ) -> AsyncIterator[AsyncIterator[tuple[int, dict[str, Any]]]]:
        """Tail the session's event log after the event numbered ``since``, each event with its number. A control
        event, such as ``gap``, has number 0. A session that has logged nothing yet has no log: that raises a
        :class:`WireError` with status 404."""
        async with self._stream("GET", f"/sessions/{session}/events", params={"since": since}) as events:
            yield events

    @contextlib.asynccontextmanager
    async def _stream(
        self, method: str, path: str, **request: Any
    ) -> AsyncIterator[AsyncIterator[tuple[int, dict[str, Any]]]]:
        async with self._client.stream(method, path, timeout=_STREAM_TIMEOUT, **request) as response:
            if response.status_code >= 400:
                raise WireError(method, path, response.status_code, _reason(await response.aread()))
            yield _events(response)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


async def _events(response: httpx.Response) -> AsyncIterator[tuple[int, dict[str, Any]]]:
    """The events of a stream, each with the number its ``id`` field gives, or 0."""
    number = 0
    async for line in response.aiter_lines():
        if line.startswith("id:"):
            number = int(line[3:])
        elif line.startswith("data:"):
            event = json.loads(line[5:])
            if isinstance(event, dict):
                yield number, event
            number = 0
