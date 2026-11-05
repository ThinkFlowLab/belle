"""Summarize, extract and classify through configured providers."""

import json
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from belle_agent.tools.host import request_metadata
from belle_agent.tools.providers import ProviderError

AttachmentResolver = Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]]
ProviderRequest = Callable[..., Awaitable[dict[str, Any]]]


def _provider_error(endpoint: Mapping[str, Any], status: int, message: Any) -> str:
    """The model's view of a provider error: its text with the endpoint key redacted, or a generic line."""
    detail = "Provider refused the request"
    if isinstance(message, str):
        secret = endpoint.get("api_key")
        detail = message.replace(secret, "[redacted]") if isinstance(secret, str) and secret else message
    return json.dumps(
        {"ok": False, "error": "provider_error", "status": status, "detail": detail[:1000]}, ensure_ascii=False
    )


class ContentToolkit:
    """Expose configured operations and resolve attachments within the current turn."""

    def __init__(
        self,
        endpoints: Mapping[str, Mapping[str, Any]],
        *,
        request: ProviderRequest,
        resolve_attachment: AttachmentResolver | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._request = request
        self._resolve_attachment = resolve_attachment

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return cards only for operations configured by the operator."""
        common = {
            "text": {"type": "string", "minLength": 1},
            "attachment_ref": {
                "type": "string",
                "minLength": 1,
                "description": "Platform attachment reference accessible in this conversation.",
            },
        }
        threshold = {"type": "number", "minimum": 0, "maximum": 1}
        fields: dict[str, dict[str, Any]] = {
            "summarize": {"instructions": {"type": "string"}, "max_tokens": {"type": "integer", "minimum": 1}},
            "extract": {
                "entities": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"type": "string", "minLength": 1},
                },
                "threshold": threshold,
            },
            "classify": {
                **{
                    key: {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {field: {"type": "string", "minLength": 1} for field in names},
                            "required": list(names),
                            "additionalProperties": False,
                        },
                    }
                    for key, names in (("chunks", ("id", "text")), ("labels", ("name", "rubric")))
                },
                "multi_label": {"type": "boolean"},
                "threshold": threshold,
                "reasoning": {"type": "boolean"},
                "system_prompt": {"type": "string"},
            },
        }
        return [
            {
                "name": operation,
                "description": (
                    "Classify text and/or attachment_ref, or chunks exclusively."
                    if operation == "classify"
                    else f"{operation.capitalize()} exactly one of text or attachment_ref."
                ),
                "input_params": {
                    "type": "object",
                    "properties": {**common, **fields[operation]},
                    "required": {"extract": ["entities"], "classify": ["labels"]}.get(operation, []),
                    "additionalProperties": False,
                },
            }
            for operation in fields
            if operation in self._endpoints
        ]

    async def _run(self, operation: str, values: dict[str, Any]) -> str:
        try:
            sources = [key for key in ("text", "attachment_ref", "chunks") if values.get(key) is not None]
            if operation == "classify":
                if not sources or ("chunks" in sources and len(sources) != 1):
                    raise ValueError("provide text and/or attachment_ref, or chunks exclusively")
            elif len(sources) != 1:
                raise ValueError("provide exactly one of text or attachment_ref")
            for key, value in values.items():
                if value is None:
                    continue
                if key in ("text", "attachment_ref", "instructions", "system_prompt"):
                    if not isinstance(value, str) or (key in ("text", "attachment_ref") and not value.strip()):
                        raise ValueError(
                            f"{key} must be a string"
                            + (" containing text" if key in ("text", "attachment_ref") else "")
                        )
                elif key in ("chunks", "labels"):
                    fields = ("id", "text") if key == "chunks" else ("name", "rubric")
                    if (
                        not isinstance(value, list)
                        or not value
                        or any(
                            not isinstance(item, dict)
                            or set(item) != set(fields)
                            or any(not isinstance(item[field], str) or not item[field].strip() for field in fields)
                            for item in value
                        )
                    ):
                        raise ValueError(
                            f"{key} must be a nonempty array of objects with nonempty string fields {', '.join(fields)}"
                        )
                elif key == "entities":
                    if (
                        not isinstance(value, dict)
                        or not value
                        or any(
                            not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
                            for k, v in value.items()
                        )
                    ):
                        raise ValueError("entities must map nonempty entity names to nonempty descriptions")
                elif key in ("multi_label", "reasoning") and not isinstance(value, bool):
                    raise ValueError(f"{key} must be a boolean")
                elif key == "max_tokens" and (type(value) is not int or value < 1):
                    raise ValueError("max_tokens must be a positive integer")
                elif key == "threshold" and (
                    type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                ):
                    raise ValueError("threshold must be a number between 0 and 1")
            if operation == "extract" and values.get("entities") is None:
                raise ValueError("entities must map nonempty entity names to nonempty descriptions")
            if operation == "classify" and values.get("labels") is None:
                raise ValueError("labels must be a nonempty array of objects with nonempty string fields name, rubric")
            payload = {key: value for key, value in values.items() if value is not None and key != "attachment_ref"}
            if "attachment_ref" in sources:
                if self._resolve_attachment is None:
                    raise ValueError("attachment_ref is unavailable on this platform; provide text")
                payload.update(await self._resolve_attachment(values["attachment_ref"], request_metadata()))
        except ValueError as exc:
            return json.dumps({"ok": False, "error": "invalid_arguments", "detail": str(exc)})
        endpoint = self._endpoints.get(operation)
        if endpoint is None:
            return json.dumps({"ok": False, "error": "tool_unavailable", "detail": f"{operation} is not configured"})
        try:
            result = await self._request(**endpoint, operation=operation, payload=payload)
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
            except ValueError:
                body = None
            message = (body.get("detail") or body.get("error")) if isinstance(body, dict) else None
            return _provider_error(endpoint, exc.response.status_code, message)
        except ProviderError as exc:
            # A reply that carries an error goes back to the model, like a refusal, and never into a log.
            message = exc.detail.get("message") if isinstance(exc.detail, dict) else exc.detail
            return _provider_error(endpoint, 200, message)
        return json.dumps(result, ensure_ascii=False)

    async def summarize(
        self,
        text: str | None = None,
        attachment_ref: str | None = None,
        instructions: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Summarize text or an accessible attachment."""
        return await self._run(
            "summarize",
            {"text": text, "attachment_ref": attachment_ref, "instructions": instructions, "max_tokens": max_tokens},
        )

    async def extract(
        self,
        entities: dict[str, str],
        text: str | None = None,
        attachment_ref: str | None = None,
        threshold: float | None = None,
    ) -> str:
        """Extract the named entities from text or an accessible attachment."""
        return await self._run(
            "extract", {"text": text, "attachment_ref": attachment_ref, "entities": entities, "threshold": threshold}
        )

    async def classify(
        self,
        labels: list[dict[str, str]],
        text: str | None = None,
        attachment_ref: str | None = None,
        chunks: list[dict[str, str]] | None = None,
        multi_label: bool | None = None,
        threshold: float | None = None,
        reasoning: bool | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Classify text, an accessible attachment, or chunks against the supplied labels."""
        return await self._run(
            "classify",
            {
                "text": text,
                "attachment_ref": attachment_ref,
                "chunks": chunks,
                "labels": labels,
                "multi_label": multi_label,
                "threshold": threshold,
                "reasoning": reasoning,
                "system_prompt": system_prompt,
            },
        )


def content_toolkit_from_config(config: Mapping[str, Any], platform: Any) -> ContentToolkit | None:
    """Build configured content tools with the platform's permission-aware attachment resolver."""
    from belle_agent.config import model_entry

    endpoints = {}
    for operation, name in (("summarize", "summarizer"), ("extract", "extractor"), ("classify", "classifier")):
        block = model_entry(config, name)
        if (
            block is not None
            and block.get("api") in ("scaledown", "openrouter")
            and block.get("api_base")
            and block.get("api_key")
        ):
            endpoints[operation] = {
                "api": block["api"],
                "base_url": block.get("api_base"),
                "api_key": block.get("api_key"),
                "model": block.get("model_name"),
            }
    if not endpoints:
        return None
    from belle_agent.tools.providers import request_provider

    return ContentToolkit(
        endpoints, request=request_provider, resolve_attachment=getattr(platform, "resolve_content_attachment", None)
    )
