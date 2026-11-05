"""A scripted OpenAI-compatible ``/embeddings`` endpoint for the corpus index.

A text's vector is its count of words from each of a few topics, so a question
can share no word with a summary and still point the way it does. Like the
summarizer's stand-in, it listens on a port of its own and keeps a log.
"""

import base64
import json
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOPICS = (
    {"lighthouse", "lens", "fresnel", "keeper", "keepers", "lamp", "beacon", "mantle"},
    {"kite", "bridle", "tow", "wind", "stall", "stalls", "glider"},
    {"tide", "tides", "estuary", "slipway", "ferry", "harbour", "flood", "floods"},
    {"hive", "brood", "varroa", "mites", "bees", "beekeeper", "frames"},
)


def vector(text: str) -> list[float]:
    words = re.findall(r"[a-z]+", text.lower())
    return [float(sum(word in topic for word in words)) for topic in TOPICS] + [0.01]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        owner = self.server.owner
        with owner.lock:
            owner.log.append({"path": self.path, "model": body.get("model"), "input": texts})
        as_base64 = body.get("encoding_format") == "base64"
        data = []
        for index, text in enumerate(texts):
            values = vector(text)
            packed = base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode() if as_base64 else values
            data.append({"object": "embedding", "index": index, "embedding": packed})
        payload = json.dumps(
            {"object": "list", "data": data, "model": body.get("model"), "usage": {"prompt_tokens": 1, "total_tokens": 1}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class EmbeddingStandIn:
    """The embedder's endpoint, served from a thread of the test process on loopback."""

    def __init__(self) -> None:
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.owner = self
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def texts(self) -> list[str]:
        """Each text it was asked to embed, in order."""
        with self.lock:
            return [text for entry in self.log for text in entry["input"]]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
