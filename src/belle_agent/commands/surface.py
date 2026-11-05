"""The answers of a platform's text command, from the host's command operations, the same on each platform.

A platform parses where a command comes from and who sent it, builds the
``InboundMessage`` that names that place and person, and hands the text after
the command word to ``CommandAnswers.answer``. Each answer is Markdown, with
what became of the command for the log. The platform delivers the answer its
own way. A private question is the platform's own act: ``ask_privately``.
"""

import time
from collections.abc import Callable
from typing import Any

from belle_agent import capability_catalog
from belle_agent.backend import CATALOG_SKILLS, CATALOG_SUBAGENTS
from belle_agent.click_notices import stop_notice
from belle_agent.commands.grammar import (
    Subcommand,
    catalog_caveat,
    experts_text,
    narrowing_fields,
    resolve,
    run_models_command,
    skills_text,
    subagents_text,
    usage,
    usage_text,
)
from belle_agent.models import ConfiguredModels
from belle_agent.platforms.base import InboundDrop, InboundMessage, InboundStop, OnInbound
from belle_agent.scheduling.commands import schedule_command
from belle_agent.scheduling.session_commands import session_command

#: The host's command operations for one platform: see ``belle_agent.host.commands``.
Host = Any

HELP = ("help",)
STATUS = ("status",)
STOP = ("stop",)
QUEUE_LIST = ("queue", "list")
QUEUE_DROP = ("queue", "drop")
SKILLS_LIST = ("skills", "list")
SKILLS_USE = ("skills", "use")
SUBAGENTS_LIST = ("subagents", "list")
SUBAGENTS_USE = ("subagents", "use")
EXPERTS_LIST = ("experts", "list")
ASK = ("ask",)

FAILED = "That command could not be run. Please try again."
CATALOG_UNREAD = "Belle could not read the list of {kind}. Please try again."


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _strong(text: str) -> str:
    return f"**{text}**"


class CommandAnswers:
    """Answer the subcommands that read and change the host. A platform gives its words and its private question."""

    #: The platform's name, as its records carry it.
    platform = ""
    #: What a place of the platform is called in an answer, such as ``channel`` or ``room``.
    place = "conversation"

    def __init__(self, subcommands: tuple[Subcommand, ...]) -> None:
        self.subcommands = subcommands
        self._by_words = {sub.words: sub for sub in subcommands}

    def message_id(self, text: str) -> str:
        """The id of a message that ``text`` names, alone or in a link, or empty."""
        raise NotImplementedError

    def name_of(self, user: str) -> str:
        """How an answer names a person."""
        return user

    async def ask_privately(
        self, at: InboundMessage, question: str, label: str, *, skill: str = "", subagent: str = ""
    ) -> tuple[str, str]:
        """Start a turn on ``question`` that only the person who asked reads. Return the answer and the outcome."""
        raise NotImplementedError

    async def answer(self, host: Host, on_inbound: OnInbound, at: InboundMessage, text: str, command: str) -> tuple[str, str]:
        """Answer the command ``text``, typed after ``command`` where ``at`` names. Return the answer and the outcome."""
        sub, arguments = resolve(text, self._by_words, self._by_words[HELP])
        if sub is None:
            return f"That is not a `{command}` command.\n\n{self.help(host, command)}", "refused:unknown"
        if reason := host.command_unavailable(sub.words):
            return f"`{usage(sub, command)}` is not available here: {reason}.", "refused:unavailable"
        words = sub.words
        if words == HELP:
            return self.help(host, command), "answered:help"
        if words == STATUS:
            return await self._status(host, at), "answered:status"
        if words == STOP:
            return await self._stop(host, on_inbound, at, arguments)
        if words == QUEUE_LIST:
            return self._queue(host, at, command), "answered:queue-list"
        if words == QUEUE_DROP:
            return await self._drop(on_inbound, at, arguments, command)
        if words in (SKILLS_LIST, SUBAGENTS_LIST, EXPERTS_LIST):
            return await self._list(host, at, words)
        if words in (SKILLS_USE, SUBAGENTS_USE):
            return await self._use(host, at, sub, arguments, command)
        if words == ASK:
            if not arguments.strip():
                return usage_text(sub, command), "refused:no-question"
            return await self.ask_privately(at, arguments.strip(), f"{command} {sub.name}")
        if words[0] == "schedule":
            return await schedule_command(host.schedule_toolkit, at, sub, arguments, command)
        if words[0] in {"bg", "sessions"}:
            return await session_command(host.session_toolkit, at, sub, arguments, command)
        return await self._models(host, at, sub, arguments, command)

    def help(self, host: Host, command: str) -> str:
        """Each subcommand with what it does, and why it is not available here where it is not."""
        lines = ["**Belle commands**"]
        for sub in self.subcommands:
            reason = host.command_unavailable(sub.words)
            note = f" _Not available here: {reason}._" if reason else ""
            lines.append(f"- `{usage(sub, command)}`: {sub.summary}{note}")
        return "\n".join(lines)

    @staticmethod
    async def _status(host: Host, at: InboundMessage) -> str:
        capabilities = host.backend_capabilities
        started = time.monotonic()
        catalog = await host.catalog()
        if catalog is not None:
            version = " ".join(part for part in (capabilities.backend, capabilities.version) if part)
            agent = f"running, {version} (status check took {time.monotonic() - started:.1f} s)"
        else:
            agent = "not answering. Messages will wait or fail until it is back."
        model = host.settled(at).model or (catalog.models.default if catalog is not None else "")
        running = len(host.running(at.workspace, at.conversation))
        waiting = len(host.waiting(at.workspace, at.conversation))
        now = f"{_count(running, 'turn', 'turns')} running, {_count(waiting, 'message', 'messages')} waiting"
        lines = ["**Belle status**", f"- **Agent**: {agent}", f"- **Model**: `{model or 'unknown'}`", f"- **Right now**: {now}."]
        if held := host.held(at.workspace, at.conversation):
            lines.append(f"- **Waiting for an answer**: {_count(held, 'tool call', 'tool calls')} held until a person agrees.")
        return "\n".join(lines)

    async def _stop(self, host: Host, on_inbound: OnInbound, at: InboundMessage, arguments: str) -> tuple[str, str]:
        running = host.running(at.workspace, at.conversation)
        wanted = self.message_id(arguments)
        if arguments.strip() and not wanted:
            return "Give the id of the message whose turn to stop, or its link.", "refused:no-message-id"
        turns = [turn for turn in running if not wanted or turn.inbound.message_id == wanted]
        if not turns:
            return f"No running turn was found in this {self.place}.", "ignored:no-turn-running"
        if len(turns) > 1:
            return "Several turns run here. Give the id or the link of the message whose turn to stop.", "refused:ambiguous"
        outcome = await on_inbound(
            InboundStop(platform=self.platform, workspace=at.workspace, conversation=at.conversation,
                        message_id=turns[0].inbound.message_id, sender=at.sender)
        )
        return stop_notice(outcome)[0], outcome

    def _queue(self, host: Host, at: InboundMessage, command: str) -> str:
        waiting = host.waiting(at.workspace, at.conversation)
        if not waiting:
            return f"No message waits for a turn in this {self.place}."
        lines = [f"**Waiting messages** ({len(waiting)})"]
        for turn in waiting:
            preview = " ".join(turn.inbound.text.split())[:80]
            lines.append(f"- `{turn.inbound.message_id}` by {self.name_of(turn.inbound.sender)}: {preview}")
        lines.append(f"Remove one with `{usage(self._by_words[QUEUE_DROP], command)}`.")
        return "\n".join(lines)

    async def _drop(self, on_inbound: OnInbound, at: InboundMessage, arguments: str, command: str) -> tuple[str, str]:
        wanted = self.message_id(arguments)
        if not wanted:
            return f"Usage: `{usage(self._by_words[QUEUE_DROP], command)}`, with a message id or link.", "refused:no-message-id"
        outcome = await on_inbound(
            InboundDrop(platform=self.platform, workspace=at.workspace, conversation=at.conversation, message_id=wanted,
                        sender=at.sender)
        )
        if outcome.startswith("removed"):
            return "Removed that waiting message.", outcome
        if outcome.startswith("refused"):
            return "You are not permitted to remove that message.", outcome
        return f"No message with that id waits for a turn in this {self.place}.", outcome

    @staticmethod
    async def _models(host: Host, at: InboundMessage, sub: Subcommand, arguments: str, command: str) -> tuple[str, str]:
        said: list[str] = []

        async def answer(text: str, **_: Any) -> None:
            said.append(text)

        async def answer_lines(lines: list[str], reply: Callable[..., Any]) -> None:
            await reply("\n".join(lines))

        catalog = await host.catalog()
        outcome = await run_models_command(
            sub, arguments,
            models=catalog.models if catalog is not None else ConfiguredModels(),
            scoped=host.scoped_model(at), overrides=host.model_choices, key=(at.workspace, at.conversation),
            command=command, answer=answer, answer_lines=answer_lines, picker_blocks=None, escape=str,
        )
        return "\n".join(said), outcome

    # ── the catalogs ─────────────────────────────────────────────────────

    @staticmethod
    async def _list(host: Host, at: InboundMessage, words: tuple[str, ...]) -> tuple[str, str]:
        kind = words[0]
        catalog = await host.catalog()
        if catalog is None:
            return CATALOG_UNREAD.format(kind=kind), f"failed:{kind}-list-unavailable"
        scope = host.settled(at)
        if words == EXPERTS_LIST:
            installed = [{"id": name, "installed": True, "connection_state": "connected"} for name in catalog.experts]
            return experts_text(installed, scope.expert, escape=str, strong=_strong), "answered:experts-list"
        if words == SKILLS_LIST:
            skills = capability_catalog.usable_skills(catalog.skills, scope.skills_available, scope.skills_unavailable)
            text = skills_text(skills, scope.skills_required, escape=str, strong=_strong)
        else:
            names = capability_catalog.usable_subagents(catalog.subagents, scope.subagents_available)
            text = subagents_text(names, escape=str, strong=_strong)
        caveat = catalog_caveat(kind, narrowing_fields(scope), host.backend_capabilities)
        return text + caveat, f"answered:{kind}-list"

    async def _use(self, host: Host, at: InboundMessage, sub: Subcommand, arguments: str, command: str) -> tuple[str, str]:
        """Ask privately, with a skill loaded or a subagent given the task, where the scope lets the person use it."""
        is_skill = sub.words == SKILLS_USE
        kind = CATALOG_SKILLS if is_skill else CATALOG_SUBAGENTS
        parts = arguments.strip().split(maxsplit=1)
        if len(parts) < 2:
            return usage_text(sub, command), f"refused:{kind}-use-usage"
        name, task = parts
        catalog = await host.catalog()
        if catalog is None:
            return CATALOG_UNREAD.format(kind=kind), f"failed:{kind}-list-unavailable"
        scope = host.settled(at)
        if is_skill:
            found = capability_catalog.find_skill(capability_catalog.usable_skills(
                catalog.skills, scope.skills_available, scope.skills_unavailable), name)
        else:
            found = capability_catalog.find_name(capability_catalog.usable_subagents(catalog.subagents, scope.subagents_available), name)
        if not found:
            listing = usage(self._by_words[SKILLS_LIST if is_skill else SUBAGENTS_LIST], command)
            return f"No {kind[:-1]} called `{name}` can be used here. `{listing}` shows the ones that can.", (
                f"refused:{kind[:-1]}-not-usable-here"
            )
        label = f"{command} {sub.name} {found}"
        answer, outcome = await self.ask_privately(at, task, label, skill=found if is_skill else "", subagent="" if is_skill else found)
        return answer, f"{outcome} {kind[:-1]}={found}"


def refused_in_direct(host: Host, at: InboundMessage, skill: str, subagent: str, where: str) -> str:
    """Why the scope of the direct conversation ``at``, where a private turn runs, does not let it use the skill or the subagent."""
    scope = host.settled(at)
    if skill and not capability_catalog.scope_allows(skill, scope.skills_available, scope.skills_unavailable):
        name = skill
    elif subagent and scope.subagents_available is not None and subagent not in scope.subagents_available:
        name = subagent
    else:
        return ""
    return f"`{name}` is not allowed in {where}, where the task would run. Nothing was asked."
