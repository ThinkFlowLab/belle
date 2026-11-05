"""Map message states to platform reactions."""

from collections.abc import Awaitable, Callable

from belle_agent.platforms.base import MARK_REACTIONS


async def react_marks(
    react: Callable[..., Awaitable[None]],
    conversation: str,
    message_id: str,
    state: str,
    emoji: Callable[[str], str],
) -> None:
    """Remove old reactions, then add those configured for ``state``."""
    for name, remove in reaction_changes(state, emoji):
        await react(conversation, message_id, name, remove=remove)


def reaction_changes(state: str, emoji: Callable[[str], str]) -> tuple[tuple[str, bool], ...]:
    """Plan named reaction removals before additions; omit disabled reactions."""
    add, remove = MARK_REACTIONS.get(state, ((), ()))
    return tuple(
        (name, removed)
        for keys, removed in ((remove, True), (add, False))
        for key in keys
        if (name := emoji(key))
    )
