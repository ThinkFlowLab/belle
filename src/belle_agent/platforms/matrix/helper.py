"""Start the Matrix helper, talk to it, and stop it.

The helper is a child process. Belle makes a socket pair, and the child gets
one end as fd 3. No socket file, port or token exists. The helper exits when
the socket closes, so a Belle that dies leaves no helper behind. Secrets go in
the ``start`` request, never in the argument list or the child's environment.
"""

import asyncio
import collections
import contextlib
import itertools
import logging
import os
import signal
import socket
from collections.abc import Callable
from typing import Any

from belle_agent.platforms.matrix import protocol

logger = logging.getLogger(__name__)

#: The fd number the helper reads its socket from.
HELPER_FD = 3
#: How long ``close`` waits after ``shutdown``, then after SIGTERM, before the next step.
STOP_SECONDS = 10.0
#: The lines of the helper's standard error kept for the log of its exit.
STDERR_KEPT = 20


class HelperError(Exception):
    """A request the helper refused or could not do. ``code`` is a homeserver ``errcode`` or a ``helper_`` code."""

    def __init__(self, code: str, message: str, *, reason: str = "", retry_after_ms: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.reason = reason
        self.retry_after_ms = retry_after_ms


class PartialPostError(HelperError):
    """A failed post with the event IDs already sent, in order."""

    def __init__(self, error: HelperError, event_ids: list[str]) -> None:
        super().__init__(error.code, error.message, reason=error.reason, retry_after_ms=error.retry_after_ms)
        self.event_ids = list(event_ids)


def _as_helper_fd(fd: int) -> Callable[[], None]:
    """Make ``fd`` the child's fd 3, open across exec. Every fd that Python opened closes at exec."""

    def move() -> None:
        if fd == HELPER_FD:
            os.set_inheritable(fd, True)
        else:
            os.dup2(fd, HELPER_FD)

    return move


class Helper:
    """One helper process, and the requests and events on its socket."""

    def __init__(
        self,
        path: str,
        *,
        max_line_bytes: int,
        on_event: Callable[[dict[str, Any]], None],
        stop_seconds: float = STOP_SECONDS,
    ) -> None:
        self._path = path
        self._max_line = max_line_bytes
        self._on_event = on_event
        self._stop_seconds = stop_seconds
        self._ids = itertools.count(1)
        #: Each request that waits for its answer: its op, and the future of its result.
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_KEPT)
        self.hello: dict[str, Any] = {}
        #: Set once the process has ended, with its exit code.
        self.exited: asyncio.Future[int] | None = None

    async def open(self) -> dict[str, Any]:
        """Start the process and read its ``hello``. Refuse a protocol other than this one, naming both."""
        parent, child = socket.socketpair()
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._path,
                "--max-line-bytes",
                str(self._max_line),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                close_fds=False,
                preexec_fn=_as_helper_fd(child.fileno()),
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        reader, self._writer = await asyncio.open_unix_connection(sock=parent, limit=self._max_line)
        loop = asyncio.get_running_loop()
        self.exited = loop.create_future()
        hello: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._tasks = [
            asyncio.create_task(self._read(reader, hello)),
            asyncio.create_task(self._relay_stderr()),
            asyncio.create_task(self._watch_exit()),
        ]
        exited: asyncio.Future[Any] = self.exited
        await asyncio.wait({hello, exited}, return_when=asyncio.FIRST_COMPLETED)
        if not hello.done():
            hello.cancel()
            raise HelperError(protocol.RESTARTED, f"the helper exited with code {self.exited.result()} before its hello")
        self.hello = hello.result()
        if self.hello.get("protocol") != protocol.PROTOCOL:
            await self.close()
            raise HelperError(
                protocol.BAD_REQUEST,
                f"the helper speaks protocol {self.hello.get('protocol')!r} and Belle speaks {protocol.PROTOCOL}:"
                " the image mixes two builds",
            )
        return self.hello

    async def request(self, op: str, **fields: Any) -> dict[str, Any]:
        """Send one request and return its result. Raise ``HelperError`` with the helper's error."""
        if self._writer is None or self.exited is None or self.exited.done():
            raise HelperError(protocol.RESTARTED, "the helper is not running")
        message = {"id": next(self._ids), "op": op, **fields}
        try:
            protocol.check_request(message)
        except protocol.ProtocolError as exc:
            raise HelperError(protocol.BAD_REQUEST, str(exc)) from exc
        line = protocol.encode(message)
        if len(line) > self._max_line:
            raise HelperError(
                protocol.BAD_REQUEST, f"{op}: the request is {len(line)} bytes, above the line limit of {self._max_line}"
            )
        answer: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = (op, answer)
        try:
            self._writer.write(line)
            await self._writer.drain()
            return await answer
        finally:
            self._pending.pop(message["id"], None)

    async def _read(self, reader: asyncio.StreamReader, hello: asyncio.Future[dict[str, Any]]) -> None:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                logger.error("matrix helper sent a line above the limit of %d bytes; stopping it", self._max_line)
                self._kill()
                return
            if not line:
                return
            try:
                message = protocol.decode(line)
                if not hello.done():
                    if message.get("event") != "hello":
                        raise protocol.ProtocolError("the first line from the helper is not hello")
                    hello.set_result(message)
                    continue
                protocol.check_from_helper(message, {i: op for i, (op, _) in self._pending.items()})
            except protocol.ProtocolError as exc:
                logger.error("matrix helper broke the protocol: %s; stopping it", exc)
                self._kill()
                return
            if "event" in message:
                self._on_event(message)
                continue
            pending = self._pending.get(message["id"])
            if pending is None or pending[1].done():
                logger.warning("matrix helper answered request %s, which nothing waits for", message["id"])
            elif message["ok"]:
                pending[1].set_result(message["result"])
            else:
                error = message["error"]
                pending[1].set_exception(
                    HelperError(
                        error["code"], error["message"], reason=error.get("reason", ""), retry_after_ms=error.get("retry_after_ms")
                    )
                )

    async def _relay_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        async for raw in self._process.stderr:
            text = raw.decode(errors="replace").rstrip()
            self._stderr.append(text)
            logger.info("matrix helper: %s", text)

    async def _watch_exit(self) -> None:
        assert self._process is not None and self.exited is not None
        code = await self._process.wait()
        # The last standard error lines arrive before the pipe closes.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._tasks[1]), 1)
        for op, answer in list(self._pending.values()):
            if not answer.done():
                answer.set_exception(HelperError(protocol.RESTARTED, f"the helper exited with code {code} before it answered {op}"))
        if not self.exited.done():
            self.exited.set_result(code)
        level = logging.INFO if code == 0 else logging.WARNING
        logger.log(level, "matrix helper exited with code %s; its last lines:\n%s", code, "\n".join(self._stderr) or "(none)")

    def _kill(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()

    async def close(self) -> int | None:
        """Ask the helper to stop, then SIGTERM, then SIGKILL, each after ``stop_seconds``. Return its exit code."""
        process, exited = self._process, self.exited
        if process is None or exited is None:
            return None
        if not exited.done():
            with contextlib.suppress(HelperError, asyncio.TimeoutError, ConnectionError):
                await asyncio.wait_for(self.request("shutdown"), self._stop_seconds)
        for step in (None, signal.SIGTERM, signal.SIGKILL):
            if step is not None and process.returncode is None:
                logger.warning("matrix helper did not stop; sending %s", step.name)
                process.send_signal(step)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(exited), self._stop_seconds)
                break
        if self._writer is not None:
            self._writer.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        return exited.result() if exited.done() else None

