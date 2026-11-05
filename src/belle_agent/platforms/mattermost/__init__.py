"""Mattermost: a bot account on one server or on several, over the REST API and the WebSocket."""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import expand_env
from belle_agent.platforms.mattermost.settings import KEY_PATH, KEY_SERVERS, SECTION, TOKEN_ENV, server_entries


def create_platform(config: Mapping[str, Any], state_dir: Path | None = None) -> Any:
    """Build the Mattermost connector from the ``mattermost`` section.

    The connector is imported here and not with the package, so a process that
    uses one part of the package loads only that part.
    """
    from belle_agent.platforms.mattermost.connector import create_platform as create

    return create(config)


def credentials(section: Mapping[str, Any]) -> dict[str, bool]:
    """Name each credential the ``mattermost`` section needs, and whether it resolves.

    The address is named by the variable it is read from, where the section reads it from one. A section
    that lists servers needs one server that is on, and an entry that is on in part is refused here.
    """
    if section.get(KEY_SERVERS):
        on = any(not entry.off for entry in server_entries(section))
        return {f"a server of {KEY_PATH}.{KEY_SERVERS} whose url and bot token resolve": on}
    url = str(section.get("url") or "")
    variable = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", url.strip())
    return {
        variable[1] if variable else f"{SECTION}.url": bool(expand_env(url).strip()),
        TOKEN_ENV: bool(os.environ.get(TOKEN_ENV, "").strip()),
    }
