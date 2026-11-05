"""The slash command on Mattermost: a custom command that the server posts to Belle's listener.

The server sends the command's token with each request, in the header
``Authorization: Token <token>``. A request whose header does not hold the
token the operator set is refused. Every answer goes back in
the request itself, ephemeral, to the person who typed the command: the
server shows it to that person only, with no permission of the bot's. A stop
and a removal are handed to the host as records, so the host decides who may
by the rule of the stop button and of every other platform.

A private question (``ask``, ``skills use``, ``subagents use``) is posted in
the bot's direct channel with the person who asked, and handed to the host
as a direct message there. Asked from another channel, it opens a
conversation of its own in the thread of that post.
"""

import hmac
import logging
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from belle_agent.commands.grammar import MODELS_CURRENT, MODELS_LIST, MODELS_RESET, Subcommand
from belle_agent.commands.surface import FAILED, CommandAnswers, refused_in_direct
from belle_agent.platforms.base import TRIGGER_DM, TRIGGER_NONE, InboundMessage, OnInbound
from belle_agent.platforms.mattermost.capabilities import CHAT_TYPE_DIRECT, CHAT_TYPE_GROUP, NAME
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError, created_post_id
from belle_agent.platforms.mattermost.directory import Directory
from belle_agent.platforms.mattermost.settings import COMMAND_TOKEN_ENV
from belle_agent.scheduling.commands import SCHEDULE_SUBCOMMANDS
from belle_agent.scheduling.session_commands import SESSION_SUBCOMMANDS

logger = logging.getLogger(__name__)

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
#: A post id, alone or at the end of a permalink.
_MESSAGE = re.compile(r"(?:^|/)([a-z0-9]{26})/?$")

TOKEN_REFUSED = "This command was not sent with the token that this Belle was given."
TOKEN_UNSET = f"This Belle takes no command: {COMMAND_TOKEN_ENV} is not set."
NOT_PERMITTED = "You are not permitted to use Belle."
NOT_SERVED = "Belle does not take part in this channel."
NOT_ASKED = "Your question could not be posted, so nothing was asked. Please try again."
NOT_STARTED = "Your question was posted, but Belle could not start on it. Please try again."
#: Private questions asked from another channel, each the thread of a post in a direct channel. The oldest goes first.
MAX_SIDE_CONVERSATIONS = 512

#: The host's command operations for this platform: see ``belle_agent.host.commands``.
Host = Any


class Connector(Protocol):
    """What a command reads of its connector, once the connector is started and bound."""

    bot_user_id: str
    bot_username: str

    def workspace(self, chat_type: str, team_id: str) -> str:
        """The workspace of a channel."""

    @property
    def host(self) -> Host:
        """The host's command operations."""

    @property
    def receiver(self) -> OnInbound | None:
        """Where a record goes."""


def _answer(text: str) -> dict[str, Any]:
    return {"response_type": "ephemeral", "text": text}


def _message_id(text: str) -> str:
    found = _MESSAGE.search(text.strip())
    return found[1] if found else ""


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def _channel_name(record: Mapping[str, Any], channel: str) -> str:
    return f"~{record['name']}" if record.get("name") else channel


class Commands(CommandAnswers):
    """Answer the slash command of one connector from the conversation host."""

    platform = NAME
    place = "channel"

    def __init__(self, client: MattermostClient, directory: Directory, token: str, connector: "Connector") -> None:
        super().__init__(SUBCOMMANDS)
        self._client = client
        self._directory = directory
        self._token = token
        self._connector = connector
        #: Each private question asked from another channel: its direct channel and the post that roots it.
        self._sides: OrderedDict[tuple[str, str], None] = OrderedDict()

    def side(self, channel: str, root: str) -> str:
        """The private conversation a post in the thread ``root`` of a direct channel belongs to, or empty."""
        return root if root and (channel, root) in self._sides else ""

    async def on_command(self, request: Mapping[str, Any], authorization: str) -> dict[str, Any]:
        """Answer one command, sent with the ``Authorization`` header given, as the server shows it. Log what became of it."""
        try:
            answer, outcome = await self._command(request, authorization)
        except Exception:  # A command that fails is told so, and the listener stays up.
            logger.exception("mattermost command failed: %s", request.get("command") or "-")
            answer, outcome = _answer(FAILED), "failed"
        logger.info(
            "mattermost command: %s %s channel=%s user=%s outcome=%s",
            request.get("command") or "-", (str(request.get("text") or "").split() or ["-"])[0],
            request.get("channel_id") or "-", request.get("user_id") or "-", outcome,
        )
        return answer

    async def _command(self, request: Mapping[str, Any], authorization: str) -> tuple[dict[str, Any], str]:
        if not self._token:
            return _answer(TOKEN_UNSET), "refused:no-command-token"
        if not hmac.compare_digest(authorization.encode(), f"Token {self._token}".encode()):
            return _answer(TOKEN_REFUSED), "refused:token"
        user = str(request.get("user_id") or "")
        channel = str(request.get("channel_id") or "")
        host, on_inbound = self._connector.host, self._connector.receiver
        if not user or not channel:
            return _answer(FAILED), "dropped:no-user-or-channel"
        if host is None or on_inbound is None:
            return _answer(FAILED), "dropped:platform-not-started"
        if not host.admits(user):
            return _answer(NOT_PERMITTED), "refused:allow_from"
        try:
            found = await self._directory.channel(channel)
        except MattermostError as exc:
            logger.warning("mattermost command channel %s not read: %s", channel, exc)
            return _answer(FAILED), "dropped:channel-not-read"
        chat_type = str(found.get("type") or "")
        direct = chat_type in (CHAT_TYPE_DIRECT, CHAT_TYPE_GROUP)
        if not direct and not host.serves(channel):
            return _answer(NOT_SERVED), "refused:channel-not-served"
        at = InboundMessage(
            platform=NAME, workspace=self._connector.workspace(chat_type, str(found.get("team_id") or "")),
            conversation=channel, message_id="", sender=user, text="", chat_type=chat_type,
            event_id=str(request.get("trigger_id") or ""),
            trigger=TRIGGER_DM if chat_type == CHAT_TYPE_DIRECT else TRIGGER_NONE,
        )
        command = str(request.get("command") or "/belle")
        text, outcome = await self.answer(host, on_inbound, at, str(request.get("text") or ""), command)
        return _answer(text), outcome

    def message_id(self, text: str) -> str:
        """A post id, alone or at the end of a permalink, or empty."""
        return _message_id(text)

    def name_of(self, user: str) -> str:
        """A person as ``@name`` where the directory knows it, else by id."""
        return f"@{name}" if (name := self._directory.known_name(user)) else user

    # ── a private question ───────────────────────────────────────────────

    async def ask_privately(
        self, at: InboundMessage, question: str, label: str, *, skill: str = "", subagent: str = ""
    ) -> tuple[str, str]:
        """Post the question in the bot's direct channel with the person, and hand that post to the host as theirs."""
        host, on_inbound = self._connector.host, self._connector.receiver
        assert on_inbound is not None, "a command runs only while the connector runs"
        bot_id, bot_name = self._connector.bot_user_id, self._connector.bot_username
        try:
            opened = await self._client.request("POST", "/channels/direct", json=[bot_id, at.sender])
            direct = str((opened if isinstance(opened, Mapping) else {}).get("id") or "")
            here = direct == at.conversation
            source = "" if here else _channel_name(await self._directory.channel(at.conversation), at.conversation)
            if here:
                anchor = f"You asked with `{label}`:\n{_quoted(question)}"
            else:
                start = (
                    "The subagent receives only the task above." if subagent
                    else "No conversation of that channel is copied, so this answer starts without it."
                )
                anchor = (
                    f"You asked in {source} with `{label}`:\n{_quoted(question)}\n"
                    f"_{start} Reply in this thread to go on. Nothing is posted in {source}._"
                )
            posted = await self._client.request("POST", "/posts", json={"channel_id": direct, "message": anchor})
            anchor_id = created_post_id(posted)
        except MattermostError as exc:
            logger.warning("mattermost %s could not post the question: %s", label, exc)
            return NOT_ASKED, f"failed:anchor-not-posted ({exc.code})"
        if not direct or not anchor_id:
            return NOT_ASKED, "failed:anchor-not-posted"
        at_direct = replace(
            at, workspace=self._connector.workspace(CHAT_TYPE_DIRECT, ""), conversation=direct,
            chat_type=CHAT_TYPE_DIRECT, trigger=TRIGGER_DM,
        )
        if refusal := refused_in_direct(host, at_direct, skill, subagent, f"your direct message with @{bot_name}"):
            return refusal, "refused:not-allowed-in-the-direct-channel"
        if not here:
            self._sides[(direct, anchor_id)] = None
            while len(self._sides) > MAX_SIDE_CONVERSATIONS:
                self._sides.popitem(last=False)
        text = question if here else (
            f"[Asked privately from Mattermost channel {source} with {label}. Your reply goes only to the person"
            f" who asked, in their direct message, and not to the channel.]\n\n{question}"
        )
        outcome = await on_inbound(
            replace(
                at_direct, message_id=anchor_id, event_id=f"{NAME}-ask-{anchor_id}", text=text,
                thread="" if here else anchor_id, reply_thread="" if here else anchor_id,
                side="" if here else anchor_id,
                skills_required=(skill,) if skill else (), subagent_required=(subagent, question) if subagent else None,
            )
        )
        if not outcome.startswith(("dispatched:", "queued:", "steered:")):
            return NOT_STARTED, f"failed:{outcome}"
        told = "Answering below." if here else f"Answering in your direct message with @{bot_name}. Nothing is posted here."
        return told, f"asked:{outcome}"
