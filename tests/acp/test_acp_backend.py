"""The ACP backend against a scripted agent: turns, duplicates, files, cancellation and restored sessions."""

import asyncio
import base64
import json
import stat
import sys
from dataclasses import replace

import httpx
import pytest
import scripted_agent
from acp import RequestError
from acp_kit import TOKEN, bind, collect, kinds, settings
from scripted_agent import TURN_HEADER, ScriptedAgent, capabilities, say_twice

from belle_agent.backend import (
    CONVERSATION_UNAVAILABLE,
    DUPLICATE,
    EVENT_ACCEPTED,
    EVENT_FAILED,
    EVENT_INTERRUPTED,
    EVENT_QUESTION,
    EVENT_STEP,
    EVENT_SUCCEEDED,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    OP_CONTINUE,
    OP_FORGET,
    OP_HEALTH,
    OP_RECOVER,
    OP_STEER,
    TurnRequest,
    TurnScope,
)
from belle_agent.config import BelleConfigError
from belle_agent.integrations.acp import AcpBackend, create_backend
from belle_agent.integrations.acp.adapter import ALREADY_RUNNING, UNRESOLVED, AcpError


def data_uri(mime: str, content: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(content).decode()}"


async def first(stream, kind: str):
    """Read a stream until an event of ``kind``, and return it."""
    async for event in stream:
        if event.kind == kind:
            return event
    raise AssertionError(f"no {kind} event")


async def test_metadata_binding_cannot_start_independent_agent_processes(agent, tmp_path, tools):
    backend = AcpBackend(settings(agent.path, connection="per_conversation"), tmp_path / "s")
    with pytest.raises(AcpError, match="binding.meta needs connection: shared"):
        await backend.start(tools)
    assert agent.open == 0
    assert backend._tool_server is None


async def test_a_text_turn_streams_its_chunks_and_ends_once_with_the_whole_text(agent, backend):
    events = await collect(backend, text="the trusted envelope\nwith its lines")
    assert kinds(events) == [EVENT_ACCEPTED, EVENT_TEXT_DELTA, EVENT_TEXT_DELTA, EVENT_SUCCEEDED]
    assert events[-1].text == "Hello, world."
    assert agent.prompts[0][1] == "the trusted envelope\nwith its lines"


async def test_a_turn_with_a_tool_call_ends_with_the_text_after_it(agent, backend):
    async def narrated(prompt):
        await prompt.think("planning")
        await prompt.say("Let me check.", "m1")
        await prompt.tool("t1", "search", raw_input={"q": "x"})
        await prompt.tool_done("t1", "3 hits")
        await prompt.say("Three.", "m2")
        return "end_turn"

    agent.on_prompt = narrated
    events = await collect(backend)
    assert kinds(events) == [
        EVENT_ACCEPTED,
        EVENT_TEXT_DELTA,
        EVENT_TEXT_DELTA,
        EVENT_STEP,
        EVENT_TOOL_CALLED,
        EVENT_TOOL_RESULT,
        EVENT_STEP,
        EVENT_TEXT_DELTA,
        EVENT_SUCCEEDED,
    ]
    assert events[1].data == {"thinking": True}
    assert events[-1].text == "Three."


async def test_many_chunks_arrive_in_order_and_make_the_whole_final(agent, backend):
    async def burst(prompt):
        for index in range(2000):
            await prompt.say(f"{index},", "m1")
        return "end_turn"

    agent.on_prompt = burst
    events = await collect(backend)  # a reader that does not suspend
    expected = "".join(f"{index}," for index in range(2000))
    assert "".join(event.text for event in events if event.kind == EVENT_TEXT_DELTA) == expected
    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, expected)


@pytest.mark.parametrize("stop", ["max_tokens", "max_turn_requests", "refusal", "cancelled"])
async def test_a_stop_reason_other_than_end_turn_is_a_failure_that_names_it(agent, backend, stop):
    async def stops(prompt):
        await prompt.say("partial")
        return stop

    sessions = []

    async def stops_and_says_where(prompt):
        sessions.append(prompt.session.id)
        return await stops(prompt)

    agent.on_prompt = stops_and_says_where
    events = await collect(backend)
    assert kinds(events)[-1] == EVENT_FAILED and stop in events[-1].text
    # The stop reason is the failure's type, and the session leads to the agent's own record of it.
    assert events[-1].data == {"stop_reason": stop, "error_type": stop, "backend_session": sessions[0]}


async def test_an_agent_error_fails_the_turn_once(agent, backend):
    async def broken(prompt):
        raise RequestError.internal_error({"details": "provider said no"})

    sessions = []

    async def broken_where_it_says(prompt):
        sessions.append(prompt.session.id)
        await broken(prompt)

    agent.on_prompt = broken_where_it_says
    events = await collect(backend)
    assert [event.kind for event in events if event.terminal] == [EVENT_FAILED]
    # The state file records this text, and an error's data can echo a request with the tool token.
    assert "error -32603: Internal error" in events[-1].text and "provider said no" not in events[-1].text
    # The error's code is the failure's type. Its message and data stay out.
    assert (events[-1].data["error_type"], events[-1].data["backend_session"]) == ("-32603", sessions[0])
    assert "Internal error" not in str(events[-1].data)


async def test_a_request_that_ran_starts_nothing_and_is_a_duplicate(agent, backend):
    await collect(backend, "c1", "r1")
    again = await collect(backend, "c1", "r1")
    assert kinds(again) == [EVENT_FAILED] and again[0].data == {DUPLICATE: True}
    assert len(agent.prompts) == 1
    # The same id in another conversation is another request.
    assert kinds(await collect(backend, "c2", "r1"))[-1] == EVENT_SUCCEEDED
    assert len(agent.prompts) == 2


async def test_one_turn_runs_at_a_time_in_a_conversation(agent, backend):
    release = asyncio.Event()

    async def held(prompt):
        await release.wait()
        await prompt.say("done")
        return "end_turn"

    agent.on_prompt = held
    running = asyncio.ensure_future(collect(backend, "c1", "r1"))
    await asyncio.sleep(0.1)
    assert [(e.kind, e.text) for e in await collect(backend, "c1", "r2")] == [(EVENT_FAILED, ALREADY_RUNNING)]
    release.set()
    assert (await running)[-1].kind == EVENT_SUCCEEDED


async def test_files_go_as_the_content_blocks_the_agent_takes(socket_dir, make_backend):
    agent = ScriptedAgent(f"{socket_dir}/files.sock", capabilities=capabilities(image=True, embedded_context=True))
    await agent.start()
    try:
        backend = await make_backend(agent.path)
        files = (
            {"name": "a.png", "uri": data_uri("image/png", b"\x89PNG")},
            {"name": "notes.md", "uri": data_uri("text/markdown", b"# notes")},
            {"name": "blob.bin", "uri": data_uri("application/octet-stream", b"\xff\xfe")},
        )
        assert kinds(await collect(backend, files=files))[-1] == EVENT_SUCCEEDED
        blocks = [block.model_dump(by_alias=True, exclude_none=True) for block in agent.prompts[0][2]]
        assert [block["type"] for block in blocks] == ["text", "image", "resource", "resource"]
        assert blocks[2]["resource"] == {"uri": "file:///notes.md", "mimeType": "text/markdown", "text": "# notes"}
        assert blocks[3]["resource"]["blob"] == base64.b64encode(b"\xff\xfe").decode()
    finally:
        await agent.close()


async def test_a_file_the_agent_does_not_take_is_refused_before_anything_is_sent(agent, backend):
    files = ({"name": "song.mp3", "uri": data_uri("audio/mpeg", b"ID3")},)
    events = await collect(backend, files=files)
    assert kinds(events) == [EVENT_FAILED] and "audio/mpeg" in events[0].text and "song.mp3" in events[0].text
    assert agent.prompts == []
    # Nothing was submitted, so the same request can run once its file is dropped.
    assert kinds(await collect(backend))[-1] == EVENT_SUCCEEDED


async def test_capabilities_claim_no_scope_field_and_no_operation_acp_lacks(backend):
    capabilities = backend.capabilities()
    assert capabilities.scope_fields == frozenset()
    assert {OP_RECOVER, OP_CONTINUE} <= capabilities.operations
    assert not capabilities.operations & {OP_STEER, OP_FORGET, OP_HEALTH}
    assert capabilities.version == "scripted 1"


# ── cancellation ─────────────────────────────────────────────────────────


async def slow(prompt):
    await prompt.say("working")
    if await prompt.wait_cancel():
        return "cancelled"
    return "end_turn"


async def test_a_cancel_ends_the_turn_and_the_session_takes_the_next_prompt(agent, backend):
    agent.on_prompt = slow
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    await first(stream, EVENT_TEXT_DELTA)
    assert await backend.cancel("c1") is True
    rest = [event async for event in stream]
    assert [event.kind for event in rest if event.terminal] == [EVENT_INTERRUPTED]
    assert rest[-1].data == {"stop_reason": "cancelled", "after": "cancel"}
    assert ("cancel", agent.prompts[0][0]) in agent.calls

    agent.on_prompt = say_twice
    assert (await collect(backend, request_id="r2"))[-1].kind == EVENT_SUCCEEDED
    assert agent.prompts[0][0] == agent.prompts[1][0]  # the same session
    assert await backend.cancel("c1") is False  # nothing runs


async def test_a_stop_right_after_accepted_reaches_the_agent_after_the_prompt(agent, make_backend):
    backend = await make_backend(agent.path, cancel_timeout=1.0)
    agent.on_prompt = slow
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    await first(stream, EVENT_ACCEPTED)
    # In the same loop step: a session/cancel sent before the prompt is lost, and the turn runs to its end.
    assert await backend.cancel("c1") is True
    rest = [event async for event in stream]
    assert [event.kind for event in rest if event.terminal] == [EVENT_INTERRUPTED]


async def test_a_turn_the_agent_does_not_end_after_cancel_is_reported_unresolved(agent, make_backend):
    backend = await make_backend(agent.path, cancel_timeout=0.2)

    async def stubborn(prompt):
        await prompt.say("ignoring")
        await asyncio.sleep(60)
        return "end_turn"

    agent.on_prompt = stubborn
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    await first(stream, EVENT_TEXT_DELTA)
    assert await backend.cancel("c1") is False
    rest = [event async for event in stream]
    assert [(event.kind, event.text) for event in rest] == [(EVENT_FAILED, UNRESOLVED)]


@pytest.mark.parametrize("connection", ["shared", "per_conversation"])
async def test_the_late_messages_of_an_unresolved_turn_reach_no_later_turn(
    socket_dir, make_backend, tools, toolkit, connection
):
    per_conversation = connection == "per_conversation"
    agent = ScriptedAgent(f"{socket_dir}/late.sock", binding_line=per_conversation, tools_token=TOKEN)
    await agent.start()
    try:
        header = TURN_HEADER if per_conversation else ""
        backend = await make_backend(agent.path, header=header, cancel_timeout=0.2)
        agent.tools_url = backend._tools_url
        asked = []

        async def stubborn(prompt):
            await prompt.say("old start")
            await asyncio.sleep(0.6)
            await prompt.say("OLD LATE TEXT")
            await prompt.call_tool("post", {"text": "old call"})
            asked.append(await prompt.ask(call_id="old-call", title="rm -rf old"))
            return "end_turn"

        async def waits(prompt):
            await asyncio.sleep(1.5)
            await prompt.say("new reply")
            return "end_turn"

        agent.on_prompt = stubborn
        bind(tools, "c1")
        stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
        await first(stream, EVENT_TEXT_DELTA)
        assert await backend.cancel("c1") is False
        [event async for event in stream]
        agent.on_prompt = waits
        events = await asyncio.wait_for(collect(backend, request_id="r2"), 10)
        assert [event.text for event in events if event.kind == EVENT_TEXT_DELTA] == ["new reply"]
        assert EVENT_QUESTION not in kinds(events) and events[-1].kind == EVENT_SUCCEEDED
        assert toolkit.posts == []
        # A late permission request is answered cancelled, unless the agent process ended with its connection.
        assert all(answer.outcome.outcome == "cancelled" for answer in asked)
    finally:
        await agent.close()


async def hang(self, cwd, mcp_servers=None, **kwargs):
    await asyncio.Event().wait()


async def test_a_stop_while_the_session_opens_ends_the_turn_once(agent, make_backend, monkeypatch):
    monkeypatch.setattr(scripted_agent._Connection, "new_session", hang)  # before the connection opens
    backend = await make_backend(agent.path)
    events = []

    async def read():
        events.extend([event async for event in backend.run_turn(TurnRequest("c1", "r1", "go"))])

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.2)
    assert await backend.cancel("c1") is True
    await asyncio.wait_for(reader, 5)
    assert kinds(events) == [EVENT_INTERRUPTED]
    assert await backend.active() == []


async def test_a_session_that_does_not_open_in_time_fails_the_turn_once(agent, make_backend, monkeypatch):
    monkeypatch.setattr(scripted_agent._Connection, "new_session", hang)
    backend = await make_backend(agent.path, start_timeout=0.2)
    events = await asyncio.wait_for(collect(backend), 5)
    assert kinds(events) == [EVENT_FAILED] and "start_timeout_seconds" in events[0].text


async def test_a_session_that_does_not_load_in_time_is_not_retained(agent, make_backend, tmp_path, monkeypatch):
    first_run = await make_backend(agent.path, state=tmp_path / "s")
    await collect(first_run)
    await first_run.close()
    monkeypatch.setattr(scripted_agent._Connection, "load_session", hang)
    backend = await make_backend(agent.path, state=tmp_path / "s", start_timeout=0.2)
    assert await asyncio.wait_for(backend.retained("c1"), 5) is False


async def test_a_reader_that_leaves_cancels_the_turn(agent, backend):
    agent.on_prompt = slow
    stream = backend.run_turn(TurnRequest("c1", "r1", "go"))
    await first(stream, EVENT_TEXT_DELTA)
    await stream.aclose()
    assert ("cancel", agent.prompts[0][0]) in agent.calls
    assert await backend.active() == []


# ── state and restart ────────────────────────────────────────────────────


async def test_the_state_file_is_private_and_holds_the_final_and_no_prompt_and_no_token(agent, make_backend, tmp_path):
    backend = await make_backend(agent.path, state=tmp_path / "s")
    await collect(backend, text="a secret plan")
    path = tmp_path / "s" / "acp_test.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "a secret plan" not in text and backend._tool_server.token not in text
    stored = json.loads(text)
    assert stored["sessions"] == {"c1": agent.prompts[0][0]}
    assert stored["requests"]["c1 r1"]["terminal"]["text"] == "Hello, world."


async def test_a_withheld_final_is_kept_out_of_the_state_file(agent, make_backend, tmp_path):
    backend = await make_backend(agent.path, state=tmp_path / "s")
    events = await collect(backend, scope=TurnScope(withhold_final=True))
    assert (events[-1].kind, events[-1].text) == (EVENT_SUCCEEDED, "Hello, world.")
    assert "Hello, world." not in (tmp_path / "s" / "acp_test.json").read_text()
    recovered = await backend.recover(TurnRequest("c1", "r1", "hi"))
    assert (recovered.terminal.kind, recovered.terminal.text) == (EVENT_SUCCEEDED, "")


async def test_a_restart_restores_the_session_and_suppresses_the_replay(agent, make_backend, tmp_path):
    state = tmp_path / "s"
    await collect(await make_backend(agent.path, state=state))
    restarted = await make_backend(agent.path, state=state)
    events = await collect(restarted, request_id="r2")
    assert [event.text for event in events if event.kind == EVENT_TEXT_DELTA] == ["Hello, ", "world."]
    assert ("load", agent.prompts[0][0]) in agent.calls  # the replay of "hi" and "Hello, world." reached no turn
    assert agent.prompts[1][0] == agent.prompts[0][0]


async def test_a_restart_resumes_where_the_agent_advertises_it(socket_dir, make_backend, tmp_path):
    agent = ScriptedAgent(f"{socket_dir}/r.sock", capabilities=capabilities(resume=True))
    await agent.start()
    try:
        await collect(await make_backend(agent.path, state=tmp_path / "s"))
        await collect(await make_backend(agent.path, state=tmp_path / "s"), request_id="r2")
        assert [call for call, _ in agent.calls if call in ("load", "resume")] == ["resume"]
    finally:
        await agent.close()


async def test_without_load_or_resume_a_needed_conversation_is_unavailable(socket_dir, make_backend, tmp_path):
    agent = ScriptedAgent(f"{socket_dir}/n.sock", capabilities=capabilities(load=False))
    await agent.start()
    try:
        await collect(await make_backend(agent.path, state=tmp_path / "s"))
        restarted = await make_backend(agent.path, state=tmp_path / "s")
        events = await collect(restarted, request_id="r2", existing_conversation_only=True)
        assert [(event.kind, event.text) for event in events] == [(EVENT_FAILED, CONVERSATION_UNAVAILABLE)]
        assert await restarted.retained("c1") is False
        # A turn that does not need the context starts a new session, and the loss is logged.
        await collect(restarted, request_id="r3")
        assert len({session for session, _, _ in agent.prompts}) == 2
    finally:
        await agent.close()


async def test_the_start_warns_of_what_the_agent_does_not_give(socket_dir, make_backend, caplog):
    agent = ScriptedAgent(f"{socket_dir}/w.sock", capabilities=capabilities(load=False, image=True))
    await agent.start()
    try:
        await make_backend(agent.path, connection="per_conversation", header="X-Belle-Turn")
        warned = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert any("no usage" in line for line in warned)
        assert any("neither session/resume nor session/load" in line for line in warned)
        assert any("takes image files only" in line for line in warned)
        assert any("no idle limit" in line for line in warned)
    finally:
        await agent.close()


async def test_the_start_warns_of_no_loss_the_agent_does_not_have(socket_dir, make_backend, caplog):
    agent = ScriptedAgent(f"{socket_dir}/f.sock", capabilities=capabilities(resume=True, embedded_context=True))
    await agent.start()
    try:
        await make_backend(agent.path)
        warned = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert warned == ["acp: ACP reports no usage and offers no health check"]
    finally:
        await agent.close()


async def test_a_refused_resume_keeps_the_error_data_and_its_token_out_of_the_log(
    socket_dir, make_backend, tmp_path, monkeypatch, caplog
):
    async def echo(self, cwd, session_id, mcp_servers=None, **kwargs):
        # An agent's error data can echo the request, with the tool server's token in its mounts.
        raise RequestError.invalid_params({"request": [mount.model_dump() for mount in mcp_servers or ()]})

    agent = ScriptedAgent(f"{socket_dir}/e.sock", capabilities=capabilities(resume=True))
    await agent.start()
    try:
        await collect(await make_backend(agent.path, state=tmp_path / "s"))
        monkeypatch.setattr(scripted_agent._Connection, "resume_session", echo)
        restarted = await make_backend(agent.path, state=tmp_path / "s")
        assert kinds(await collect(restarted, request_id="r2", existing_conversation_only=True)) == [EVENT_FAILED]
        assert kinds(await collect(restarted, request_id="r3"))[-1] == EVENT_SUCCEEDED
        logged = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert restarted._tool_server.token not in caplog.text
        assert sum("error -32602: Invalid params" in line for line in logged) == 2
    finally:
        await agent.close()


async def test_a_conversation_never_seen_is_unavailable_when_it_must_exist(agent, backend):
    events = await collect(backend, existing_conversation_only=True)
    assert [(event.kind, event.text) for event in events] == [(EVENT_FAILED, CONVERSATION_UNAVAILABLE)]
    assert [call for call, _ in agent.calls if call == "new"] == []
    assert await backend.retained("c1") is False


async def test_a_profile_without_a_mount_serves_no_tool_and_runs_turns(agent, tmp_path, tools):
    profile = replace(settings(agent.path).profile, tools_mount="none", binding_meta="")
    backend = AcpBackend(replace(settings(agent.path), profile=profile), tmp_path / "s")
    await backend.start(tools)
    try:
        assert backend._tool_server is None
        assert kinds(await collect(backend))[-1] == EVENT_SUCCEEDED
        assert all(not session.mounts for session in agent.sessions.values())
    finally:
        await backend.close()


async def test_a_wildcard_bind_admits_the_host_of_its_url(agent, tmp_path, tools):
    url = "http://belle:18300/mcp"
    backend = AcpBackend(replace(settings(agent.path), tools_bind="0.0.0.0", tools_url=url), tmp_path / "s")
    await backend.start(tools)
    try:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {
            "Host": "belle:18300",
            "Authorization": f"Bearer {backend._tool_server.token}",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient() as client:
            answer = await client.post(f"http://127.0.0.1:{backend._tool_server.port}/mcp", json=body, headers=headers)
        assert answer.status_code == 200, answer.text
        assert backend._tools_url == url
    finally:
        await backend.close()


async def test_an_ipv6_bind_without_a_url_gives_a_url_with_the_address_in_brackets(agent, tmp_path, tools):
    backend = AcpBackend(replace(settings(agent.path), tools_bind="::1"), tmp_path / "s")
    await backend.start(tools)
    try:
        assert backend._tools_url == f"http://[::1]:{backend._tool_server.port}/mcp"
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {
            "Authorization": f"Bearer {backend._tool_server.token}",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient() as client:
            answer = await client.post(backend._tools_url, json=body, headers=headers)
        assert answer.status_code == 200, answer.text
    finally:
        await backend.close()


def test_a_wildcard_bind_without_a_url_is_refused_by_name(tmp_path):
    profile = {"socket": "/s", "cwd": "/w", "connection": "shared", "tools_mount": "none"}
    config = {"backends": {"acp": {"profile": "p", "profiles": {"p": profile}, "tools": {"bind": "0.0.0.0"}}}}
    with pytest.raises(BelleConfigError, match=r"backends\.acp\.tools\.url must name the address"):
        create_backend(config, tmp_path)


def test_a_mount_without_the_mcp_extra_is_refused_by_name_and_no_mount_needs_none(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mcp", None)
    profile = {"socket": "/s", "cwd": "/w", "connection": "shared", "tools_mount": "session", "binding": {"meta": "k"}}
    config = {"backends": {"acp": {"profile": "p", "profiles": {"p": profile}}}}
    with pytest.raises(BelleConfigError, match=r"profiles\.p\.tools_mount: session needs .* belle-agent\[acp,mcp\]"):
        create_backend(config, tmp_path)
    del profile["binding"]
    profile["tools_mount"] = "none"
    assert create_backend(config, tmp_path)
