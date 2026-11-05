"""A pass-through to the model provider that records what passes.

On a provider run the stand-in is out of the loop, so nothing in the Belle
container sees what OpenCode sends the model. It runs in a container of its
own: OpenCode's `API_BASE` names it, it has the outbound route in OpenCode's
place, and it forwards each request to the provider with the headers OpenCode
sent. It holds no key of its own. The key passes through in the authorization
header and is never stored: that header is dropped from every record, and its
value is cut out of any text kept.

    POST /v1/...     forwarded to OCB_UPSTREAM, answered as the provider answers
    POST /_script    {"answers": [...]}: answer the next requests here, in order, and forward nothing.
                     An answer is {"text": ...}, {"calls": [[tool, arguments], ...]} or {"status": 400}.
    GET  /v1/models  the names in the provider's public model list
    GET  /_ready     200

Every exchange is one JSON line in OCB_PROXY_LOG. A title request (OpenCode's
own, one per session) is answered here and not forwarded: it tests nothing and
would be a third of the requests. OCB_MAX_REQUESTS bounds what one run may
forward. A request over the bound is answered 400 here, which ends its turn.
So is a request that offers the model a tool named in OCB_REFUSED_TOOLS: on a
provider run OpenCode's shell and file tools can reach the key.
"""

import contextlib
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

UPSTREAM = os.environ.get("OCB_UPSTREAM", "").rstrip("/")
LOG = os.environ.get("OCB_PROXY_LOG", "/state/artefacts/provider-requests.jsonl")
MAX_REQUESTS = int(os.environ.get("OCB_MAX_REQUESTS") or 40)
# Tools no forwarded request may offer the model: a request that does is answered 400 here.
REFUSED_TOOLS = set(filter(None, os.environ.get("OCB_REFUSED_TOOLS", "").split(",")))
PORT = 14100
# Never stored. `x-opencode-session-id` and the rest say nothing of the key.
SECRET_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie"}
# Rewritten by this hop.
HOP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
KEPT_BODY = 400_000

_lock = threading.Lock()
_script: deque[dict] = deque()
_forwarded = 0


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {"id": "chatcmpl-ocb-proxy", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "answered-by-the-proxy", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def scripted(answer: dict) -> list[dict]:
    """The stream of one answer given here."""
    if answer.get("calls"):
        calls = [
            {"index": index, "id": f"call_ocb_{time.time_ns()}_{index}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}
            for index, (name, arguments) in enumerate(answer["calls"])
        ]
        return [_chunk({"tool_calls": calls}), _chunk({}, "tool_calls")]
    if "text" in answer:
        return [_chunk({"role": "assistant", "content": answer["text"]}), _chunk({}, "stop")]
    return [_chunk({"role": "assistant"}, "stop")]  # no content at all


def summary(text: str) -> dict:
    """What a streamed or plain completion said: the model that served it, usage, finish, text and calls."""
    found: dict = {"content": "", "tool_calls": [], "chunks": 0}
    payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")] or [text]
    names: dict[int, dict] = {}
    for payload in payloads:
        try:
            part = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(part, dict):
            continue
        found["chunks"] += 1
        for key in ("model", "provider", "usage", "error"):
            if part.get(key) is not None:
                found[key] = part[key]
        for choice in part.get("choices") or ():
            delta = choice.get("delta") or choice.get("message") or {}
            found["content"] += delta.get("content") or ""
            for call in delta.get("tool_calls") or ():
                entry = names.setdefault(call.get("index", 0), {"name": "", "arguments": ""})
                function = call.get("function") or {}
                entry["name"] += function.get("name") or ""
                entry["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason"):
                found["finish_reason"] = choice["finish_reason"]
    found["tool_calls"] = [names[index] for index in sorted(names)]
    return found


#: The opening of OpenCode's title agent prompt. The proxy runs as one file, so it keeps its own copy of
#: ``tests/conformance/standin.py``'s, and ``test_recording_proxy.py`` checks that the two agree.
TITLE_PROMPT = "You name conversations so the user can find them"


def is_title(body: dict) -> bool:
    return TITLE_PROMPT in json.dumps((body.get("messages") or [])[:1])


def record(entry: dict, secret: str) -> None:
    line = json.dumps(entry)
    if secret:
        line = line.replace(secret, "<removed>")
    with _lock, open(LOG, "a") as handle:
        handle.write(line + "\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, chunks, end: bool = True) -> None:
        for data in chunks:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
        if end:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def do_GET(self):
        if self.path == "/v1/models":
            # The provider's public model list, asked with no header of the caller's. Not a model request.
            answer = httpx.get(UPSTREAM + "/models", timeout=30)
            names = sorted(str(model.get("id")) for model in answer.json().get("data") or ())
            return self._json(answer.status_code, {"models": names})
        self._json(200, {"ready": True, "forwarded": _forwarded, "limit": MAX_REQUESTS})

    def do_POST(self):
        global _forwarded
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        if self.path == "/_script":
            with _lock:
                _script.extend(body.get("answers") or ())
            return self._json(200, {"queued": len(_script)})
        authorization = self.headers.get("Authorization", "")
        secret = authorization.split(" ", 1)[-1].strip()
        entry = {
            "at": time.time(),
            "path": self.path,
            "session": self.headers.get("x-opencode-session-id"),
            "headers": {k.lower(): v for k, v in self.headers.items() if k.lower() not in SECRET_HEADERS},
            "body": body,
        }
        local = None
        with _lock:
            if is_title(body):
                entry["answered"] = "title"
                local = {"text": "a title"}
            elif _script:
                entry["answered"] = "script"
                local = _script.popleft()
            elif offered := sorted(REFUSED_TOOLS & {tool["function"]["name"] for tool in body.get("tools") or ()}):
                entry["answered"] = "refused-tools"
                local = {"status": 400, "message": f"ocb: this run must not offer the model {', '.join(offered)}"}
            elif _forwarded >= MAX_REQUESTS:
                entry["answered"] = "budget"
                local = {"status": 400, "message": f"ocb: this run has forwarded its {MAX_REQUESTS} requests"}
            else:
                _forwarded += 1
                entry["forwarded"] = _forwarded
        if local is not None:
            entry["response"] = {"status": local.get("status", 200), "local": local}
            record(entry, secret)
            if "status" in local:
                return self._json(local["status"], {"error": {"message": local.get("message", "scripted failure")}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            return self._stream(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in scripted(local))
        self._forward(raw, entry, secret)

    def _forward(self, raw: bytes, entry: dict, secret: str) -> None:
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        headers["Accept-Encoding"] = "identity"
        url = UPSTREAM + self.path.removeprefix("/v1")
        kept, started, listening = [], time.monotonic(), True
        try:
            with httpx.stream("POST", url, headers=headers, content=raw, timeout=httpx.Timeout(300, connect=30)) as answer:
                status = answer.status_code
                shown = {k: v for k, v in answer.headers.items() if k.lower() not in SECRET_HEADERS}
                self.send_response(status)
                self.send_header("Content-Type", answer.headers.get("content-type", "application/json"))
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for data in answer.iter_bytes():
                    kept.append(data)
                    # OpenCode hangs up once it has read the end of a stream. The answer is still recorded whole.
                    with contextlib.suppress(OSError):
                        if listening:
                            listening = False
                            self._stream([data], end=False)
                            listening = True
        except httpx.HTTPError as exc:
            entry["response"] = {"status": 0, "error": f"{type(exc).__name__}: {exc}"}
            record(entry, secret)
            if not kept:
                # Nothing was answered yet. A 400 ends the turn once; OpenCode retries a 5xx and a silence.
                with contextlib.suppress(OSError):
                    self._json(400, {"error": {"message": f"ocb: the provider was not reached: {type(exc).__name__}"}})
            return
        text = b"".join(kept).decode("utf-8", "replace")
        entry["response"] = {
            "status": status,
            "seconds": round(time.monotonic() - started, 2),
            "headers": shown,
            "summary": summary(text),
            "body": text[:KEPT_BODY],
        }
        record(entry, secret)
        # The record is written before the answer ends, so whoever saw the end can read the record.
        with contextlib.suppress(OSError):
            if listening:
                self._stream([])


def selftest() -> None:
    stream = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in scripted({"calls": [["echo", {"a": 1}]]}))
    assert summary(stream)["tool_calls"] == [{"name": "echo", "arguments": '{"a": 1}'}]
    assert summary(stream)["finish_reason"] == "tool_calls"
    said = summary("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in scripted({"text": "hello"})))
    assert (said["content"], said["finish_reason"], said["model"]) == ("hello", "stop", "answered-by-the-proxy")
    assert "content" not in scripted({})[0]["choices"][0]["delta"]
    assert is_title({"messages": [{"role": "system", "content": TITLE_PROMPT}]})
    assert not is_title({"messages": [{"role": "user", "content": "a title"}]})
    assert summary('{"error": {"message": "no"}}')["error"] == {"message": "no"}


def serve(port: int = PORT, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    selftest()
    assert UPSTREAM, "OCB_UPSTREAM names the provider"
    serve()
    threading.Event().wait()
