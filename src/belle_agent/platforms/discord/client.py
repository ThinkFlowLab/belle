"""Call the Discord REST API (v10) and open the gateway socket with the bot's token.

Discord sets its rate limits per route bucket and reports them in ``X-RateLimit-*`` headers. The client
waits before a call on a spent bucket, waits as long as a 429 asks and tries again a few times, and then
raises. Errors hold the method, the path and what Discord answered, never the token.
"""

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from belle_agent.tools.host import active_turn_alive

logger = logging.getLogger(__name__)

API_URL = "https://discord.com/api/v10"
#: A request that takes longer is answered as ``unreachable``.
TIMEOUT_SECONDS = 30.0
#: Discord asks for this form of user agent: ``DiscordBot ($url, $version)``.
USER_AGENT = "DiscordBot (https://pypi.org/project/belle-agent/, 0.0.0)"
#: How many times one request is sent again after a 429, waiting as long as each asks, before it is refused.
RATE_LIMIT_RETRIES = 3
#: Discord restricts an IP address after 10,000 answers of 401, 403 or 429 in 10 minutes, and that harm
#: lasts. The client stops calling at half of that, so other processes on the same address keep the rest.
INVALID_REQUEST_BUDGET = 5_000
INVALID_REQUEST_WINDOW_SECONDS = 600.0
RATE_CACHE_PRUNE_SECONDS = 60.0
_INVALID_STATUSES = frozenset({401, 403, 429})

#: The code of a refusal by its HTTP status. Any other status is ``http_<status>``.
_STATUS_CODES = {
    400: "bad_request",
    401: "token_rejected",
    403: "forbidden",
    404: "not_found",
    413: "too_large",
    429: "rate_limited",
}
# The top-level ids that make one bucket of a route per resource: Discord's "major parameters".
_MAJOR = re.compile(r"\A/(?:channels/[0-9]+|guilds/[0-9]+|webhooks/[0-9]+(?:/[^/]+)?)")
_ID = re.compile(r"/[0-9]{15,}")
#: The interaction token in the paths that answer an interaction. It acts for the application for 15 minutes, so
#: no error, log line or bucket names it.
_INTERACTION_TOKEN = re.compile(r"\A(/(?:interactions|webhooks)/[0-9]+/)([^/]+)")
#: The hosts a message's attachment URLs point at. Only these are fetched, and without the bot's token:
#: the URL comes from a message, and the token must reach no other host.
CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


class DiscordError(RuntimeError):
    """A failed API call or malformed response. Its text holds no credential."""

    def __init__(
        self, code: str, method: str, path: str, status: int = 0, *, discord_code: int = 0, retry_after: float | None = None
    ) -> None:
        self.code = code
        self.method = method
        self.path = path
        self.status = status
        #: Discord's own JSON error code, such as 50013 (missing permissions), or 0.
        self.discord_code = discord_code
        #: The seconds a rate limit asked to wait, for a ``rate_limited`` refusal.
        self.retry_after = retry_after
        answer = f"{status}" if status else ("malformed response" if code == "bad_response" else "no answer")
        detail = f" (Discord code {discord_code})" if discord_code else ""
        wait = f", retry after {retry_after:.3f} s" if retry_after is not None else ""
        super().__init__(f"{code}: {method} {path} -> {answer}{detail}{wait}")


def _code(status: int) -> str:
    if status in _STATUS_CODES:
        return _STATUS_CODES[status]
    return "server_error" if status >= 500 else f"http_{status}"


def shown_path(path: str) -> str:
    """The path as errors and log lines name it: an interaction token is replaced by ``:token``."""
    return _INTERACTION_TOKEN.sub(r"\1:token", path)


def _rate_path(path: str) -> str:
    if path.startswith("/webhooks/"):
        # Discord's webhook major resource includes the token; retain only its digest in keys.
        return _INTERACTION_TOKEN.sub(
            lambda match: f"{match[1]}{hashlib.sha256(match[2].encode()).hexdigest()}", path,
        )
    return shown_path(path)


def route_key(method: str, path: str) -> str:
    """The method and major resource, with webhook tokens hashed and non-major IDs replaced."""
    path = _rate_path(path)
    major = _MAJOR.match(path)
    rest = path[major.end():] if major else path
    head = major.group(0) if major else ""
    return f"{method} {head}{_ID.sub('/:id', rest)}"


@dataclass
class _Bucket:
    remaining: int = 1
    #: When the bucket fills again, on the client's clock.
    reset_at: float = 0.0


class GatewaySocket(Protocol):
    """The gateway's websocket, as decoded payloads."""

    #: The close code the socket ended with, once it has ended, or ``None``.
    close_code: int | None

    async def receive(self) -> Mapping[str, Any] | None:
        """Return the next payload, or ``None`` once the socket is closed."""

    async def send(self, payload: Mapping[str, Any]) -> None:
        """Send one payload."""

    async def close(self, code: int) -> None:
        """Close the socket with ``code``."""


#: Opens the gateway socket at a ``wss`` URL. Tests give a fake in its place.
OpenSocket = Callable[[str], Awaitable[GatewaySocket]]


class DiscordClient:
    """The REST API as the bot."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        open_socket: OpenSocket | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        invalid_budget: int = INVALID_REQUEST_BUDGET,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=API_URL,
            headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        self._cdn = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS, transport=transport)
        self.open_socket = open_socket or _open_aiohttp
        self._clock = clock
        self._sleep = sleep
        self._invalid_budget = invalid_budget
        self._invalid: collections.deque[float] = collections.deque()
        #: The bucket id Discord named for each route, and each bucket's state by that id and major id.
        self._route_buckets: dict[str, str] = {}
        self._buckets: dict[str, _Bucket] = {}
        # Requests retain their lock while holding it or waiting; idle routes need no lock.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._prune_at = 0.0
        self._global_until = 0.0

    def _bucket_key(self, route: str, path: str) -> str:
        major = _MAJOR.match(_rate_path(path))
        return f"{self._route_buckets.get(route, route)}|{major.group(0) if major else ''}"

    def _check_invalid_budget(self, method: str, path: str) -> None:
        now = self._clock()
        while self._invalid and now - self._invalid[0] > INVALID_REQUEST_WINDOW_SECONDS:
            self._invalid.popleft()
        if len(self._invalid) >= self._invalid_budget:
            wait = INVALID_REQUEST_WINDOW_SECONDS - (now - self._invalid[0])
            logger.error(
                "discord client stopped: %d answers of 401, 403 or 429 in %.0f s; calls resume in %.0f s",
                len(self._invalid), INVALID_REQUEST_WINDOW_SECONDS, wait,
            )
            raise DiscordError("invalid_request_budget", method, path, retry_after=wait)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send one request below the API root and return its decoded JSON body, or raise ``DiscordError``."""
        self._prune_limits()
        shown = shown_path(path)
        route = route_key(method, path)
        # Callbacks have a 3-second deadline; another interaction's HTTP response cannot hold them up.
        lock = contextlib.nullcontext() if path.startswith("/interactions/") else self._locks.setdefault(route, asyncio.Lock())
        async with lock:
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                self._check_invalid_budget(method, shown)
                await self._wait_for(route, path)
                if method not in {"GET", "HEAD", "OPTIONS"} and not active_turn_alive():
                    raise DiscordError("turn_ended", method, shown)
                response = await self._send(method, path, shown, **kwargs)
                self._keep_limits(route, path, response)
                if response.status_code in _INVALID_STATUSES:
                    self._invalid.append(self._clock())
                if response.status_code != 429:
                    return self._decode(method, shown, response)
                retry_after = self._retry_after(response)
                if attempt == RATE_LIMIT_RETRIES:
                    raise DiscordError("rate_limited", method, shown, 429, retry_after=retry_after)
                logger.info("discord %s %s rate limited: waiting %.3f s", method, shown, retry_after)
                await self._sleep(retry_after)
        raise AssertionError("unreachable")  # pragma: no cover

    def _prune_limits(self) -> None:
        now = self._clock()
        if now < self._prune_at:
            return
        self._prune_at = now + RATE_CACHE_PRUNE_SECONDS
        for key, bucket in list(self._buckets.items()):
            if bucket.reset_at <= now:
                del self._buckets[key]
        for route, bucket_id in list(self._route_buckets.items()):
            # route_key already hashed token majors; do not hash them a second time.
            major = _MAJOR.match(route.partition(" ")[2])
            if f"{bucket_id}|{major.group(0) if major else ''}" not in self._buckets:
                del self._route_buckets[route]

    async def _wait_for(self, route: str, path: str) -> None:
        now = self._clock()
        bucket = self._buckets.get(self._bucket_key(route, path))
        # Discord exempts interaction callbacks and token webhooks from the bot's global limit.
        global_wait = 0.0 if _INTERACTION_TOKEN.match(path) else self._global_until - now
        wait = max(global_wait, bucket.reset_at - now if bucket and bucket.remaining <= 0 else 0.0)
        if wait > 0:
            await self._sleep(wait)

    async def _send(self, method: str, path: str, shown: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # The exception text may hold the URL; it never holds the headers. Only its type is kept.
            logger.warning("discord %s %s not answered: %s", method, shown, type(exc).__name__)
            raise DiscordError("unreachable", method, shown) from None

    def _keep_limits(self, route: str, path: str, response: httpx.Response) -> None:
        headers = response.headers
        if name := headers.get("X-RateLimit-Bucket"):
            self._route_buckets[route] = name
        try:
            remaining = int(headers["X-RateLimit-Remaining"])
            reset_after = float(headers["X-RateLimit-Reset-After"])
        except (KeyError, ValueError):
            pass
        else:
            self._buckets[self._bucket_key(route, path)] = _Bucket(remaining, self._clock() + reset_after)
        if (
            response.status_code == 429 and not _INTERACTION_TOKEN.match(path)
            and (headers.get("X-RateLimit-Global") or self._body(response).get("global"))
        ):
            self._global_until = self._clock() + self._retry_after(response)

    def _retry_after(self, response: httpx.Response) -> float:
        value = self._body(response).get("retry_after", response.headers.get("Retry-After"))
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _body(response: httpx.Response) -> Mapping[str, Any]:
        try:
            found = response.json()
        except ValueError:
            return {}
        return found if isinstance(found, Mapping) else {}

    def _decode(self, method: str, path: str, response: httpx.Response) -> Any:
        if response.is_error:
            code = self._body(response).get("code")
            raise DiscordError(
                _code(response.status_code), method, path, response.status_code,
                discord_code=code if isinstance(code, int) else 0,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise DiscordError("bad_response", method, path, response.status_code) from None

    async def mapping(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        """``request`` for a route that answers with one object; anything else is a malformed response."""
        found = await self.request(method, path, **kwargs)
        if not isinstance(found, Mapping):
            raise DiscordError("bad_response", method, path, 200)
        return found

    async def respond(self, interaction_id: str, token: str, body: Mapping[str, Any]) -> None:
        """Answer an interaction, as Discord asks within 3 seconds of it."""
        await self.request("POST", f"/interactions/{interaction_id}/{token}/callback", json=body)

    async def edit_response(self, application_id: str, token: str, body: Mapping[str, Any]) -> None:
        """Edit the message an interaction answered, or its own answer, while its token lasts (15 minutes)."""
        await self.request("PATCH", f"/webhooks/{application_id}/{token}/messages/@original", json=body)

    async def followup(self, application_id: str, token: str, body: Mapping[str, Any]) -> None:
        """Send a further message for an interaction, while its token lasts (15 minutes)."""
        await self.request("POST", f"/webhooks/{application_id}/{token}", json=body)

    async def gateway_bot(self) -> Mapping[str, Any]:
        """Where the gateway is, how many shards Discord asks for, and what is left of the identify budget."""
        return await self.mapping("GET", "/gateway/bot")

    async def download(self, url: str, max_bytes: int) -> bytes:
        """Return the body of an attachment URL on Discord's CDN, or raise ``DiscordError``.

        A URL on another host is refused, and no token is sent. A body longer than ``max_bytes`` is not read
        past that size, and raises the code ``too_large``.
        """
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or parsed.host not in CDN_HOSTS:
            raise DiscordError("not_cdn", "GET", f"{parsed.scheme}://{parsed.host}")
        path = parsed.path
        try:
            async with self._cdn.stream("GET", url) as response:
                if response.is_error:
                    raise DiscordError(_code(response.status_code), "GET", path, response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise DiscordError("too_large", "GET", path, response.status_code)
                    body.extend(chunk)
        except httpx.HTTPError as exc:
            logger.warning("discord GET %s not answered: %s", path, type(exc).__name__)
            raise DiscordError("unreachable", "GET", path) from None
        return bytes(body)

    async def close(self) -> None:
        """Close the HTTP connections."""
        await self._http.aclose()
        await self._cdn.aclose()


class _AiohttpSocket:
    """A ``GatewaySocket`` over aiohttp, which the ``discord`` extra installs."""

    def __init__(self, session: Any, socket: Any) -> None:
        self._session = session
        self._socket = socket
        self.close_code: int | None = None

    async def receive(self) -> Mapping[str, Any] | None:
        import aiohttp

        while True:
            message = await self._socket.receive()
            if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                self.close_code = self._socket.close_code
                # The gateway does not close a connection Discord closed, so its session ends here.
                await self._session.close()
                return None
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except ValueError:
                logger.warning("discord gateway sent a frame that is not JSON; skipping it")
                continue
            if isinstance(payload, Mapping):
                return payload

    async def send(self, payload: Mapping[str, Any]) -> None:
        import aiohttp

        try:
            await self._socket.send_str(json.dumps(payload))
        except (aiohttp.ClientError, OSError):
            raise DiscordError("unreachable", "SEND", "gateway") from None

    async def close(self, code: int) -> None:
        try:
            await self._socket.close(code=code)
        finally:
            await self._session.close()


async def _open_aiohttp(url: str) -> GatewaySocket:
    import aiohttp

    session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    try:
        socket = await session.ws_connect(url, autoping=True, max_msg_size=0)
    except (aiohttp.ClientError, OSError):
        await session.close()
        raise DiscordError("unreachable", "GET", "gateway") from None
    return _AiohttpSocket(session, socket)
