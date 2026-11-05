"""HTTP transport shared by decision providers."""

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from belle_agent.typed_decision.schema import TypedDecisionError


async def post_json(
    url: str,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    logger: logging.Logger,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
) -> Any:
    """Post a decision request without exposing refused response bodies."""
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise TypedDecisionError(f"decisions request failed: {exc}") from exc
    if response.status_code >= 400:
        # Keep the response body out of the exception.
        logger.debug(
            "[typed_decision] decisions endpoint refused: status=%s",
            response.status_code,
        )
        raise TypedDecisionError(f"decisions endpoint returned {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TypedDecisionError("decisions response was not JSON") from exc
    return payload
