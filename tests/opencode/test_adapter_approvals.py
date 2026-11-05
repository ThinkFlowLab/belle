"""On OpenCode: built-in tools follow the scope, and an invented form runs.

These read OpenCode's own provider requests and drive a real form, so they live
here rather than in the backend-neutral suite.
"""

import asyncio
import secrets

import pytest
from drive import Client
from probe_builtin_actions import CALLS, CODE_MODE, directive, offered
from standin import call, said, tools

from belle_agent.backend import Answer, Question
from belle_agent.host import OUTCOME_INTERRUPTED, OUTCOME_POSTED_FINAL
from belle_agent.integrations.opencode.permissions import BUILTIN_ACTIONS
from belle_agent.integrations.opencode.sessions import session_id
from belle_agent.platforms.base import QuestionRefused

pytestmark = pytest.mark.adapter

DENY_SHELL_IN_C1 = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, chat: C1}
    permissions: {tools: {shell: deny}}
"""


def _session(model, text: str) -> str:
    return next(entry["session"] for entry in reversed(model.log) if said(entry["body"], "user")[-1:] == [text])


def _shell(command: str) -> str:
    return "go " + call("shell", command=command, description="a scope check")


async def test_a_built_in_tool_follows_the_scope_in_both_directions(build_world, model):
    mark = secrets.token_hex(4)
    world = await build_world(DENY_SHELL_IN_C1)

    # Denied in C1: shell is removed from the roster and refused at execution, so the marker is never written.
    refused = _shell(f"touch DENIED_{mark}")
    await world.send(world.message(refused, conversation="C1"))
    assert "shell" not in tools(model.requests(_session(model, refused))[0])

    # Allowed in C2: a conversation the scope names no tool levels for keeps OpenCode's built-in shell.
    listed = _shell(f"touch CONTROL_{mark}; ls")
    await world.send(world.message(listed, conversation="C2"))
    allowed_session = _session(model, listed)
    assert "shell" in tools(model.requests(allowed_session)[0])
    directory = said(model.requests(allowed_session)[-1], "tool")[-1]
    assert f"CONTROL_{mark}" in directory
    assert f"DENIED_{mark}" not in directory


async def test_the_form_half_runs_against_a_real_form(world):
    backend = world.kit.backend
    conversation = "loopback_W1_D-U1_U1"
    session = session_id(backend._namespace, conversation)
    await asyncio.to_thread(Client().api, "POST", "/api/session", {"id": session})

    form_id = await backend.ask(conversation, Question(id="", text="Admit this turn?", options=("yes", "no")))

    assert form_id.startswith("frm_")
    pending = await backend.pending(conversation)
    assert any(question.id == form_id and question.text == "Admit this turn?" for question in pending)

    await backend.answer(conversation, form_id, Answer(values=("yes",)))

    assert all(question.id != form_id for question in await backend.pending(conversation))


async def test_a_question_the_platform_cannot_show_reaches_the_model_as_its_reason(world, model):
    async def cannot_show(*_: object, **__: object) -> str:
        raise QuestionRefused("a question here offers at most 25 options; this one has 26")

    world.platform.ask = cannot_show
    options = [{"label": f"option {n}", "description": f"option {n}"} for n in range(26)]
    text = "go " + call("question", questions=[{"question": "Pick one", "header": "Pick one", "options": options}])

    await world.send(world.direct(text))

    # OpenCode hands the model the reason as the question tool's result, and the turn goes on to its final.
    (result,) = said(model.requests(_session(model, text))[-1], "tool")
    assert "at most 25 options; this one has 26" in result
    assert [post.text for post in world.posts("D-U1")] == ["tool done"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_a_scoped_ask_tool_makes_no_provider_progress_until_it_is_answered(build_world, model):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
    permissions: {tools: {post_message: ask}}
"""
    )
    text = "go " + call("belle_post_message", text="said through the tool")
    await world.platform.inject(world.direct(text))
    (shown,) = await world.wait_question(conversation="D-U1")

    session = _session(model, text)
    # OpenCode paused on the permission ask: one provider request, no post, no further request.
    assert shown.question.tool == "belle_post_message"
    assert len(model.requests(session)) == 1
    assert world.posts() == []

    await world.send(world.answer_to(shown, approved=True))

    assert [post.text for post in world.posts("D-U1")] == ["said through the tool"]
    assert OUTCOME_POSTED_FINAL not in world.outcomes()  # the tool posted; no final was needed


async def test_a_rejected_approval_ends_the_turn_on_opencode(build_world, model):
    """What OpenCode does today, held as it is. Whether a turn should go on after a reject is undecided.

    On this pin a rejected permission ends the execution:
    nothing is posted, the model is not asked again, and the turn is recorded as interrupted.
    """
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
    permissions: {tools: {post_message: ask}}
"""
    )
    text = "go " + call("belle_post_message", text="never said")
    await world.platform.inject(world.direct(text))
    (shown,) = await world.wait_question(conversation="D-U1")

    await world.send(world.answer_to(shown, approved=False))

    assert world.posts() == []
    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert len(model.requests(_session(model, text))) == 1


ONLY = """
scopes:
  - match: {{channel: loopback}}
    delivery: {{mode: [all]}}
  - match: {{channel: loopback, chat: C1}}
    permissions: {{tools: {{{rules}}}}}
"""


@pytest.mark.parametrize("name", sorted(BUILTIN_ACTIONS))
async def test_a_denied_built_in_tool_is_not_offered_and_is_refused_by_name(build_world, model, name):
    """Every built-in tool of the pin, in both adapter modes: a deny on the tool's name holds for that tool."""
    world = await build_world(ONLY.format(rules=f"{name}: deny"))
    mark = secrets.token_hex(4)
    # A level governs every tool of its permission action: `edit`, `write` and `opencode_worktree_remove` share `edit`.
    # A deny on `execute` keeps the Code Mode tools: OpenCode then offers them as direct tools.
    governed = {tool for tool, action in BUILTIN_ACTIONS.items() if action == BUILTIN_ACTIONS[name]}

    called = f"denied {mark} " + directive(name)
    await world.send(world.message(called, conversation="C1"))
    await world.send(world.message(f"roster {mark} SAY=x", conversation="C2"))

    requests = model.requests(_session(model, called))
    control = offered(model.requests(_session(model, f"roster {mark} SAY=x"))[0])
    # Absent from what the model is offered, beside a conversation the scope does not name.
    assert set(BUILTIN_ACTIONS) <= control
    assert control - offered(requests[0]) == governed
    # Refused when the model calls it by name all the same: by the session ruleset, and behind the
    # plugin by the tool host before that. Inside `execute`, a Code Mode tool that is not offered is unknown.
    refusal = said(requests[-1], "tool")[-1]
    if name in CODE_MODE:
        assert f"Unknown tool '{name.replace('_', '.', 1)}'" in refusal, refusal
    else:
        assert f'No tool named \\"{name}\\" is currently available' in refusal or f"{name} is denied in this conversation" in refusal

    # Control: in the same conversation a tool the scope does not name is offered and runs.
    other = "read" if name != "read" else "skill"
    ran = f"control {mark} " + call(other, **CALLS[other])
    await world.send(world.message(ran, conversation="C1"))
    result = said(model.requests(_session(model, ran))[-1], "tool")[-1]
    assert "No tool named" not in result and "is denied" not in result, result


async def test_an_ask_on_write_asks_a_person_before_the_file_is_written(build_world, model):
    """A rule on `write` matches nothing in OpenCode, so `write: ask` once let the file be written unasked."""
    world = await build_world(ONLY.format(rules="write: ask"))
    name = f"ASKED_{secrets.token_hex(4)}.txt"
    text = "go " + call("write", path=f"/workspace/{name}", content="x")

    await world.platform.inject(world.message(text, conversation="C1"))
    (shown,) = await world.wait_question(conversation="C1")

    # The request is for OpenCode's `edit` permission, which is what gates `write`.
    assert shown.question.tool == "edit" and name in shown.question.text
    await world.send(world.answer_to(shown, approved=False))
    listing = "go " + call("read", path="/workspace")
    await world.send(world.message(listing, conversation="C2"))
    assert name not in said(model.requests(_session(model, listing))[-1], "tool")[-1]

    # Control: approved, the file is written.
    await world.platform.inject(world.message(text + " again", conversation="C1"))
    shown = (await world.wait_question(2, conversation="C1"))[-1]
    await world.send(world.answer_to(shown, approved=True))
    await world.send(world.message(listing + " again", conversation="C2"))
    assert name in said(model.requests(_session(model, listing + " again"))[-1], "tool")[-1]


async def test_a_read_outside_the_working_directory_is_refused_where_the_scope_denies_it(build_world, model):
    world = await build_world(ONLY.format(rules="external_directory: deny"))
    text = "go " + call("read", path="/etc/opencode/opencode.jsonc")

    await world.send(world.message(text, conversation="C1"))

    # With no such rule OpenCode asks a person ("Allow external_directory: /etc/opencode/*") and then reads the file.
    assert world.questions() == []
    assert "Permission denied: external_directory" in said(model.requests(_session(model, text))[-1], "tool")[-1]
    # Control: the working directory itself is read with no rule and no question.
    inside = "go " + call("read", path="/workspace")
    await world.send(world.message(inside, conversation="C1"))
    assert "Read directory /workspace" in said(model.requests(_session(model, inside))[-1], "tool")[-1]
