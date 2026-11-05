"""JiuwenSwarm uses Belle's summarizer endpoint when one is selected."""

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("jiuwenswarm")

from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND, _build_model
from belle_agent.second_brain.default_backend import SECOND_BRAIN_BACKEND as SHARED_BACKEND


def test_host_wrapper_uses_shared_reader_and_embedder() -> None:
    assert SECOND_BRAIN_BACKEND.parse_file is SHARED_BACKEND.parse_file
    assert SECOND_BRAIN_BACKEND.extract_html is SHARED_BACKEND.extract_html


@pytest.fixture
def no_registry(monkeypatch) -> None:
    from jiuwenswarm.extensions.sdk import host_config

    def unexpected_lookup():
        raise AssertionError("host model registry was read")

    monkeypatch.setattr(host_config, "get_default_models", unexpected_lookup)


@pytest.mark.parametrize("api", ["chat_completions", "openrouter", "scaledown"])
def test_belle_model_skips_host_registry(no_registry, api) -> None:
    model = _build_model(
        {
            "api": api,
            "api_base": "https://example.invalid/v1",
            "api_key": "test",
            "model": "example/model",
        }
    )
    assert model is not None and model.model == "example/model"


@pytest.mark.parametrize("api", ["openrouter", "scaledown"])
def test_a_provider_entry_without_api_base_uses_the_host_registry(monkeypatch, api) -> None:
    from jiuwenswarm.extensions.sdk import host_config

    looked_up = []
    monkeypatch.setattr(host_config, "get_default_models", lambda: looked_up.append(True) or [])

    assert _build_model({"api": api, "api_key": "test"}) is None
    assert looked_up == [True]


async def test_a_registry_model_call_drops_the_library_records(monkeypatch) -> None:
    from jiuwenswarm.extensions.sdk import host_config
    from openjiuwen.core.foundation.llm import Model

    entry = {"model_client_config": {"model_name": "registry/model", "api_base": "https://example.invalid/v1", "api_key": "test"}}
    monkeypatch.setattr(host_config, "get_default_models", lambda: [entry])
    seen: list[logging.LogRecord] = []
    grab = logging.Handler(logging.DEBUG)
    grab.emit = seen.append
    library = logging.getLogger("llm")
    library.addHandler(grab)
    monkeypatch.setattr(library, "level", logging.DEBUG)

    async def failing_invoke(self, messages):
        library.error("request failed: %s", messages[-1].content)
        return SimpleNamespace(content="reply")

    monkeypatch.setattr(Model, "invoke", failing_invoke)
    try:
        model = _build_model({"model": "registry/model"})
        assert model is not None and model.model == "registry/model"
        assert await model.invoke("system", "document text", 5.0) == "reply"
    finally:
        library.removeHandler(grab)
    assert seen == []
