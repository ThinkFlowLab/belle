"""The ``!belle`` command on Matrix: the first word of a message, after an optional mention of the bot (D24).

Element answers ``/belle`` with an "Unknown command" dialog unless a person sends it as a message,
so both ``!belle`` and ``/belle`` are commands. The host gets no message of a command: the answers
come from its command operations, as on every platform, in a notice in the command's thread. A
person whom ``allow_from`` does not admit gets the refused mark and no answer. A room that the host
does not serve gets nothing.

Matrix has no message that one person sees alone (D15). A private question (``ask``, ``skills use``,
``subagents use``) is posted in the bot's direct room with the person who asked, and handed to the
host as a direct message there. Asked from another room, it opens a conversation of its own in the
thread of that post. Nothing of its answer goes to the room it was asked from.
"""

import html
import logging
import re
import urllib.parse
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from belle_agent.commands.grammar import MODELS_CURRENT, MODELS_LIST, MODELS_RESET, Subcommand
from belle_agent.commands.surface import FAILED, CommandAnswers, refused_in_direct
from belle_agent.platforms.base import MARK_REFUSED, TRIGGER_DM, InboundMessage, OnInbound, envelope
from belle_agent.platforms.matrix import render
from belle_agent.platforms.matrix.capabilities import CHAT_TYPE_DIRECT, NAME
from belle_agent.platforms.matrix.helper import HelperError
from belle_agent.platforms.matrix.ids import EVENT_RE
from belle_agent.scheduling.commands import SCHEDULE_SUBCOMMANDS
from belle_agent.scheduling.session_commands import SESSION_SUBCOMMANDS

logger = logging.getLogger(__name__)

HELP = Subcommand(("help",), "List the commands you can use here.")
STATUS = Subcommand(("status",), "Show whether Belle answers, its model, and what runs and waits here.")
STOP = Subcommand(("stop",), "Stop the turn that runs here. With several, give the message id or link.", "[message]")
QUEUE_LIST = Subcommand(("queue", "list"), "List the messages that wait for a turn here.")
QUEUE_DROP = Subcommand(("queue", "drop"), "Remove a waiting message.", "<message>")
MODELS_USE = Subcommand(("models", "use"), "Select a model for new requests in this room.", "<name>")
SKILLS_LIST = Subcommand(("skills", "list"), "List the skills that can be used here.")
SKILLS_USE = Subcommand(("skills", "use"), "Ask privately, with one skill loaded for that one task.", "<skill> <task>")
SUBAGENTS_LIST = Subcommand(("subagents", "list"), "List the subagents that can be used here.")
SUBAGENTS_USE = Subcommand(("subagents", "use"), "Give one task to a subagent, privately.", "<name> <task>")
EXPERTS_LIST = Subcommand(("experts", "list"), "List the installed experts and the one that the scope selects here.")
ASK = Subcommand(("ask",), "Ask privately. The answer comes in your direct room with Belle.", "<question>")
SUBCOMMANDS = (
    HELP, STATUS, SKILLS_LIST, SKILLS_USE, SUBAGENTS_LIST, SUBAGENTS_USE, EXPERTS_LIST,
    MODELS_LIST, MODELS_CURRENT, MODELS_USE, MODELS_RESET, QUEUE_LIST, QUEUE_DROP, ASK, STOP,
    *SCHEDULE_SUBCOMMANDS,
    *SESSION_SUBCOMMANDS,
)

NOT_ASKED = "Your question could not be posted, so nothing was asked. Please try again."
NOT_STARTED = "Your question was posted, but Belle could not start on it. Please try again."
#: Private questions asked from another room, each the thread of a notice in a direct room. The oldest goes first.
MAX_SIDE_CONVERSATIONS = 512
#: A mention that a client puts first in the HTML of a message: a link to a user, and its label.
_LEADING_LINK = re.compile(r"\s*<a href=\"https://matrix\.to/#/([^\"]+)\">(.*?)</a>", re.DOTALL)

#: The host's command operations for this platform: see ``belle_agent.host.commands``.
Host = Any


class Account(Protocol):
    """What a command needs of the connector, once it is started and bound."""

    user_id: str

    @property
    def host(self) -> Host:
        """The host's command operations."""

    @property
    def receiver(self) -> OnInbound | None:
        """Where a record goes."""

    async def call(self, op: str, **fields: Any) -> dict[str, Any]:
        """One request of the helper. Raise ``HelperError`` where it refuses."""
        ...

    async def send_event(self, conversation: str, event_type: str, content: Mapping[str, Any]) -> str:
        """Send one event, and return its id."""
        ...

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        """Show what became of a message."""
        ...


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def _room_link(room: str) -> str:
    """A room as a link, which Element shows with the room's name."""
    return f"[{room}](https://matrix.to/#/{urllib.parse.quote(room, safe='!:')})"


class Commands(CommandAnswers):
    """Answer the ``!belle`` command of one connector from the conversation host."""

    platform = NAME
    place = "room"

    def __init__(self, account: Account, name: str) -> None:
        super().__init__(SUBCOMMANDS)
        self._account = account
        self._prefixes = (f"!{name}", f"/{name}")
        #: Each private question asked from another room: its direct room and the notice that roots it.
        self._sides: OrderedDict[tuple[str, str], None] = OrderedDict()

    def side(self, room: str, root: str) -> str:
        """The private conversation a message in the thread ``root`` of a direct room belongs to, or empty."""
        return root if root and (room, root) in self._sides else ""

    def parse(self, content: Mapping[str, Any]) -> tuple[str, str] | None:
        """The command word and the text after it, where the message is a command. ``None`` where it is none."""
        words = self._after_mention(content).split(maxsplit=1)
        if not words or words[0].lower() not in self._prefixes:
            return None
        return words[0].lower(), words[1] if len(words) > 1 else ""

    def _after_mention(self, content: Mapping[str, Any]) -> str:
        """The text of a message without a mention of the bot in front of it, where it starts with one."""
        body = str(content.get("body") or "").lstrip()
        found = _LEADING_LINK.match(str(content.get("formatted_body") or ""))
        if found and urllib.parse.unquote(found[1]) == self._account.user_id:
            label = html.unescape(re.sub(r"<[^>]+>", "", found[2]))
            if label and body.startswith(label):
                return body[len(label) :].lstrip(":").lstrip()
        if body.startswith(self._account.user_id):
            return body[len(self._account.user_id) :].lstrip(":").lstrip()
        return body

    def message_id(self, text: str) -> str:
        """An event id, alone or at the end of a ``matrix.to`` link, or empty."""
        words = text.split()
        if len(words) != 1:
            return ""
        found = urllib.parse.unquote(words[0].split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
        return found if EVENT_RE.match(found) else ""

    async def run(self, inbound: InboundMessage, command: str, text: str) -> str:
        """Answer the command ``text`` of the message ``inbound``. Return what became of it, and log it."""
        host, on_inbound = self._account.host, self._account.receiver
        if host is None or on_inbound is None:
            outcome = "dropped:platform-not-started"
        elif not host.admits(inbound.sender):
            await self._account.mark(inbound, MARK_REFUSED)
            outcome = "refused:allow_from"
        elif inbound.chat_type != CHAT_TYPE_DIRECT and not host.serves(inbound.conversation):
            outcome = "refused:room-not-served"
        else:
            at = replace(inbound, text="", files=(), edited=False)
            try:
                # Answers name the command as ``!belle``, which Element sends as typed.
                answer, outcome = await self.answer(host, on_inbound, at, text, self._prefixes[0])
            except Exception:  # A command that fails is told so, and the sync goes on.
                logger.exception("matrix command failed: %s", command)
                answer, outcome = FAILED, "failed"
            try:
                await self._account.send_event(
                    inbound.conversation, "m.room.message", render.note(answer, inbound.message_id, inbound.reply_thread or "")
                )
            except HelperError as exc:
                logger.warning("matrix command answer not posted: %s", exc)
                outcome = f"{outcome} (answer-not-posted {exc.code})"
        logger.info(
            "matrix command: %s %s room=%s user=%s outcome=%s",
            command, (text.split() or ["-"])[0], inbound.conversation, inbound.sender, outcome,
        )
        return outcome

    async def ask_privately(
        self, at: InboundMessage, question: str, label: str, *, skill: str = "", subagent: str = ""
    ) -> tuple[str, str]:
        """Post the question in the bot's direct room with the person, and hand that notice to the host as theirs."""
        host, on_inbound = self._account.host, self._account.receiver
        assert on_inbound is not None, "a command runs only while the connector runs"
        try:
            direct = str((await self._account.call("dm_room", user=at.sender, create=True))["room"])
            here = direct == at.conversation
            source = _room_link(at.conversation)
            if here:
                anchor = f"You asked with `{label}`:\n{_quoted(question)}"
            else:
                start = (
                    "The subagent receives only the task above." if subagent
                    else "No conversation of that room is copied, so this answer starts without it."
                )
                anchor = (
                    f"You asked in {source} with `{label}`:\n{_quoted(question)}\n\n"
                    f"_{start} Reply in this thread to go on. Nothing is posted in that room._"
                )
            anchor_id = await self._account.send_event(direct, "m.room.message", render.notice(anchor))
        except HelperError as exc:
            logger.warning("matrix %s could not post the question: %s", label, exc)
            return NOT_ASKED, f"failed:anchor-not-posted ({exc.code})"
        at_direct = replace(at, conversation=direct, chat_type=CHAT_TYPE_DIRECT, trigger=TRIGGER_DM)
        if refusal := refused_in_direct(host, at_direct, skill, subagent, "your direct room with Belle"):
            return refusal, "refused:not-allowed-in-the-direct-room"
        thread = "" if here else anchor_id
        if not here:
            self._sides[(direct, anchor_id)] = None
            while len(self._sides) > MAX_SIDE_CONVERSATIONS:
                self._sides.popitem(last=False)
        text = question if here else (
            f"[Asked privately from Matrix room {at.conversation} with {label}. Your reply goes only to the person"
            f" who asked, in their direct room, and not to the room.]\n\n{question}"
        )
        outcome = await on_inbound(
            replace(
                at_direct, message_id=anchor_id, event_id=f"{NAME}-ask-{anchor_id}", text=text,
                thread=thread, reply_thread=thread, side=thread, envelope=envelope(direct, anchor_id, thread),
                skills_required=(skill,) if skill else (), subagent_required=(subagent, question) if subagent else None,
            )
        )
        if not outcome.startswith(("dispatched:", "queued:", "steered:")):
            return NOT_STARTED, f"failed:{outcome}"
        told = "Answering below." if here else "Answering in your direct room with Belle. Nothing is posted here."
        return told, f"asked:{outcome}"
