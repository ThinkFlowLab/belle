"""A Discord that exists only in the test process: a recorded REST API and a scripted gateway socket.

Nothing here opens a connection. The REST API answers each ``(method, path)`` from a queue a test fills,
as Discord would, headers included. The gateway socket hands the client the payloads a test puts in it,
records what the client sends, and ends with the close code a test chooses. The token is synthetic.
"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

TOKEN = "synthetic-discord-token-0123456789"
#: The bot's user id, as ``ready`` names it.
BOT_ID = "1000000000000000116"
GATEWAY_URL = "wss://gateway.invalid"
RESUME_URL = "wss://resume.invalid"


@dataclass
class Request:
    """One request the client sent."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: Any


@dataclass
class FakeRest:
    """The REST API: each ``(method, path below /api/v10)`` answered by the next of its queued answers."""

    #: Queued answers as ``(status, body, headers)``; the last one stays once the others are used.
    routes: dict[tuple[str, str], list[tuple[int, Any, dict[str, str]]]] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    unreachable: bool = False
    #: Set, a route with no queued answer is served as Discord would: channels from ``channels``, a new id for
    #: each message posted, and a thread with its root message's id. Unset, it answers ``{}``.
    serve: bool = False
    #: Channel and thread records by id, as ``GET /channels/{id}`` gives them.
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Each message posted, in order: ``(channel, body, id)``.
    posted: list[tuple[str, Any, str]] = field(default_factory=list)
    _next_id: int = 1710000000000000000

    def answer(self, method: str, path: str, *answers: tuple[Any, ...]) -> None:
        """Queue answers for a route, each ``(status, body)`` or ``(status, body, headers)``."""
        self.routes[(method, path)] = [(entry[0], entry[1], dict(entry[2]) if len(entry) > 2 else {}) for entry in answers]

    def gateway(self, *, shards: int = 1, remaining: int = 1000, reset_after: int = 0) -> None:
        """Answer ``GET /gateway/bot`` with the shards and the identify budget given."""
        self.answer("GET", "/gateway/bot", (200, {
            "url": GATEWAY_URL, "shards": shards,
            "session_start_limit": {"total": 1000, "remaining": remaining, "reset_after": reset_after, "max_concurrency": 1},
        }))

    def transport(self) -> httpx.MockTransport:
        """The httpx transport that answers from the queues."""
        return httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        path = request.url.path.removeprefix("/api/v10")
        self.requests.append(
            Request(request.method, path, dict(request.headers), json.loads(request.content) if request.content else None)
        )
        queue = self.routes.get((request.method, path)) or [self._served(request.method, path)]
        status, body, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    def _served(self, method: str, path: str) -> tuple[int, Any, dict[str, str]]:
        """What Discord would answer a route that has no queued answer, where ``serve`` is set."""
        parts = path.strip("/").split("/")
        if not self.serve:
            return 200, {}, {}
        if method == "GET" and len(parts) == 2 and parts[0] == "channels":
            found = self.channels.get(parts[1])
            return (200, found, {}) if found else (404, {"code": 10003, "message": "Unknown Channel"}, {})
        if method == "POST" and len(parts) == 3 and parts[0] == "channels" and parts[2] == "messages":
            self._next_id += 1
            body = self.requests[-1].body
            self.posted.append((parts[1], body, str(self._next_id)))
            return 200, {"id": str(self._next_id), "channel_id": parts[1]}, {}
        if method == "POST" and len(parts) == 5 and parts[2] == "messages" and parts[4] == "threads":
            thread = {"id": parts[3], "type": 11, "parent_id": parts[1], "owner_id": BOT_ID}
            self.channels[parts[3]] = thread
            return 201, thread, {}
        return 204 if method in ("PUT", "DELETE") else 200, {}, {}

    def calls(self, method: str, path: str) -> list[Request]:
        """Every request sent to one route, in order."""
        return [request for request in self.requests if request.method == method and request.path == path]


class Clock:
    """A clock that only ``sleep`` moves, so a test reads every wait the client took."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        """The time now."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Record the wait and move the clock on by it."""
        self.slept.append(round(seconds, 3))
        self.now += seconds
        await asyncio.sleep(0)


class FakeSocket:
    """One gateway connection: payloads in, payloads out, and the close code it ends with."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.close_code: int | None = None
        self.sent: list[dict[str, Any]] = []
        self.closed_with: int | None = None
        self._inbox: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()

    def feed(self, payload: Mapping[str, Any]) -> None:
        """The server sends ``payload``."""
        self._inbox.put_nowait(payload)

    def end(self, code: int) -> None:
        """The server closes the connection with ``code``."""
        self.close_code = code
        self._inbox.put_nowait(None)

    async def receive(self) -> Mapping[str, Any] | None:
        """The next payload, or ``None`` once the connection is closed."""
        return await self._inbox.get()

    async def send(self, payload: Mapping[str, Any]) -> None:
        """Record what the client sends."""
        self.sent.append(dict(payload))

    async def close(self, code: int) -> None:
        """The client closes the connection with ``code``."""
        self.closed_with = code
        if self.close_code is None:
            self.close_code = code
        self._inbox.put_nowait(None)

    def ops(self) -> list[int]:
        """The opcode of each payload the client sent, in order."""
        return [payload["op"] for payload in self.sent]


class FakeGateway:
    """Opens a new ``FakeSocket`` for each connection the client makes. ``script`` fills each as it opens."""

    def __init__(self, *scripts: Any) -> None:
        self.sockets: list[FakeSocket] = []
        self._scripts = list(scripts)
        self.refuse = 0

    async def open(self, url: str) -> FakeSocket:
        """Open the next connection, or refuse it while ``refuse`` counts down."""
        if self.refuse:
            self.refuse -= 1
            from belle_agent.platforms.discord.client import DiscordError

            raise DiscordError("unreachable", "GET", "gateway")
        socket = FakeSocket(url)
        self.sockets.append(socket)
        if self._scripts:
            self._scripts.pop(0)(socket)
        return socket


def hello(interval_ms: int = 41250) -> dict[str, Any]:
    """The first payload of every connection."""
    return {"op": 10, "d": {"heartbeat_interval": interval_ms}, "s": None, "t": None}


def ready(session: str = "session-1", *, guilds: int = 2) -> dict[str, Any]:
    """The READY dispatch of a new session, with ``guilds`` unavailable guilds."""
    return {"op": 0, "s": 1, "t": "READY", "d": {
        "v": 10, "session_id": session, "resume_gateway_url": RESUME_URL,
        "user": {"id": BOT_ID, "username": "belle", "bot": True},
        "guilds": [{"id": str(1000000000000000200 + n), "unavailable": True} for n in range(guilds)],
        "application": {"id": "1000000000000000116", "flags": 0},
    }}


def dispatch(seq: int, name: str = "MESSAGE_CREATE", **data: Any) -> dict[str, Any]:
    """An event of the gateway, with its sequence number."""
    return {"op": 0, "s": seq, "t": name, "d": data}
