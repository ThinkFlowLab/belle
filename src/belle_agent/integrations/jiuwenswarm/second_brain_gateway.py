"""Wire JiuwenSwarm channels to AgentServer second-brain ingestion."""

import logging
from typing import Any

from jiuwenswarm.extensions.sdk.agent_server import request_agent_unary, shares_gateway_directory

from belle_agent.integrations.jiuwenswarm.config import load_belle_config
from belle_agent.second_brain.paths import second_brain_config

logger = logging.getLogger(__name__)


def wire_brain_ingest_hook(channel: Any, agent_client: Any) -> None:
    """Install the hook only when AgentServer shares Gateway's sessions directory."""
    setter = getattr(channel, "set_brain_ingest_hook", None)
    if setter is None:
        return

    try:
        config = load_belle_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ChannelManager] cannot read Belle config for second_brain: %s", exc)
        return
    if not second_brain_config(config).get("enabled") or agent_client is None:
        return

    if not shares_gateway_directory(agent_client):
        logger.info(
            "[ChannelManager] second-brain ingest is not wired for %s: the"
            " AgentServer does not share this Gateway's filesystem",
            getattr(channel, "channel_id", "?"),
        )
        return

    platform = str(getattr(channel, "channel_id", "") or "").strip() or "im"

    async def _brain_hook(params: dict[str, Any]) -> dict[str, Any]:
        ok, payload = await request_agent_unary(
            agent_client=agent_client,
            method="brain.ingest",
            params=params,
            channel_id=platform,
            label="brain.ingest",
            # A Gateway timeout would not stop AgentServer summarization.
        )
        if not ok:
            raise RuntimeError(str(payload.get("error") or "brain.ingest failed"))
        return payload

    setter(_brain_hook)
