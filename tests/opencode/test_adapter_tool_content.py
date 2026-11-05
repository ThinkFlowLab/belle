"""A tool result with an image, through OpenCode to the model endpoint. The model is the stand-in.

The stand-in cannot see. What this holds is what OpenCode sends it: the
image, whole, with its type. `test_provider_tool_content.py` asks a real
model what it sees.
"""

import base64
import json

import pytest
from drive import tool_result
from probe_tool_content import png
from standin import call, said

from belle_agent.host import OUTCOME_POSTED_FINAL

pytestmark = pytest.mark.adapter

READ = "belle_read_file"


def _images(request: dict) -> list[str]:
    """Each image one provider request holds, as the URL it is given by."""
    return [
        part["image_url"]["url"]
        for message in request.get("messages", [])
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        if part.get("type") == "image_url"
    ]


def _last_request(model, text: str) -> dict:
    session = next(entry["session"] for entry in reversed(model.log) if said(entry["body"], "user")[:1] == [text])
    return model.requests(session)[-1]


async def test_an_image_a_tool_returns_is_sent_to_the_model_whole(world, model):
    picture = png()
    world.platform.files["seven.png"] = ("image/png", picture)
    text = "look " + call(READ, name="seven.png")

    # The stand-in describes its model as OpenCode's default does: it takes text and images.
    assert world.host.tools.content_types == {"image/png", "image/jpeg", "image/gif", "image/webp"}
    await world.send(world.direct(text))

    request = _last_request(model, text)
    # The tool's own message is its text. The image follows in a message of its own, as OpenCode places it.
    (result,) = said(request, "tool")
    assert json.loads(result) == {"ok": True, "name": "seven.png", "mimetype": "image/png", "size_bytes": len(picture)}
    assert _images(request) == ["data:image/png;base64," + base64.b64encode(picture).decode()]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_an_image_over_six_mib_in_a_message_reaches_the_model(world, model):
    """OpenCode before 2.2.0 failed such a prompt with HTTP 500, as its base64 check gave up at about 5.9 MiB."""
    picture = png(noise=7 * 1024 * 1024)
    uri = "data:image/png;base64," + base64.b64encode(picture).decode()

    await world.send(world.direct("see SAY=seen", files=({"name": "large.png", "uri": uri},)))

    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    # OpenCode scales the image down to its own image limit before the model reads it.
    (image,) = next(images for entry in reversed(model.log) if (images := _images(entry["body"])))
    assert image.startswith("data:image/") and len(image) < len(uri)


async def test_content_the_model_does_not_take_never_leaves_belle(world, model):
    world.platform.files["report.pdf"] = ("application/pdf", b"%PDF-1.4 not for this model")
    text = "look " + call(READ, name="report.pdf")

    await world.send(world.direct(text))

    request = _last_request(model, text)
    (result,) = said(request, "tool")
    assert tool_result(result)["error"] == "tool_content_not_passed"
    assert _images(request) == [] and "JVBERi0" not in json.dumps(request)


async def test_an_image_type_opencode_would_not_pass_inline_is_refused(world, model):
    # OpenCode would save it on its server and give the model the path instead.
    world.platform.files["seven.bmp"] = ("image/bmp", b"BM not passed")
    text = "look " + call(READ, name="seven.bmp")

    await world.send(world.direct(text))

    (result,) = said(_last_request(model, text), "tool")
    refused = tool_result(result)
    assert refused["error"] == "tool_content_not_passed"
    assert "image/bmp" in refused["detail"] and "image/png" in refused["detail"]
