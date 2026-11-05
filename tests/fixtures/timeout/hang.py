"""A test that outlives OCB_TEST_TIMEOUT. ``tests/test_test_timeout.py`` runs it."""

import asyncio
import os
import socket
from pathlib import Path

import pytest


@pytest.fixture
async def backend():
    """Hold a port until the work under the lock stops, as a backend does. Record the close in OCB_PROBE_TEARDOWN."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    lock = asyncio.Lock()
    yield lock
    async with lock:
        server.close()
    Path(os.environ["OCB_PROBE_TEARDOWN"]).write_text("closed")


async def test_hangs(backend):
    async with backend:
        await asyncio.Event().wait()
