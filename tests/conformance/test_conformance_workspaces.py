"""Conversations of several workspaces behind one host. Every backend and every platform must pass.

A conversation id and a person's id mean something in one workspace only.
Two workspaces that happen to use the same ids are two places, so the host
keeps them apart everywhere it keeps state: the session, the queue, the
window of seen messages and the tools bound to a running turn.
"""

from dataclasses import replace

import pytest
from conformance_kit import ModelTurn

from belle_agent.backend import VERDICT_ALLOW, VERDICT_DENY, ToolCall

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""

OTHER_WORKSPACE = "W2"


async def test_the_same_ids_in_two_workspaces_share_no_session_no_queue_and_no_seen_message(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    first = world.message("from one", conversation="C1", sender="U1", thread="root")
    # The same conversation, sender, thread and message id, in another workspace.
    twin = replace(first, workspace=OTHER_WORKSPACE, event_id="e-twin", text="from two")
    world.kit.play(ModelTurn(final="one answered", hold=True), ModelTurn(final="two answered"))

    assert (await world.platform.inject(first)).startswith("dispatched:")
    await world.kit.holding()
    # Neither a second delivery of the first message, nor a message that waits behind its turn.
    assert (await world.platform.inject(twin)).startswith("dispatched:")
    await world.handed(2)

    one, two = (request.conversation for request in world.requests)
    assert one != two
    # The tools of each session are open to its own turn only. The first workspace's turn is the one still running.
    react = {"name": "react_to_message", "arguments": {"emoji": "eyes"}}
    assert (await world.host.tools.authorize(ToolCall(conversation=one, **react))).action == VERDICT_ALLOW
    # Control: in the first workspace the same message again is a duplicate, and a new one waits its turn.
    assert await world.platform.inject(replace(first, event_id="e-again")) == "ignored:already-handled (dedupe)"
    later = world.message("from one again", conversation="C1", sender="U1", thread="root")
    assert (await world.platform.inject(later)).startswith("queued:")

    world.kit.release()
    await world.host.idle()

    assert world.texts() == ["from one", "from two", "from one again"]
    assert [request.conversation for request in world.requests] == [one, two, one]
    assert (await world.host.tools.authorize(ToolCall(conversation=two, **react))).action == VERDICT_DENY


async def test_a_question_asked_in_one_workspace_is_not_answered_from_another(build_world):
    world = await build_world(
        ANSWER_EVERYTHING + "  - match: {channel: loopback}\n    permissions: {tools: {react_to_message: ask}}\n"
    )
    world.kit.play(ModelTurn(final="done", calls=(("react_to_message", {"emoji": "eyes"}),)))
    await world.platform.inject(world.message("react", conversation="C1"))
    (shown,) = await world.wait_question(conversation="C1")

    elsewhere = replace(world.answer_to(shown, approved=True), workspace=OTHER_WORKSPACE)

    assert await world.platform.inject(elsewhere) == "ignored:question-not-waiting"
    assert [action for action in world.platform.log if action.kind == "reaction"] == []
    # Control: the same answer from the workspace that asked releases the call.
    assert (await world.send(world.answer_to(shown, approved=True))).startswith("answered")
    assert [action.name for action in world.platform.log if action.kind == "reaction"] == ["eyes"]


async def test_every_action_the_host_asks_for_names_the_workspace_of_its_message(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    first = world.message("from one", conversation="C1")
    world.kit.play(ModelTurn(final="one answered"), ModelTurn(final="two answered", ask=("Proceed?", ("yes", "no"))))

    await world.send(first)
    await world.platform.inject(replace(first, workspace=OTHER_WORKSPACE, event_id="e-twin"))
    (shown,) = await world.wait_question()
    await world.send(replace(world.answer_to(shown, values=("yes",)), workspace=OTHER_WORKSPACE))

    named = [(action.kind, action.workspace) for action in world.platform.log]
    assert named == [("post", "W1"), ("question", OTHER_WORKSPACE), ("post", OTHER_WORKSPACE)]


async def test_post_tool_keeps_identical_message_ids_in_separate_workspaces(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    if world.platform_kit.name != "loopback":
        pytest.skip("loopback owns the in-process message identity registry")
    first = world.message("from one", conversation="C1", sender="U1", thread="root")
    twin = replace(first, workspace=OTHER_WORKSPACE, event_id="e-twin", text="from two")
    call = ("post_message", {"text": "tool reply", "reply_to_message_id": "m1"})
    world.kit.play(ModelTurn(final="done one", calls=(call,)), ModelTurn(final="done two", calls=(call,)))

    await world.send(first)
    await world.send(twin)

    posts = [action for action in world.platform.log if action.kind == "post"]
    assert [(post.workspace, post.conversation, post.thread, post.text) for post in posts] == [
        ("W1", "C1", "root", "tool reply"),
        (OTHER_WORKSPACE, "C1", "root", "tool reply"),
    ]
    assert len({post.message_id for post in posts}) == 2


async def test_confirmation_in_one_workspace_does_not_authorize_a_post_in_another(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    if world.platform_kit.name != "loopback":
        pytest.skip("loopback confirmation targets include a workspace")
    world.platform_kit.widens(world.platform, "C1", "C2", workspace="W1")
    world.kit.play(ModelTurn(final="handled", calls=(("post_message", {"text": "cross-workspace", "chat_id": "C2"}),)))

    await world.send(replace(world.message("try the post", conversation="C1"), workspace=OTHER_WORKSPACE))

    assert world.questions() == []
    assert world.posts("C2") == []
    assert [(post.workspace, post.conversation, post.text) for post in world.posts()] == [
        (OTHER_WORKSPACE, "C1", "handled"),
    ]
