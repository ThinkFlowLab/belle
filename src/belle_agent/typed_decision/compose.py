"""Build an optional typed-decision client from Belle settings."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from belle_agent.config import model_entry
from belle_agent.typed_decision.adapters.jev import JevAdapter
from belle_agent.typed_decision.adapters.openai import OpenAIDecisionsAdapter
from belle_agent.typed_decision.adapters.openrouter_text import OpenRouterTextStateAdapter
from belle_agent.typed_decision.adapters.scaledown import ScaleDownAdapter
from belle_agent.typed_decision.client import (
    DEFAULT_TIMEOUT_SECONDS,
    TypedDecisionClient,
)
from belle_agent.typed_decision.schema import TypedDecisionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionModel:
    """Connection settings for a decision model."""

    api: str
    model_name: str
    api_base: str
    api_key: str
    extra_body: Mapping[str, Any]


ADAPTERS: Mapping[str, Any] = {
    "openrouter": JevAdapter,
    "openrouter_text_state": OpenRouterTextStateAdapter,
    "openai": OpenAIDecisionsAdapter,
    "scaledown": ScaleDownAdapter,
}


def decision_model(config: Mapping[str, Any] | None) -> DecisionModel | None:
    """Return configured model settings, or ``None`` when incomplete."""
    if not isinstance(config, Mapping):
        return None
    block = model_entry(config, "decision")
    if not isinstance(block, Mapping):
        return None
    api = block.get("api", "openrouter")
    if not isinstance(api, str) or api not in ADAPTERS:
        raise TypedDecisionError(f"unsupported decision API: {api!r}")
    model_name = str(block.get("model_name") or "").strip()
    api_base = str(block.get("api_base") or "").strip()
    api_key = str(block.get("api_key") or "").strip()
    if not model_name or not api_base or not api_key:
        return None
    model_config_obj = block.get("model_config_obj")
    extra_body: Mapping[str, Any] = {}
    if isinstance(model_config_obj, Mapping):
        raw = model_config_obj.get("extra_body")
        if isinstance(raw, Mapping):
            extra_body = MappingProxyType(dict(raw))
    return DecisionModel(
        api=api,
        model_name=model_name,
        api_base=api_base,
        api_key=api_key,
        extra_body=extra_body,
    )


def select_adapter_factory(
    api: str,
    adapters: Mapping[str, Any] = ADAPTERS,
) -> Any:
    """Return the adapter for the configured API."""
    try:
        return adapters[api]
    except KeyError as exc:
        raise TypedDecisionError(f"unsupported decision API: {api!r}") from exc


def build_client(config: Mapping[str, Any] | None) -> TypedDecisionClient | None:
    """Build the configured client, or return ``None`` when disabled."""
    settings = decision_model(config)
    if settings is None:
        return None
    adapter = select_adapter_factory(settings.api)(
        model_name=settings.model_name,
        api_base=settings.api_base,
        api_key=settings.api_key,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        extra_body=settings.extra_body,
    )
    logger.info(
        "[typed_decision] decision model configured: adapter=%s model=%s",
        getattr(adapter, "name", "unknown"),
        settings.model_name,
    )
    return TypedDecisionClient(adapter)
