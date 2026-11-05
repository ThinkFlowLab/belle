"""Slack: the parts every Slack connection uses, and the connector that makes a platform of them."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.platforms.slack.validation import validate_config as validate_config


def create_platform(config: Mapping[str, Any], state_dir: Path | None = None) -> Any:
    """Build the Slack connector from the ``slack`` section.

    The connector is imported here and not with the package, so a process that
    uses one part of the package loads only that part.
    """
    from belle_agent.platforms.slack.connector import create_platform as create

    return create(config)


#: The variables the token pair is read from where the section writes none, as the connector reads them.
TOKEN_VARIABLES = {"bot_token": "SLACK_BOT_TOKEN", "app_token": "SLACK_APP_TOKEN"}
#: The token fields of the section, which take only a ``${NAME}`` reference.
SECRET_FIELDS: dict[tuple[str, ...], str] = {
    **{(key,): variable for key, variable in TOKEN_VARIABLES.items()},
    **{("workspaces", "[]", key): variable for key, variable in TOKEN_VARIABLES.items()},
}


def credentials(section: Mapping[str, Any]) -> dict[str, bool]:
    """Name each credential the ``slack`` section needs, and whether it resolves.

    A section that lists workspaces holds its credentials in them, and the connector names an entry that has none.
    """
    if section.get("workspaces"):
        return {"platforms.slack.workspaces": True}
    found = {}
    for key, variable in TOKEN_VARIABLES.items():
        configured = str(section.get(key) or "").strip()
        found[f"platforms.slack.{key}" if configured else variable] = bool(configured or os.environ.get(variable, "").strip())
    return found
