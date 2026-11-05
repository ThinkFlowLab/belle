"""A platform that keeps its conversations in memory."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import expand_env, platform_section
from belle_agent.platforms.loopback.platform import (
    CHAT_TYPE_CHANNEL,
    CHAT_TYPE_DM,
    LOOPBACK_CAPABILITIES,
    NAME,
    POST_MESSAGE,
    REACT_TO_MESSAGE,
    LoopbackPlatform,
    LoopbackToolkit,
    Outbound,
)


def create_platform(config: Mapping[str, Any], state_dir: Path | None = None) -> LoopbackPlatform:
    """Build the platform. Its one setting is ``platforms.loopback.control``, the socket a person drives it through."""
    section = platform_section(config, NAME)
    control = (section if isinstance(section, Mapping) else {}).get("control")
    return LoopbackPlatform(control_path=expand_env(str(control or "")))


__all__ = [
    "CHAT_TYPE_CHANNEL",
    "CHAT_TYPE_DM",
    "LOOPBACK_CAPABILITIES",
    "NAME",
    "POST_MESSAGE",
    "REACT_TO_MESSAGE",
    "LoopbackPlatform",
    "LoopbackToolkit",
    "Outbound",
    "create_platform",
]
