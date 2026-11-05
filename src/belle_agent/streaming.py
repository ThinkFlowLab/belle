"""Platform-neutral reply snapshot writing and final-tail matching."""

import asyncio
from typing import Any

_STREAM_ANCHOR_MIN_CHARS = 8


def streamed_remainder(sent: str, root: str) -> str | None:
    """Return the final reply tail, or None when the streamed prefix changed."""
    if not sent:
        return root
    if root.startswith(sent):
        return root[len(sent) :]
    trimmed = sent.rstrip()
    if root.startswith(trimmed):
        return root[len(trimmed) :]
    if trimmed.startswith(root):
        return ""
    needle = root[:_STREAM_ANCHOR_MIN_CHARS]
    if needle:
        for text in (sent, trimmed):
            start = text.find(needle)
            while start != -1:
                if root.startswith(text[start:]):
                    return root[len(text) - start :]
                start = text.find(needle, start + 1)
    return None


class SnapshotWriter:
    """Write the newest text of a reply to its surface, one write at a time.

    A surface spaces its own writes. Text that arrives while one waits or is
    in flight is written by the next, so a reply that is written fast costs
    one call for each interval and not one for each delta.
    """

    def __init__(self, surface: Any, handle: str) -> None:
        self._surface = surface
        self._handle = handle
        self._text = ""
        self._changed = asyncio.Event()
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="reply-stream")

    def replace(self, text: str) -> None:
        self._text = text
        self._changed.set()

    async def _run(self) -> None:
        while True:
            await self._changed.wait()
            self._changed.clear()
            if self._stopping:
                return
            await self._surface.write(self._handle, self._text, 0)

    def cancel(self) -> None:
        self._task.cancel()

    async def stop(self) -> str:
        """Stop writing and hand the handle back. A write in flight ends first, so none lands after the reply is ended."""
        self._stopping = True
        self._changed.set()
        await asyncio.gather(self._task, return_exceptions=True)
        return self._handle
