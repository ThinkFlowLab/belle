"""Serve JSON and form hook events at ``/hook/<name>`` over HTTP.

A configured token requires bearer authentication. An empty token allows any
caller. The listener checks request size and parses events. The adapter supplies
JSON answers. Callback failures return HTTP 500 so the harness fails the hook.
The endpoint keeps the last refusal for each watched session, so a failed turn can name its own hook error.
"""

import asyncio
import hmac
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable, Coroutine, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "/hook/"
#: The content type of an HTML form, whose fields become the event.
FORM = "application/x-www-form-urlencoded"
#: The endpoint holds a whole event in memory, so the limit bounds what one caller can make it allocate.
#: A harness plugin forwards file contents as their length only, so a turn's events stay well under it.
MAX_REQUEST_BYTES = 8 * 1024 * 1024

#: Answer a hook from its name, event, and headers with lowercase names. Runs in a server thread.
Answer = Callable[[str, Mapping[str, Any], Mapping[str, str]], dict[str, Any]]


class _Server(ThreadingHTTPServer):
    owner: "HookServer"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status: int, error: str) -> None:
        # What a caller without the token sent, its path included, is kept for nobody to read.
        if status != 401:
            self.server.owner.record_refusal(self.headers.get("X-Belle-Session"), f"HTTP {status} for {self.path}: {error}")
        self._send(status, {"error": error})

    def do_POST(self) -> None:
        owner = self.server.owner
        expected = f"Bearer {owner.token}"
        if owner.token and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            return self._refuse(401, "this hook endpoint needs the token it was started with")
        if not self.path.startswith(owner.prefix):
            return self._refuse(404, "not a hook")
        length = self.headers.get("Content-Length") or "0"
        if not length.isdigit():
            return self._refuse(400, f"Content-Length must be a whole number of bytes, not {length!r}")
        if int(length) > MAX_REQUEST_BYTES:
            return self._refuse(413, f"the event is {length} bytes; a hook event may hold at most {MAX_REQUEST_BYTES}")
        body = self.rfile.read(int(length))
        try:
            if self.headers.get_content_type() == FORM:
                event: Any = dict(urllib.parse.parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=True))
            else:
                event = json.loads(body)
        except ValueError:
            return self._refuse(400, "not JSON, and not a form")
        hook = self.path[len(owner.prefix) :]
        try:
            headers = {name.lower(): value for name, value in self.headers.items()}
            answer = owner.answer(hook, event if isinstance(event, Mapping) else {}, headers)
        except Exception:  # A hook that cannot be answered fails closed: the harness fails it on a non-200.
            logger.exception("hook endpoint could not answer hook %s", hook)
            return self._refuse(500, "hook endpoint failed")
        self._send(200, answer)


class HookServer:
    """A hook endpoint. An empty ``token`` lets any caller in. A hook's name is the path after ``prefix``."""

    def __init__(
        self, answer: Answer, *, token: str = "", host: str = "127.0.0.1", port: int = 0, prefix: str = PREFIX
    ) -> None:
        self.answer = answer
        self.token = token
        #: Active sessions to their last hook refusal, or None.
        self.refused: dict[str, tuple[float, str] | None] = {}
        #: The last refusal of a caller that held the token, with or without a session: when, and why.
        self.last_refusal: tuple[float, str] | None = None
        self._refusal_lock = threading.Lock()
        self.prefix = prefix
        self._address = (host, port)
        self._server: _Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def watch_session(self, session: str) -> None:
        with self._refusal_lock:
            self.refused[session] = None

    def record_refusal(self, session: str | None, error: str) -> None:
        with self._refusal_lock:
            self.last_refusal = (time.monotonic(), error)
            if session in self.refused:
                self.refused[session] = self.last_refusal

    def session_refusal(self, session: str) -> tuple[float, str] | None:
        with self._refusal_lock:
            return self.refused.get(session)

    def refusal_since(self, began: float) -> str:
        """Why the endpoint last refused a caller that held the token, if it did at ``began`` or later."""
        with self._refusal_lock:
            refused = self.last_refusal
        return refused[1] if refused is not None and refused[0] >= began else ""

    def unwatch_session(self, session: str) -> None:
        with self._refusal_lock:
            self.refused.pop(session, None)

    @property
    def port(self) -> int:
        """The port it listens on, once started."""
        assert self._server is not None, "the hook endpoint is not started"
        return self._server.server_address[1]

    async def start(self) -> None:
        """Listen. ``run`` uses the loop that called this."""
        self._loop = asyncio.get_running_loop()
        self._server = _Server(self._address, _Handler)
        self._server.daemon_threads = True
        self._server.owner = self
        threading.Thread(target=self._server.serve_forever, args=(0.05,), name="belle-hooks", daemon=True).start()

    async def close(self) -> None:
        """Stop listening. A second call does nothing."""
        if self._server is not None:
            await asyncio.to_thread(self._server.shutdown)
            self._server.server_close()
            self._server = None

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run a coroutine on the loop that started this endpoint, from a server thread, and return its result."""
        assert self._loop is not None, "the hook endpoint is not started"
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()
