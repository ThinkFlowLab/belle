"""The recording proxy keeps no key, forwards what it should and answers the rest itself."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import recording_proxy
import standin

KEY = "sk-the-provider-key"


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        # A provider that echoes the key, which a real one does not.
        data = json.dumps({"model": "served", "seen": self.headers.get("Authorization"), "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    monkeypatch.setattr(recording_proxy, "UPSTREAM", f"http://127.0.0.1:{upstream.server_address[1]}/api/v1")
    monkeypatch.setattr(recording_proxy, "LOG", str(tmp_path / "log" / "requests.jsonl"))
    monkeypatch.setattr(recording_proxy, "MAX_REQUESTS", 2)
    monkeypatch.setattr(recording_proxy, "REFUSED_TOOLS", {"shell"})
    monkeypatch.setattr(recording_proxy, "_forwarded", 0)
    server = recording_proxy.serve(0, "127.0.0.1")
    client = httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", headers={"Authorization": f"Bearer {KEY}"})
    yield client, tmp_path / "log" / "requests.jsonl"
    client.close()
    server.shutdown()
    upstream.shutdown()


def _ask(client, **body):
    return client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}], **body})


def test_the_proxy_passes_the_key_on_and_stores_it_nowhere(proxy):
    client, log = proxy

    answer = _ask(client).json()

    assert answer == {"model": "served", "seen": f"Bearer {KEY}", "path": "/api/v1/chat/completions"}
    (entry,) = map(json.loads, log.read_text().splitlines())
    assert KEY not in log.read_text()
    assert "authorization" not in entry["headers"]
    assert entry["response"]["summary"]["model"] == "served"


def test_the_proxy_answers_what_it_must_not_forward(proxy):
    client, log = proxy
    client.post("/_script", json={"answers": [{"text": "scripted"}, {"status": 400}]})

    scripted = _ask(client)
    failed = _ask(client)
    title = client.post("/v1/chat/completions", json={"messages": [{"role": "system", "content": recording_proxy.TITLE_PROMPT}]})
    tools = [{"type": "function", "function": {"name": "shell"}}]
    refused = _ask(client, tools=tools)
    first, second, over = _ask(client), _ask(client), _ask(client)

    assert "scripted" in scripted.text and "a title" in title.text
    assert (failed.status_code, refused.status_code) == (400, 400) and "shell" in refused.text
    assert (first.status_code, second.status_code, over.status_code) == (200, 200, 400)
    answered = [json.loads(line).get("answered") for line in log.read_text().splitlines()]
    assert answered == ["script", "script", "title", "refused-tools", None, None, "budget"]


def test_the_proxys_own_shapes():
    recording_proxy.selftest()


def test_the_proxy_finds_a_title_request_as_the_stand_in_does():
    assert recording_proxy.TITLE_PROMPT == standin.TITLE_PROMPT
    for body in ({"messages": [{"role": "system", "content": standin.TITLE_PROMPT}]}, {"messages": [{"role": "user", "content": "SAY=x"}]}):
        assert recording_proxy.is_title(body) == standin.is_title(body)
