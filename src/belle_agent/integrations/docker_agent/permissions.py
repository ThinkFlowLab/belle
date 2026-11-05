"""Write a scope's tool levels as Docker Agent's session permissions.

A scope only tightens: a tool it names at ``deny`` or ``ask`` gets a pattern of
that kind, and a tool it does not name gets none. The session runs in the
``autonomous`` safety mode, so a tool with no pattern runs without a question.
"""

from collections.abc import Callable, Mapping

from belle_agent.scopes import LEVEL_ASK, LEVEL_DENY


def session_permissions(tools: Mapping[str, str], advertised: Callable[[str], str]) -> dict[str, list[str]]:
    """The ``deny`` and ``ask`` patterns of a scope's tool levels.

    ``advertised`` gives the name Docker Agent advertises a tool under: one
    of Belle's own tools has the toolset's prefix, any other keeps its name.
    """
    return {
        kind: sorted(advertised(name) for name, level in tools.items() if level == kind)
        for kind in (LEVEL_DENY, LEVEL_ASK)
    }
