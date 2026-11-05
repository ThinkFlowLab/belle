#!/usr/bin/env python3
"""Answer numbered questions about the pinned Docker Agent build, one check for each.

Run in the Belle container beside a running Docker Agent container on an
internal network. The stand-in model, a stub hook endpoint and Belle's core
``ToolServer`` are threads of this process, so Docker Agent reaches all three
across the network.

    main        every question that needs one server lifetime, on the stand-in
    restarted   after the Docker Agent container was stopped and started
    provider    one plain turn and one turn that calls a Belle tool, on a real model

Each check prints one JSON line: the question number and what was observed.

Environment: DAB_AGENT_URL, DOCKER_AGENT_AUTH_TOKEN, BELLE_RELAY_TOKEN, and
DAB_STATE, where the lines are also appended. DAB_HOLD_SECONDS is how long the
confirmation check keeps a paused turn open, default 45.
"""

import asyncio
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path[:0] = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", name) for name in ("opencode", "conformance")
]
from listeners import Relay
from standin import StandIn, call, said, tools

from belle_agent.tools import server as tool_server
from belle_agent.tools.host import BoundTurn, ToolHost, request_metadata

AGENT = "belle"
URL = os.environ.get("DAB_AGENT_URL", "http://dab-agent:8080").rstrip("/")
STATE = os.environ.get("DAB_STATE", "/state")
HOLD = float(os.environ.get("DAB_HOLD_SECONDS", "45"))
MARK = "SYSTEM-ADDITION-MARK-7Q"
TITLES = ("dab-probe-a", "dab-probe-b")

stage = sys.argv[1] if len(sys.argv) > 1 else "main"


def record(question: int, check: str, **observed) -> None:
    line = json.dumps({"question": question, "stage": stage, "check": check, **observed})
    print(line, flush=True)
    with open(os.path.join(STATE, "probe-questions.jsonl"), "a") as handle:
        handle.write(line + "\n")


# ---------------------------------------------------------------- the wire


def _headers(extra=None) -> dict:
    return {
        "Authorization": f"Bearer {os.environ['DOCKER_AGENT_AUTH_TOKEN']}",
        "Content-Type": "application/json",
        **(extra or {}),
    }


def request(method: str, path: str, body=None, headers=None, timeout: float = 120):
    """One call with the server's bearer token. Return (status, decoded JSON or text)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        URL + "/api" + path,
        data,
        _headers(headers),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    try:
        return status, json.loads(raw) if raw.strip() else None
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def new_session(title: str, **fields) -> str:
    status, session = request("POST", "/sessions", {"title": title, "safety_policy": "autonomous", **fields})
    assert status == 200, (status, session)
    return session["id"]


def turn(session: str, prompt: str, headers=None, model: str = "", on_event=None) -> list[dict]:
    """Run one turn and return its SSE events. An HTTP error is one event of type `http_error`."""
    body = {"messages": [{"role": "user", "content": prompt}], **({"model": model} if model else {})}
    request = urllib.request.Request(
        f"{URL}/api/sessions/{session}/agent/{AGENT}",
        json.dumps(body).encode(),
        _headers(headers),
    )
    events = []
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            for line in response:
                if line.startswith(b"data: "):
                    events.append(json.loads(line[6:]))
                    if on_event:
                        on_event(events[-1])
                elif line.strip():
                    events.append({"type": "non_data_line", "line": line.decode("utf-8", "replace").strip()})
    except urllib.error.HTTPError as error:
        events.append({"type": "http_error", "status": error.code, "body": error.read().decode("utf-8", "replace")})
    return events


def kinds(events: list[dict]) -> list[str]:
    return [event.get("type", "") for event in events]


def text(events: list[dict]) -> str:
    return "".join(event.get("content", "") for event in events if event.get("type") == "agent_choice")


# ---------------------------------------------------------------- Belle's side


class Toolkit:
    """One tool that records each call with the metadata of the turn it was bound to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_tool_definitions(self):
        schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
        return [{"name": "echo", "description": "Echo a text back.", "input_params": schema}]

    async def echo(self, text: str) -> str:
        self.calls.append((text, dict(request_metadata())))
        return f"echo saw {text}"


class Belle:
    """The core tool server and a stub hook endpoint. A turn is bound by a fresh `X-Belle-Turn` value."""

    def __init__(self) -> None:
        self.toolkit = Toolkit()
        self.host = ToolHost([self.toolkit])
        self.turns: dict[str, str] = {}  # turn header value to conversation
        #: Every request the tool server received: its JSON-RPC method (None when refused) and headers.
        self.mcp: list[dict] = []
        self.relay = Relay(token=os.environ["BELLE_RELAY_TOKEN"])
        self.relay.answer = self._answer
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.tools = tool_server.ToolServer(
            self.host, self._conversation_of, host="0.0.0.0", port=18300,
            advertised_url=os.environ.get("BELLE_TOOLS_URL", "http://belle:18300/mcp"),
        )
        self._watch()
        asyncio.run_coroutine_threadsafe(self.tools.start(), self.loop).result()

    def _watch(self) -> None:
        """Record requests without relying on a transport handler class."""
        original = tool_server._uvicorn

        def watched(app):
            async def observe(scope, receive, send):
                if scope["type"] != "http":
                    await app(scope, receive, send)
                    return
                entry = {"method": None, "headers": {k.decode().lower(): v.decode() for k, v in scope["headers"]}}
                self.mcp.append(entry)
                chunks = []

                async def copy_receive():
                    message = await receive()
                    if message["type"] == "http.request":
                        chunks.append(message.get("body", b""))
                        if not message.get("more_body", False):
                            try:
                                entry["method"] = json.loads(b"".join(chunks)).get("method")
                            except (ValueError, AttributeError):
                                pass
                    return message

                await app(scope, copy_receive, send)

            return original(observe)

        tool_server._uvicorn = watched

    async def _conversation_of(self, meta, headers) -> str:
        return self.turns.get(headers.get("x-belle-turn", ""), "")

    def _answer(self, hook: str, event: dict) -> dict:
        if event.get("hook_event_name") == "user_prompt_submit":
            return {"hook_specific_output": {"hook_event_name": "user_prompt_submit", "additional_context": MARK}}
        return {}

    def bind(self, conversation: str) -> dict:
        """Open a conversation to tool calls and return the headers of its next turn request."""
        value = secrets.token_hex(8)
        self.turns[value] = conversation
        self.host.bind(conversation, BoundTurn(value, metadata={"conversation": conversation}))
        return {"X-Belle-Tools-Token": self.tools.token, "X-Belle-Turn": value}

    def hooks(self, name: str = "") -> list[dict]:
        return [e for _, _, e in list(self.relay.hits) if name in ("", e.get("hook_event_name"))]


def provider_requests(model: StandIn) -> list[dict]:
    return [entry["body"] for entry in list(model.log) if entry["path"].endswith("/chat/completions")]


def systems(body: dict) -> str:
    return "\n".join(said(body, "system"))


def tool_results(events: list[dict]) -> list[str]:
    return [str(event.get("response", "")) for event in events if event.get("type") == "tool_call_response"]


# ---------------------------------------------------------------- stages


def main() -> None:
    model = StandIn()
    belle = Belle()

    status, agent = request("GET", f"/agents/{AGENT}")
    record(12, "agent configuration", status=status, lists_a_skill="group-chat-participation" in json.dumps(agent))
    if isinstance(agent, dict):
        with open(os.path.join(STATE, "agent.json"), "w") as handle:
            json.dump(agent, handle, indent=1)

    # A plain turn: session, stream, terminal event, one provider request, one hook round trip.
    a = new_session(TITLES[0])
    before = len(provider_requests(model))
    events = turn(a, "SAY=hello", belle.bind("conversation-a"))
    first = provider_requests(model)[before:]
    record(
        0,
        "plain turn",
        events=kinds(events),
        text=text(events),
        provider_requests=len(first),
        stream_stopped="stream_stopped" in kinds(events),
    )
    assert "stream_stopped" in kinds(events), events[-1:]
    prompts = belle.hooks("user_prompt_submit")
    record(
        3,
        "relay token from the environment",
        hooks=[e.get("hook_event_name") for e in belle.hooks()],
        unauthorized=list(belle.relay.unauthorized),
    )
    record(
        4,
        "hook round trip across containers",
        user_prompt_submit=len(prompts),
        addition_in_system=bool(first) and MARK in systems(first[0]),
    )
    record(
        1,
        "tool server requests of the first turn",
        requests=[
            {
                "method": e["method"],
                "token": e["headers"].get("authorization") == f"Bearer {belle.tools.token}",
                "turn": e["headers"].get("x-belle-turn"),
            }
            for e in belle.mcp
        ],
    )
    advertised = tools(first[0]) if first else []
    record(2, "advertised names", tools=advertised)
    record(17, "remote.url from the environment", tools_listed=bool(advertised))

    # A turn that calls Belle's tool: the token and this turn's header reach the call.
    echo = next((name for name in advertised if name.endswith("echo")), "belle_echo")
    headers = belle.bind("conversation-a")
    before, seen = len(provider_requests(model)), len(belle.mcp)
    events = turn(a, call(echo, text="T1"), headers)
    calls = [e for e in belle.mcp[seen:] if e["method"] == "tools/call"]
    requests = provider_requests(model)[before:]
    record(
        0,
        "tool call bound by the turn header",
        toolkit=belle.toolkit.calls[-1:],
        results=tool_results(events),
        calls=[
            {
                "token": e["headers"].get("authorization") == f"Bearer {headers['X-Belle-Tools-Token']}",
                "turn": e["headers"].get("x-belle-turn") == headers["X-Belle-Turn"],
            }
            for e in calls
        ],
        tool_guard=len(belle.hooks("tool_guard")),
    )
    record(9, "events of a turn with two model calls", events=kinds(events), provider_requests=len(requests))
    record(
        11,
        "system addition on each model call of a turn",
        provider_requests=len(requests),
        with_addition=[MARK in systems(body) for body in requests],
    )

    # The same session with a turn header that no turn holds: Belle refuses the call.
    count = len(belle.toolkit.calls)
    events = turn(a, call(echo, text="T2"), {**belle.bind("conversation-a"), "X-Belle-Turn": "not-a-turn"})
    record(0, "wrong turn header refused", ran=len(belle.toolkit.calls) - count, results=tool_results(events))

    # No token header: the placeholder is sent as written and the tool server answers 401.
    b = new_session(TITLES[1])
    seen, before = len(belle.mcp), len(provider_requests(model))
    events = turn(b, "SAY=untokened", {"X-Belle-Turn": "none"})
    requests = provider_requests(model)[before:]
    record(
        1,
        "tool server refuses the first request",
        events=kinds(events),
        text=text(events),
        errors=[e for e in events if e.get("type") in ("error", "http_error", "warning")][:3],
        mcp=[
            {"method": e["method"], "authorization": e["headers"].get("authorization", "")[:40]}
            for e in belle.mcp[seen:]
        ],
        tools=[tools(body) for body in requests],
    )
    events = turn(b, call(echo, text="T3"), belle.bind("conversation-b"))
    record(
        1,
        "the next turn of that session, with the token",
        events=kinds(events),
        results=tool_results(events),
        ran=belle.toolkit.calls[-1:],
    )

    # Session permissions changed between two turns of a built runtime.
    count = len(belle.toolkit.calls)
    status, _ = request("PATCH", f"/sessions/{a}/permissions", {"permissions": {"deny": [echo]}})
    events = turn(a, call(echo, text="T4"), belle.bind("conversation-a"))
    record(
        6,
        "deny pattern on the next turn",
        patch=status,
        ran=len(belle.toolkit.calls) - count,
        events=kinds(events),
        results=tool_results(events),
    )
    request("PATCH", f"/sessions/{a}/permissions", {"permissions": {}})

    # A model reference outside the YAML's models block, and an unknown one.
    for reference in ("belle/second-model", "nothing-of-this-name"):
        before = len(provider_requests(model))
        events = turn(a, "SAY=model", belle.bind("conversation-a"), model=reference)
        requests = provider_requests(model)[before:]
        record(
            7,
            f"model field {reference}",
            events=kinds(events),
            text=text(events),
            errors=[e for e in events if e.get("type") in ("error", "http_error")][:2],
            models=[body.get("model") for body in requests],
        )
    events = turn(a, "SAY=after", belle.bind("conversation-a"))
    record(
        7, "the turn after, with no model field", models=[body.get("model") for body in provider_requests(model)[-1:]]
    )

    # A paused turn: what the stream carries while it waits.
    request("PATCH", f"/sessions/{a}/permissions", {"permissions": {"ask": [echo]}})
    paused = threading.Event()
    started = time.monotonic()
    timeline: list[tuple[float, str]] = []

    def on_event(event: dict) -> None:
        timeline.append((round(time.monotonic() - started, 1), event.get("type", "")))
        if event.get("type") == "tool_call_confirmation":
            paused.set()

    def answer() -> None:
        if paused.wait(60):
            time.sleep(HOLD)
            request("POST", f"/sessions/{a}/resume", {"confirmation": "reject", "reason": "probe"})

    threading.Thread(target=answer, daemon=True).start()
    events = turn(a, call(echo, text="T5"), belle.bind("conversation-a"), on_event=on_event)
    record(
        8,
        "a paused turn's stream",
        hold_seconds=HOLD,
        timeline=timeline,
        non_data=[e for e in events if e["type"] == "non_data_line"][:5],
    )
    request("PATCH", f"/sessions/{a}/permissions", {"permissions": {}})

    status, listed = request("GET", "/sessions")
    record(
        5, "titles before the restart", titles=sorted(s.get("title", "") for s in listed or [] if isinstance(s, dict))
    )
    with open(os.path.join(STATE, "sessions.json"), "w") as handle:
        json.dump({"a": a, "b": b}, handle)


def restarted() -> None:
    with open(os.path.join(STATE, "sessions.json")) as handle:
        sessions = json.load(handle)
    status, listed = request("GET", "/sessions")
    record(
        5,
        "titles after the restart",
        status=status,
        sessions=sorted(
            (s.get("id") in sessions.values(), s.get("title", "")) for s in listed or [] if isinstance(s, dict)
        ),
    )
    model = StandIn()
    belle = Belle()
    events = turn(sessions["a"], "SAY=again", belle.bind("conversation-a"))
    history = provider_requests(model)[-1:]
    record(
        5,
        "the next turn holds the earlier ones",
        events=kinds(events),
        text=text(events),
        user_messages=len(said(history[0], "user")) if history else None,
    )


def provider() -> None:
    """The real model. Each session allows three model calls per turn."""
    belle = Belle()
    a = new_session(TITLES[0], max_iterations=3)
    events = turn(a, "Reply with the single word READY.", belle.bind("conversation-a"))
    record(
        0,
        "provider plain turn",
        events=kinds(events),
        text=text(events)[:200],
        model_calls=kinds(events).count("token_usage"),
    )
    events = turn(
        a,
        "Call the belle_echo tool with the text PING-7, then reply with what it returned.",
        belle.bind("conversation-a"),
    )
    record(
        0,
        "provider tool turn",
        events=kinds(events),
        text=text(events)[:200],
        toolkit=belle.toolkit.calls,
        model_calls=kinds(events).count("token_usage"),
        results=tool_results(events),
    )


def wait_ready(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if request("GET", "/ping", timeout=5)[0] == 200:
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
        time.sleep(0.5)


wait_ready()
{"main": main, "restarted": restarted, "provider": provider}[stage]()
