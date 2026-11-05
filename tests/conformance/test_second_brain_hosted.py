"""What a host does with its second brain that needs a backend to fail or be counted. Scripted backend only."""

import asyncio
import logging

import pytest
from conformance_kit import DOCUMENTS, ModelTurn, as_file, scripted_second_brain
from scripted_backend import ScriptedBackend

from belle_agent.host import OUTCOME_POSTED_FINAL
from belle_agent.second_brain.hosted import STATUS_FAILED, SecondBrain

FENCE = "<<<BRAIN_DOC doc_id="


@pytest.fixture
def kit() -> ScriptedBackend:
    return ScriptedBackend()


def _settings(directory, channels: str = "[C-DOCS]") -> str:
    return f"""
second_brain: {{enabled: true, corpus_dir: {directory / "corpus"}, ingest: {{channels: {channels}}}}}
scopes:
  - match: {{channel: loopback, chat: C1}}
    delivery: {{mode: [all], session: channel}}
"""


async def test_a_second_delivery_of_a_shared_document_is_ingested_once(build_world, tmp_path, caplog):
    world = await build_world(_settings(tmp_path), second_brain=scripted_second_brain())
    shared = world.message("see this", conversation="C-DOCS", files=(as_file("kite-bridle.md"),))

    with caplog.at_level(logging.INFO, logger="belle_agent.host"):
        assert [await world.send(shared), await world.send(shared)] == ["ingested:second-brain"] * 2

    # The second delivery was not handed to the corpus at all: no link is fetched twice.
    handed = [record.getMessage() for record in caplog.records if "second brain:" in record.getMessage()]
    assert len(handed) == 1 and "status=ingested" in handed[0]


async def test_a_file_in_a_direct_message_is_not_ingested_where_every_conversation_is(build_world, tmp_path):
    world = await build_world(_settings(tmp_path, '["*"]'), second_brain=scripted_second_brain())
    world.kit.play(ModelTurn(final="kept"), ModelTurn(final="listed", calls=(("brain_list", {}),)))

    # A direct message asks the bot something. It is answered, and what it carries is not shared with a room.
    await world.send(world.direct("keep this for me", files=(as_file("hive-inspection.md"),)))
    await world.send(world.message("see this", conversation="C-ANY", files=(as_file("kite-bridle.md"),)))
    await world.send(world.message("which documents are there?", conversation="C1"))

    assert "1 document(s)" in world.kit.results[-1]
    assert "kite-bridle-" in world.kit.results[-1] and "hive-inspection" not in world.kit.results[-1]


async def test_one_file_that_cannot_be_read_does_not_stop_the_next(tmp_path):
    brain = SecondBrain({"second_brain": {"corpus_dir": str(tmp_path)}}, scripted_second_brain())

    local, shared = await brain.ingest("", ({"uri": "file:///etc/hostname", "name": "hostname"}, as_file("kite-bridle.md")), {})

    # Only a data: URI is opened. A path on this machine is not a document someone shared.
    assert local["status"] == STATUS_FAILED and "data: URI" in local["reason"]
    assert shared["status"] == "ingested" and shared["doc_id"].startswith("kite-bridle-")


async def test_an_index_that_cannot_be_opened_leaves_the_host_starting_and_the_turn_running(build_world, tmp_path):
    # A file where the corpus directory should be: nothing can be indexed there.
    (tmp_path / "corpus").write_text("not a directory")

    world = await build_world(_settings(tmp_path), second_brain=scripted_second_brain())
    world.kit.play(ModelTurn(final="answered all the same", calls=(("brain_search", {"query": "kite"}),)))

    await world.send(world.message("why does a delta kite stall?", conversation="C1"))

    assert [post.text for post in world.posts("C1")] == ["answered all the same"]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
    assert not any(FENCE in text for text in world.requests[0].scope.system_additions)
    # The search says it could not look. It does not say that nothing matched.
    assert "not available" in world.kit.results[-1] and "matched" not in world.kit.results[-1]


async def test_a_message_that_waited_keeps_its_place_and_is_matched_when_its_turn_starts(build_world, tmp_path):
    world = await build_world(_settings(tmp_path), second_brain=scripted_second_brain())
    world.kit.play(ModelTurn(final="first", hold=True), ModelTurn(final="second"))

    await world.platform.inject(world.message("hold on", conversation="C1"))
    await world.handed(1)
    # The question waits behind the running turn. Its documents arrive only now.
    assert (await world.platform.inject(world.message("why does a delta kite stall?", conversation="C1"))).startswith("queued:")
    for name in DOCUMENTS:
        await world.platform.inject(world.message(f"see {name}", conversation="C-DOCS", files=(as_file(name),)))
    while len(world.host._tasks) > 1:
        await asyncio.sleep(0.01)
    world.kit.release()
    await world.host.idle()

    assert [post.text for post in world.posts("C1")] == ["first", "second"]
    assert any(FENCE + "kite-bridle-" in text for text in world.requests[1].scope.system_additions)


async def test_the_host_names_each_second_brain_setting_that_does_nothing(build_world, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await build_world(_settings(tmp_path, "[public, C-DOCS]"))

    warned = [record.getMessage() for record in caplog.records if "second_brain" in record.getMessage()]
    assert any("no second brain backend is loaded" in text and "nobody looked" in text for text in warned)
    assert any("'public'" in text and "selects nothing" in text for text in warned)


async def test_the_host_is_quiet_about_a_second_brain_that_works(build_world, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="belle_agent.host"):
        await build_world(_settings(tmp_path, '["*"]'), second_brain=scripted_second_brain())

    assert "second_brain" not in caplog.text


def test_only_its_own_id_selects_an_end_to_end_encrypted_conversation(tmp_path):
    def brain(*channels: str) -> SecondBrain:
        return SecondBrain({"second_brain": {"corpus_dir": str(tmp_path), "ingest": {"channels": list(channels)}}}, scripted_second_brain())

    listed, everything = brain("public", "E1"), brain("*")

    assert listed.ingests_from("E1", encrypted=True)
    # Neither * nor public selects an encrypted conversation: its server cannot read it, and the corpus can.
    assert not listed.ingests_from("E2", public=True, encrypted=True)
    assert not everything.ingests_from("E2", encrypted=True)
    assert listed.ingests_from("E2", public=True) and everything.ingests_from("E3")
