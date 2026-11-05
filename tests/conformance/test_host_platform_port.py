"""What the host asks of a platform beyond delivering text: marks, placement, the envelope and files.

These are checked on the loopback platform, whose records a check writes field by field and whose
marks it reads back. ``tests/test_slack_connector.py`` checks what Slack does with the same calls.
"""

import pytest
from conformance_kit import LoopbackKit, ModelTurn, as_file, scripted_second_brain
from scripted_backend import ScriptedBackend, ScriptedTurn

from belle_agent import host
from belle_agent.platforms.base import (
    MARK_DONE,
    MARK_FAILED,
    MARK_INGEST_DECLINED,
    MARK_INGEST_EMPTY,
    MARK_INGEST_FAILED,
    MARK_INGESTED,
    MARK_INGESTING,
    MARK_QUEUED,
    MARK_RECEIVED,
    MARK_REFUSED,
    MARK_STARTED,
    MARK_STOPPED,
)
from belle_agent.second_brain.hosted import SecondBrain

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all]}
"""


UNREADABLE = {"uri": "file:///etc/hostname", "name": "hostname"}


def _ingesting(directory) -> str:
    return f"second_brain: {{enabled: true, corpus_dir: {directory / 'corpus'}, ingest: {{channels: [C-DOCS]}}}}\n"


async def test_a_turn_has_a_platform_neutral_place(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    message = world.message("hello", thread="root")
    await world.send(message)

    assert world.requests[-1].metadata["belle_place"] == {
        "platform": world.platform_kit.name,
        "workspace": message.workspace,
        "conversation": message.conversation,
        "thread": "root",
        "sender": message.sender,
        "chat_type": message.chat_type,
        "direct": False,
    }


@pytest.fixture
def kit() -> ScriptedBackend:
    return ScriptedBackend()


@pytest.fixture
def platform_kit() -> LoopbackKit:
    return LoopbackKit()


async def test_the_platform_is_told_what_became_of_each_message_in_order(build_world):
    world = await build_world(ANSWER_EVERYTHING)
    world.kit.play(ModelTurn(final="first", hold=True), ModelTurn(final="second"))
    first, second = world.message("one", thread="root"), world.message("two", thread="root")

    await world.platform.inject(first)
    await world.platform.inject(second)
    await world.handed(1)
    assert world.platform.marks == [(first.message_id, MARK_RECEIVED), (second.message_id, MARK_QUEUED)]

    world.kit.release()
    await world.host.idle()

    assert world.platform.marks[2:] == [
        (first.message_id, MARK_DONE),
        (second.message_id, MARK_STARTED),
        (second.message_id, MARK_DONE),
    ]
    # A mark is not an outbound action: the log holds the two answers and nothing else.
    assert [action.kind for action in world.platform.log] == ["post", "post"]


async def test_a_failed_a_stopped_and_a_refused_message_are_each_marked_as_such(build_world):
    world = await build_world("platforms: {loopback: {allow_from: [U1]}}\n" + ANSWER_EVERYTHING)
    world.kit.play(ScriptedTurn(error="The provider refused the request."), ModelTurn(final="never said", hold=True))
    failed, stopped, refused = world.message("one"), world.message("two"), world.message("three", sender="U2")

    await world.send(failed)
    await world.platform.inject(stopped)
    await world.kit.holding()
    await world.platform.inject(world.stop(stopped))
    world.kit.release()
    await world.host.idle()
    await world.send(refused)

    assert world.platform.marks == [
        (failed.message_id, MARK_RECEIVED),
        (failed.message_id, MARK_FAILED),
        (stopped.message_id, MARK_RECEIVED),
        (stopped.message_id, MARK_STOPPED),
        (refused.message_id, MARK_REFUSED),
    ]


async def test_a_mark_the_platform_cannot_make_changes_nothing_else(build_world, monkeypatch):
    world = await build_world(ANSWER_EVERYTHING)

    async def broken(inbound, state):
        raise RuntimeError("the platform refused the mark")

    monkeypatch.setattr(world.platform, "mark", broken)
    world.kit.play(ModelTurn(final="the answer"))

    await world.send(world.message("a question"))

    assert [post.text for post in world.posts()] == ["the answer"]
    assert world.outcomes() == [host.OUTCOME_POSTED_FINAL]


async def test_an_answer_goes_where_the_platform_places_it(build_world):
    world = await build_world(ANSWER_EVERYTHING)

    # The platform's own rule can open a thread under a message that has none, or answer beside a thread.
    await world.send(world.message("one", reply_thread="m-one"))
    await world.send(world.message("two", thread="root", reply_thread=""))
    # Control: with no placement the answer goes in the message's own thread.
    await world.send(world.message("three", thread="root"))

    assert [post.thread for post in world.posts("C1")] == ["m-one", "", "root"]
    # Where the answer goes does not change which conversation a message belongs to.
    one, two, three = (request.conversation for request in world.requests)
    assert two == three != one


async def test_a_turn_is_told_what_the_platform_adds_about_its_message(build_world):
    world = await build_world(ANSWER_EVERYTHING)

    await world.send(world.message("a question", envelope="[id: m1]"))
    await world.send(world.message("another"))

    assert [request.text for request in world.requests] == ["a question\n\n[id: m1]", "another"]


async def test_a_file_is_read_only_once_its_message_has_a_turn(build_world, monkeypatch):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [has_file]}
"""
    )
    asked: list[str] = []
    fetch = world.platform.fetch_files

    async def counted(inbound):
        asked.append(inbound.message_id)
        return await fetch(inbound)

    monkeypatch.setattr(world.platform, "fetch_files", counted)
    ignored = world.message("not for the bot", conversation="C2", files=(as_file("kite-bridle.md"),))
    answered = world.message("for the bot", files=(as_file("kite-bridle.md"),))

    await world.send(ignored)
    await world.send(answered)

    # A platform that holds content by reference downloads nothing for a message nobody answers.
    assert asked == [answered.message_id]


ANSWER_DOCUMENTS = """
scopes:
  - match: {channel: loopback, chat: C-DOCS}
    delivery: {mode: [has_file]}
"""


async def test_a_file_of_a_message_that_is_ingested_and_answered_is_read_once_for_both(build_world, tmp_path, monkeypatch):
    world = await build_world(_ingesting(tmp_path) + ANSWER_DOCUMENTS, second_brain=scripted_second_brain())
    asked: list[str] = []
    fetch = world.platform.fetch_files

    async def counted(inbound):
        asked.append(inbound.message_id)
        return await fetch(inbound)

    monkeypatch.setattr(world.platform, "fetch_files", counted)
    shared = world.message("see this", conversation="C-DOCS", files=(as_file("kite-bridle.md"),))

    await world.send(shared)

    # One download serves both the ingest and the turn.
    assert asked == [shared.message_id]
    assert world.requests[-1].files == shared.files
    assert (shared.message_id, MARK_INGESTED) in world.platform.marks


async def test_a_message_whose_file_could_not_be_read_and_that_says_nothing_else_runs_no_turn(build_world, monkeypatch):
    world = await build_world(
        """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [has_file]}
"""
    )

    async def unreadable(inbound):
        return ()

    monkeypatch.setattr(world.platform, "fetch_files", unreadable)

    await world.send(world.message("", files=(as_file("kite-bridle.md"),)))

    assert world.requests == []
    assert world.posts() == []
    assert world.outcomes() == [host.OUTCOME_FAILED]


@pytest.mark.parametrize(
    ("files", "ending"),
    [
        ((as_file("kite-bridle.md"), UNREADABLE), MARK_INGESTED),
        ((UNREADABLE, as_file("empty.md", "")), MARK_INGEST_FAILED),
        ((as_file("empty.md", ""),), MARK_INGEST_DECLINED),
    ],
)
async def test_a_shared_document_is_marked_while_it_is_ingested_and_then_by_its_outcome(build_world, tmp_path, files, ending):
    world = await build_world(_ingesting(tmp_path), second_brain=scripted_second_brain())
    shared = world.message("see this", conversation="C-DOCS", files=files)

    assert await world.send(shared) == "ingested:second-brain"

    assert world.platform.marks == [(shared.message_id, MARK_INGESTING), (shared.message_id, ending)]


@pytest.mark.parametrize(("ingest", "ending"), [([], MARK_INGEST_EMPTY), (None, MARK_INGEST_FAILED)])
async def test_an_ingest_with_no_result_or_that_raises_still_ends_its_mark(build_world, tmp_path, monkeypatch, ingest, ending):
    async def ingested(self, text, files, meta):
        if ingest is None:
            raise RuntimeError("the corpus is gone")
        return ingest

    monkeypatch.setattr(SecondBrain, "ingest", ingested)
    world = await build_world(_ingesting(tmp_path), second_brain=scripted_second_brain())
    shared = world.message("see https://example.test/doc", conversation="C-DOCS")

    await world.send(shared)

    assert world.platform.marks == [(shared.message_id, MARK_INGESTING), (shared.message_id, ending)]
