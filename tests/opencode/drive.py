"""Drive an unmodified OpenCode v2 server over its HTTP API.

HTTP basic auth, JSON calls, and one reader thread on the server's event
stream. Standard library only.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

TERMINAL = {
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
}


def _decode(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def tool_result(text: str):
    """A tool's JSON answer as a provider request gives it to the model.

    OpenCode gives the answer of a failed call as the ``message`` of its ``tool.execution`` error.
    """
    result = json.loads(text)
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict) and error.get("type") == "tool.execution":
        return json.loads(error["message"])
    return result


class Client:
    """HTTP basic auth client for one OpenCode server."""

    def __init__(self, url: str | None = None, password: str | None = None, username: str = "opencode"):
        self.url = (url or os.environ["OCB_OPENCODE_URL"]).rstrip("/")
        password = password if password is not None else os.environ["OPENCODE_SERVER_PASSWORD"]
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def call(self, method: str, path: str, body=None, timeout: float = 30):
        """Return (status, decoded JSON or raw text). An HTTP error is returned, not raised."""
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())

    def api(self, method: str, path: str, body=None):
        """The `data` member of a successful call."""
        status, result = self.call(method, path, body)
        assert status in (200, 204), (method, path, status, result)
        return result.get("data") if isinstance(result, dict) else result

    def wait_ready(self, timeout: float = 120) -> dict:
        """Wait for the model catalog. The listener answers before it is built.

        Returns the default model's ids only. The full answer of
        `GET /api/model/default` holds the provider key in `settings.apiKey`,
        so it is never returned, logged or stored.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                status, result = self.call("GET", "/api/model/default", timeout=5)
                if status == 200 and isinstance(result.get("data"), dict):
                    return {key: result["data"].get(key) for key in ("id", "providerID")}
            except OSError:
                pass
            assert time.monotonic() < deadline, "OpenCode did not become ready"
            time.sleep(0.5)

    def session(self, **body) -> str:
        return self.api("POST", "/api/session", body)["id"]

    def prompt(self, session: str, text: str, **body):
        return self.call("POST", f"/api/session/{session}/prompt", {"text": text, **body})

    def messages(self, session: str) -> list[dict]:
        """Stored session messages, oldest first."""
        return list(reversed(self.api("GET", f"/api/session/{session}/message")))


class Events:
    """One subscriber on `GET /api/event`. Subscribe before creating a session."""

    def __init__(self, client: Client):
        self.seen: list[tuple[float, dict]] = []
        self.closed = threading.Event()
        self._changed = threading.Condition()
        request = urllib.request.Request(client.url + "/api/event", headers=client.headers)
        self.stream = urllib.request.urlopen(request, timeout=None)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.stream:
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    with self._changed:
                        self.seen.append((time.monotonic(), json.loads(line[5:].strip())))
                        self._changed.notify_all()
        except (OSError, ValueError, AttributeError):  # AttributeError: closed under the reader
            pass
        finally:
            self.closed.set()
            with self._changed:
                self._changed.notify_all()

    def of(self, session: str | None = None, start: int = 0) -> list[dict]:
        """Events seen from index `start`, of one session when one is named."""
        with self._changed:
            events = [event for _, event in self.seen[start:]]
        return [e for e in events if session is None or session_of(e) == session]

    def mark(self) -> int:
        with self._changed:
            return len(self.seen)

    def wait(self, session: str, types=TERMINAL, start: int = 0, timeout: float = 90) -> dict:
        """The first event of `session` from index `start` whose type is in `types`."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for _, event in self.seen[start:]:
                    if event.get("type") in types and session_of(event) == session:
                        return event
                remaining = deadline - time.monotonic()
                assert remaining > 0 and not self.closed.is_set(), (
                    f"no {sorted(types)} for {session}; saw {[e.get('type') for _, e in self.seen[start:]][-12:]}"
                )
                self._changed.wait(remaining)

    def wait_for_type(self, kind: str, start: int = 0, timeout: float = 30) -> dict:
        """The first event of type `kind` from index `start`, of any session."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for _, event in self.seen[start:]:
                    if event.get("type") == kind:
                        return event
                remaining = deadline - time.monotonic()
                assert remaining > 0 and not self.closed.is_set(), f"no {kind} event"
                self._changed.wait(remaining)

    def close(self) -> None:
        self.stream.close()


def session_of(event: dict) -> str | None:
    data = event.get("data") or {}
    return data.get("sessionID") or (event.get("durable") or {}).get("aggregateID")


def text(events: list[dict]) -> str:
    return "".join(e["data"]["delta"] for e in events if e.get("type") == "session.text.delta")


def turn(client: Client, events: Events, session: str, prompt: str, timeout: float = 90, **body) -> dict:
    """Send one prompt and wait for the execution to end. Returns the terminal event."""
    start = events.mark()
    status, result = client.prompt(session, prompt, **body)
    assert status == 200, (status, result)
    return events.wait(session, start=start, timeout=timeout)


def mount(client: Client, name: str, port: int = 18300) -> None:
    """Mount the MCP server of this process on the wire, advertised by name (`<name>_<tool>`)."""
    config = {"type": "remote", "url": f"http://{os.environ['OCB_PEER']}:{port}/mcp", "oauth": False, "codemode": False}
    status, body = client.call("PUT", f"/api/experimental/mcp/{name}", {"config": config})
    assert status in (200, 204), (status, body)
    # No event marks the refreshed tool catalog (check 7 of probe_questions.py). A turn
    # sent 0.3 s after the PUT saw the tool; one sent at once did not.
    time.sleep(2)
