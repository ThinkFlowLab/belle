"""What marks a turn as the run of a session, apart from a turn that a message or an event started."""

from dataclasses import dataclass
from typing import Any

from belle_agent.scheduling.address import Address


@dataclass(frozen=True)
class SessionRunTurn:
    """The session run that a host turn carries out."""

    session_id: str
    run_id: str
    target: Address
    questions: str
    turn: dict[str, Any]
    #: post_final, notice or store: see ``SessionRun.delivery``.
    delivery: str
