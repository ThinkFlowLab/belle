"""Call Mattermost REST API v4 and open its WebSocket with the bot's token.

Errors contain the method, path, and available response metadata, never credentials.
"""

import json
import logging
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

API = "/api/v4"
#: A request that takes longer is answered as ``unreachable``.
TIMEOUT_SECONDS = 30.0
#: How long an upload waits for ``100 Continue`` or an answer before it sends the body anyway.
CONTINUE_WAIT_SECONDS = 1.0

#: The code of a refusal by its HTTP status. Any other status is ``http_<status>``.
_STATUS_CODES = {
    400: "bad_request",
    401: "token_rejected",
    403: "forbidden",
    404: "not_found",
    413: "too_large",
    429: "rate_limited",
}


class MattermostError(RuntimeError):
    """A failed API call or malformed response. Its text holds no credential."""

    def __init__(self, code: str, method: str, path: str, status: int = 0, server_id: str = "") -> None:
        self.code = code
        self.method = method
        self.path = path
        self.status = status
        #: The server's own error id, such as ``api.context.session_expired.app_error``, or empty.
        self.server_id = server_id
        answer = f"{status}" if status else ("malformed response" if code == "bad_response" else "no answer")
        super().__init__(f"{code}: {method} {path} -> {answer}{f' ({server_id})' if server_id else ''}")


def created_post_id(answer: Any, path: str = "/posts") -> str:
    """Return the id of a created post, or reject a malformed success response."""
    post_id = answer.get("id") if isinstance(answer, Mapping) else None
    if not isinstance(post_id, str) or not post_id.strip():
        raise MattermostError("bad_response", "POST", path)
    return post_id


def _code(status: int) -> str:
    if status in _STATUS_CODES:
        return _STATUS_CODES[status]
    return "server_error" if status >= 500 else f"http_{status}"


class EventStream(Protocol):
    """The WebSocket of one connection, as decoded events."""

    async def receive(self) -> Mapping[str, Any] | None:
        """Return the next event, or ``None`` once the connection is closed."""

    async def close(self) -> None:
        """Close the connection."""


#: Opens the WebSocket at a ``ws`` or ``wss`` URL with a token. Tests give a feeder in its place.
OpenEvents = Callable[[str, str], Awaitable[EventStream]]

#: Sends one ``POST`` with a body to a URL, with query parameters and a token, and returns the
#: status and the body of the answer. ``OSError`` or ``TimeoutError`` where nothing answered.
SendBody = Callable[[str, Mapping[str, str], str, bytes], Awaitable[tuple[int, bytes]]]


class MattermostClient:
    """The REST API of one server, as the bot account."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        open_events: OpenEvents | None = None,
        send_body: SendBody | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=f"{self.url}{API}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        self._open_events = open_events or _open_aiohttp
        self._send_body = send_body or _send_aiohttp

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send one request below ``/api/v4`` and return its decoded JSON body, or raise ``MattermostError``."""
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # The exception text may hold the URL; it never holds the headers. Only its type is kept.
            logger.warning("mattermost %s %s not answered: %s", method, path, type(exc).__name__)
            raise MattermostError("unreachable", method, path) from None
        if response.is_error:
            try:
                server_id = str(response.json().get("id") or "")
            except (ValueError, AttributeError):
                server_id = ""
            raise MattermostError(_code(response.status_code), method, path, response.status_code, server_id)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise MattermostError("bad_response", method, path, response.status_code) from None

    async def download(self, path: str, max_bytes: int) -> bytes:
        """Return the body of a ``GET`` below ``/api/v4``, or raise ``MattermostError``.

        A body longer than ``max_bytes`` is not read past that size, and raises
        the code ``too_large``.
        """
        try:
            async with self._http.stream("GET", path) as response:
                if response.is_error:
                    await response.aread()
                    try:
                        server_id = str(response.json().get("id") or "")
                    except (ValueError, AttributeError):
                        server_id = ""
                    raise MattermostError(_code(response.status_code), "GET", path, response.status_code, server_id)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise MattermostError("too_large", "GET", path, response.status_code)
                    body.extend(chunk)
        except httpx.HTTPError as exc:
            logger.warning("mattermost GET %s not answered: %s", path, type(exc).__name__)
            raise MattermostError("unreachable", "GET", path) from None
        return bytes(body)

    async def upload(self, channel_id: str, filename: str, data: bytes) -> Mapping[str, Any]:
        """Upload one file into a channel and return its record, or raise ``MattermostError``.

        The server can refuse a file before it reads the body, and then closes
        the connection. The request therefore asks the server to accept the
        body first (``Expect: 100-continue``), so that such a refusal is read
        as an answer and not lost as a broken connection. A server need not
        answer that request, so the body follows after ``CONTINUE_WAIT_SECONDS``
        without an answer.
        """
        path = "/files"
        try:
            status, body = await self._send_body(
                f"{self.url}{API}{path}", {"channel_id": channel_id, "filename": filename}, self._token, data
            )
        except (OSError, TimeoutError) as exc:
            logger.warning("mattermost POST %s not answered: %s", path, type(exc).__name__)
            raise MattermostError("unreachable", "POST", path) from None
        try:
            found = json.loads(body) if body else None
        except ValueError:
            found = None
        if status >= 400:
            server_id = str(found.get("id") or "") if isinstance(found, Mapping) else ""
            raise MattermostError(_code(status), "POST", path, status, server_id)
        infos = found.get("file_infos") if isinstance(found, Mapping) else None
        if not isinstance(infos, list) or not infos or not isinstance(infos[0], Mapping):
            raise MattermostError("bad_response", "POST", path, status)
        return infos[0]

    async def me(self) -> Mapping[str, Any]:
        """Return the bot account's own user record."""
        found = await self.request("GET", "/users/me")
        if not isinstance(found, Mapping):
            raise MattermostError("bad_response", "GET", "/users/me", 200)
        return found

    async def events(self, connection_id: str = "", sequence_number: int = 0) -> EventStream:
        """Open the WebSocket of the API, or resume the connection ``connection_id`` from ``sequence_number``.

        The token goes in the ``Authorization`` header of the upgrade request.
        """
        scheme, rest = self.url.split("://", 1)
        url = f"{'wss' if scheme == 'https' else 'ws'}://{rest}{API}/websocket"
        if connection_id:
            url += "?" + urllib.parse.urlencode({"connection_id": connection_id, "sequence_number": sequence_number})
        return await self._open_events(url, self._token)

    async def close(self) -> None:
        """Close the HTTP connections."""
        await self._http.aclose()


class _AiohttpEvents:
    """An ``EventStream`` over aiohttp, which the ``mattermost`` extra installs."""

    def __init__(self, session: Any, socket: Any) -> None:
        self._session = session
        self._socket = socket

    async def receive(self) -> Mapping[str, Any] | None:
        import aiohttp

        while True:
            message = await self._socket.receive()
            if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return None
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(message.data)
            except ValueError:
                logger.warning("mattermost websocket sent a frame that is not JSON; skipping it")
                continue
            if isinstance(event, Mapping):
                return event

    async def close(self) -> None:
        await self._socket.close()
        await self._session.close()


async def _open_aiohttp(url: str, token: str) -> EventStream:
    import aiohttp

    session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"})
    try:
        socket = await session.ws_connect(url, heartbeat=30.0)
    except aiohttp.ClientResponseError as exc:
        await session.close()
        raise MattermostError(_code(exc.status), "GET", f"{API}/websocket", exc.status) from None
    except (aiohttp.ClientError, OSError):
        await session.close()
        raise MattermostError("unreachable", "GET", f"{API}/websocket") from None
    return _AiohttpEvents(session, socket)


def _stop_waiting(waiter: Any) -> None:
    if not waiter.done():
        waiter.set_result(True)


async def _send_aiohttp(url: str, params: Mapping[str, str], token: str, data: bytes) -> tuple[int, bytes]:
    import aiohttp

    class ContinueAfterWait(aiohttp.ClientRequest):
        # aiohttp waits for 100 Continue until the total timeout. Like curl, which sends the body
        # after 1 s (--expect100-timeout), stop waiting sooner: RFC 9110 lets a client do so, and a
        # server may read the body before it answers. Mattermost does that for a body of up to 10 KiB.
        def update_expect_continue(self, expect: bool = False) -> None:
            super().update_expect_continue(expect)
            if self._continue is not None:
                self.loop.call_later(CONTINUE_WAIT_SECONDS, _stop_waiting, self._continue)

    try:
        async with (
            aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
                request_class=ContinueAfterWait,
            ) as session,
            session.post(url, params=dict(params), data=data, expect100=True) as response,
        ):
            return response.status, await response.read()
    except aiohttp.ClientError as exc:
        raise OSError(type(exc).__name__) from None
