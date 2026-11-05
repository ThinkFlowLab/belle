"""Permission requests become held questions, and Belle's tools run under the conversation of their binding."""

import asyncio
import json

import httpx
import pytest
import scripted_agent
from acp import RequestError
from acp.schema import NewSessionResponse, PermissionOption
from acp_kit import TOKEN, bind, collect
from scripted_agent import SESSION_META, TURN_HEADER, Prompt, ScriptedAgent, Session, result_text

from belle_agent.backend import (
    APPROVAL,
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_QUESTION,
    EVENT_SUCCEEDED,
    Answer,
    TurnRequest,
)
from belle_agent.tools.host import APPROVE, REJECT


class Action:
    """The side effect a permission guards: it runs only on an ``allow`` answer."""

    def __init__(self, agent, *, after_reject: str = "end_turn") -> None:
        self.ran: list[str] = []
        self.answers: list[str] = []
        self.after_reject = after_reject
        agent.on_prompt = self.prompt

    async def prompt(self, prompt):
        response = await prompt.ask()
        outcome = response.outcome
        self.answers.append(getattr(outcome, "option_id", outcome.outcome))
        if self.answers[-1] == "allow":
            self.ran.append(prompt.session.id)
        if outcome.outcome == "cancelled" or prompt.cancelled:
            return "cancelled"
        if self.answers[-1] == "reject" and self.after_reject == "cancelled":
            return "cancelled"  # as OpenCode ends a turn whose action a person refused
        await prompt.say("done")
        return "end_turn"


async def asked(stream):
    async for event in stream:
        if event.kind == EVENT_QUESTION:
            return event.question
    raise AssertionError("no question")


async def test_an_approved_action_runs_once(agent, backend):
    action = Action(agent)
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    question = await asked(stream)
    assert (question.kind, question.options, question.tool) == (APPROVAL, (APPROVE, REJECT), "touch /tmp/x")
    assert await backend.pending("c1") == [question]
    await backend.answer("c1", question.id, Answer(values=(APPROVE,)))
    assert [event async for event in stream][-1].kind == EVENT_SUCCEEDED
    assert action.answers == ["allow"] and len(action.ran) == 1


async def test_a_rejected_action_does_not_run_and_the_turn_goes_on(agent, backend):
    action = Action(agent)
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    await backend.answer("c1", (await asked(stream)).id, Answer(approved=False))
    assert [event async for event in stream][-1].kind == EVENT_SUCCEEDED
    assert (action.answers, action.ran) == (["reject"], [])


async def test_a_turn_the_agent_cancels_after_a_rejection_is_interrupted_after_the_reject(agent, backend):
    action = Action(agent, after_reject="cancelled")
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    await backend.answer("c1", (await asked(stream)).id, Answer(approved=False))
    ended = [event async for event in stream][-1]
    assert (ended.kind, ended.data) == (EVENT_INTERRUPTED, {"stop_reason": "cancelled", "after": "reject"})
    assert action.ran == []


async def test_an_unanswered_request_expires_cancelled_and_a_late_answer_runs_nothing(agent, make_backend):
    backend = await make_backend(agent.path, permission_timeout=0.2)
    action = Action(agent)
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    question = await asked(stream)
    rest = [event async for event in stream]
    assert rest[-1].kind == EVENT_FAILED  # the agent cancelled the turn on its own
    await backend.answer("c1", question.id, Answer(approved=True))
    assert (action.answers, action.ran) == (["cancelled"], [])


async def test_a_cancel_answers_the_held_request_cancelled(agent, backend):
    action = Action(agent)
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    question = await asked(stream)
    assert await backend.cancel("c1") is True
    ended = [event async for event in stream][-1]
    assert (ended.kind, ended.data["after"]) == (EVENT_INTERRUPTED, "cancel")
    assert (action.answers, action.ran) == (["cancelled"], [])
    # An answer after the turn ended runs nothing.
    await backend.answer("c1", question.id, Answer(approved=True))
    assert action.ran == [] and await backend.pending("c1") == []


async def test_a_request_the_agent_withdraws_is_answered_cancelled(agent, backend):
    answers = []

    async def withdraws(prompt):
        asking = asyncio.ensure_future(prompt.ask())
        await asyncio.sleep(0.1)
        rpc_id = prompt._connection.client._conn._next_request_id - 1
        line = {"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"requestId": rpc_id}}
        prompt._connection.writer.write(json.dumps(line).encode() + b"\n")
        answers.append((await asking).outcome.outcome)
        return "cancelled"

    agent.on_prompt = withdraws
    events = await collect(backend)
    assert answers == ["cancelled"]
    assert [event.kind for event in events if event.terminal] == [EVENT_FAILED]  # the agent cancelled on its own


async def test_a_request_without_a_plain_approve_and_reject_is_refused_and_asks_nobody(agent, backend):
    errors = []

    async def ambiguous(prompt):
        options = [PermissionOption(option_id="always", name="Always", kind="allow_always")]
        try:
            await prompt.ask(options=options)
        except RequestError as exc:
            errors.append(exc.data["details"])
        return "end_turn"

    agent.on_prompt = ambiguous
    events = await collect(backend)
    assert EVENT_QUESTION not in [event.kind for event in events]
    assert errors == [
        "Belle answers a permission request that offers allow_once and reject_once. This one offers: allow_always"
    ]


async def test_a_client_method_belle_does_not_offer_is_refused_by_name(agent, backend):
    errors = []

    async def reads(prompt):
        try:
            await prompt._connection.client.read_text_file(path="/etc/passwd", session_id=prompt.session.id)
        except RequestError as exc:
            errors.append((exc.code, exc.data["method"]))
        try:
            await prompt._connection.client.create_terminal(command="sh", session_id=prompt.session.id)
        except RequestError as exc:
            errors.append((exc.code, exc.data["method"]))
        return "end_turn"

    agent.on_prompt = reads
    await collect(backend)
    assert errors == [(-32601, "fs/read_text_file"), (-32601, "terminal/create")]


# ── Belle's tools ────────────────────────────────────────────────────────


def posting(results: dict):
    async def script(prompt):
        await asyncio.sleep(0.05)  # the two turns overlap
        # The model writes a conversation into the arguments: it chooses nothing.
        answer = await prompt.call_tool("post", {"text": prompt.text, "conversation": "c-forged"})
        results[prompt.text] = answer
        await prompt.say("posted")
        return "end_turn"

    return script


async def test_two_sessions_on_one_connection_each_call_under_their_own_conversation(agent, backend, tools, toolkit):
    results = {}
    agent.on_prompt = posting(results)
    bind(tools, "c1"), bind(tools, "c2")
    await asyncio.gather(collect(backend, "c1", text="from c1"), collect(backend, "c2", text="from c2"))
    assert sorted(toolkit.posts) == [("from c1", "c1"), ("from c2", "c2")]
    assert all(result_text(answer)["ok"] for answer in results.values())


async def test_a_session_binding_is_revoked_when_its_turn_ends(agent, backend, tools, toolkit):
    agent.on_prompt = posting({})
    bind(tools, "c1")
    await collect(backend, "c1", text="in turn")
    session = agent.prompts[0][0]
    body = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "post", "arguments": {"text": "after"}, "_meta": {SESSION_META: session}},
    }
    async with httpx.AsyncClient() as client:
        answer = (
            await client.post(
                backend._tools_url, json=body, headers={"Authorization": f"Bearer {backend._tool_server.token}"}
            )
        ).json()
    assert result_text(answer)["ok"] is False
    assert toolkit.posts == [("in turn", "c1")]


async def test_two_agent_processes_each_call_under_their_own_binding(socket_dir, make_backend, tools, toolkit):
    agent = ScriptedAgent(f"{socket_dir}/da.sock", binding_line=True, tools_token=TOKEN)
    await agent.start()
    try:
        backend = await make_backend(agent.path, header=TURN_HEADER)
        agent.tools_url = backend._tools_url
        agent.on_prompt = posting({})
        bind(tools, "c1"), bind(tools, "c2")
        await asyncio.gather(collect(backend, "c1", text="from c1"), collect(backend, "c2", text="from c2"))
        assert sorted(toolkit.posts) == [("from c1", "c1"), ("from c2", "c2")]
        # A probe at start, then one connection, and so one agent process, per conversation.
        assert len(agent.bindings) == 3 and len(set(agent.bindings)) == 3

        # A binding is honoured only while its conversation's turn runs.
        url = backend._tools_url
        body = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "post", "arguments": {"text": "x"}},
        }
        async with httpx.AsyncClient() as client:
            for binding in agent.bindings:
                headers = {"Authorization": f"Bearer {TOKEN}", TURN_HEADER: binding}
                assert result_text((await client.post(url, json=body, headers=headers)).json())["ok"] is False
            wrong = {"Authorization": "Bearer guess", TURN_HEADER: agent.bindings[1]}
            assert (await client.post(url, json=body, headers=wrong)).status_code == 401
        assert len(toolkit.posts) == 2
    finally:
        await agent.close()


async def test_a_session_mount_sends_the_binding_header_of_its_connection(socket_dir, make_backend, tools, toolkit):
    agent = ScriptedAgent(f"{socket_dir}/sm.sock", binding_line=True)
    await agent.start()
    try:
        backend = await make_backend(agent.path, header=TURN_HEADER, tools_mount="session")
        agent.on_prompt = posting({})
        bind(tools, "c1"), bind(tools, "c2")
        await asyncio.gather(collect(backend, "c1", text="from c1"), collect(backend, "c2", text="from c2"))
        assert sorted(toolkit.posts) == [("from c1", "c1"), ("from c2", "c2")]
    finally:
        await agent.close()


@pytest.mark.parametrize("restoring", [False, True])
async def test_session_setup_cannot_call_tools_before_its_prompt(
    socket_dir, make_backend, tools, toolkit, monkeypatch, restoring
):
    agent = ScriptedAgent(f"{socket_dir}/setup.sock", binding_line=True)
    await agent.start()
    calls = []
    creations = 0

    async def new_session(connection, cwd, mcp_servers=None, **kwargs):
        nonlocal creations
        creations += 1
        session = Session("ses_local", list(mcp_servers or ()))
        agent.sessions[session.id] = session
        if creations == 2:
            calls.append(await Prompt(agent, connection, session, "").call_tool("post", {"text": "setup"}))
        return NewSessionResponse(session_id=session.id)

    original_load = scripted_agent._Connection.load_session

    async def load_session(connection, cwd, session_id, mcp_servers=None, **kwargs):
        session = agent.sessions[session_id]
        session.mounts = list(mcp_servers or ())
        calls.append(await Prompt(agent, connection, session, "").call_tool("post", {"text": "replay"}))
        return await original_load(connection, cwd, session_id, mcp_servers, **kwargs)

    async def script(prompt):
        if prompt.text == "c1":
            await prompt.ask()
        else:
            calls.append(await prompt.call_tool("post", {"text": "in prompt"}))
        return "end_turn"

    monkeypatch.setattr(scripted_agent._Connection, "new_session", new_session)
    monkeypatch.setattr(scripted_agent._Connection, "load_session", load_session)
    agent.on_prompt = script
    stream = None
    try:
        backend = await make_backend(agent.path, header=TURN_HEADER, tools_mount="session")
        bind(tools, "c1"), bind(tools, "c2")
        if restoring:
            backend._state.set_session("c2", "ses_local")
        stream = backend.run_turn(TurnRequest("c1", "r1", "c1"))
        async with asyncio.timeout(10):
            question = await asked(stream)
            events = await collect(backend, "c2", "r2", "c2")
            await backend.answer("c1", question.id, Answer(approved=False))
            [event async for event in stream]
        assert events[-1].kind == EVENT_SUCCEEDED
        assert toolkit.posts == [("in prompt", "c2")]
        assert [call["result"]["isError"] for call in calls] == [True, False]
    finally:
        if stream is not None:
            await stream.aclose()
        await agent.close()


@pytest.mark.parametrize("header", ["", TURN_HEADER])
@pytest.mark.parametrize("stopped", ["cancelled", "completed"])
async def test_tools_are_revoked_before_a_stopped_turn_is_removed(
    socket_dir, make_backend, tools, toolkit, header, stopped
):
    agent = ScriptedAgent(f"{socket_dir}/late.sock", binding_line=bool(header))
    await agent.start()
    release = asyncio.Event()
    captured = asyncio.get_running_loop().create_future()

    async def script(prompt):
        await prompt.call_tool("post", {"text": "in prompt"})
        captured.set_result(prompt)
        await release.wait()
        return "end_turn"

    agent.on_prompt = script
    stream = None
    stopping = None
    try:
        backend = await make_backend(agent.path, header=header, tools_mount="session")
        bind(tools, "c1")
        stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
        await anext(stream)  # Pause the reader while the prompt and its callbacks continue.
        async with asyncio.timeout(10):
            prompt = await captured
            turn = backend._turns["c1"]
            if stopped == "cancelled":
                stopping = asyncio.create_task(backend.cancel("c1"))
                while not turn.cancelled:
                    await asyncio.sleep(0)
            else:
                release.set()
                await asyncio.wait({turn.prompt})
            assert "c1" in backend._turns
            answer = await prompt.call_tool("post", {"text": "after stop"})
            assert answer["result"]["isError"] is True
            assert toolkit.posts == [("in prompt", "c1")]
    finally:
        release.set()
        if stopping is not None:
            await stopping
        if stream is not None:
            await stream.aclose()
        await agent.close()


async def test_an_approval_during_stop_cannot_authorize_the_held_action(agent, backend, monkeypatch):
    action = Action(agent)
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    question = await asked(stream)
    stopping = asyncio.Event()
    release = asyncio.Event()
    original = backend._submitted

    async def paused(turn):
        if turn.cancelled:
            stopping.set()
            await release.wait()
        return await original(turn)

    monkeypatch.setattr(backend, "_submitted", paused)
    cancellation = asyncio.create_task(backend.cancel("c1"))
    try:
        async with asyncio.timeout(10):
            await stopping.wait()
            await backend.answer("c1", question.id, Answer(approved=True))
            release.set()
            assert await cancellation is True
            [event async for event in stream]
        assert action.answers == ["cancelled"]
        assert action.ran == []
    finally:
        release.set()
        await cancellation
        await stream.aclose()


async def test_a_permission_after_prompt_completion_is_cancelled_before_cleanup(agent, backend):
    captured = asyncio.get_running_loop().create_future()

    async def script(prompt):
        captured.set_result(prompt)
        return "end_turn"

    agent.on_prompt = script
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    try:
        async with asyncio.timeout(10):
            await anext(stream)
            prompt = await captured
            await asyncio.wait({backend._turns["c1"].prompt})
            assert "c1" in backend._turns
            assert (await prompt.ask()).outcome.outcome == "cancelled"
            assert await backend.pending("c1") == []
    finally:
        await stream.aclose()
