"""Parse Slack slash subcommands and format their private responses."""

import html
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent.commands import grammar as command_grammar
from belle_agent.commands.grammar import (
    MAX_DESCRIBED_SKILLS,
    MAX_NAMED_SKILLS,
    MODELS_CURRENT,
    MODELS_LIST,
    MODELS_RESET,
    MODELS_USE,
    Subcommand,
    usage,
    usage_text,
)
from belle_agent.scheduling.commands import SCHEDULE_SUBCOMMANDS
from belle_agent.scheduling.session_commands import SESSION_SUBCOMMANDS

HELP = Subcommand(("help",), "List the commands you can use in Slack.")
SKILLS_LIST = Subcommand(("skills", "list"), "List available skills.")
EXPERTS_LIST = Subcommand(
    ("experts", "list"),
    "List installed experts and the expert selected for this conversation.",
)
SUBAGENTS_LIST = Subcommand(("subagents", "list"), "List available subagents.")
SUBAGENTS_USE = Subcommand(
    ("subagents", "use"),
    "Give one task to a subagent. Omit the task to open a form.",
    "<name> <task>",
)
QUEUE_LIST = Subcommand(("queue", "list"), "List waiting messages.")
QUEUE_DROP = Subcommand(
    ("queue", "drop"),
    "Select a waiting message to remove, or give its link.",
    "[message link]",
)
ASK = Subcommand(
    ("ask",),
    "Ask privately. Omit the question to open a form. Answers arrive in your DM.",
    "<question>",
)

SKILLS_USE = Subcommand(
    ("skills", "use"),
    "Require one skill for one private task. Omit the task to open a form.",
    "<skill> <task>",
)
STATUS = Subcommand(
    ("status",),
    "Show agent status and running turns.",
)
STOP = Subcommand(
    ("stop",),
    "Select a running turn to stop, or give its message link.",
    "[message link]",
)

SUBCOMMANDS: tuple[Subcommand, ...] = (
    HELP,
    STATUS,
    SKILLS_LIST,
    SKILLS_USE,
    SUBAGENTS_LIST,
    SUBAGENTS_USE,
    EXPERTS_LIST,
    MODELS_LIST,
    MODELS_CURRENT,
    MODELS_USE,
    MODELS_RESET,
    QUEUE_LIST,
    QUEUE_DROP,
    ASK,
    STOP,
    *SCHEDULE_SUBCOMMANDS,
    *SESSION_SUBCOMMANDS,
)
_COMMAND_BY_WORDS = {sub.words: sub for sub in SUBCOMMANDS}



STOP_LINK_INVALID_NOTICE = "Use a message link from this Slack conversation."
STOP_NONE_NOTICE = "No running turn was found in this conversation."
STOP_AMBIGUOUS_NOTICE = "Several turns are running here. Give the message link of the one to stop."
STOP_ACCEPTED_NOTICE = "Stopping that turn."
STOP_REFUSED_NOTICE = "You are not permitted to stop that turn."

FAILED_NOTICE = "That command could not be run. Please try again."
ASK_NOT_POSTED_NOTICE = "Your question could not be posted, so nothing was asked. Please try again."
SKILLS_UNAVAILABLE_NOTICE = "The skill list could not be read. Please try again."


def foreign_workspace_notice(bot_name: str) -> str:
    return f"{bot_name} is not set up for this workspace."


def not_permitted_notice(bot_name: str) -> str:
    return f"You are not permitted to use {bot_name}."


def conversation_not_enabled_notice(bot_name: str) -> str:
    return f"{bot_name} is not enabled in this conversation."


def conversation_kind_unknown_notice(bot_name: str) -> str:
    return f"{bot_name} cannot verify this conversation's type yet. Send a message the bot can see here, then retry."


def ask_needs_dm_text(command: str, *, bot_name: str) -> str:
    return (
        f"`{usage(ASK, command)}` does not work in a direct message between"
        " people. Use it in a channel, or in your direct message with"
        f" {bot_name}."
    )


def ask_answering_in_dm_notice(bot_name: str) -> str:
    return f"Answering in your direct message with {bot_name}. Nothing is posted here."


def subagent_starting_in_dm_notice(bot_name: str) -> str:
    return f"Starting the task in your direct message with {bot_name}. The command posts nothing here."


def ask_not_dispatched_notice(bot_name: str) -> str:
    return f"Your question was posted but {bot_name} could not start on it. Please try again."


def experts_unavailable_notice(bot_name: str) -> str:
    return f"{bot_name} could not read the expert list. Please try again."


def subagents_unavailable_notice(bot_name: str) -> str:
    return f"{bot_name} could not read the subagent list. Try again."


def subagent_unknown_text(name: str, command: str) -> str:
    return (
        f"No subagent called `{escape(name)}` can be used here."
        f" `{usage(SUBAGENTS_LIST, command)}` shows the ones that can."
    )


def subagent_not_in_dm_text(name: str, *, bot_name: str) -> str:
    return (
        f"`{escape(name)}` is not allowed in your direct message with"
        f" {bot_name}, where the task would run. {bot_name} did not delegate it."
    )


def skill_unknown_text(name: str, command: str) -> str:
    """Explain a skill name that the current scope cannot use."""
    return (
        f"No skill called `{escape(name)}` can be used here. `{usage(SKILLS_LIST, command)}` shows the ones that can."
    )


def skill_not_in_dm_text(name: str, *, bot_name: str) -> str:
    """Explain a skill blocked by the destination DM scope."""
    return (
        f"`{escape(name)}` is not allowed in your direct message with"
        f" {bot_name}, where the answer would run, so nothing was asked."
    )


def resolve(text: str) -> tuple["Subcommand | None", str]:
    """Match the longest command name and preserve the remaining text."""
    return command_grammar.resolve(text, _COMMAND_BY_WORDS, HELP)


def help_text(command: str, *, bot_name: str, unavailable: Mapping[Subcommand, str] | None = None) -> str:
    """What ``help`` answers: every subcommand and one line on each.

    ``unavailable`` gives the reason a deployment cannot run a subcommand, which its line then states.
    """
    lines = [f"*{bot_name} commands in Slack*"]
    for sub in SUBCOMMANDS:
        reason = (unavailable or {}).get(sub)
        note = f" _Not available here: {escape(reason)}._" if reason else ""
        lines.append(f"• `{usage(sub, command)}`: {sub.summary}{note}")
    return "\n".join(lines)


def not_available_text(sub: Subcommand, command: str, reason: str) -> str:
    """Answer a subcommand a deployment cannot run, with the reason."""
    return f"`{usage(sub, command)}` is not available here: {escape(reason)}."


def unknown_text(command: str, *, bot_name: str) -> str:
    """Show help without echoing untrusted command text."""
    return f"That is not a `{command}` command.\n\n{help_text(command, bot_name=bot_name)}"


async def run_models_command(sub: Subcommand, arguments: str, **given: Any) -> str:
    """Apply one model command, with names escaped for Slack."""
    return await command_grammar.run_models_command(sub, arguments, escape=escape, **given)


def escape(text: str) -> str:
    """Escape Slack markup once, including text already HTML-escaped."""
    plain = html.unescape(text or "")
    return plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quoted(question: str) -> str:
    """*question* as an escaped mrkdwn blockquote, one line of it at minimum."""
    return "\n".join(f">{line}" for line in escape(question).splitlines() or [""])


def anchor_text(question: str, command: str) -> str:
    """Quote a private slash question in the bot's DM before its turn."""
    return f"You asked with `{command}`:\n{_quoted(question)}"


def command_label(command: str, skill: str = "", subagent: str = "") -> str:
    """Name the configured command, including turns started by a form."""
    if subagent:
        return f"{command} {SUBAGENTS_USE.name} {escape(subagent)}"
    if skill:
        return f"{command} {SKILLS_USE.name} {escape(skill)}"
    return f"{command} {ASK.name}"


#: Context chosen for a private question asked from a channel.
CONTEXT_THREAD = "thread"
CONTEXT_CHANNEL = "channel"
CONTEXT_NONE = "none"
CONTEXT_TASK = "task"
#: The deployment copies no conversation into a private question.
CONTEXT_FRESH = "fresh"


def side_anchor_text(
    question: str,
    channel_id: str,
    context: str,
    thread_link: str = "",
    *,
    command: str,
) -> str:
    """Quote a channel question in the DM and name its source context."""
    if context == CONTEXT_THREAD:
        where = f"<{thread_link}|its most recent thread>" if thread_link else ("its most recent thread")
        source = f"Answering from {where}."
    elif context == CONTEXT_CHANNEL:
        source = "Answering from the channel's conversation."
    elif context == CONTEXT_TASK:
        source = "The subagent receives only the task above."
    elif context == CONTEXT_FRESH:
        source = "This deployment does not copy a channel's conversation, so this answer starts without it."
    else:
        source = "No conversation of that channel's was found, so this answer starts without it."
    return (
        f"You asked in <#{channel_id}> with `{command}`:\n{_quoted(question)}\n"
        f"_{source} Reply in this thread to continue; the channel is not changed._"
    )


def side_question_text(question: str, channel_id: str, command: str) -> str:
    """Tell the DM turn where the question came from and where to answer."""
    return (
        f"[Asked privately from Slack channel <#{channel_id}> with {command}."
        " Your reply goes only to the person who asked, in their direct"
        " message, and not to the channel.]\n\n"
        f"{question}"
    )


@dataclass(frozen=True)
class StatusFacts:
    """Public status fields gathered by the connector."""

    agent_answered: bool
    agent_seconds: float = 0.0
    version: str = ""
    model: str = ""
    provider: str = ""
    model_override: str = ""
    chat_type: str = ""
    mid_turn: str = ""
    running: int = 0
    event_running: bool = False
    running_message_link: str = ""
    queued: int = 0
    pending_events: int = 0
    lost_events: int = 0
    held_calls: int = 0
    ask_context: str = ""
    ask_link: str = ""


_MID_TURN_WORDS = {
    "queue": "waits until the current turn ends",
    "cancel": "stops the current turn and starts a new one",
    "steer": "is added to the current turn",
}


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def status_text(facts: StatusFacts, command: str, *, bot_name: str) -> str:
    """What ``status`` answers with."""
    lines = [f"*{bot_name} status*"]

    if facts.agent_answered:
        agent = "running"
        if facts.version:
            agent += f", version {escape(facts.version)}"
        agent += f" (status check took {facts.agent_seconds:.1f} s)"
    else:
        agent = "not answering. Messages will wait or fail until it is back."
    lines.append(f"• *Agent*: {agent}")

    if facts.model_override:
        model = f"`{escape(facts.model_override)}`, set for this conversation"
    elif facts.model:
        model = f"`{escape(facts.model)}`"
        if facts.provider:
            model += f" via {escape(facts.provider)}"
    else:
        model = "unknown"
    lines.append(f"• *Model*: {model}")

    if facts.running or facts.queued or facts.pending_events:
        now = _count(facts.running, "turn", "turns") + " running"
        if facts.running == 1 and facts.running_message_link:
            now += f" (<{facts.running_message_link}|source message>)"
        if facts.queued:
            now += ", " + _count(facts.queued, "message", "messages") + " waiting"
        if facts.pending_events:
            now += ", " + _count(facts.pending_events, "event turn", "event turns") + " waiting"
    else:
        now = "nothing running"
    mid_turn = _MID_TURN_WORDS.get(facts.mid_turn)
    if facts.event_running:
        now += ". A message sent during an event turn waits until it ends"
    elif mid_turn:
        now += f". A message sent during a turn {mid_turn}"
    lines.append(f"• *Right now*: {now}.")
    if facts.held_calls:
        lines.append(
            f"• *Waiting for an answer*: {_count(facts.held_calls, 'tool call', 'tool calls')} held until a person agrees."
        )
    if facts.lost_events:
        lines.append(
            f"• *Event turns lost since restart*: {facts.lost_events}."
            " Check the service log for event IDs and manual recovery."
        )

    if facts.chat_type != "im":
        if facts.ask_context == CONTEXT_THREAD:
            source = f"<{facts.ask_link}|the most recent thread>" if facts.ask_link else "the most recent thread"
        elif facts.ask_context == CONTEXT_CHANNEL:
            source = "the channel's conversation"
        elif facts.ask_context == CONTEXT_FRESH:
            source = "a new conversation, without this channel's history"
        else:
            source = "no conversation yet, so it would start without context"
        lines.append(f"• *`{command} {ASK.name}` here would use*: {source}")
    return "\n".join(lines)


def _strong(text: str) -> str:
    return f"*{text}*"


def subagents_text(names: Iterable[str]) -> str:
    """What ``subagents list`` answers."""
    return command_grammar.subagents_text(names, escape=escape, strong=_strong)


def skills_text(skills: list[Mapping[str, Any]], required: Iterable[str] = ()) -> str:
    """Describe initial skills, then list the remaining names."""
    return command_grammar.skills_text(skills, required, escape=escape, strong=_strong)


def experts_text(catalog: Iterable[Any], selected: str) -> str:
    """Show installed expert packages and the scope's current selection."""
    return command_grammar.experts_text(catalog, selected, escape=escape, strong=_strong)


__all__ = [
    "ASK",
    "ASK_NOT_POSTED_NOTICE",
    "CONTEXT_CHANNEL",
    "CONTEXT_FRESH",
    "CONTEXT_NONE",
    "CONTEXT_TASK",
    "CONTEXT_THREAD",
    "EXPERTS_LIST",
    "FAILED_NOTICE",
    "HELP",
    "MAX_DESCRIBED_SKILLS",
    "MAX_NAMED_SKILLS",
    "MODELS_CURRENT",
    "MODELS_LIST",
    "MODELS_RESET",
    "MODELS_USE",
    "SKILLS_LIST",
    "SKILLS_UNAVAILABLE_NOTICE",
    "SKILLS_USE",
    "STATUS",
    "SUBAGENTS_LIST",
    "SUBAGENTS_USE",
    "SUBCOMMANDS",
    "StatusFacts",
    "Subcommand",
    "anchor_text",
    "ask_answering_in_dm_notice",
    "ask_needs_dm_text",
    "ask_not_dispatched_notice",
    "command_label",
    "conversation_kind_unknown_notice",
    "conversation_not_enabled_notice",
    "escape",
    "experts_text",
    "experts_unavailable_notice",
    "foreign_workspace_notice",
    "help_text",
    "not_available_text",
    "not_permitted_notice",
    "resolve",
    "run_models_command",
    "side_anchor_text",
    "side_question_text",
    "skill_not_in_dm_text",
    "skill_unknown_text",
    "skills_text",
    "status_text",
    "subagent_not_in_dm_text",
    "subagent_starting_in_dm_notice",
    "subagent_unknown_text",
    "subagents_text",
    "subagents_unavailable_notice",
    "unknown_text",
    "usage",
    "usage_text",
]
