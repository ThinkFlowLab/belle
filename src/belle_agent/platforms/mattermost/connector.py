"""The platform port on Mattermost: a bot account over the REST API and the WebSocket."""

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.attachments import (
    FILE_REASON_TOO_LARGE,
    AttachmentUnreadable,
    fetch_message_files,
)
from belle_agent.backend import Question, TurnEvent
from belle_agent.config import BelleConfigError, platform_section
from belle_agent.message_decisions import (
    KIND_DIRECT,
    KIND_GROUP,
    KIND_PRIVATE,
    KIND_PUBLIC,
    bot_identity,
    message_entry,
)
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
from belle_agent.platforms.marks import react_marks
from belle_agent.platforms.mattermost.actions import ActionListener
from belle_agent.platforms.mattermost.affordances import MATTERMOST_AFFORDANCES
from belle_agent.platforms.mattermost.capabilities import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_GROUP,
    CHAT_TYPE_OPEN,
    CHAT_TYPE_PRIVATE,
    MATTERMOST_CAPABILITIES,
    NAME,
    PUBLIC_CHAT_TYPES,
)
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError, created_post_id
from belle_agent.platforms.mattermost.commands import Commands
from belle_agent.platforms.mattermost.directory import Directory
from belle_agent.platforms.mattermost.events import (
    EVENT_KINDS,
    EVENT_TYPES,
    KIND_MEMBER,
    KIND_REACTION,
    MEMBER_POSTS,
    render_events,
)
from belle_agent.platforms.mattermost.questions import Questions
from belle_agent.platforms.mattermost.render import RENDERER
from belle_agent.platforms.mattermost.resources import MattermostResourcesToolkit
from belle_agent.platforms.mattermost.settings import KEY_SERVERS, MattermostSettings, read_settings
from belle_agent.platforms.mattermost.socket import MattermostSocket, NoHello
from belle_agent.platforms.mattermost.surfaces import STOP_LABEL, TurnSurfaces
from belle_agent.platforms.mattermost.toolkit import (
    METADATA_CHANNEL,
    METADATA_CHANNEL_TYPE,
    METADATA_MESSAGE,
    METADATA_ROOT,
    METADATA_SERVER,
    METADATA_USER,
    MattermostToolkit,
    post_files,
)
from belle_agent.platforms.mattermost.typing_indicator import TypingIndicator
from belle_agent.tools.inputs import iso_utc

logger = logging.getLogger(__name__)

EVENT_POSTED = "posted"
EVENT_POST_EDITED = "post_edited"
#: The codes of a failed call that the same call can succeed after: the server did not answer, or said it cannot serve now.
TRANSIENT_CODES = frozenset({"unreachable", "server_error", "rate_limited"})
#: The workspace of a direct or group channel, which belongs to no team. No team id has this form.
WORKSPACE_DIRECT = "direct"
#: The ``uri`` of a file a message carries by reference, until ``fetch_files`` loads it.
FILE_SCHEME = "mattermost-file:"
_FILE_ID = "mattermost_file_id"
#: How long the files of one message may take to load, all together.
FILE_BUDGET_SECONDS = 120.0
FILE_REASON_DOWNLOAD_FAILED = "the download from Mattermost failed"
#: The reason a file was not read, told to its sender, by the client's code.
_FILE_REASONS = {
    "forbidden": "the server does not let the bot read the file",
    "not_found": "the server holds no such file that the bot can see",
    "too_large": FILE_REASON_TOO_LARGE,
}
#: How many thread roots keep their author in memory.
MAX_ROOT_AUTHORS = 1024
#: How long before a gap its posts are read back from. Belle and the server stamp times with their own clocks;
#: the host drops a read-back post that the connection did deliver, as a duplicate.
READ_BACK_OVERLAP_SECONDS = 60.0
#: The largest page of posts that the server gives.
POSTS_PAGE = 200
#: The event of a reaction that the server added, which carries the reaction's time.
EVENT_REACTION_ADDED = "reaction_added"

#: The decision gate's word for each kind of channel.
_CONVERSATION_KINDS = {
    CHAT_TYPE_OPEN: KIND_PUBLIC,
    CHAT_TYPE_PRIVATE: KIND_PRIVATE,
    CHAT_TYPE_DIRECT: KIND_DIRECT,
    CHAT_TYPE_GROUP: KIND_GROUP,
}

class EphemeralRefused(MattermostError):
    """The server refused a post for one person: the bot lacks the permission ``create_post_ephemeral``."""

    def __init__(self, refused: MattermostError) -> None:
        super().__init__(refused.code, refused.method, refused.path, refused.status, refused.server_id)
        self.args = (
            (
                f"{refused}: a post for one person needs the permission create_post_ephemeral, which the server"
                " gives a system administrator only by default. An operator grants it to the bot's role"
            ),
        )


def _decoded(value: Any, kind: type) -> Any:
    """A field the server may send as JSON text, such as ``post`` and ``mentions``, decoded; or an empty ``kind``."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return kind()
    return value if isinstance(value, kind) else kind()


class MattermostPlatform:
    """A Mattermost server, as one bot account sees it."""

    name = NAME
    posting_tool = "post_message"
    capabilities = MATTERMOST_CAPABILITIES
    public_chat_types = PUBLIC_CHAT_TYPES
    direct_chat_types = frozenset({CHAT_TYPE_DIRECT})
    event_kinds: Mapping[str, EventKind] = EVENT_KINDS
    ephemeral_posts = True
    affordances = MATTERMOST_AFFORDANCES

    def command_prefix(self, workspace: str) -> str:
        return "/belle"

    def __init__(self, settings: MattermostSettings, *, client: MattermostClient | None = None) -> None:
        self._settings = settings
        self._client = client or MattermostClient(settings.url, settings.token)
        self.socket = MattermostSocket(self._client, on_gap=self._read_back)
        self.directory = Directory(self._client, audience_member_max_pages=settings.audience_member_max_pages)
        self._reader: asyncio.Task[None] | None = None
        self._on_inbound: OnInbound | None = None
        #: The bot account's user id and user name, once started.
        self.bot_user_id = ""
        self.bot_username = ""
        #: The author of each thread root read so far, newest last.
        self._root_authors: OrderedDict[str, str] = OrderedDict()
        #: The member changes the connection delivered lately: channel, member, event, and when, oldest first. A
        #: read-back after a gap reaches back to them, and skips the system posts that record them.
        self._members_delivered: deque[tuple[str, str, str, float]] = deque()
        self._host: Any = None
        #: The listener of clicks and of the slash command, where the settings give the server an address for it.
        self.actions: ActionListener | None = None
        self.questions: Questions | None = None
        self.commands: Commands | None = None
        if settings.actions_url:
            self.actions = ActionListener(
                settings.actions_url, bind=settings.actions_bind, port=settings.actions_port,
                on_action=self._on_action, on_command=self._on_command,
            )
            self.questions = Questions(self._client, self.actions, lambda: self._on_inbound)
            self.commands = Commands(self._client, self.directory, settings.command_token, self)
        questions = self.questions
        self.surfaces = TurnSurfaces(
            self._client,
            self.directory,
            settings,
            None if questions is None else lambda workspace, message: questions.stop_button(
                STOP_LABEL, workspace=workspace, message_id=message
            ),
        )
        self.typing = TypingIndicator(self._client, self.directory, enabled=settings.typing_indicator)

    def bind(self, host: Any) -> None:
        """Keep the host's command operations, which the slash command answers from."""
        self._host = host

    @property
    def server(self) -> str:
        """The server's name where several are served, or empty."""
        return self._settings.server

    @property
    def label(self) -> str:
        """What a log line names this connector by."""
        return f"mattermost server {self.server}" if self.server else "mattermost"

    def workspace(self, chat_type: str, team_id: str) -> str:
        """The workspace of a channel: its team, or ``direct`` for a direct or group channel, which has none.

        Where several servers are served, the server's name and a colon come first: a team id names a team of
        one server only.
        """
        place = WORKSPACE_DIRECT if chat_type in (CHAT_TYPE_DIRECT, CHAT_TYPE_GROUP) else team_id
        return f"{self.server}:{place}" if self.server else place

    @property
    def workspaces(self) -> None:
        """Team names are checked against each target channel, not a startup snapshot."""
        return None

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck:
        try:
            record = await self.directory.channel(conversation, fresh=True)
            if not record.get("id"):
                return PlaceCheck(False, False, f"conversation {conversation} was not found")
            expected = self.workspace(str(record.get("type") or ""), str(record.get("team_id") or ""))
            if workspace and workspace != expected:
                return PlaceCheck(False, False, f"conversation {conversation} belongs to workspace {expected}, not {workspace}")
            if thread not in ("", "new"):
                post = await self._client.request("GET", f"/posts/{thread}")
                if not isinstance(post, Mapping) or post.get("channel_id") != conversation:
                    return PlaceCheck(False, False, f"thread {thread} is not in {conversation}")
        except MattermostError as exc:
            return PlaceCheck(False, False, str(exc))
        chat_type = str(record.get("type") or "")
        try:
            await self._client.request("GET", f"/channels/{conversation}/members/{self.bot_user_id}")
        except MattermostError as exc:
            return PlaceCheck(True, False, str(exc), chat_type)
        return PlaceCheck(True, True, chat_type=chat_type)

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        record = await self.directory.channel(conversation, fresh=True)
        expected = self.workspace(str(record.get("type") or ""), str(record.get("team_id") or ""))
        if workspace and workspace != expected:
            return False
        path = (
            f"/teams/{record['team_id']}/members/{user}"
            if record.get("type") == CHAT_TYPE_OPEN and not record.get("shared")
            else f"/channels/{conversation}/members/{user}"
        )
        try:
            await self._client.request("GET", path)
        except MattermostError as exc:
            if exc.code == "not_found":
                return False
            raise
        return True

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None:
        if workspace:
            for conversation in (origin, target):
                record = await self.directory.channel(conversation, fresh=True)
                expected = self.workspace(str(record.get("type") or ""), str(record.get("team_id") or ""))
                if workspace != expected:
                    raise ValueError(f"conversation {conversation} belongs to workspace {expected}, not {workspace}")
        found, _ = await self.directory.newcomers(origin, target, fresh=True)
        return found

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str:
        response = await self._client.request("POST", "/channels/direct", json=[self.bot_user_id, user])
        room = str((response if isinstance(response, Mapping) else {}).get("id") or "")
        if not room:
            raise MattermostError("bad_response", "POST", "/channels/direct")
        return room

    def mention(self, user: str) -> str:
        name = self.directory.known_name(user)
        return f"@{name}" if name else user

    @property
    def host(self) -> Any:
        """The host's command operations, once bound."""
        return self._host

    @property
    def receiver(self) -> OnInbound | None:
        """Where a record goes, while the connector runs."""
        return self._on_inbound

    async def start(self, on_inbound: OnInbound) -> None:
        """Check the token, open the WebSocket and read its ``hello``, or raise ``BelleConfigError`` with the cause.

        Raise ``PlatformUnreachable`` where the server did not answer. Nothing is open then, so ``start`` can be
        called again.
        """
        try:
            await self._connect()
        except PlatformUnreachable:
            raise
        except BaseException:
            await self.stop()
            raise
        if self.actions is not None:
            try:
                await self.actions.start()
            except OSError as exc:
                await self.stop()
                raise BelleConfigError(
                    f"the Mattermost action listener could not listen on {self._settings.actions_bind}:"
                    f"{self._settings.actions_port}: {exc.strerror or exc}"
                ) from None
        self._on_inbound = on_inbound
        self._reader = asyncio.create_task(self._read())
        logger.info("%s connected: user=%s connection=%s", self.label, self.bot_user_id, self.socket.connection_id)

    async def _connect(self) -> None:
        try:
            me = await self._client.me()
        except MattermostError as exc:
            if exc.code == "token_rejected":
                raise BelleConfigError(
                    f"Mattermost did not accept the bot token of {self._settings.token_env}: {exc}"
                ) from None
            if exc.code in TRANSIENT_CODES:
                raise PlatformUnreachable(f"Mattermost did not answer the bot's identity check: {exc}") from None
            raise BelleConfigError(f"Mattermost refused the bot's identity check: {exc}") from None
        self.bot_user_id = str(me.get("id") or "")
        self.bot_username = str(me.get("username") or "")
        if not self.bot_user_id:
            raise BelleConfigError("Mattermost named no user for the bot token, so no post can be told from the bot's own")
        try:
            await self.socket.connect()
        except MattermostError as exc:
            if exc.code in TRANSIENT_CODES:
                raise PlatformUnreachable(f"the Mattermost WebSocket did not open: {exc}") from None
            raise BelleConfigError(
                f"the Mattermost WebSocket did not open with the bot token of {self._settings.token_env}: {exc}"
            ) from None
        except NoHello:
            raise BelleConfigError(
                "the Mattermost WebSocket did not start with hello, so the connection is not authenticated"
            ) from None

    async def _read(self) -> None:
        """Hand each event to ``handle`` until the socket is closed. An event that fails is logged and skipped."""
        async for event in self.socket.events():
            try:
                await self.handle(event)
            except Exception:
                logger.exception("mattermost event %s seq=%s raised", event.get("event"), event.get("seq"))

    async def stop(self) -> None:
        """Close the WebSocket and the HTTP connections."""
        self._on_inbound = None
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if self.actions is not None:
            await self.actions.close()
        self.typing.close()
        await self.surfaces.close()
        await self.socket.close()
        await self._client.close()

    # ── inbound ──────────────────────────────────────────────────────────

    async def handle(self, event: Mapping[str, Any]) -> str:
        """Hand one WebSocket event to the host, as the record the port defines. Return what became of it."""
        kind = str(event.get("event") or "")
        if kind not in (EVENT_POSTED, EVENT_POST_EDITED, *EVENT_TYPES):
            return f"ignored:event-{kind or '-'}"
        if self._on_inbound is None:
            outcome = "dropped:platform-not-started"
        elif kind in EVENT_TYPES:
            outcome = await self._event(event, EVENT_TYPES[kind])
        else:
            outcome = await self._posted(event, edited=kind == EVENT_POST_EDITED)
        logger.info("%s inbound %s: seq=%s outcome=%s", self.label, kind, event.get("seq"), outcome)
        return outcome

    async def _posted(self, event: Mapping[str, Any], *, edited: bool) -> str:
        assert self._on_inbound is not None
        data = _decoded(event.get("data"), dict)
        post = _decoded(data.get("post"), dict)
        post_id = str(post.get("id") or "")
        sender = str(post.get("user_id") or "")
        channel = str(post.get("channel_id") or "")
        root = str(post.get("root_id") or "")
        if not (post_id and channel):
            return "dropped:malformed-post"
        if sender == self.bot_user_id:
            if not root:
                self._remember_root(post_id, sender)
            return "ignored:own-post"
        if post.get("type"):
            # A system message, such as a member joining, is no person's message.
            return f"ignored:post-type-{post['type']}"
        chat_type = str(data.get("channel_type") or "")
        self.directory.learn_name(sender, str(data.get("sender_name") or "").removeprefix("@"))
        text = str(post.get("message") or "")
        trigger = TRIGGER_NONE
        if chat_type == CHAT_TYPE_DIRECT:
            trigger = TRIGGER_DM
        elif self._mentions_bot(text):
            trigger = TRIGGER_MENTION
            text = self._without_leading_mention(text)
        elif root and await self._root_author(root) == self.bot_user_id:
            trigger = TRIGGER_REPLY
        side = self.commands.side(channel, root) if self.commands is not None and chat_type == CHAT_TYPE_DIRECT else ""
        return await self._on_inbound(
            InboundMessage(
                platform=NAME,
                workspace=self.workspace(chat_type, str(data.get("team_id") or "")),
                conversation=channel,
                message_id=post_id,
                sender=sender,
                text=text,
                event_id=f"{post_id}:{post.get('update_at') or ''}" if edited else post_id,
                thread=root,
                sender_is_bot=str(_decoded(post.get("props"), dict).get("from_bot") or "").lower() == "true",
                chat_type=chat_type,
                trigger=trigger,
                edited=edited,
                # In a channel the answer goes in the message's thread. In a direct channel it stays where the message is.
                reply_thread=root or ("" if chat_type == CHAT_TYPE_DIRECT else post_id),
                envelope=envelope(channel, post_id, root, trigger),
                side=side,
                # By reference: nothing is read until the host says the message has a turn or is ingested.
                files=tuple(
                    {"name": entry.get("name") or entry["file_id"], "uri": f"{FILE_SCHEME}{entry['file_id']}", _FILE_ID: entry["file_id"]}
                    for entry in post_files(post)
                ),
            )
        )

    async def _read_back(self, dropped_at: float, reopened_at: float) -> None:
        """Hand the host what a gap that a new connection did not send held, oldest first.

        That is its posts, the member changes that its system posts record, and the reactions added to the latest
        posts of each channel, as far back as ``gap_reaction_scan_max_pages`` reaches. A removed reaction leaves no
        record, nor does a member who leaves the team in the team's channels other than its default one: those
        are lost.
        """
        since = int((dropped_at - READ_BACK_OVERLAP_SECONDS) * 1000)
        try:
            channels = await self._client.request("GET", "/users/me/channels")
        except MattermostError as exc:
            logger.warning("%s did not read back the gap since %s: %s", self.label, iso_utc(since / 1000), exc)
            return
        found: list[tuple[int, str, Mapping[str, Any], Mapping[str, Any]]] = []
        for channel in channels if isinstance(channels, list) else []:
            if not isinstance(channel, Mapping):
                continue
            # A reaction moves no channel's last post time, so every channel is read.
            try:
                posts = await self._latest_posts(str(channel.get("id") or ""), since)
            except MattermostError as exc:
                logger.warning(
                    "%s did not read back channel %s since %s: %s", self.label, channel.get("id"), iso_utc(since / 1000), exc
                )
                continue
            for post in posts:
                created = int(post.get("create_at") or 0)
                if created >= since:
                    found.append((created, "member" if post.get("type") in MEMBER_POSTS else "post", post, channel))
                for reaction in (_decoded(post.get("metadata"), dict).get("reactions") or ()):
                    # The bot's own reactions are its marks.
                    if not isinstance(reaction, Mapping) or str(reaction.get("user_id") or "") == self.bot_user_id:
                        continue
                    if (added := int(reaction.get("create_at") or 0)) >= since:
                        found.append((added, "reaction", {**reaction, "post_id": post.get("id")}, channel))
        found.sort(key=lambda item: item[0])
        counts = {kind: sum(item[1] == kind for item in found) for kind in ("post", "member", "reaction")}
        logger.info(
            "%s read back %d posts, %d member changes and %d reactions since %s, for a gap that ended at %s",
            self.label, counts["post"], counts["member"], counts["reaction"], iso_utc(since / 1000), iso_utc(reopened_at),
        )
        for _, kind, item, channel in found:
            try:
                if kind == "post":
                    data = {"post": item, "channel_type": channel.get("type"), "team_id": channel.get("team_id")}
                    await self.handle({"event": EVENT_POSTED, "data": data})
                elif kind == "member":
                    await self._member_read_back(item, since / 1000)
                else:
                    reaction = {"reaction": {**item, "channel_id": channel.get("id")}}
                    await self.handle({"event": EVENT_REACTION_ADDED, "data": reaction, "broadcast": {"channel_id": channel.get("id")}})
            except Exception:
                logger.exception("mattermost %s read back after a gap raised", kind)

    async def _latest_posts(self, channel: str, since: int) -> list[Mapping[str, Any]]:
        """A channel's posts, newest first: those created at ``since``, in milliseconds since the epoch, or later,
        and at least ``gap_reaction_scan_max_pages`` pages of them."""
        found: list[Mapping[str, Any]] = []
        page = 0
        while True:
            # The listing with ``since`` stops at 1000 posts, and right after many writes leaves some out, both
            # without saying so. This one pages, newest first.
            listing = await self._client.request(
                "GET", f"/channels/{channel}/posts", params={"page": page, "per_page": POSTS_PAGE}
            )
            listing = listing if isinstance(listing, Mapping) else {}
            posts, order = listing.get("posts") or {}, listing.get("order") or []
            older = False
            for post_id in order:
                post = posts.get(post_id) if isinstance(posts, Mapping) else None
                if isinstance(post, Mapping):
                    found.append(post)
                    older |= int(post.get("create_at") or 0) < since
            page += 1
            if len(order) < POSTS_PAGE or older and page >= self._settings.gap_reaction_scan_max_pages:
                return found

    async def _member_read_back(self, post: Mapping[str, Any], since: float) -> None:
        """Hand the host the member change that a system post records, unless the connection delivered it since
        ``since``, in seconds since the epoch."""
        event_type, prop = MEMBER_POSTS[str(post.get("type"))]
        member = str((_decoded(post.get("props"), dict).get(prop) if prop else post.get("user_id")) or "")
        channel = str(post.get("channel_id") or "")
        delivered = next(
            (index for index, (where, who, kind, when) in enumerate(self._members_delivered)
             if (where, who, kind) == (channel, member, event_type) and when >= since),
            None,
        )
        if delivered is not None:
            del self._members_delivered[delivered]
            outcome = "ignored:delivered-before-the-gap"
        else:
            event = {"event": event_type, "data": {"user_id": member}, "broadcast": {"channel_id": channel}}
            # The system post is the change's own record: a change read back twice has one id.
            outcome = await self._event(event, KIND_MEMBER, event_id=str(post.get("id") or ""))
        logger.info("%s read back %s: post=%s outcome=%s", self.label, event_type, post.get("id"), outcome)

    async def _event(self, event: Mapping[str, Any], kind: str, *, event_id: str = "") -> str:
        """Hand a reaction, or a member who joined or left a channel, to the host as an event.

        ``event_id`` names an event that a read-back made from the change's record. A live event is named by what
        it is where it carries enough for that, and else by the connection and its number.
        """
        assert self._on_inbound is not None
        data = _decoded(event.get("data"), dict)
        broadcast = _decoded(event.get("broadcast"), dict)
        event_type = str(event.get("event") or "")
        record: dict[str, Any] = {"family": kind, "event_type": event_type}
        live = not event_id
        # A resumed connection sends a missed event again under the same number.
        event_id = event_id or f"{self.socket.connection_id}:{event.get('seq')}"
        if kind == KIND_REACTION:
            reaction = _decoded(data.get("reaction"), dict)
            actor, anchor = str(reaction.get("user_id") or ""), str(reaction.get("post_id") or "")
            # A removed reaction names no channel: the broadcast does.
            channel = str(reaction.get("channel_id") or broadcast.get("channel_id") or "")
            emoji, created = str(reaction.get("emoji_name") or ""), int(reaction.get("create_at") or 0)
            record.update(emoji_name=emoji, message_id=anchor)
            if event_type == EVENT_REACTION_ADDED and created:
                # The reaction itself, so that one read back after a gap has the id it had when it was delivered.
                event_id = f"{anchor}:{actor}:{emoji}:{created}"
        else:
            actor, anchor = str(data.get("user_id") or ""), ""
            channel = str(broadcast.get("channel_id") or data.get("channel_id") or "")
            if live:
                self._delivered(channel, actor, event_type)
        if not actor:
            return "dropped:no-actor-on-event"
        if actor == self.bot_user_id:
            # What the bot did itself, such as its own marks, is no news to it.
            return "ignored:done-by-this-bot"
        if not channel:
            return "dropped:no-conversation-on-event"
        try:
            found = await self.directory.channel(channel)
        except MattermostError as exc:
            logger.warning("mattermost event in channel %s not handed over: %s", channel, exc)
            return "dropped:channel-not-read"
        chat_type = str(found.get("type") or "")
        record.update(author_user_id=actor, chat_id=channel)
        if name := self.directory.known_name(actor):
            record["author_name"] = name
        return await self._on_inbound(
            InboundEvent(
                platform=NAME,
                workspace=self.workspace(chat_type, str(found.get("team_id") or "")),
                conversation=channel,
                kind=kind,
                actor=actor,
                record=record,
                event_id=event_id,
                chat_type=chat_type,
                direct=chat_type == CHAT_TYPE_DIRECT,
                anchor=anchor,
                anchor_thread=await self._thread_of(anchor) if anchor else "",
            )
        )

    def _delivered(self, channel: str, member: str, event_type: str) -> None:
        """Remember a member change the connection delivered, for as long as a read-back can reach back to it."""
        now = self.socket.last_event_at
        # A later gap starts at a later event, and its read-back reaches no further back than the overlap.
        while self._members_delivered and self._members_delivered[0][3] < now - READ_BACK_OVERLAP_SECONDS:
            self._members_delivered.popleft()
        self._members_delivered.append((channel, member, event_type, now))

    async def _thread_of(self, post_id: str) -> str:
        """The root of the thread a post is in, or the post itself where it roots none or cannot be read."""
        try:
            post = await self._client.request("GET", f"/posts/{post_id}")
        except MattermostError as exc:
            logger.warning("mattermost post %s not read for its thread: %s", post_id, exc)
            return post_id
        return str((post if isinstance(post, Mapping) else {}).get("root_id") or post_id)

    def _mentions_bot(self, text: str) -> bool:
        """Whether the text names the bot as ``@name``.

        The ``mentions`` of an event cannot tell: it lists the bot for ``@channel``, ``@all`` and
        ``@here``, and for every post in a group channel.
        """
        name = rf"(?<![\w.-])@{re.escape(self.bot_username)}(?![\w-]|\.\w)"
        return bool(self.bot_username) and re.search(name, text, re.IGNORECASE) is not None

    def _without_leading_mention(self, text: str) -> str:
        """The text without the bot's ``@name`` in front, where it is there."""
        mention = f"@{self.bot_username}"
        stripped = text.lstrip()
        if self.bot_username and stripped.lower().startswith(mention.lower()):
            rest = stripped[len(mention):]
            if not rest or not (rest[0].isalnum() or rest[0] in "._-"):
                return rest.strip()
        return text

    def _remember_root(self, post_id: str, author: str) -> None:
        self._root_authors[post_id] = author
        self._root_authors.move_to_end(post_id)
        while len(self._root_authors) > MAX_ROOT_AUTHORS:
            self._root_authors.popitem(last=False)

    async def _root_author(self, root: str) -> str:
        """The author of a thread's root post, read once per root. Empty where the server does not say."""
        if root not in self._root_authors:
            try:
                found = await self._client.request("GET", f"/posts/{root}")
            except MattermostError as exc:
                logger.warning("mattermost thread root %s not read: %s", root, exc)
                return ""
            self._remember_root(root, str((found if isinstance(found, Mapping) else {}).get("user_id") or ""))
        return self._root_authors[root]

    # ── outbound ─────────────────────────────────────────────────────────

    async def post(
        self, conversation: str, thread: str, text: str, *, ephemeral_to: str | None = None, workspace: str = ""
    ) -> str:
        """Post ``text`` in a channel, in the thread of ``thread`` where one is named. Return the first post's id.

        A text longer than the server takes goes in parts, in order, the later ones in the thread of the first.
        A post for one person goes through the ephemeral route, which the server grants to a system
        administrator only by default. Its refusal is raised, and names the permission.
        """
        first = ""
        for part in RENDERER.parts(text, await self.directory.post_limit()):
            body = {"channel_id": conversation, **part.payload, "root_id": thread}
            try:
                if ephemeral_to:
                    posted = await self._client.request("POST", "/posts/ephemeral", json={"user_id": ephemeral_to, "post": body})
                else:
                    posted = await self._client.request("POST", "/posts", json=body)
            except MattermostError as exc:
                if ephemeral_to and exc.code == "forbidden":
                    raise EphemeralRefused(exc) from None
                raise
            post_id = created_post_id(posted, "/posts/ephemeral" if ephemeral_to else "/posts")
            if not first:
                first = post_id
                if post_id and not thread and not ephemeral_to:
                    self._remember_root(post_id, self.bot_user_id)
            thread = thread or ("" if ephemeral_to else first)
        return first

    async def react(
        self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = ""
    ) -> None:
        """Add or remove the bot's reaction ``name`` on a post."""
        if remove:
            await self._client.request("DELETE", f"/users/{self.bot_user_id}/posts/{message_id}/reactions/{name}")
        else:
            await self._client.request(
                "POST", "/reactions", json={"user_id": self.bot_user_id, "post_id": message_id, "emoji_name": name}
            )

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str = "") -> str:
        """Post a question with a button per option. With no listener, raise: a call that needs a person is refused."""
        if self.questions is None:
            raise NotImplementedError("platforms.mattermost.actions.url is not set, so the server cannot deliver a click")
        return await self.questions.ask(conversation, thread, question, workspace=workspace)

    # ── what a running turn shows ────────────────────────────────────────

    @property
    def shows(self) -> Mapping[str, str]:
        """Each kind of event of a running turn this connector shows as configured, with its setting."""
        return self.surfaces.shows

    async def begin(self, inbound: InboundMessage) -> None:
        """Start what a turn shows before it reports anything: its card, with the stop button."""
        await self.surfaces.begin(inbound)

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Show one event of a running turn: its reply as it is written, or a tool call on its card."""
        await self.surfaces.progress(inbound, event)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End what was shown of a turn. Return whether the reply on screen became ``final``."""
        return await self.surfaces.conclude(inbound, state, final)

    async def _on_action(self, request: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        assert self.questions is not None
        return await self.questions.on_click(request)

    async def _on_command(self, request: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        assert self.commands is not None
        return await self.commands.on_command(request, headers.get("authorization", ""))

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        """Show what became of a message, and of its ingest by the second brain, with the reactions the settings name.

        While the turn of the message runs, the bot is shown typing where its answer goes.
        """
        if state in TYPING_STARTS:
            await self.typing.start(inbound)
        elif state in TYPING_ENDS:
            self.typing.stop(inbound)
        await react_marks(self.react, inbound.conversation, inbound.message_id, state, self._settings.emoji.__getitem__)

    async def _load_file(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Read one file of a message, as the ``data:`` URI a turn and the second brain take."""
        file_id = str(entry[_FILE_ID])
        try:
            info = await self._client.request("GET", f"/files/{file_id}/info")
            info = info if isinstance(info, Mapping) else {}
            size = int(info.get("size") or 0)
            limit = self._settings.file_download_max_bytes
            if size > limit:
                raise AttachmentUnreadable(
                    f"{size} bytes, over platforms.mattermost.file_download_max_bytes ({limit})", FILE_REASON_TOO_LARGE,
                )
            data = await self._client.download(f"/files/{file_id}", limit)
        except MattermostError as exc:
            raise AttachmentUnreadable(str(exc), _FILE_REASONS.get(exc.code, FILE_REASON_DOWNLOAD_FAILED)) from None
        name = str(info.get("name") or entry.get("name") or file_id)
        mime = str(info.get("mime_type") or "application/octet-stream")
        logger.info("mattermost file: loaded %s (%d bytes) file_id=%s", name, len(data), file_id)
        return {"name": name, "uri": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}

    async def fetch_files(self, inbound: InboundMessage) -> tuple[Mapping[str, Any], ...]:
        """Read the files of a message. Those not read are left out, and a post in its thread says why."""

        async def notify(notice: str) -> None:
            try:
                await self.post(inbound.conversation, inbound.reply_thread or "", notice)
            except MattermostError as exc:
                logger.warning("mattermost could not say which files were not read: %s", exc)

        return await fetch_message_files(
            [dict(entry) for entry in inbound.files if entry.get(_FILE_ID)],
            self._load_file,
            name=lambda entry: str(entry.get("name") or entry[_FILE_ID]),
            budget_seconds=FILE_BUDGET_SECONDS,
            logger=logger,
            label="Mattermost file",
            failed_reason=FILE_REASON_DOWNLOAD_FAILED,
            conversation=inbound.conversation,
            notify=notify,
        )

    def render_events(self, held: Mapping[str, Any] | None, started_by: InboundEvent | None = None) -> str:
        """Write what a turn is told of events: the one that started it, and those its conversation kept."""
        return render_events(held, started_by)

    def toolkits(self, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
        """Return the toolkit of the posting, reaction and reading tools."""
        return [
            MattermostResourcesToolkit(
                self._client, self.directory, lambda: self.bot_user_id, metadata_provider,
                max_text_chars=self._settings.history_max_total_chars,
                directory_scan_max_pages=self._settings.directory_scan_max_pages,
                file_download_max_bytes=self._settings.file_download_max_bytes,
                file_upload_max_bytes=self._settings.file_upload_max_bytes,
            )
        ]

    async def resolve_content_attachment(self, ref: str, metadata: Mapping[str, Any]) -> dict[str, str]:
        """Read a file for a content tool, by the read rule of the turn that ``metadata`` describes."""
        toolkit = MattermostToolkit(
            self._client, self.directory, lambda: self.bot_user_id, lambda: metadata,
            max_text_chars=self._settings.history_max_total_chars,
            file_download_max_bytes=self._settings.file_download_max_bytes,
            file_upload_max_bytes=self._settings.file_upload_max_bytes,
        )
        return await toolkit.content_attachment(ref)

    def request_metadata(self, inbound: InboundMessage) -> dict[str, Any]:
        """The message a turn's tool calls belong to: its channel, its thread, its id and its sender."""
        return {
            METADATA_CHANNEL: inbound.conversation,
            METADATA_CHANNEL_TYPE: inbound.chat_type,
            METADATA_MESSAGE: inbound.message_id,
            METADATA_ROOT: inbound.thread,
            METADATA_USER: inbound.sender,
            METADATA_SERVER: self.server,
        }

    # ── decisions ────────────────────────────────────────────────────────

    def _author(self, user_id: str, names: Mapping[str, str]) -> str:
        if user_id == self.bot_user_id:
            return self.bot_username or "the assistant"
        return f"@{names[user_id]}" if user_id in names else user_id or "someone"

    def decision_facts(self, inbound: InboundMessage) -> dict[str, Any]:
        """What the decision gate is told of a message: its author by name, and the bot as a mention names it."""
        names = {inbound.sender: name} if (name := self.directory.known_name(inbound.sender)) else {}
        return inbound_decision_facts(
            inbound,
            author=self._author(inbound.sender, names),
            bot_name=bot_identity(self.bot_username, f"@{self.bot_username}" if self.bot_username else ""),
            kinds=_CONVERSATION_KINDS,
        )

    async def recent_messages(self, conversation: str, limit: int, before: str, *, workspace: str = "") -> list[dict[str, Any]]:
        """The messages of a channel before the post ``before``, the bot's own included, oldest first.

        A message the server wrote, such as a join, is no person's message and is left out.
        """
        page = await self._client.request(
            "GET", f"/channels/{conversation}/posts", params={"before": before, "per_page": max(1, int(limit))}
        )
        page = page if isinstance(page, Mapping) else {}
        found = page.get("posts") or {}
        posts = [found[post_id] for post_id in reversed(page.get("order") or []) if isinstance(found.get(post_id), Mapping)]
        posts = [post for post in posts if not post.get("type")]
        names = await self.directory.usernames(str(post.get("user_id") or "") for post in posts)
        return [
            message_entry(
                self._author(str(post.get("user_id") or ""), names),
                str(post.get("message") or ""),
                ts=str(post.get("id") or ""),
                thread_ts=str(post.get("root_id") or ""),
                file_count=len(post.get("file_ids") or ()),
            )
            for post in posts
        ]


def create_platform(config: Mapping[str, Any]) -> Any:
    """Build the connector a configuration describes: one server, or one connector for each of several.

    It is not started.
    """
    section = platform_section(config, NAME)
    if isinstance(section, Mapping) and section.get(KEY_SERVERS):
        from belle_agent.platforms.mattermost.servers import create_servers

        return create_servers(config)
    return MattermostPlatform(read_settings(config))
