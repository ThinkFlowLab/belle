"""HTTP requests for Belle's provider task tools."""

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


class ProviderError(ValueError):
    """A provider answered with an ``error`` field. ``detail`` holds that field, which may quote the request."""

    def __init__(self, detail: Any) -> None:
        super().__init__("provider returned an error")
        self.detail = detail


def _request(
    api: str, base_url: str, model: str | None, operation: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    if api not in {"scaledown", "openrouter", "chat_completions"}:
        raise ValueError("api must be scaledown, openrouter, or chat_completions")
    if operation not in {"summarize", "extract", "classify"}:
        raise ValueError("operation must be summarize, extract, or classify")
    if api == "openrouter" and operation == "classify":
        raise ValueError("classify requires api scaledown; OpenRouter classification uses the Decisions API")
    if api == "chat_completions" and operation != "summarize":
        raise ValueError("operation must be summarize when api is chat_completions")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("base_url must be an absolute HTTP URL without query or fragment")
    path = parts.path.rstrip("/")
    body = dict(payload)
    if operation == "classify":
        path = path.removesuffix("/v1") + "/classify"
    else:
        if api == "scaledown" and not path.endswith("/v1"):
            path += "/v1"
        path += "/chat/completions"
        body["model"] = model or (f"scaledown/{operation}" if api == "openrouter" else operation)
        messages: list[dict[str, Any]] = []
        instruction_key = "instructions" if operation == "summarize" else "instruction"
        if instruction_key in body:
            messages.append({"role": "system", "content": body[instruction_key]})
        content: Any = body.get("text", f"{operation.capitalize()} the attached document.")
        if api in {"openrouter", "chat_completions"}:
            body.pop("text", None)
            body.pop(instruction_key, None)
            if "document" in body:
                document = body.pop("document")
                mime_type = body.pop("document_mime_type")
                data_url = f"data:{mime_type};base64,{document}"
                part = (
                    {"type": "image_url", "image_url": {"url": data_url}}
                    if mime_type.startswith("image/")
                    else {"type": "file", "file": {"filename": "document.pdf", "file_data": data_url}}
                )
                content = [{"type": "text", "text": content}, part]
            if operation == "extract":
                options = {key: body.pop(key) for key in ("entities", "threshold", "top_n", "context_chars") if key in body}
                body["provider"] = {"options": {"scaledown": options}}
        messages.append({"role": "user", "content": content})
        body["messages"] = messages
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), body


async def request_provider(
    *,
    api: str,
    base_url: str,
    api_key: str,
    model: str | None,
    operation: str,
    payload: dict[str, Any],
    timeout: float = 120.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Send a validated task payload and return the complete provider response.

    HTTP, transport, and JSON errors propagate to the calling tool.
    """
    url, body = _request(api, base_url, model, operation, payload)
    headers = {"x-api-key": api_key} if api == "scaledown" else {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.post(url, json=body, headers=headers)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise TypeError("provider response must be a JSON object")
    if "error" in result:
        raise ProviderError(result["error"])
    return result



def summary_text(api: str, result: dict[str, Any]) -> str:
    """Return the summary text of a ``summarize`` response.

    OpenRouter puts the text in ``choices[0].message.content``. Direct ScaleDown puts a JSON object
    there, and the text is its ``summary`` field.
    """
    choices = result.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list) and api != "scaledown":
        content = " ".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str) or not content:
        raise ValueError(f"{api} response has no text at choices[0].message.content")
    if api != "scaledown":
        return content
    try:
        reply = json.loads(content)
    except ValueError:
        reply = None
    summary = reply.get("summary") if isinstance(reply, dict) else None
    if not isinstance(summary, str):
        raise ValueError("scaledown response has no text at choices[0].message.content.summary")  # noqa: TRY004
    return summary
