"""The slash command on Discord: ``/belle``, with one text option, answered for the person who typed it alone.

Belle registers the command globally at start, for every guild and the bot's direct messages.
Registering creates the command or replaces the one of
the same name; the application's other commands are left as they are. The command arrives as an
``INTERACTION_CREATE`` over the gateway and is acknowledged at once as a deferred answer only its sender sees,
since Discord wants an answer within 3 seconds; the answer replaces it when the host has it. The text is read
by the shared command grammar unchanged. A stop and a removal are handed to the host as records, so the host
decides who may by the rule of the Stop button.

A private question (``ask``, ``skills use``, ``subagents use``) is posted in the bot's direct message with the
person who asked, and handed to the host as a direct message there.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol

from belle_agent.commands.grammar import MODELS_CURRENT, MODELS_LIST, MODELS_RESET, Subcommand
from belle_agent.commands.surface import FAILED, CommandAnswers, refused_in_direct
from belle_agent.platforms.base import TRIGGER_DM, TRIGGER_NONE, InboundMessage, OnInbound, envelope
from belle_agent.platforms.discord.affordances import MESSAGE_LIMIT
from belle_agent.platforms.discord.capabilities import CHAT_TYPE_DM, NAME
from belle_agent.platforms.discord.client import DiscordClient, DiscordError
from belle_agent.platforms.discord.questions import EPHEMERAL, INTERACTION_COMMAND, _person
from belle_agent.platforms.discord.render import RENDERER
from belle_agent.scheduling.commands import SCHEDULE_SUBCOMMANDS
from belle_agent.scheduling.session_commands import SESSION_SUBCOMMANDS

logger = logging.getLogger(__name__)

COMMAND_NAME = "belle"
COMMAND = f"/{COMMAND_NAME}"
OPTION_NAME = "text"
#: The callback type of a deferred answer, which Discord shows as "thinking" until it is edited.
DEFERRED_ANSWER = 5
#: Where the command may be used: a guild, and the bot's direct messages (Discord's interaction contexts).
CONTEXTS = (0, 1)

HELP = Subcommand(("help",), "List the commands you can use here.")
STATUS = Subcommand(("status",), "Show whether Belle answers, its model, and what runs and waits here.")
STOP = Subcommand(("stop",), "Stop the turn that runs here. With several, give the message id or link.", "[message]")
QUEUE_LIST = Subcommand(("queue", "list"), "List the messages that wait for a turn here.")
QUEUE_DROP = Subcommand(("queue", "drop"), "Remove a waiting message.", "<message>")
MODELS_USE = Subcommand(("models", "use"), "Select a model for new requests in this channel.", "<name>")
SKILLS_LIST = Subcommand(("skills", "list"), "List the skills that can be used here.")
SKILLS_USE = Subcommand(("skills", "use"), "Ask privately, with one skill loaded for that one task.", "<skill> <task>")
SUBAGENTS_LIST = Subcommand(("subagents", "list"), "List the subagents that can be used here.")
SUBAGENTS_USE = Subcommand(("subagents", "use"), "Give one task to a subagent, privately.", "<name> <task>")
EXPERTS_LIST = Subcommand(("experts", "list"), "List the installed experts and the one that the scope selects here.")
ASK = Subcommand(("ask",), "Ask privately. The answer comes in your direct message with Belle.", "<question>")
SUBCOMMANDS = (
    HELP, STATUS, SKILLS_LIST, SKILLS_USE, SUBAGENTS_LIST, SUBAGENTS_USE, EXPERTS_LIST,
    MODELS_LIST, MODELS_CURRENT, MODELS_USE, MODELS_RESET, QUEUE_LIST, QUEUE_DROP, ASK, STOP,
    *SCHEDULE_SUBCOMMANDS,
    *SESSION_SUBCOMMANDS,
)
#: The command as Discord registers it. Discord limits each description to 100 characters.
DEFINITION: Mapping[str, Any] = {
    "name": COMMAND_NAME,
    "type": 1,
    "description": "Run a Belle command. Leave the text empty, or write help, to list them.",
    "contexts": list(CONTEXTS),
    "options": [{
        "type": 3, "name": OPTION_NAME, "required": False,
        "description": "The command and its arguments, such as status, models list or stop",
    }],
}
#: A message id, alone or at the end of a message link.
_MESSAGE = re.compile(r"(?:^|/)([0-9]{15,25})/?$")

NOT_PERMITTED = "You are not permitted to use Belle."
NOT_SERVED = "Belle does not take part in this channel."
NOT_ASKED = "Your question could not be posted, so nothing was asked. Please try again."
NOT_STARTED = "Your question was posted, but Belle could not start on it. Please try again."
#: Mentions in a command's answer name people and notify nobody.
NO_MENTIONS: Mapping[str, Any] = {"parse": []}

#: The host's command operations for this platform: see ``belle_agent.host.commands``.
Host = Any


class Connector(Protocol):
    """What a command reads of its connector, once the connector is started and bound."""

    @property
    def host(self) -> Host:
        """The host's command operations."""

    @property
    def receiver(self) -> OnInbound | None:
        """Where a record goes."""

    def workspace(self, guild: str) -> str:
        """Qualify a raw guild or direct marker for this bot."""

    def learn_channel(self, channel: Mapping[str, Any]) -> None:
        """Keep what Discord said of a channel."""


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


class Commands(CommandAnswers):
    """Register ``/belle`` and answer it from the conversation host."""

    platform = NAME
    place = "channel"

    def __init__(
        self,
        client: DiscordClient,
        connector: Connector,
        application_id: Callable[[], str],
        conversation_of: Callable[[str], Awaitable[tuple[str, str]]],
    ) -> None:
        super().__init__(SUBCOMMANDS)
        self._client = client
        self._connector = connector
        self._application_id = application_id
        self._conversation_of = conversation_of

    async def register(self) -> str:
        """Create or replace the global ``/belle`` command. Return what became of it.

        A refusal is logged and the platform runs on without the command: questions and messages do not need it.
        """
        application = self._application_id()
        path = f"/applications/{application}/commands"
        if not application:
            logger.warning("discord /%s not registered: READY named no application", COMMAND_NAME)
            return "failed:no-application"
        try:
            await self._client.request("POST", path, json=DEFINITION)
        except DiscordError as exc:
            hint = " (invite the bot with the applications.commands scope)" if exc.code == "forbidden" else ""
            logger.warning("discord /%s not registered globally: %s%s", COMMAND_NAME, exc, hint)
            return f"failed:{exc.code}"
        logger.info("discord /%s registered globally", COMMAND_NAME)
        return "registered"

    def owns(self, interaction: Mapping[str, Any]) -> bool:
        """Whether an interaction is ``/belle``."""
        data = interaction.get("data")
        return interaction.get("type") == INTERACTION_COMMAND and isinstance(data, Mapping) and data.get("name") == COMMAND_NAME

    async def on_interaction(self, interaction: Mapping[str, Any]) -> str:
        """Acknowledge one command, answer it for its sender alone, and log what became of it."""
        token = str(interaction.get("token") or "")
        try:
            await self._client.respond(str(interaction.get("id") or ""), token, {"type": DEFERRED_ANSWER, "data": {"flags": EPHEMERAL}})
        except DiscordError as exc:
            logger.warning("discord command not acknowledged: %s", exc)
            return f"dropped:not-acknowledged ({exc.code})"
        text = _text(interaction)
        try:
            answer, outcome = await self._command(interaction, text)
        except Exception:  # A command that fails is told so, and the reader stays up.
            logger.exception("discord command failed: %s", COMMAND)
            answer, outcome = FAILED, "failed"
        await self._answer(token, answer)
        logger.info(
            "discord command: %s %s channel=%s user=%s outcome=%s",
            COMMAND, (text.split() or ["-"])[0], interaction.get("channel_id") or "-", _person(interaction)[0] or "-", outcome,
        )
        return outcome

    async def _command(self, interaction: Mapping[str, Any], text: str) -> tuple[str, str]:
        user = _person(interaction)[0]
        channel = str(interaction.get("channel_id") or "")
        host, on_inbound = self._connector.host, self._connector.receiver
        if not user or not channel:
            return FAILED, "dropped:no-user-or-channel"
        if host is None or on_inbound is None:
            return FAILED, "dropped:platform-not-started"
        if not host.admits(user):
            return NOT_PERMITTED, "refused:allow_from"
        conversation, chat_type = await self._conversation_of(channel)
        direct = chat_type == CHAT_TYPE_DM or not interaction.get("guild_id")
        if not direct and not host.serves(conversation):
            return NOT_SERVED, "refused:channel-not-served"
        at = InboundMessage(
            platform=NAME, workspace=self._connector.workspace("direct" if direct else str(interaction.get("guild_id") or "")),
            conversation=conversation, message_id="", sender=user, text="", chat_type=chat_type or CHAT_TYPE_DM,
            event_id=str(interaction.get("id") or ""), trigger=TRIGGER_DM if direct else TRIGGER_NONE,
        )
        return await self.answer(host, on_inbound, at, text, COMMAND)

    async def _answer(self, token: str, text: str) -> None:
        """Replace the deferred answer with ``text``; what does not fit one message follows, for the same person."""
        parts = [part.markdown for part in RENDERER.parts(text, MESSAGE_LIMIT)] or [FAILED]
        try:
            await self._client.edit_response(self._application_id(), token, {"content": parts[0], "allowed_mentions": NO_MENTIONS})
            for part in parts[1:]:
                await self._client.followup(self._application_id(), token,
                                            {"content": part, "flags": EPHEMERAL, "allowed_mentions": NO_MENTIONS})
        except DiscordError as exc:
            logger.warning("discord command answer not shown: %s", exc)

    def message_id(self, text: str) -> str:
        """A message id, alone or at the end of a message link, or empty."""
        found = _MESSAGE.search(text.strip())
        return found[1] if found else ""

    def name_of(self, user: str) -> str:
        """A person as a mention, which notifies nobody in a command's answer."""
        return f"<@{user}>"

    # ── a private question ───────────────────────────────────────────────

    async def ask_privately(
        self, at: InboundMessage, question: str, label: str, *, skill: str = "", subagent: str = ""
    ) -> tuple[str, str]:
        """Post the question in the bot's direct message with the person, and hand that message to the host as theirs."""
        host, on_inbound = self._connector.host, self._connector.receiver
        assert on_inbound is not None, "a command runs only while the connector runs"
        try:
            opened = await self._client.mapping("POST", "/users/@me/channels", json={"recipient_id": at.sender})
            self._connector.learn_channel(opened)
            direct = str(opened.get("id") or "")
            here = direct == at.conversation
            if here:
                anchor = f"You asked with `{label}`:\n{_quoted(question)}"
            else:
                start = (
                    "The subagent receives only the task above." if subagent
                    else "No conversation of that channel is copied, so this answer starts without it."
                )
                anchor = f"You asked in <#{at.conversation}> with `{label}`:\n{_quoted(question)}\n_{start} Nothing is posted there._"
            anchor_id = ""
            for part in RENDERER.parts(anchor, MESSAGE_LIMIT):
                posted = await self._client.mapping(
                    "POST", f"/channels/{direct}/messages", json={"content": part.markdown, "allowed_mentions": NO_MENTIONS}
                )
                anchor_id = anchor_id or str(posted.get("id") or "")
        except DiscordError as exc:
            logger.warning("discord %s could not post the question: %s", label, exc)
            return NOT_ASKED, f"failed:anchor-not-posted ({exc.code})"
        if not direct or not anchor_id:
            return NOT_ASKED, "failed:anchor-not-posted"
        at_direct = replace(at, workspace=self._connector.workspace("direct"), conversation=direct, chat_type=CHAT_TYPE_DM, trigger=TRIGGER_DM)
        if refusal := refused_in_direct(host, at_direct, skill, subagent, "your direct message with Belle"):
            return refusal, "refused:not-allowed-in-the-direct-message"
        text = question if here else (
            f"[Asked privately from Discord channel {at.conversation} with {label}. Your reply goes only to the person"
            f" who asked, in their direct message, and not to the channel.]\n\n{question}"
        )
        outcome = await on_inbound(
            replace(
                at_direct, message_id=anchor_id, event_id=f"{NAME}-ask-{anchor_id}", text=text,
                thread="", reply_thread="", envelope=envelope(direct, anchor_id, ""),
                skills_required=(skill,) if skill else (), subagent_required=(subagent, question) if subagent else None,
            )
        )
        if not outcome.startswith(("dispatched:", "queued:", "steered:")):
            return NOT_STARTED, f"failed:{outcome}"
        told = "Answering below." if here else "Answering in your direct message with Belle. Nothing is posted here."
        return told, f"asked:{outcome}"


def _text(interaction: Mapping[str, Any]) -> str:
    """The value of the command's text option, or empty."""
    data = interaction.get("data")
    options = data.get("options") if isinstance(data, Mapping) else None
    for option in options if isinstance(options, list) else ():
        if isinstance(option, Mapping) and option.get("name") == OPTION_NAME:
            return str(option.get("value") or "")
    return ""
