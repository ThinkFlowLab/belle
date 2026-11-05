import pytest

from belle_agent.second_brain import lexical


def test_the_score_is_the_one_the_jiuwenswarm_index_gives():
    memory = pytest.importorskip("jiuwenswarm.extensions.sdk.memory")
    for rank in (-12.5, -1.0, -0.54, -1e-6, 0.0, 3.0):
        assert lexical.rank_to_score(rank) == memory.bm25_rank_to_score(rank)
