"""The E2A landing, the index settings, and where the rail gets mounted."""

import asyncio
import json
from importlib.resources import files
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ContributedReqMethod, parse_req_method
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.gateway_adapter import AdapterRegistry
from jiuwenswarm.server.runtime.gateway_adapter.contributed import (
    contributed_adapters,
    mount_contributed_adapters,
    unregister_gateway_adapter,
)
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from belle_agent.integrations.jiuwenswarm.extensions.second_brain import extension
from belle_agent.integrations.jiuwenswarm.second_brain_adapter import SecondBrainAdapter


def _request(params) -> AgentRequest:
    return AgentRequest(
        request_id="r1",
        channel_id="slack",
        req_method=ContributedReqMethod("brain.ingest"),
        params=params,
        metadata={},
    )


@pytest.mark.asyncio
async def test_extension_registers_adapter_through_passed_registry() -> None:
    adapters = []
    await extension.register_extensions(SimpleNamespace(register_gateway_adapter=adapters.append))
    assert len(adapters) == 1
    assert isinstance(adapters[0], SecondBrainAdapter)
    assert not any(isinstance(adapter, SecondBrainAdapter) for adapter in contributed_adapters())


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(
        SecondBrainAdapter, "_load_config", staticmethod(lambda: {"second_brain": {"enabled": True}})
    )


@pytest.mark.asyncio
async def test_extension_answers_ingest_rpc_after_registry_rebuild(monkeypatch, tmp_path) -> None:
    root = files("belle_agent.integrations.jiuwenswarm").joinpath(
        "extensions/second_brain"
    )
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    loader = ExtensionLoader(registry)
    with pytest.raises(ValueError):
        parse_req_method("brain.ingest")
    monkeypatch.setattr(
        SecondBrainAdapter, "_load_config", staticmethod(lambda: {"second_brain": {"enabled": True}})
    )
    monkeypatch.setattr(SecondBrainAdapter, "_corpus_dir", staticmethod(lambda _: tmp_path))
    from belle_agent.second_brain import ingest

    monkeypatch.setattr(
        ingest,
        "SecondBrainIngestService",
        lambda *args, **kwargs: SimpleNamespace(ingest_link=AsyncMock(return_value={"id": "doc-1"})),
    )
    await loader.load_extension(root)
    try:
        assert parse_req_method("brain.ingest").value == "brain.ingest"
        server = object.__new__(AgentWebSocketServer)
        for _ in range(2):
            server._adapter_registry = AdapterRegistry()
            mount_contributed_adapters(server._adapter_registry)
            ws = SimpleNamespace(send=AsyncMock())
            handled = await server._dispatch_gateway_adapter_request(
                ws, _request({"kind": "link", "url": "https://example.invalid/doc"}), asyncio.Lock()
            )
            assert handled
            wire = json.loads(ws.send.await_args.args[0])
            assert wire["request_id"] == "r1"
            assert wire["response_kind"] == "e2a.complete"
            assert "doc-1" in str(wire)
    finally:
        for adapter in contributed_adapters():
            if isinstance(adapter, SecondBrainAdapter):
                unregister_gateway_adapter(adapter)


@pytest.mark.asyncio
async def test_missing_kind_is_a_bad_request(_enabled) -> None:
    response = await SecondBrainAdapter().handle(_request({"path": "/x"}))
    assert response.ok is False
    assert response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_unknown_kind_is_a_bad_request(_enabled) -> None:
    response = await SecondBrainAdapter().handle(_request({"kind": "video"}))
    assert response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_link_without_a_url_is_a_bad_request(_enabled) -> None:
    response = await SecondBrainAdapter().handle(_request({"kind": "link"}))
    assert response.payload["code"] == "BAD_REQUEST"
    assert "url is required" in response.payload["error"]


@pytest.mark.asyncio
async def test_a_path_outside_the_sessions_directory_is_refused(
    _enabled, tmp_path, monkeypatch
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions
    )

    response = await SecondBrainAdapter().handle(
        _request({"kind": "file", "path": str(outside)})
    )
    assert response.ok is False
    assert "sessions directory" in response.payload["error"]


@pytest.mark.asyncio
async def test_a_symlink_out_of_the_sessions_directory_is_refused(
    _enabled, tmp_path, monkeypatch
) -> None:
    """Both sides are resolved, so a planted symlink cannot point out."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF")
    link = sessions / "innocent.pdf"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions
    )

    response = await SecondBrainAdapter().handle(
        _request({"kind": "file", "path": str(link)})
    )
    assert response.ok is False


@pytest.mark.asyncio
async def test_disabled_second_brain_refuses(monkeypatch) -> None:
    monkeypatch.setattr(
        SecondBrainAdapter, "_load_config", staticmethod(lambda: {"second_brain": {"enabled": False}})
    )
    response = await SecondBrainAdapter().handle(_request({"kind": "link", "url": "https://x.test"}))
    assert response.ok is False
    assert "not enabled" in response.payload["error"]




def test_disabled_config_builds_no_rail(monkeypatch) -> None:
    from belle_agent.integrations.jiuwenswarm import agent_runtime

    monkeypatch.setattr(
        "belle_agent.integrations.jiuwenswarm.config.load_belle_config",
        lambda: {"second_brain": {"enabled": False}},
    )
    assert agent_runtime._second_brain_rail() is None


def test_default_config_builds_a_rail(monkeypatch, tmp_path) -> None:
    from belle_agent.integrations.jiuwenswarm import agent_runtime

    monkeypatch.setattr(
        "belle_agent.integrations.jiuwenswarm.config.load_belle_config",
        lambda: {"second_brain": {"corpus_dir": str(tmp_path)}},
    )
    rail = agent_runtime._second_brain_rail()
    assert rail is not None
    assert rail.priority == 78
    from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND

    assert rail._backend is SECOND_BRAIN_BACKEND


@pytest.fixture
def _jiuwenswarm_embed(monkeypatch):
    """JiuwenSwarm's own embedding settings, as its configuration file gives them."""
    monkeypatch.setattr(
        "jiuwenswarm.extensions.sdk.memory.get_embed_config",
        lambda: {"api_key": "sk-swarm", "base_url": "http://swarm-embeddings.invalid/v1", "model": "swarm/embed"},
    )


def test_the_embedder_is_read_from_jiuwenswarm_when_belle_names_none(_jiuwenswarm_embed, monkeypatch) -> None:
    from belle_agent.integrations.jiuwenswarm.second_brain_backend import (
        SECOND_BRAIN_BACKEND,
        jiuwenswarm_embedding_settings,
    )

    assert jiuwenswarm_embedding_settings() == {
        "api_base": "http://swarm-embeddings.invalid/v1",
        "api_key": "sk-swarm",
        "model": "swarm/embed",
    }
    # Its variables win over its file, as they do for its own memory.
    monkeypatch.setenv("EMBED_BASE_URL", "http://variable.invalid/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "variable/embed")
    monkeypatch.setenv("EMBED_API_KEY", "sk-variable")
    assert jiuwenswarm_embedding_settings() == {
        "api_base": "http://variable.invalid/v1",
        "api_key": "sk-variable",
        "model": "variable/embed",
    }
    assert SECOND_BRAIN_BACKEND.build_embedder({}).model == "variable/embed"


def test_an_endpoint_in_belles_configuration_is_used_as_it_is(_jiuwenswarm_embed) -> None:
    from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND

    embedder = SECOND_BRAIN_BACKEND.build_embedder({"api_base": "http://belle.invalid/v1", "model": "belle/embed"})
    assert embedder.model == "belle/embed"


def test_with_no_endpoint_anywhere_the_index_is_lexical() -> None:
    from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND

    assert SECOND_BRAIN_BACKEND.build_embedder({}) is None
