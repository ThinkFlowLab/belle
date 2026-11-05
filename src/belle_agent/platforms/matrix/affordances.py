"""Matrix capability declarations."""

from belle_agent.platforms.base import PlatformAffordances

#: A silent or stopped turn keeps its settled card: redaction would move a deleted-message notice into the room timeline.
MATRIX_AFFORDANCES = PlatformAffordances(
    questions="present", silent_card="keep", reply_parts_in_thread=False,
)
