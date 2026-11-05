"""The two listeners Belle exposes to OpenCode, as test fixtures.

`Relay` is the far end of the relay plugin: it records every hook round trip
and answers from a callable the test sets. `McpServer` is an MCP server over
HTTP: plain JSON answers to initialize, tools/list and tools/call. Both run
in threads of the test process and bind 0.0.0.0, because OpenCode reaches
them from another container.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Json(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status: int, payload=None) -> None:
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")


def _serve(port: int, handler: type, owner) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    server.owner = owner
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _RelayHandler(_Json):
    def do_POST(self):
        relay = self.server.owner
        event = self.read_json()
        hook = self.path.rsplit("/hook/", 1)[-1]
        if relay.token and self.headers.get("Authorization") != f"Bearer {relay.token}":
            relay.unauthorized.append(hook)
            return self.send_json(401, {"error": "bad relay token"})
        relay.hits.append((time.monotonic(), hook, event))
        answer = relay.answer(hook, event)
        relay.answered.append((time.monotonic(), hook))
        if isinstance(answer, int):  # a failed round trip
            return self.send_json(answer, {"error": "scripted relay failure"})
        try:
            self.send_json(200, answer or {})
        except (BrokenPipeError, ConnectionResetError):
            relay.dropped.append(hook)


class Relay:
    """Answers each relayed hook with `answer(hook, event)`: a dict, or an HTTP error status."""

    def __init__(self, port: int = 18200, token: str = ""):
        self.token = token
        self.hits: list[tuple[float, str, dict]] = []
        self.answered: list[tuple[float, str]] = []
        self.unauthorized: list[str] = []
        self.dropped: list[str] = []
        self.answer = lambda hook, event: {}
        self.server = _serve(port, _RelayHandler, self)

    def count(self, hook: str, session: str | None = None) -> int:
        return len(self.events(hook, session))

    def events(self, hook: str, session: str | None = None) -> list[dict]:
        return [e for _, h, e in list(self.hits) if h == hook and (session is None or e.get("sessionID") == session)]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _McpHandler(_Json):
    def do_GET(self):  # no server-initiated stream
        self.send_json(405)

    def do_DELETE(self):
        self.send_json(200, {})

    def do_POST(self):
        mcp = self.server.owner
        request = self.read_json()
        method, params = request.get("method"), request.get("params") or {}
        headers = {k.lower(): v for k, v in self.headers.items()}
        mcp.log.append(
            {
                "at": time.monotonic(),
                "method": method,
                "params": params,
                "headers": headers,
                "id": request.get("id"),
                "path": self.path,
            }
        )
        if "id" not in request:  # a notification
            return self.send_json(202)
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "belle-mcp", "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": [definition for definition, _ in mcp.tools.values()]}
        elif method == "tools/call" and params.get("name") in mcp.tools:
            answer = mcp.tools[params["name"]][1](params.get("arguments") or {})
            # A function may answer with content blocks of its own in place of one text.
            result = {"content": answer if isinstance(answer, list) else [{"type": "text", "text": answer}]}
        else:
            return self.send_json(
                200, {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": f"no {method}"}}
            )
        try:
            self.send_json(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})
        except (BrokenPipeError, ConnectionResetError):
            mcp.dropped.append(params.get("name"))


class McpServer:
    """Serves `tools`: name to (MCP tool definition, function of the arguments returning text)."""

    def __init__(self, port: int = 18300):
        self.tools: dict[str, tuple[dict, object]] = {}
        self.log: list[dict] = []
        self.dropped: list = []
        self.server = _serve(port, _McpHandler, self)

    def add(self, name: str, function, description: str = "", properties: dict | None = None) -> None:
        properties = properties or {"token": {"type": "string"}}
        schema = {"type": "object", "properties": properties, "required": list(properties)[:1]}
        self.tools[name] = ({"name": name, "description": description or name, "inputSchema": schema}, function)

    def calls(self, name: str | None = None) -> list[dict]:
        return [e for e in list(self.log) if e["method"] == "tools/call" and name in (None, e["params"].get("name"))]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
