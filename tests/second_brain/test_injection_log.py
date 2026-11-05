"""Record recall outcomes and the document text attached to a turn."""

import asyncio
import json

import pytest

from belle_agent.second_brain import injection_log
from belle_agent.second_brain.recall import SecondBrainRecall
from belle_agent.second_brain.store import BrainCorpus, BrainDoc

pytestmark = pytest.mark.usefixtures("_delegate_to_the_manager")


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv(injection_log.ENV_FLAG, "1")


class _Manager:
    def __init__(self, results) -> None:
        self.results = results

    async def search(self, query, opts=None):
        return self.results


def _corpus(tmp_path, *, docs=1, body="The summary body.", without_summary=()):
    """Build a corpus with optional missing summary files."""
    store = BrainCorpus(tmp_path)
    store.ensure_layout()
    for index in range(docs):
        doc_id = f"doc-{index:08x}"
        if index not in without_summary:
            store.summary_path_for(doc_id).write_text(
                f'---\ndoc_id: "{doc_id}"\n---\n\n{body} ({index})\n', encoding="utf-8"
            )
        store.put(BrainDoc(doc_id=doc_id, sha256=f"{index:064x}", title=f"Doc {index}"))
    return tmp_path


def _rail(tmp_path, results, **kw) -> SecondBrainRecall:
    return SecondBrainRecall(corpus_dir=tmp_path, manager=_Manager(results), **kw)


def _hits(*indices, score=0.9):
    return [
        {"path": f"doc-{i:08x}.md", "score": score - (n * 0.01)}
        for n, i in enumerate(indices)
    ]


def _lines(tmp_path):
    path = tmp_path / injection_log.LOG_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---- the flag ----------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("false", False), ("off", False), ("", False)],
)
def test_the_flag_reads_an_explicit_off_as_off(monkeypatch, value, expected) -> None:
    monkeypatch.setenv(injection_log.ENV_FLAG, value)
    assert injection_log.enabled() is expected


def test_the_flag_is_off_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(injection_log.ENV_FLAG, raising=False)
    assert injection_log.enabled() is False


@pytest.mark.asyncio
async def test_nothing_is_written_when_the_flag_is_off(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(injection_log.ENV_FLAG, raising=False)
    rail = _rail(_corpus(tmp_path), _hits(0))

    assert await rail.recall("a question") is not None
    assert not (tmp_path / injection_log.LOG_NAME).exists()
    assert not (tmp_path / injection_log.SNAPSHOT_NAME).exists()


# ---- what gets recorded ------------------------------------------------


@pytest.mark.asyncio
async def test_an_injection_records_its_documents_and_the_text(tmp_path) -> None:
    rail = _rail(_corpus(tmp_path, docs=2), _hits(0, 1))

    injected = await rail.recall("what about retention policy?")

    (record,) = _lines(tmp_path)
    assert record["outcome"] == injection_log.OUTCOME_INJECTED
    assert record["query"] == "what about retention policy?"
    assert record["injected_doc_ids"] == ["doc-00000000", "doc-00000001"]
    assert record["injected_bytes"] > 0

    # The snapshot carries what the model saw; the log deliberately does not.
    snapshot = (tmp_path / injection_log.SNAPSHOT_NAME).read_text()
    assert "<<<BRAIN_DOC" in snapshot
    assert "doc-00000000" in snapshot
    assert "text" not in record
    assert injected is not None and "<<<BRAIN_DOC" in injected


@pytest.mark.asyncio
async def test_a_miss_is_recorded_rather_than_omitted(tmp_path) -> None:
    rail = _rail(_corpus(tmp_path), [])

    assert await rail.recall("nothing like this is in the corpus") is None

    (record,) = _lines(tmp_path)
    assert record["outcome"] == injection_log.OUTCOME_NO_MATCH
    assert record["injected_doc_ids"] == []
    assert record["hits"] == []


@pytest.mark.asyncio
async def test_a_failed_search_is_recorded_with_its_error(tmp_path) -> None:
    class _Broken:
        async def search(self, query, opts=None):
            raise RuntimeError("the index is on fire")

    rail = SecondBrainRecall(corpus_dir=_corpus(tmp_path))
    rail._manager = _Broken()

    assert await rail.recall("a question") is None

    (record,) = _lines(tmp_path)
    assert record["outcome"] == injection_log.OUTCOME_SEARCH_FAILED
    assert "on fire" in record["error"]


@pytest.mark.asyncio
async def test_a_recall_with_no_index_is_still_recorded(tmp_path) -> None:
    rail = SecondBrainRecall(corpus_dir=_corpus(tmp_path))
    rail._manager = None

    assert await rail.recall("a question") is None

    (record,) = _lines(tmp_path)
    assert record["outcome"] == injection_log.OUTCOME_NO_INDEX


# ---- the parts that make document recall measurable --------------------


@pytest.mark.asyncio
async def test_hits_record_the_ranking_before_the_top_k_cut(tmp_path) -> None:
    rail = _rail(_corpus(tmp_path, docs=4), _hits(0, 1, 2, 3), top_k=2)

    await rail.recall("a question")

    (record,) = _lines(tmp_path)
    assert record["injected_doc_ids"] == ["doc-00000000", "doc-00000001"]
    # All four are on the record, ranked, including the two that were cut.
    assert [h["doc_id"] for h in record["hits"]] == [
        "doc-00000000", "doc-00000001", "doc-00000002", "doc-00000003",
    ]
    assert [h["rank"] for h in record["hits"]] == [1, 2, 3, 4]
    assert record["hits"][0]["score"] >= record["hits"][-1]["score"]


@pytest.mark.asyncio
async def test_the_injected_list_is_not_a_prefix_of_the_recalled_one(tmp_path) -> None:
    rail = _rail(
        _corpus(tmp_path, docs=3, without_summary=(1,)), _hits(0, 1, 2), top_k=3
    )

    await rail.recall("a question")

    (record,) = _lines(tmp_path)
    assert record["doc_ids"] == ["doc-00000000", "doc-00000001", "doc-00000002"]
    assert record["injected_doc_ids"] == ["doc-00000000", "doc-00000002"]


@pytest.mark.asyncio
async def test_documents_matched_with_no_summary_on_disk_are_recorded(tmp_path) -> None:
    rail = _rail(_corpus(tmp_path, docs=2, without_summary=(0, 1)), _hits(0, 1))

    assert await rail.recall("a question") is None

    (record,) = _lines(tmp_path)
    assert record["outcome"] == injection_log.OUTCOME_NO_SUMMARY
    assert record["doc_ids"] == ["doc-00000000", "doc-00000001"]
    assert record["injected_doc_ids"] == []


# ---- the log as a run, not a turn --------------------------------------


@pytest.mark.asyncio
async def test_the_log_appends_and_the_snapshot_replaces(tmp_path) -> None:
    rail = _rail(_corpus(tmp_path, docs=2), _hits(0))
    await rail.recall("first question")
    rail._manager = _Manager(_hits(1))
    await rail.recall("second question")

    records = _lines(tmp_path)
    assert [r["query"] for r in records] == ["first question", "second question"]
    assert [r["seq"] for r in records] == sorted(r["seq"] for r in records)

    snapshot = (tmp_path / injection_log.SNAPSHOT_NAME).read_text()
    assert "second question" in snapshot
    assert "first question" not in snapshot


@pytest.mark.asyncio
async def test_a_cancelled_recall_still_leaves_a_line(tmp_path) -> None:
    """``finally``: an attempt that never finished is still an attempt."""

    class _Slow:
        async def search(self, query, opts=None):
            await asyncio.sleep(10)

    rail = SecondBrainRecall(corpus_dir=_corpus(tmp_path))
    rail._manager = _Slow()

    task = asyncio.create_task(rail.recall("a question"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (record,) = _lines(tmp_path)
    assert record["query"] == "a question"


# ---- instrumentation must not break the thing it observes --------------


@pytest.mark.asyncio
async def test_a_write_failure_does_not_fail_the_turn(tmp_path, monkeypatch) -> None:
    def _explode(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(injection_log, "_write_sync", _explode)
    rail = _rail(_corpus(tmp_path), _hits(0))

    injected = await rail.recall("a question")

    assert injected is not None and "<<<BRAIN_DOC" in injected


@pytest.mark.asyncio
async def test_the_record_is_written_even_into_an_empty_corpus_dir(tmp_path) -> None:
    """The rail can recall before anything has created the layout."""
    target = tmp_path / "not-yet"
    rail = SecondBrainRecall(corpus_dir=target)
    rail._manager = _Manager([])

    await rail.recall("a question")

    assert (target / injection_log.LOG_NAME).is_file()
