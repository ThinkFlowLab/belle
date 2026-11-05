import pytest
from topic_embedder import TopicEmbedder

from belle_agent.second_brain.index import ENV_USE_EMBEDDINGS


@pytest.fixture(autouse=True)
def _no_embedding_switch(monkeypatch):
    """A test turns embeddings on in its own configuration."""
    monkeypatch.delenv(ENV_USE_EMBEDDINGS, raising=False)


@pytest.fixture
def topics():
    return TopicEmbedder()
