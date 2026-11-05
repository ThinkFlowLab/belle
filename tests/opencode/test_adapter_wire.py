"""The adapter against a scripted OpenCode wire. No container and no network."""

import asyncio
import itertools
import json
import logging
from typing import Any

import httpx
import pytest

from belle_agent import hook_server
from belle_agent.backend import (
    APPROVAL,
    CONVERSATION_UNAVAILABLE,
    DUPLICATE,
    EVENT_ACCEPTED,
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_STEP,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    EVENT_USAGE,
    OP_ACTIVE,
    OP_ANSWER,
    OP_ASK,
    OP_CANCEL,
    OP_CATALOG,
    OP_CONTINUE,
    OP_FORGET,
    OP_HEALTH,
    OP_PEEK_HISTORY,
    OP_PENDING,
    OP_RECOVER,
    OP_REFUSE_QUESTION,
    RECOVERY_NOT_SUBMITTED,
    RECOVERY_RUNNING,
    RECOVERY_TERMINAL,
    Answer,
    BackendError,
    Question,
    TurnRequest,
    TurnScope,
)
from belle_agent.config import BelleConfigError, load_config
from belle_agent.host import host_from_config
from belle_agent.integrations.opencode import OpenCodeBackend, OpenCodeError, adapter, wire
from belle_agent.integrations.opencode.permissions import BUILTIN_ACTIONS, TOOL_LEVELS, permission_rules
from belle_agent.integrations.opencode.pin import OPENCODE_VERSION
from belle_agent.integrations.opencode.sessions import message_id, session_id
from belle_agent.integrations.opencode.settings import create_backend
from belle_agent.platforms.base import InboundMessage
from belle_agent.tools.content import ContentToolkit
from belle_agent.tools.host import BoundTurn, ToolHost
from belle_agent.tools.server import PATH

KEY = "sk-the-provider-key"
C1 = session_id("", "c1")


IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
BUILTIN_PLUGIN = {"id": "opencode.session", "source": {"type": "builtin"}}
RELAY_PLUGIN = {"id": "belle.relay", "source": {"type": "project"}}


class FakeOpenCode:
    """Answers the calls the adapter makes and streams the events a test emits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.stream: asyncio.Queue[dict | None] = asyncio.Queue()
        self.version = OPENCODE_VERSION
        #: Answers of `GET /api/model/default` that come before the catalog is built.
        self.not_ready = 0
        #: The kinds of input the default model takes, as the server describes it. Nothing: it does not say.
        self.model_takes: list[str] | None = None
        self.model_inputs: dict[str, list[str]] = {}
        self.prompt_status = 200
        #: What runs before a prompt is answered, as the relay plugin's hooks do. Nothing runs by default.
        self.before_prompt = None
        self.running: set[str] = set()
        #: The configuration of each `PUT /api/experimental/mcp/belle`, in order.
        self.mounts: list[dict] = []
        self.mount_present = True
        self.mount_status = {"status": "connected"}
        #: What a failed mount becomes when OpenCode is asked to connect again. Nothing leaves it as it is.
        self.status_after_connect: dict | None = None
        #: The plugins GET /api/plugin lists once they are loaded. A real server always names its own.
        self.plugins: list[dict] = [BUILTIN_PLUGIN]
        #: How many times GET /api/plugin answers an empty list first, as a server that has not loaded them does.
        self.plugins_unloaded = 0
        #: Whether an interrupted execution reports its end at once.
        self.reports_interrupt = True
        #: Child session id to the session that spawned it.
        self.parents: dict[str, str] = {}
        #: Session id to the metadata it was created with.
        self.sessions: dict[str, dict] = {}
        #: Session id to its stored messages, oldest first.
        self.messages: dict[str, list[dict]] = {}
        #: Session id to its pending permission requests.
        self.permissions: dict[str, list[dict]] = {}
        #: Session id to the permission ruleset the adapter last set for a turn.
        self.permission_rules: dict[str, list[dict]] = {}
        #: Session id to its open forms, and the replies the adapter sent.
        self.forms: dict[str, list[dict]] = {}
        self.form_replies: list[tuple[str, Any]] = []
        self.permission_replies: list[tuple[str, str | None]] = []
        #: The message of each permission reply that carried one, and each cancelled form with its message.
        self.permission_messages: list[tuple[str, str]] = []
        self.forms_cancelled: list[tuple[str, str | None]] = []
        #: Session id to the model it was last switched to. A session that never switched reports none.
        self.models: dict[str, dict] = {}
        self.unmounts = 0
        #: Seconds between the comment lines the stream sends while no event comes. Nothing: it sends none.
        self.heartbeat: float | None = None
        self._clock = iter(range(1, 10_000))

    def paths(self, method: str = "POST") -> list[str]:
        return [path for verb, path, _ in self.calls if verb == method]

    def emit(self, kind: str, session: str = C1, **data) -> None:
        self.stream.put_nowait({"type": kind, "data": {"sessionID": session, **data}})

    def report(self, child: str, state: str = "completed", text: str = "done", parent: str = C1) -> None:
        """End a child session the way OpenCode tells its parent: a message of its own making in the parent's inbox."""
        self.running.discard(child)
        metadata = {"source": "subagent", "childID": child, "agent": "General", "state": state}
        body = f'<subagent sessionID="{child}" state="{state}" description="d">\n{text}\n</subagent>'
        self.store("synthetic", parent, id=f"msg_report_{child}", metadata=metadata, text=body)
        item = {"type": "synthetic", "payload": {"text": body, "description": "d", "metadata": metadata}, "delivery": "steer"}
        self.emit("session.inbox.enqueued", parent, inboxID=f"msg_report_{child}", item=item)

    def wake(self, child: str, parent: str = C1) -> None:
        """Start the execution OpenCode runs in an idle parent for a child's report."""
        self.running.add(parent)
        self.emit("session.execution.started", parent)
        self.emit("session.inbox.delivered", parent, inboxID=f"msg_report_{child}")

    def drop_stream(self) -> None:
        self.stream.put_nowait(None)

    def store(self, kind: str, session: str = C1, **fields) -> None:
        self.messages.setdefault(session, []).append({"type": kind, "time": {"created": next(self._clock)}, **fields})

    def end(self, outcome: str, session: str = C1, **assistant) -> None:
        """End a session's execution where no event says so: in its stored messages only.

        The error of a failed step is also the error of the execution, which the ``idle`` message stores.
        """
        self.store("assistant", session, **assistant)
        idle = {"error": assistant["error"]} if "error" in assistant else {}
        self.store("idle", session, outcome=outcome, **idle)
        self.running.discard(session)

    async def _events(self):
        yield b": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(self.stream.get(), self.heartbeat)
            except TimeoutError:
                yield b": heartbeat\n\n"
                continue
            if event is None:
                return
            yield b"data: " + json.dumps(event).encode() + b"\n\n"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/api/model/default":
            self.not_ready -= 1
            # The real answer carries the key in its settings.
            ready = {"data": {"id": "m", "providerID": "configured", "settings": {"apiKey": KEY}}}
            if self.model_takes is not None:
                ready["data"]["capabilities"] = {"tools": True, "input": self.model_takes, "output": ["text"]}
            return httpx.Response(200, json=ready if self.not_ready < 0 else {"settings": {"apiKey": KEY}})
        if path == "/api/model":
            models = [
                {"providerID": name.split("/", 1)[0], "id": name.split("/", 1)[1],
                 "capabilities": {"input": kinds}}
                for name, kinds in self.model_inputs.items()
            ]
            return httpx.Response(200, json={"data": models})
        if path == "/api/info":
            return httpx.Response(200, json={"version": self.version})
        if path == "/api/plugin":
            self.plugins_unloaded -= 1
            return httpx.Response(200, json={"data": self.plugins if self.plugins_unloaded < 0 else []})
        if path == "/api/event":
            return httpx.Response(200, content=self._events())
        if path == "/api/experimental/mcp/belle/connect":
            self.mount_status = self.status_after_connect or self.mount_status
            return httpx.Response(204)
        if path == "/api/experimental/mcp/belle":
            if request.method == "DELETE":
                self.unmounts += 1
                self.mount_present = False
            else:
                self.mounts.append(body["config"])
                self.mount_present = True
            return httpx.Response(204)
        if path == "/api/mcp":
            servers = [{"name": "belle", "status": self.mount_status}] if self.mount_present else []
            return httpx.Response(200, json={"data": servers})
        if path == "/api/session":
            self.sessions.setdefault(body["id"], body.get("metadata") or {})
            model = {"model": self.models[body["id"]]} if body["id"] in self.models else {}
            return httpx.Response(200, json={"data": {"id": body["id"], "cost": 2.0, **model}})
        if path == "/api/session/active":
            return httpx.Response(200, json={"data": {session: {"type": "running"} for session in self.running}})
        if "/instructions/entries/" in path:
            return httpx.Response(204)
        session = path.split("/")[3]
        stored = self.messages.get(session, [])
        if request.method == "PATCH":  # the turn's permission ruleset
            self.permission_rules[session] = (body or {}).get("permissions") or []
            return httpx.Response(200, json={"data": {"id": session, "metadata": self.sessions.get(session, {})}})
        if path.endswith("/form") and request.method == "POST":
            form = {"id": f"frm_{len(self.forms)}", "sessionID": session, **(body or {})}
            self.forms.setdefault(session, []).append(form)
            self.emit("form.created", session, form=form)
            return httpx.Response(200, json={"data": {"id": form["id"]}})
        if "/form/" in path and request.method == "DELETE":
            form_id = path.split("/")[5]
            self.forms[session] = [form for form in self.forms.get(session, []) if form["id"] != form_id]
            self.forms_cancelled.append((form_id, request.url.params.get("message")))
            return httpx.Response(204)
        if "/form/" in path and path.endswith("/reply"):
            form_id = path.split("/")[5]
            self.forms[session] = [form for form in self.forms.get(session, []) if form["id"] != form_id]
            self.form_replies.append((form_id, body))
            return httpx.Response(204)
        if "/permission/" in path and path.endswith("/reply"):
            self.permission_replies.append((path.split("/")[5], (body or {}).get("decision")))
            if "message" in (body or {}):
                self.permission_messages.append((path.split("/")[5], body["message"]))
            return httpx.Response(204)
        if path.endswith("/model") and request.method == "POST":
            self.models[session] = body["model"]
            return httpx.Response(200, json={"data": {"id": session}})
        if path.endswith("/message"):
            messages = stored[::-1]
            limit = int(request.url.params.get("limit", "50"))
            if not 1 <= limit <= 200 or ("cursor" in request.url.params and "order" in request.url.params):
                return httpx.Response(400, json={"message": "invalid message page"})
            offset = int(request.url.params.get("cursor", "0"))
            page = messages[offset:offset + limit]
            return httpx.Response(200, json={"data": page, "cursor": {"next": str(offset + len(page)) if page else None}})
        if "/message/" in path:
            found = [message for message in stored if message.get("id") == path.rsplit("/", 1)[-1]]
            return httpx.Response(200, json={"data": found[0]}) if found else httpx.Response(404, json={})
        if path.endswith("/permission"):
            return httpx.Response(200, json={"data": self.permissions.get(session, [])})
        if path.endswith("/form"):
            return httpx.Response(200, json={"data": self.forms.get(session, [])})
        if request.method == "DELETE":
            if self.sessions.pop(session, None) is None:
                return httpx.Response(404, json={"message": "no such session"})
            return httpx.Response(204)
        if request.method == "GET":
            if session in self.sessions:
                return httpx.Response(200, json={"data": {"id": session, "metadata": self.sessions[session]}})
            if session not in self.parents:
                return httpx.Response(404, json={"message": "no such session"})
            return httpx.Response(200, json={"data": {"id": session, "parentID": self.parents[session]}})
        if path.endswith("/prompt"):
            if self.before_prompt is not None:
                await self.before_prompt(body)
            if self.prompt_status != 200:
                return httpx.Response(self.prompt_status, json={"message": "refused"})
            # A prompt with a known message id is answered 200 and runs nothing.
            if not any(message.get("id") == body["id"] for message in stored):
                self.store("user", session, id=body["id"])
                self.running.add(session)
                self.emit("session.inbox.delivered", session, inboxID=body["id"])
            return httpx.Response(200, json={"data": {"id": body["id"]}})
        if path.endswith("/interrupt"):
            interrupted = session in self.running
            self.running.discard(session)
            if interrupted and self.reports_interrupt:
                self.emit("session.execution.interrupted", session, reason="user")
                if session in self.parents:
                    # A child that is stopped says so to its parent, and OpenCode runs the parent for that report.
                    self.report(session, "cancelled", "Subagent cancelled", self.parents[session])
                    self.wake(session, self.parents[session])
            return httpx.Response(200, json={"interrupted": interrupted})
        return httpx.Response(404, json={"message": "no such route"})


_REQUESTS: list = []


@pytest.fixture
def opencode(monkeypatch) -> FakeOpenCode:
    _REQUESTS.append(itertools.count(1))
    monkeypatch.setattr(adapter, "READY_POLL_SECONDS", 0)
    monkeypatch.setattr(adapter, "RECONNECT_SECONDS", 0)
    monkeypatch.setattr(adapter, "MOUNT_SETTLE_SECONDS", 0)
    return FakeOpenCode()


@pytest.fixture
async def backend(opencode):
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    yield backend
    await backend.close()


async def _accepted(backend, conversation: str = "c1", **fields):
    """Start a turn and return its stream once the prompt is in. The requests of one test are r1, r2 and so on."""
    request_id = f"r{next(_REQUESTS[-1])}"
    stream = backend.run_turn(TurnRequest(conversation=conversation, request_id=request_id, text="hello", **fields))
    assert (await anext(stream)).kind == EVENT_ACCEPTED
    return stream


def translate_cost(backend, opencode) -> float:
    """The cost the adapter reports for the latest usage event of a session whose earlier cost was 2.0."""
    turn = adapter._Turn(conversation="c1", prompt_id="p", delivered=True, cost_before=2.0)
    return adapter.translate(opencode.stream.get_nowait(), turn).data["cost"]


def test_usage_tokens_are_session_totals_minus_turn_baseline(opencode):
    turn = adapter._Turn(
        conversation="c1", prompt_id="p", delivered=True,
        cost_before=2.0, tokens_in_before=40, tokens_out_before=10,
    )
    opencode.emit("session.usage.updated", cost=2.5, tokens={"input": 55, "output": 18})
    event = adapter.translate(opencode.stream.get_nowait(), turn)
    assert event.kind == EVENT_USAGE
    assert event.data["cost"] == 0.5
    assert event.data["tokens"] == {"input": 15, "output": 8}


async def _rest(stream) -> list:
    return [event async for event in stream]


@pytest.mark.parametrize(
    ("takes", "content"),
    [
        (["text", "image"], IMAGES),
        # OpenCode saves audio to a file on its server and hands the model the path, so no audio is passed.
        (["text", "image", "pdf", "audio", "a kind not known here"], IMAGES | {"application/pdf"}),
        (["text"], set()),
        # A server that does not describe its model: nothing is assumed of it.
        (None, set()),
    ],
)
async def test_the_content_a_tool_result_may_carry_follows_what_the_model_takes(opencode, takes, content):
    opencode.model_takes = takes
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))

    # Not known before the server has been asked.
    assert backend.capabilities().tool_content == frozenset()
    await backend.start(ToolHost([]))
    try:
        assert backend.capabilities().tool_content == content
    finally:
        await backend.close()


async def test_selected_models_use_their_own_content_types(opencode):
    opencode.model_takes = ["text", "image"]
    opencode.model_inputs = {
        "configured/text-only": ["text"],
        "configured/other-image": ["text", "image"],
    }
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    try:
        capabilities = backend.capabilities()
        assert capabilities.content_for("") == IMAGES
        assert capabilities.content_for("configured/m") == IMAGES
        assert capabilities.content_for("configured/text-only") == frozenset()
        assert capabilities.content_for("configured/other-image") == IMAGES
        assert capabilities.content_for("configured/not-declared") == frozenset()
        opencode.model_takes = ["text"]
        await backend._ready()  # A reconnect reads the server's current model catalog.
        assert backend.capabilities().content_for("") == frozenset()
    finally:
        await backend.close()


async def test_host_binds_each_conversation_to_its_selected_models_content(opencode, tmp_path):
    opencode.model_takes = ["text", "image"]
    opencode.model_inputs = {"configured/text-only": ["text"], "configured/other-image": ["text", "image"]}
    path = tmp_path / "config.yaml"
    path.write_text("""
platforms: {loopback: {enabled: true}}
scopes:
  - match: {channel: loopback, chat: C2}
    agent: {model_name: configured/text-only}
  - match: {channel: loopback, chat: C3}
    agent: {model_name: configured/other-image}
""")
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    host = host_from_config(load_config(path), backend, state_dir=tmp_path)
    await host.start()
    try:
        def types(chat: str) -> frozenset[str]:
            inbound = InboundMessage("loopback", "W1", chat, f"m-{chat}", "U1", "show image")
            return host._turn(inbound, chat, {}, reply_optional=False).bound.content_types

        assert (types("C1"), types("C2"), types("C3")) == (IMAGES, frozenset(), IMAGES)
    finally:
        await host.stop()


async def test_no_turn_starts_before_the_model_catalog_is_ready(opencode, caplog):
    opencode.not_ready = 3
    caplog.set_level(logging.DEBUG)
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))

    await backend.start(ToolHost([]))
    try:
        # Three answers without a `data` object, then the one that has it, then everything else.
        assert [path for _, path, _ in opencode.calls[:6]] == ["/api/model/default"] * 4 + ["/api/model", "/api/info"]
        assert opencode.paths() == []
        # The answer holds the provider key. Nothing of it is logged.
        assert KEY not in caplog.text
    finally:
        await backend.close()


async def test_a_server_of_another_version_is_refused(opencode):
    opencode.version = "2.0.25"
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))

    with pytest.raises(OpenCodeError, match="2.0.25"):
        await backend.start(ToolHost([]))
    await backend.close()


async def test_a_turn_is_one_session_one_prompt_and_events_that_end_once(opencode, backend):
    stream = await _accepted(backend)
    step = {"assistantMessageID": "msg_1"}
    opencode.emit("session.execution.started")
    opencode.emit("session.step.started", **step)
    opencode.emit("session.text.delta", delta="let me ", **step)
    opencode.emit("session.text.ended", text="let me look", **step)
    opencode.emit("session.tool.input.started", id="call_1", name="belle_post_message")
    opencode.emit("session.tool.called", id="call_1", input={"text": "hi"}, executed=False)
    opencode.emit("session.tool.success", id="call_1", content=[{"type": "text", "text": "posted"}])
    opencode.emit("session.tool.input.started", id="call_2", name="shell")
    opencode.emit("session.tool.called", id="call_2", input={})
    opencode.emit("session.tool.failed", id="call_2", error={"type": "tool.execution", "message": "no"})
    opencode.emit("session.usage.updated", cost=0, tokens={"input": 3, "output": 1})
    # An event of another session that is not a routed child of this turn is dropped.
    opencode.emit("session.text.ended", "ses_unrelated", text="other session text")
    opencode.emit("session.step.started", **step)
    opencode.emit("session.text.ended", text="the final", **step)
    opencode.emit("session.execution.succeeded")

    events = await _rest(stream)

    assert [(event.kind, event.text) for event in events] == [
        (EVENT_STEP, ""),
        (EVENT_TEXT_DELTA, "let me "),
        (EVENT_TOOL_CALLED, ""),
        (EVENT_TOOL_RESULT, "posted"),
        (EVENT_TOOL_CALLED, ""),
        (EVENT_TOOL_RESULT, "no"),
        (EVENT_USAGE, ""),
        (EVENT_STEP, ""),
        # The final is the text of the last step, not of the steps before a tool call.
        (EVENT_SUCCEEDED, "the final"),
    ]
    assert events[2].data == {"name": "belle_post_message", "call_id": "call_1", "arguments": {"text": "hi"}}
    assert events[5].data == {"name": "shell", "call_id": "call_2", "failed": True}
    # OpenCode reports what the session has cost. The turn's share is what it added.
    opencode.emit("session.usage.updated", cost=2.5, tokens={})
    assert translate_cost(backend, opencode) == 0.5
    assert [(method, path, body) for method, path, body in opencode.calls if method == "POST"][-2:] == [
        ("POST", "/api/session", {"id": C1, "metadata": {"belle": {"conversation": "c1"}}}),
        ("POST", f"/api/session/{C1}/prompt", {"id": message_id("", "c1", "r1"), "text": "hello"}),
    ]


async def test_a_failed_execution_is_one_failed_event_without_the_provider_body(opencode, backend):
    stream = await _accepted(backend)
    error = {"type": "provider.invalid-request", "message": "bad request", "status": 400, "response": {"body": KEY}}
    opencode.emit("session.step.failed", error=error)
    opencode.emit("session.execution.failed", error=error)

    (failed,) = await _rest(stream)

    assert (failed.kind, failed.text) == (EVENT_FAILED, "bad request")
    # OpenCode's error type and session lead to its own record of the failure; its message stays out.
    assert failed.data == {
        "failure_kind": "provider", "status": 400, "error_type": "provider.invalid-request", "backend_session": C1,
    }
    # OpenCode ended the execution itself, so nothing is left to interrupt.
    assert not any(path.endswith("/interrupt") for path in opencode.paths())


CONTINUE = "Your last response hit the output token limit (stop reason: length)."
# The failure of a tool call that a step cut at the output limit. OpenCode does not run the call.
INCOMPLETE = {"type": "tool.input-incomplete", "message": "The tool call was not executed because the output token limit"}
# A tool that the provider runs itself. OpenCode marks its call `executed`.
HOSTED = "web_search"
# OpenCode's prompt to go on after a step whose response stopped after it started.
CONTINUE_BROKEN = (
    "The previous response was interrupted. Continue from where you left off without repeating completed content."
)
# How OpenCode fails such a step: a stream that ended with no finish, and a finish that the provider did not name.
BROKEN = {
    "no-finish": {"type": "provider.invalid-output", "message": "OpenAI Chat stream ended without finish_reason"},
    "unknown": {
        "type": "provider.invalid-output", "message": "The provider response ended with an unknown finish reason.",
    },
}
# Steps as (text, finish, the tool the step calls or ""), and the final OpenCode's run of them gives. A finish
# that ``BROKEN`` names fails the step, and OpenCode continues it.
STEPS = {
    # A cut step that called no tool is continued by the next step.
    "continued": ([("the quick br", "length", ""), ("own fox", "stop", "")], "the quick brown fox"),
    # A cut step that called a tool is a tool-calls step: the next step answers the failure of the call.
    "cut-tool-call": ([("I will write the file.", "length", "write"), ("Done in pieces.", "stop", "")], "Done in pieces."),
    # A cut text and the tool-calls step that continues it are one step, and tell of the call.
    "cut-then-tool-calls": (
        [("The answer is forty", "length", ""), ("-two. Let me check.", "tool-calls", "read"), ("Confirmed.", "stop", "")],
        "Confirmed.",
    ),
    # The provider ran the tool and answered in the same step.
    "hosted-tool": ([("Paris is the capital.", "stop", HOSTED)], "Paris is the capital."),
    # OpenCode continues a cut step whose only call the provider ran.
    "cut-hosted-tool": ([("Paris is", "length", HOSTED), (" the capital.", "stop", "")], "Paris is the capital."),
    # A step whose stream broke is continued by the next step.
    "broken": (
        [("The review is on Thursday. Bring the Q3 fig", "no-finish", ""), ("ures.", "stop", "")],
        "The review is on Thursday. Bring the Q3 figures.",
    ),
    "unknown-finish": ([("forty", "unknown", ""), ("-two", "stop", "")], "forty-two"),
    # A broken text and a cut that continues it run on into the step that continues both.
    "broken-then-cut": ([("one ", "no-finish", ""), ("two ", "length", ""), ("three", "stop", "")], "one two three"),
    # A broken step that called a tool is a tool-calls step.
    "broken-tool-call": ([("I will read it.", "no-finish", "read"), ("Done.", "stop", "")], "Done."),
    "broken-hosted-tool": ([("Paris is", "no-finish", HOSTED), (" the capital.", "stop", "")], "Paris is the capital."),
}


def _emit_steps(opencode, steps) -> None:
    """Emit steps as OpenCode 2.1.4 does.

    A tool call of a cut or broken step fails before it runs. A cut step that called no tool that OpenCode runs is
    followed by OpenCode's prompt to go on. A broken step fails, and OpenCode waits to retry it and then prompts.
    """
    for index, (text, finish, tool) in enumerate(steps):
        step = {"assistantMessageID": f"msg_{index}"}
        opencode.emit("session.step.started", **step)
        opencode.emit("session.text.delta", delta=text, **step)
        opencode.emit("session.text.ended", text=text, **step)
        if tool:
            call = {"id": f"call_{index}", **step}
            opencode.emit("session.tool.input.started", name=tool, **call)
            if finish == "length" or finish in BROKEN:
                opencode.emit("session.tool.failed", error=INCOMPLETE, executed=tool == HOSTED, **call)
            else:
                opencode.emit("session.tool.called", input={}, executed=tool == HOSTED, **call)
        if finish in BROKEN:
            opencode.emit("session.step.failed", error=BROKEN[finish], **step)
            opencode.emit("session.retry.scheduled", attempt=2, at=0, error=BROKEN[finish], **step)
            opencode.emit("session.synthetic", text=CONTINUE_BROKEN)
            continue
        opencode.emit("session.step.ended", finish=finish, cost=0, tokens={}, **step)
        if finish == "length" and tool in ("", HOSTED):
            opencode.emit("session.synthetic", text=CONTINUE)


def _stored_steps(steps) -> list[dict]:
    """The same steps as OpenCode 2.1.4 stores them."""
    messages = []
    for index, (text, finish, tool) in enumerate(steps):
        called = [{"type": "tool", "id": f"call_{index}", "name": tool, "executed": tool == HOSTED}] if tool else []
        content = [{"type": "text", "text": text}, *called]
        if finish in BROKEN:
            # The failed step keeps its retry after the next step starts.
            retry = {"attempt": 2, "error": BROKEN[finish]}
            messages.append(
                {"type": "assistant", "finish": "error", "error": BROKEN[finish], "retry": retry, "content": content}
            )
            messages.append({"type": "synthetic", "text": CONTINUE_BROKEN})
            continue
        messages.append({"type": "assistant", "finish": finish, "content": content})
        if finish == "length" and tool in ("", HOSTED):
            messages.append({"type": "synthetic", "text": CONTINUE})
    return messages


@pytest.mark.parametrize(("steps", "final"), STEPS.values(), ids=STEPS)
async def test_a_step_cut_at_the_output_limit_ends_as_the_step_that_continues_it(opencode, backend, steps, final):
    stream = await _accepted(backend)
    _emit_steps(opencode, steps)
    opencode.emit("session.execution.succeeded")

    events = await _rest(stream)

    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, final)


@pytest.mark.parametrize(("steps", "final"), STEPS.values(), ids=STEPS)
def test_stored_steps_cut_at_the_output_limit_are_read_as_they_ran(steps, final):
    ended = adapter.stored(_stored_steps(steps), "succeeded")

    assert (ended.kind, ended.text) == (EVENT_SUCCEEDED, final)


@pytest.mark.parametrize("name", ["continued", "broken"])
async def test_the_step_that_continues_a_cut_or_broken_reply_says_so(opencode, backend, name):
    (first, _, _), (second, _, _) = STEPS[name][0]
    stream = await _accepted(backend)
    _emit_steps(opencode, STEPS[name][0])
    opencode.emit("session.execution.succeeded")

    events = await _rest(stream)

    # A platform starts a new paragraph at a step event, unless the step continues the text before it.
    shown = [(event.kind, event.text or event.data) for event in events if event.kind in (EVENT_STEP, EVENT_TEXT_DELTA)]
    assert shown == [(EVENT_STEP, {}), (EVENT_TEXT_DELTA, first), (EVENT_STEP, {"continues": True}), (EVENT_TEXT_DELTA, second)]


async def test_a_step_cut_before_it_wrote_any_text_is_continued_by_the_next(opencode, backend):
    stream = await _accepted(backend)
    step = {"assistantMessageID": "msg_reasoning"}
    opencode.emit("session.step.started", **step)
    opencode.emit("session.step.ended", finish="length", cost=0, tokens={}, **step)
    opencode.emit("session.synthetic", text=CONTINUE)
    _emit_steps(opencode, [("Forty-two.", "stop", "")])
    opencode.emit("session.execution.succeeded")

    events = await _rest(stream)

    assert [event.data for event in events if event.kind == EVENT_STEP] == [{}, {"continues": True}]
    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "Forty-two.")


async def test_after_a_reconnect_a_running_turn_goes_on_from_the_steps_its_session_stored(opencode, backend):
    stream = await _accepted(backend)
    opencode.emit("session.step.started", assistantMessageID="msg_cut")
    assert (await anext(stream)).kind == EVENT_STEP
    # The step is cut while no event is read. The session stores it.
    for message in _stored_steps([("the quick br", "length", "")]):
        opencode.store(message.pop("type"), **message)
    opencode.drop_stream()
    reading = asyncio.create_task(_rest(stream))
    async with asyncio.timeout(5):
        while f"/api/session/{C1}/message" not in opencode.paths("GET"):
            await asyncio.sleep(0.01)
    _emit_steps(opencode, [("own fox", "stop", "")])
    opencode.store("assistant", finish="stop", content=[{"type": "text", "text": "own fox"}])
    opencode.store("idle", outcome="succeeded")
    opencode.emit("session.execution.succeeded")

    events = await reading

    # The step that follows continues the cut text, and says so.
    assert [event.data for event in events if event.kind == EVENT_STEP] == [{"continues": True}]
    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "the quick brown fox")


async def test_a_step_retried_from_its_start_is_a_new_step_and_not_a_continuation(opencode, backend):
    stream = await _accepted(backend)
    step = {"assistantMessageID": "msg_0"}
    opencode.emit("session.step.started", **step)
    opencode.emit("session.retry.scheduled", attempt=2, at=0, error=BROKEN["no-finish"], **step)
    _emit_steps(opencode, [("pong", "stop", "")])
    opencode.emit("session.execution.succeeded")

    events = await _rest(stream)

    assert [event.data for event in events if event.kind == EVENT_STEP] == [{}, {}]
    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "pong")


@pytest.mark.parametrize("between", ["model-switched", "agent-switched", "location-switched", "shell"])
async def test_a_reconnect_reads_a_broken_step_as_continued_when_other_messages_come_before_the_prompt(
    opencode, backend, between
):
    stream = await _accepted(backend)
    while not opencode.stream.empty():
        opencode.stream.get_nowait()
    failed, prompt, last = _stored_steps(STEPS["broken"][0])
    # OpenCode stores what happens in the session while it waits to retry the failed step, such as a model switch.
    for message in (failed, {"type": between}, prompt, last):
        opencode.store(message.pop("type"), **message)
    opencode.store("idle", outcome="succeeded")
    opencode.running.discard(C1)
    opencode.drop_stream()

    (ended,) = await _rest(stream)

    assert (ended.kind, ended.text) == (EVENT_SUCCEEDED, STEPS["broken"][1])


TRANSPORT = {"type": "provider.transport", "message": "The connection to the provider closed."}
STEP_INTERRUPTED = {"type": "aborted", "message": "Step interrupted"}
STOPPED = "The model's response stopped: The connection to the provider closed."


@pytest.mark.parametrize("retry", [{}, {"retry": {"attempt": 2, "error": TRANSPORT}}], ids=["interrupted", "waiting"])
def test_stored_steps_read_a_failed_step_that_the_next_step_follows_with_no_prompt_as_no_answer(retry):
    # OpenCode goes on with a new step and no prompt when it resumes a turn that a shutdown stopped, also when the
    # shutdown ended the wait for a retry.
    text = [{"type": "text", "text": "Hal"}]
    failed = {"type": "assistant", "finish": "error", "error": STEP_INTERRUPTED, **retry, "content": text}
    whole = {"type": "assistant", "finish": "stop", "content": [{"type": "text", "text": "Whole."}]}

    assert adapter.stored([failed, whole], "succeeded").text == "Whole."


@pytest.mark.parametrize(
    ("steps", "text", "tool", "final"),
    [
        ([], "two", "", f"two\n\n{STOPPED}"),
        ([("one ", "no-finish", "")], "two", "", f"one two\n\n{STOPPED}"),
        # The text of a step that called a tool tells of the call.
        ([], "I will read it.", "read", TRANSPORT["message"]),
        ([("Paris is", "stop", "")], "I will read it.", "read", f"Paris is\n\n{STOPPED}"),
        # A turn that wrote nothing fails with OpenCode's message alone.
        ([], "", "", TRANSPORT["message"]),
    ],
    ids=["first", "after-a-continuation", "tool-call", "tool-call-after-an-answer", "no-text"],
)
async def test_a_failed_step_that_opencode_does_not_continue_fails_the_turn_after_its_text(
    opencode, backend, steps, text, tool, final
):
    stream = await _accepted(backend)
    _emit_steps(opencode, steps)
    step = {"assistantMessageID": "msg_last"}
    opencode.emit("session.step.started", **step)
    opencode.emit("session.text.delta", delta=text, **step)
    opencode.emit("session.text.ended", text=text, **step)
    if tool:
        opencode.emit("session.tool.input.started", id="call_last", name=tool, **step)
        opencode.emit("session.tool.failed", id="call_last", error=TRANSPORT, executed=False, **step)
    opencode.emit("session.step.failed", error=TRANSPORT, **step)
    opencode.emit("session.execution.failed", error=TRANSPORT)

    events = await _rest(stream)

    assert (events[-1].kind, events[-1].text) == (EVENT_FAILED, final)
    called = [{"type": "tool", "id": "call_last", "name": tool, "executed": False}] if tool else []
    failed = {"type": "assistant", "finish": "error", "error": TRANSPORT, "content": [{"type": "text", "text": text}, *called]}
    idle = {"type": "idle", "outcome": "failed", "error": TRANSPORT}
    ended = adapter.stored([*_stored_steps(steps), failed, idle], "failed")
    assert (ended.kind, ended.text) == (EVENT_FAILED, final)


async def test_peek_history_reads_stored_steps_cut_at_the_output_limit_as_one_answer(opencode, backend):
    opencode.store("user", id=message_id("", "c1", "r-peek"))
    for message in _stored_steps(STEPS["continued"][0] + STEPS["cut-tool-call"][0] + STEPS["broken"][0]):
        opencode.store(message.pop("type"), **message)
    # A broken step that OpenCode waits to retry. It has no prompt to go on yet.
    waiting = {"finish": "error", "error": TRANSPORT, "retry": {"attempt": 2}}
    opencode.store("assistant", **waiting, content=[{"type": "text", "text": "Next"}])

    parts = await backend.peek_history("c1", "r-peek")

    assert [part.text for part in parts] == [
        "the quick brown fox", "Done in pieces.", "The review is on Thursday. Bring the Q3 figures.", "Next",
    ]


async def test_peek_history_reads_the_text_of_a_stored_step_that_failed_and_was_not_continued(opencode, backend):
    opencode.store("user", id=message_id("", "c1", "r-peek"))
    opencode.store("assistant", finish="error", error=TRANSPORT, content=[{"type": "text", "text": "Half an ans"}])

    parts = await backend.peek_history("c1", "r-peek")

    assert [part.text for part in parts] == ["Half an ans"]


CUT_THREE_TIMES = [("one ", "length", ""), ("two ", "length", ""), ("three", "length", "")]
# The error of an execution that OpenCode ends when no continuation is left. No step failed.
OUTPUT_LIMIT = {"type": "output-limit", "message": "Response still truncated after two output token limit continuations"}


async def test_an_answer_still_cut_after_the_continuations_fails_with_opencodes_reason(opencode, backend):
    stream = await _accepted(backend)
    _emit_steps(opencode, CUT_THREE_TIMES)
    opencode.emit("session.execution.failed", error=OUTPUT_LIMIT)

    events = await _rest(stream)

    assert (events[-1].kind, events[-1].text) == (EVENT_FAILED, f"one two three\n\nThe model's response stopped: {OUTPUT_LIMIT['message']}.")
    assert events[-1].data == {"failure_kind": "backend", "status": None, "error_type": "output-limit", "backend_session": C1}


async def test_a_reconnect_reads_the_reason_of_an_answer_still_cut_from_the_end_of_the_execution(opencode, backend):
    stream = await _accepted(backend)
    while not opencode.stream.empty():
        opencode.stream.get_nowait()
    for message in _stored_steps(CUT_THREE_TIMES):
        opencode.store(message.pop("type"), **message)
    opencode.store("idle", outcome="failed", error=OUTPUT_LIMIT)
    opencode.running.discard(C1)
    opencode.drop_stream()

    (ended,) = await _rest(stream)

    assert (ended.kind, ended.text) == (EVENT_FAILED, f"one two three\n\nThe model's response stopped: {OUTPUT_LIMIT['message']}.")
    assert (ended.data["error_type"], ended.data["backend_session"]) == ("output-limit", C1)


async def test_ids_are_derived_so_a_restart_finds_the_same_session():
    assert session_id("", "c1") == session_id("", "c1") != session_id("", "c2")
    assert session_id("a", "c1") != session_id("b", "c1")
    assert message_id("", "c1", "r1") != message_id("", "c1", "r2")
    # The forms OpenCode accepts: `ses...` and `msg_...`.
    assert session_id("", "loopback_W1_D-U1_U1").startswith("ses_")
    assert message_id("", "c1", "e1").startswith("msg_")


async def test_a_turns_system_additions_become_the_sessions_instruction_entry(opencode, backend):
    entry = f"/api/experimental/session/{C1}/instructions/entries/belle"
    assert backend.capabilities().scope_fields == {
        "system_additions", "tools", "skills_required", "skills_available", "skills_unavailable", "model"
    }

    stream = await _accepted(backend, scope=TurnScope(system_additions=("Answer in French.", "Post with the tool.")))
    opencode.emit("session.execution.succeeded")
    await _rest(stream)
    stream = await _accepted(backend)
    opencode.emit("session.execution.succeeded")
    await _rest(stream)

    # Set before the prompt of the turn that has additions, removed before the prompt of the one that has none.
    calls = [call for call in opencode.calls if call[0] != "GET" and (call[1] == entry or call[1].endswith("/prompt"))]
    assert [(method, path.rsplit("/", 1)[-1]) for method, path, _ in calls] == [
        ("PUT", "belle"),
        ("POST", "prompt"),
        ("DELETE", "belle"),
        ("POST", "prompt"),
    ]
    assert calls[0][2] == {"value": "Answer in French.\n\nPost with the tool."}


async def test_the_host_warns_for_what_the_adapter_does_not_apply_and_not_for_the_prompt_additions(
    opencode, tmp_path, caplog
):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
platforms: {loopback: {enabled: true}}
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], reply: optional, prompt: Answer in French.}
    agent: {model_name: small-model, expert: translator}
""",
        encoding="utf-8",
    )
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    host = host_from_config(load_config(path), backend, state_dir=tmp_path)

    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await host.start()
    await host.stop()

    # model_name and the prompt additions are applied now; only expert is not, so only it is warned about.
    warned = [record.getMessage() for record in caplog.records if "enforces nothing" in record.getMessage()]
    assert len(warned) == 1 and "agent.expert is set" in warned[0], warned


async def test_a_second_prompt_for_a_running_conversation_is_refused_not_joined(opencode, backend):
    first = await _accepted(backend)

    refused = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r2", text="again")))

    assert [(event.kind, event.text) for event in refused] == [(EVENT_FAILED, adapter.ALREADY_RUNNING)]
    # One prompt reached OpenCode, and the refusal did not interrupt the running turn.
    assert opencode.paths() == ["/api/session", f"/api/session/{C1}/prompt"]

    # Control: another conversation runs beside it, and the first turn still ends as its own.
    other = await _accepted(backend, "c2")
    opencode.emit("session.execution.succeeded", session_id("", "c2"))
    opencode.emit("session.execution.succeeded")
    assert [event.kind for event in await _rest(other)] == [EVENT_SUCCEEDED]
    assert [event.kind for event in await _rest(first)] == [EVENT_SUCCEEDED]


async def test_a_reader_that_leaves_stops_the_turn_and_frees_the_conversation(opencode, backend):
    stream = await _accepted(backend)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await anext(stream)

    assert opencode.paths()[-1] == f"/api/session/{C1}/interrupt"
    # The conversation runs again once OpenCode has reported the stop.
    again = await _accepted(backend)
    opencode.emit("session.execution.succeeded")
    assert [event.kind for event in await _rest(again)] == [EVENT_SUCCEEDED]


async def test_a_reader_that_leaves_as_the_host_stops_leaves_the_turn_running(opencode, backend):
    stream = await _accepted(backend)
    other = await _accepted(backend, "c2")
    backend.shutting_down()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await anext(stream)

    assert "/interrupt" not in " ".join(opencode.paths())
    # A stop of a turn still interrupts it.
    assert await backend.cancel("c2") is True
    assert opencode.paths()[-1] == f"/api/session/{session_id('', 'c2')}/interrupt"
    assert [event.kind for event in await _rest(other)] == [EVENT_INTERRUPTED]


async def test_cancel_interrupts_the_running_turn_only(opencode, backend):
    assert backend.capabilities().operations == {OP_CANCEL, OP_ACTIVE, OP_RECOVER, OP_PENDING, OP_ASK, OP_ANSWER,
                                                 OP_REFUSE_QUESTION, OP_CATALOG, OP_CONTINUE, OP_FORGET,
                                                 OP_PEEK_HISTORY, OP_HEALTH}
    assert backend.capabilities().scheduler == ""
    assert backend.capabilities().tool_result_max_chars == 0
    assert await backend.cancel("c1") is False

    stream = await _accepted(backend)
    assert await backend.cancel("c1") is True
    assert [event.kind for event in await _rest(stream)] == [EVENT_INTERRUPTED]


async def test_a_turn_that_still_runs_after_a_lost_event_stream_is_resumed(opencode, backend):
    stream = await _accepted(backend)
    opencode.emit("session.step.started")
    opencode.drop_stream()
    assert (await anext(stream)).kind == EVENT_STEP
    # The adapter subscribes again, reads the session and finds the turn running.
    reading = asyncio.create_task(_rest(stream))
    async with asyncio.timeout(5):
        while f"/api/session/{C1}/message" not in opencode.paths("GET"):
            await asyncio.sleep(0.01)
    # The step's text ended while nothing was read. The session has it.
    opencode.store("assistant", content=[{"type": "text", "text": "read from the session"}])
    opencode.store("idle", outcome="succeeded")
    opencode.emit("session.execution.succeeded")

    assert [(event.kind, event.text) for event in await reading] == [(EVENT_SUCCEEDED, "read from the session")]
    # The turn was running when the stream came back, so nothing stopped it.
    assert not any(path.endswith("/interrupt") for path in opencode.paths())


async def test_a_stalled_event_stream_is_opened_again_and_its_turn_ends_as_its_session_says(opencode, monkeypatch):
    monkeypatch.setattr(wire, "STREAM_IDLE_SECONDS", 0.2)
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    try:
        stream = await _accepted(backend)
        # The connection is dead: nothing more is read, and nothing says so.
        while not opencode.stream.empty():
            opencode.stream.get_nowait()
        opencode.end("succeeded", content=[{"type": "text", "text": "done unseen"}])

        async with asyncio.timeout(2):
            (ended,) = await _rest(stream)

        assert (ended.kind, ended.text) == (EVENT_SUCCEEDED, "done unseen")
        assert opencode.paths("GET").count("/api/event") == 2
    finally:
        await backend.close()


async def test_a_quiet_event_stream_that_sends_heartbeats_is_kept(opencode, monkeypatch):
    monkeypatch.setattr(wire, "STREAM_IDLE_SECONDS", 0.2)
    opencode.heartbeat = 0.05
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    try:
        await asyncio.sleep(0.6)

        assert opencode.paths("GET").count("/api/event") == 1
    finally:
        await backend.close()


async def test_reconnect_reads_a_turn_longer_than_the_message_limit(opencode, backend):
    stream = await _accepted(backend)
    while not opencode.stream.empty():
        opencode.stream.get_nowait()
    for index in range(230):
        opencode.store("assistant", content=[{"type": "text", "text": f"answer {index}"}], finish="stop")
    opencode.store("idle", outcome="succeeded")
    opencode.running.discard(C1)
    opencode.drop_stream()

    (ended,) = await _rest(stream)

    assert ended.kind == EVENT_SUCCEEDED
    assert ended.text == "\n\n".join(f"answer {index}" for index in range(230))


async def test_reconnect_waits_when_a_new_execution_is_active_after_an_idle(opencode, backend):
    stream = await _accepted(backend)
    opencode.store("assistant", content=[{"type": "text", "text": "first answer"}], finish="stop")
    opencode.store("idle", outcome="succeeded")

    assert await backend._reconcile(C1, backend._turns[C1]) is None
    opencode.running.discard(C1)
    opencode.emit("session.execution.succeeded")
    (ended,) = await _rest(stream)

    assert (ended.kind, ended.text) == (EVENT_SUCCEEDED, "first answer")


@pytest.mark.parametrize(
    ("outcome", "assistant", "expected"),
    [
        ("succeeded", {"content": [{"type": "text", "text": "done unseen"}]}, (EVENT_SUCCEEDED, "done unseen")),
        ("interrupted", {"content": [{"type": "text", "text": "half"}]}, (EVENT_INTERRUPTED, "")),
        ("failed", {"error": {"type": "provider.internal", "message": "broke", "response": KEY}}, (EVENT_FAILED, "broke")),
    ],
)
async def test_a_turn_that_ended_while_no_event_was_read_ends_once_as_its_session_says(
    opencode, backend, outcome, assistant, expected
):
    stream = await _accepted(backend)
    # The delivery of the prompt is lost too: nothing of this turn has been read.
    while not opencode.stream.empty():
        opencode.stream.get_nowait()
    opencode.end(outcome, **assistant)
    opencode.drop_stream()

    (ended,) = await _rest(stream)

    assert (ended.kind, ended.text) == expected
    assert KEY not in str(ended)
    if outcome == "failed":
        # Read from the session's store, the failure names OpenCode's error type and session as a live one does.
        assert (ended.data["error_type"], ended.data["backend_session"]) == ("provider.internal", C1)
    assert not any(path.endswith("/interrupt") for path in opencode.paths())
    # Control: the conversation's next turn runs and ends as its own.
    again = backend.run_turn(TurnRequest(conversation="c1", request_id="r2", text="again"))
    assert (await anext(again)).kind == EVENT_ACCEPTED
    opencode.emit("session.execution.succeeded")
    assert [event.kind for event in await _rest(again)] == [EVENT_SUCCEEDED]


async def test_a_turn_opencode_lost_with_its_process_fails_once(opencode, backend, monkeypatch):
    monkeypatch.setattr(adapter, "RECONCILE_SETTLE_SECONDS", 0)
    stream = await _accepted(backend)
    # A restarted OpenCode runs nothing and stored no end for the execution it was running.
    opencode.running.clear()
    opencode.store("assistant", content=[{"type": "text", "text": "half an ans"}], finish="error")
    opencode.drop_stream()

    (ended,) = await _rest(stream)

    assert (ended.kind, ended.text) == (EVENT_FAILED, adapter.EXECUTION_LOST)
    # OpenCode reported no error, so the failure has no type of OpenCode's. Its session is still known.
    assert ended.data["backend_session"] == C1 and "error_type" not in ended.data


async def test_a_request_id_that_ran_before_ends_at_once_and_sends_no_prompt(opencode, backend):
    first = await _accepted(backend)
    opencode.emit("session.execution.succeeded")
    await _rest(first)
    prompts = opencode.paths().count(f"/api/session/{C1}/prompt")

    async with asyncio.timeout(1):
        (again,) = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hello")))

    assert (again.kind, again.data) == (EVENT_FAILED, {DUPLICATE: True})
    assert opencode.paths().count(f"/api/session/{C1}/prompt") == prompts
    assert not any(path.endswith("/interrupt") for path in opencode.paths())
    # Control: a new request id with the same text is a new prompt.
    other = backend.run_turn(TurnRequest(conversation="c1", request_id="r2", text="hello"))
    assert (await anext(other)).kind == EVENT_ACCEPTED
    assert opencode.paths().count(f"/api/session/{C1}/prompt") == prompts + 1
    opencode.emit("session.execution.succeeded")
    await _rest(other)


async def test_continuation_uses_retained_session_without_creating_one(opencode, backend):
    first = await _accepted(backend)
    opencode.emit("session.execution.succeeded")
    await _rest(first)
    assert await backend.retained("c1")
    continuation = backend.run_turn(TurnRequest(
        conversation="c1", request_id="r2", text="next",
        existing_conversation_only=True,
    ))
    assert (await anext(continuation)).kind == EVENT_ACCEPTED
    assert opencode.paths().count("/api/session") == 1
    opencode.emit("session.execution.succeeded")
    await _rest(continuation)

    del opencode.sessions[C1]
    assert not await backend.retained("c1")
    missing = await _rest(backend.run_turn(TurnRequest(
        conversation="c1", request_id="r3", text="must not create",
        existing_conversation_only=True,
    )))
    assert [(event.kind, event.text) for event in missing] == [
        (EVENT_FAILED, CONVERSATION_UNAVAILABLE)
    ]
    # The turn had no session, so it names none.
    assert "backend_session" not in missing[0].data
    assert opencode.paths().count("/api/session") == 1
    assert opencode.paths().count(f"/api/session/{C1}/prompt") == 2


async def test_forget_deletes_the_session_counts_a_missing_one_as_deleted_and_refuses_a_running_turn(
    opencode, backend
):
    assert OP_FORGET in backend.capabilities().operations
    stream = await _accepted(backend)
    with pytest.raises(BackendError, match="still has a running turn"):
        await backend.forget("c1")
    opencode.emit("session.execution.succeeded")
    await _rest(stream)
    # A turn the server runs that this process did not start is refused too.
    opencode.running.add(C1)
    with pytest.raises(BackendError, match="still has a running turn"):
        await backend.forget("c1")
    opencode.running.discard(C1)

    await backend.forget("c1")
    assert ("DELETE", f"/api/session/{C1}", None) in opencode.calls
    assert not await backend.retained("c1")
    await backend.forget("c1")
    assert opencode.paths("DELETE").count(f"/api/session/{C1}") == 2


async def test_active_lists_this_deployments_running_conversations_only(opencode, backend):
    stream = await _accepted(backend)
    # A session of another deployment on the same server, and one that is not Belle's at all.
    opencode.sessions[session_id("other", "c9")] = {"belle": {"conversation": "c9"}}
    opencode.sessions["ses_foreign"] = {}
    opencode.running |= {session_id("other", "c9"), "ses_foreign", "ses_gone"}

    assert await backend.active() == ["c1"]

    await stream.aclose()
    opencode.running.clear()
    assert await backend.active() == []


async def test_pending_lists_what_a_session_waits_on_a_person_for(opencode, backend):
    assert await backend.pending("c1") == []
    opencode.permissions[C1] = [{"id": "per_1", "action": "shell", "resources": ["rm -r build"]}]

    (question,) = await backend.pending("c1")

    assert (question.id, question.kind, question.tool) == ("per_1", APPROVAL, "shell")
    assert "rm -r build" in question.text


async def test_close_removes_the_tool_mount(opencode):
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    assert (len(opencode.mounts), opencode.unmounts) == (1, 0)

    await backend.close()

    assert opencode.unmounts == 1


async def test_missing_mount_is_restored_before_a_prompt(opencode, backend):
    assert len(opencode.mounts) == 1
    opencode.mount_present = False

    stream = await _accepted(backend)

    assert len(opencode.mounts) == 2
    assert opencode.mount_present
    mounted = [i for i, (method, path, _) in enumerate(opencode.calls)
               if method == "PUT" and path == "/api/experimental/mcp/belle"]
    prompted = next(i for i, (method, path, _) in enumerate(opencode.calls)
                    if method == "POST" and path == f"/api/session/{C1}/prompt")
    assert mounted[-1] < prompted
    await stream.aclose()


@pytest.mark.parametrize("present", [False, True], ids=["missing", "failed"])
async def test_remount_failure_releases_the_conversation_for_retry(opencode, backend, present):
    opencode.mount_present = present
    opencode.mount_status = {"status": "failed", "error": "Unable to connect"}
    request = TurnRequest(conversation="c1", request_id="r1", text="hello")

    async with asyncio.timeout(5):
        events = await _rest(backend.run_turn(request))

    assert [event.kind for event in events] == [EVENT_FAILED]
    assert "Unable to connect" in events[0].text
    assert f"/api/session/{C1}/prompt" not in opencode.paths()
    assert backend._turns == {}
    assert opencode.paths().count("/api/experimental/mcp/belle/connect") == 1

    opencode.mount_status = {"status": "connected"}
    retry = backend.run_turn(request)
    assert (await anext(retry)).kind == EVENT_ACCEPTED
    opencode.emit("session.execution.succeeded")
    assert (await _rest(retry))[-1].kind == EVENT_SUCCEEDED
    assert opencode.paths().count(f"/api/session/{C1}/prompt") == 1
    assert backend._turns == {}


async def test_concurrent_conversations_restore_a_missing_mount_once(opencode, backend, monkeypatch):
    opencode.mount_present = False
    mounting = asyncio.Event()
    release = asyncio.Event()
    both_waiting = asyncio.Event()
    mount = backend._mount
    ensure_mount = backend._ensure_mount
    waiting = 0

    async def blocked_mount():
        mounting.set()
        await release.wait()
        await mount()

    async def counted_ensure_mount():
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            both_waiting.set()
        await ensure_mount()

    monkeypatch.setattr(backend, "_mount", blocked_mount)
    monkeypatch.setattr(backend, "_ensure_mount", counted_ensure_mount)
    first = asyncio.create_task(_accepted(backend, "c1"))
    second = asyncio.create_task(_accepted(backend, "c2"))
    try:
        async with asyncio.timeout(5):
            await mounting.wait()
            await both_waiting.wait()
            assert not any(path.endswith("/prompt") for path in opencode.paths())
            release.set()
            streams = await asyncio.gather(first, second)
        assert len(opencode.mounts) == 2  # Startup and one shared recovery.
        for conversation, stream in zip(("c1", "c2"), streams, strict=True):
            opencode.emit("session.execution.succeeded", session=session_id("", conversation))
            assert (await _rest(stream))[-1].kind == EVENT_SUCCEEDED
        assert backend._turns == {}
    finally:
        release.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        results = await asyncio.gather(first, second, return_exceptions=True)
        for result in results:
            if not isinstance(result, BaseException):
                await result.aclose()


async def test_a_prompt_opencode_refuses_is_one_failed_event(opencode, backend):
    opencode.prompt_status = 500

    events = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hello")))

    assert [event.kind for event in events] == [EVENT_FAILED]
    assert "HTTP 500" in events[0].text
    # The session was made before the prompt was refused, and OpenCode's log of the refusal names it.
    assert events[0].data == {"backend_session": C1}


async def test_a_file_travels_as_a_data_uri_and_in_no_other_form(opencode, backend):
    local = ({"uri": "file:///etc/passwd", "name": "passwd"},)

    events = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="read", files=local)))

    # OpenCode would read that path from its own filesystem. The prompt is never sent.
    assert [event.kind for event in events] == [EVENT_FAILED]
    assert opencode.paths() == []

    # Control: the same turn with the content inline is sent.
    inline = ({"uri": "data:text/plain;base64,aGk=", "name": "note.txt"},)
    await _accepted(backend, files=inline)
    assert opencode.calls[-1][2]["files"] == [{"uri": "data:text/plain;base64,aGk=", "name": "note.txt"}]


async def test_what_a_session_reports_before_the_prompt_is_delivered_is_not_this_turns(opencode, backend):
    # A stopped execution can report its end after the next turn of the conversation has begun.
    opencode.emit("session.execution.interrupted", reason="user")
    stream = await _accepted(backend)
    opencode.emit("session.execution.succeeded")

    assert [event.kind for event in await _rest(stream)] == [EVENT_SUCCEEDED]


class Toolkit:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def get_tool_definitions(self):
        return [{"name": "post_message", "description": "Post.", "input_params": {"type": "object"}}]

    async def post_message(self, text: str) -> str:
        self.posts.append(text)
        return json.dumps({"ok": True})


@pytest.fixture
async def served(opencode):
    """A started adapter that serves one toolkit, and what calls its tool server as OpenCode would."""
    toolkit, calls = Toolkit(), iter(range(1, 100))
    tools = ToolHost([toolkit])
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(tools)
    mount = opencode.mounts[-1]

    async def call(session: str, text: str, headers: dict | None = None) -> httpx.Response:
        params = {"name": "post_message", "arguments": {"text": text}, "_meta": {"ai.opencode/sessionID": session}}
        body = {"jsonrpc": "2.0", "id": next(calls), "method": "tools/call", "params": params}
        async with httpx.AsyncClient() as client:
            return await client.post(mount["url"], json=body, headers=mount["headers"] if headers is None else headers)

    yield backend, tools, toolkit, call
    await backend.close()


def _result(response: httpx.Response) -> dict:
    return json.loads(response.json()["result"]["content"][0]["text"])


async def test_the_tools_are_mounted_by_name_with_a_token_of_this_run(opencode, served):
    backend, _, toolkit, call = served

    (mount,) = opencode.mounts
    assert mount["url"].startswith("http://127.0.0.1:") and mount["url"].endswith(PATH)
    assert (mount["type"], mount["codemode"], mount["oauth"]) == ("remote", False, False)
    # OpenCode advertises the tools of server `belle` as `belle_<tool>`, and the host is told so.
    assert backend.capabilities().tool_name("post_message") == "belle_post_message"
    # The listener needs the token the mount carries. Without it nothing runs.
    assert (await call(C1, "forged", headers={})).status_code == 401
    assert (await call(C1, "forged", headers={"Authorization": "Bearer guess"})).status_code == 401
    assert toolkit.posts == []


async def test_content_tool_is_mounted(opencode):
    async def request(**kwargs):
        return {"summary": "done"}

    toolkit = ContentToolkit({"summarize": {}}, request=request)
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    try:
        await backend.start(ToolHost([toolkit]))
        assert "summarize" in backend._belle_tools
        assert backend.capabilities().tool_name("summarize") == "belle_summarize"
    finally:
        await backend.close()


async def test_a_tool_call_is_bound_through_the_session_that_made_it(opencode, served):
    backend, tools, toolkit, call = served
    running = await _accepted(backend)
    tools.bind("c1", BoundTurn("r1"))
    tools.bind("c2", BoundTurn("r1"))
    opencode.parents = {"ses_child": C1, "ses_grandchild": "ses_child", "ses_orphan": "ses_gone"}

    # A session that runs no turn of this adapter is refused: one OpenCode never heard of, one whose
    # ancestry ends elsewhere, and the session of a conversation that the tool host does hold a turn
    # for but that this adapter is not running.
    for session in ("ses_unknown", "ses_orphan", session_id("", "c2"), ""):
        assert _result(await call(session, "not mine"))["error"] == "tool_denied"
    assert toolkit.posts == []

    # Control: the session of the running turn posts, and so does a subagent's child session, which
    # carries its own id and descends from it.
    for session in (C1, "ses_child", "ses_grandchild"):
        assert _result(await call(session, f"from {session}"))["ok"] is True
    assert toolkit.posts == [f"from {C1}", "from ses_child", "from ses_grandchild"]
    await running.aclose()


async def test_the_tools_are_mounted_again_whenever_the_event_stream_is_opened(opencode, served):
    backend, _, _, _ = served
    stream = await _accepted(backend)
    opencode.drop_stream()
    opencode.emit("session.execution.succeeded")
    await _rest(stream)

    # A restart of OpenCode drops the stream and the mount together. The next turn waits for both.
    again = backend.run_turn(TurnRequest(conversation="c1", request_id="r2", text="again"))
    assert (await anext(again)).kind == EVENT_ACCEPTED
    assert len(opencode.mounts) == 2
    assert opencode.mounts[0] == opencode.mounts[1]
    opencode.emit("session.execution.succeeded")
    await _rest(again)


async def test_a_mount_that_failed_is_asked_to_connect_once_more(opencode):
    # A Belle that comes back at a new address: OpenCode first tries the address it remembers.
    opencode.mount_status = {"status": "failed", "error": "Request timed out"}
    opencode.status_after_connect = {"status": "connected"}
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))

    await backend.start(ToolHost([]))
    await backend.close()

    assert opencode.paths().count("/api/experimental/mcp/belle/connect") == 1


async def test_a_tool_server_opencode_cannot_reach_stops_the_start_and_says_so(opencode):
    opencode.mount_status = {"status": "failed", "error": "Unable to connect"}
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))

    # At once, with OpenCode's own reason: not at the end of the wait for readiness, with the reason in a log.
    async with asyncio.timeout(5):
        with pytest.raises(OpenCodeError, match="cannot reach the tool server at .*: Unable to connect"):
            await backend.start(ToolHost([]))
    assert backend._tool_server is None
    # A failed start has closed everything, and whoever started it may close again.
    await backend.close()


async def test_a_mount_that_fails_after_the_start_is_tried_again_and_ends_nothing(opencode, backend):
    opencode.mount_status = {"status": "failed", "error": "Unable to connect"}
    opencode.drop_stream()
    await asyncio.sleep(0.05)
    opencode.mount_status = {"status": "connected"}

    stream = await _accepted(backend)
    opencode.emit("session.execution.succeeded")
    assert (await _rest(stream))[-1].kind == EVENT_SUCCEEDED


async def test_a_refused_password_stops_the_start_at_once(opencode):
    async def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    backend = OpenCodeBackend("http://opencode.test", "wrong", transport=httpx.MockTransport(refusing))

    async with asyncio.timeout(5):
        with pytest.raises(OpenCodeError, match="http://opencode.test.*password was refused") as raised:
            await backend.start(ToolHost([]))
    assert "wrong" not in str(raised.value)


async def test_a_server_that_cannot_be_reached_stops_the_start_and_names_the_address(opencode):
    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed", request=request)

    backend = OpenCodeBackend("http://opencode.test", "password", ready_timeout=0.2, transport=httpx.MockTransport(unreachable))

    with pytest.raises(OpenCodeError, match="http://opencode.test was not ready .* within 0.2s: it cannot be reached"):
        await backend.start(ToolHost([]))


async def test_a_tool_server_that_cannot_listen_stops_the_start_and_names_the_port(opencode):
    taken = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await taken.start(ToolHost([]))
    backend = OpenCodeBackend(
        "http://opencode.test", "password", tools_port=taken._tool_server.port, transport=httpx.MockTransport(opencode)
    )
    try:
        with pytest.raises(OpenCodeError, match=f"tool server cannot listen on 127.0.0.1:{taken._tool_server.port}"):
            await backend.start(ToolHost([]))
    finally:
        await taken.close()


# ── Approvals and operator questions, on the scripted wire ──────────


class _PostToolkit:
    """One Belle tool, so the adapter knows ``post_message`` is advertised ``belle_post_message``."""

    def get_tool_definitions(self):
        return [{"name": "post_message", "description": "post", "input_params": {"type": "object"}}]

    async def post_message(self, text: str) -> str:
        return "{}"


def test_capabilities_declare_the_approval_operations_and_the_tools_scope_field():
    capabilities = OpenCodeBackend("http://opencode.test", "password").capabilities()
    assert {OP_ASK, OP_ANSWER, OP_CANCEL, OP_PENDING, OP_ACTIVE} <= capabilities.operations
    assert "tools" in capabilities.scope_fields


def test_translate_turns_a_permission_into_an_approval_question():
    event = {
        "type": "permission.asked",
        "data": {"id": "per_1", "sessionID": C1, "action": "belle_post_message", "resources": ["*"]},
    }
    assert adapter.event_session(event) == C1
    turn = adapter._Turn(conversation="c1", prompt_id="p", delivered=True)
    question = adapter.translate(event, turn).question
    assert (question.id, question.kind, question.tool) == ("per_1", APPROVAL, "belle_post_message")


def test_translate_turns_a_form_into_a_question_and_routes_it_by_its_nested_session():
    form = {
        "id": "frm_1", "sessionID": C1, "title": "Proceed?",
        "fields": [{"key": "q0", "type": "string", "options": [{"value": "yes"}, {"value": "no"}]}],
    }
    event = {"type": "form.created", "data": {"form": form}}
    assert adapter.event_session(event) == C1
    turn = adapter._Turn(conversation="c1", prompt_id="p", delivered=True)
    question = adapter.translate(event, turn).question
    assert (question.id, question.kind, question.options) == ("frm_1", "question", ("yes", "no"))


async def test_a_scope_becomes_the_sessions_permission_ruleset(opencode):
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([_PostToolkit()]))
    scope = TurnScope(tools={"post_message": "ask", "shell": "deny"})

    stream = await _accepted(backend, scope=scope)
    opencode.emit("session.execution.succeeded")
    await _rest(stream)
    await backend.close()

    # A Belle tool is named by its advertised name; a built-in keeps its own. `allow` is never written.
    assert {(rule["action"], rule["effect"]) for rule in opencode.permission_rules[C1]} == {
        ("belle_post_message", "ask"),
        ("shell", "deny"),
    }


async def test_each_unavailable_skill_is_denied_after_the_tool_levels(opencode):
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([_PostToolkit()]))
    scope = TurnScope(tools={"skill": "ask"}, skills_unavailable=frozenset({"slack-daily-update", "slack-app-home"}))

    stream = await _accepted(backend, scope=scope)
    opencode.emit("session.execution.succeeded")
    await _rest(stream)
    await backend.close()

    # OpenCode applies the last rule that matches, so the deny of one skill wins over the level on every skill.
    assert opencode.permission_rules[C1] == [
        {"action": "skill", "resource": "*", "effect": "ask"},
        {"action": "skill", "resource": "slack-app-home", "effect": "deny"},
        {"action": "skill", "resource": "slack-daily-update", "effect": "deny"},
    ]


async def test_available_skills_are_allowed_after_a_deny_of_every_skill(opencode):
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([_PostToolkit()]))
    scopes = {
        "c1": TurnScope(skills_available=frozenset({"notes", "digest"}), skills_unavailable=frozenset({"digest"})),
        "c2": TurnScope(tools={"skill": "ask"}, skills_available=frozenset({"notes"})),
        "c3": TurnScope(skills_available=frozenset()),
    }

    for conversation, scope in scopes.items():
        stream = await _accepted(backend, conversation, scope=scope)
        opencode.emit("session.execution.succeeded", session=session_id("", conversation))
        await _rest(stream)
    await backend.close()

    # OpenCode applies the last rule that matches: the deny of every skill, then each listed skill, then each
    # unavailable one. A listed skill keeps the level of the skill tool.
    assert opencode.permission_rules[C1] == [
        {"action": "skill", "resource": "*", "effect": "deny"},
        {"action": "skill", "resource": "digest", "effect": "allow"},
        {"action": "skill", "resource": "notes", "effect": "allow"},
        {"action": "skill", "resource": "digest", "effect": "deny"},
    ]
    assert opencode.permission_rules[session_id("", "c2")] == [
        {"action": "skill", "resource": "*", "effect": "ask"},
        {"action": "skill", "resource": "*", "effect": "deny"},
        {"action": "skill", "resource": "notes", "effect": "ask"},
    ]
    assert opencode.permission_rules[session_id("", "c3")] == [{"action": "skill", "resource": "*", "effect": "deny"}]


async def test_a_scope_can_override_backend_default_denies(opencode):
    backend = OpenCodeBackend(
        "http://opencode.test", "password", deny_tools=frozenset({"shell", "read"}),
        transport=httpx.MockTransport(opencode),
    )
    await backend.start(ToolHost([_PostToolkit()]))
    stream = await _accepted(backend, scope=TurnScope(tools={"read": "allow"}))
    opencode.emit("session.execution.succeeded")
    await _rest(stream)
    await backend.close()

    assert opencode.permission_rules[C1] == [{"action": "shell", "resource": "*", "effect": "deny"}]


@pytest.mark.parametrize(
    ("levels", "rules"),
    [
        # `write` and `edit` are one permission in OpenCode, `edit`. A rule on `write` matches nothing.
        ({"write": "deny"}, {("edit", "deny")}),
        ({"edit": "deny"}, {("edit", "deny")}),
        ({"write": "ask"}, {("edit", "ask")}),
        # Two tools of one action take the stricter level, whichever the scope names first.
        ({"write": "deny", "edit": "ask"}, {("edit", "deny")}),
        ({"edit": "ask", "write": "deny"}, {("edit", "deny")}),
        # So is the Code Mode tool that removes a worktree.
        ({"opencode_worktree_remove": "ask"}, {("edit", "ask")}),
        # Every other built-in tool is its own action, and so is the permission that is no tool.
        ({name: "deny" for name, action in BUILTIN_ACTIONS.items() if name == action},
         {(name, "deny") for name, action in BUILTIN_ACTIONS.items() if name == action}),
        ({"external_directory": "deny"}, {("external_directory", "deny")}),
        # A name nobody declares is written as it stands: it may be a tool of a server the operator mounted.
        ({"github_create_issue": "ask", "post_message": "deny"}, {("github_create_issue", "ask"), ("belle_post_message", "deny")}),
        ({"shell": "allow"}, set()),
    ],
)
def test_a_tool_level_is_written_on_the_action_that_gates_the_tool(levels, rules):
    written = permission_rules(levels, lambda name: f"belle_{name}" if name == "post_message" else None)

    assert {(rule["action"], rule["effect"]) for rule in written} == rules
    assert len(written) == len(rules) and all(rule["resource"] == "*" for rule in written)


async def test_the_host_names_at_start_each_tool_level_this_backend_cannot_hold(opencode, tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
platforms: {loopback: {enabled: true}}
scopes:
  - match: {channel: loopback}
    permissions:
      tools: {write: deny, edit: ask, shell: deny, external_directory: deny, post_message: ask,
              execute: ask, shel: deny}
  - match: {channel: loopback, chat: C1}
    permissions: {tools: {execute: ask, question: deny}}
""",
        encoding="utf-8",
    )
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    host = host_from_config(load_config(path), backend, state_dir=tmp_path)

    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await host.start()
    await host.stop()

    warned = sorted(record.getMessage() for record in caplog.records if "permissions.tools." in record.getMessage())
    # OpenCode never asks before `execute` runs, and nobody has a tool named `shel`. Each is said once.
    assert len(warned) == 2, warned
    assert "permissions.tools.execute=ask" in warned[0] and "does not apply ask to execute" in warned[0]
    assert "permissions.tools.shel=deny" in warned[1] and "declares a tool of that name" in warned[1]


async def test_a_silent_scope_writes_an_empty_ruleset_so_nothing_is_restricted(backend, opencode):
    stream = await _accepted(backend)
    opencode.emit("session.execution.succeeded")
    await _rest(stream)

    assert opencode.permission_rules[C1] == []


async def test_recovery_distinguishes_unsubmitted_running_and_finished_requests(backend, opencode, monkeypatch):
    monkeypatch.setattr(adapter, "RECONCILE_SETTLE_SECONDS", 0)
    request = TurnRequest("c1", "recover-1", "SAY=answer", TurnScope(tools={"shell": "deny"}))
    prompt_id = message_id("", "c1", request.request_id)

    assert OP_RECOVER in backend.capabilities().operations
    assert (await backend.recover(request)).state == RECOVERY_NOT_SUBMITTED
    opencode.store("user", C1, id=prompt_id)
    opencode.running.add(C1)

    running = await backend.recover(request)
    assert running.state == RECOVERY_RUNNING
    assert backend._turns[C1].scope == request.scope
    assert any(rule["action"] == "shell" and rule["effect"] == "deny" for rule in opencode.permission_rules[C1])

    opencode.end("succeeded", C1, content=[{"type": "text", "text": "answer"}], finish="stop")
    finished = await backend.recover(request)
    assert finished.state == RECOVERY_TERMINAL
    assert finished.terminal.kind == EVENT_SUCCEEDED and finished.terminal.text == "answer"
    assert C1 not in backend._turns
    assert not any(path.endswith("/prompt") for path in opencode.paths())


async def test_recovery_refuses_to_bind_a_different_request_to_an_active_turn(backend, opencode):
    first = TurnRequest("c1", "one", "first")
    second = TurnRequest("c1", "two", "second")
    opencode.store("user", C1, id=message_id("", "c1", first.request_id))
    opencode.running.add(C1)
    assert (await backend.recover(first)).state == RECOVERY_RUNNING

    with pytest.raises(OpenCodeError, match="another request"):
        await backend.recover(second)


async def test_recovery_refuses_an_active_session_without_this_requests_prompt(backend, opencode):
    request = TurnRequest("c1", "not-submitted", "new work")
    opencode.store("user", C1, id=message_id("", "c1", "older-request"))
    opencode.running.add(C1)

    with pytest.raises(OpenCodeError, match="prompt is absent"):
        await backend.recover(request)
    assert C1 not in backend._turns


async def test_recovery_restores_a_pending_question_and_can_cancel_the_turn(backend, opencode):
    request = TurnRequest("c1", "recover-question", "ask")
    opencode.store("user", C1, id=message_id("", "c1", request.request_id))
    opencode.running.add(C1)
    opencode.forms[C1] = [{"id": "frm_recover", "title": "Choose", "fields": [{"key": "answer"}]}]

    assert (await backend.recover(request)).state == RECOVERY_RUNNING
    assert [question.id for question in await backend.pending("c1")] == ["frm_recover"]
    await backend.answer("c1", "frm_recover", Answer(values=("yes",)))
    assert opencode.form_replies[-1] == ("frm_recover", {"answer": {"answer": "yes"}})
    assert await backend.cancel("c1") is True


async def test_ask_creates_a_form_and_answer_replies_to_a_form_or_a_permission(backend, opencode):
    form_id = await backend.ask("c1", Question(id="", text="Admit this turn?", options=("yes", "no")))
    assert form_id.startswith("frm_")
    assert opencode.forms[C1][0]["title"] == "Admit this turn?"

    await backend.answer("c1", form_id, Answer(values=("yes",)))
    assert opencode.form_replies[-1] == (form_id, {"answer": {"answer": "yes"}})

    await backend.answer("c1", "per_approve", Answer(approved=True))
    await backend.answer("c1", "per_reject", Answer(approved=False))
    assert opencode.permission_replies == [("per_approve", "once"), ("per_reject", "reject")]


async def test_a_refused_question_cancels_its_form_or_rejects_its_permission_with_the_reason(backend, opencode):
    reason = "The question was not shown, so nobody was asked: at most 25 options & 1 select."
    form_id = await backend.ask("c1", Question(id="", text="Pick one", options=("a", "b")))

    await backend.refuse_question("c1", form_id, reason)
    await backend.refuse_question("c1", "per_post", reason)

    assert opencode.forms[C1] == [] and opencode.forms_cancelled == [(form_id, reason)]
    assert opencode.permission_replies == [("per_post", "reject")]
    assert opencode.permission_messages == [("per_post", reason)]


# ── Per-turn scope on the wire, and the relay plugin mode ───────────


async def test_the_prompt_attaches_the_turns_required_skills(opencode, backend):
    stream = await _accepted(backend, scope=TurnScope(skills_required=("group-chat-participation",)))
    opencode.emit("session.execution.succeeded")
    await _rest(stream)

    prompt = next(body for method, path, body in opencode.calls if path.endswith("/prompt"))
    assert prompt["skills"] == [{"id": "group-chat-participation"}]


async def test_a_turns_model_serves_that_turn_and_the_default_serves_the_next(opencode, backend):
    switches = []
    for model in ("", "configured/some/model", "configured/some/model", "", ""):
        stream = await _accepted(backend, scope=TurnScope(model=model))
        opencode.emit("session.execution.succeeded")
        await _rest(stream)
        switches.append([body["model"] for method, path, body in opencode.calls if method == "POST" and path.endswith("/model")])
        opencode.calls.clear()

    named, default = {"providerID": "configured", "id": "some/model"}, {"providerID": "configured", "id": "m"}
    # OpenCode keeps a switch for the session: one call when the model changes, none while it stays,
    # and one back to the default for the first turn that names no model.
    assert switches == [[], [named], [], [default], []]


async def test_the_relay_plugin_mode_starts_the_endpoint_and_moves_system_off_the_entry(opencode):
    opencode.plugins = [BUILTIN_PLUGIN, RELAY_PLUGIN]
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    assert backend._plugin is True and backend._relay is not None

    stream = await _accepted(backend, scope=TurnScope(system_additions=("Answer in French.",)))
    opencode.emit("session.execution.succeeded")
    await _rest(stream)
    await backend.close()

    # With the plugin loaded the system additions travel through the context hook, not the instruction entry.
    # (The context answer itself is covered by tests/opencode/test_relay_endpoint.py.)
    assert not any("/instructions/entries/" in path for _, path, _ in opencode.calls)


@pytest.mark.parametrize("loaded", [True, False])
async def test_a_cold_server_that_lists_no_plugin_yet_is_asked_again(opencode, monkeypatch, loaded):
    """OpenCode answers its first plugin list before it has loaded the plugins: empty, in both modes."""
    monkeypatch.setattr(adapter, "READY_POLL_SECONDS", 0.01)
    opencode.plugins_unloaded = 2
    opencode.plugins = [BUILTIN_PLUGIN, RELAY_PLUGIN] if loaded else [BUILTIN_PLUGIN]
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))

    await backend.start(ToolHost([]))

    # An empty list says nothing of the relay plugin. Read as "no plugin", it left a plugin's hooks unanswered.
    assert (backend._plugin, backend._relay is not None) == (loaded, loaded)
    assert [path for _, path, _ in opencode.calls].count("/api/plugin") == 3
    await backend.close()


async def test_a_server_that_never_lists_its_plugins_stops_the_start_and_says_so(opencode, monkeypatch):
    monkeypatch.setattr(adapter, "READY_POLL_SECONDS", 0.01)
    opencode.plugins_unloaded = 10**6
    backend = OpenCodeBackend("http://opencode.test", "password", ready_timeout=0.2, transport=httpx.MockTransport(opencode))

    with pytest.raises(OpenCodeError, match="it lists no plugin yet"):
        await backend.start(ToolHost([]))


async def test_a_prompt_that_fails_on_a_plugin_loaded_after_the_start_names_the_cause(opencode, backend, caplog):
    assert backend._plugin is False
    # The plugin appears once turns run, and its hook fails the prompt with nothing in OpenCode's answer.
    opencode.plugins = [BUILTIN_PLUGIN, RELAY_PLUGIN]
    opencode.prompt_status = 500

    with caplog.at_level(logging.WARNING, logger="belle_agent.integrations.opencode.adapter"):
        events = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hello")))

    assert "loaded the relay plugin after this adapter had started" in events[0].text
    assert any("loaded the relay plugin after" in record.getMessage() for record in caplog.records)
    # The endpoint is started now, so the same message sent again finds its hooks answered.
    assert backend._plugin is True and backend._relay is not None


async def test_a_prompt_that_fails_behind_the_plugin_names_the_relay_address(opencode):
    opencode.plugins = [BUILTIN_PLUGIN, RELAY_PLUGIN]
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    opencode.prompt_status = 500

    events = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hello")))
    await backend.close()

    assert "HTTP 500" in events[0].text and "BELLE_RELAY_URL" in events[0].text
    assert "refused no hook while this turn ran" in events[0].text


async def test_a_prompt_that_fails_on_a_refused_hook_names_the_refusal(opencode, monkeypatch):
    opencode.plugins = [BUILTIN_PLUGIN, RELAY_PLUGIN]
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    monkeypatch.setattr(hook_server, "MAX_REQUEST_BYTES", 16)

    async def plugin(body):
        # The plugin's context hook posts an event over the endpoint's limit, and its refusal fails the prompt.
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://127.0.0.1:{backend._relay.port}/hook/context", content=b"x" * 32,
                headers={"X-Belle-Session": adapter.session_id(backend._namespace, "c1")},
            )
        opencode.prompt_status = 500

    opencode.before_prompt = plugin
    events = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hello")))
    port = backend._relay.port
    await backend.close()

    assert (
        f"The relay endpoint on 127.0.0.1:{port} refused a hook while this turn ran:"
        " HTTP 413 for /hook/context: the event is 32 bytes; a hook event may hold at most 16"
    ) in events[0].text


async def test_a_prompt_failure_does_not_name_another_sessions_hook_refusal(opencode, monkeypatch):
    opencode.plugins = [BUILTIN_PLUGIN, RELAY_PLUGIN]
    backend = OpenCodeBackend("http://opencode.test", "password", transport=httpx.MockTransport(opencode))
    await backend.start(ToolHost([]))
    monkeypatch.setattr(hook_server, "MAX_REQUEST_BYTES", 16)

    async def plugin(body):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://127.0.0.1:{backend._relay.port}/hook/context", content=b"x" * 32,
                headers={"X-Belle-Session": adapter.session_id(backend._namespace, "other")},
            )
        opencode.prompt_status = 500

    opencode.before_prompt = plugin
    events = await _rest(backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hello")))
    assert backend._relay.refused == {}
    await backend.close()

    assert "refused no hook while this turn ran" in events[0].text
    assert "HTTP 413" not in events[0].text


async def test_no_plugin_mode_keeps_the_instruction_entry(opencode, backend):
    assert backend._plugin is False
    stream = await _accepted(backend, scope=TurnScope(system_additions=("Answer in French.",)))
    opencode.emit("session.execution.succeeded")
    await _rest(stream)

    assert any("/instructions/entries/" in path for _, path, _ in opencode.calls)


def test_deny_tools_refuses_a_name_it_does_not_know_and_names_the_ones_it_takes(monkeypatch):
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "password")
    config = {"backends": {"opencode": {"url": "http://opencode.test", "deny_tools": ["shell", "todo_write"]}}}

    with pytest.raises(BelleConfigError, match="deny_tools holds names that it does not take: todo_write[.]") as refused:
        create_backend(config)

    assert all(name in str(refused.value) for name in TOOL_LEVELS)
