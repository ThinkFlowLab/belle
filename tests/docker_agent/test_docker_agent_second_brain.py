"""The second brain through the Docker Agent container: its tools over MCP, and the recall of a turn.

The second brain runs in the Belle process. Its backend here has no library
behind it, as in the conformance suite.
"""

import pytest
from conformance_kit import DOCUMENTS, as_file, scripted_second_brain
from standin import call, said

pytestmark = pytest.mark.docker_agent_server

FENCE = "<<<BRAIN_DOC doc_id="
#: Docker Agent replaces a text tool result over 50 KiB or 2 000 lines with this notice and the result's end.
TOO_LARGE = "Tool call result was too large"
#: A document whose full text exceeds Docker Agent's text result limit.
LONG = "# Lighthouse Log\n\n" + "".join(
    f"Entry {number:04}: the lamp turned and the keeper wrote.\n" for number in range(1100)
)


@pytest.fixture
async def world(build_world, tmp_path):
    """A host whose second brain ingests from C-DOCS. Every message in C1 starts a turn."""
    return await build_world(
        f"""
second_brain:
  enabled: true
  corpus_dir: {tmp_path / "corpus"}
  ingest: {{channels: [C-DOCS]}}
scopes:
  - match: {{channel: loopback, chat: C1}}
    delivery: {{mode: [all]}}
""",
        second_brain=scripted_second_brain(),
    )


async def _share(world, documents: dict[str, str]) -> None:
    for name, text in documents.items():
        shared = world.message(f"see {name}", conversation="C-DOCS", files=(as_file(name, text),))
        assert await world.send(shared) == "ingested:second-brain"


def _requests(model, text: str) -> list[dict]:
    """The provider requests of the turn whose message was ``text``, in order."""
    return [entry["body"] for entry in list(model.log) if text in said(entry["body"], "user")]


async def _called(world, model, tool: str, **arguments) -> str:
    """What the model read back from one call of a Belle tool."""
    text = call(world.kit.backend.capabilities().tool_name(tool), **arguments)
    await world.send(world.message(text, conversation="C1"))
    return said(_requests(model, text)[-1], "tool")[-1]


async def test_an_ingested_document_is_listed_to_the_model_and_a_matching_turn_carries_its_recall(world, model):
    await _share(world, DOCUMENTS)

    listing = await _called(world, model, "brain_list")
    assert "5 document(s) in the second brain" in listing and "kite-bridle-" in listing

    matching, unrelated = "why does a delta kite stall? SAY=aft", "thanks, see you tomorrow SAY=bye"
    await world.send(world.message(matching, conversation="C1"))
    await world.send(world.message(unrelated, conversation="C1"))
    # The recall reaches the model as system text of its own turn only.
    recalled = "\n".join(said(_requests(model, matching)[0], "system"))
    assert FENCE + "kite-bridle-" in recalled and "A delta kite stalls" in recalled
    assert FENCE + "hive-inspection-" not in recalled
    later = "\n".join(said(_requests(model, unrelated)[0], "system"))
    assert FENCE not in later and "## Second brain" in later


async def test_a_text_result_over_docker_agents_limit_reaches_the_model_as_its_end(world, model):
    await _share(world, {"lighthouse-log.md": LONG})
    doc_id = next(
        word.rstrip(":")
        for word in (await _called(world, model, "brain_list")).split()
        if word.startswith("lighthouse-log-")
    )

    refused = await _called(world, model, "brain_read", doc_id=doc_id, location="full", max_chars=20000)
    assert "exceeding max_chars=20000" in refused
    assert f"max_chars to at least {len(LONG)}" in refused
    assert FENCE not in refused and not refused.startswith(TOO_LARGE)

    read = await _called(world, model, "brain_read", doc_id=doc_id, location="full", max_chars=len(LONG))

    assert read.startswith(TOO_LARGE)
    assert "Entry 1099:" in read and "Entry 0000:" not in read
