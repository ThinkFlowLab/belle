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
        stream = self._client.stream("POST", path, json=body, headers=dict(headers), timeout=_STREAM_TIMEOUT)
        async with stream as response:
            if response.status_code >= 400:
                raise WireError("POST", path, response.status_code, _reason(await response.aread()))
            yield _events(response)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


async def _events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            event = json.loads(line[5:])
            if isinstance(event, dict):
                yield event
