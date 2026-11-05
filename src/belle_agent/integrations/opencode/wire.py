"""Talk to one OpenCode server: JSON calls and the event stream."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

# The pinned server writes a comment line on the event stream every 15 s while nothing else happens.
# A stream silent for three of them is taken for lost: a dropped connection can raise no error at all.
STREAM_IDLE_SECONDS = 45.0


class WireError(Exception):
    """OpenCode refused a call."""

    def __init__(self, method: str, path: str, status: int, reason: str = "") -> None:
        super().__init__(f"{method} {path}: HTTP {status}{f' {reason}' if reason else ''}")
        self.status = status


def _reason(response: httpx.Response) -> str:
    """Read the message of an error answer. A successful body is never quoted."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("message") or "")[:200] if isinstance(body, dict) else ""


class Wire:
    """HTTP basic auth client for one OpenCode server."""

    def __init__(
        self,
        url: str,
        password: str,
        *,
        username: str = "opencode",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), auth=(username, password), timeout=30.0, transport=transport
        )
        #: Why the server could not run a turn when it was last asked for its default model.
        self.unready = "it has not been asked yet"
        #: The kinds of input the default model takes, as the server describes it: ``text``, ``image``.
        self.model_input: frozenset[str] = frozenset()

    async def call(self, method: str, path: str, body: Any = None, *, unwrap: bool = True) -> Any:
        """Make one call and usually return its answer unwrapped from ``data``."""
        response = await self._client.request(method, path, json=body)
        if response.status_code >= 400:
            raise WireError(method, path, response.status_code, _reason(response))
        if not response.content:
            return None
        answer = response.json()
        return answer["data"] if unwrap and isinstance(answer, dict) and "data" in answer else answer

    async def listing(self, path: str, *members: str) -> list[dict[str, Any]]:
        """Read a list, and keep the named members of each entry.

        The rest is dropped here. A model entry holds the provider key in its
        settings, as the answer of ``GET /api/model/default`` does, and an
        agent entry holds request headers.
        """
        answer = await self.call("GET", path)
        entries = answer if isinstance(answer, list) else ()
        return [{member: entry.get(member) for member in members} for entry in entries if isinstance(entry, dict)]

    async def _fetch_default_model(self) -> tuple[tuple[str, str], frozenset[str]] | int:
        """GET ``/api/model/default``. Auth refusal raises; transport errors propagate.

        Returns ``((provider, id), input kinds)`` when both ids are present, or
        the HTTP status when the server answers without a usable model.
        """
        response = await self._client.get("/api/model/default")
        if response.status_code in (401, 403):
            raise WireError("GET", "/api/model/default", response.status_code, "the server password was refused")
        try:
            data = response.json().get("data") if response.status_code == 200 else None
        except (ValueError, AttributeError):
            data = None
        if not isinstance(data, dict):
            return response.status_code
        provider, model_id = str(data.get("providerID") or ""), str(data.get("id") or "")
        if not provider or not model_id:
            return response.status_code
        takes = (data.get("capabilities") or {}).get("input") if isinstance(data.get("capabilities"), dict) else None
        kinds = frozenset(str(kind) for kind in takes) if isinstance(takes, list) else frozenset()
        return (provider, model_id), kinds

    async def probe_default_model(self) -> tuple[str, str] | None:
        """GET ``/api/model/default`` without updating readiness fields.

        Raises :class:`WireError` when the password is refused. Propagates
        :class:`httpx.HTTPError` when the server cannot be reached. Returns
        ``None`` when the server answers but names no model yet.
        """
        found = await self._fetch_default_model()
        return None if isinstance(found, int) else found[0]

    async def default_model(self) -> tuple[str, str] | None:
        """Name the server's default model as provider and id, or nothing while it knows none yet.

        The listener answers before the model catalog is built. The answer of
        ``GET /api/model/default`` holds the provider key in its settings, so
        it is read here for the two ids and the kinds of input the model
        takes, and then dropped: nothing else of it is returned, logged or stored. ``unready`` says why the last call
        named no model. A refused password is not a wait: it raises.
        """
        try:
            found = await self._fetch_default_model()
        except httpx.HTTPError as exc:
            self.unready = f"it cannot be reached ({type(exc).__name__}: {exc})"
            return None
        if isinstance(found, int):
            self.unready = f"it names no default model yet (HTTP {found})"
            return None
        default, kinds = found
        self.model_input = kinds
        return default

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """Open ``GET /api/event``. Events sent while no reader is connected are lost.

        A stream that sends nothing, not even a heartbeat, for ``STREAM_IDLE_SECONDS``
        raises :class:`httpx.ReadTimeout`.
        """
        timeout = httpx.Timeout(30.0, read=STREAM_IDLE_SECONDS)
        async with self._client.stream("GET", "/api/event", timeout=timeout) as response:
            if response.status_code != 200:
                raise WireError("GET", "/api/event", response.status_code)
            yield _events(response)

    async def close(self) -> None:
        await self._client.aclose()


async def _events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    lines = response.aiter_lines()
    while True:
        try:
            async with asyncio.timeout(STREAM_IDLE_SECONDS):
                line = await anext(lines)
        except StopAsyncIteration:
            return
        except TimeoutError:
            message = f"no event and no heartbeat within {STREAM_IDLE_SECONDS:g}s"
            raise httpx.ReadTimeout(message, request=response.request) from None
        if line.startswith("data:"):
            event = json.loads(line[5:])
            if isinstance(event, dict):
                yield event
