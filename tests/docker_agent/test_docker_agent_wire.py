"""The Docker Agent adapter against a scripted wire. No container and no network."""

import asyncio
import base64
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from belle_agent import launcher
from belle_agent.backend import (
    APPROVAL,
    CATALOG_EXPERTS,
    CATALOG_SUBAGENTS,
    CONVERSATION_UNAVAILABLE,
    DUPLICATE,
    EVENT_ACCEPTED,
    EVENT_FAILED,
    EVENT_QUESTION,
    EVENT_STEP,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    EVENT_USAGE,
    NOT_STEERED,
    OP_CONTINUE,
    OP_FORGET,
    OP_STEER,
    Answer,
    BackendError,
    Question,
    ToolVerdict,
    TurnRequest,
    TurnScope,
)
from belle_agent.config import BelleConfigError
from belle_agent.integrations.docker_agent import DockerAgentBackend, DockerAgentError, adapter, hooks
from belle_agent.tools.content import ContentToolkit
from belle_agent.tools.host import APPROVE, REJECT, BoundTurn, ToolHost

TOKEN = "the-server-token"
STOPPED = {"type": "stream_stopped", "session_id": "s1", "reason": "normal"}
#: The entry agent as Docker Agent describes Belle's YAML, which relays Belle's hooks.
ROOT = {
    "Name": "root",
    "model": "default",
    "sub_agents": ["helper"],
    "hooks": {name: [{}] for name in hooks.RELAYED | {hooks.USER_STEERING}},
}
HELPER = {**ROOT, "Name": "helper", "sub_agents": []}


class Echo:
    def get_tool_definitions(self):
        return [{"name": "echo", "description": "Echo a text back.", "input_params": {"type": "object"}}]

    async def echo(self, text: str = "") -> str:
        return text


def _started(session: str = "s1") -> list[dict]:
    """The events Docker Agent sends before the first model call of a turn."""
    return [
        {"type": "session_title", "session_id": session, "title": "t"},
        {"type": "user_message", "session_id": session, "message": "hi"},
        {"type": "stream_started", "session_id": session},
        {"type": "toolset_info", "available_tools": 2},
        {"type": "agent_info", "agent_name": "root", "model": "default"},
    ]


def _usage(cost: float) -> dict:
    return {
        "type": "token_usage",
        "session_id": "s1",
        "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 9.0, "last_message": {"Cost": cost}},
    }


TEXT_TURN = [
    *_started(),
    {"type": "agent_choice", "session_id": "s1", "content": "hel"},
    {"type": "agent_choice", "session_id": "s1", "content": "lo"},
    _usage(0.5),
    STOPPED,
]


class FakeDockerAgent:
    """Answers the adapter's calls and streams one scripted turn per turn request."""

    def __init__(
        self,
        *turns: list[dict],
        sessions: list[dict] | None = None,
        root: dict | None = None,
        others: tuple[dict, ...] = (),
        models: dict | None = None,
    ) -> None:
        self.agents = [root or ROOT, HELPER, *others]
        self.models = models or {"default": {}, "second": {}}
        self.turns = list(turns)
        self.sessions = sessions or []
        #: The messages of each session, as `GET /api/sessions/:id` lists them.
        self.history: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.turn_headers: list[dict[str, str]] = []
        self.created = 0
        #: How the server answers a steered message.
        self.steer_status = 202
        #: How many messages wait in the session's steer queue.
        self.steer_depth = 0
        #: The sessions the server has, and those it runs a turn in.
        self.live: set[str] = {str(entry["id"]) for entry in self.sessions if entry.get("id")}
        self.streaming: set[str] = set()
        #: Fail each delete, whether the session exists or not.
        self.delete_fails = False
        #: Refuse each turn, whether the session exists or not.
        self.run_fails = False
        #: Called as each turn request arrives, before its stream.
        self.on_run: Any = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api")
        self.calls.append((request.method, path, body))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if path == "/ping":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/agents/belle":
            return httpx.Response(200, json={"agents": self.agents, "models": self.models})
        if path == "/sessions" and request.method == "GET":
            return httpx.Response(200, json=self.sessions)
        if path == "/sessions" and request.method == "POST":
            self.created += 1
            self.live.add(f"s{self.created}")
            return httpx.Response(200, json={"id": f"s{self.created}", "title": body["title"]})
        if path.startswith("/sessions/") and request.method == "DELETE":
            session = path.split("/")[2]
            if session not in self.live or self.delete_fails:
                # As Docker Agent 1.152 answers a missing session: 500, not 404.
                return httpx.Response(500, json={"message": "failed to delete session: session not found"})
            self.live.discard(session)
            return httpx.Response(200, json={"message": "session deleted"})
        if path.count("/") == 2 and path.startswith("/sessions/") and request.method == "GET":
            found = path.split("/")[2] in self.live
            session = path.split("/")[2]
            return httpx.Response(200, json={"id": session, "messages": self.history.get(session, [])}) if found else httpx.Response(404, json={})
        if path.endswith("/messages") and request.method == "POST":
            self.history.setdefault(path.split("/")[2], []).append(body["message"])
            return httpx.Response(201, json={"status": "added"})
        if path.endswith("/permissions") and request.method == "PATCH":
            if path.split("/")[2] not in self.live:
                # As Docker Agent 1.152 answers a missing session: 500, not 404.
                return httpx.Response(500, json={"message": "failed to update session permissions: session not found"})
            return httpx.Response(200, json={"message": "session permissions updated"})
        if path.endswith("/status"):
            return httpx.Response(200, json={"streaming": path.split("/")[2] in self.streaming})
        if path.endswith("/steer"):
            return httpx.Response(self.steer_status, json={"status": "queued"})
        if path.endswith("/queue"):
            return httpx.Response(200, json={"steer": {"depth": self.steer_depth, "capacity": 5}})
        if path.endswith("/resume"):
            return httpx.Response(200, json={"message": "session resumed"})
        if path.endswith("/agent/belle"):
            if path.split("/")[2] not in self.live or self.run_fails:
                return httpx.Response(500, json={"message": "failed to run session: session not found"})
            self.turn_headers.append(dict(request.headers))
            if self.on_run is not None:
                self.on_run()
            lines = "".join(f"data: {json.dumps(event)}\n\n" for event in self.turns.pop(0))
            return httpx.Response(200, content=lines.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"message": "no route"})


async def _started_backend(
    fake: FakeDockerAgent, namespace: str = "", state: Path | None = None, **settings: Any
) -> DockerAgentBackend:
    """An adapter on ``fake``, started. ``state`` is its state directory, a new one unless given."""
    backend = DockerAgentBackend(
        "http://docker-agent.test:8080",
        TOKEN,
        relay_token="relay",
        namespace=namespace,
        state_dir=state or Path(tempfile.mkdtemp()),
        transport=httpx.MockTransport(fake),
        **settings,
    )
    await backend.start(ToolHost([Echo()]))
    return backend


async def test_content_tool_is_served(tmp_path):
    async def request(**kwargs):
        return {"summary": "done"}

    toolkit = ContentToolkit({"summarize": {}}, request=request)
    backend = DockerAgentBackend(
        "http://docker-agent.test:8080",
        TOKEN,
        relay_token="relay",
        state_dir=tmp_path,
        transport=httpx.MockTransport(FakeDockerAgent()),
    )
    try:
        await backend.start(ToolHost([toolkit]))
        assert backend._belle_tools["belle_summarize"] == "summarize"
    finally:
        await backend.close()


async def _events(backend, text: str = "hi", conversation: str = "c1", request_id: str = "r1", **fields) -> list:
    request = TurnRequest(conversation=conversation, request_id=request_id, text=text, **fields)
    return [event async for event in backend.run_turn(request)]


async def test_a_turn_reports_its_text_and_ends_with_the_final():
    backend = await _started_backend(FakeDockerAgent(TEXT_TURN))
    try:
        events = await _events(backend)
    finally:
        await backend.close()

    assert [event.kind for event in events] == [
        EVENT_ACCEPTED,
        EVENT_STEP,
        EVENT_TEXT_DELTA,
        EVENT_TEXT_DELTA,
        EVENT_USAGE,
        EVENT_SUCCEEDED,
    ]
    assert events[-1].text == "hello"
    # The usage of the turn is the cost of its model calls, not the session's.
    assert events[4].data == {"cost": 0.5, "tokens": {"input": 10, "output": 2}}


async def test_a_call_of_a_belle_tool_is_named_as_the_core_names_it():
    call = {"id": "call_1", "type": "function", "function": {"name": "belle_echo", "arguments": '{"text": "T"}'}}
    turn = [
        *_started(),
        {"type": "partial_tool_call", "tool_call": call},
        _usage(0.1),
        {"type": "tool_call", "tool_call": call, "tool_definition": {"name": "belle_echo"}},
        {
            "type": "tool_call_response",
            "tool_call_id": "call_1",
            "tool_definition": {"name": "belle_echo"},
            "response": "echo saw T",
            "result": {"output": "echo saw T"},
        },
        {"type": "agent_info", "agent_name": "root"},
        {"type": "agent_choice", "session_id": "s1", "content": "done"},
        _usage(0.2),
        STOPPED,
    ]
    backend = await _started_backend(FakeDockerAgent(turn))
    try:
        events = await _events(backend)
    finally:
        await backend.close()

    called = next(event for event in events if event.kind == EVENT_TOOL_CALLED)
    result = next(event for event in events if event.kind == EVENT_TOOL_RESULT)
    assert called.data == {"name": "echo", "call_id": "call_1", "arguments": {"text": "T"}}
    assert (result.text, result.data) == ("echo saw T", {"name": "echo", "call_id": "call_1"})
    # Each model call is a step, and the final is the text of the last one.
    assert [event.kind for event in events].count(EVENT_STEP) == 2
    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "done")


async def test_a_subagents_session_answers_its_hooks_from_the_turn_that_started_it_and_its_cost_is_the_turns():
    transfer = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "transfer_task", "arguments": '{"agent": "helper"}'},
    }
    echo = {"id": "call_2", "type": "function", "function": {"name": "belle_echo", "arguments": "{}"}}
    turn = [
        *_started(),
        {"type": "tool_call", "tool_call": transfer, "tool_definition": {"name": "transfer_task"}},
        {"type": "stream_started", "session_id": "sub1"},
        {"type": "agent_info", "agent_name": "helper"},
        {"type": "agent_choice", "session_id": "sub1", "content": "the helper's own text"},
        {**_usage(0.25), "session_id": "sub1"},
        {"type": "tool_call", "tool_call": echo, "tool_definition": {"name": "belle_echo"}},
        {"type": "agent_info", "agent_name": "root"},
        {"type": "agent_choice", "session_id": "s1", "content": "done"},
        _usage(0.5),
        STOPPED,
    ]
    backend = await _started_backend(FakeDockerAgent(turn))
    backend._tools.bind("c1", BoundTurn(request_id="r1"))
    guard = {"hook_event_name": "tool_guard", "tool_name": "belle_echo", "tool_input": {}, "tool_use_id": "call_2"}
    answers, events = [], []
    try:
        async for event in backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hi")):
            events.append(event)
            if event.data.get("call_id") == "call_2":
                for session in ("sub1", "elsewhere"):
                    answers.append(
                        await asyncio.to_thread(backend._hook, "tool_guard", {**guard, "session_id": session}, {})
                    )
        after = await asyncio.to_thread(backend._hook, "tool_guard", {**guard, "session_id": "sub1"}, {})
    finally:
        await backend.close()

    # The subagent's call is decided under the turn's scope. A session no turn started is refused, during and after.
    assert [_decision(answer) for answer in (*answers, after)] == ["", "deny", "deny"]
    assert all(hooks.NO_TURN in str(answer) for answer in (answers[1], after))
    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "done")
    assert [event.data["cost"] for event in events if event.kind == EVENT_USAGE] == [0.25, 0.75]


@pytest.mark.parametrize(
    ("turn", "expected"),
    [
        # A toolset that did not start leaves the turn without Belle's tools. Docker Agent only warns.
        (
            [
                *_started()[:3],
                {"type": "warning", "message": "Some toolsets failed to initialize for agent 'root'.\n\n- belle: 401"},
                *_started()[3:],
                {"type": "agent_choice", "session_id": "s1", "content": "no tools"},
                STOPPED,
            ],
            adapter.TOOLS_MISSING,
        ),
        (
            [
                *_started(),
                {"type": "error", "session_id": "s1", "error": "model failed: 400"},
                {**STOPPED, "reason": "error"},
            ],
            "model failed: 400",
        ),
        # A turn that stops early, on an error before any model call, gives no reason.
        (
            [
                *_started(),
                {"type": "error", "session_id": "s1", "error": "failed to get tools"},
                {"type": "stream_stopped", "session_id": "s1"},
            ],
            "failed to get tools",
        ),
        ([*_started(), {"type": "agent_choice", "session_id": "s1", "content": "cut"}], adapter.STREAM_LOST),
        # The stream breaks while a call waits for a person: the call never runs, and the turn fails as lost.
        (
            [*_started(), {"type": "tool_call_confirmation", "session_id": "s1", "tool_call": {"id": "call_1"}}],
            adapter.STREAM_LOST,
        ),
    ],
    ids=["toolset-warning", "error", "error-without-reason", "no-stream-stopped", "lost-while-a-call-waits"],
)
async def test_a_turn_that_cannot_end_well_fails_once(turn, expected):
    backend = await _started_backend(FakeDockerAgent(turn))
    try:
        events = await _events(backend)
    finally:
        await backend.close()

    assert [event.kind for event in events if event.terminal] == [EVENT_FAILED]
    assert events[-1].text.startswith(expected)
    # However the turn fails, the session leads to Docker Agent's own record of it.
    assert events[-1].data["backend_session"] == "s1"
    assert not [event for event in events if event.kind == EVENT_TEXT_DELTA and "no tools" in event.text]


#: Docker Agent's end of a run that a hook blocked, with the relay's reason.
RELAY_FAILED = "The relay to Belle failed: curl: (22) The requested URL returned error: 413"
HOOK_BLOCKED_TURN = [
    {"type": "error", "session_id": "s1", "error": RELAY_FAILED, "code": "hook_blocked"},
    {**STOPPED, "reason": "error"},
]


@pytest.mark.parametrize("refused", [True, False], ids=["refused-while-it-ran", "refused-before"])
async def test_a_turn_a_hook_ended_names_the_refusal_of_belles_hook_endpoint_while_it_ran(refused):
    refusal = "HTTP 413 for /hook/docker_agent: the event is 32 bytes; a hook event may hold at most 16"
    model_failed = [{"type": "error", "session_id": "s1", "error": "model failed: 400"}, {**STOPPED, "reason": "error"}]
    fake = FakeDockerAgent(HOOK_BLOCKED_TURN, model_failed)
    backend = await _started_backend(fake)

    def record() -> None:
        backend._hooks.record_refusal(None, refusal)

    if refused:
        fake.on_run = record
    else:
        record()
    try:
        blocked = await _events(backend)
        # Another way of failing is told as it came, whatever the endpoint refused.
        fake.on_run = record
        other = await _events(backend, "again", request_id="r2")
    finally:
        await backend.close()

    note = f"\n{adapter.HOOK_REFUSED}: {refusal}" if refused else ""
    assert (blocked[-1].kind, blocked[-1].text) == (EVENT_FAILED, RELAY_FAILED + note)
    assert other[-1].text == "model failed: 400"


@pytest.mark.parametrize(
    ("turn", "error_type"),
    [
        (HOOK_BLOCKED_TURN, "hook_blocked"),
        # Docker Agent gives an error no code of its own: the failure names no type.
        ([*_started(), {"type": "error", "session_id": "s1", "error": "model failed: 400"}, {**STOPPED, "reason": "error"}], None),
    ],
    ids=["with-a-code", "without-a-code"],
)
async def test_a_failed_turn_names_docker_agents_error_code_and_session_and_not_its_message(turn, error_type):
    backend = await _started_backend(FakeDockerAgent(turn))
    try:
        events = await _events(backend)
    finally:
        await backend.close()

    ended = events[-1]
    assert ended.kind == EVENT_FAILED
    # The session leads to Docker Agent's own record of the failure. Its message is the reader's, not the log's.
    assert (ended.data.get("error_type"), ended.data["backend_session"]) == (error_type, "s1")
    assert "model failed" not in str(ended.data) and "curl" not in str(ended.data)


async def test_a_conversation_keeps_its_session_and_a_new_adapter_finds_it_in_its_state_or_by_its_title(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake, namespace="n1", state=tmp_path)
    try:
        await _events(backend, conversation="c1", request_id="r1")
        await _events(backend, conversation="c1", request_id="r2")
        await _events(backend, conversation="c2", request_id="r3")
    finally:
        await backend.close()
    turns = [path for method, path, _ in fake.calls if path.endswith("/agent/belle")]
    assert turns == ["/sessions/s1/agent/belle", "/sessions/s1/agent/belle", "/sessions/s2/agent/belle"]
    created = [body for method, path, body in fake.calls if (method, path) == ("POST", "/sessions")]
    assert created == [
        {"title": "belle:n1:c1", "safety_policy": "autonomous"},
        {"title": "belle:n1:c2", "safety_policy": "autonomous"},
    ]

    # The same state: the map is read from it, and the server is not asked to list its sessions.
    restarted = FakeDockerAgent(TEXT_TURN)
    restarted.live = fake.live.copy()
    backend = await _started_backend(restarted, namespace="n1", state=tmp_path)
    try:
        await _events(backend, conversation="c2", request_id="r4")
    finally:
        await backend.close()
    assert [path for _, path, _ in restarted.calls if path.endswith("/agent/belle")] == ["/sessions/s2/agent/belle"]
    assert not [path for _, path, _ in restarted.calls if path == "/sessions"]

    # Without that state, or with the state of another namespace, the map is rebuilt from this namespace's titles.
    listed = [
        {"id": "s1", "title": "belle:n1:c1", "created_at": "1"},
        {"id": "s8", "title": "belle:n2:c1", "created_at": "2"},
        {"id": "s9", "title": "belle:other:c1", "created_at": "3"},
    ]
    for namespace, state, session in (("n1", None, "s1"), ("n2", tmp_path, "s8")):
        rebuilt = FakeDockerAgent(TEXT_TURN, sessions=listed)
        backend = await _started_backend(rebuilt, namespace=namespace, state=state)
        try:
            await _events(backend, conversation="c1", request_id="r5")
        finally:
            await backend.close()
        assert [path for _, path, _ in rebuilt.calls if path.endswith("/agent/belle")] == [
            f"/sessions/{session}/agent/belle"
        ]
        assert ("POST", "/sessions") not in [(method, path) for method, path, _ in rebuilt.calls]


async def test_forget_drops_the_session_entry_and_deletes_the_server_session(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        assert OP_FORGET in backend.capabilities().operations
        assert backend.capabilities().scheduler == ""
        assert backend.capabilities().tool_result_max_chars == 0
        await _events(backend, conversation="fresh", request_id="one")
        assert backend._sessions["fresh"] == "s1"
        # A turn the server runs in the session, which this process did not start, is refused.
        fake.streaming.add("s1")
        with pytest.raises(BackendError, match="still has a running turn"):
            await backend.forget("fresh")
        fake.streaming.clear()
        await backend.forget("fresh")
        assert "fresh" not in backend._sessions
        assert json.loads(backend._sessions_file.read_text(encoding="utf-8"))["sessions"] == {}
        assert ("DELETE", "/sessions/s1", None) in fake.calls
        await backend.forget("fresh")
        assert [call for call in fake.calls if call[0] == "DELETE"] == [("DELETE", "/sessions/s1", None)]
    finally:
        await backend.close()


async def test_forget_counts_a_session_the_server_lost_as_deleted_and_keeps_one_it_could_not_delete(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        await _events(backend, conversation="lost", request_id="one")
        await _events(backend, conversation="kept", request_id="two")
        fake.live.discard("s1")
        await backend.forget("lost")
        assert "lost" not in backend._sessions

        fake.delete_fails = True
        with pytest.raises(adapter.WireError):
            await backend.forget("kept")
        assert backend._sessions["kept"] == "s2" and "s2" in fake.live
    finally:
        await backend.close()


async def test_retained_reads_the_server(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        assert not await backend.retained("c1")
        await _events(backend, conversation="c1", request_id="r1")
        assert await backend.retained("c1")
        # Removed from the server by other means: the map still names it, but it is not retained.
        fake.live.clear()
        assert backend._sessions["c1"] == "s1" and not await backend.retained("c1")
    finally:
        await backend.close()


async def test_continuation_of_a_lost_conversation_fails(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        assert OP_CONTINUE in backend.capabilities().operations
        await _events(backend, conversation="c1", request_id="r1")
        continued = [event async for event in backend.run_turn(TurnRequest(
            conversation="c1", request_id="r2", text="next", existing_conversation_only=True,
        ))]
        assert continued[-1].kind != EVENT_FAILED
        posts = [call for call in fake.calls if call[0] == "POST" and call[1] == "/sessions"]
        fake.live.clear()
        missing = [event async for event in backend.run_turn(TurnRequest(
            conversation="c1", request_id="r3", text="must not create", existing_conversation_only=True,
        ))]
        assert [(event.kind, event.text) for event in missing] == [(EVENT_FAILED, CONVERSATION_UNAVAILABLE)]
        assert "backend_session" not in missing[-1].data  # the turn had no session
        assert [call for call in fake.calls if call[0] == "POST" and call[1] == "/sessions"] == posts
    finally:
        await backend.close()


def _in_session(turn: list[dict], session: str) -> list[dict]:
    """A scripted turn's events as Docker Agent streams them for another session."""
    return [{**event, "session_id": session} if "session_id" in event else event for event in turn]


async def test_a_turn_whose_session_the_server_lost_runs_in_a_new_one(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN, _in_session(TEXT_TURN, "s2"))
    backend = await _started_backend(fake, state=tmp_path)
    try:
        await _events(backend, conversation="c1", request_id="r1")
        # The server lost it while Belle ran: the permissions are set, so the turn request is the first call.
        fake.live.clear()
        events = await _events(backend, conversation="c1", request_id="r2")
        assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "hello")
        assert backend._sessions["c1"] == "s2"
    finally:
        await backend.close()

    # The server lost it while Belle was stopped: the session map in the state names it.
    restarted = FakeDockerAgent(_in_session(TEXT_TURN, "s3"))
    restarted.created = 2
    backend = await _started_backend(restarted, state=tmp_path)
    try:
        events = await _events(backend, conversation="c1", request_id="r3")
        assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "hello")
    finally:
        await backend.close()
    assert [path for _, path, _ in restarted.calls if path.endswith("/agent/belle")] == ["/sessions/s3/agent/belle"]
    assert json.loads((tmp_path / adapter.SESSIONS_FILE).read_text(encoding="utf-8"))["sessions"] == {"c1": "s3"}
    turn = next(body for _, path, body in restarted.calls if path.endswith("/agent/belle"))
    assert turn["messages"][0]["content"] == "hi"


async def test_a_continuation_whose_session_is_lost_after_the_check_fails_and_makes_none(tmp_path, monkeypatch):
    fake = FakeDockerAgent(TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        await _events(backend, conversation="c1", request_id="r1")
        # The server loses the session between the check that it is retained and the turn.
        fake.live.clear()

        async def retained(conversation):
            return True

        monkeypatch.setattr(backend, "retained", retained)
        events = await _events(backend, conversation="c1", request_id="r2", existing_conversation_only=True)
        assert [(event.kind, event.text) for event in events] == [(EVENT_FAILED, CONVERSATION_UNAVAILABLE)]
        assert "c1" not in backend._sessions and fake.created == 1
    finally:
        await backend.close()


async def test_a_turn_the_server_refuses_on_a_session_it_has_fails_and_keeps_the_session(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        await _events(backend, conversation="c1", request_id="r1")
        fake.run_fails = True
        events = await _events(backend, conversation="c1", request_id="r2")
        assert events[-1].kind == EVENT_FAILED and "HTTP 500" in events[-1].text
        assert backend._sessions["c1"] == "s1" and fake.created == 1
    finally:
        await backend.close()


async def test_a_call_held_for_a_person_is_one_approval_and_the_answer_resumes_or_refuses_it():
    call = {"id": "call_1", "type": "function", "function": {"name": "belle_echo", "arguments": '{"text": "T"}'}}

    def held(session: str) -> list[dict]:
        confirmation = {"type": "tool_call_confirmation", "session_id": session, "tool_call": call}
        return [*_started(), confirmation, {"type": "agent_choice", "session_id": "s1", "content": "done"}, STOPPED]

    # The second turn's call is a subagent's: its session asks, and the turn's session resumes.
    fake = FakeDockerAgent(held("s1"), held("s9"))
    backend = await _started_backend(fake)
    asked = []
    try:
        for request_id, answer in (("r1", Answer(approved=True)), ("r2", Answer(values=(REJECT,)))):
            async for event in backend.run_turn(TurnRequest(conversation="c1", request_id=request_id, text="hi")):
                if event.kind == EVENT_QUESTION:
                    asked.append(event.question)
                    assert await backend.pending("c1") == [event.question]
                    await backend.answer("c1", "another-question", answer)  # nothing waits for it
                    await backend.answer("c1", event.question.id, answer)
                    assert await backend.pending("c1") == []
            assert (event.kind, event.text) == (EVENT_SUCCEEDED, "done")
        await backend.answer("c1", "call_1", Answer(approved=True))  # the turn is over
    finally:
        await backend.close()

    expected = Question(
        id="call_1", text='Allow echo: {"text": "T"}', options=(APPROVE, REJECT), kind=APPROVAL, tool="echo"
    )
    assert asked == [expected, expected]
    assert [(method, path, body) for method, path, body in fake.calls if path.endswith("/resume")] == [
        ("POST", "/sessions/s1/resume", {"confirmation": "approve"}),
        ("POST", "/sessions/s1/resume", {"confirmation": "reject", "reason": adapter.REFUSED}),
    ]


async def test_a_stream_that_breaks_fails_the_turn_once_as_lost_and_an_unreached_server_is_named():
    fake = FakeDockerAgent()

    def server(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/agent/belle"):
            return fake(request)
        if len(fake.turn_headers) == 1:
            raise httpx.ConnectError("")
        fake.turn_headers.append(dict(request.headers))

        async def broken():
            for event in _started():
                yield f"data: {json.dumps(event)}\n\n".encode()
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

        return httpx.Response(200, content=broken(), headers={"content-type": "text/event-stream"})

    backend = await _started_backend(server)
    try:
        broken = await _events(backend, request_id="r1")
        unreached = await _events(backend, request_id="r2")
    finally:
        await backend.close()

    assert [(event.kind, event.text) for event in broken if event.terminal] == [(EVENT_FAILED, adapter.STREAM_LOST)]
    assert [(event.kind, event.text) for event in unreached if event.terminal] == [
        (EVENT_FAILED, "The turn could not run on Docker Agent: ConnectError")
    ]


async def test_a_turn_request_carries_the_tool_token_and_a_binding_only_its_turn_holds():
    fake = FakeDockerAgent(TEXT_TURN)
    backend = await _started_backend(fake)
    try:
        await _events(backend)
        headers = fake.turn_headers[0]
        assert headers["x-belle-tools-token"] == backend._tool_server.token
        # Once the turn ended its binding names no conversation.
        assert await backend._conversation_of({}, {"x-belle-turn": headers["x-belle-turn"]}) == ""
    finally:
        await backend.close()


async def test_a_request_that_ran_starts_nothing_even_after_a_restart(tmp_path):
    fake = FakeDockerAgent(TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        await _events(backend, request_id="r1")
        again = await _events(backend, request_id="r1")
    finally:
        await backend.close()
    restarted = await _started_backend(fake, state=tmp_path)
    try:
        after_restart = await _events(restarted, request_id="r1")
    finally:
        await restarted.close()

    assert [(event.kind, dict(event.data)) for event in again] == [(EVENT_FAILED, {DUPLICATE: True})]
    assert [(event.kind, dict(event.data)) for event in after_restart] == [(EVENT_FAILED, {DUPLICATE: True})]
    assert len(fake.turn_headers) == 1


def _said(role: str, text: str, **fields: Any) -> dict[str, Any]:
    """One message as `GET /api/sessions/:id` lists it."""
    return {"agent_name": "root", "message": {"role": role, "content": text}, **fields}


def _session_reads(fake: FakeDockerAgent) -> list[tuple[str, str]]:
    """The calls that read or add to a session's messages, and the turn requests, in order."""
    return [
        (method, path)
        for method, path, _ in fake.calls
        if path == "/sessions/s1" or path.endswith(("/messages", "/agent/belle"))
    ]


@pytest.mark.parametrize(
    "last, answered",
    [
        (_said("user", "hi"), True),
        # The run went on to its answer in the server after its stream was lost.
        (_said("assistant", "hello"), False),
        # The task that started a subagent's session is Docker Agent's own message.
        (_said("user", "the task", implicit=True), False),
    ],
    ids=["unanswered", "answered-in-the-server", "a-subagents-task"],
)
async def test_the_message_a_failed_run_left_unanswered_is_answered_before_the_next_run_even_after_a_restart(
    tmp_path, last, answered
):
    lost = [*_started(), {"type": "agent_choice", "session_id": "s1", "content": "cut"}]
    fake = FakeDockerAgent(lost, TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        assert (await _events(backend, request_id="r1"))[-1].text == adapter.STREAM_LOST
    finally:
        await backend.close()
    fake.history["s1"] = [last]
    fake.calls.clear()

    restarted = await _started_backend(fake, state=tmp_path)
    try:
        assert (await _events(restarted, request_id="r2"))[-1].kind == EVENT_SUCCEEDED
        # The run succeeded: the next one does not read the session first.
        await _events(restarted, request_id="r3")
    finally:
        await restarted.close()

    note = [("POST", "/sessions/s1/messages")] if answered else []
    turn = ("POST", "/sessions/s1/agent/belle")
    assert _session_reads(fake) == [("GET", "/sessions/s1"), *note, turn, turn]
    if answered:
        assert fake.history["s1"][-1] == _said("assistant", adapter.INTERRUPTED)


@pytest.mark.parametrize("state", ["written-before-the-record", "rebuilt-from-titles"])
async def test_a_session_map_without_a_record_of_its_unsettled_conversations_reads_each_session_once(tmp_path, state):
    if state == "written-before-the-record":
        stored = {"prefix": "belle::", "sessions": {"c1": "s1"}}
        (tmp_path / adapter.SESSIONS_FILE).write_text(json.dumps(stored), encoding="utf-8")
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN, sessions=[{"id": "s1", "title": "belle::c1", "created_at": "1"}])
    fake.history["s1"] = [_said("user", "hi")]
    backend = await _started_backend(fake, state=tmp_path)
    try:
        await _events(backend, request_id="r1")
        await _events(backend, request_id="r2")
    finally:
        await backend.close()

    turn = ("POST", "/sessions/s1/agent/belle")
    assert _session_reads(fake) == [("GET", "/sessions/s1"), ("POST", "/sessions/s1/messages"), turn, turn]


async def test_a_request_id_names_a_request_in_one_conversation_and_an_older_entry_still_counts(tmp_path):
    # An entry written when the store held request ids alone.
    (tmp_path / adapter.REQUESTS_FILE).write_text(json.dumps({"version": 1, "events": ["r0"]}), encoding="utf-8")
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake, state=tmp_path)
    try:
        first = await _events(backend, conversation="loopback_W1_c1", request_id="r1")
        elsewhere = await _events(backend, conversation="slack_W1_c1", request_id="r1")
        older = await _events(backend, conversation="slack_W1_c1", request_id="r0")
    finally:
        await backend.close()

    # The second conversation's request reached the server: it was no duplicate of the first.
    assert first[-1].kind != EVENT_FAILED and not any(DUPLICATE in event.data for event in elsewhere)
    assert [(event.kind, dict(event.data)) for event in older] == [(EVENT_FAILED, {DUPLICATE: True})]
    assert len(fake.turn_headers) == 2


def _file(name: str, mime: str, content: bytes) -> dict[str, str]:
    return {"name": name, "uri": f"data:{mime};base64,{base64.b64encode(content).decode()}"}


FILES = (
    _file("notes.md", "text/markdown", "café".encode()),
    _file("chart.png", "image/png", b"\x89PNG"),
    _file("report.pdf", "application/pdf", b"%PDF"),
)


async def test_files_reach_the_model_inline_where_it_takes_their_type_and_the_turn_is_told_of_the_others():
    models = {"default": {"capabilities": {"image": True}}, "second": {}}
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN, TEXT_TURN, models=models)
    backend = await _started_backend(fake)
    try:
        await _events(backend, "see these", files=FILES)
        await _events(backend, "see these", request_id="r2", files=FILES, scope=TurnScope(model="second"))
        await _events(backend, "no files", request_id="r3")
        capabilities = backend.capabilities()
    finally:
        await backend.close()

    (default, second, plain) = [body["messages"] for method, path, body in fake.calls if path.endswith("/agent/belle")]
    text, *documents = default[0]["multi_content"]
    assert [(d["document"]["name"], d["document"]["mime_type"], d["document"]["source"]) for d in documents] == [
        ("notes.md", "text/markdown", {"inline_text": "café"}),
        ("chart.png", "image/png", {"inline_data": base64.b64encode(b"\x89PNG").decode()}),
    ]
    assert text == {"type": "text", "text": default[0]["content"]}
    assert text["text"] == f"see these\n\n{adapter.NOT_PASSED}: report.pdf (application/pdf)."
    # A model with no capabilities in the YAML reads text only.
    assert [d["document"]["name"] for d in second[0]["multi_content"][1:]] == ["notes.md"]
    assert "chart.png (image/png), report.pdf (application/pdf)." in second[0]["content"]
    assert plain == [{"role": "user", "content": "no files"}]
    # A tool result's content is passed by the same capabilities.
    assert capabilities.tool_content == {"text/*", "image/*"}
    assert capabilities.content_for("second") == {"text/*"}


@pytest.mark.parametrize("text, files", [("see these", FILES[:2]), ("x" * 400, ())])
async def test_a_request_larger_than_the_operator_limit_is_refused_without_submitting(text, files):
    models = {"default": {"capabilities": {"image": True}}}
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN, models=models)
    backend = await _started_backend(fake, max_request_bytes=300)
    try:
        refused = await _events(backend, text, files=files)
        await _events(backend, "see one", request_id="r2", files=FILES[:1])
    finally:
        await backend.close()

    assert [event.kind for event in refused] == [EVENT_FAILED]
    assert "backends.docker_agent.max_request_bytes (300)" in refused[0].text
    assert "Nothing was submitted" in refused[0].text
    (smaller,) = [body["messages"][0] for method, path, body in fake.calls if path.endswith("/agent/belle")]
    assert [part["type"] for part in smaller["multi_content"]] == ["text", "document"]


async def test_the_catalog_lists_the_yaml_models_default_first_the_packaged_skills_and_the_subagents():
    backend = await _started_backend(FakeDockerAgent())
    try:
        catalog = await backend.catalog()
    finally:
        await backend.close()

    assert catalog.models.names == ("default", "second")
    assert catalog.subagents == ("helper",)
    assert "chat-digest" in {skill["name"] for skill in catalog.skills}


async def test_a_refused_server_token_ends_the_start_and_says_so():
    backend = DockerAgentBackend(
        "http://docker-agent.test:8080",
        TOKEN,
        relay_token="relay",
        state_dir=Path(tempfile.mkdtemp()),
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "unauthorized"})),
    )
    with pytest.raises(DockerAgentError, match="refused the start: GET /ping: HTTP 401"):
        await backend.start(ToolHost([]))


async def test_each_turn_names_its_model_and_the_session_gets_the_scopes_tool_levels_when_they_change():
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake)
    try:
        await _events(
            backend, request_id="r1", scope=TurnScope(model="second", tools={"echo": "deny", "read_skill": "ask"})
        )
        await _events(backend, request_id="r2")
        await _events(backend, request_id="r3")
    finally:
        await backend.close()

    # A model named for one turn stays on a Docker Agent session, so the next turn names the default again.
    assert [body["model"] for _, path, body in fake.calls if path.endswith("/agent/belle")] == [
        "second",
        "default",
        "default",
    ]
    # Belle's tool under the name Docker Agent gives it. The third turn's levels are the second's: nothing to set.
    assert [body for method, _, body in fake.calls if method == "PATCH"] == [
        {"permissions": {"deny": ["belle_echo"], "ask": ["read_skill"]}},
        {"permissions": {"deny": [], "ask": []}},
    ]


@pytest.mark.parametrize(
    ("root", "missing"),
    [
        ({**ROOT, "hooks": {"user_prompt_submit": [{}], "skill_content_guard": [{}]}}, "tool_guard"),
        ({**ROOT, "routing": {"allowed_agents": ["translator"]}}, "before_agent_run"),
        # The expert is not in the YAML, so it relays nothing.
        (
            {
                **ROOT,
                "routing": {"allowed_agents": ["translator"]},
                "hooks": {**ROOT["hooks"], "before_agent_run": [{}]},
            },
            "skill_content_guard, tool_guard, user_prompt_submit",
        ),
        ({**ROOT, "sub_agents": ["bare"]}, "skill_content_guard, tool_guard, user_prompt_submit"),
    ],
    ids=["no-tool-guard", "experts-without-routing-hook", "expert-without-hooks", "subagent-without-hooks"],
)
async def test_an_agent_that_does_not_relay_belles_hooks_is_refused_at_start(root, missing):
    with pytest.raises(DockerAgentError, match=f"does not relay these hooks to Belle: {missing}"):
        await _started_backend(FakeDockerAgent(root=root))


async def test_a_message_steered_into_a_running_turn_goes_to_its_session_and_none_is_taken_otherwise():
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake)

    def steered(text: str) -> TurnRequest:
        return TurnRequest(conversation="c1", request_id=f"steer-{text}", text=text)

    taken = []
    try:
        taken.append(await backend.steer(steered("before")))
        for request_id, status in (("r1", 202), ("r2", 409)):
            fake.steer_status = status
            async for event in backend.run_turn(TurnRequest(conversation="c1", request_id=request_id, text="hi")):
                if event.kind == EVENT_STEP:
                    taken.append(await backend.steer(steered(f"during {request_id}")))
        taken.append(await backend.steer(steered("after")))
    finally:
        await backend.close()

    assert taken == [False, True, False, False]
    assert [(path, body) for method, path, body in fake.calls if path.endswith("/steer")] == [
        ("/sessions/s1/steer", {"messages": [{"role": "user", "content": "during r1"}]}),
        ("/sessions/s1/steer", {"messages": [{"role": "user", "content": "during r2"}]}),
    ]


@pytest.mark.parametrize(("depth", "sent"), [(1, []), (0, [{"role": "user", "content": "b"}])], ids=["held", "lost"])
async def test_a_steered_message_the_turn_did_not_read_is_named_at_its_end_and_sent_again_only_if_lost(depth, sent):
    fake = FakeDockerAgent(TEXT_TURN, TEXT_TURN)
    backend = await _started_backend(fake)
    try:
        async for event in backend.run_turn(TurnRequest(conversation="c1", request_id="r1", text="hi")):
            if event.kind == EVENT_STEP:
                for text in ("a", "b"):
                    await backend.steer(TurnRequest(conversation="c1", request_id=f"steer-{text}", text=text))
                # Docker Agent read the first before its last model call. The second stays in its queue.
                fake.steer_depth = 1
        # The session lost its queue before the next turn, or did not.
        fake.steer_depth = depth
        await _events(backend, text="b", request_id="steer-b")
    finally:
        await backend.close()

    assert event.data[NOT_STEERED] == ["steer-b"]
    turns = [body for method, path, body in fake.calls if path.endswith("/agent/belle")]
    assert turns[1]["messages"] == sent


@pytest.mark.parametrize(
    ("root", "others"),
    [
        ({**ROOT, "hooks": {name: [{}] for name in hooks.RELAYED}}, ()),
        (
            {
                **ROOT,
                "routing": {"allowed_agents": ["translator"]},
                "hooks": {**ROOT["hooks"], "before_agent_run": [{}]},
            },
            ({**ROOT, "Name": "translator", "hooks": {name: [{}] for name in hooks.RELAYED}},),
        ),
    ],
    ids=["entry-agent", "expert"],
)
async def test_an_agent_yaml_that_does_not_relay_the_steering_hook_declares_no_steer(root, others):
    steers = await _started_backend(FakeDockerAgent())
    queues = await _started_backend(FakeDockerAgent(root=root, others=others))
    try:
        assert OP_STEER in steers.capabilities().operations
        assert OP_STEER not in queues.capabilities().operations
    finally:
        await steers.close()
        await queues.close()


async def test_the_experts_and_subagents_the_yaml_declares_are_listed_and_a_scope_may_name_an_expert():
    plain = await _started_backend(FakeDockerAgent())
    routed = {
        **ROOT,
        "sub_agents": [],
        "routing": {"allowed_agents": ["translator"]},
        "hooks": {**ROOT["hooks"], "before_agent_run": [{}]},
    }
    experts = await _started_backend(FakeDockerAgent(root=routed, others=({**ROOT, "Name": "translator"},)))
    try:
        assert "expert" not in plain.capabilities().scope_fields
        assert (await plain.catalog()).experts == ()
        assert CATALOG_SUBAGENTS in plain.capabilities().catalog
        assert CATALOG_SUBAGENTS not in experts.capabilities().catalog
        assert "expert" in experts.capabilities().scope_fields
        assert CATALOG_EXPERTS in experts.capabilities().catalog
        assert (await experts.catalog()).experts == ("translator",)
    finally:
        await plain.close()
        await experts.close()


PLAIN = TurnScope()
ALLOWED = ToolVerdict.allow()


def _answer(
    event: dict, scope: TurnScope = PLAIN, verdict: ToolVerdict = ALLOWED, running: bool = True
) -> tuple[dict, list]:
    """The hook answer for ``event`` in a turn of ``scope``, and the calls the tool host was asked about."""
    asked = []

    def authorize(call):
        asked.append(call)
        return verdict

    turn = ("c1", scope) if running else None
    return hooks.answer(event, turn, authorize, lambda name: {"belle_echo": "echo"}.get(name, name), "root"), asked


def _guard(tool: str, **arguments) -> dict:
    return {"hook_event_name": "tool_guard", "tool_name": tool, "tool_input": arguments, "tool_use_id": "t1"}


def _decision(answer: dict) -> str:
    return answer.get("hook_specific_output", {}).get("permission_decision", answer.get("decision", ""))


QUESTION = ToolVerdict.ask(Question(id="q1", text="Allow echo to run?"))


@pytest.mark.parametrize("name", sorted(hooks.CONTEXT_HOOKS))
def test_the_prompt_and_steering_hooks_carry_the_system_additions_of_their_turn_only(name):
    prompt = {"hook_event_name": name}
    assert _answer(prompt, TurnScope(system_additions=("one", "two")))[0] == {
        "hook_specific_output": {"hook_event_name": name, "additional_context": "one\n\ntwo"}
    }
    assert _answer(prompt)[0] == {}
    assert _answer(prompt, running=False)[0] == {}


@pytest.mark.parametrize(
    ("verdict", "scope", "expected"),
    [
        (ToolVerdict.allow(), TurnScope(), ""),
        (ToolVerdict.deny("echo is denied here"), TurnScope(), "deny"),
        (QUESTION, TurnScope(tools={"echo": "ask"}), "ask"),
        # A question of the tool's own: the tool server holds the call and asks the person.
        (QUESTION, TurnScope(), ""),
    ],
    ids=["allow", "deny", "scope-ask", "tool-question"],
)
def test_the_tool_guard_answers_with_the_tool_hosts_verdict_on_the_call(verdict, scope, expected):
    answer, asked = _answer(_guard("belle_echo", text="T"), scope, verdict)

    assert _decision(answer) == expected
    assert [(call.conversation, call.name, dict(call.arguments), call.call_id) for call in asked] == [
        ("c1", "echo", {"text": "T"}, "t1")
    ]


def test_a_subagent_the_scope_does_not_allow_is_refused_before_the_tool_host_is_asked():
    scope = TurnScope(subagents_available=frozenset({"helper"}))
    refused, asked = _answer(_guard("transfer_task", agent="other", task="x"), scope)
    allowed, _ = _answer(_guard("transfer_task", agent="helper", task="x"), scope)

    assert (_decision(refused), asked) == ("deny", [])
    assert _decision(allowed) == ""


@pytest.mark.parametrize(
    ("available", "unavailable", "expected"),
    [
        (None, frozenset(), {"continue": True}),
        (frozenset({"chat-digest"}), frozenset(), {"continue": True}),
        (frozenset({"other"}), frozenset(), "block"),
        (None, frozenset({"chat-digest"}), "block"),
        (frozenset({"chat-digest"}), frozenset({"chat-digest"}), "block"),
        (None, frozenset({"other"}), {"continue": True}),
    ],
    ids=["no-restriction", "listed", "not-listed", "unavailable", "listed-and-unavailable", "another-unavailable"],
)
def test_the_skill_guard_lets_a_skill_load_only_where_the_scope_allows_it(available, unavailable, expected):
    answer, _ = _answer(
        {"hook_event_name": "skill_content_guard", "skill": {"name": "chat-digest"}},
        TurnScope(skills_available=available, skills_unavailable=unavailable),
    )
    assert answer == expected or answer.get("decision") == expected


def test_the_turn_is_routed_to_its_expert_once_from_the_entry_agent():
    scope = TurnScope(expert="translator")
    route = {"hook_specific_output": {"transition": {"action": "route", "agent": "translator"}}}

    assert _answer({"hook_event_name": "before_agent_run", "agent_name": "root"}, scope)[0] == route
    assert _answer({"hook_event_name": "before_agent_run", "agent_name": "translator"}, scope)[0] == {}
    assert _answer({"hook_event_name": "before_agent_run", "agent_name": "root"})[0] == {}


@pytest.mark.parametrize("event", ["tool_guard", "skill_content_guard", "before_agent_run"])
def test_a_guard_outside_a_belle_turn_refuses(event):
    answer, asked = _answer({"hook_event_name": event, "tool_name": "belle_echo"}, running=False)
    assert _decision(answer) in ("deny", "block")
    assert asked == []


def _config(tmp_path, section: str) -> dict:
    path = tmp_path / "belle.yaml"
    path.write_text(
        f"platforms: {{loopback: {{enabled: true}}}}\nbackend: docker_agent\nbackends:\n  docker_agent: {section}\n", encoding="utf-8"
    )
    from belle_agent.config import load_config

    return load_config(path)


def test_the_launcher_selects_the_backend_and_its_tokens_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_AGENT_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("BELLE_RELAY_TOKEN", "relay")

    backend = launcher.create_backend(
        _config(tmp_path, "{url: 'http://docker-agent.test:8080', relay: {port: 18200}}"), tmp_path
    )

    assert backend.capabilities().backend == "docker_agent"


@pytest.mark.parametrize(
    ("section", "unset", "message"),
    [
        ("{url: 'http://docker-agent.test:8080'}", "DOCKER_AGENT_AUTH_TOKEN", "DOCKER_AGENT_AUTH_TOKEN is not set"),
        ("{url: 'http://docker-agent.test:8080'}", "BELLE_RELAY_TOKEN", "BELLE_RELAY_TOKEN is not set"),
        ("{url: 'http://docker-agent.test:8080', token: x}", "", "a secret is never written"),
        ("{url: 'http://docker-agent.test:8080', agent: other}", "", "unknown keys: agent"),
        ("{url: 'http://a:b@docker-agent.test:8080'}", "", "with no secret"),
        ("{url: ''}", "", "must be the http address"),
        ("{url: 'http://docker-agent.test:8080', max_request_bytes: 1MiB}", "", "must be a number of bytes"),
    ],
)
def test_a_configuration_that_cannot_build_the_backend_says_why(tmp_path, monkeypatch, section, unset, message):
    monkeypatch.setenv("DOCKER_AGENT_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("BELLE_RELAY_TOKEN", "relay")
    if unset:
        monkeypatch.delenv(unset)

    with pytest.raises(BelleConfigError, match=message):
        launcher.create_backend(_config(tmp_path, section), tmp_path)
