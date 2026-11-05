"""A tool result that carries content beside its text. Every backend and every platform must pass.

A platform's tool can return a file's bytes with its media type. The backend
says which types it hands to the model. The tool host returns a result whose
content the backend takes, and refuses one it does not take, so that a model
is never told a file was read when its content went nowhere. What a backend
then sends its model is checked with that backend.
"""

import json
import struct
import zlib

import pytest
from conformance_kit import ModelTurn

from belle_agent.backend import EVENT_TOOL_RESULT

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


#: One red pixel, as a PNG a decoder accepts. A backend may decode an image before it passes it on.
PICTURE = (
    b"\x89PNG\r\n\x1a\n"
    + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + _chunk(b"IDAT", zlib.compress(b"\x00\xd0\x10\x10"))
    + _chunk(b"IEND", b"")
)


@pytest.fixture
async def showing(build_world, platform_kit):
    """A world whose platform keeps what each tool call ended with."""
    return await build_world(getattr(platform_kit, "shows", "") + ANSWER_EVERYTHING)


async def _read(world, name: str, mime: str) -> dict:
    """Run one turn that reads a held file with the platform's tool, and return the result the turn reported."""
    tool, arguments = world.platform_kit.holds(world.platform, name, mime, PICTURE)
    world.kit.play(ModelTurn(final="done", calls=((tool, arguments),)))
    message = world.message(f"read {name}", conversation="C1")
    await world.send(message)
    (result,) = [
        event.text for message_id, event in world.platform.progressed
        if message_id == message.message_id and event.kind == EVENT_TOOL_RESULT
    ]
    return json.loads(result)


async def test_content_the_backend_hands_to_its_model_is_returned(showing):
    result = await _read(showing, "chart.png", "image/png")

    assert result["ok"] is True and result["mimetype"] == "image/png"


async def test_content_the_backend_does_not_hand_to_its_model_is_refused_with_the_types_it_takes(showing):
    result = await _read(showing, "blob.bin", "application/x-belle-unknown")

    assert (result["ok"], result["error"]) == (False, "tool_content_not_passed")
    assert "application/x-belle-unknown" in result["detail"] and "image/" in result["detail"]
    # What the tool said about the file is kept, so the model can still say what the file is.
    assert json.loads(result["result_without_content"])["mimetype"] == "application/x-belle-unknown"
