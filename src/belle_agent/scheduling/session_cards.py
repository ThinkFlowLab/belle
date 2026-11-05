"""Tool cards for detached and recorded sessions."""

import json
from importlib.resources import files
from typing import Any, cast


def session_cards() -> list[dict[str, Any]]:
    """Return the session tool cards."""
    return cast(list[dict[str, Any]], json.loads(
        files("belle_agent.scheduling").joinpath("session_cards.json").read_text()
    ))
