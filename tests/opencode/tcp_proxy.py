"""A TCP relay a check can cut, to lose the adapter's connections to OpenCode without restarting anything."""

import asyncio
import contextlib
import time
import urllib.parse


class Proxy:
    def __init__(self, target: str) -> None:
        parsed = urllib.parse.urlsplit(target)
        self._target = (parsed.hostname, parsed.port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._refuse_until = 0.0
        self.url = ""

    async def start(self) -> "Proxy":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def close(self) -> None:
        self.drop()
        self._server.close()

    def drop(self, seconds: float = 0) -> None:
        """Cut every connection, and accept none for ``seconds``."""
        self._refuse_until = time.monotonic() + seconds
        for writer in list(self._writers):
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if time.monotonic() < self._refuse_until:
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        except OSError:
            writer.close()
            return
        self._writers |= {writer, upstream_writer}
        try:
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            self._writers -= {writer, upstream_writer}


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    writer.close()
