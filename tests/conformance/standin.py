"""A scripted OpenAI-compatible endpoint that replaces the model.

The harness is unmodified. Only the completion is fabricated, so a turn still
runs the real session, inbox, permission and tool machinery. The reply is
scripted by the last user message of the request:

    SAY=<word>             final text <word>
    CALL=<tool>=<base64>   one call of <tool> with base64 JSON arguments, then
                           the final text "tool done". Several directives are
                           called in order, one for each request
    TICKS=<n>              n text deltas "t0 " to "t<n-1> "; the word SLOW in
                           the message paces them two per second
    FAIL=<status>          an HTTP error with that status
    WAIT=<seconds>         answer the turn's first request that much later
    anything else          final text "pong"

Only the last user message is read, so a directive in one turn does not repeat
in later turns of the same session. `python standin.py serve` serves it alone. Every request is kept in `StandIn.log`,
and appended to a JSON lines file when one is named, with the authorization
header removed. A request whose body is not a JSON object of the length it
announces, such as one its client cancelled while sending it, asks the model
nothing: it gets 400, and is neither kept nor scripted.
"""

import base64
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE = {"prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290}


def call(tool: str, **arguments) -> str:
    """The directive that makes the stand-in call `tool` with `arguments`."""
    return f"CALL={tool}={base64.b64encode(json.dumps(arguments).encode()).decode()}"


def text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-standin",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "standin",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


#: The opening of OpenCode's title agent prompt.
TITLE_PROMPT = "You name conversations so the user can find them"


def is_title(body: dict) -> bool:
    """Whether a provider request is OpenCode's own title request, whose first message is the title prompt."""
    return TITLE_PROMPT in json.dumps((body.get("messages") or [])[:1])


def last_user(body: dict) -> tuple[str, int]:
    """The text of the last user message, and how many tool results follow it."""
    messages = body.get("messages", [])
    last = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    text = text_of(messages[last]) if last >= 0 else ""
    return text, sum(m.get("role") == "tool" for m in messages[last + 1 :])


def script(body: dict) -> list[dict] | int:
    """The chunks to stream for one request, or an HTTP error status."""
    text, answered = last_user(body)
    wait = re.search(r"WAIT=([\d.]+)", text)
    if wait and not answered:
        time.sleep(float(wait.group(1)))
    fail = re.search(r"FAIL=(\d{3})", text)
    if fail:
        return int(fail.group(1))
    directives = re.findall(r"CALL=([\w.-]+)=([A-Za-z0-9+/=]+)", text)
    if answered < len(directives):
        name, arguments = directives[answered]
        tool_call = {
            "index": 0,
            "id": f"call_standin_{time.time_ns()}",
            "type": "function",
            "function": {"name": name, "arguments": base64.b64decode(arguments).decode()},
        }
        return [_chunk({"tool_calls": [tool_call]}), _chunk({}, "tool_calls")]
    if directives:
        return [_chunk({"content": "tool done"}), _chunk({}, "stop")]
    ticks = re.search(r"TICKS=(\d+)", text)
    if ticks:
        return [_chunk({"content": f"t{i} "}) for i in range(int(ticks.group(1)))] + [_chunk({}, "stop")]
    say = re.search(r"SAY=(\S+)", text)
    return [_chunk({"content": say.group(1) if say else "pong"}), _chunk({}, "stop")]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # some clients list /v1/models before the first completion
        self._json(200, {"object": "list", "data": [{"id": "standin", "object": "model"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if len(raw) == length else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            message = f"the body is {len(raw)} of the {length} bytes it announced, or is not a JSON object"
            try:
                return self._json(400, {"error": {"message": message, "code": 400}})
            except (BrokenPipeError, ConnectionResetError):
                return None
        owner = self.server.owner
        owner.record(self.path, self.headers, body)
        chunks = owner.script(body, self.headers.get("x-opencode-session-id"))
        if isinstance(chunks, int):
            return self._json(chunks, {"error": {"message": "scripted failure", "code": chunks}})
        slow = "SLOW" in last_user(body)[0] and len(chunks) > 2
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for chunk in chunks:
                self._event(chunk)
                if slow:
                    time.sleep(0.5)
            self._event(dict(_chunk({}), usage=USAGE, choices=[]))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _event(self, payload: dict) -> None:
        data = ("data: " + json.dumps(payload) + "\n\n").encode()
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StandIn:
    """The stand-in model, served from a thread of the test process."""

    def __init__(self, port: int = 14099, log_path: str | None = None, host: str = "0.0.0.0"):
        self.log: list[dict] = []
        self.log_path = log_path
        #: What answers a request: its body and session id to chunks or an HTTP status. A test may replace it.
        self.script = lambda body, session: script(body)
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer((host, port), _Handler)
        self.server.daemon_threads = True
        self.server.owner = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def record(self, path: str, headers, body: dict) -> None:
        entry = {
            "at": time.monotonic(),
            "path": path,
            "session": headers.get("x-opencode-session-id"),
            "headers": {k.lower(): v for k, v in headers.items() if k.lower() != "authorization"},
            "body": body,
        }
        with self._lock:
            self.log.append(entry)
            if self.log_path:
                with open(self.log_path, "a") as handle:
                    handle.write(json.dumps(entry) + "\n")

    def requests(self, session: str) -> list[dict]:
        """Provider request bodies of one session, in order, without title requests."""
        with self._lock:
            entries = [e for e in self.log if e["session"] == session]
        return [e["body"] for e in entries if not is_title(e["body"])]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def tools(body: dict) -> list[str]:
    """The tool names one provider request advertises."""
    return sorted(tool["function"]["name"] for tool in body.get("tools", []))


def said(body: dict, role: str) -> list[str]:
    return [text_of(message) for message in body.get("messages", []) if message.get("role") == role]


def selftest() -> None:
    """Each directive produces the reply shape the checks rely on."""

    def deltas(text: str, tool_results: int = 0) -> list[dict]:
        messages = [{"role": "user", "content": "SAY=old"}, {"role": "assistant", "content": "old"}]
        messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        messages += [{"role": "tool", "content": "done"}] * tool_results
        return [chunk["choices"][0]["delta"] for chunk in script({"messages": messages})]

    assert deltas("hello")[0] == {"content": "pong"}
    assert deltas("x SAY=WORD y")[0] == {"content": "WORD"}
    assert [d["content"] for d in deltas("TICKS=3")[:-1]] == ["t0 ", "t1 ", "t2 "]
    function = deltas(call("echo", token="T"))[0]["tool_calls"][0]["function"]
    assert function == {"name": "echo", "arguments": '{"token": "T"}'}
    assert deltas(call("echo", token="T"), tool_results=1)[0] == {"content": "tool done"}
    two = f"{call('echo', token='T')} {call('other')}"
    assert deltas(two, tool_results=1)[0]["tool_calls"][0]["function"]["name"] == "other"
    assert deltas(two, tool_results=2)[0] == {"content": "tool done"}
    assert script({"messages": [{"role": "user", "content": "FAIL=503"}]}) == 503
    assert is_title({"messages": [{"role": "system", "content": TITLE_PROMPT}]})
    assert not is_title({"messages": [{"role": "user", "content": "SAY=title"}]})


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        # The model of a running system that no test drives, such as a loopback example.
        StandIn()
        threading.Event().wait()
    selftest()
    print("selftest ok")
