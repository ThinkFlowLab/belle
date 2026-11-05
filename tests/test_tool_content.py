"""A tool result with content that is not text: what the tool host returns, and what the tool server renders."""

import base64
import json

import httpx
import pytest

from belle_agent.backend import BackendCapabilities, ContentPart, ToolCall, ToolContent
from belle_agent.tools.host import AWAITING_CONFIRMATION, BoundTurn, ToolHost, held, result_text, succeeded
from belle_agent.tools.server import PATH, ToolServer, _blocks

PICTURE = b"\x89PNG\r\n\x1a\n\x00picture"


def test_model_content_types_default_to_text_when_the_selected_model_is_unknown():
    capabilities = BackendCapabilities(
        backend="test", tool_content=frozenset({"image/*"}),
        tool_content_by_model={"provider/other": frozenset({"application/pdf"})},
    )

    assert capabilities.content_for("") == {"image/*"}
    assert capabilities.content_for("provider/other") == {"application/pdf"}
    assert capabilities.content_for("provider/unknown") == frozenset()


class Toolkit:
    def get_tool_definitions(self):
        schema = {"type": "object", "properties": {"mime": {"type": "string"}}}
        return [{"name": name, "description": name, "input_params": schema} for name in ("fetch", "post")]

    async def fetch(self, mime: str = "image/png") -> ToolContent:
        return ToolContent(text=json.dumps({"ok": True, "mimetype": mime}), parts=(ContentPart(mime, PICTURE, "a file.png"),))

    async def post(self, mime: str = "") -> ToolContent:
        posted = {"ok": True, "chat_id": "a", "message_id": "m1"}
        return ToolContent(text=json.dumps(posted), parts=(ContentPart("image/png", PICTURE),))


def _host(*types: str) -> ToolHost:
    host = ToolHost([Toolkit()], posting_tool="post")
    host.content_types = frozenset(types)
    host.bind("a", BoundTurn("r1"))
    return host


def _call(name: str = "fetch", **arguments) -> ToolCall:
    return ToolCall(conversation="a", name=name, arguments=arguments)


async def test_content_of_a_type_the_backend_takes_is_returned_whole():
    result = await _host("image/*").call(_call())

    assert isinstance(result, ToolContent) and result.parts == (ContentPart("image/png", PICTURE, "a file.png"),)
    assert succeeded(result) and not held(result) and json.loads(result_text(result))["ok"] is True


async def test_content_of_a_type_the_backend_does_not_take_is_refused_and_says_what_it_takes():
    refused = await _host("image/*").call(_call(mime="application/pdf"))
    exact = await _host("application/pdf", "image/*").call(_call(mime="application/pdf"))

    assert isinstance(refused, str) and not succeeded(refused)
    body = json.loads(refused)
    assert body["error"] == "tool_content_not_passed"
    assert "application/pdf" in body["detail"] and "image/*" in body["detail"]
    assert json.loads(body["result_without_content"]) == {"ok": True, "mimetype": "application/pdf"}
    # No byte of the content is in the refusal.
    assert base64.b64encode(PICTURE).decode() not in refused
    # Control: the same call on a backend that takes that type returns it.
    assert isinstance(exact, ToolContent)


@pytest.mark.parametrize("mime", ["image/PNG", "image/png; name=a", " Image/Png ;charset=x"])
async def test_a_media_type_is_compared_without_its_parameters_and_in_any_case(mime):
    assert isinstance(await _host("image/png").call(_call(mime=mime)), ToolContent)


async def test_a_backend_that_declares_no_content_gets_text_and_refusals_only():
    body = json.loads(await _host().call(_call()))

    assert body["error"] == "tool_content_not_passed" and "beside text: none" in body["detail"]


async def test_content_types_follow_each_turn_even_when_conversations_overlap():
    host = _host("image/*")
    host.bind("b", BoundTurn("r2", content_types=frozenset()))
    assert isinstance(await host.call(_call()), ToolContent)
    refused = await host.call(ToolCall(conversation="b", name="fetch", arguments={}))
    assert json.loads(refused)["error"] == "tool_content_not_passed"

    host.content_types = frozenset()
    host.bind("c", BoundTurn("r3", content_types=frozenset({"image/*"})))
    accepted = await host.call(ToolCall(conversation="c", name="fetch", arguments={}))
    assert isinstance(accepted, ToolContent)


async def test_a_post_made_with_content_still_counts_as_the_turns_reply():
    host = _host("image/*")

    await host.call(_call("post"))

    assert host._turns["a"].delivered is True


def test_a_held_call_and_a_refusal_are_read_from_the_text_of_a_result_with_content():
    waiting = ToolContent(text=json.dumps({"ok": False, "error": AWAITING_CONFIRMATION}))

    assert held(waiting) and not succeeded(waiting)
    assert succeeded(ToolContent(text="plain words"))


def test_each_part_is_rendered_as_the_mcp_block_of_its_kind():
    encoded = base64.b64encode(PICTURE).decode()
    result = ToolContent(
        text="three parts",
        parts=(ContentPart("image/png", PICTURE), ContentPart("audio/wav", PICTURE), ContentPart("application/pdf", PICTURE, "a report.pdf")),
    )

    assert _blocks("words") == [{"type": "text", "text": "words"}]
    assert _blocks(result) == [
        {"type": "text", "text": "three parts"},
        {"type": "image", "data": encoded, "mimeType": "image/png"},
        {"type": "audio", "data": encoded, "mimeType": "audio/wav"},
        {"type": "resource", "resource": {"uri": "file:///a%20report.pdf", "mimeType": "application/pdf", "blob": encoded}},
    ]


async def test_the_server_answers_a_call_with_the_text_and_the_content():
    host = _host("image/*")

    async def conversation_of(meta, headers) -> str:
        return "a"

    server = ToolServer(host, conversation_of)
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fetch", "arguments": {}}}
            answer = await client.post(PATH, json=body, headers={"Authorization": f"Bearer {server.token}"})
    finally:
        await server.close()

    text, image = answer.json()["result"]["content"]
    assert json.loads(text["text"])["ok"] is True
    assert image == {"type": "image", "data": base64.b64encode(PICTURE).decode(), "mimeType": "image/png"}
