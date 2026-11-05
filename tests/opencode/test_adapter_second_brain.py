"""The shared Second Brain backend on OpenCode, in both adapter modes.

What the model was sent is read from the stand-in's captured requests. The
second brain's own model calls go to another stand-in, with a log of its own.
"""

import html

import pytest
from conformance_kit import DOCUMENTS, as_file
from standin import call, said, tools
from summarizer_standin import MARK, SummarizerStandIn

from belle_agent.second_brain.default_backend import SECOND_BRAIN_BACKEND

pytestmark = pytest.mark.adapter

FENCE = "<<<BRAIN_DOC doc_id="


@pytest.fixture
def summarizer():
    standin = SummarizerStandIn()
    yield standin
    standin.close()


@pytest.fixture
async def world(build_world, tmp_path, summarizer, monkeypatch):
    """A host whose second brain ingests from C-DOCS and whose conversation C1 is one session."""
    monkeypatch.setenv("SUMMARIZER_API_KEY", "standin")
    return await build_world(
        f"""
models:
  summarizer:
    entries:
      - {{id: documents, api: chat_completions, api_base: "{summarizer.url}", api_key: "${{SUMMARIZER_API_KEY}}", model_name: standin/summarizer}}
second_brain:
  enabled: true
  corpus_dir: {tmp_path / "corpus"}
  ingest:
    channels: [C-DOCS]
    summarizer: {{model: documents}}
scopes:
  - match: {{channel: loopback, chat: C1}}
    delivery: {{mode: [all], session: channel}}
""",
        second_brain=SECOND_BRAIN_BACKEND,
    )


def _session(model, text: str) -> str:
    """The OpenCode session of the latest turn whose message was ``text``."""
    return next(entry["session"] for entry in reversed(model.log) if said(entry["body"], "user")[-1:] == [text])


def _request(model, text: str) -> dict:
    """The first provider request of the turn whose message was ``text``."""
    return next(body for body in model.requests(_session(model, text)) if said(body, "user")[-1:] == [text])


def _everything(body: dict) -> str:
    """What one request tells the model outside its own turns, as the model can read it.

    An instruction entry that arrives after a session's first turn is sent
    escaped, inside a ``user`` role update. See the last check of this file.
    """
    return html.unescape("\n".join(said(body, "system") + said(body, "user")))


async def _share(world, *names: str) -> None:
    for name in names:
        outcome = await world.send(world.message(f"see {name}", conversation="C-DOCS", files=(as_file(name),)))
        assert outcome == "ingested:second-brain"


async def test_a_document_ingested_through_the_platform_is_listed_to_the_model(world, model, summarizer):
    turns_before = len(model.log)
    await _share(world, "kite-bridle.md")

    # Ingest asked the summarizer's stand-in and started no turn: the model of the conversations saw nothing.
    assert len(model.log) == turns_before
    assert len(summarizer.log) == 2 and "A delta kite stalls" in summarizer.prompts()[0]
    assert not any("x-opencode-session-id" in entry["headers"] for entry in summarizer.log)

    text = "which documents are there? " + call("belle_brain_list")
    await world.send(world.message(text, conversation="C1"))

    session = _session(model, text)
    assert {"belle_brain_search", "belle_brain_read", "belle_brain_list"} <= set(tools(model.requests(session)[0]))
    (listing,) = said(model.requests(session)[-1], "tool")
    assert "1 document(s) in the second brain" in listing and "kite-bridle-" in listing and "from C-DOCS" in listing
    # A turn asked the conversation's model and nothing of the summarizer.
    assert len(summarizer.log) == 2
    assert not any("DOCUMENT TEXT BEGINS" in _everything(entry["body"]) for entry in model.log[turns_before:])


async def test_a_matching_turn_carries_the_recall_in_its_provider_request(world, model):
    await _share(world, *DOCUMENTS)
    unrelated, matching = "thanks, see you tomorrow SAY=bye", "why does a delta kite stall? SAY=aft"

    await world.send(world.message(unrelated, conversation="C1"))
    await world.send(world.message(matching, conversation="C1"))
    await world.send(world.message(unrelated, conversation="C2", trigger="mention"))

    # A turn whose message matches no document carries none, and is still told how the corpus is used.
    first = _everything(_request(model, unrelated))
    assert FENCE not in first and "## Second brain" in first
    # The turn whose message matches carries the summary of that document and of no other.
    recalled = _everything(_request(model, matching))
    assert FENCE + "kite-bridle-" in recalled and f"{MARK} # Kite Bridle Geometry A delta kite stalls" in recalled
    assert FENCE + "hive-inspection-" not in recalled
    # Control: another conversation, asked the unrelated thing after the match, carries none.
    assert FENCE not in _everything(model.requests(_session(model, unrelated))[-1])


@pytest.mark.plugin
async def test_behind_the_plugin_a_recall_is_in_its_own_turns_request_and_in_no_later_one(world, model):
    await _share(world, *DOCUMENTS)
    matching, later = "why does a delta kite stall? SAY=aft", "thanks, see you tomorrow SAY=bye"

    await world.send(world.message(later, conversation="C1"))
    await world.send(world.message(matching, conversation="C1"))
    await world.send(world.message(later + " again", conversation="C1"))

    before, recalled, after = model.requests(_session(model, matching))
    # The recall arrives after the session's first turn, in the system message, with its fence as written.
    assert FENCE + "kite-bridle-" in "\n".join(said(recalled, "system"))
    # It is stored nowhere: the turns around it carry none, and no message of the history holds it.
    for request in (before, after):
        assert FENCE not in "\n".join(said(request, "system"))
    assert said(after, "user") == [later, matching, later + " again"]


@pytest.mark.no_plugin
async def test_a_recall_stays_in_the_history_of_its_session_on_this_route(world, model):
    """A record of an effect of the instruction entry, with no plugin loaded. It is not a requirement."""
    await _share(world, *DOCUMENTS)

    await world.send(world.message("why does a delta kite stall? SAY=aft", conversation="C1"))
    later = "thanks, see you tomorrow SAY=bye"
    await world.send(world.message(later, conversation="C1"))

    # The later turn recalled nothing. OpenCode still sends the earlier turn's recall, which was this
    # session's first entry and so sits in its system message, with a notice that the instructions changed.
    request = _request(model, later)
    assert FENCE + "kite-bridle-" in "\n".join(said(request, "system"))
    assert any("supersedes" in text for text in said(request, "user"))

    # A recall that arrives after the session's first turn is a stored `user` role update, and OpenCode
    # escapes it: the model reads the fence as `&lt;&lt;&lt;BRAIN_DOC`, not as the instructions spell it.
    await world.send(world.message("how do I count varroa mites? SAY=board", conversation="C1"))
    update = next(text for text in said(_request(model, "how do I count varroa mites? SAY=board"), "user") if "hive-inspection-" in text)
    assert update.startswith("<system-update>") and "&lt;&lt;&lt;BRAIN_DOC doc_id=hive-inspection-" in update
    assert FENCE + "hive-inspection-" not in update
