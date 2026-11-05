"""What a running turn shows: its reply as it is written, and what it is doing. Every backend must pass.

A check reads what the host handed its platform (``progressed``), how it ended
what the platform showed (``concluded``), and what was posted. A platform that
shows nothing of a running turn has no ``shows`` in its kit and is skipped.
"""

import pytest
from conformance_kit import ModelTurn

from belle_agent.backend import EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT
from belle_agent.host import OUTCOME_INTERRUPTED, OUTCOME_POSTED_BY_TOOL, OUTCOME_POSTED_FINAL, OUTCOME_WITHHELD
from belle_agent.platforms.base import ENDED_SILENT, MARK_DONE, MARK_STOPPED

OPTIONAL_REPLY_IN_C1 = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], reply: optional}
"""


@pytest.fixture
async def showing(build_world, platform_kit):
    """Return a builder of a world whose platform shows all it can of a running turn."""
    shows = getattr(platform_kit, "shows", None)
    if shows is None:
        pytest.skip("this platform shows nothing of a running turn")

    async def build(settings: str = ""):
        return await build_world(shows + settings)

    return build


def written(world, message) -> str:
    """The reply to a message as the platform was handed it while it was written."""
    return "".join(
        event.text for message_id, event in world.platform.progressed
        if message_id == message.message_id and event.kind == EVENT_TEXT_DELTA
    )


def doing(world, message) -> list[tuple[str, str]]:
    """What the turn of a message did, as the platform was handed it: each tool event's kind and tool name."""
    return [
        (event.kind, event.data.get("name"))
        for message_id, event in world.platform.progressed
        if message_id == message.message_id and event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT)
    ]


async def test_a_required_reply_is_shown_as_it_is_written_and_ends_as_one_post(showing):
    world = await showing()
    final = "The tide tables are in the harbour office.\nThe ferry leaves at noon.\n"
    world.kit.play(ModelTurn(final=final))
    message = world.direct("when does the ferry leave")

    await world.send(message)

    # The platform was handed the reply while it was written, and then the whole of it to end with.
    assert written(world, message).strip() == final.strip()
    assert world.platform.concluded == [(message.message_id, MARK_DONE, final.strip())]
    # One message, and it reads as the final: the reply that was shown became it.
    assert [post.text.strip() for post in world.posts()] == [final.strip()]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_an_optional_reply_is_never_shown_while_it_is_written(showing):
    world = await showing(OPTIONAL_REPLY_IN_C1)
    world.kit.play(ModelTurn(final="a thought nobody asked for\n"), ModelTurn(final="an answer that is owed\n"))
    unasked = world.message("just talking")
    asked = world.direct("a question")

    await world.send(unasked)
    await world.send(asked)

    # The final of an optional reply is withheld, so none of it was ever handed over to be shown.
    assert written(world, unasked) == ""
    assert world.platform.concluded[0] == (unasked.message_id, ENDED_SILENT, "")
    assert world.posts("C1") == []
    # Control: with the same backend, a reply that is owed is shown.
    assert written(world, asked).strip() == "an answer that is owed"
    assert world.outcomes() == [OUTCOME_WITHHELD, OUTCOME_POSTED_FINAL]


async def test_a_reply_given_through_the_posting_tool_does_not_end_as_the_final_too(showing):
    world = await showing()
    world.kit.play(ModelTurn(final="posted it\n", calls=(("post_message", {"text": "said through the tool"}),)))
    message = world.direct("say it")

    await world.send(message)

    # The tool's post is the reply. The host hands no final to end with, and nothing written after the post.
    assert [post.text for post in world.posts()] == ["said through the tool"]
    assert world.platform.concluded == [(message.message_id, MARK_DONE, "")]
    assert "posted it" not in written(world, message)
    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]


async def test_what_a_turn_does_is_handed_over_under_the_names_its_scopes_use(showing):
    world = await showing()
    world.kit.play(ModelTurn(final="done\n", calls=(("post_message", {"text": "one"}), ("post_message", {"text": "two"}))))
    message = world.direct("do two things")

    await world.send(message)

    # Each call and its result, in order, named as the core names the tool and not as a backend spells it.
    assert doing(world, message) == [(EVENT_TOOL_CALLED, "post_message"), (EVENT_TOOL_RESULT, "post_message")] * 2
    events = [event for _, event in world.platform.progressed if event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT)]
    calls = [event.data.get("call_id") for event in events]
    # A result names the call it answers.
    assert all(calls) and calls[0] == calls[1] != calls[2] == calls[3]


async def test_a_stopped_turn_ends_what_it_showed_as_stopped_and_posts_nothing(showing):
    world = await showing()
    world.kit.play(ModelTurn(final="said after the stop\n", hold=True), ModelTurn(final="the control\n"))
    message = world.direct("do it")
    await world.platform.inject(message)
    await world.kit.holding()

    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    world.kit.release()
    await world.host.idle()

    assert world.platform.concluded == [(message.message_id, MARK_STOPPED, "")]
    assert world.posts() == []
    assert world.outcomes() == [OUTCOME_INTERRUPTED]

    # Control: the next turn of the conversation is shown and posted.
    await world.send(world.direct("do it now"))
    assert [post.text.strip() for post in world.posts()] == ["the control"]
