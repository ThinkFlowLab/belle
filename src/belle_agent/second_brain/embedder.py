"""OpenAI-compatible embeddings for the Second Brain index."""

import asyncio
import base64
import math
import struct
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from belle_agent.second_brain.backend import Embedder

_BATCH_SIZE = 8
_CONCURRENCY = 4
_TIMEOUT = 60.0


def build_embedder(
    settings: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> Embedder | None:
    """Build an embedder when the configuration names an endpoint and model."""
    base = str(settings.get("api_base") or "").strip()
    model = str(settings.get("model") or "").strip()
    if not base or not model:
        return None
    parts = urlsplit(base)
    path = parts.path.rstrip("/").removesuffix("/embeddings") + "/embeddings"
    url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    key = str(settings.get("api_key") or "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    async def documents(texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batches = [texts[i:i + _BATCH_SIZE] for i in range(0, len(texts), _BATCH_SIZE)]
        gate = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
            async def one(batch: list[str]) -> list[list[float]]:
                async with gate:
                    return await _embed(client, url, headers, model, batch, retry=True)

            groups = await asyncio.gather(*(one(batch) for batch in batches))
        return [vector for group in groups for vector in group]

    async def query(text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
            return (await _embed(client, url, headers, model, [text], retry=False))[0]

    return Embedder(model=model, embed_documents=documents, embed_query=query)


async def _embed(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], model: str,
    texts: list[str], *, retry: bool,
) -> list[list[float]]:
    attempts = 3 if retry else 1
    for number in range(attempts):
        try:
            response = await client.post(url, headers=headers, json={"model": model, "input": texts})
            response.raise_for_status()
            return _vectors(response.json(), len(texts))
        except httpx.TransportError:
            if number + 1 == attempts:
                raise
            await asyncio.sleep(number + 1)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if number + 1 == attempts or (status != 429 and status < 500):
                raise
            delay = _retry_after(exc.response.headers.get("Retry-After"), number + 1)
            if delay > _TIMEOUT:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("embedding retries exhausted")


def _retry_after(value: str | None, fallback: int) -> float:
    if value is None:
        return float(fallback)
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            return max(0.0, (moment - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return float(fallback)


def _vectors(reply: Any, expected: int) -> list[list[float]]:
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, list):
        raise TypeError("embedding response data must be a list")
    ordered: list[list[float] | None] = [None] * expected
    for item in data:
        if not isinstance(item, dict) or type(item.get("index")) is not int or not 0 <= item["index"] < expected:
            raise ValueError("embedding response data[i].index must be a distinct input index")
        index = item["index"]
        if ordered[index] is not None:
            raise ValueError("embedding response data[i].index must be a distinct input index")
        ordered[index] = _vector(item.get("embedding"))
    if any(vector is None for vector in ordered):
        raise ValueError("embedding response data must contain one vector per input")
    return [vector for vector in ordered if vector is not None]


def _vector(value: Any) -> list[float]:
    values: Sequence[float]
    if isinstance(value, str):
        try:
            raw = base64.b64decode(value, validate=True)
            if not raw or len(raw) % 4:
                raise ValueError
            values = struct.unpack(f"<{len(raw) // 4}f", raw)
        except (ValueError, struct.error) as exc:
            raise ValueError("embedding response data[i].embedding must be float or base64 float32") from exc
    elif isinstance(value, list) and value:
        values = value
    else:
        raise ValueError("embedding response data[i].embedding must be float or base64 float32")
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in values):
        raise ValueError("embedding response data[i].embedding must contain finite numbers")
    return [float(item) for item in values]
