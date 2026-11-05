"""Files and tool content through the Docker Agent container, as the model receives them.

The harness's default model declares that it takes images and PDFs. Docker
Agent sends a file to that model inline, and the content of a tool result as
a user message after the tool's.
"""

import base64

import pytest
from docker_agent_kit import ATTACHED
from standin import call, said
from test_conformance_tool_content import PICTURE

from belle_agent.backend import EVENT_FAILED, TurnRequest
from belle_agent.integrations.docker_agent import adapter

pytestmark = pytest.mark.docker_agent_server

PDF = b"%PDF-1.4\n%%EOF\n"
NOTES = b"# Notes\n\nThe kite flies at dawn.\n"
#: Text, an image and a PDF reach the model. Docker Agent passes no audio of a tool result, so Belle sends none.
HELD = (
    ("notes.md", "text/markdown", NOTES),
    ("chart.png", "image/png", PICTURE),
    ("report.pdf", "application/pdf", PDF),
)


def _data(mime: str, content: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(content).decode()}"


def _file(name: str, mime: str, content: bytes) -> dict[str, str]:
    return {"name": name, "uri": _data(mime, content)}


def _first_request(model, marker: str) -> dict:
    return next(
        entry["body"] for entry in list(model.log) if any(marker in text for text in said(entry["body"], "user"))
    )


async def test_the_files_of_a_message_reach_the_model_inline_and_one_of_another_type_is_named(world, model):
    files = (*(_file(*held) for held in HELD), _file("song.ogg", "audio/ogg", b"OggS"))
    await world.send(world.direct("see these SAY=seen", files=files))

    assert [post.text for post in world.posts()] == ["seen"]
    (message,) = [m for m in _first_request(model, "see these")["messages"] if m["role"] == "user"]
    text, notes, chart, report = message["content"]
    assert text == {"type": "text", "text": f"see these SAY=seen\n\n{adapter.NOT_PASSED}: song.ogg (audio/ogg)."}
    assert notes["type"] == "text" and "The kite flies at dawn." in notes["text"]
    assert chart == {"type": "image_url", "image_url": {"url": _data("image/png", PICTURE)}}
    assert report == {"type": "file", "file": {"file_data": _data("application/pdf", PDF), "filename": "report.pdf"}}


async def test_the_content_of_a_tool_result_reaches_the_next_model_request(world, model):
    tool = world.kit.backend.capabilities().tool_name("read_file")
    for name, mime, content in HELD:
        world.platform_kit.holds(world.platform, name, mime, content)
        await world.send(world.direct(call(tool, name=name)))

        # The last request of the turn is the one after the tool's result, and ends with its content.
        *_, result, attached = model.log[-1]["body"]["messages"]
        assert result["role"] == "tool" and f'"mimetype": "{mime}"' in said({"messages": [result]}, "tool")[0]
        label, part = attached["content"]
        assert attached["role"] == "user"
        assert label["text"].startswith(f"{ATTACHED} (tool_call_id=") and label["text"].endswith("):")
        if mime == "text/markdown":
            assert "The kite flies at dawn." in part["text"]
        else:
            url = part["image_url"]["url"] if mime == "image/png" else part["file"]["file_data"]
            assert url == _data(mime, content)


async def test_files_larger_than_docker_agent_takes_in_one_request_are_refused(world, model):
    big = _file("big.pdf", "application/pdf", b"%" * (1 << 20))
    before = len(model.log)
    events = [event async for event in world.kit.backend.run_turn(
        TurnRequest("oversized", "oversized", "see this SAY=told", files=(big,))
    )]

    assert [event.kind for event in events] == [EVENT_FAILED]
    assert "backends.docker_agent.max_request_bytes (1048576)" in events[0].text
    assert "Nothing was submitted" in events[0].text
    assert len(model.log) == before
