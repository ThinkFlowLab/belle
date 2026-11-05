"""Spell a scope's tool levels as OpenCode's session permission rules.

A scope names tools. An OpenCode permission rule names an action, and the
action that gates a tool is not always the tool's name. The table below was
measured on the pinned build with ``tests/opencode/probe_builtin_actions.py``:
each built-in tool was called under an ``ask`` rule and the action of the
request it raised was recorded, and each name was denied and the tools that
left the roster were recorded.

Two facts of that measurement shape this module. ``write`` and ``edit`` are
gated by one action, ``edit``, and a rule on ``write`` matches nothing: so a
level on either tool is written on ``edit`` and governs both. ``execute``
raises no request under any ``ask`` rule: it can be denied and cannot be
asked about.

The Code Mode tools are not in the roster. The model calls them inside
``execute``. A deny on ``execute`` removes ``execute`` alone, and OpenCode
then offers the Code Mode tools as direct tools. Each one has the name
that OpenCode gives it, ``opencode_worktree_create`` for
``opencode.worktree_create``, and a deny on that name removes that tool
alone. The exception is ``opencode_worktree_remove``: ``edit`` gates it, as
it gates ``write``.
Five other Code Mode tools raise no request under any ``ask`` rule, as
``execute`` does. ``opencode_list_mcp_resources`` raises one request that
names every mounted MCP server, so it asks while a server is mounted, as
Belle's is.
OpenCode offers ``todowrite`` only when its ``experimental.todo`` setting is
on.
"""

from collections.abc import Callable, Collection, Iterable, Mapping

from belle_agent.scopes import LEVEL_ASK, LEVEL_DENY

EFFECT_ALLOW = "allow"
EFFECT_ASK = "ask"
EFFECT_DENY = "deny"
_EFFECTS = {LEVEL_ASK: EFFECT_ASK, LEVEL_DENY: EFFECT_DENY}

#: Each built-in tool of the pinned build, and the permission action that gates it.
BUILTIN_ACTIONS: Mapping[str, str] = {
    "shell": "shell",
    "execute": "execute",
    "read": "read",
    "grep": "grep",
    "glob": "glob",
    "edit": "edit",
    "write": "edit",
    "webfetch": "webfetch",
    "websearch": "websearch",
    "subagent": "subagent",
    "skill": "skill",
    "question": "question",
    "todowrite": "todowrite",
    "opencode_session_rename": "opencode_session_rename",
    "opencode_session_move": "opencode_session_move",
    "opencode_worktree_create": "opencode_worktree_create",
    "opencode_worktree_list": "opencode_worktree_list",
    "opencode_worktree_remove": "edit",
    "opencode_models": "opencode_models",
    "opencode_list_mcp_resources": "opencode_list_mcp_resources",
    "opencode_read_mcp_resource": "opencode_read_mcp_resource",
}
#: Actions that are no tool's name and that a scope may still name. ``external_directory`` gates every
#: path outside the server's working directory, for whichever tool reaches for it.
OTHER_ACTIONS: frozenset[str] = frozenset({"external_directory"})
#: Tools that run without a permission request whatever rule says ``ask``.
NEVER_ASKED: frozenset[str] = frozenset({
    "execute",
    "opencode_session_rename",
    "opencode_session_move",
    "opencode_worktree_create",
    "opencode_worktree_list",
    "opencode_models",
})

#: What a scope may name of OpenCode's own, with the levels OpenCode applies to each.
TOOL_LEVELS: Mapping[str, frozenset[str]] = {
    name: frozenset({LEVEL_DENY} if name in NEVER_ASKED else {LEVEL_ASK, LEVEL_DENY})
    for name in (*BUILTIN_ACTIONS, *OTHER_ACTIONS)
}


def permission_rules(
    tools: Mapping[str, str],
    advertised: Callable[[str], str | None],
    skills_available: Collection[str] | None = None,
    skills_unavailable: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Write a scope's tool levels as a session ruleset, one rule per action.

    ``advertised`` gives the name OpenCode advertises one of Belle's own tools
    under, which is also its action, and nothing for any other name. A
    built-in tool is written on the action that gates it. A name that is
    neither is written as it stands: it may be a tool of a server the operator
    mounted, which OpenCode gates by its advertised name. A rule on an action
    OpenCode does not know matches nothing, and the host reports such a name
    at start.

    Two tools of one action take the stricter of their levels. A tool the
    scope does not name gets no rule, so an empty scope restricts nothing.

    The skill rules come last: OpenCode applies the last rule that matches.
    A ``skills_available`` list, ``None`` for every skill, denies every skill
    on the ``skill`` action, then gives each listed skill the level of
    ``skill`` itself, or ``allow``. Then each name in ``skills_unavailable``
    gets a ``deny`` rule for that skill alone. So a skill is usable where
    ``capability_catalog.scope_allows`` allows it.
    """
    effects: dict[str, str] = {}
    for name, level in tools.items():
        effect = _EFFECTS.get(level)
        if effect is None:
            continue
        action = advertised(name) or BUILTIN_ACTIONS.get(name, name)
        if effects.get(action) != EFFECT_DENY:
            effects[action] = effect
    rules = [{"action": action, "resource": "*", "effect": effect} for action, effect in effects.items()]
    skill = BUILTIN_ACTIONS["skill"]
    if skills_available is not None:
        listed = effects.get(skill, EFFECT_ALLOW)
        rules.append({"action": skill, "resource": "*", "effect": EFFECT_DENY})
        rules += [{"action": skill, "resource": name, "effect": listed} for name in sorted(skills_available)]
    return rules + [{"action": skill, "resource": name, "effect": EFFECT_DENY} for name in sorted(skills_unavailable)]
