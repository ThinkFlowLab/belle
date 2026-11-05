"""Turn and reply decisions shared by conversation adapters."""

from belle_agent.scopes import (
    MID_TURN_DEFAULT,
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    MID_TURN_VALUES,
)


def mid_turn_mode(configured: str | None, *, shared_session: bool, requires_separate_turn: bool = False) -> str:
    """Choose how a new message joins a running conversation."""
    mode = configured if configured in MID_TURN_VALUES else MID_TURN_DEFAULT
    if mode == MID_TURN_STEER and (shared_session or requires_separate_turn):
        return MID_TURN_QUEUE
    return mode


def conversation_key(
    *, platform: str, workspace: str, conversation: str, sender: str, thread: str,
    direct: bool, shared_session: bool = False,
) -> str:
    """Name the conversation key of the attached session that a message belongs to. No workspace gives no key.

    A direct message keys on its sender, a shared conversation on the platform
    conversation alone, and any other message on the thread it sits in.
    """
    workspace = str(workspace or "").strip()
    if not workspace:
        return ""
    if direct:
        return f"{platform}_{workspace}_{conversation}_{sender}"
    if shared_session:
        return f"{platform}_{workspace}_{conversation}"
    return f"{platform}_{workspace}_{conversation}_{thread}"
