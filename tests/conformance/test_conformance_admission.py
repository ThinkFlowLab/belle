"""Who gets a turn, once, and in what order. Every backend must pass."""

from dataclasses import replace

from conformance_kit import ModelTurn, as_file

from belle_agent import host
from belle_agent.backend import OP_STEER
from belle_agent.platforms.base import TRIGGER_MENTION, TRIGGER_REPLY

ANSWER_EVERYTHING_IN_C1 = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all]}
"""


async def test_a_direct_message_gets_one_reply(world):
    world.kit.play(ModelTurn(final="hello back"))

    outcome = await world.send(world.direct("hello"))

    assert outcome.startswith("dispatched:dm")
    assert [(post.conversation, post.text) for post in world.posts()] == [("D-U1", "hello back")]
    assert world.texts() == ["hello"]


async def test_a_second_delivery_of_one_event_starts_no_turn(world):
    first = world.direct("hello")
    await world.send(first)

    assert await world.send(first) == "ignored:already-handled (dedupe)"
    assert len(world.posts()) == 1
    assert len(world.requests) == 1

    # Control: the same text under a new id is a new message.
    await world.send(world.direct("hello"))
    assert len(world.posts()) == 2
    assert len(world.requests) == 2


async def test_one_message_delivered_as_two_events_starts_one_turn(world):
    mention = world.message("hello", trigger=TRIGGER_MENTION)
    await world.send(mention)

    assert await world.send(replace(mention, event_id="another-event")) == "ignored:already-handled (dedupe)"
    assert len(world.requests) == 1


async def test_a_channel_message_needs_a_trigger(world):
    assert await world.send(world.message("just talking")) == "ignored:no-trigger-matched"
    assert world.requests == []
    assert world.posts() == []

    # Control: the same conversation answers a mention.
    await world.send(world.message("a question", trigger=TRIGGER_MENTION))
    assert len(world.requests) == 1
    assert len(world.posts("C1")) == 1


async def test_a_scope_decides_what_a_trigger_does(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [reply]}
"""
    )

    # The platform reports the mention. The scope for C1 does not answer mentions.
    assert await world.send(world.message("a question", trigger=TRIGGER_MENTION)) == "ignored:no-trigger-matched"
    assert (await world.send(world.message("and then?", trigger=TRIGGER_REPLY, thread="m0"))).startswith(
        "dispatched:reply"
    )
    # Control: another conversation keeps the default and answers a mention.
    assert (await world.send(world.message("a question", conversation="C2", trigger=TRIGGER_MENTION))).startswith(
        "dispatched:mention"
    )


async def test_an_edit_and_an_app_message_start_no_turn(build_world):
    world = await build_world(ANSWER_EVERYTHING_IN_C1)

    assert await world.send(world.message("reworded", edited=True)) == "ignored:edit-is-not-new-content"
    assert await world.send(world.message("from an app", sender_is_bot=True)) == "ignored:posted-by-an-app"
    assert world.requests == []

    # Control: an ordinary message in the same conversation runs.
    await world.send(world.message("new content"))
    assert len(world.requests) == 1


async def test_two_quick_messages_run_in_arrival_order(build_world):
    world = await build_world(ANSWER_EVERYTHING_IN_C1)
    world.kit.play(ModelTurn(final="first answer", hold=True), ModelTurn(final="second answer"))
    thread = {"thread": "root"}

    assert (await world.platform.inject(world.message("first", **thread))).startswith("dispatched:")
    assert (await world.platform.inject(world.message("second", **thread))).startswith("queued:")
    await world.handed(1)
    # The second message waits: the backend has been handed one turn only.
    assert world.texts() == ["first"]

    world.kit.release()
    await world.host.idle()

    assert world.texts() == ["first", "second"]
    assert [post.text for post in world.posts("C1")] == ["first answer", "second answer"]


async def test_a_message_during_a_turn_joins_it_where_the_backend_steers_and_waits_where_it_does_not(build_world):
    world = await build_world(ANSWER_EVERYTHING_IN_C1.replace("mode: [all]", "mode: [all], mid_turn: steer"))
    world.kit.play(ModelTurn(final="first answer", hold=True), ModelTurn(final="second answer"))
    thread = {"thread": "root"}

    assert (await world.platform.inject(world.message("first", **thread))).startswith("dispatched:")
    await world.kit.holding()
    outcome = await world.platform.inject(world.message("second", **thread))
    world.kit.release()
    await world.host.idle()

    if OP_STEER in world.host.backend_capabilities.operations:
        # The model reads the second message within the first turn, and that turn's last answer is its reply.
        assert outcome.startswith("steered:")
        assert world.texts() == ["first"]
        assert [post.text for post in world.posts("C1")] == ["second answer"]
    else:
        assert outcome.startswith("queued:")
        assert world.texts() == ["first", "second"]
        assert [post.text for post in world.posts("C1")] == ["first answer", "second answer"]


async def test_a_thread_is_one_conversation_and_two_threads_are_two(build_world):
    world = await build_world(ANSWER_EVERYTHING_IN_C1)

    await world.send(world.message("one", thread="root-a"))
    await world.send(world.message("two", thread="root-a"))
    await world.send(world.message("three", thread="root-b"))

    first, second, third = (request.conversation for request in world.requests)
    assert first == second != third
    # Each answer lands in the thread its message came from.
    assert [post.thread for post in world.posts("C1")] == ["root-a", "root-a", "root-b"]


async def test_a_turn_that_does_not_end_is_stopped_and_frees_its_conversation(world, monkeypatch):
    monkeypatch.setattr(host, "TURN_TIMEOUT_SECONDS", 3.0)
    world.kit.play(ModelTurn(final="too late", hold=True), ModelTurn(final="fine now"))

    await world.send(world.direct("first"))

    # The reader is owed an answer, so it is told. The held final never arrives.
    assert [post.text for post in world.posts()] == [host.TURN_TIMEOUT_NOTICE]
    assert world.outcomes() == [host.OUTCOME_FAILED]

    world.kit.release()
    await world.send(world.direct("second"))
    assert [post.text for post in world.posts()] == [host.TURN_TIMEOUT_NOTICE, "fine now"]


ONLY_SOME_ARE_SERVED = """
platforms:
  loopback:
    allow_from: [U1, U-LEAD]
    allowed_channel_ids: [C1]
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
  - match: {channel: loopback, chat: C3}
    delivery: {prompt: Be brief.}
"""


async def test_only_the_people_and_the_conversations_the_platform_lists_are_served(build_world):
    world = await build_world(ONLY_SOME_ARE_SERVED)

    assert await world.send(world.message("one", conversation="C2")) == "ignored:channel-not-in-allowed_channel_ids"
    assert await world.send(world.message("two", sender="U2")) == "refused:user-not-in-allow_from"
    assert world.requests == []
    assert world.posts() == []

    # Controls: a listed person in a listed conversation, in a conversation a scope of its own opts in,
    # and in a direct message, which the list of conversations does not hold.
    for message in (world.message("three"), world.message("four", conversation="C3"), world.direct("five")):
        assert (await world.send(message)).startswith("dispatched:")
    assert world.texts() == ["three", "four", "five"]


async def test_a_link_or_a_file_starts_a_turn_where_the_scope_answers_them(build_world):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [url, has_file]}
"""
    )

    assert await world.send(world.message("nothing to follow")) == "ignored:no-trigger-matched"
    assert (await world.send(world.message("see https://example.test/page"))).startswith("dispatched:url")
    assert (await world.send(world.message("the notes", files=(as_file("kite-bridle.md"),)))).startswith(
        "dispatched:has_file"
    )
    # Control: a conversation whose scope does not name them answers neither.
    assert await world.send(world.message("see https://example.test/page", conversation="C2")) == (
        "ignored:no-trigger-matched"
    )

    link, shared = world.requests
    assert link.files == ()
    # The turn is handed the file with its content, whatever way the platform held it.
    assert [(entry["name"], entry["uri"].partition(",")[0]) for entry in shared.files] == [
        ("kite-bridle.md", "data:text/markdown;base64")
    ]
