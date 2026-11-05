"""Corpus paths, slugs, and manifest."""

import json
import os
import re
import subprocess
import sys

import pytest

from belle_agent.second_brain.paths import (
    doc_slug,
    ensure_layout,
    extracted_dir,
    manifest_path,
    resolve_second_brain_dir,
    sources_dir,
    summaries_dir,
)
from belle_agent.second_brain.store import BrainCorpus, BrainDoc

_BARE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_default_corpus_uses_belle_data_dir(tmp_path, monkeypatch) -> None:
    data = tmp_path / "belle-data"
    monkeypatch.setenv("BELLE_AGENT_DATA_DIR", str(data))
    assert resolve_second_brain_dir(workspace_dir=tmp_path / "host") == data / "second_brain"


def test_configured_corpus_keeps_workspace_relative_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BELLE_AGENT_DATA_DIR", str(tmp_path / "belle-data"))
    config = {"second_brain": {"corpus_dir": "custom"}}
    assert resolve_second_brain_dir(config, workspace_dir=tmp_path / "host") == tmp_path / "host/custom"


def test_summaries_live_in_a_directory_called_memory(tmp_path) -> None:
    """Retrieval finds files by this convention; a rename breaks indexing."""
    assert summaries_dir(tmp_path).name == "memory"
    assert summaries_dir(tmp_path).parent == tmp_path


def test_ensure_layout_is_idempotent(tmp_path) -> None:
    ensure_layout(tmp_path)
    ensure_layout(tmp_path)
    for directory in (summaries_dir, sources_dir, extracted_dir):
        assert directory(tmp_path).is_dir()


@pytest.mark.parametrize(
    "title",
    [
        "2026-01-01",
        "2026-01-01.md",
        "   2026-12-31   ",
        "",
        "...",
        "上海会议纪要",
    ],
)
def test_doc_slug_never_produces_a_bare_date(title) -> None:
    slug = doc_slug(title, "9f3a2c1b" + "0" * 56)
    assert not _BARE_DATE.match(slug)
    assert slug.endswith("-9f3a2c1b")


def test_doc_slug_is_stable_and_collision_resistant() -> None:
    a = doc_slug("Scalable Diffusion Models", "a" * 64)
    b = doc_slug("Scalable Diffusion Models", "b" * 64)
    assert a == doc_slug("Scalable Diffusion Models", "a" * 64)
    assert a != b, "same title, different bytes must not collide"


def test_doc_slug_requires_a_digest() -> None:
    with pytest.raises(ValueError):
        doc_slug("anything", "")


def test_manifest_round_trips(tmp_path) -> None:
    corpus = BrainCorpus(tmp_path)
    corpus.ensure_layout()
    doc = BrainDoc(
        doc_id="dit-9f3a2c1b",
        sha256="9f3a2c1b" + "0" * 56,
        title="DiT",
        pages=24,
        outline=[{"n": 1, "section": "p.1", "location": "p.1-6", "start": 0, "end": 600}],
    )
    corpus.put(doc)

    reopened = BrainCorpus(tmp_path)
    found = reopened.find_by_sha256(doc.sha256)
    assert found is not None
    assert found.doc_id == "dit-9f3a2c1b"
    assert found.outline[0]["location"] == "p.1-6"
    assert reopened.get("dit-9f3a2c1b") is not None
    assert reopened.get("nope") is None


def test_sha256_dedup_matches_bytes_not_names(tmp_path) -> None:
    corpus = BrainCorpus(tmp_path)
    corpus.ensure_layout()
    digest = BrainCorpus.sha256_of_bytes(b"hello")
    corpus.put(BrainDoc(doc_id="a-1", sha256=digest, title="a"))
    assert corpus.find_by_sha256(digest) is not None
    assert corpus.find_by_sha256(BrainCorpus.sha256_of_bytes(b"other")) is None


def test_a_corrupt_manifest_degrades_to_empty(tmp_path) -> None:
    """Retrieval reads the memory/ files, not this, so a raise would cost more."""
    ensure_layout(tmp_path)
    manifest_path(tmp_path).write_text("{not json", encoding="utf-8")
    corpus = BrainCorpus(tmp_path)
    assert len(corpus) == 0
    corpus.put(BrainDoc(doc_id="a-1", sha256="a" * 64, title="a"))
    assert json.loads(manifest_path(tmp_path).read_text())["documents"][0]["doc_id"] == "a-1"


def test_list_is_newest_first(tmp_path) -> None:
    corpus = BrainCorpus(tmp_path)
    corpus.ensure_layout()
    corpus.put(BrainDoc(doc_id="old", sha256="a" * 64, title="o", ingested_at="2026-01-01T00:00:00Z"))
    corpus.put(BrainDoc(doc_id="new", sha256="b" * 64, title="n", ingested_at="2026-06-01T00:00:00Z"))
    assert [d.doc_id for d in corpus.list()] == ["new", "old"]
    assert [d.doc_id for d in corpus.list(1)] == ["new"]


_CONCURRENT_WRITER = """
import sys, time
from pathlib import Path
from belle_agent.second_brain.store import BrainCorpus, BrainDoc

root, tag, count, go = sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
while not go.exists():
    time.sleep(0.001)
for i in range(count):
    BrainCorpus(root).put(BrainDoc(doc_id=f"{tag}-{i}", sha256=f"{tag}{i:063d}", title=tag))
"""


def test_two_processes_writing_one_corpus_keep_every_document(tmp_path) -> None:
    """Without the manifest lock, each writer overwrites the other's additions."""
    corpus_dir = tmp_path / "corpus"
    BrainCorpus(corpus_dir).ensure_layout()
    go = tmp_path / "go"
    count = 100
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_WRITER, str(corpus_dir), tag, str(count), str(go)],
            env=env,
        )
        for tag in ("a", "b")
    ]
    go.touch()
    assert [writer.wait(timeout=120) for writer in writers] == [0, 0]

    docs = BrainCorpus(corpus_dir).list()
    assert len(docs) == 2 * count
