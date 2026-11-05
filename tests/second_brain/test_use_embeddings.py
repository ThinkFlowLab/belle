"""The switch for vectors, and the weights that mix them with the lexical score.

Embedding every document is the expensive half of an index, so turning the
vectors off must stop the embedding as well as the vector ranking.
"""

import pytest

from belle_agent.second_brain import index, retrieval
from belle_agent.second_brain.backend import SecondBrainBackend


def _cfg(value):
    return {"second_brain": {"retrieval": {"use_embeddings": value}}}


# ---- the decision ------------------------------------------------------


def test_off_by_default() -> None:
    assert index.use_embeddings({}) is False
    assert index.use_embeddings(None) is False


def test_config_can_switch_it_on() -> None:
    assert index.use_embeddings(_cfg(True)) is True


@pytest.mark.parametrize(
    "value,expected",
    [(value, False) for value in (None, "", "false", "0", "no", "off", "OFF")]
    + [(value, True) for value in (True, "true", "1", "yes", "on", "ON")],
)
def test_config_spelling_selects_embeddings(value, expected) -> None:
    assert index.use_embeddings(_cfg(value)) is expected


def test_the_environment_overrides_config(monkeypatch) -> None:
    """One process pointed the other way without a second config file."""
    monkeypatch.setenv(index.ENV_USE_EMBEDDINGS, "0")
    assert index.use_embeddings(_cfg(True)) is False
    monkeypatch.setenv(index.ENV_USE_EMBEDDINGS, "1")
    assert index.use_embeddings(_cfg(False)) is True


# ---- what it switches --------------------------------------------------


def _backend(topics, built: list) -> SecondBrainBackend:
    def build_embedder(settings):
        built.append(settings)
        return topics.embedder()

    return SecondBrainBackend(
        build_model=lambda settings: None,
        parse_file=None,
        extract_html=None,
        build_embedder=build_embedder,
    )


@pytest.mark.asyncio
async def test_off_builds_no_embedder(tmp_path, topics) -> None:
    built: list = []
    manager = await index.get_second_brain_manager(_cfg(False), corpus_dir=tmp_path, backend=_backend(topics, built))

    assert manager is not None and manager.embedder is None
    assert built == []


@pytest.mark.asyncio
async def test_on_builds_the_embedder_from_its_settings(tmp_path, topics) -> None:
    built: list = []
    endpoint = {"api_base": "http://embeddings.invalid/v1", "model": "topics-v1"}
    config = {"second_brain": {"retrieval": {"use_embeddings": True, "embeddings": endpoint}}}

    manager = await index.get_second_brain_manager(config, corpus_dir=tmp_path, backend=_backend(topics, built))

    assert manager.embedder is not None and built == [endpoint]


@pytest.mark.asyncio
async def test_the_switch_bites_on_the_query_path(tmp_path, topics, monkeypatch) -> None:
    """Off at query time means no query vector, over an index that has vectors."""
    backend = _backend(topics, [])
    manager = await index.get_second_brain_manager(_cfg(True), corpus_dir=tmp_path, backend=backend)

    await retrieval.search_corpus(manager, "a question", limit=5, min_score=0.35, config=_cfg(False), backend=backend)
    assert topics.queries == []
    await retrieval.search_corpus(manager, "a question", limit=5, min_score=0.35, config=_cfg(True), backend=backend)
    assert topics.queries == ["a question"]


# ---- the weights -------------------------------------------------------


@pytest.mark.parametrize(
    "retrieval_settings,expected",
    [
        ({}, (0.7, 0.3)),
        ({"vector_weight": 0.5, "text_weight": 0.5}, (0.5, 0.5)),
        ({"vector_weight": "0.6", "text_weight": "0.4"}, (0.6, 0.4)),
        *[({"vector_weight": value, "text_weight": value}, (0.7, 0.3)) for value in (None, "", "abc", [])],
    ],
)
@pytest.mark.asyncio
async def test_embedding_weights(tmp_path, topics, retrieval_settings, expected) -> None:
    config = {"second_brain": {"retrieval": retrieval_settings}}
    manager = await index.get_second_brain_manager(config, corpus_dir=tmp_path, backend=_backend(topics, []))

    assert (manager.vector_weight, manager.text_weight) == expected
