"""Serve Belle tools over MCP Streamable HTTP."""

import asyncio
import base64
import contextlib
import hmac
import secrets
import socket
from collections.abc import Awaitable, Callable, Mapping
from importlib.metadata import version
from typing import Any
from urllib.parse import quote

from belle_agent.backend import ContentPart, ToolCall, ToolContent, ToolHost
from belle_agent.config import TOOLS_PATH, tools_authority
from belle_agent.tools.host import held, succeeded

PATH = TOOLS_PATH
MAX_REQUEST_BYTES = 42 * 1024 * 1024
ConversationOf = Callable[[Mapping[str, Any], Mapping[str, str]], Awaitable[str]]


def _block(part: ContentPart) -> dict[str, Any]:
    data = base64.b64encode(part.data).decode("ascii")
    family = part.mime.partition("/")[0]
    if family in ("image", "audio"):
        return {"type": family, "data": data, "mimeType": part.mime}
    return {"type": "resource", "resource": {"uri": f"file:///{quote(part.name or 'content')}", "mimeType": part.mime, "blob": data}}


def _blocks(result: str | ToolContent) -> list[dict[str, Any]]:
    if isinstance(result, ToolContent):
        return [{"type": "text", "text": result.text}, *map(_block, result.parts)]
    return [{"type": "text", "text": result}]


async def _refuse(send: Any, status: int, message: bytes, *headers: tuple[bytes, bytes]) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(message)).encode()), *headers,
    ]})
    await send({"type": "http.response.body", "body": message})


class _BearerGuard:
    def __init__(self, app: Any, token: str) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope["headers"]
        if any(name.lower() == b"origin" for name, _ in headers):
            await _refuse(send, 403, b'{"error":"this tool server takes no requests that carry an Origin header"}')
            return
        tokens = [value for name, value in headers if name.lower() == b"authorization"]
        expected = f"Bearer {self.token}".encode()
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], expected):
            await _refuse(send, 401, b'{"error":"this tool server needs the token it was mounted with"}')
            return
        if scope["method"] == "GET":
            # A stateless server sends no message of its own. The SDK would hold an empty event stream open, and the
            # server could not close while a client held it. MCP lets a server without a stream answer 405.
            await _refuse(send, 405, b'{"error":"this tool server offers no event stream: POST each request"}',
                          (b"allow", b"POST"))
            return
        await self.app(scope, receive, send)


def _uvicorn(app: Any) -> Any:
    import uvicorn

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Any:
            yield

    return Server(uvicorn.Config(app, access_log=False, lifespan="on", log_level="warning"))


class ToolServer:
    """An authenticated MCP endpoint backed by a Belle tool host."""

    def __init__(
        self,
        tools: ToolHost,
        conversation_of: ConversationOf,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        advertised_url: str = "",
        token: str = "",
    ) -> None:
        self._tools = tools
        self._conversation_of = conversation_of
        self._address = (host, port)
        self._advertised_url = advertised_url
        #: What every request must carry as ``Authorization: Bearer``: ``token``, or a new one at each construction.
        #: A harness that reads the token from its own environment needs a stable one.
        self.token = token or secrets.token_urlsafe(32)
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        assert self._port is not None, "the tool server is not started"
        return self._port

    @property
    def url(self) -> str:
        """Where clients reach the server: the advertised URL, or else the address it listens on."""
        if self._advertised_url:
            return self._advertised_url
        host = self._address[0]
        return f"http://{f'[{host}]' if ':' in host else host}:{self.port}{PATH}"

    def _hosts(self, port: int) -> list[str]:
        host = self._address[0]
        advertised = tools_authority(host, self._advertised_url)
        hosts = {advertised} if advertised else set()
        if host in {"127.0.0.1", "localhost", "0.0.0.0"}:
            hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})
        if host in {"::1", "::"}:
            hosts.add(f"[::1]:{port}")
        if host not in {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}:
            hosts.add(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
        return sorted(hosts)

    async def start(self) -> None:
        """Start the SDK endpoint on Belle's event loop."""
        if self._task is not None:
            raise RuntimeError("the tool server is already started")
        try:
            import mcp_types as types
            from mcp.server.lowlevel import Server
            from mcp.server.transport_security import TransportSecuritySettings
        except ImportError as exc:
            raise RuntimeError("the tool server needs belle-agent[mcp]") from exc

        host, port = self._address
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.create_server((host, port), family=family)
        self._socket = sock
        self._port = int(sock.getsockname()[1])
        try:
            allowed_hosts = self._hosts(self.port)

            async def list_tools(ctx: Any, params: Any) -> Any:
                tools = [types.Tool.model_validate({
                    "name": definition["name"],
                    "description": definition.get("description", ""),
                    "inputSchema": definition.get("input_params") or {"type": "object"},
                }) for definition in self._tools.definitions("")]
                return types.ListToolsResult(tools=tools)

            async def call_tool(ctx: Any, params: Any) -> Any:
                meta = params.meta
                if not isinstance(meta, Mapping):
                    meta = meta.model_dump(by_alias=True, exclude_none=True) if meta is not None else {}
                headers = {name.lower(): value for name, value in ctx.request.headers.items()} if ctx.request else {}
                conversation = await self._conversation_of(meta, headers)
                result = await self._tools.call(ToolCall(
                    conversation=conversation,
                    name=params.name,
                    arguments=dict(params.arguments or {}),
                    call_id=f"mcp-{secrets.token_urlsafe(16)}",
                ))
                return types.CallToolResult.model_validate({
                    "content": _blocks(result), "isError": not (succeeded(result) or held(result)),
                })

            sdk = Server("belle", version=version("belle-agent"), on_list_tools=list_tools, on_call_tool=call_tool)
            app = sdk.streamable_http_app(
                stateless_http=True,
                json_response=True,
                max_request_body_size=MAX_REQUEST_BYTES,
                transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts, allowed_origins=[]),
            )
            self._server = _uvicorn(_BearerGuard(app, self.token))
            self._task = asyncio.create_task(self._server.serve(sockets=[sock]), name="belle-mcp-server")
            while not self._server.started:
                if self._task.done():
                    await self._task
                    raise RuntimeError("the tool server exited before it started")
                await asyncio.sleep(0.01)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        try:
            if self._server is not None:
                self._server.should_exit = True
            if self._task is not None:
                await self._task
        finally:
            if self._socket is not None:
                self._socket.close()
            self._socket = None
            self._task = None
            self._server = None
            self._port = None
