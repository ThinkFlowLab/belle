"""The three tools, unwrapped from their cards."""

import pytest

pytest.importorskip("openjiuwen")

from belle_agent.integrations.openjiuwen.second_brain.tools import create_second_brain_tools
from belle_agent.second_brain.store import BrainCorpus, BrainDoc
from belle_agent.second_brain.tools import (
    SecondBrainToolContext,
    strip_frontmatter,
)


@pytest.fixture(autouse=True)
def _delegate_to_the_manager(monkeypatch):
    """See test_rail.py: these tests are about the tools, not the ranking."""
    async def search(manager, query, *, limit, min_score, **_):
        return await manager.search(
            query, opts={"maxResults": limit, "minScore": min_score}
        )

    monkeypatch.setattr("belle_agent.second_brain.tools.search_corpus", search)


@pytest.fixture
def corpus(tmp_path):
    store = BrainCorpus(tmp_path)
    store.ensure_layout()
    extract = store.extract_path_for("dit-9f3a2c1b")
    body = ("PAGE ONE " * 10) + ("PAGE TWO " * 10)
    extract.write_text(body, encoding="utf-8")
    split = len("PAGE ONE " * 10)
    store.summary_path_for("dit-9f3a2c1b").write_text(
        '---\ndoc_id: "dit-9f3a2c1b"\nsource_path: "sources/secret.pdf"\n---\n\n'
        "# DiT\n\nThe summary body.\n",
        encoding="utf-8",
    )
    store.put(
        BrainDoc(
            doc_id="dit-9f3a2c1b",
            sha256="9f" * 32,
            title="DiT",
            extract_path=str(extract),
            outline=[
                {"n": 1, "section": "Method", "location": "p.1", "start": 0, "end": split},
                {"n": 2, "section": "Ablation", "location": "## Ablation", "start": split, "end": len(body)},
            ],
        )
    )
    return tmp_path


def _tools(corpus_dir, manager=None):
    ctx = SecondBrainToolContext(corpus_dir=corpus_dir, manager=manager)
    # Unwrapped via _func, the pattern test_visual_gen_tools.py uses: the tool
    # is a LocalFunction and its callable is not part of the public surface.
    return {t.card.name: t._func for t in create_second_brain_tools(ctx)}


def test_strip_frontmatter_removes_paths_and_permalinks() -> None:
    raw = '---\nsource_path: "sources/x.pdf"\n---\n\nBody.\n'
    assert strip_frontmatter(raw) == "Body."
    assert "source_path" not in strip_frontmatter(raw)


def test_strip_frontmatter_leaves_a_document_without_one() -> None:
    assert strip_frontmatter("# Title\n\nBody.") == "# Title\n\nBody."


@pytest.mark.asyncio
async def test_brain_read_returns_the_summary_without_frontmatter(corpus) -> None:
    out = await _tools(corpus)["brain_read"]("dit-9f3a2c1b")
    assert "The summary body." in out
    assert "sources/secret.pdf" not in out
    assert "<<<BRAIN_DOC" in out and "<<<END_BRAIN_DOC" in out


@pytest.mark.asyncio
async def test_brain_read_slices_by_location(corpus) -> None:
    out = await _tools(corpus)["brain_read"]("dit-9f3a2c1b", location="p.1")
    assert "PAGE ONE" in out
    assert "PAGE TWO" not in out


@pytest.mark.asyncio
async def test_brain_read_matches_a_heading_without_its_markers(corpus) -> None:
    """A model that writes 'Ablation' for '## Ablation' named the right region."""
    out = await _tools(corpus)["brain_read"]("dit-9f3a2c1b", location="Ablation")
    assert "PAGE TWO" in out


@pytest.mark.asyncio
async def test_brain_read_reports_an_unknown_location_with_the_real_ones(corpus) -> None:
    out = await _tools(corpus)["brain_read"]("dit-9f3a2c1b", location="p.99")
    assert "p.1" in out and "## Ablation" in out


@pytest.mark.asyncio
async def test_unknown_doc_id_is_an_error_string_not_an_exception(corpus) -> None:
    out = await _tools(corpus)["brain_read"]("nope")
    assert "nope" in out
    assert isinstance(out, str)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..\\windows", ".hidden", ""])
@pytest.mark.asyncio
async def test_brain_read_refuses_a_path_shaped_doc_id(corpus, bad) -> None:
    out = await _tools(corpus)["brain_read"](bad)
    # The refusal names what it refused, so the id itself is expected in the
    # message; what must never appear is anything read off the filesystem.
    assert "not a valid doc_id" in out or "needs a doc_id" in out
    assert "root:" not in out and "<<<BRAIN_DOC" not in out


@pytest.mark.asyncio
async def test_brain_list_names_what_is_there(corpus) -> None:
    out = await _tools(corpus)["brain_list"]()
    assert "dit-9f3a2c1b" in out and "DiT" in out


@pytest.mark.asyncio
async def test_brain_list_clamps_a_zero_limit(corpus) -> None:
    out = await _tools(corpus)["brain_list"](limit=0)
    assert "dit-9f3a2c1b" in out


@pytest.mark.asyncio
async def test_brain_list_on_an_empty_corpus(tmp_path) -> None:
    BrainCorpus(tmp_path).ensure_layout()
    assert "empty" in await _tools(tmp_path)["brain_list"]()


class _Manager:
    """A manager that records the options it was searched with."""

    def __init__(self, results) -> None:
        self.results, self.opts = results, None

    async def search(self, query, opts=None):
        self.opts = opts
        return self.results


@pytest.mark.asyncio
async def test_brain_search_uses_camelcase_options(corpus) -> None:
    manager = _Manager([{"path": "dit-9f3a2c1b.md", "score": 0.9}])
    await _tools(corpus, manager)["brain_search"]("diffusion")
    assert "maxResults" in manager.opts and "minScore" in manager.opts
    assert "max_results" not in manager.opts


@pytest.mark.parametrize(("top_k", "expected"), [(-1, 3), (100, 60)])
@pytest.mark.asyncio
async def test_brain_search_clamps_the_result_limit(corpus, top_k, expected) -> None:
    manager = _Manager([])
    await _tools(corpus, manager)["brain_search"]("diffusion", top_k=top_k)
    assert manager.opts["maxResults"] == expected


@pytest.mark.asyncio
async def test_brain_search_collapses_chunk_hits_on_one_document(corpus) -> None:
    """search() returns chunks, so one document can fill every slot."""
    manager = _Manager(
        [{"path": "dit-9f3a2c1b.md", "score": 0.9}] * 4
        + [{"path": "dit-9f3a2c1b.md", "score": 0.8}]
    )
    out = await _tools(corpus, manager)["brain_search"]("diffusion")
    assert out.count("<<<BRAIN_DOC") == 1


@pytest.mark.asyncio
async def test_brain_search_with_no_manager_says_so(corpus) -> None:
    out = await _tools(corpus, None)["brain_search"]("x")
    assert "not available" in out


@pytest.mark.asyncio
async def test_brain_search_with_no_hits(corpus) -> None:
    out = await _tools(corpus, _Manager([]))["brain_search"]("x")
    assert "Nothing" in out


_PASSAGE = (
    "Fallback activated on chat-default at 20:12 UTC and the revert landed at"
    " 20:26, fifty-two minutes end to end, with error rate held under 1.0%."
)


@pytest.mark.asyncio
async def test_brain_search_returns_the_passage_that_matched(corpus) -> None:
    """A hit is the text that matched, not the document's whole summary."""
    manager = _Manager(
        [{"path": "dit-9f3a2c1b.md", "score": 0.9, "snippet": _PASSAGE}]
    )
    out = await _tools(corpus, manager)["brain_search"]("fallback")
    assert "20:12 UTC" in out
    assert "The summary body." not in out


@pytest.mark.asyncio
async def test_render_hit_preserves_a_match_after_long_prose(corpus) -> None:
    from belle_agent.second_brain.tools import render_hit

    prefix = "Earlier summary prose. " * 8
    fact = "The rare schooner Kittiwake carries the sealed archive."
    doc = BrainCorpus(corpus).get("dit-9f3a2c1b")
    body = await render_hit(corpus, doc, "dit-9f3a2c1b", {"snippet": prefix + "x " * 400 + fact})

    assert len(prefix) >= 80
    assert fact in body
    assert "The summary body." not in body


@pytest.mark.asyncio
async def test_brain_search_lists_the_regions_to_read_next(corpus) -> None:
    """The point of a hit is the call that reads the document."""
    manager = _Manager(
        [{"path": "dit-9f3a2c1b.md", "score": 0.9, "snippet": _PASSAGE}]
    )
    out = await _tools(corpus, manager)["brain_search"]("fallback")
    assert 'brain_read(doc_id="dit-9f3a2c1b"' in out
    assert '"p.1"' in out and '"## Ablation"' in out
    assert "Method" in out and "Ablation" in out


@pytest.mark.asyncio
async def test_a_frontmatter_chunk_falls_back_to_the_summary(corpus) -> None:
    """Stripped of its paths and its sha, that chunk says nothing at all."""
    manager = _Manager(
        [
            {
                "path": "dit-9f3a2c1b.md",
                "score": 0.9,
                "snippet": '---\ndoc_id: "dit-9f3a2c1b"\nsource_path: "sources/secret.pdf"\n---\n',
            }
        ]
    )
    out = await _tools(corpus, manager)["brain_search"]("dit")
    assert "The summary body." in out
    assert "sources/secret.pdf" not in out


@pytest.mark.asyncio
async def test_brain_search_caps_the_regions_one_hit_lists(tmp_path) -> None:
    """Forty rows on each of ten hits is the volume this replaced."""
    store = BrainCorpus(tmp_path)
    store.ensure_layout()
    store.summary_path_for("long-1").write_text("# Long\n\nBody.\n", encoding="utf-8")
    store.put(
        BrainDoc(
            doc_id="long-1",
            sha256="ab" * 32,
            title="Long",
            outline=[
                {"n": i, "section": f"S{i}", "location": f"p.{i}", "start": 0, "end": 1}
                for i in range(1, 41)
            ],
        )
    )
    manager = _Manager([{"path": "long-1.md", "score": 0.9, "snippet": _PASSAGE}])
    out = await _tools(tmp_path, manager)["brain_search"]("fallback")
    assert '"p.12"' in out and '"p.13"' not in out
    assert "28 more region(s)" in out


@pytest.mark.asyncio
async def test_an_outline_chunk_preserves_its_evidence_without_table_headers(corpus) -> None:
    """Outline data is evidence; column labels and separators are scaffolding."""
    manager = _Manager(
        [
            {
                "path": "dit-9f3a2c1b.md",
                "score": 0.9,
                "snippet": (
                    "\n## Outline\n\n| # | Section | Location | What is there |\n"
                    "|---|---------|----------|---------------|\n"
                    + "".join(
                        f"| {i} | Section {i} | `p.{i}` | a long enough cell that"
                        " the table alone would clear the snippet floor |\n"
                        for i in range(1, 6)
                    )
                ),
            }
        ]
    )
    out = await _tools(corpus, manager)["brain_search"]("dit")
    assert "|---|" not in out
    assert "a long enough cell" in out
    assert "The summary body." not in out


# ---- the whole text ---------------------------------------------------------


@pytest.fixture
def plain_corpus(tmp_path):
    """A document with no outline: a web page, or plain text without headings."""
    store = BrainCorpus(tmp_path)
    store.ensure_layout()
    extract = store.extract_path_for("page-1a2b3c4d")
    extract.write_text("FIRST LINE\n" + ("body text " * 50) + "\nLAST LINE", encoding="utf-8")
    store.summary_path_for("page-1a2b3c4d").write_text(
        '---\ndoc_id: "page-1a2b3c4d"\n---\n\n# A page\n\nThe summary body.\n',
        encoding="utf-8",
    )
    store.put(
        BrainDoc(
            doc_id="page-1a2b3c4d", sha256="1a" * 32, title="A page",
            extract_path=str(extract), outline=[],
        )
    )
    return tmp_path


@pytest.mark.asyncio
async def test_full_reads_the_whole_text_of_an_outlined_document(corpus) -> None:
    out = await _tools(corpus)["brain_read"]("dit-9f3a2c1b", location="full")
    assert "PAGE ONE" in out and "PAGE TWO" in out
    assert "[full]" in out


@pytest.mark.asyncio
async def test_a_document_without_an_outline_can_still_be_read(plain_corpus) -> None:
    read = _tools(plain_corpus)["brain_read"]
    out = await read("page-1a2b3c4d", location="full")
    assert "FIRST LINE" in out and "LAST LINE" in out
    # Any location names the full text when there are no regions to refuse it for.
    out = await read("page-1a2b3c4d", location="p.3")
    assert "FIRST LINE" in out
    assert "no region called" not in out


@pytest.mark.asyncio
async def test_an_unknown_location_offers_full(corpus) -> None:
    out = await _tools(corpus)["brain_read"]("dit-9f3a2c1b", location="p.99")
    assert 'location="full"' in out


@pytest.mark.asyncio
async def test_a_hit_on_a_document_without_an_outline_points_at_full(plain_corpus) -> None:
    from belle_agent.second_brain.tools import render_hit

    doc = BrainCorpus(plain_corpus).get("page-1a2b3c4d")
    body = await render_hit(plain_corpus, doc, "page-1a2b3c4d", {"snippet": ""})
    assert 'brain_read(doc_id="page-1a2b3c4d", location="full")' in body


@pytest.mark.asyncio
async def test_full_respects_max_chars(plain_corpus) -> None:
    read = _tools(plain_corpus)["brain_read"]
    out = await read("page-1a2b3c4d", location="full", max_chars=500)
    assert "exceeding max_chars=500" in out
    assert "<<<BRAIN_DOC" not in out
    assert "LAST LINE" not in out
    retry = await read("page-1a2b3c4d", location="full", max_chars=1000)
    assert "FIRST LINE" in retry and "LAST LINE" in retry


@pytest.mark.parametrize("location", ["summary", "full", "p.1"])
@pytest.mark.asyncio
async def test_brain_read_reports_exact_size_and_accepts_a_larger_budget(corpus, location) -> None:
    store = BrainCorpus(corpus)
    doc = store.get("dit-9f3a2c1b")
    body = "é" * 60_001 + "END"
    store.summary_path_for(doc.doc_id).write_text(body, encoding="utf-8")
    store.extract_path_for(doc.doc_id).write_text(body, encoding="utf-8")
    doc.outline = [{"location": "p.1", "start": 0, "end": len(body.encode("utf-8"))}]
    store.put(doc)
    read = _tools(corpus)["brain_read"]

    refused = await read(doc.doc_id, location=location)
    assert f"{len(body)} characters" in refused
    assert f"max_chars to at least {len(body)}" in refused
    assert "<<<BRAIN_DOC" not in refused

    result = await read(doc.doc_id, location=location, max_chars=len(body))
    assert body in result
    assert "<<<END_BRAIN_DOC" in result


@pytest.mark.parametrize("max_chars", [True, 0, -1, 1.5, "100"])
@pytest.mark.asyncio
async def test_brain_read_refuses_invalid_character_budgets(corpus, max_chars) -> None:
    result = await _tools(corpus)["brain_read"]("dit-9f3a2c1b", max_chars=max_chars)
    assert result == "brain_read max_chars must be a positive integer."


@pytest.mark.parametrize("region_count", [13, 60])
async def test_search_preserves_outline_evidence_beyond_the_region_preview(tmp_path, monkeypatch, region_count):
    from belle_agent.second_brain.corpus_index import CorpusIndex
    from belle_agent.second_brain.summarize import render_summary

    store = BrainCorpus(tmp_path)
    store.ensure_layout()
    fact = "The Kittiwake archive opens at midnight."
    rows = [
        {"n": i, "section": f"Section {i}", "location": f"p.{i}", "note": fact if i == region_count else "Routine notes."}
        for i in range(1, region_count + 1)
    ]
    store.put(BrainDoc(doc_id="outline-1", sha256="ab" * 32, title="Outline", outline=rows))
    store.summary_path_for("outline-1").write_text(
        render_summary(doc_id="outline-1", title="Outline", frontmatter={}, prose="Unrelated prose.", rows=rows),
        encoding="utf-8",
    )
    index = CorpusIndex(tmp_path)
    index.sync()

    async def search(manager, query, *, limit, min_score, **_):
        return await manager.search(query, limit=limit, min_score=min_score, semantic=False)

    monkeypatch.setattr("belle_agent.second_brain.tools.search_corpus", search)
    try:
        out = await _tools(tmp_path, index)["brain_search"]("Kittiwake archive")
        assert fact in out
        assert f"`p.{region_count}`" in out
        assert "|---|" not in out
        assert "Use a location value above" not in out
        assert len(out) < 3000
    finally:
        index.db.close()
