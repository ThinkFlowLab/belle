"""Reader-facing Slack fields for shared bounded text extraction."""

from belle_agent.structured_text import MAX_TEXT_DEPTH as MAX_RICH_TEXT_DEPTH  # noqa: F401
from belle_agent.structured_text import MAX_TEXT_STRINGS as MAX_RICH_TEXT_STRINGS  # noqa: F401
from belle_agent.structured_text import (  # noqa: F401 - compatibility exports
    walk_strings,
    walk_strings_bounded,
)

# Only reader-facing fields; omit IDs and Block Kit syntax.
RICH_TEXT_KEYS = frozenset(
    {
        "text",
        "title",
        "pretext",
        "footer",
        "label",
        "placeholder",
        "alt_text",
        "fallback",
        "name",
        "url",
    }
)
