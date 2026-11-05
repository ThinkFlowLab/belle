"""Run Belle's turns on an agent harness over ACP v1, through a relay's Unix socket.

The ACP SDK is an optional dependency, in the ``acp`` extra. Only this package imports it.
A profile with ``tools_mount: session`` or ``agent`` also needs the tool server of the ``mcp`` extra.
"""

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import acp  # noqa: F401
except ImportError as exc:
    raise ImportError("backend acp needs the ACP SDK of the acp extra: install belle-agent[acp]") from exc

from belle_agent.config import BelleConfigError
from belle_agent.integrations.acp.adapter import AcpBackend
from belle_agent.integrations.acp.settings import MOUNT_NONE, read_settings


def create_backend(config: Mapping[str, Any], state_dir: Path) -> AcpBackend:
    """Build the backend the selected ``backends.acp`` profile describes. It is not started."""
    settings = read_settings(config)
    profile = settings.profile
    if profile.tools_mount != MOUNT_NONE and importlib.util.find_spec("mcp") is None:
        raise BelleConfigError(
            f"Belle config backends.acp.profiles.{profile.name}.tools_mount: {profile.tools_mount} needs the tool"
            " server of the mcp extra: install belle-agent[acp,mcp], or set tools_mount: none"
        )
    return AcpBackend(settings, state_dir)


__all__ = ["AcpBackend", "create_backend"]
