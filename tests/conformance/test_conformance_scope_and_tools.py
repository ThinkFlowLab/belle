"""What a turn is offered, and what a tool call may touch. Every backend must pass."""

import json

from conformance_kit import ModelTurn

from belle_agent.backend import ToolCall


async def test_two_conversations_with_different_scopes_get_different_turn_scopes(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, chat: C1}
    delivery: {prompt: Answer in French.}
    agent:
      model_name: small-model
      expert: translator
      subagents: [researcher]
      skills: {available: [glossary, style], unavailable: [style], required: [glossary]}
    permissions: {tools: {react_to_message: deny}}
"""
    )

    await world.send(world.message("bonjour", conversation="C1"))
    await world.send(world.message("hello", conversation="C2"))

    # What a turn was offered is read from the request the backend received.
    scoped, plain = (request.scope for request in world.requests)
    assert scoped != plain
    assert scoped.model == "small-model"
    assert scoped.expert == "translator"
    assert scoped.subagents_available == frozenset({"researcher"})
    assert scoped.skills_available == frozenset({"glossary", "style"})
    assert scoped.skills_unavailable == frozenset({"style"})
    assert scoped.skills_required == ("glossary",)
    assert scoped.system_additions == ("Answer in French.",)
    assert scoped.tools == {"react_to_message": "deny"}
    assert (plain.model, plain.expert, plain.skills_available, plain.subagents_available) == ("", "", None, None)
    assert plain.skills_unavailable == frozenset()
    assert plain.system_additions == ()
    assert plain.tools == {}


async def test_two_senders_in_one_conversation_get_their_own_scope(build_world):
    world = await build_world(
        """
people: {ada: {loopback: [U-ADA]}}
roles: {leads: [ada]}
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, role: [leads]}
    agent: {model_name: large-model}
"""
    )

    await world.send(world.message("one", sender="U-ADA"))
    await world.send(world.message("two", sender="U-BOB"))
    await world.send(world.message("three", sender="U-ADA"))

    assert [request.scope.model for request in world.requests] == ["large-model", "", "large-model"]


async def test_a_model_scoped_to_one_sender_serves_that_senders_turns_only(build_world, kit):
    world = await build_world(
        f"""
people: {{ada: {{loopback: [U-ADA]}}}}
roles: {{leads: [ada]}}
scopes:
  - match: {{channel: loopback}}
    delivery: {{mode: [all], session: channel}}
  - match: {{channel: loopback, role: [leads]}}
    agent: {{model_name: {kit.other_model}}}
"""
    )

    await world.send(world.message("one", sender="U-BOB"))
    await world.send(world.message("two", sender="U-ADA"))
    await world.send(world.message("three", sender="U-BOB"))

    # One conversation, one session: the model a scope names for one sender does not stay for the next.
    default, scoped, after = kit.models()
    assert scoped == kit.other_model
    assert default == after != scoped


async def test_a_post_aimed_at_another_conversation_is_refused(world):
    world.kit.play(
        ModelTurn(final="tried", calls=(("post_message", {"text": "leak", "chat_id": "C-OTHER"}),)),
        ModelTurn(final="done", calls=(("post_message", {"text": "here"}),)),
    )

    await world.send(world.direct("post it elsewhere"))

    # A deny is an absent side effect: nothing reached the other conversation.
    assert world.posts("C-OTHER") == []
    assert [post.text for post in world.posts()] == ["tried"]

    # Control: the same tool, aimed at its own conversation, posts.
    await world.send(world.direct("post it here"))
    assert [post.text for post in world.posts()] == ["tried", "here"]


async def test_a_tool_call_for_a_conversation_with_no_turn_is_refused(world):
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.direct("hello"))
    await world.handed(1)
    running = world.requests[0].conversation
    arguments = {"text": "not mine to post"}

    refused = await world.host.tools.call(ToolCall(conversation="loopback_W1_D-U2_U2", name="post_message", arguments=arguments))

    assert json.loads(refused)["error"] == "tool_denied"
    assert world.platform.log == []

    # Control: the same call bound to the conversation whose turn is running posts there.
    allowed = await world.host.tools.call(ToolCall(conversation=running, name="post_message", arguments=arguments))
    assert json.loads(allowed)["ok"] is True
    assert [(post.conversation, post.text) for post in world.posts()] == [("D-U1", "not mine to post")]

    world.kit.release()
    await world.host.idle()
    # Once the turn has ended its conversation is closed to tool calls again.
    late = await world.host.tools.call(ToolCall(conversation=running, name="post_message", arguments=arguments))
    assert json.loads(late)["error"] == "tool_denied"
    assert len(world.posts()) == 1


async def test_a_tool_the_scope_denies_is_refused_when_called_by_name(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all]}
    permissions: {tools: {post_message: deny}}
  - match: {channel: loopback, chat: C2}
    delivery: {mode: [all]}
"""
    )
    call = (("post_message", {"text": "said through the tool"}),)
    world.kit.play(ModelTurn(final="final one", calls=call), ModelTurn(final="final two", calls=call))

    await world.send(world.message("go", conversation="C1"))
    await world.send(world.message("go", conversation="C2"))

    # The denied call left no post, so the reply C1 was owed is the final.
    assert [post.text for post in world.posts("C1")] == ["final one"]
    # Control: where no scope denies it, the same call posts.
    assert [post.text for post in world.posts("C2")] == ["said through the tool"]
