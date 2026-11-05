"""Ingestion end to end: dedup, extraction, and what actually gets indexed."""

import asyncio
import time
from functools import partial
from pathlib import Path

import pytest
from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND
from belle_agent.second_brain import ingest as ingest_module
from belle_agent.second_brain.index import (
    get_second_brain_manager as _get_second_brain_manager,
)
from belle_agent.second_brain.ingest import (
    STATUS_DUPLICATE,
    STATUS_FILTERED,
    STATUS_INGESTED,
)
from belle_agent.second_brain.ingest import (
    SecondBrainIngestService as _SecondBrainIngestService,
)
from belle_agent.second_brain.store import BrainCorpus

SecondBrainIngestService = partial(_SecondBrainIngestService, backend=SECOND_BRAIN_BACKEND)
get_second_brain_manager = partial(_get_second_brain_manager, backend=SECOND_BRAIN_BACKEND)


@pytest.fixture
def offline(monkeypatch):
    """Disable model calls while testing ingestion."""
    monkeypatch.setattr(SECOND_BRAIN_BACKEND, "build_model", lambda settings: None)


@pytest.fixture
def no_index(monkeypatch):
    async def _get_manager(*args, **kwargs):
        return None

    monkeypatch.setattr(ingest_module, "get_second_brain_manager", _get_manager)


@pytest.fixture
def service(tmp_path, offline, no_index):
    """A service with no model and no index, so tests exercise the pipeline."""
    return SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)


@pytest.mark.asyncio
async def test_a_markdown_file_lands_everywhere_it_should(service, tmp_path) -> None:
    result = await service.ingest_bytes(
        b"# Diffusion Transformers\n\nA paper about patches.\n" + b"x" * 200,
        "DiT.md",
        "text/markdown",
        {"poster": "U1", "slack_channel": "C-BRAIN"},
    )
    assert result["status"] == STATUS_INGESTED
    doc_id = result["doc_id"]

    corpus = BrainCorpus(tmp_path)
    doc = corpus.get(doc_id)
    assert doc is not None
    assert doc.poster == "U1"
    # The summary is the only thing indexed, and it must be under memory/.
    assert service.corpus.summary_path_for(doc_id).is_file()
    assert service.corpus.summary_path_for(doc_id).parent.name == "memory"
    # The raw copy and the flattened text are kept but never indexed.
    assert list((tmp_path / "sources").iterdir())
    assert service.corpus.extract_path_for(doc_id).is_file()


@pytest.mark.asyncio
async def test_the_same_bytes_are_ingested_once(service) -> None:
    """The sha is checked before any model call, so a repost costs a hash."""
    first = await service.ingest_bytes(b"# T\n\n" + b"y" * 300, "a.md", "text/markdown")
    second = await service.ingest_bytes(b"# T\n\n" + b"y" * 300, "different-name.md", "text/markdown")

    assert first["status"] == STATUS_INGESTED
    assert second["status"] == STATUS_DUPLICATE
    assert second["doc_id"] == first["doc_id"]


@pytest.mark.asyncio
async def test_an_oversized_file_is_filtered(tmp_path, offline, no_index) -> None:
    service = SecondBrainIngestService(
        {"second_brain": {"ingest": {"max_file_bytes": 10}}}, corpus_dir=tmp_path
    )
    result = await service.ingest_bytes(b"x" * 100, "big.md", "text/markdown")
    assert result["status"] == STATUS_FILTERED


@pytest.mark.asyncio
async def test_an_empty_file_is_filtered(service) -> None:
    assert (await service.ingest_bytes(b"", "e.md"))["status"] == STATUS_FILTERED


@pytest.mark.asyncio
async def test_a_file_with_no_readable_text_is_filtered(service) -> None:
    """A scan with no text layer is not an error; it is nothing to summarise."""
    result = await service.ingest_bytes(b"\x00\x01\x02\x03" * 50, "scan.bin", "application/octet-stream")
    assert result["status"] == STATUS_FILTERED


@pytest.mark.asyncio
async def test_ingest_path_reads_what_the_gateway_wrote(service, tmp_path) -> None:
    """The ordinary Slack path: the connector downloaded it, this reads it."""
    staged = tmp_path / "staged.md"
    staged.write_text("# Staged\n\n" + "z" * 300, encoding="utf-8")
    result = await service.ingest_path(staged, "text/markdown")
    assert result["status"] == STATUS_INGESTED


@pytest.mark.asyncio
async def test_ingest_path_on_a_missing_file_is_filtered(service, tmp_path) -> None:
    result = await service.ingest_path(tmp_path / "nope.md")
    assert result["status"] == STATUS_FILTERED


@pytest.mark.asyncio
async def test_frontmatter_paths_are_relative_to_the_corpus(service, tmp_path) -> None:
    result = await service.ingest_bytes(b"# T\n\n" + b"q" * 300, "t.md", "text/markdown")
    summary = service.corpus.summary_path_for(result["doc_id"]).read_text()
    assert 'source_path: "sources/' in summary
    assert str(tmp_path) not in summary, "absolute paths leak the deployment layout"


@pytest.mark.asyncio
async def test_the_slack_comment_becomes_the_context_note(service) -> None:
    result = await service.ingest_bytes(
        b"# T\n\n" + b"w" * 300,
        "t.md",
        "text/markdown",
        {"poster": "Ana", "slack_channel_name": "#papers", "comment": "backbone for Sora"},
    )
    summary = service.corpus.summary_path_for(result["doc_id"]).read_text()
    assert "Posted by Ana in #papers" in summary
    assert "backbone for Sora" in summary


@pytest.mark.asyncio
async def test_the_corpus_actually_indexes_and_searches(
    tmp_path, monkeypatch, offline
) -> None:
    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)
    await service.ingest_bytes(
        b"# Diffusion Transformers\n\n" + b"patch size ablation study. " * 40,
        "DiT.md",
        "text/markdown",
    )
    await service.ingest_bytes(
        b"# Kubernetes Operators\n\n" + b"reconcile loop and CRDs. " * 40,
        "k8s.md",
        "text/markdown",
    )

    manager = await get_second_brain_manager(corpus_dir=tmp_path)
    assert manager is not None
    results = await manager.search("diffusion transformers", limit=5, min_score=0.0, semantic=True)
    assert results, "the corpus indexed nothing"
    assert any("dit" in str(r.get("path", "")).lower() for r in results)


@pytest.mark.asyncio
async def test_an_unusable_file_is_remembered_and_not_re_read(
    tmp_path, monkeypatch, offline, no_index
) -> None:

    parses = {"n": 0}
    real = ingest_module.extract_text

    async def _counting(path, mime="", **kwargs):
        parses["n"] += 1
        return await real(path, mime, **kwargs)

    monkeypatch.setattr(ingest_module, "extract_text", _counting)

    unreadable = b"%PDF-1.4\n" + b"\x00\x01\x02" * 200
    for _ in range(3):
        result = await SecondBrainIngestService(
            {"second_brain": {}}, corpus_dir=tmp_path
        ).ingest_bytes(unreadable, "scan.pdf", "application/pdf")
        assert result["status"] == STATUS_FILTERED

    assert parses["n"] == 1, "the document was parsed again after being rejected"


@pytest.mark.asyncio
async def test_a_pdf_without_text_is_ignored_not_parsed(
    tmp_path, monkeypatch, offline, no_index
) -> None:
    """The parser fallback would write cropped page images into the cwd."""
    parsed: list[Path] = []

    async def _parse(path):
        parsed.append(path)
        return "text the parser should never have been asked for"

    monkeypatch.setattr(SECOND_BRAIN_BACKEND, "parse_file", _parse)

    result = await SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path).ingest_bytes(
        b"%PDF-1.4\n" + b"\x00\x01\x02" * 200, "scan.pdf", "application/pdf"
    )
    assert result["status"] == STATUS_FILTERED
    assert "no readable text" in result["reason"]
    assert parsed == []


@pytest.mark.asyncio
async def test_a_remembered_rejection_repeats_its_own_reason(
    tmp_path, offline, no_index
) -> None:
    unreadable = b"%PDF-1.4\n" + b"\x00\x01\x02" * 200

    first = await SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path).ingest_bytes(
        unreadable, "scan.pdf", "application/pdf"
    )
    second = await SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path).ingest_bytes(
        unreadable, "scan.pdf", "application/pdf"
    )
    assert second["reason"] == first["reason"]
    assert second["status"] == STATUS_FILTERED, "never reported as a duplicate"


@pytest.mark.asyncio
async def test_an_oversized_file_is_remembered_too(tmp_path, offline, no_index) -> None:
    config = {"second_brain": {"ingest": {"max_file_bytes": 10}}}

    for _ in range(2):
        result = await SecondBrainIngestService(config, corpus_dir=tmp_path).ingest_bytes(
            b"x" * 100, "big.md", "text/markdown"
        )
        assert result["status"] == STATUS_FILTERED

    corpus = BrainCorpus(tmp_path)
    assert len(corpus.list(include_filtered=True)) == 1


@pytest.mark.asyncio
async def test_rejected_entries_are_invisible_to_the_agent(
    tmp_path, offline, no_index
) -> None:
    """They are bookkeeping. A doc_id with no summary is a tool error waiting."""
    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)

    await service.ingest_bytes(b"%PDF-1.4\n" + b"\x00" * 400, "scan.pdf", "application/pdf")
    good = await service.ingest_bytes(b"# T\n\n" + b"z" * 300, "good.md", "text/markdown")

    corpus = BrainCorpus(tmp_path)
    assert len(corpus) == 1
    assert [d.doc_id for d in corpus.list()] == [good["doc_id"]]
    assert len(corpus.list(include_filtered=True)) == 2


@pytest.mark.asyncio
async def test_the_same_file_arriving_twice_at_once_is_ingested_once(
    tmp_path, offline, no_index
) -> None:
    data = b"# Paper\n\n" + b"real content. " * 60

    # Same bytes, the names Slack's uniquifier would produce.
    results = await asyncio.gather(
        SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path).ingest_bytes(
            data, "DiT.md", "text/markdown"
        ),
        SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path).ingest_bytes(
            data, "DiT-1.md", "text/markdown"
        ),
    )

    statuses = sorted(r["status"] for r in results)
    assert statuses == sorted([STATUS_DUPLICATE, STATUS_INGESTED]), statuses

    summaries = sorted(p.name for p in (tmp_path / "memory").glob("*.md"))
    assert len(summaries) == 1, f"two summaries on disk: {summaries}"

    corpus = BrainCorpus(tmp_path)
    assert len(corpus) == 1


def _slow_summarizer(order: list[str], gate: asyncio.Event | None = None):
    """Record summary start order and optionally block the first document."""
    seen_first = {"done": False}

    async def _summarize(*, title: str, **kwargs):
        order.append(title)
        if gate is not None and not seen_first["done"]:
            seen_first["done"] = True
            await gate.wait()
        return f"# {title}\n\nprose.\n", []

    return _summarize


@pytest.mark.asyncio
async def test_documents_are_ingested_in_the_order_they_were_submitted(
    tmp_path, monkeypatch, offline, no_index
) -> None:

    order: list[str] = []
    delays = {"first": 0.05, "second": 0.0, "third": 0.0}

    async def _summarize(*, title: str, **kwargs):
        await asyncio.sleep(delays.get(title, 0.0))
        order.append(title)
        return f"# {title}\n\nprose.\n", []

    monkeypatch.setattr(ingest_module, "summarize_document", _summarize)

    async def _ingest(name: str) -> dict:
        service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)
        return await service.ingest_bytes(
            f"# {name}\n\n".encode() + f"{name} body. ".encode() * 60,
            f"{name}.md",
            "text/markdown",
        )

    results = await asyncio.gather(
        _ingest("first"), _ingest("second"), _ingest("third")
    )

    assert all(r["status"] == STATUS_INGESTED for r in results), results

    # The first submitted document is slowest; other completion order can vary.
    assert sorted(order) == ["first", "second", "third"], order
    assert order[-1] == "first", order

    # The manifest orders documents by submission timestamp.
    listed = BrainCorpus(tmp_path).list()
    assert [doc.title for doc in listed] == ["third", "second", "first"], listed


@pytest.mark.asyncio
async def test_a_repost_is_answered_without_waiting_for_the_queue(
    tmp_path, monkeypatch, offline, no_index
) -> None:

    data = b"# Known\n\n" + b"already in the corpus. " * 40
    first = await SecondBrainIngestService(
        {"second_brain": {}}, corpus_dir=tmp_path
    ).ingest_bytes(data, "known.md", "text/markdown")
    assert first["status"] == STATUS_INGESTED

    gate = asyncio.Event()
    order: list[str] = []
    monkeypatch.setattr(
        ingest_module, "summarize_document", _slow_summarizer(order, gate)
    )

    finished: list[str] = []

    async def _slow_new_document() -> None:
        result = await SecondBrainIngestService(
            {"second_brain": {}}, corpus_dir=tmp_path
        ).ingest_bytes(
            b"# Blocking\n\n" + b"a long new paper. " * 60,
            "blocking.md",
            "text/markdown",
        )
        finished.append(f"blocking:{result['status']}")

    blocking = asyncio.create_task(_slow_new_document())
    # Thread hops need wall-clock time to reach the summarizer.
    for _ in range(200):
        if order:
            break
        await asyncio.sleep(0.01)
    assert order == ["blocking"], "the blocking ingest never reached the summariser"

    # Without the pre-queue duplicate check, the repost waits on the held document.
    try:
        repost = await asyncio.wait_for(
            SecondBrainIngestService(
                {"second_brain": {}}, corpus_dir=tmp_path
            ).ingest_bytes(data, "known.md", "text/markdown"),
            timeout=2.0,
        )
    except TimeoutError:  # pragma: no cover - the failure being guarded
        gate.set()
        await blocking
        pytest.fail("the repost queued behind the running ingest instead of "
                    "being answered from the manifest")
    finished.append(f"repost:{repost['status']}")

    gate.set()
    await blocking

    assert repost["status"] == STATUS_DUPLICATE
    assert repost["doc_id"] == first["doc_id"]
    assert finished == [
        f"repost:{STATUS_DUPLICATE}",
        f"blocking:{STATUS_INGESTED}",
    ], "the repost waited for the document ahead of it in the queue"


def _counting_fetch(active: dict, gate: asyncio.Event | None = None):
    """Record concurrent fetches, optionally holding them at a gate."""
    from belle_agent.second_brain.links import LinkFetch

    async def _fetch(url, *, cfg=None, **_):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        try:
            if gate is not None:
                await gate.wait()
            else:
                await asyncio.sleep(0.02)
        finally:
            active["now"] -= 1
        name = url.rsplit("/", 1)[-1]
        return LinkFetch(
            url=url,
            kind="html",
            title=name,
            text=f"{name} body. " * 60,
            filename=f"{name}.txt",
            mime_type="text/html",
        )

    return _fetch


@pytest.mark.asyncio
async def test_concurrent_fetches_are_bounded_across_requests(
    tmp_path, monkeypatch, offline, no_index
) -> None:

    active = {"now": 0, "peak": 0}
    monkeypatch.setattr(ingest_module, "fetch_link", _counting_fetch(active))

    async def _ingest(n: int) -> dict:
        service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)
        return await service.ingest_link(f"https://example.test/doc{n}")

    results = await asyncio.gather(*(_ingest(n) for n in range(6)))

    assert all(r["status"] == STATUS_INGESTED for r in results), results
    assert active["peak"] <= ingest_module._FETCH_CONCURRENCY, active


@pytest.mark.asyncio
async def test_a_fetch_is_not_gated_on_another_document_being_summarised(
    tmp_path, monkeypatch, offline, no_index
) -> None:

    active = {"now": 0, "peak": 0}
    fetched: list[str] = []
    inner = _counting_fetch(active)

    async def _fetch(url, *, cfg=None, **kwargs):
        result = await inner(url, cfg=cfg, **kwargs)
        fetched.append(url)
        return result

    monkeypatch.setattr(ingest_module, "fetch_link", _fetch)

    order: list[str] = []
    gate = asyncio.Event()
    monkeypatch.setattr(
        ingest_module, "summarize_document", _slow_summarizer(order, gate)
    )

    def _ingest(n: int):
        async def _run():
            service = SecondBrainIngestService(
                {"second_brain": {}}, corpus_dir=tmp_path
            )
            return await service.ingest_link(f"https://example.test/doc{n}")

        return asyncio.create_task(_run())

    tasks = [_ingest(0)]
    # Thread hops need wall-clock time to reach the summarizer.
    for _ in range(300):
        if order:
            break
        await asyncio.sleep(0.01)
    assert order, "the first document never reached the summariser"
    assert fetched == ["https://example.test/doc0"]

    # A blocked summary must not hold a fetch slot.
    tasks += [_ingest(n) for n in (1, 2, 3)]
    for _ in range(300):
        if len(fetched) == 4:
            break
        await asyncio.sleep(0.01)

    assert len(fetched) == 4, (
        "a link is still waiting on a fetch slot held by a document that is not"
        f" fetching, but being summarised: {fetched}"
    )
    assert active["peak"] <= ingest_module._FETCH_CONCURRENCY, active

    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10.0)
    assert all(r["status"] == STATUS_INGESTED for r in results), results


@pytest.mark.asyncio
async def test_an_oversized_file_is_refused_without_being_read(
    tmp_path, monkeypatch, offline, no_index
) -> None:

    oversized = tmp_path / "big.md"
    oversized.write_bytes(b"x" * 100)

    def _explode(self, *args, **kwargs):
        raise AssertionError(f"{self} was read despite being over the limit")

    monkeypatch.setattr(Path, "read_bytes", _explode)

    service = SecondBrainIngestService(
        {"second_brain": {"ingest": {"max_file_bytes": 10}}}, corpus_dir=tmp_path
    )
    result = await service.ingest_path(oversized)

    assert result["status"] == STATUS_FILTERED
    assert "over the" in result["reason"], result


@pytest.mark.asyncio
async def test_an_ingest_syncs_the_index_through_the_guard(
    tmp_path, monkeypatch, offline
) -> None:
    manager = object()

    async def _get_manager(*a, **kw):
        return manager

    monkeypatch.setattr(ingest_module, "get_second_brain_manager", _get_manager)

    guarded: list[bool] = []

    async def _refresh(target):
        guarded.append(target is manager)
        return True

    monkeypatch.setattr(ingest_module, "refresh_index", _refresh)

    service = SecondBrainIngestService({"second_brain": {}}, corpus_dir=tmp_path)
    result = await service.ingest_bytes(
        b"# Guarded\n\n" + b"body. " * 60, "guarded.md", "text/markdown"
    )

    assert result["status"] == STATUS_INGESTED
    assert guarded == [True], guarded


@pytest.mark.asyncio
async def test_recording_a_rejection_does_not_block_the_loop(
    tmp_path, monkeypatch, offline, no_index
) -> None:

    real_save = BrainCorpus._save_locked  # noqa: SLF001

    def _slow_save(self) -> None:
        time.sleep(0.2)
        real_save(self)

    monkeypatch.setattr(BrainCorpus, "_save_locked", _slow_save)

    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(_heartbeat())
    try:
        service = SecondBrainIngestService(
            {"second_brain": {"ingest": {"max_file_bytes": 10}}}, corpus_dir=tmp_path
        )
        result = await service.ingest_bytes(b"x" * 100, "big.md", "text/markdown")
    finally:
        beat.cancel()

    assert result["status"] == STATUS_FILTERED
    # And it really was recorded -- off the loop, not skipped.
    assert len(BrainCorpus(tmp_path).list(include_filtered=True)) == 1
    assert ticks >= 10, (
        f"the loop stalled through the manifest write: {ticks} ticks in 0.2s"
    )
