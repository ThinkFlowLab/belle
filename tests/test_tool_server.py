"""The tool server serves a tool host over MCP and binds each call by what the model cannot write."""

import asyncio
import base64
import builtins
import json

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from belle_agent.scopes import LEVEL_ASK, LEVEL_DENY
from belle_agent.tools.host import BoundTurn, ToolHost, active_call, request_metadata
from belle_agent.tools.server import MAX_REQUEST_BYTES, PATH, ToolServer

SESSION_KEY = "harness/session"
LEGACY = "2025-11-25"
MODERN = "2026-07-28"


class Toolkit:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.call_keys: list[tuple[str, str]] = []

    def get_tool_definitions(self):
        schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
        return [
            {"name": name, "description": f"{name} something", "input_params": schema}
            for name in ("post", "erase", "publish")
        ]

    async def post(self, text: str, conversation: str | None = None) -> str:
        self.posts.append((text, request_metadata()))
        if current := active_call():
            self.call_keys.append(current[0])
        return json.dumps({"ok": True})

    erase = publish = post


class World:
    def __init__(self, server: ToolServer, toolkit: Toolkit, client: httpx.AsyncClient) -> None:
        self.server, self.toolkit, self.client = server, toolkit, client
        self._ids = iter(range(1, 100))

    async def rpc(
        self, method: str, params: dict | None = None, *, token: str | None = None, headers: dict | None = None
    ) -> httpx.Response:
        token = self.server.token if token is None else token
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Mcp-Protocol-Version": LEGACY,
            **(headers or {}),
        }
        return await self.client.post(PATH, json=body, headers=headers)

    async def call(self, name: str, session: str, **arguments) -> dict:
        params = {"name": name, "arguments": arguments, "_meta": {SESSION_KEY: session}}
        response = await self.rpc("tools/call", params)
        return json.loads(response.json()["result"]["content"][0]["text"])


@pytest.fixture
async def world():
    toolkit = Toolkit()
    tools = ToolHost([toolkit], posting_tool="post")
    tools.bind("conversation-a", BoundTurn("r1", metadata={"origin": "a"}, tools={"erase": LEVEL_DENY}))
    tools.bind("conversation-b", BoundTurn("r2", metadata={"origin": "b"}, tools={"publish": LEVEL_ASK}))
    sessions = {"ses_a": "conversation-a", "ses_b": "conversation-b", "ses_idle": "conversation-idle"}

    async def conversation_of(meta, headers) -> str:
        return sessions.get(meta.get(SESSION_KEY) or headers.get("x-harness-session"), "")

    server = ToolServer(tools, conversation_of)
    await server.start()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
        yield World(server, toolkit, client)
    await server.close()


async def test_a_request_without_the_token_is_refused_whatever_it_asks(world):
    call = {"name": "post", "arguments": {"text": "forged"}, "_meta": {SESSION_KEY: "ses_a"}}

    for token in ("guess", world.server.token[:-1]):
        for method, params in (("initialize", {}), ("tools/list", {}), ("tools/call", call)):
            assert (await world.rpc(method, params, token=token)).status_code == 401
    assert (await world.client.post(PATH, json={"method": "tools/call", "params": call})).status_code == 401
    assert world.toolkit.posts == []

    # Control: the same call with the token runs.
    assert (await world.call("post", "ses_a", text="mine"))["ok"] is True
    assert world.toolkit.posts == [("mine", {"origin": "a"})]


async def test_legacy_initialize_and_modern_discovery(world):
    params = {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    response = await world.rpc("initialize", params)
    assert response.json()["result"]["protocolVersion"] == LEGACY

    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    }
    response = await world.rpc(
        "server/discover", {"_meta": meta}, headers={"Mcp-Protocol-Version": MODERN, "Mcp-Method": "server/discover"}
    )
    assert response.json()["result"]["supportedVersions"] == [MODERN]

    unknown = {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    response = await world.rpc("initialize", unknown)
    assert response.json()["result"]["protocolVersion"] == LEGACY


async def test_a_request_from_a_web_page_is_refused_even_with_the_token(world):
    call = {"name": "post", "arguments": {"text": "rebound"}, "_meta": {SESSION_KEY: "ses_a"}}
    page = {"Origin": "http://attacker.example"}

    for method, params in (("initialize", {}), ("tools/list", {}), ("tools/call", call)):
        assert (await world.rpc(method, params, headers=page)).status_code == 403
    headers = {"Authorization": f"Bearer {world.server.token}", **page}
    assert (await world.client.get(PATH, headers=headers)).status_code == 403
    assert (await world.client.delete(PATH, headers=headers)).status_code == 403
    assert world.toolkit.posts == []


async def test_a_request_for_an_event_stream_is_refused_and_holds_nothing_open(world):
    headers = {"Authorization": f"Bearer {world.server.token}", "Accept": "text/event-stream"}

    async with asyncio.timeout(5):
        response = await world.client.get(PATH, headers=headers)
    assert (response.status_code, response.headers["allow"]) == (405, "POST")
    assert response.json() == {"error": "this tool server offers no event stream: POST each request"}
    # Control: the token is checked first, and a POST is answered.
    assert (await world.client.get(PATH, headers={"Accept": "text/event-stream"})).status_code == 401
    assert (await world.call("post", "ses_a", text="after"))["ok"] is True


async def test_empty_origin_and_wrong_host_are_refused_before_dispatch(world):
    call = {"name": "post", "arguments": {"text": "rebound"}, "_meta": {SESSION_KEY: "ses_a"}}
    assert (await world.rpc("tools/call", call, headers={"Origin": ""})).status_code == 403
    assert (await world.rpc("tools/call", call, headers={"Host": "unrelated.example"})).status_code == 421
    assert world.toolkit.posts == []


@pytest.mark.parametrize(("host", "advertised", "expected"), [
    ("127.0.0.1", "", "http://127.0.0.1:{port}/mcp"),
    ("::1", "", "http://[::1]:{port}/mcp"),
    ("0.0.0.0", "http://belle.test:18300/mcp", "http://belle.test:18300/mcp"),
])
async def test_the_url_is_where_clients_reach_the_server(host, advertised, expected):
    async def conversation_of(meta, headers) -> str:
        return ""

    server = ToolServer(ToolHost([]), conversation_of, host=host, advertised_url=advertised)
    await server.start()
    try:
        assert server.url == expected.format(port=server.port)
    finally:
        await server.close()


async def test_a_wildcard_listener_requires_an_advertised_client_host():
    async def conversation_of(meta, headers) -> str:
        return ""

    missing = ToolServer(ToolHost([]), conversation_of, host="0.0.0.0")
    with pytest.raises(ValueError, match="tools.url"):
        await missing.start()

    server = ToolServer(ToolHost([]), conversation_of, host="0.0.0.0", advertised_url="http://belle.test/mcp")
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            params = {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
            headers = {"Authorization": f"Bearer {server.token}", "Accept": "application/json, text/event-stream"}
            accepted = await client.post(PATH, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params},
                                         headers={**headers, "Host": "belle.test"})
            refused = await client.post(PATH, json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": params},
                                        headers={**headers, "Host": "unrelated.test"})
        assert accepted.status_code == 200
        assert refused.status_code == 421
    finally:
        await server.close()


@pytest.mark.parametrize("url", [
    "http://belle.test/other", "http://belle.test/mcp?session=1",
    "http://user@belle.test/mcp", "http://belle.test/mcp#fragment",
])
async def test_advertised_url_must_name_the_served_endpoint(url):
    async def conversation_of(meta, headers) -> str:
        return ""

    server = ToolServer(ToolHost([]), conversation_of, host="0.0.0.0", advertised_url=url)
    with pytest.raises(ValueError, match="tools.url"):
        await server.start()
    assert server._socket is None


def test_ipv6_bind_host_uses_brackets_in_allowed_host():
    server = ToolServer(ToolHost([]), None, host="2001:db8::1", port=18300)
    assert server._hosts(18300) == ["[2001:db8::1]:18300"]


async def test_modern_revision_lists_and_calls_with_its_envelope(world):
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        SESSION_KEY: "ses_a",
    }
    listed = await world.rpc(
        "tools/list", {"_meta": meta}, headers={"Mcp-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}
    )
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["post", "erase", "publish"]
    called = await world.rpc(
        "tools/call", {"name": "post", "arguments": {"text": "modern"}, "_meta": meta},
        headers={"Mcp-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": "post"},
    )
    assert json.loads(called.json()["result"]["content"][0]["text"])["ok"] is True
    assert world.toolkit.posts == [("modern", {"origin": "a"})]


async def test_the_official_mcp_client_lists_and_calls_tools(world):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {world.server.token}"}) as http,
        streamable_http_client(f"http://127.0.0.1:{world.server.port}{PATH}", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool("post", {"text": "sdk client"}, meta={SESSION_KEY: "ses_a"})

    assert [tool.name for tool in listed.tools] == ["post", "erase", "publish"]
    assert json.loads(result.content[0].text)["ok"] is True
    assert world.toolkit.posts == [("sdk client", {"origin": "a"})]


async def test_each_server_makes_its_own_token(world):
    other = ToolServer(ToolHost([]), None)
    assert other.token != world.server.token and len(world.server.token) >= 32


async def test_start_names_the_optional_extra_when_the_sdk_is_missing(monkeypatch):
    original = builtins.__import__

    def missing_sdk(name, *args, **kwargs):
        if name == "mcp_types":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_sdk)
    async def conversation_of(meta, headers):
        return ""

    with pytest.raises(RuntimeError, match=r"belle-agent\[mcp\]"):
        await ToolServer(ToolHost([]), conversation_of).start()


async def test_a_server_given_a_token_admits_that_token_only():
    server = ToolServer(ToolHost([]), None, token="operator-set")
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            for token, status in (("operator-set", 200), ("operator-se", 401), ("operator-set2", 401)):
                response = await client.post(PATH, json=body, headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": LEGACY})
                assert response.status_code == status
    finally:
        await server.close()


async def test_it_answers_the_three_requests_a_harness_makes(world):
    params = {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    hello = (await world.rpc("initialize", params)).json()["result"]
    assert hello["protocolVersion"] == LEGACY and "tools" in hello["capabilities"]

    listed = (await world.rpc("tools/list")).json()["result"]["tools"]
    # One list for everyone: a tool some conversation's scope denies is still listed, and refused when called.
    assert [tool["name"] for tool in listed] == ["post", "erase", "publish"]
    assert listed[0]["inputSchema"]["required"] == ["text"]
    assert listed[0]["description"] == "post something"

    assert (await world.rpc("no/such")).json()["error"]["code"] == -32601
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    headers = {"Authorization": f"Bearer {world.server.token}"}
    headers.update({"Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": LEGACY})
    assert (await world.client.post(PATH, json=notification, headers=headers)).status_code == 202
    assert (await world.client.get(PATH, headers=headers)).status_code == 405


async def test_a_call_is_bound_to_the_conversation_the_harness_names(world):
    # The arguments name another conversation. They decide nothing.
    await world.call("post", "ses_a", text="from a", conversation="conversation-b")
    await world.call("post", "ses_b", text="from b")

    assert world.toolkit.posts == [("from a", {"origin": "a"}), ("from b", {"origin": "b"})]


async def test_a_request_header_the_harness_sets_can_name_the_conversation(world):
    call = {"name": "post", "arguments": {"text": "by header"}}
    await world.rpc("tools/call", call, headers={"X-Harness-Session": "ses_b"})

    assert world.toolkit.posts == [("by header", {"origin": "b"})]


async def test_each_mcp_invocation_has_its_own_call_identity(world):
    for _ in range(2):
        assert (await world.call("post", "ses_a", text="same arguments"))["ok"] is True

    first, second = world.toolkit.call_keys
    assert first[0] == second[0] == "r1"
    assert first[1] and second[1] and first[1] != second[1]
    assert active_call() is None


async def test_a_call_with_no_conversation_or_no_running_turn_runs_nothing(world):
    for session in ("ses_unknown", "ses_idle", ""):
        assert (await world.call("post", session, text="not mine"))["error"] == "tool_denied"
    response = await world.rpc("tools/call", {"name": "post", "arguments": {"text": "no meta"}})
    assert json.loads(response.json()["result"]["content"][0]["text"])["error"] == "tool_denied"
    assert world.toolkit.posts == []

    # Control: a session whose conversation has a running turn posts.
    assert (await world.call("post", "ses_a", text="mine"))["ok"] is True


async def test_the_turns_scope_is_enforced_on_a_tool_called_by_name(world):
    denied = await world.rpc("tools/call", {
        "name": "erase", "arguments": {"text": "x"}, "_meta": {SESSION_KEY: "ses_a"},
    })
    assert json.loads(denied.json()["result"]["content"][0]["text"])["error"] == "tool_denied"
    assert denied.json()["result"]["isError"] is True
    # A tool that waits for a person is refused at once: nobody is awaited inside a call.
    assert (await world.call("publish", "ses_b", text="x"))["error"] == "approval_required"
    assert world.toolkit.posts == []

    # Control: the same tools where the scope leaves them alone.
    assert (await world.call("erase", "ses_b", text="x"))["ok"] is True
    assert (await world.call("publish", "ses_a", text="x"))["ok"] is True


async def test_a_call_waiting_for_approval_is_not_an_mcp_error():
    class AskingToolkit(Toolkit):
        async def widening_question(self, name, arguments):
            return "Post this elsewhere?"

    async def confirm(call, turn, question):
        return True

    async def conversation_of(meta, headers):
        return "conversation-a"

    toolkit = AskingToolkit()
    tools = ToolHost([toolkit], confirm=confirm)
    tools.bind("conversation-a", BoundTurn("r1"))
    server = ToolServer(tools, conversation_of)
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            response = await client.post(PATH, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "post", "arguments": {"text": "elsewhere"}},
            }, headers={"Authorization": f"Bearer {server.token}", "Accept": "application/json, text/event-stream"})
        result = response.json()["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["error"] == "awaiting_confirmation"
        assert toolkit.posts == []
    finally:
        await server.close()


async def test_a_malformed_call_is_an_error_and_runs_nothing(world):
    assert (await world.rpc("tools/call", {"name": "post", "arguments": "text"})).json()["error"]["code"] == -32602
    assert (await world.call("post", "ses_a", wrong="x"))["error"] == "invalid_arguments"
    assert (await world.call("nothing", "ses_a"))["error"] == "unknown_tool"
    assert world.toolkit.posts == []


async def test_a_full_size_inline_file_fits_in_a_tool_request(world):
    content = base64.b64encode(bytes(30 * 1024 * 1024)).decode("ascii")

    assert (await world.call("post", "ses_a", text=content))["ok"] is True
    assert world.toolkit.posts[0][0] == content


async def test_a_request_larger_than_the_documented_limit_is_refused(world):
    response = await world.client.post(
        PATH,
        content=b"x" * (MAX_REQUEST_BYTES + 1),
        headers={"Authorization": f"Bearer {world.server.token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 413
    assert world.toolkit.posts == []
