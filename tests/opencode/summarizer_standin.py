"""A scripted OpenAI-compatible endpoint for the second brain's own model calls.

It is not the stand-in that plays conversation turns. It listens on a port of
its own and keeps a log of its own, so a check can tell a summarizer request
from a turn's: nothing OpenCode sends arrives here, and nothing the second
brain sends arrives there. It answers whole, not as a stream, with the words
of the document it was shown, so that a search of the summary has something
to rank. A request with an ``x-api-key`` header gets direct ScaleDown's
reply form.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARK = "Summarized by the stand-in."


def summary(prompt: str) -> str:
    if "REGIONS BEGIN" in prompt:
        return "1: what the section says."
    body = prompt.split("DOCUMENT TEXT BEGINS ---", 1)[-1].split("--- DOCUMENT TEXT ENDS", 1)[0]
    return f"{MARK} " + " ".join(body.split())[:600]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        owner = self.server.owner
        with owner.lock:
            owner.log.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )
        messages = body.get("messages") or [{}]
        content = messages[-1].get("content")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = summary(str(content or ""))
        if self.headers.get("x-api-key"):
            # Direct ScaleDown answers with a JSON object in the message content.
            text = json.dumps({"summary": text, "output_chars": len(text), "notice": None})
        answer = {
            "id": "chatcmpl-summarizer",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "summarizer"),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SummarizerStandIn:
    """The summarizer's model, served from a thread of the test process on loopback."""

    def __init__(self) -> None:
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.owner = self
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def prompts(self) -> list[str]:
        """The user text of each request, in order."""
        with self.lock:
            return [str(entry["body"].get("messages", [{}])[-1].get("content", "")) for entry in self.log]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
