"""Resolve the optional decision configuration."""

import pytest

from belle_agent.typed_decision import (
    JevAdapter,
    OpenAIDecisionsAdapter,
    TypedDecisionClient,
    TypedDecisionError,
    build_client,
    decision_model,
    select_adapter_factory,
)


def decision(**fields) -> dict:
    """A configuration with one ``models.decision`` entry."""
    return {"models": {"decision": {"entries": [{"id": "decision", "api": "openrouter", **fields}]}}}


FILLED = {
    "api_base": "https://openrouter.example/api/v1",
    "api_key": "sk-live",
    "model_name": "typesafe/jev-1.13",
    "model_config_obj": {"extra_body": {"provider": {"order": ["TypeSafe"]}}},
}
CONFIGURED = decision(**FILLED)


def test_a_filled_block_resolves_whole() -> None:
    settings = decision_model(CONFIGURED)
    assert settings is not None
    assert settings.api == "openrouter"
    assert settings.model_name == "typesafe/jev-1.13"
    assert settings.extra_body == {"provider": {"order": ["TypeSafe"]}}


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"models": {"decision": None}},
        # What the shipped template resolves to with no DECISION_* variable
        # set: every key present, every value empty.
        decision(api_base="", api_key="", model_name="", model_config_obj=FILLED["model_config_obj"]),
        decision(api_base="https://x", api_key="k"),
    ],
    ids=[
        "no-config",
        "empty-config",
        "decision-is-blank",
        "template-with-nothing-filled-in",
        "no-model-name",
    ],
)
def test_nothing_configured_builds_no_client(config) -> None:
    """The absence of a client is the whole contract of the off switch."""
    assert decision_model(config) is None
    assert build_client(config) is None


def test_a_filled_block_builds_a_client_over_the_claiming_adapter() -> None:
    client = build_client(CONFIGURED)
    assert isinstance(client, TypedDecisionClient)
    assert client.name == "openrouter"
    assert isinstance(client.adapter, JevAdapter)
    assert client.adapter.url.endswith("/api/alpha/decisions")


def test_api_selects_adapter_independently_of_model_vendor() -> None:
    assert select_adapter_factory("openrouter") is JevAdapter
    assert select_adapter_factory("openai") is OpenAIDecisionsAdapter
    with pytest.raises(TypedDecisionError, match="unsupported decision API"):
        select_adapter_factory("other")


def test_native_openai_builds_a_client_without_openrouter_options() -> None:
    config = decision(api="openai", api_base="https://api.openai.com/v1", api_key="secret", model_name="gpt-6-luna")
    client = build_client(config)
    assert isinstance(client.adapter, OpenAIDecisionsAdapter)
    assert client.adapter.url == "https://api.openai.com/v1/decisions"


def test_native_openai_rejects_openrouter_provider_options() -> None:
    config = decision(**{**FILLED, "api": "openai", "api_base": "https://api.openai.com/v1"})
    with pytest.raises(TypedDecisionError, match="extra_body requires an OpenRouter decision API"):
        build_client(config)
