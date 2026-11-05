"""What a turn's scope lets through Docker Agent: skills, Docker Agent's own tools, experts and system text.

The checks run the host against the Docker Agent container and the stand-in model, whose directives make the
calls. Each reads what the model was given from the stand-in's log.
"""

import base64
import json

import pytest
from agent_yaml import EXPERT_INSTRUCTION, SUBAGENT
from conformance_kit import ModelTurn, worlds
from docker_agent_kit import DockerAgentKit, asked
from standin import said, script, text_of

from belle_agent.host import OUTCOME_FAILED, OUTCOME_POSTED_FINAL
from belle_agent.skills import packaged_skill_text

pytestmark = pytest.mark.docker_agent_server

SKILL = "chat-digest"
#: The user message of a subagent's session. Its task is in its system message.
HELPER_PROMPT = "Please proceed."
#: A value shaped like an access token. Docker Agent's secret redaction knows the shape.
SECRET = "dckr_pat_" + "A" * 27


def _call(tool: str, **arguments: str) -> str:
    """The stand-in's directive to call one tool."""
    return f"CALL={tool}={base64.b64encode(json.dumps(arguments).encode()).decode()}"


def _results(model, text: str) -> str:
    """The tool results the model was given in the turn whose message is ``text``."""
    return "\n".join(said(asked(model, text)[-1], "tool"))


async def test_a_skill_is_read_only_where_the_scope_allows_it_and_its_tool_only_where_not_denied(build_world, model):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, chat: C1}
    agent: {skills: {available: [welcome-onboarding]}}
  - match: {channel: loopback, chat: C2}
    permissions: {tools: {read_skill: deny}}
  - match: {channel: loopback, chat: C4}
    agent: {skills: {unavailable: [chat-digest]}}
"""
    )
    line = [line for line in packaged_skill_text(SKILL).splitlines() if line.strip()][-1]
    texts = {chat: f"{_call('read_skill', name=SKILL)} in {chat}" for chat in ("C1", "C2", "C3", "C4")}

    for chat, text in texts.items():
        await world.send(world.message(text, conversation=chat))

    # The skill is withheld where the scope does not list it, and its tool refused where the scope denies it.
    assert line not in _results(model, texts["C1"])
    assert line not in _results(model, texts["C2"])
    # The same where the scope makes the skill unavailable.
    assert line not in _results(model, texts["C4"])
    # Control: where no scope restricts, the skill's text reaches the model.
    assert line in _results(model, texts["C3"])
    assert world.outcomes() == [OUTCOME_POSTED_FINAL] * 4


async def test_an_expert_answers_the_turn_its_scope_names_with_its_additions_and_the_next_turn_is_back_on_the_entry_agent(
    build_world, model
):
    world = await build_world(
        """
people: {ada: {loopback: [U-ADA]}}
roles: {leads: [ada]}
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], session: channel}
  - match: {channel: loopback, role: [leads]}
    agent: {expert: translator}
    delivery: {prompt: MARK-EXPERT}
"""
    )
    assert "translator" in (await world.host.catalog()).experts

    await world.send(world.message("SAY=one", sender="U-ADA"))
    await world.send(world.message("SAY=two", sender="U-BOB"))

    expert, entry = ("\n".join(said(asked(model, text)[-1], "system")) for text in ("SAY=one", "SAY=two"))
    assert EXPERT_INSTRUCTION in expert
    assert EXPERT_INSTRUCTION not in entry
    # The routed agent relays the prompt hook too, so the turn's system additions reach it.
    assert "MARK-EXPERT" in expert
    assert "MARK-EXPERT" not in entry
    assert [post.text for post in world.posts()] == ["one", "two"]


async def test_a_system_addition_reaches_its_own_turn_only(build_world, model):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, chat: C1}
    delivery: {prompt: MARK-ADDED}
"""
    )
    await world.send(world.message("SAY=one", conversation="C1"))
    await world.send(world.message("SAY=two", conversation="C2"))

    one, two = ("\n".join(said(asked(model, text)[-1], "system")) for text in ("SAY=one", "SAY=two"))
    assert "MARK-ADDED" in one
    assert "MARK-ADDED" not in two


async def test_a_system_addition_stays_in_the_model_calls_after_a_steered_message(model, tmp_path):
    with DockerAgentKit.scripted() as kit:
        async with worlds(kit, tmp_path) as build:
            world = await build(
                """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], mid_turn: steer, prompt: MARK-ADDED}
"""
            )
            kit.play(ModelTurn(final="first answer", hold=True), ModelTurn(final="second answer"))
            await world.platform.inject(world.message("first", thread="root"))
            await kit.holding()
            assert (await world.platform.inject(world.message("then this", thread="root"))).startswith("steered:")
            kit.release()
            await world.host.idle()

    steered = asked(model, "then this")
    assert steered
    assert all("MARK-ADDED" in "\n".join(said(body, "system")) for body in steered)


def _helper_reads_its_task(body: dict, session: str | None) -> list[dict] | int:
    """The stand-in, where a subagent follows the directives of its task. Docker Agent puts the task in the
    subagent's system message and asks it with a fixed user message."""
    messages = [dict(message) for message in body.get("messages", [])]
    users = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    if users and text_of(messages[users[-1]]) == HELPER_PROMPT:
        messages[users[-1]]["content"] = "\n".join(said(body, "system"))
    return script({**body, "messages": messages})


async def test_a_subagent_calls_belles_tools_under_the_scope_of_the_turn_that_handed_it_the_task(
    build_world, model, monkeypatch
):
    monkeypatch.setattr(model, "script", _helper_reads_its_task)
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], prompt: MARK-TURN}
  - match: {channel: loopback, chat: C2}
    permissions: {tools: {post_message: deny}}
"""
    )
    task = {chat: _call("belle_post_message", text=f"from the helper in {chat}") for chat in ("C1", "C2")}
    texts = {chat: _call("transfer_task", agent=SUBAGENT, task=task[chat], expected_output="a post") for chat in task}

    for chat, text in texts.items():
        await world.send(world.message(text, conversation=chat))

    # The subagent's call is the turn's: it posts where the scope allows the tool, and is refused where it denies it.
    # Where nothing was posted, the host posts the turn's final.
    assert [(post.conversation, post.text) for post in world.posts()] == [
        ("C1", "from the helper in C1"),
        ("C2", "tool done"),
    ]
    helper = [entry["body"] for entry in list(model.log) if task["C2"] in "\n".join(said(entry["body"], "system"))]
    refused = "\n".join(said(helper[-1], "tool"))
    print(f"the helper's refused call answered: {refused!r}")
    assert "post_message" in refused and "denied" in refused.lower()
    # The turn's system text reaches the agent that got the message, and not the subagent.
    assert "MARK-TURN" in "\n".join(said(asked(model, texts["C2"])[-1], "system"))
    assert "MARK-TURN" not in "\n".join(said(helper[-1], "system"))


async def test_with_the_hook_endpoint_stopped_a_message_reaches_no_model(world, model):
    await world.kit.backend._hooks.close()
    before = len(model.log)

    await world.send(world.direct("SAY=unseen"))

    assert world.outcomes() == [OUTCOME_FAILED]
    assert len(model.log) == before


async def test_a_secret_shaped_text_reaches_belles_tools_and_the_model_as_written(world, model):
    # The post lands in the sender's direct conversation, whose name the tool's result repeats.
    text = f"{_call('belle_post_message', text=SECRET)} {SECRET}"
    before = len(model.log)

    await world.send(world.direct(text, sender=SECRET))

    # The tool's argument, the message the person wrote, and the tool's result each carry the value unchanged.
    assert len(model.log) - before == 2
    last = list(model.log)[-1]["body"]
    reached = ([post.text for post in world.posts()], said(last, "user")[-1], "\n".join(said(last, "tool")))
    assert reached[:2] == ([SECRET], text), reached
    assert SECRET in reached[2], reached
