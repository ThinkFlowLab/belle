"""A scripted typed-decision endpoint, for the checks of the decision gate.

It is not the stand-in that plays conversation turns, and it is no backend.
It listens on a port of its own in the test process and keeps a log of its
own, so a check can tell what Belle asked the decision model from what a
turn asked the conversation model. It stores no authorization header: a
request is logged with whether it carried the expected key.
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: A synthetic key. It names no account anywhere.
KEY = "decision-key-of-the-stand-in"
#: The variable the configuration names for the key.
KEY_ENV = "DECISION_API_KEY"
MODEL = "typesafe/stand-in"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        owner: DecisionStandIn = self.server.owner
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with owner.lock:
            owner.log.append(
                {"path": self.path, "authorized": self.headers.get("Authorization") == f"Bearer {KEY}", "body": body}
            )
        if owner.delay:
            time.sleep(owner.delay)
        data = json.dumps(owner.answer(body)).encode()
        self.send_response(owner.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            # The client gave up on a slow answer.
            pass


class DecisionStandIn:
    """Answer every question it is asked, the same way each time unless a check says otherwise."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        #: Every request received, in order.
        self.log: list[dict[str, Any]] = []
        #: Question id to the value answered, in place of the default.
        self.values: dict[str, Any] = {}
        self.status = 200
        #: Seconds to wait before answering.
        self.delay = 0.0
        #: Answer with this body, whatever was asked.
        self.raw: Any = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def config(self, forward: bool | None = None, extra: str = "", api: str = "") -> str:
        """The settings that point a host at this endpoint. The key is named, never written."""
        section = "" if forward is None else f"platforms: {{loopback: {{decision_forward: {str(forward).lower()}{extra}}}}}\n"
        # The native OpenAI API is addressed at a base that ends in /v1.
        chosen, base = (api, f"{self.url}/v1") if api else ("openrouter", self.url)
        entry = f'{{id: standin, api: {chosen}, api_base: "{base}", api_key: "${{{KEY_ENV}}}", model_name: {MODEL}}}'
        return f"models: {{decision: {{entries: [{entry}]}}}}\n{section}"

    def answer(self, body: dict[str, Any]) -> Any:
        if self.raw is not None:
            return self.raw
        answers = {}
        for question_id, question in (body.get("questions") or {}).items():
            kind = question["type"]
            default = next(iter(question["criteria"])) if kind == "choice" else 0.25
            answers[question_id] = {"type": kind, kind: self.values.get(question_id, default)}
        return {"answers": answers}

    def states(self) -> list[dict[str, Any]]:
        with self.lock:
            return [entry["body"]["state"] for entry in self.log]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


async def logged(caplog, count: int = 1) -> list[dict[str, Any]]:
    """Wait for the gate's own records of ``count`` decisions. A logged decision waits for nobody, so a check waits for it."""
    label = "decision shadow:"
    async with asyncio.timeout(10):
        while len(lines := [line for line in caplog.messages if label in line]) < count:
            await asyncio.sleep(0.01)
    return [json.loads(line.split(label, 1)[1]) for line in lines]
