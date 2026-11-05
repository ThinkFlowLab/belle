"""The grammar of a chat command, and the answers to its model subcommands, shared by the platforms.

A command is a word, such as ``/belle``, then one or two words that name a
subcommand, then its arguments. Each platform lists its subcommands and
writes its own help. The model subcommands change what the host keeps for a
conversation, so their rule and their answers are the same on every
platform, and so are the lists of skills, subagents and experts. A platform
gives the function that escapes a name in its markup, and the one that makes
a text strong.
"""

import re
from collections.abc import Callable, Collection, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from belle_agent.backend import CATALOG_SKILLS, CATALOG_SUBAGENTS, BackendCapabilities, TurnScope
from belle_agent.capability_catalog import gloss, skill_name
from belle_agent.models import ConfiguredModels


@dataclass(frozen=True)
class Subcommand:
    """One or two words after the slash command."""

    words: tuple[str, ...]
    summary: str
    arguments: str = ""

    @property
    def name(self) -> str:
        """The words of the subcommand, as a person types them."""
        return " ".join(self.words)

    @property
    def requires_arguments(self) -> bool:
        """Whether the arguments name a required ``<part>``; a ``[part]`` is optional."""
        return "<" in re.sub(r"\[[^]]*\]", "", self.arguments)


def usage(sub: Subcommand, command: str) -> str:
    """Return the command token, subcommand, and arguments as one line."""
    return " ".join(part for part in (command, sub.name, sub.arguments) if part)


MODELS_LIST = Subcommand(("models", "list"), "List configured models.")
MODELS_CURRENT = Subcommand(("models", "current"), "Show the model selected for this conversation.")
MODELS_USE = Subcommand(
    ("models", "use"),
    "Select a model. Omit the name to open a menu.",
    "<name>",
)
MODELS_RESET = Subcommand(("models", "reset"), "Remove this conversation's temporary model selection.")


def resolve(text: str, subcommands: Mapping[tuple[str, ...], Subcommand], default: Subcommand) -> tuple[Subcommand | None, str]:
    """Match the longest command name and preserve the remaining text. An empty text names ``default``."""
    stripped = (text or "").strip()
    if not stripped:
        return default, ""
    parts = stripped.split(maxsplit=2)
    for count in (2, 1):
        sub = subcommands.get(tuple(part.lower() for part in parts[:count]))
        if sub is not None:
            return sub, stripped.split(maxsplit=count)[count] if len(parts) > count else ""
    return None, ""


def usage_text(sub: Subcommand, command: str) -> str:
    """Show usage for invalid arguments."""
    return f"Usage: `{usage(sub, command)}`"


async def run_models_command(
    sub: Subcommand, arguments: str, *, models: ConfiguredModels, scoped: str,
    overrides: MutableMapping[tuple[str, str], str], key: tuple[str, str],
    command: str, answer: Callable[..., Any], answer_lines: Callable[..., Any],
    picker_blocks: Callable[[], Any] | None, escape: Callable[[str], str],
) -> str:
    """Apply one model command using explicit selection and response callbacks.

    ``picker_blocks`` makes the menu a platform shows for ``models use`` with no name, or is ``None`` where it
    shows none: a missing name then gets the usage.
    """
    if not models.known:
        await answer("The configured model list is unavailable. Try again later.")
        return "refused:models-unavailable"
    scoped = scoped if models.accepts(scoped) else ""
    selected = overrides.get(key, "")
    if selected and not models.accepts(selected):
        overrides.pop(key, None)
        selected = ""
    if sub is MODELS_LIST:
        lines = ["Configured models:"]
        for name in models.names:
            aliases = ", ".join(
                escape(alias) for alias, target in models.alias_pairs if target == name
            )
            suffix = f" (aliases: {aliases})" if aliases else ""
            lines.append(f"• `{escape(name)}`{suffix}")
        await answer_lines(lines, answer)
        return "answered:models-list"
    if sub is MODELS_CURRENT:
        if scoped:
            name, source = scoped, "scope"
        elif selected:
            name, source = selected, "temporary selection"
        else:
            name, source = models.default, "configured default"
        await answer(f"Model for your new requests here: `{escape(name)}` ({source}).")
        return "answered:models-current"
    if sub is MODELS_RESET:
        overrides.pop(key, None)
        name = scoped or models.default
        source = "scope" if scoped else "configured default"
        await answer(
            "Temporary selection removed. "
            f"Model for your new requests here: `{escape(name)}` ({source})."
        )
        return "answered:models-reset"
    if scoped:
        await answer(
            "A scope selects your model here. Change the scope to use another model."
        )
        return "refused:models-scoped"
    name = arguments.strip()
    if not name and picker_blocks is None:
        await answer(usage_text(MODELS_USE, command))
        return "refused:models-name-required"
    if not name:
        assert picker_blocks is not None
        await answer(
            f"Choose a model below, or use `{usage(MODELS_USE, command)}`.",
            blocks=picker_blocks(),
        )
        return "answered:models-picker"
    if not models.accepts(name):
        await answer(
            f"Unknown model `{escape(name)}`. Use `{usage(MODELS_LIST, command)}`"
            " to see configured names and aliases."
        )
        return "refused:models-unknown"
    overrides[key] = name
    await answer(
        f"Selected `{escape(name)}` for new requests. "
        "This affects everyone in this conversation unless a scope selects "
        "their model. Running and queued requests keep their models. "
        "The configured default may return after a restart."
    )
    return "selected:models-use"


#: Text functions of a platform's markup: one escapes a name, one makes a text strong.
Markup = Callable[[str], str]
MAX_DESCRIBED_SKILLS = 20
MAX_NAMED_SKILLS = 100


def subagents_text(names: Iterable[str], *, escape: Markup, strong: Markup) -> str:
    """What ``subagents list`` answers."""
    listed = list(names)
    if not listed:
        return "No subagents are available in this conversation."
    lines = [f"{strong('Subagents available in this conversation')} ({len(listed)})"]
    lines.extend(f"• `{escape(name)}`" for name in listed)
    return "\n".join(lines)


def _skill_line(item: Mapping[str, Any], always: frozenset[str], *, describe: bool, escape: Markup) -> str:
    name = skill_name(item)
    line = f"`{escape(name)}`" + (" (always loaded here)" if name in always else "")
    summary = gloss(str(item.get("description") or "")) if describe else ""
    return line + (f": {escape(summary)}" if summary else "")


def skills_text(skills: list[Mapping[str, Any]], required: Iterable[str] = (), *, escape: Markup, strong: Markup) -> str:
    """Describe the first skills, then list the names of the rest."""
    if not skills:
        return "No skills are available in this conversation."
    always = frozenset(required)
    lines = [f"{strong('Skills available in this conversation')} ({len(skills)})"]
    lines.extend(f"• {_skill_line(item, always, describe=True, escape=escape)}" for item in skills[:MAX_DESCRIBED_SKILLS])
    rest = skills[MAX_DESCRIBED_SKILLS:]
    if rest:
        named = ", ".join(_skill_line(item, always, describe=False, escape=escape) for item in rest[:MAX_NAMED_SKILLS])
        more = len(rest) - MAX_NAMED_SKILLS
        lines.append(f"Also: {named}" + (f", and {more} more." if more > 0 else "."))
    return "\n".join(lines)


def experts_text(catalog: Iterable[Any], selected: str, *, escape: Markup, strong: Markup) -> str:
    """Show the installed expert packages and the expert that the scope selects."""
    experts: dict[str, tuple[str, bool]] = {}
    for item in catalog:
        if not isinstance(item, Mapping) or item.get("installed") is not True:
            continue
        name = str(item.get("packageName") or item.get("id") or "").strip()
        if name:
            description = item.get("displayDescription")
            if isinstance(description, Mapping):
                description = description.get("en") or description.get("zh") or ""
            summary = gloss(description if isinstance(description, str) else "")
            experts[name] = (summary, item.get("connection_state") == "connected")

    current = f"`{escape(selected)}`" if selected else "none"
    if selected and selected not in experts:
        current += " (not installed)"
    elif selected and not experts[selected][1]:
        current += " (connectors are not ready)"
    lines = [strong("Experts in this conversation"), f"• {strong('Selected by scope')}: {current}"]
    lines.append("The scope selects the expert. This command does not change the selected expert.")
    if not experts:
        lines.append(f"• {strong('Installed')}: none")
    else:
        lines.append(f"• {strong('Installed')} ({len(experts)}):")
        for name in sorted(experts, key=str.casefold):
            summary, ready = experts[name]
            state = "" if ready else " (connectors are not ready)"
            detail = f": {escape(summary)}" if summary else ""
            lines.append(f"  • `{escape(name)}`{state}{detail}")
    return "\n".join(lines)


#: For each list that a scope can narrow: the scope fields a backend applies it through, with their settings.
CATALOG_FILTERS: Mapping[str, tuple[tuple[str, str], ...]] = {
    CATALOG_SKILLS: (
        ("skills_available", "agent.skills.available"),
        ("skills_unavailable", "agent.skills.unavailable"),
    ),
    CATALOG_SUBAGENTS: (("subagents_available", "agent.subagents"),),
}


def narrowing_fields(scope: TurnScope) -> frozenset[str]:
    """The fields of ``scope`` that narrow a list: one that lists the available names, or a non-empty deny list."""
    named = {"skills_available": scope.skills_available, "subagents_available": scope.subagents_available}
    return frozenset(
        {field for field, names in named.items() if names is not None}
        | ({"skills_unavailable"} if scope.skills_unavailable else set())
    )


def catalog_caveat(kind: str, narrowed: Collection[str], capabilities: BackendCapabilities) -> str:
    """Say that the backend does not hold a turn to the list a scope wrote, where a scope wrote one and it does not.

    ``narrowed`` names the scope fields that narrowed the list.
    """
    settings = [
        f"`{setting}`" for field, setting in CATALOG_FILTERS[kind]
        if field in narrowed and field not in capabilities.scope_fields
    ]
    if not settings:
        return ""
    return (
        f"\n_This list follows {' and '.join(settings)}, which the {capabilities.backend} backend does not apply"
        f" to a turn: a turn here can still use one of the {kind} that are not listed._"
    )
