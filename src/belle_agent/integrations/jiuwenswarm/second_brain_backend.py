"""The second brain on JiuwenSwarm, with optional host model settings.

The index and the search are the core's, as on any host. What this adds is
where the endpoints come from when Belle's configuration names none: the
summarizer's model from JiuwenSwarm's model registry, and the embedder from
the ``embed`` section of JiuwenSwarm's configuration and its ``EMBED_*``
variables. An endpoint written in Belle's configuration is used as it is.
"""

import asyncio
import contextvars
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache, partial
from typing import Any

from openjiuwen.core.common.logging.log_config import LogConfig, configure_log_config, get_log_config_snapshot

from belle_agent.second_brain.backend import SUMMARY_TEMPERATURE, Embedder, SummaryModel
from belle_agent.second_brain.default_backend import SECOND_BRAIN_BACKEND as SHARED_BACKEND
from belle_agent.second_brain.default_backend import build_model as build_shared_model
from belle_agent.second_brain.embedder import build_embedder as build_shared_embedder

logger = logging.getLogger(__name__)
_IN_REGISTRY_CALL = contextvars.ContextVar("second_brain_registry_call", default=False)


class _DropRegistryRecord(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _IN_REGISTRY_CALL.get()


for _name in ("common", "llm"):
    logging.getLogger(_name).addFilter(_DropRegistryRecord())

if get_log_config_snapshot() == LogConfig().get_snapshot():
    configure_log_config({"level": "WARNING", "output": ["console"],
                          "interface_output": ["console"], "performance_output": ["console"]})


@contextmanager
def _registry_call() -> Iterator[None]:
    token = _IN_REGISTRY_CALL.set(True)
    try:
        yield
    finally:
        _IN_REGISTRY_CALL.reset(token)


async def _invoke_registry_model(model: Any, system: str, prompt: str, timeout: float) -> str:
    from openjiuwen.core.foundation.llm import SystemMessage, UserMessage

    with _registry_call():
        response = await asyncio.wait_for(
            model.invoke([SystemMessage(content=system), UserMessage(content=prompt)]), timeout=timeout
        )
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def jiuwenswarm_embedding_settings() -> dict[str, str]:
    """The embedding endpoint JiuwenSwarm's own memory uses, read as it reads it, as Belle's settings."""
    from jiuwenswarm.extensions.sdk.memory import get_embed_config

    try:
        configured = get_embed_config() or {}
    except Exception:  # noqa: BLE001
        configured = {}
    return {
        "api_base": os.getenv("EMBED_BASE") or os.getenv("EMBED_BASE_URL") or str(configured.get("base_url") or ""),
        "api_key": os.getenv("EMBED_KEY") or os.getenv("EMBED_API_KEY") or str(configured.get("api_key") or ""),
        "model": os.getenv("EMBED_MODEL") or os.getenv("EMBEDDING_MODEL") or str(configured.get("model") or ""),
    }


def _build_embedder(settings: dict[str, Any]) -> Embedder | None:
    if str(settings.get("api_base") or "").strip():
        return build_shared_embedder(settings)
    return build_shared_embedder(jiuwenswarm_embedding_settings())


@lru_cache(maxsize=1)
def _warn_legacy_model_source() -> None:
    logger.warning("[SecondBrain] host model lookup is deprecated; configure models.summarizer in Belle")


def _build_model(settings: dict[str, Any]) -> SummaryModel | None:
    """Build the summarizer: a Belle model entry, or one from JiuwenSwarm's registry."""
    if settings.get("api") == "chat_completions" or str(settings.get("api_base") or "").strip():
        return build_shared_model(settings)
    _warn_legacy_model_source()
    try:
        from jiuwenswarm.extensions.sdk.host_config import get_default_models
        from openjiuwen.core.foundation.llm import (
            Model,
            ModelClientConfig,
            ModelRequestConfig,
        )
    except ImportError as exc:  # pragma: no cover - deployment without agent-core
        logger.warning("[SecondBrain] model unavailable: %s", exc)
        return None

    entries = get_default_models() or []
    if not entries:
        logger.warning("[SecondBrain] no default models configured; skipping prose")
        return None

    wanted = str(settings.get("model") or "").strip()
    entry = None
    if wanted:
        entry = next(
            (
                candidate
                for candidate in entries
                if str(
                    (candidate.get("model_client_config") or {}).get("model_name") or ""
                )
                == wanted
            ),
            None,
        )
        if entry is None:
            logger.warning(
                "[SecondBrain] summarizer model %r is not in models.defaults;"
                " using the default",
                wanted,
            )
    if entry is None:
        entry = next((e for e in entries if e.get("is_default")), entries[0])

    client_config = dict(entry.get("model_client_config") or {})
    model_name = str(client_config.pop("model_name", "") or "default")
    if client_config.get("custom_headers") == "":
        del client_config["custom_headers"]
    client_config.setdefault("api_key", "")
    client_config.setdefault("api_base", "")
    client_config.setdefault("client_provider", "OpenAI")

    from jiuwenswarm.extensions.sdk.host_config import (
        build_reasoning_model_request_kwargs,
    )

    request_config = dict(entry.get("model_config_obj") or {})
    request_config["temperature"] = SUMMARY_TEMPERATURE
    try:
        request_kwargs = build_reasoning_model_request_kwargs(
            model_client_config={**client_config, "model_name": model_name},
            model_config_obj=request_config,
            model_name=model_name,
        )
        model = Model(
            model_client_config=ModelClientConfig(
                model_name=model_name, **client_config
            ),
            model_config=ModelRequestConfig(**request_kwargs),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SecondBrain] could not build summarizer model: %s", exc)
        return None
    return SummaryModel(model=model_name, invoke=partial(_invoke_registry_model, model))


SECOND_BRAIN_BACKEND = replace(SHARED_BACKEND, build_model=_build_model, build_embedder=_build_embedder)

__all__ = ["SECOND_BRAIN_BACKEND", "jiuwenswarm_embedding_settings"]
