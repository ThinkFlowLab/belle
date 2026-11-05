"""Discord: a bot application over the REST API and the gateway."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import platform_section
from belle_agent.platforms.discord.settings import SECTION, TOKEN_ENV, bot_entries, read_bots, read_settings


def validate_config(config: Mapping[str, Any], config_path: Path) -> None:
    """Check the ``discord`` section's keys and forms where it is present, before shared validation."""
    section = platform_section(config, SECTION)
    if isinstance(section, Mapping):
        if "bots" in section:
            entries = bot_entries(section)
            # Config validation checks the shape before credential availability is checked at startup.
            read_bots(section, {entry["token_env"]: str(index) for index, entry in enumerate(entries)})
            if all(os.environ.get(entry["token_env"]) for entry in entries):
                read_bots(section)
        else:
            read_settings(section, {**os.environ, TOKEN_ENV: os.environ.get(TOKEN_ENV) or "unchecked"})


def create_platform(config: Mapping[str, Any], state_dir: Path | None = None) -> Any:
    """Build the Discord connector from the ``discord`` section.

    The connector is imported here and not with the package, so a process that
    uses one part of the package loads only that part.
    """
    from belle_agent.platforms.discord.connector import create_platform as create

    return create(config)


def credentials(section: Mapping[str, Any]) -> dict[str, bool]:
    """Name each credential the ``discord`` section needs, and whether it resolves."""
    if "bots" in section:
        return {entry["token_env"]: bool(os.environ.get(entry["token_env"], "").strip()) for entry in bot_entries(section)}
    return {TOKEN_ENV: bool(os.environ.get(TOKEN_ENV, "").strip())}
