"""The second brain behind a host: its tools, the recall of a turn, and ingest. Every backend must pass.

The second brain backend here has no library behind it. What the one that
uses OpenJiuwen adds is checked in ``tests/opencode``.
"""

import json

from conformance_kit import DOCUMENTS, ModelTurn, as_file, scripted_second_brain

from belle_agent.backend import ToolCall
from belle_agent.second_brain.hosted import NOBODY_LOOKED

FENCE = "<<<BRAIN_DOC doc_id="


def _settings(directory, extra: str = "") -> str:
    return f"""
second_brain:
  enabled: true
  corpus_dir: {directory / "corpus"}
  ingest: {{channels: [C-DOCS]}}
scopes:
  - match: {{channel: loopback, chat: C1}}
    delivery: {{mode: [all]}}
{extra}"""


async def _share(world, *names: str, conversation: str = "C-DOCS") -> list[str]:
    return [await world.send(world.message(f"see {name}", conversation=conversation, files=(as_file(name),))) for name in names]


async def _call_in_a_turn(world, name: str, **arguments) -> str:
    """Call one tool as the model of a running turn would, and return what it would read."""
    world.kit.play(ModelTurn(final="done", hold=True))
    await world.platform.inject(world.message("go", conversation="C1"))
    await world.handed(len(world.requests) + 1)
    try:
        call = ToolCall(conversation=world.requests[-1].conversation, name=name, arguments=arguments)
        return await world.host.tools.call(call)
    finally:
        world.kit.release()
        await world.host.idle()


async def test_a_document_shared_where_ingest_is_selected_is_listed_and_read(build_world, tmp_path):
    world = await build_world(_settings(tmp_path), second_brain=scripted_second_brain())

    # No trigger matches in C-DOCS, so the message starts no turn. It is ingested all the same.
    assert await _share(world, "kite-bridle.md") == ["ingested:second-brain"]
    # Control: another file, in a conversation ingest does not select.
    assert await _share(world, "harbour-tides.md", conversation="C-OTHER") == ["ignored:no-trigger-matched"]

    listing = await _call_in_a_turn(world, "brain_list")
    assert "1 document(s) in the second brain" in listing
    assert "kite-bridle" in listing and "harbour-tides" not in listing
    doc_id = next(word.rstrip(":") for word in listing.split() if word.startswith("kite-bridle-"))
    assert "A delta kite stalls when the bridle point moves aft." in await _call_in_a_turn(
        world, "brain_read", doc_id=doc_id, location="full"
    )


async def test_a_turn_is_told_the_documents_its_message_matches_and_no_others(build_world, tmp_path):
    world = await build_world(_settings(tmp_path), second_brain=scripted_second_brain())
    await _share(world, *DOCUMENTS)

    await world.send(world.message("why does a delta kite stall when the bridle moves?", conversation="C1"))
    await world.send(world.message("thanks, see you tomorrow", conversation="C1"))

    matching, unrelated = (request.scope.system_additions for request in world.requests)
    (recalled,) = [text for text in matching if FENCE in text]
    assert FENCE + "kite-bridle-" in recalled and "A delta kite stalls" in recalled
    assert FENCE + "hive-inspection-" not in recalled
    # Control: a message that matches nothing carries no document, and still carries how to use the corpus.
    assert not any(FENCE in text for text in unrelated)
    assert any("## Second brain" in text for text in unrelated)


async def test_a_search_finds_the_document_a_question_is_about(build_world, tmp_path):
    world = await build_world(_settings(tmp_path), second_brain=scripted_second_brain())
    await _share(world, *DOCUMENTS)

    found = await _call_in_a_turn(world, "brain_search", query="varroa mites on the sticky board")
    assert FENCE + "hive-inspection-" in found and FENCE + "kite-bridle-" not in found
    # Control: a search that matches nothing says so, having looked.
    assert await _call_in_a_turn(world, "brain_search", query="zeppelin") == "Nothing in the second brain matched that."


async def test_with_no_second_brain_backend_the_tools_say_nobody_looked(build_world, tmp_path):
    world = await build_world(_settings(tmp_path))

    # Nothing can summarize or index here, so nothing is ingested.
    assert await _share(world, "kite-bridle.md") == ["ignored:no-trigger-matched"]
    for name, arguments in (("brain_search", {"query": "kite"}), ("brain_read", {"doc_id": "x"}), ("brain_list", {})):
        answer = await _call_in_a_turn(world, name, **arguments)
        # Not "the second brain is empty", and not "nothing matched": nobody looked.
        assert answer == NOBODY_LOOKED, name
    assert not any("Second brain" in text or FENCE in text for request in world.requests for text in request.scope.system_additions)


async def test_a_second_brain_tool_follows_the_scope_like_any_tool(build_world, tmp_path):
    denied = "    permissions: {tools: {brain_search: deny}}\n"
    world = await build_world(_settings(tmp_path, denied), second_brain=scripted_second_brain())
    await _share(world, *DOCUMENTS)

    assert world.requests == []
    refused = await _call_in_a_turn(world, "brain_search", query="varroa mites")
    assert json.loads(refused)["error"] == "tool_denied"
    assert world.requests[-1].scope.tools == {"brain_search": "deny"}
    # Control: a tool of the same toolkit the scope does not name still answers.
    assert "5 document(s)" in await _call_in_a_turn(world, "brain_list")


async def test_where_the_second_brain_is_not_enabled_no_tool_of_it_is_served(build_world, tmp_path):
    world = await build_world("second_brain: {enabled: false}\n", second_brain=scripted_second_brain())

    assert not [tool["name"] for tool in world.host.tools.definitions("") if tool["name"].startswith("brain_")]
    await world.send(world.direct("hello"))
    assert world.requests[0].scope.system_additions == ()
