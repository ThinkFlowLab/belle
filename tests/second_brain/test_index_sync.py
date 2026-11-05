"""One index sync of a corpus at a time, and what happens when one of them fails."""

import asyncio
import sqlite3

import pytest

from belle_agent.second_brain.corpus_index import CorpusIndex, refresh_index


class _Recorder:
    """Stands in for a sync that awaits the embedder, and records how many were inside at once."""

    def __init__(self, delay: float = 0.01) -> None:
        self._delay = delay
        self.inside = 0
        self.peak = 0
        self.calls = 0

    async def __call__(self) -> None:
        self.inside += 1
        self.calls += 1
        self.peak = max(self.peak, self.inside)
        await asyncio.sleep(self._delay)
        self.inside -= 1


def _index(root, recorder) -> CorpusIndex:
    index = CorpusIndex(root)
    index._refresh_locked = recorder
    return index


@pytest.mark.asyncio
async def test_syncs_of_one_corpus_do_not_overlap(tmp_path) -> None:
    recorder = _Recorder()
    index = _index(tmp_path, recorder)

    assert all(await asyncio.gather(*(refresh_index(index) for _ in range(5))))

    assert recorder.peak == 1, "two syncs were inside the index at once"
    assert recorder.calls == 5, "every caller still gets its sync"


@pytest.mark.asyncio
async def test_two_corpora_do_not_wait_on_each_other(tmp_path) -> None:
    """The guard is per corpus: two corpora share no connection and no lock."""
    first, second = _Recorder(delay=0.05), _Recorder(delay=0.05)
    both = {"peak": 0, "now": 0}

    async def _run(index):
        both["now"] += 1
        both["peak"] = max(both["peak"], both["now"])
        try:
            await refresh_index(index)
        finally:
            both["now"] -= 1

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    await asyncio.gather(_run(_index(tmp_path / "a", first)), _run(_index(tmp_path / "b", second)))
    assert both["peak"] == 2


@pytest.mark.asyncio
async def test_a_failing_sync_is_reported_and_lets_the_next_one_through(tmp_path, monkeypatch) -> None:
    index = CorpusIndex(tmp_path)
    calls = []

    def _sync():
        calls.append(1)
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(index, "sync", _sync)

    assert await asyncio.wait_for(refresh_index(index), timeout=5.0) is False
    assert await asyncio.wait_for(refresh_index(index), timeout=5.0) is False
    assert len(calls) == 2
