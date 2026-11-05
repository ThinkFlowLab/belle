"""How a turn reaches its readers, and when it stays silent. Every backend must pass."""

import json

import pytest
from conformance_kit import WORKSPACE, ModelTurn

from belle_agent.host import OUTCOME_FAILED, OUTCOME_POSTED_BY_TOOL, OUTCOME_POSTED_FINAL, OUTCOME_WITHHELD

OPTIONAL_REPLY_IN_C1 = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], reply: optional}
"""


async def test_an_optional_reply_with_no_posting_call_posts_nothing(build_world):
    world = await build_world(OPTIONAL_REPLY_IN_C1)
    world.kit.play(ModelTurn(final="a thought nobody asked for"))

    await world.send(world.message("just talking", thread="root"))

    assert world.platform.log == []
    # Silence is no post beside the host's own record that the turn ended.
    assert world.outcomes() == [OUTCOME_WITHHELD]
    assert world.requests[0].scope.withhold_final is True

    # Control: in the same session, a turn that calls the posting tool is read.
    world.kit.play(ModelTurn(final="done", calls=(("post_message", {"text": "worth saying"}),)))
    await world.send(world.message("a question", thread="root"))

    assert world.requests[0].conversation == world.requests[1].conversation
    assert [post.text for post in world.posts("C1")] == ["worth saying"]
    assert world.outcomes() == [OUTCOME_WITHHELD, OUTCOME_POSTED_BY_TOOL]


async def test_an_optional_reply_tells_the_turn_how_to_reach_readers(build_world):
    world = await build_world(OPTIONAL_REPLY_IN_C1)

    await world.send(world.message("just talking"))
    await world.send(world.direct("hello"))

    optional, direct = (request.scope for request in world.requests)
    assert any("post_message" in addition for addition in optional.system_additions)
    # A direct message is owed an answer, so its final is delivered and nothing says otherwise.
    assert direct.system_additions == ()
    assert direct.withhold_final is False


async def test_a_turn_ends_when_the_work_it_started_has_ended_and_posts_each_answer_once(world):
    started = ModelTurn(final="The search is started.", background="The search found the tide tables.")
    world.kit.play(started, ModelTurn(final="the next turn"))

    await world.send(world.direct("look for the tide tables"))

    # One turn and one post: the answer given when the work started, then the answer to what it reported.
    assert [post.text for post in world.posts()] == ["The search is started.\n\nThe search found the tide tables."]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]

    # Control: the conversation is free for its next turn, and nothing of the first is posted again.
    await world.send(world.direct("and now"))
    assert [post.text for post in world.posts()][1:] == ["the next turn"]


async def test_a_required_reply_is_the_final_posted_once(world):
    world.kit.play(ModelTurn(final="the answer"))

    await world.send(world.direct("a question"))

    assert [post.text for post in world.posts()] == ["the answer"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_a_required_reply_with_an_empty_final_is_a_failure(world):
    world.kit.play(ModelTurn(final="   "), ModelTurn(final="the answer"))

    await world.send(world.direct("hello"))

    assert world.platform.log == []
    assert world.outcomes() == [OUTCOME_FAILED]

    # Control: the conversation's next turn is answered.
    await world.send(world.direct("hello again"))
    assert [post.text for post in world.posts()] == ["the answer"]


async def test_a_required_reply_given_through_the_tool_is_not_posted_twice(world):
    # The JiuwenSwarm Slack channel posts a required final whether or not the tool posted.
    # The host does not. This check holds the host to what it does today, on every backend.
    world.kit.play(ModelTurn(final="the answer again", calls=(("post_message", {"text": "the answer"}),)))

    await world.send(world.direct("a question"))

    assert [post.text for post in world.posts()] == ["the answer"]
    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]


def _posts_without_asking(world, monkeypatch, conversation: str) -> None:
    """Let the posting tool reach ``conversation`` with no person asked, as a platform does where nobody new would read it."""
    run = world.host._tools.run_confirmed

    async def run_confirmed(call, turn):
        if call.name != "post_message" or call.arguments.get("chat_id") != conversation:
            return await run(call, turn)
        message_id = await world.platform.post(conversation, "", call.arguments["text"], workspace=WORKSPACE)
        return json.dumps({"ok": True, "chat_id": conversation, "message_id": message_id, "thread_id": ""})

    monkeypatch.setattr(world.host._tools, "run_confirmed", run_confirmed)


@pytest.mark.parametrize("platform_kit", ["loopback"], indirect=True)
async def test_a_post_in_another_conversation_does_not_replace_the_reply_owed_here(world, monkeypatch):
    _posts_without_asking(world, monkeypatch, "C2")
    world.kit.play(ModelTurn(final="Posted it in C2.", calls=(("post_message", {"text": "for C2", "chat_id": "C2"}),)))
    message = world.direct("post this in C2 and tell me here")

    await world.send(message)

    assert [post.text for post in world.posts("C2")] == ["for C2"]
    assert [post.text for post in world.posts(message.conversation)] == ["Posted it in C2."]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


@pytest.mark.parametrize("platform_kit", ["loopback"], indirect=True)
async def test_a_post_in_another_conversation_with_no_final_is_not_a_failure(world, monkeypatch):
    # The post was made. Failing the message would invite a retry, and with it a second post.
    _posts_without_asking(world, monkeypatch, "C2")
    world.kit.play(ModelTurn(final="", calls=(("post_message", {"text": "for C2", "chat_id": "C2"}),)))
    message = world.direct("post this in C2")

    await world.send(message)

    assert [post.text for post in world.posts("C2")] == ["for C2"]
    assert world.posts(message.conversation) == []
    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]


async def test_a_reaction_lands_on_the_source_message_and_posts_nothing(build_world):
    world = await build_world(OPTIONAL_REPLY_IN_C1)
    world.kit.play(ModelTurn(final="", calls=(("react_to_message", {"emoji": "eyes"}),)))
    message = world.message("look at this")

    await world.send(message)

    assert [(action.kind, action.message_id, action.name) for action in world.platform.log] == [
        ("reaction", message.message_id, "eyes")
    ]
    assert world.outcomes() == [OUTCOME_WITHHELD]
