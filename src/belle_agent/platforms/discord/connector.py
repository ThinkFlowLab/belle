"""The platform port on Discord: a bot application over the REST API and the gateway.

A public thread uses its parent channel as the conversation and its own ID as ``thread``.
A private thread uses its own ID for both, so its smaller audience has a separate conversation.
A thread started from a message has that message's id, so a channel message is answered in a thread
named by its own id, which ``post`` starts on first use.
"""

import asyncio
import base64
import contextlib
import logging
import re
import secrets
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.attachments import FILE_REASON_TOO_LARGE, AttachmentUnreadable, fetch_message_files
from belle_agent.backend import Question, TurnEvent
from belle_agent.config import BelleConfigError, platform_section
from belle_agent.message_decisions import KIND_DIRECT, KIND_PRIVATE, bot_identity
from belle_agent.platforms.base import (
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_NONE,
    TRIGGER_REPLY,
    TYPING_ENDS,
    TYPING_STARTS,
    EventKind,
    InboundEvent,
    InboundMessage,
    OnInbound,
    PlaceCheck,
    PlatformUnreachable,
    envelope,
)
from belle_agent.platforms.decision_facts import inbound_decision_facts
from belle_agent.platforms.discord.affordances import DISCORD_AFFORDANCES, MESSAGE_LIMIT
from belle_agent.platforms.discord.capabilities import (
    CHANNEL_TYPES,
    CHAT_TYPE_DM,
    CHAT_TYPE_PRIVATE_THREAD,
    DISCORD_CAPABILITIES,
    NAME,
)
from belle_agent.platforms.discord.client import DiscordClient, DiscordError
from belle_agent.platforms.discord.commands import Commands
from belle_agent.platforms.discord.editing import DiscordEditToolkit
from belle_agent.platforms.discord.events import EVENT_KINDS, EVENT_TYPES, KIND_REACTION, render_events
from belle_agent.platforms.discord.file_send import DiscordFileToolkit
from belle_agent.platforms.discord.forum_post import FORUM_TYPES, create_forum_thread
from belle_agent.platforms.discord.gateway import DiscordGateway, GatewayStopped
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE
from belle_agent.platforms.discord.native import DiscordNativeToolkit
from belle_agent.platforms.discord.questions import Questions
from belle_agent.platforms.discord.render import RENDERER
from belle_agent.platforms.discord.resources import DiscordResourcesToolkit
from belle_agent.platforms.discord.rich_post import RichPost
from belle_agent.platforms.discord.settings import DiscordSettings, read_settings
from belle_agent.platforms.discord.surfaces import STOP_LABEL, TurnSurfaces
from belle_agent.platforms.discord.targets import SEND, SEND_THREADS, VIEW, Target, TargetResolver, permissions
from belle_agent.platforms.discord.toolkit import (
    METADATA_CHANNEL,
    METADATA_CHANNEL_TYPE,
    METADATA_GUILD,
    METADATA_MESSAGE,
    METADATA_REPLY_THREAD,
    METADATA_THREAD,
    METADATA_USER,
    DiscordToolkit,
)
from belle_agent.platforms.discord.typing_indicator import TypingIndicator
from belle_agent.platforms.discord.writing import DiscordWritingToolkit
from belle_agent.platforms.marks import react_marks

logger = logging.getLogger(__name__)

#: The codes of a failed call that the same call can succeed after: Discord did not answer, or said it cannot serve now.
TRANSIENT_CODES = frozenset({"unreachable", "server_error", "rate_limited"})
#: The raw DM workspace marker; named bot installations qualify it with their name.
WORKSPACE_DIRECT = "direct"
#: The ``uri`` of a file a message carries by reference, until ``fetch_files`` loads it.
FILE_SCHEME = "discord-file:"
_FILE_URL = "discord_file_url"
_FILE_SIZE = "discord_file_size"
_FILE_TYPE = "discord_file_type"
#: How long the files of one message may take to load, all together.
FILE_BUDGET_SECONDS = 120.0
FILE_REASON_DOWNLOAD_FAILED = "the download from Discord failed"
_FILE_REASONS = {"forbidden": "Discord does not let the bot read the file", "not_found": "the file is gone from Discord",
                 "too_large": FILE_REASON_TOO_LARGE}
#: The message types that are a person's message: a plain message and a reply. Any other is the system's.
_PERSON_TYPES = frozenset({0, 19})
_GROUP_DM = 3
_THREAD_TYPES = frozenset({10, 11, 12})
#: Discord's code for "a thread has already been created for this message".
_THREAD_EXISTS = 160004
#: The most characters a thread name holds, as Discord documents it.
THREAD_NAME_MAX = 100
#: How many channels, and how many messages, the connector keeps what it learned of.
MAX_CHANNELS = 4096
MAX_MESSAGES = 2048
_CONVERSATION_KINDS = {CHAT_TYPE_DM: KIND_DIRECT, CHAT_TYPE_PRIVATE_THREAD: KIND_PRIVATE}


def _conversation_id(channel_id: str, channel: Mapping[str, Any]) -> str:
    """Keep private-thread context separate from the parent channel's audience."""
    return str(channel.get("parent_id") or channel_id) if channel.get("type") in (10, 11) else channel_id


def registered_toolkits(platform: Any, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
    """Build the toolkits offered by one Discord installation."""
    post = DiscordToolkit(platform, metadata_provider)
    return [
        post,
        DiscordResourcesToolkit(platform, metadata_provider),
        DiscordWritingToolkit(platform, metadata_provider),
        DiscordFileToolkit(platform, metadata_provider, post),
        DiscordEditToolkit(platform, metadata_provider),
        DiscordNativeToolkit(platform, metadata_provider),
    ]


class DiscordPlatform:
    """A Discord application, as its bot sees it in every guild it is in."""

    name = NAME
    posting_tool = "post_message"
    posting_tools = ("post_message", "send_discord_file")
    capabilities = DISCORD_CAPABILITIES
    #: Role grants, member overwrites and private-thread membership cannot be proven by a chat type.
    #: No Discord type satisfies Second Brain public selection; explicit parent IDs opt in.
    public_chat_types: frozenset[str] = frozenset()
    direct_chat_types = frozenset({CHAT_TYPE_DM})
    event_kinds: Mapping[str, EventKind] = EVENT_KINDS
    #: A message only one person sees exists only as an answer to that person's interaction.
    ephemeral_posts = False
    #: What no other member states.
    affordances = DISCORD_AFFORDANCES

    def command_prefix(self, workspace: str) -> str:
        """The command word a report names, in every guild."""
        return "/belle"

    def __init__(self, settings: DiscordSettings, *, client: DiscordClient | None = None) -> None:
        self.bot = settings.bot
        self._settings = settings
        self._client = client or DiscordClient(settings.token)
        self.gateway = DiscordGateway(self._client, settings)
        self.typing = TypingIndicator(self._client, enabled=settings.typing_indicator)
        self._reader: asyncio.Task[None] | None = None
        self._registration: asyncio.Task[None] | None = None
        self._on_inbound: OnInbound | None = None
        self._host: Any = None
        #: The bot's user id and user name, from READY.
        self.bot_user_id = ""
        self.bot_username = ""
        #: What is known of each channel and thread: its type, guild, parent and owner.
        self._channels: OrderedDict[str, dict[str, Any]] = OrderedDict()
        #: Each message seen: the channel it is in and its text, for its reactions and the name of its thread.
        self._messages: OrderedDict[str, tuple[str, str]] = OrderedDict()
        #: The roles the bot holds in each guild. GUILD_CREATE carries the bot's member, and Discord sends a
        #: GUILD_MEMBER_UPDATE for the bot whatever the intents, so a role mention needs no call.
        self._bot_roles: dict[str, frozenset[str]] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self.questions = Questions(
            self._client, lambda: self.gateway.application_id, self._conversation_of, lambda: self._on_inbound,
            workspace=self.workspace,
            pending_questions_max=settings.pending_questions_max,
            question_expiry_seconds=settings.question_expiry_seconds,
        )
        self.commands = Commands(self._client, self, lambda: self.gateway.application_id, self._conversation_of)
        questions = self.questions
        self.surfaces = TurnSurfaces(
            self._client, settings, self._thread_channel,
            lambda message: questions.stop_button(STOP_LABEL, message_id=message),
        )
        #: The interactions being answered, each in a task of its own so that no event waits behind another.
        self._interactions: set[asyncio.Task[str]] = set()
        #: Why the gateway stopped, once it has.
        self.stopped = ""
        self._down = asyncio.Event()

    def workspace(self, guild: str) -> str:
        """Qualify the raw guild or direct marker with this bot's stable installation name."""
        return f"{self.bot}:{guild}" if self.bot else guild

    @property
    def workspaces(self) -> None:
        """Discord guilds are discovered dynamically and checked when a place is resolved."""
        return None

    async def _place(self, channel: str, workspace: str = "") -> Target | None:
        target = await TargetResolver(self).resolve(channel, "", posting=False)
        actual = self.workspace(target.guild or WORKSPACE_DIRECT)
        if workspace and workspace != actual:
            return None
        return target

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck:
        """Check a Discord channel or thread using its current membership and permissions."""
        try:
            target = await self._place(thread if thread not in ("", "new") else conversation, workspace)
        except DiscordError as exc:
            return PlaceCheck(False, False, str(exc))
        if target is None:
            return PlaceCheck(False, False, f"conversation {conversation} is not in workspace {workspace}")
        if (
            thread not in ("", "new") and target.parent != conversation
            and not (target.kind == 12 and thread == conversation)
        ):
            return PlaceCheck(False, False, f"thread {thread} is not in conversation {conversation}")
        chat_type = CHANNEL_TYPES.get(target.kind, "")
        required = SEND_THREADS if target.parent else SEND
        can_post = not target.guild or bool(target.bot_permissions & required)
        member = self.bot_user_id in target.readers and can_post
        reason = "bot lacks permission to post in this Discord place" if not can_post else ""
        return PlaceCheck(True, member, reason, chat_type)

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        """Check current reader access without granting access from cached channel metadata."""
        try:
            target = await self._place(conversation, workspace)
        except DiscordError:
            return False
        return target is not None and user in target.readers

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None:
        """Return readers newly reached by a post, only when both places share a workspace."""
        try:
            source = await self._place(origin, workspace)
            destination = await self._place(target, workspace)
        except DiscordError:
            return None
        if source is None or destination is None:
            return None
        if self.workspace(source.guild or WORKSPACE_DIRECT) != self.workspace(destination.guild or WORKSPACE_DIRECT):
            return None
        return tuple(sorted(destination.readers - source.readers))

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str:
        """Open a DM in this Discord installation."""
        if workspace and workspace != self.workspace(WORKSPACE_DIRECT):
            raise ValueError(
                f"workspace {workspace} cannot open a Discord DM; use {self.workspace(WORKSPACE_DIRECT)}"
            )
        channel = await self._client.mapping("POST", "/users/@me/channels", json={"recipient_id": user})
        if channel.get("type") != 1 or not channel.get("id"):
            raise DiscordError("bad_response", "POST", "/users/@me/channels")
        self._learn_channel(channel)
        return str(channel["id"])

    def mention(self, user: str) -> str:
        return f"<@{user}>"

    def bind(self, host: Any) -> None:
        """Keep the host's command operations, which the slash command answers from."""
        self._host = host

    @property
    def host(self) -> Any:
        """The host's command operations, once bound."""
        return self._host

    @property
    def receiver(self) -> OnInbound | None:
        """Where a record goes while the connector runs."""
        return self._on_inbound

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self, on_inbound: OnInbound) -> None:
        """Open the gateway and wait for READY, or raise ``BelleConfigError`` with the cause.

        Raise ``PlatformUnreachable`` where Discord did not answer. Nothing is open then, so ``start`` can be
        called again.
        """
        try:
            await self.gateway.connect()
        except DiscordError as exc:
            if exc.code in TRANSIENT_CODES:
                raise PlatformUnreachable(f"Discord did not answer: {exc}") from None
            await self.stop()
            raise BelleConfigError(f"Discord did not start: {exc}") from None
        except GatewayStopped as exc:
            await self.stop()
            raise BelleConfigError(f"Discord did not start: {exc}") from None
        self.bot_user_id = str(self.gateway.user.get("id") or "")
        self.bot_username = str(self.gateway.user.get("username") or "")
        if not self.bot_user_id:
            await self.stop()
            raise BelleConfigError("Discord's READY named no user for the bot, so no message can be told from its own")
        self._on_inbound = on_inbound
        self._reader = asyncio.create_task(self._read())
        logger.info("discord connected: bot=%s user=%s guilds=%d", self.bot or "default", self.bot_user_id, len(self.gateway.guilds))
        # Return before either task runs so named-bot identity validation can publish routing first.
        self._registration = asyncio.create_task(self._register_commands())

    async def _register_commands(self) -> None:
        try:
            await self.commands.register()
        except Exception as exc:  # noqa: BLE001 - Unexpected registration failures must reach host health.
            self.stopped = f"discord command registration failed ({type(exc).__name__})"
            if self._reader is not None:
                self._reader.cancel()

    async def _stop_registration(self) -> None:
        registration, self._registration = self._registration, None
        if registration is not None:
            registration.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await registration

    async def _read(self) -> None:
        """Read frames continuously and admit events in order. Dispatch interactions immediately."""
        pending: asyncio.Queue[tuple[Mapping[str, Any], str]] = asyncio.Queue(self._settings.pending_events_max)

        async def dispatch() -> None:
            while True:
                event, session_id = await pending.get()
                try:
                    await self.handle(event, session_id=session_id)
                except Exception:
                    logger.exception("discord event %s seq=%s raised", event.get("t"), event.get("s"))

        dispatcher = asyncio.create_task(dispatch())
        try:
            async for event in self.gateway.events():
                if event.get("t") == "INTERACTION_CREATE":
                    try:
                        await self.handle(event)
                    except Exception:
                        logger.exception("discord event %s seq=%s raised", event.get("t"), event.get("s"))
                else:
                    pending.put_nowait((event, self.gateway.session_id))
        except asyncio.CancelledError:
            if not self.stopped:
                raise
        except asyncio.QueueFull:
            self.stopped = (
                f"discord event queue needs {pending.qsize() + 1} slots, "
                f"exceeding platforms.discord.pending_events_max={self._settings.pending_events_max}"
            )
        except GatewayStopped as exc:
            self.stopped = str(exc)
        except Exception as exc:  # noqa: BLE001 - A reader failure must reach host health.
            # Exception messages can contain transport response bodies or credentials.
            self.stopped = f"discord gateway reader failed ({type(exc).__name__})"
        else:
            self.stopped = "discord gateway reader ended unexpectedly"
        finally:
            dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher
        self._on_inbound = None
        await self._stop_registration()
        self.typing.close()
        for task in list(self._interactions):
            task.cancel()
        try:
            await self.questions.close()
            await self.surfaces.close()
            await self.gateway.close()
            await self._client.close()
        finally:
            logger.error("discord bot=%s down: %s", self.bot or "default", self.stopped)
            self._down.set()

    async def wait_down(self) -> str:
        """Return the cause when the Gateway stops permanently; reconnects stay internal."""
        await self._down.wait()
        return self.stopped

    async def stop(self) -> None:
        """Close the gateway and the HTTP connections."""
        self._on_inbound = None
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
        await self._stop_registration()
        if reader is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        for task in list(self._interactions):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.typing.close()
        await self.questions.close()
        await self.surfaces.close()
        await self.gateway.close()
        await self._client.close()

    # ── what is known of channels ────────────────────────────────────────

    def learn_channel(self, channel: Mapping[str, Any]) -> None:
        """Keep what Discord said of a channel, such as a direct message a command opened."""
        self._learn_channel(channel)

    def _learn_channel(self, channel: Mapping[str, Any], guild: str = "") -> None:
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            return
        self._channels[channel_id] = {
            "type": channel.get("type"),
            "guild_id": str(channel.get("guild_id") or guild),
            "parent_id": str(channel.get("parent_id") or ""),
            "owner_id": str(channel.get("owner_id") or ""),
        }
        self._channels.move_to_end(channel_id)
        while len(self._channels) > MAX_CHANNELS:
            self._channels.popitem(last=False)

    async def _channel(self, channel_id: str) -> dict[str, Any]:
        """What is known of a channel, read from Discord once where the gateway did not say."""
        if channel_id not in self._channels:
            self._learn_channel(await self._client.mapping("GET", f"/channels/{channel_id}"))
        return self._channels.get(channel_id, {})

    async def _conversation_of(self, channel_id: str) -> tuple[str, str]:
        """Return the conversation and chat type, with a separate conversation for each private thread."""
        try:
            channel = await self._channel(channel_id)
        except DiscordError as exc:
            logger.warning("discord channel %s not read: %s", channel_id, exc)
            return channel_id, ""
        channel_type = channel.get("type")
        chat_type = CHANNEL_TYPES.get(channel_type, "") if isinstance(channel_type, int) else ""
        return _conversation_id(channel_id, channel), chat_type

    def _learn_bot_roles(self, member: Any, guild: str) -> bool:
        """Keep the roles of ``member`` in ``guild`` where it is the bot. Return whether it was."""
        user = member.get("user") if isinstance(member, Mapping) else None
        if not guild or not isinstance(user, Mapping) or str(user.get("id")) != self.bot_user_id:
            return False
        self._bot_roles[guild] = frozenset(map(str, member.get("roles") or ()))
        return True

    def _learn_message(self, message_id: str, channel: str, text: str) -> None:
        self._messages[message_id] = (channel, text)
        self._messages.move_to_end(message_id)
        while len(self._messages) > MAX_MESSAGES:
            self._messages.popitem(last=False)

    # ── inbound ──────────────────────────────────────────────────────────

    async def handle(self, event: Mapping[str, Any], *, session_id: str | None = None) -> str:
        """Hand one gateway event to the host, as the record the port defines. Return what became of it."""
        kind = str(event.get("t") or "")
        data = event.get("d")
        data = data if isinstance(data, Mapping) else {}
        if kind == "GUILD_CREATE":
            guild = str(data.get("id") or "")
            for channel in [*(data.get("channels") or ()), *(data.get("threads") or ())]:
                if isinstance(channel, Mapping):
                    self._learn_channel(channel, guild)
            for member in data.get("members") or ():
                self._learn_bot_roles(member, guild)
            return "learned:guild"
        if kind == "GUILD_DELETE":
            self._bot_roles.pop(str(data.get("id") or ""), None)
            return "forgot:guild"
        if kind == "GUILD_MEMBER_UPDATE" and self._learn_bot_roles(data, str(data.get("guild_id") or "")):
            return "learned:bot-roles"
        if kind in ("CHANNEL_CREATE", "CHANNEL_UPDATE", "THREAD_CREATE", "THREAD_UPDATE"):
            self._learn_channel(data)
            return "learned:channel"
        if kind == "INTERACTION_CREATE":
            return self._interaction(data)
        if kind not in ("MESSAGE_CREATE", "MESSAGE_UPDATE", *EVENT_TYPES):
            return f"ignored:event-{kind or '-'}"
        if self._on_inbound is None:
            outcome = "dropped:platform-not-started"
        elif kind in EVENT_TYPES:
            sequence = event.get("s")
            session_id = self.gateway.session_id if session_id is None else session_id
            if not session_id or not isinstance(sequence, int) or isinstance(sequence, bool):
                return "dropped:no-dispatch-identity"
            outcome = await self._event(
                kind, data, EVENT_TYPES[kind], f"{session_id}:{sequence}"
            )
        else:
            outcome = await self._message(data, edited=kind == "MESSAGE_UPDATE")
        logger.info("discord inbound %s: seq=%s outcome=%s", kind, event.get("s"), outcome)
        return outcome

    def _interaction(self, data: Mapping[str, Any]) -> str:
        """Answer ``/belle``, or a click or a form of Belle's, in a task of its own: Discord wants each acknowledged within 3 s."""
        if self.commands.owns(data):
            answering = self.commands.on_interaction(data)
        elif self.questions.owns(data):
            answering = self.questions.on_interaction(data)
        else:
            return f"ignored:interaction-type-{data.get('type')}"
        task = asyncio.create_task(answering)
        self._interactions.add(task)
        task.add_done_callback(self._interactions.discard)
        return "taken:interaction"

    async def _message(self, data: Mapping[str, Any], *, edited: bool) -> str:
        assert self._on_inbound is not None
        message_id = str(data.get("id") or "")
        channel_id = str(data.get("channel_id") or "")
        author = data.get("author")
        author = author if isinstance(author, Mapping) else {}
        sender = str(author.get("id") or "")
        if not (message_id and channel_id and sender):
            return "dropped:malformed-message" if not edited else "ignored:update-without-message"
        if edited and "content" not in data:
            return "ignored:update-without-content"
        if sender == self.bot_user_id:
            return "ignored:own-post"
        if data.get("type", 0) not in _PERSON_TYPES:
            return f"ignored:message-type-{data.get('type')}"
        try:
            channel = await self._channel(channel_id)
        except DiscordError as exc:
            logger.warning("discord channel %s not read: %s", channel_id, exc)
            return "dropped:channel-not-read"
        channel_type = channel.get("type")
        if channel_type == _GROUP_DM:
            # A bot joins a group DM only through a person's OAuth grant, which Belle does not use.
            return "ignored:group-dm"
        if channel_type not in CHANNEL_TYPES:
            return f"ignored:channel-type-{channel_type}"
        text = str(data.get("content") or "")
        attachments = [entry for entry in data.get("attachments") or () if isinstance(entry, Mapping) and entry.get("url")]
        if not text.strip() and not attachments:
            return "ignored:empty-message" if self._settings.message_content else "ignored:no-message-content"
        in_thread = channel_type in _THREAD_TYPES
        conversation = _conversation_id(channel_id, channel)
        thread = channel_id if in_thread else ""
        chat_type = CHANNEL_TYPES[channel_type]
        direct = chat_type == CHAT_TYPE_DM
        guild = str(data.get("guild_id") or channel.get("guild_id") or "")
        self._learn_message(message_id, channel_id, text)
        trigger = TRIGGER_NONE
        if direct:
            trigger = TRIGGER_DM
        elif await self._mentions_bot(data, guild):
            trigger = TRIGGER_MENTION
            text = self._without_leading_mention(text)
        elif (in_thread and channel.get("owner_id") == self.bot_user_id) or self._replies_to_bot(data):
            trigger = TRIGGER_REPLY
        if direct or in_thread:
            reply_thread = thread
        else:
            reply_thread = message_id if self._settings.reply_in_thread else ""
        return await self._on_inbound(
            InboundMessage(
                platform=NAME,
                workspace=self.workspace(WORKSPACE_DIRECT if direct else guild),
                conversation=conversation,
                message_id=message_id,
                sender=sender,
                text=text,
                event_id=f"{message_id}:{data.get('edited_timestamp') or ''}" if edited else message_id,
                thread=thread,
                # Whether an app's message is taken is the host's to decide, by the settings.
                sender_is_bot=bool(author.get("bot") or data.get("webhook_id")),
                chat_type=chat_type,
                trigger=trigger,
                edited=edited,
                reply_thread=reply_thread,
                envelope=envelope(conversation, message_id, thread, trigger),
                # By reference: nothing is read until the host says the message has a turn or is ingested.
                files=tuple(
                    {"name": str(entry.get("filename") or entry.get("id") or "file"), "uri": f"{FILE_SCHEME}{entry.get('id')}",
                     _FILE_URL: str(entry["url"]), _FILE_SIZE: int(entry.get("size") or 0),
                     _FILE_TYPE: str(entry.get("content_type") or "application/octet-stream")}
                    for entry in attachments
                ),
            )
        )

    async def _mentions_bot(self, data: Mapping[str, Any], guild: str) -> bool:
        """Whether the message mentions the bot or one of its guild roles."""
        if any(isinstance(user, Mapping) and str(user.get("id")) == self.bot_user_id
               for user in data.get("mentions") or ()):
            return True
        roles = data.get("mention_roles") or ()
        if not guild or not roles:
            return False
        if guild not in self._bot_roles:
            # Only where the guild's GUILD_CREATE did not name the bot's member.
            try:
                member = await self._client.mapping("GET", f"/guilds/{guild}/members/{self.bot_user_id}")
            except DiscordError as exc:
                logger.warning("discord bot roles not read in guild %s: %s", guild, exc)
                return False
            self._bot_roles[guild] = frozenset(map(str, member.get("roles") or ()))
        return any(str(role) in self._bot_roles[guild] for role in roles)

    def _replies_to_bot(self, data: Mapping[str, Any]) -> bool:
        referenced = data.get("referenced_message")
        author = referenced.get("author") if isinstance(referenced, Mapping) else None
        return isinstance(author, Mapping) and str(author.get("id")) == self.bot_user_id

    def _without_leading_mention(self, text: str) -> str:
        """The text without the bot's ``<@id>`` in front, where it is there."""
        found = re.match(rf"\s*<@!?{re.escape(self.bot_user_id)}>\s*", text)
        return text[found.end():] if found else text

    async def _event(self, kind_name: str, data: Mapping[str, Any], kind: str, delivery_id: str) -> str:
        """Hand a reaction, or a member who joined or left the guild, to the host as an event."""
        assert self._on_inbound is not None
        user = data.get("user")
        member = data.get("member")
        if kind == KIND_REACTION and isinstance(member, Mapping):
            user = member.get("user")
        actor = str(data.get("user_id") or (user.get("id") if isinstance(user, Mapping) else "") or "")
        if not actor:
            return "dropped:no-actor-on-event"
        if actor == self.bot_user_id:
            return "ignored:done-by-this-bot"
        if kind == KIND_REACTION and not isinstance(user, Mapping):
            # Reaction removals and DM reactions supply no member user.
            try:
                user = await self._client.mapping("GET", f"/users/{actor}")
            except DiscordError as exc:
                logger.warning("discord reaction actor %s not read: %s", actor, exc)
                return "dropped:actor-not-read"
            if str(user.get("id") or "") != actor:
                return "dropped:actor-not-read"
        record: dict[str, Any] = {"family": kind, "event_type": kind_name, "author_user_id": actor}
        guild = str(data.get("guild_id") or "")
        anchor = anchor_thread = ""
        if kind == KIND_REACTION:
            channel_id = str(data.get("channel_id") or "")
            anchor = str(data.get("message_id") or "")
            emoji = data.get("emoji")
            record.update(emoji=str(emoji.get("name") or "") if isinstance(emoji, Mapping) else "", message_id=anchor)
            try:
                channel = await self._channel(channel_id)
            except DiscordError as exc:
                logger.warning("discord event in channel %s not handed over: %s", channel_id, exc)
                return "dropped:channel-not-read"
            in_thread = channel.get("type") in _THREAD_TYPES
            conversation = _conversation_id(channel_id, channel)
            anchor_thread = channel_id if in_thread else anchor
            channel_type = channel.get("type")
            chat_type = CHANNEL_TYPES.get(channel_type, "") if isinstance(channel_type, int) else ""
        else:
            conversation = await self._member_destination(guild)
            if not conversation:
                return "dropped:no-usable-member-destination"
            chat_type = CHANNEL_TYPES[self._channels[conversation]["type"]]
        if not conversation:
            return "dropped:no-conversation-on-event"
        record["chat_id"] = conversation
        return await self._on_inbound(
            InboundEvent(
                platform=NAME,
                workspace=self.workspace(WORKSPACE_DIRECT if chat_type == CHAT_TYPE_DM else guild),
                conversation=conversation,
                kind=kind,
                actor=actor,
                record=record,
                event_id=f"{kind_name}:{delivery_id}",
                chat_type=chat_type,
                direct=chat_type == CHAT_TYPE_DM,
                actor_is_bot=bool(isinstance(user, Mapping) and user.get("bot")),
                anchor=anchor,
                anchor_thread=anchor_thread,
            )
        )

    async def _member_destination(self, guild_id: str) -> str:
        """Use a usable system channel, then the configured member-event channel."""
        try:
            guild = await self._client.mapping("GET", f"/guilds/{guild_id}")
        except DiscordError:
            logger.warning("discord member event guild=%s dropped: guild metadata could not be read", guild_id)
            return ""

        candidates = []
        system_channel_id = str(guild.get("system_channel_id") or "")
        if system_channel_id:
            candidates.append((system_channel_id, "system channel"))
        configured_id = self._settings.member_event_channels.get(guild_id, "")
        if configured_id and configured_id != system_channel_id:
            candidates.append((configured_id, "configured channel"))
        if not candidates:
            logger.warning("discord member event guild=%s dropped: no system channel or configured member-event channel", guild_id)
            return ""
        try:
            member = await self._client.mapping("GET", f"/guilds/{guild_id}/members/{self.bot_user_id}")
        except DiscordError:
            logger.warning("discord member event guild=%s dropped: bot member metadata could not be read", guild_id)
            return ""

        for channel_id, source in candidates:
            try:
                channel = await self._client.mapping("GET", f"/channels/{channel_id}")
            except DiscordError:
                logger.warning("discord member event guild=%s skipped %s %s: channel metadata could not be read", guild_id, source, channel_id)
                continue
            try:
                if (str(channel.get("id") or "") != channel_id or str(channel.get("guild_id") or "") != guild_id
                        or channel.get("type") not in (0, 5)):
                    logger.warning("discord member event guild=%s skipped %s %s: not the expected guild text or announcement channel", guild_id, source, channel_id)
                    continue
                effective = permissions(guild, channel, member)
                if effective & (VIEW | SEND) != (VIEW | SEND):
                    logger.warning("discord member event guild=%s skipped %s %s: bot lacks VIEW_CHANNEL or SEND_MESSAGES", guild_id, source, channel_id)
                    continue
            except (KeyError, TypeError, ValueError):
                logger.warning("discord member event guild=%s skipped %s %s: channel permissions could not be read", guild_id, source, channel_id)
                continue
            self._learn_channel(channel)
            return channel_id
        logger.warning("discord member event guild=%s dropped: no usable system or configured channel", guild_id)
        return ""

    # ── outbound ─────────────────────────────────────────────────────────

    async def _thread_channel(self, conversation: str, thread: str, *, options: dict[str, Any] | None = None, validate: bool = False, requester: str = "") -> str:
        """The channel a post in ``thread`` goes to: the thread, started on its root message the first time."""
        if not thread or thread == conversation:
            return conversation
        channel: Mapping[str, Any] | None = self._channels.get(thread)
        cached_message = self._messages.get(thread)
        if channel is None and (validate or not cached_message):
            try:
                channel = await self._client.mapping("GET", f"/channels/{thread}")
            except DiscordError as exc:
                if exc.status != 404:
                    raise
        if channel and channel.get("type") in _THREAD_TYPES:
            if options is not None:
                raise DiscordError("thread_options_require_new_thread", "POST", f"/channels/{thread}")
            if str(channel.get("parent_id") or "") != conversation:
                raise DiscordError("reply_target_outside_channel", "GET", f"/channels/{thread}")
            if validate:
                from belle_agent.platforms.discord.targets import TargetResolver

                await TargetResolver(self).resolve(thread, requester)
            self._learn_channel(channel)
            return thread
        message = {"id": thread, "channel_id": cached_message[0]} if cached_message else await self._client.mapping(
            "GET", f"/channels/{conversation}/messages/{thread}"
        )
        message_channel = str(message.get("channel_id") or "")
        if str(message.get("id") or "") != thread:
            raise DiscordError("reply_to_message_id_target_mismatch", "GET", f"/channels/{conversation}/messages/{thread}")
        if message_channel != conversation:
            raise DiscordError("reply_target_outside_channel", "GET", f"/channels/{conversation}/messages/{thread}")
        lock = self._thread_locks.setdefault(thread, asyncio.Lock())
        async with lock:
            if thread in self._channels and self._channels[thread].get("type") in _THREAD_TYPES:
                if options is not None:
                    raise DiscordError("thread_options_require_new_thread", "POST", f"/channels/{thread}")
                if self._channels[thread].get("parent_id") != conversation:
                    raise DiscordError("reply_target_outside_channel", "GET", f"/channels/{thread}")
                return thread
            text = self._messages.get(thread, ("", ""))[1]
            name = re.sub(r"<[@#][!&]?\d+>", "", text).strip()[:THREAD_NAME_MAX] or "Reply"
            payload: dict[str, Any] = {"name": name}
            for option, field in (("name", "name"), ("auto_archive_minutes", "auto_archive_duration"), ("rate_limit_per_user", "rate_limit_per_user")):
                if options and option in options:
                    payload[field] = options[option]
            try:
                created = await self._client.mapping(
                    "POST", f"/channels/{conversation}/messages/{thread}/threads", json=payload,
                )
                if str(created.get("id") or "") != thread or str(created.get("parent_id") or "") != conversation or created.get("type") not in _THREAD_TYPES:
                    raise DiscordError("target_mismatch", "POST", f"/channels/{conversation}/messages/{thread}/threads")
                self._learn_channel(created)
                logger.info("discord thread started: channel=%s thread=%s", conversation, thread)
            except DiscordError as exc:
                if exc.discord_code != _THREAD_EXISTS:
                    raise
                existing = await self._client.mapping("GET", f"/channels/{thread}")
                if str(existing.get("id") or "") != thread or str(existing.get("parent_id") or "") != conversation or existing.get("type") not in _THREAD_TYPES:
                    raise DiscordError("reply_target_outside_channel", "GET", f"/channels/{thread}")
                if options is not None:
                    raise DiscordError("thread_options_require_new_thread", "POST", f"/channels/{thread}")
                if validate:
                    from belle_agent.platforms.discord.targets import TargetResolver

                    await TargetResolver(self).resolve(thread, requester)
                self._learn_channel(existing)
        self._thread_locks.pop(thread, None)
        return thread

    async def post(
        self, conversation: str, thread: str, text: str, *, ephemeral_to: str | None = None, workspace: str = ""
    ) -> str:
        """Post ``text`` in a channel, or in the thread ``thread`` (started on its root message where it is new).

        Return the first message ID. A private message requires an interaction.
        """
        sent = await self.post_parts(conversation, thread, text, ephemeral_to=ephemeral_to)
        return sent[0]["message_id"] if sent else ""

    async def post_parts(
        self, conversation: str, thread: str, text: str, *,
        ephemeral_to: str | None = None, sent: list[dict[str, str]] | None = None,
        reply_to: str | None = None, thread_options: dict[str, Any] | None = None,
        validate_placement: bool = False, requester: str = "", require_thread: bool = False,
        thread_reference: str | None = None,
        prepared_parts: list[RichPost] | None = None,
        confirmations: list[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        """Send every part and retain confirmed placements if a later send fails."""
        if ephemeral_to:
            raise DiscordError("ephemeral_unsupported", "POST", "/channels/messages")
        if validate_placement:
            channel: Mapping[str, Any] | None = self._channels.get(conversation)
            if channel is None:
                channel = await self._client.mapping("GET", f"/channels/{conversation}")
                if str(channel.get("id") or "") != conversation:
                    raise DiscordError("target_mismatch", "GET", f"/channels/{conversation}")
                self._learn_channel(channel)
            kind = channel.get("type")
            if kind in FORUM_TYPES:
                if not require_thread or thread or reply_to or thread_reference:
                    raise DiscordError("forum_post_requires_new_thread", "POST", f"/channels/{conversation}/threads")
                channel = await self._client.mapping("GET", f"/channels/{conversation}")
                if str(channel.get("id") or "") != conversation or channel.get("type") not in FORUM_TYPES:
                    raise DiscordError("target_mismatch", "GET", f"/channels/{conversation}")
                parts = prepared_parts if prepared_parts is not None else [
                    RichPost(part.payload) for part in RENDERER.parts(text, MESSAGE_LIMIT)
                ]
                if not parts:
                    raise DiscordError("nothing_to_post", "POST", f"/channels/{conversation}/threads")
                posted = sent if sent is not None else []
                first, *rest = parts
                thread_id, first_id, first_message = await create_forum_thread(
                    self._client, channel, thread_options, first.payload, first.files,
                )
                self._learn_channel({"id": thread_id, "type": 11, "parent_id": conversation})
                self._learn_message(first_id, thread_id, str(first.payload.get("content") or ""))
                posted.append({"chat_id": conversation, "message_id": first_id, "thread_id": thread_id})
                if confirmations is not None:
                    confirmations.append(first_message)
                await self.post_parts(
                    conversation, thread_id, text, sent=posted, prepared_parts=rest,
                    confirmations=confirmations,
                )
                return posted
            if kind in _THREAD_TYPES:
                from belle_agent.platforms.discord.targets import TargetResolver

                resolved = await TargetResolver(self).resolve(conversation, requester)
                if not resolved.parent:
                    raise DiscordError("thread_parent_required", "GET", f"/channels/{conversation}")
                if thread_options is not None:
                    raise DiscordError("thread_options_require_new_thread", "POST", f"/channels/{conversation}")
                if thread and thread != conversation:
                    thread_reference = thread
                    thread = ""
            elif require_thread and not thread:
                raise DiscordError("reply_to_message_id_required_for_new_thread", "POST", f"/channels/{conversation}")
            elif thread and kind not in (0, 5):
                raise DiscordError("reply_mode_thread_requires_text_or_announcement_channel", "POST", f"/channels/{conversation}")
            elif kind not in (0, 1, 5):
                raise DiscordError("unsupported_post_target", "POST", f"/channels/{conversation}")
            elif thread_options is not None and not thread:
                raise DiscordError("thread_options_require_new_thread", "POST", f"/channels/{conversation}")
            elif thread_options is not None and "applied_tag_ids" in thread_options:
                raise DiscordError("thread.applied_tag_ids_requires_forum_or_media_channel", "POST", f"/channels/{conversation}")
        target = await self._thread_channel(conversation, thread, options=thread_options, validate=validate_placement, requester=requester)
        reference = reply_to or thread_reference
        if reference:
            cached = self._messages.get(reference)
            message = await self._client.mapping("GET", f"/channels/{target}/messages/{reference}") if cached is None else {"id": reference, "channel_id": cached[0]}
            if str(message.get("id") or "") != reference or str(message.get("channel_id") or "") != target:
                raise DiscordError("reply_to_message_id_outside_target_channel", "GET", f"/channels/{target}/messages/{reference}")
        posted = sent if sent is not None else []
        message_parts = prepared_parts if prepared_parts is not None else [
            RichPost(part.payload) for part in RENDERER.parts(text, MESSAGE_LIMIT)
        ]
        for part in message_parts:
            payload = {**part.payload, "nonce": str(secrets.randbits(64)), "enforce_nonce": True}
            if reply_to:
                payload["message_reference"] = {"message_id": reply_to, "channel_id": target, "fail_if_not_exists": True}
            try:
                if prepared_parts is None:
                    request = {"json": payload}
                else:
                    request = RichPost(payload, part.files).request_kwargs()
                response = await self._client.mapping("POST", f"/channels/{target}/messages", **request)
            except DiscordError as exc:
                if exc.code in {"unreachable", "bad_response"}:
                    raise DiscordError("delivery_uncertain_do_not_retry", "POST", f"/channels/{target}/messages") from None
                raise
            message_id = response.get("id")
            if (not isinstance(message_id, str) or not SNOWFLAKE_RE.fullmatch(message_id)
                    or response.get("channel_id") != target):
                raise DiscordError("delivery_uncertain_do_not_retry", "POST", f"/channels/{target}/messages")
            self._learn_message(message_id, target, str(payload.get("content") or ""))
            posted.append({"chat_id": conversation, "message_id": message_id, "thread_id": target if target != conversation or self._channels.get(target, {}).get("type") in _THREAD_TYPES else ""})
            if confirmations is not None:
                confirmations.append(response)
        return posted

    async def react(
        self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = ""
    ) -> None:
        """Add or remove the bot's reaction ``name`` (a unicode emoji, or ``name:id``) on a message."""
        channel = self._messages.get(message_id, (conversation, ""))[0]
        path = f"/channels/{channel}/messages/{message_id}/reactions/{urllib.parse.quote(name, safe=':')}/@me"
        await self._client.request("DELETE" if remove else "PUT", path)

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str = "") -> str:
        """Post a question with buttons, a select, or a button that opens a form, where the answer goes."""
        target = await self._thread_channel(conversation, thread)
        return await self.questions.ask(conversation, target, question, workspace=workspace)

    async def withdraw_question(self, question_id: str, *, workspace: str = "") -> None:
        """Remove controls for a question the host no longer accepts."""
        await self.questions.withdraw(question_id)

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        """Show what became of a message with the reactions the settings name; type while its turn runs."""
        if state in TYPING_STARTS:
            self.typing.start(inbound.message_id, lambda: self._typing_channel(inbound))
        elif state in TYPING_ENDS:
            self.typing.stop(inbound.message_id)
        await react_marks(self.react, inbound.thread or inbound.conversation, inbound.message_id, state,
                          self._settings.emoji.__getitem__)

    @property
    def shows(self) -> Mapping[str, str]:
        """What a running turn shows, as the settings turn each surface on."""
        return self.surfaces.shows

    async def begin(self, inbound: InboundMessage) -> None:
        """Start what a turn shows before it reports anything: its card, with the stop button."""
        await self.surfaces.begin(inbound)

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Show one event of a running turn: its reply as it is written, or a tool on its card."""
        await self.surfaces.progress(inbound, event)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End what a turn showed. Return whether its shown reply became ``final``."""
        return await self.surfaces.conclude(inbound, state, final)

    def _typing_channel(self, inbound: InboundMessage) -> str:
        """Where the answer to ``inbound`` shows as being typed: its thread, once that thread exists, else its channel.

        A channel message is answered in a thread started on it, which does not exist until the answer starts
        it, and a typing notice there would be refused as an unknown channel.
        """
        if inbound.thread:
            return inbound.thread
        reply = inbound.reply_thread or ""
        if reply and self._channels.get(reply, {}).get("type") in _THREAD_TYPES:
            return reply
        return inbound.conversation

    async def _load_file(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Read one file of a message from Discord's CDN, as the ``data:`` URI a turn and the second brain take."""
        size = int(entry.get(_FILE_SIZE) or 0)
        max_bytes = self._settings.file_download_max_bytes
        if size > max_bytes:
            raise AttachmentUnreadable(f"{size} bytes, over platforms.discord.file_download_max_bytes ({max_bytes})", FILE_REASON_TOO_LARGE)
        try:
            data = await self._client.download(str(entry[_FILE_URL]), max_bytes)
        except DiscordError as exc:
            raise AttachmentUnreadable(str(exc), _FILE_REASONS.get(exc.code, FILE_REASON_DOWNLOAD_FAILED)) from None
        mime = str(entry.get(_FILE_TYPE) or "application/octet-stream")
        logger.info("discord file: loaded %s (%d bytes)", entry.get("name"), len(data))
        return {"name": str(entry.get("name") or "file"), "uri": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}

    async def fetch_files(self, inbound: InboundMessage) -> tuple[Mapping[str, Any], ...]:
        """Read the files of a message. Those not read are left out, and a post where the answer goes says why."""

        async def notify(notice: str) -> None:
            try:
                await self.post(inbound.conversation, inbound.reply_thread or "", notice)
            except DiscordError as exc:
                logger.warning("discord could not say which files were not read: %s", exc)

        return await fetch_message_files(
            [dict(entry) for entry in inbound.files if entry.get(_FILE_URL)],
            self._load_file,
            name=lambda entry: str(entry.get("name") or "file"),
            budget_seconds=FILE_BUDGET_SECONDS,
            logger=logger,
            label="Discord file",
            failed_reason=FILE_REASON_DOWNLOAD_FAILED,
            conversation=inbound.conversation,
            notify=notify,
        )

    def render_events(self, held: Mapping[str, Any] | None, started_by: InboundEvent | None = None) -> str:
        """Write what a turn is told of events: the one that started it, and those its conversation kept."""
        return render_events(held, started_by)

    def toolkits(self, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
        """The turn-bound Discord tools."""
        return registered_toolkits(self, metadata_provider)

    def request_metadata(self, inbound: InboundMessage) -> dict[str, Any]:
        """The message a turn's tool calls belong to, and where its answer goes."""
        return {
            METADATA_CHANNEL: inbound.conversation,
            METADATA_CHANNEL_TYPE: inbound.chat_type,
            METADATA_MESSAGE: inbound.message_id,
            METADATA_THREAD: inbound.thread,
            METADATA_REPLY_THREAD: inbound.reply_thread or "",
            METADATA_USER: inbound.sender,
            "discord_bot": self.bot,
            METADATA_GUILD: "" if inbound.chat_type == CHAT_TYPE_DM else inbound.workspace.split(":", 1)[-1],
        }

    def decision_facts(self, inbound: InboundMessage) -> dict[str, Any]:
        """What the decision gate is told of a message, with Discord's bot mention."""
        return inbound_decision_facts(
            inbound,
            author=inbound.sender,
            bot_name=bot_identity(self.bot_username, f"<@{self.bot_user_id}>" if self.bot_user_id else ""),
            kinds=_CONVERSATION_KINDS,
        )


def create_platform(config: Mapping[str, Any]) -> Any:
    """Build the connector the ``discord`` section describes. It is not started."""
    section = platform_section(config, NAME)
    if isinstance(section, Mapping) and "bots" in section:
        from belle_agent.platforms.discord.bots import DiscordBots
        from belle_agent.platforms.discord.settings import read_bots

        return DiscordBots([DiscordPlatform(settings) for settings in read_bots(section)])
    return DiscordPlatform(read_settings(section if isinstance(section, Mapping) else {}))
