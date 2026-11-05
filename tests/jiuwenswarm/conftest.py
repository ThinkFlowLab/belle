import pytest

from belle_agent.second_brain.index import ENV_USE_EMBEDDINGS

_HOST_EMBEDDING_VARIABLES = ("EMBED_KEY", "EMBED_API_KEY", "EMBED_BASE", "EMBED_BASE_URL", "EMBED_MODEL", "EMBEDDING_MODEL")


@pytest.fixture(autouse=True)
def _no_host_embedding_endpoint(monkeypatch):
    """The JiuwenSwarm backend falls back to the machine's own embedding settings. A test names its endpoint itself."""
    for name in (*_HOST_EMBEDDING_VARIABLES, ENV_USE_EMBEDDINGS):
        monkeypatch.delenv(name, raising=False)
    try:
        import jiuwenswarm.extensions.sdk.memory  # noqa: F401
    except ImportError:
        return
    monkeypatch.setattr("jiuwenswarm.extensions.sdk.memory.get_embed_config", dict)
