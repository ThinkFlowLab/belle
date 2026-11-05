"""The Matrix platform: messages in through the helper's timeline, answers out as notices.

A turn's tools post, react, read and download files through the helper, which decrypts them.
Questions are polls, and a vote on one of the bot's polls is an answer. A running turn shows its
reply as it is written and its activity card, and a person's ⏹️ reaction stops it.
"""

import asyncio
import base64
import collections
import contextlib
import logging
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from belle_agent.attachments import (
    FILE_REASON_TOO_LARGE,
    AttachmentUnreadable,
    fetch_message_files,
)
from belle_agent.audience import Audience, audience_newcomers, write_permitted
from belle_agent.backend import Question, TurnEvent
from belle_agent.click_notices import STOP_REFUSED_NOTICE
from belle_agent.config import BelleConfigError
from belle_agent.message_decisions import KIND_DIRECT, KIND_PRIVATE, KIND_PUBLIC, bot_identity, message_entry
from belle_agent.platforms.base import (
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_NONE,
    TRIGGER_REPLY,
    TYPING_ENDS,
    TYPING_STARTS,
    EventKind,
    InboundDrop,
    InboundEvent,
    InboundMessage,
    InboundStop,
    OnInbound,
    PlaceCheck,
    PlatformUnreachable,
    envelope,
)
from belle_agent.platforms.decision_facts import inbound_decision_facts
from belle_agent.platforms.marks import react_marks
from belle_agent.platforms.matrix import protocol, render
from belle_agent.platforms.matrix.affordances import MATRIX_AFFORDANCES
from belle_agent.platforms.matrix.capabilities import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_PRIVATE,
    CHAT_TYPE_PUBLIC,
    MATRIX_CAPABILITIES,
    NAME,
    PUBLIC_CHAT_TYPES,
)
from belle_agent.platforms.matrix.commands import Commands
from belle_agent.platforms.matrix.events import (
    EVENT_KINDS,
    KIND_MEMBER,
    KIND_PIN,
    KIND_REACTION,
    MEMBER,
    PINNED,
    REACTION,
    REDACTION,
    pin_changes,
    previous,
    render_events,
)
from belle_agent.platforms.matrix.helper import Helper, HelperError, PartialPostError
from belle_agent.platforms.matrix.ids import EVENT_RE
from belle_agent.platforms.matrix.questions import POLL_RESPONSE, Questions
from belle_agent.platforms.matrix.reading import FileOverLimit, attachment, download, read_room
from belle_agent.platforms.matrix.resources import MatrixResourcesToolkit
from belle_agent.platforms.matrix.settings import (
    KEY_PATH,
    MAX_LINE_BYTES,
    MatrixSettings,
    normalize_reaction_key,
    reaction_key,
)
from belle_agent.platforms.matrix.surfaces import STOP_KEY, TurnSurfaces
from belle_agent.platforms.matrix.toolkit import (
    METADATA_ACCOUNT,
    METADATA_CHAT_TYPE,
    METADATA_MESSAGE,
    METADATA_ROOM,
    METADATA_ROOT,
    METADATA_USER,
    MatrixToolkit,
)
from belle_agent.platforms.rendering import RenderedPart

logger = logging.getLogger(__name__)

def start_fixes(settings: MatrixSettings) -> dict[str, str]:
    """What to fix for each reason the helper gives when it refuses to start, in the account's own keys and variables."""
    at = settings.where

    def variable(name: str, key: str) -> str:
        return f"{name} or {name}_FILE" if name else f"a variable in {at}.{key}"

    passphrase = settings.passphrase_env
    password, recovery = variable(settings.password_env, "password_env"), variable(settings.recovery_key_env, "recovery_key_env")
    recovery_name = settings.recovery_key_env or f"the variable of {at}.recovery_key_env"
    return {
        "store_passphrase_missing": f"set {passphrase} or {passphrase}_FILE",
        "store_open_failed": f"check {passphrase}: it must be the passphrase this store was made with",
        "password_missing": f"set {password} for the first start of a device",
        "login_failed": f"check {at}.user_id and {settings.password_env or f'the variable of {at}.password_env'}",
        "recovery_key_missing": f"set {recovery}",
        "recovery_failed": f"check {recovery_name}: it must be the account's current recovery key",
        "recovery_key_output_missing": f"set {at}.recovery_key_output to a new file outside the store",
        "recovery_key_output_exists": f"set {at}.recovery_key_output to a file that does not exist",
        "recovery_key_output_in_store": f"set {at}.recovery_key_output outside {at}.store_dir",
        "bootstrapped": f"the key is in the file of {at}.recovery_key_output: set {recovery} from it",
        "not_cross_signed": f"set {recovery_name} to recover this device; never copy a store back from a backup",
        "store_locked": f"stop the other Belle or operator command that uses {at}.store_dir",
        "identity_reset": f"the key is in the file of {at}.recovery_key_output: set {recovery} from it",
    }


#: A helper that exits this many times within ``RESTART_WINDOW_SECONDS`` is not started again.
RESTART_LIMIT = 5
RESTART_WINDOW_SECONDS = 300.0
#: The longest wait before a restart. The waits double from 1 s.
RESTART_MAX_DELAY_SECONDS = 60.0
#: How long a typing notice lasts, and how often it is sent again while a turn runs.
TYPING_TIMEOUT_MS = 30_000
TYPING_REPEAT_SECONDS = 15.0
#: How many events keep their sender and thread in memory.
MAX_KNOWN_EVENTS = 1024
#: How often a send that the server rate-limits is tried, in all.
SEND_TRIES = 5
#: What a sender in a direct room is told, once per Megolm session, of a message the bot cannot decrypt.
UNDECRYPTABLE_NOTICE = (
    "I could not decrypt a message you sent: this device has no key for it. If the key reaches me soon,"
    " I will answer then. Otherwise, please send it again."
)


#: The decision gate's kind of each chat type.
_CONVERSATION_KINDS = {CHAT_TYPE_DIRECT: KIND_DIRECT, CHAT_TYPE_PRIVATE: KIND_PRIVATE, CHAT_TYPE_PUBLIC: KIND_PUBLIC}
#: What the decision gate reads for a message the bot holds no key for.
UNDECRYPTABLE_ENTRY = "[a message Belle cannot decrypt]"
#: The ``uri`` of a file a message carries by reference, until ``fetch_files`` loads it.
FILE_SCHEME = "matrix-file:"
_FILE = "matrix_file"
#: How long the files of one message may take to load, all together.
FILE_BUDGET_SECONDS = 120.0
FILE_REASON_DOWNLOAD_FAILED = "the download from the Matrix server failed"


class MatrixStartRefused(BelleConfigError):
    """The helper refused to start, or could not. The message says what to fix, and the launcher stops with it."""


def start_request(settings: MatrixSettings) -> dict[str, Any]:
    """The fields of the ``start`` request for the account. Secrets that are not set stay out."""
    fields: dict[str, Any] = {
        "homeserver": settings.homeserver,
        "user_id": settings.user_id,
        "store_dir": settings.store_dir,
        "device_name": settings.device_name,
        "store_passphrase": settings.store_passphrase,
    }
    for key in ("password", "recovery_key", "recovery_key_output"):
        if value := getattr(settings, key):
            fields[key] = value
    return fields


def refusal(exc: HelperError, settings: MatrixSettings) -> str:
    """What a refused start says, with what to fix."""
    fix = start_fixes(settings).get(exc.reason, "")
    return f"matrix cannot start: {exc.message}{f'. {fix[:1].upper()}{fix[1:]}' if fix else ''}"


class MatrixPlatform:
    """One Matrix account, served by one helper process."""

    name = NAME
    posting_tool = "post_message"
    capabilities = MATRIX_CAPABILITIES
    public_chat_types = PUBLIC_CHAT_TYPES
    direct_chat_types = frozenset({CHAT_TYPE_DIRECT})
    event_kinds: Mapping[str, EventKind] = EVENT_KINDS
    #: Matrix has no message that one person of a room sees alone.
    ephemeral_posts = False
    #: A removed card leaves "Message deleted" in the room's timeline, so a turn that may stay silent posts none.
    cards_wait_for_a_post = True
    affordances = MATRIX_AFFORDANCES

    def __init__(self, settings: MatrixSettings, *, helper_factory: Callable[..., Helper] | None = None) -> None:
        self._settings = settings
        self._helper_factory = helper_factory
        self._helper: Helper | None = None
        #: Each event with the helper that sent it, so an acknowledgement reaches that helper only, and when it came.
        self._events: asyncio.Queue[tuple[Helper, dict[str, Any], float]] = asyncio.Queue()
        #: When Belle last handled every event it had, which lets the helper sync again. 0 before the first time.
        self._drained_at = 0.0
        self._on_inbound: OnInbound | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[Any]] = set()
        self._stopping = False
        #: Set once Matrix has stopped for good, with why: see ``wait_down``.
        self._down = asyncio.Event()
        self._down_reason = ""
        #: When the helper exited, within the restart window.
        self._exits: collections.deque[float] = collections.deque()
        #: The sender and the thread root of events seen or read, by event id.
        self._known: collections.OrderedDict[str, tuple[str, str]] = collections.OrderedDict()
        #: The bot's own reactions, by room, target and key without variation selectors.
        self._reactions: dict[tuple[str, str, str], str] = {}
        #: Each sender and Megolm session already told of a message the bot cannot decrypt.
        self._told: set[tuple[str, str]] = set()
        #: When each message too old for a turn was sent, since the last summary in the log.
        self._skipped: list[float] = []
        #: The messages whose turns run in each room, while the bot is shown typing there.
        self._typing: dict[str, set[str]] = {}
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        self.user_id = ""
        self.device_id = ""
        #: Whether this start logged in as a new device, rather than restoring the device of its store.
        self.new_device = False
        #: The user ids of the other accounts of this Belle. ``MatrixAccounts`` gives them; they are bots here.
        self.other_accounts: Callable[[], frozenset[str]] = lambda: frozenset()
        #: The server's upload cap does not limit reads of existing media.
        self.max_download_bytes = settings.file_download_max_bytes
        self.max_file_bytes = settings.file_upload_max_bytes
        self.max_text_chars = settings.max_text_chars
        self.membership_history_max_events = settings.membership_history_max_events
        self.questions = Questions(self)
        self.surfaces = TurnSurfaces(self, settings)
        self.commands = Commands(self, settings.command_name)
        self._host: Any = None
        #: The reactions of people seen, by event id, with the message and the key: a removal names only the reaction.
        self._seen_reactions: collections.OrderedDict[str, tuple[str, str]] = collections.OrderedDict()
        #: Each person and message already told that their stop or removal was refused.
        self._refused: set[tuple[str, str]] = set()

    @property
    def workspace(self) -> str:
        """The account's name: the workspace of each of its conversations."""
        return self._settings.account

    @property
    def bot_user_ids(self) -> frozenset[str]:
        """The users whose messages and reactions are a bot's: those of ``bot_user_ids`` and the other accounts."""
        return self._settings.bot_user_ids | self.other_accounts()

    @property
    def account_user_ids(self) -> frozenset[str]:
        """The user ids of this account: as written in the configuration, and as the homeserver logged it in."""
        return frozenset(user for user in (self._settings.user_id, self.user_id) if user)

    def command_prefix(self, workspace: str) -> str:
        return f"!{self._settings.command_name}"

    @property
    def workspaces(self) -> frozenset[str]:
        return frozenset({self.workspace})

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck:
        try:
            info = await self.call("room_info", room=conversation)
            if thread not in ("", "new"):
                await self.call("event", room=conversation, event_id=thread)
        except HelperError as exc:
            return PlaceCheck(False, False, str(exc))
        return PlaceCheck(True, True, chat_type=self._chat_type(info, False))

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        members = await self.call("members", room=conversation)
        return any(member["user_id"] == user and member["membership"] == "join" for member in members["members"])

    async def _place_audience(self, conversation: str) -> Audience:
        info = await self.call("room_info", room=conversation)
        members = await self.call("members", room=conversation)
        joined = frozenset(member["user_id"] for member in members["members"] if member["membership"] == "join")
        return Audience(bool(info["public"]), joined - {self.user_id}, False)

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None:
        source, reached = await self._place_audience(origin), await self._place_audience(target)
        return () if write_permitted(source, reached) else audience_newcomers(reached, source)

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str:
        result = await self.call("dm_room", user=user, create=True)
        room = result.get("room")
        if not isinstance(room, str) or not room:
            raise HelperError("helper_failed", f"no direct room was opened with {user}")
        return room

    def mention(self, user: str) -> str:
        return user

    def bind(self, host: Any) -> None:
        """Keep the host's command operations, which the commands and a stop by reaction read."""
        self._host = host

    @property
    def host(self) -> Any:
        """The host's command operations, once bound."""
        return self._host

    @property
    def receiver(self) -> OnInbound | None:
        """Where a record goes, while the connector runs."""
        return self._on_inbound

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self, on_inbound: OnInbound) -> None:
        """Start the helper, refuse to go on unless its device is cross-signed, then read events.

        Raise ``PlatformUnreachable`` where the homeserver did not answer. The helper is closed then, so ``start``
        can be called again.
        """
        self._on_inbound, self._stopping = on_inbound, False
        await self._open()
        self._tasks = [asyncio.create_task(self._consume()), asyncio.create_task(self._supervise())]

    async def _open(self) -> None:
        """Start a helper and its account. Raise ``MatrixStartRefused`` with what to fix, or ``PlatformUnreachable``."""
        settings = self._settings
        factory = self._helper_factory or Helper

        def queue(event: dict[str, Any]) -> None:
            self._events.put_nowait((helper, event, time.time()))

        configured_bytes = max(settings.file_download_max_bytes, settings.file_upload_max_bytes)
        line_bytes = max(MAX_LINE_BYTES, (configured_bytes + 2) // 3 * 4 + 64 * 1024)
        helper = factory(settings.helper_path, max_line_bytes=line_bytes, on_event=queue)
        try:
            await helper.open()
        except FileNotFoundError as exc:
            raise MatrixStartRefused(f"{KEY_PATH}.helper_path names {settings.helper_path}, which is not an executable on PATH") from exc
        try:
            result = await helper.request("start", **start_request(settings))
        except HelperError as exc:
            await helper.close()
            if exc.reason == protocol.START_UNREACHABLE:
                raise PlatformUnreachable(f"the Matrix homeserver did not answer: {exc.message}") from exc
            raise MatrixStartRefused(refusal(exc, settings)) from exc
        self._helper = helper
        self.user_id, self.device_id, self.new_device = result["user_id"], result["device_id"], result["new_device"]
        self.max_file_bytes = min(settings.file_upload_max_bytes, result.get("max_upload_bytes") or settings.file_upload_max_bytes)
        logger.info(
            "matrix connected: user=%s device=%s backup=%s helper=%s sdk=%s server_upload_limit=%s file_limit=%d",
            self.user_id, self.device_id, result["backup"], helper.hello.get("helper_version"), helper.hello.get("sdk_version"),
            result.get("max_upload_bytes", "unknown"), self.max_file_bytes,
        )

    async def wait_down(self) -> str:
        """Return why, once Matrix has stopped for good: a store that is an old copy, or a helper that does not come back.

        A helper that exits and starts again is no stop.
        """
        await self._down.wait()
        return self._down_reason

    def _gone(self, reason: str) -> None:
        if not self._down.is_set():
            self._down_reason = reason
            self._down.set()

    async def _supervise(self) -> None:
        """Start the helper again after it exits, after 1, 2, 4 ... 60 s. Give up after too many exits."""
        while not self._stopping:
            helper = self._helper
            if helper is not None and helper.exited is not None:
                code = await asyncio.shield(helper.exited)
                if self._stopping:
                    return
                logger.warning("matrix helper exited with code %s; its pending requests failed", code)
                self._helper = None
                await helper.close()
            now = time.monotonic()
            self._exits.append(now)
            while self._exits[0] < now - RESTART_WINDOW_SECONDS:
                self._exits.popleft()
            if len(self._exits) >= RESTART_LIMIT:
                logger.error(
                    "matrix stays down: the helper exited %d times within %.0f s. The other platforms go on."
                    " Read the helper's last lines above, fix the cause, then restart Belle",
                    len(self._exits), RESTART_WINDOW_SECONDS,
                )
                self._gone(f"the helper exited {len(self._exits)} times within {RESTART_WINDOW_SECONDS:.0f} s")
                return
            delay = min(RESTART_MAX_DELAY_SECONDS, 2.0 ** (len(self._exits) - 1))
            logger.warning("matrix helper starts again in %.0f s", delay)
            await asyncio.sleep(delay)
            try:
                await self._open()
            except MatrixStartRefused as exc:
                logger.error("matrix stays down: %s", exc)
                self._gone(str(exc))
                return
            except (OSError, HelperError, PlatformUnreachable) as exc:
                logger.warning("matrix helper did not start again: %s", exc)

    async def stop(self) -> None:
        """Stop reading events, then stop the helper."""
        await self._stop_reading()
        await self.surfaces.close()
        helper, self._helper = self._helper, None
        if helper is not None:
            await helper.close()

    async def withdraw(self) -> str:
        """Stop as ``stop`` does, and first log out the device where this start logged in as a new one.

        Return what became of the device, for the operator. A device that the store held before stays: it was there
        before this start, and removing it cannot be undone.
        """
        await self._stop_reading()
        helper, device = self._helper, self.device_id
        told = f"its device {device}, which its store held before this start, stays on the account"
        if helper is not None and self.new_device:
            try:
                await helper.request("log_out")
                told = f"its new device {device} is logged out and its store removed"
            except (HelperError, ConnectionError) as exc:
                told = f"its new device {device} could not be logged out ({exc}), so it stays on the account"
        await self.stop()
        return told

    async def _stop_reading(self) -> None:
        """Stop handing events on, and every task of this start."""
        self._stopping, self._on_inbound = True, None
        current = asyncio.current_task()
        tasks = [task for task in [*self._tasks, *self._typing_tasks.values()] if task is not current]
        self._tasks, self._typing_tasks, self._typing = [], {}, {}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── inbound ──────────────────────────────────────────────────────────

    async def _consume(self) -> None:
        """Hand each event on in order, and acknowledge it once handed, so the helper saves the sync position."""
        while True:
            helper, event, received = await self._events.get()
            if event["event"] == "alert" and event["kind"] == protocol.ALERT_STORE_ROLLED_BACK:
                logger.error("matrix stops: %s", event["message"])
                await self.stop()
                # Told once the helper is closed, so a launcher that exits on it leaves nothing running.
                self._gone(str(event["message"]))
                return
            try:
                await self.handle(event, received=received)
            except Exception:
                logger.exception("matrix %s event seq=%s raised", event["event"], event.get("seq", "-"))
            # An event the host could not take is acknowledged all the same: holding it back stops every sync.
            # The events of a helper that exited are not: its successor replays them and counts anew.
            if "seq" in event and helper is self._helper:
                with contextlib.suppress(HelperError, ConnectionError):
                    await helper.request("ack", seq=event["seq"])
            if self._events.empty():
                self._drained_at = time.time()
                if self._skipped:
                    self._summarize_skipped()

    def _late(self, sent: float, received: float) -> bool:
        """Whether a message reached Belle more than ``catch_up_seconds`` after it was sent.

        The helper syncs again only once Belle has handled the events it has, so a message sent while Belle was
        busy waits for Belle: that wait does not count, and neither does the wait in Belle's own queue.
        """
        return received - max(sent, self._drained_at) > self._settings.catch_up_seconds

    async def handle(self, event: Mapping[str, Any], *, received: float | None = None) -> str:
        """Act on one event from the helper. Return what became of it.

        ``received`` is when the event reached Belle, by default now.
        """
        kind = event["event"]
        if kind == "alert":
            logger.warning("matrix helper alert %s: %s", event["kind"], event["message"])
            return f"logged:alert-{event['kind']}"
        if kind == "utd":
            return await self._undecryptable(event)
        if kind == "invite":
            return await self._invited(event)
        if kind != "timeline":
            return f"ignored:event-{kind}"
        if self._on_inbound is None:
            return "dropped:platform-not-started"
        sent = event["room_event"].get("origin_server_ts")
        old = isinstance(sent, int) and self._late(sent / 1000, time.time() if received is None else received)
        if event["room_event"].get("type") in (REACTION, REDACTION, MEMBER, PINNED):
            return "ignored:older-than-catch-up" if old else await self._room_event(str(event["room"]), event["room_event"])
        if event["room_event"].get("type") == POLL_RESPONSE:
            if old:
                return "ignored:older-than-catch-up"
            outcome = await self.questions.vote(str(event["room"]), event["room_event"], self._on_inbound)
            logger.info(
                "matrix vote: room=%s user=%s outcome=%s", event["room"], event["room_event"].get("sender") or "-", outcome
            )
            return outcome
        inbound = await self.inbound(event)
        if inbound is None:
            return "ignored:no-message"
        if old:
            # The host marks it where its sender was owed an answer, so the sender learns that none comes.
            self._skipped.append(sent / 1000)
            return await self._on_inbound(replace(inbound, late=True))
        content = event["room_event"]["content"]
        if not inbound.edited and not inbound.sender_is_bot and (command := self.commands.parse(content)) is not None:
            return await self.commands.run(inbound, *command)
        outcome = await self._on_inbound(inbound)
        logger.debug("matrix %s: %s", inbound.event_id, outcome)
        return outcome

    async def _room_event(self, room: str, room_event: Mapping[str, Any]) -> str:
        """Hand a reaction, its removal, a change of members or a pin to the host as an event. ⏹️ asks for a stop."""
        kind, sender, event_id = str(room_event["type"]), str(room_event.get("sender") or ""), str(room_event.get("event_id") or "")
        content = room_event.get("content")
        content = content if isinstance(content, Mapping) else {}
        if not sender:
            return "dropped:no-actor-on-event"
        if sender == self.user_id:
            # What the bot did itself, such as its own marks, is no news to it.
            return "ignored:done-by-this-bot"
        if kind == REACTION:
            relates = content.get("m.relates_to")
            relates = relates if isinstance(relates, Mapping) else {}
            target, key = str(relates.get("event_id") or ""), str(relates.get("key") or "")
            if not target or not key:
                return "dropped:malformed-reaction"
            if normalize_reaction_key(key) == STOP_KEY:
                if sender in self.bot_user_ids:
                    # A bot's ⏹️ is the mark it puts on a turn it stopped, as this bot's own is. It asks for no stop.
                    return "ignored:done-by-an-app"
                return await self._stop_by_reaction(room, target, sender)
            self._seen_reactions[event_id] = (target, key)
            while len(self._seen_reactions) > MAX_KNOWN_EVENTS:
                self._seen_reactions.popitem(last=False)
            return await self._event(room, KIND_REACTION, sender, event_id, {"event_type": "reaction_added", "key": key}, target)
        if kind == REDACTION:
            found = self._seen_reactions.pop(str(room_event.get("redacts") or content.get("redacts") or ""), None)
            if found is None:
                return "ignored:redaction-of-no-reaction-seen"
            return await self._event(room, KIND_REACTION, sender, event_id, {"event_type": "reaction_removed", "key": found[1]}, found[0])
        if kind == MEMBER:
            membership, before = content.get("membership"), previous(room_event).get("membership")
            if membership == before:
                return "ignored:profile-change"
            record = {"event_type": MEMBER, "membership": membership, "previous_membership": before or "",
                      "user_id": str(room_event.get("state_key") or "")}
            return await self._event(room, KIND_MEMBER, sender, event_id, record, "")
        changes = pin_changes(room_event)
        if not changes:
            # No message was added or removed: the host drops a pin that names none, and says so.
            return await self._event(room, KIND_PIN, sender, event_id, {"event_type": PINNED}, "")
        outcomes = [
            await self._event(room, KIND_PIN, sender, f"{event_id}:{pinned}", {"event_type": "pin_added" if added else "pin_removed"}, pinned)
            for pinned, added in changes
        ]
        return ", ".join(outcomes)

    async def _event(self, room: str, kind: str, actor: str, event_id: str, record: dict[str, Any], anchor: str) -> str:
        """Hand one event to the host, with its room, its actor and the message it is about."""
        assert self._on_inbound is not None
        chat_type = self._chat_type(await self._call("room_info", room=room) or {}, False)
        record = {"family": kind, **record, **({"message_id": anchor} if anchor else {}), "author_user_id": actor, "chat_id": room}
        return await self._on_inbound(
            InboundEvent(
                platform=NAME, workspace=self.workspace, conversation=room, kind=kind, actor=actor, record=record, event_id=event_id,
                chat_type=chat_type, direct=chat_type == CHAT_TYPE_DIRECT, actor_is_bot=actor in self.bot_user_ids,
                anchor=anchor, anchor_thread=((await self._about(room, anchor))[1] or anchor) if anchor else "",
            )
        )

    @staticmethod
    def _chat_type(info: Mapping[str, Any], direct_hint: bool) -> str:
        """The chat type of a room, from its ``room_info``: direct with two members, public by its join rule, else private."""
        direct = (bool(info.get("direct", direct_hint)) and not info.get("public")
                  and int(info.get("joined_members", 2)) <= 2)
        return CHAT_TYPE_DIRECT if direct else CHAT_TYPE_PUBLIC if info.get("public") else CHAT_TYPE_PRIVATE

    async def _stop_by_reaction(self, room: str, target: str, sender: str) -> str:
        """A person's ⏹️ on a turn's card or message stops the turn, and on a waiting message removes it."""
        assert self._on_inbound is not None
        card = self.surfaces.card_of(target)
        message_id = card[1] if card is not None else target
        host = self._host
        waiting = {turn.inbound.message_id: turn for turn in host.waiting(self.workspace, room)} if host is not None else {}
        running = {turn.inbound.message_id: turn for turn in host.running(self.workspace, room)} if host is not None else {}
        # ⏹️ is the stop key: on any other message it asks to stop a turn, and the host finds none running.
        if message_id in waiting:
            record: InboundStop | InboundDrop = InboundDrop(NAME, self.workspace, room, message_id, sender)
            turn, refusal = waiting[message_id], "You are not permitted to remove that message."
        else:
            record, turn, refusal = InboundStop(NAME, self.workspace, room, message_id, sender), running.get(message_id), STOP_REFUSED_NOTICE
        outcome = await self._on_inbound(record)
        if outcome.startswith("refused") and (sender, message_id) not in self._refused:
            # Matrix has no message for one person: the refusal is a reply in the thread that notifies nobody.
            self._refused.add((sender, message_id))
            thread = turn.inbound.reply_thread if turn is not None else ""
            with contextlib.suppress(HelperError):
                await self.send_event(room, "m.room.message", render.note(f"{sender}: {refusal}", target, thread or ""))
        return outcome

    def _summarize_skipped(self) -> None:
        skipped, self._skipped = self._skipped, []
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        logger.info(
            "matrix gave no turn to %d message(s) older than %d s, sent from %s to %s",
            len(skipped), self._settings.catch_up_seconds,
            time.strftime(stamp, time.gmtime(min(skipped))), time.strftime(stamp, time.gmtime(max(skipped))),
        )

    async def _undecryptable(self, event: Mapping[str, Any]) -> str:
        """Log a message the bot cannot decrypt. In a direct room, tell its sender once per Megolm session."""
        sender, session = str(event["sender"]), str(event["session_id"])
        logger.warning("matrix cannot decrypt event=%s room=%s sender=%s session=%s", event["event_id"], event["room"], sender, session)
        if not event["direct"] or (sender, session) in self._told:
            return "logged:undecryptable"
        self._told.add((sender, session))
        await self.post(str(event["room"]), "", UNDECRYPTABLE_NOTICE)
        return "told:undecryptable"

    async def _invited(self, event: Mapping[str, Any]) -> str:
        """Join a room where ``allow_from`` names the inviter, or every room where it is empty. Decline the rest."""
        room, inviter = str(event["room"]), str(event["inviter"])
        allowed = self._settings.allow_from
        if allowed and inviter not in allowed:
            await self._call("leave", room=room)
            logger.info("matrix declined the invite to %s from %s: %s.allow_from does not name them", room, inviter or "-", KEY_PATH)
            return "declined:invite"
        if await self._call("join", room=room) is None:
            return "failed:join"
        logger.info("matrix joined %s on the invite of %s", room, inviter or "-")
        return "joined"

    async def call(self, op: str, **fields: Any) -> dict[str, Any]:
        """One request of the helper. Raise ``HelperError`` where it refuses, or where no helper runs."""
        if self._helper is None:
            raise HelperError(protocol.NOT_READY, "matrix is not started")
        try:
            return await self._helper.request(op, **fields)
        except ConnectionError as exc:
            raise HelperError(protocol.RESTARTED, f"the helper ended before it answered: {exc}") from exc

    async def _call(self, op: str, **fields: Any) -> dict[str, Any] | None:
        """One request whose failure is logged and changes nothing else. ``None`` where it failed."""
        if self._helper is None:
            return None
        try:
            return await self._helper.request(op, **fields)
        except (HelperError, ConnectionError) as exc:
            logger.warning("matrix %s failed: %s", op, exc)
            return None

    def _remember(self, event_id: str, sender: str, thread: str) -> None:
        self._known[event_id] = (sender, thread)
        self._known.move_to_end(event_id)
        while len(self._known) > MAX_KNOWN_EVENTS:
            self._known.popitem(last=False)

    async def _about(self, room: str, event_id: str) -> tuple[str, str]:
        """The sender of an event and the root of its thread, read once and kept. Empty where it cannot be read."""
        if event_id not in self._known:
            found = await self._call("event", room=room, event_id=event_id)
            if found is None:
                return "", ""
            room_event = found["room_event"]
            relates = (room_event.get("content") or {}).get("m.relates_to") or {}
            in_thread = isinstance(relates, Mapping) and relates.get("rel_type") == "m.thread"
            self._remember(event_id, str(room_event.get("sender") or ""), str(relates.get("event_id") or "") if in_thread else "")
        return self._known[event_id]

    def _mentions_bot(self, content: Mapping[str, Any]) -> bool:
        """The bot's id in ``m.mentions``, or, where a client sends no ``m.mentions``, a link to the bot in the HTML."""
        mentions = content.get("m.mentions")
        if isinstance(mentions, Mapping):
            return self.user_id in (mentions.get("user_ids") or [])
        html = str(content.get("formatted_body") or "")
        return any(f"https://matrix.to/#/{form}" in html for form in (self.user_id, urllib.parse.quote(self.user_id)))

    async def inbound(self, event: Mapping[str, Any]) -> InboundMessage | None:
        """The message a timeline event carries, or nothing for any other event and for the bot's own."""
        room = str(event["room"])
        room_event = event["room_event"]
        content = room_event.get("content")
        event_id = str(room_event.get("event_id") or "")
        sender = str(room_event.get("sender") or "")
        if room_event.get("type") != "m.room.message" or not isinstance(content, Mapping) or not event_id:
            return None
        relates = content.get("m.relates_to") or {}
        if not isinstance(relates, Mapping):
            return None
        rel_type = relates.get("rel_type")
        thread = str(relates.get("event_id") or "") if rel_type == "m.thread" else ""
        if sender == self.user_id:
            self._remember(event_id, sender, thread)
            return None
        message_id, edited = event_id, rel_type == "m.replace"
        if edited:
            # The new text is in m.new_content. The top-level body is a fallback with a leading "* ".
            content, message_id = content.get("m.new_content"), str(relates.get("event_id") or "")
            if not isinstance(content, Mapping) or not message_id:
                return None
            author, thread = await self._about(room, message_id)
            if author != sender:
                logger.warning("matrix dropped an edit of %s by %s, who did not send it (%s did)", message_id, sender, author or "unknown")
                return None
        else:
            self._remember(event_id, sender, thread)
        if not isinstance(content.get("body"), str):
            return None
        found = attachment(content)
        files = ({"name": found["name"], "uri": f"{FILE_SCHEME}{event_id}", _FILE: found},) if found and not edited else ()
        info = await self._call("room_info", room=room) or {}
        chat_type = self._chat_type(info, bool(event["direct"]))
        direct = chat_type == CHAT_TYPE_DIRECT
        reply = relates.get("m.in_reply_to") or {}
        # In a thread, a client adds a reply to the thread's last event as a fallback. It is no reply.
        replied = str(reply.get("event_id") or "") if isinstance(reply, Mapping) and not relates.get("is_falling_back") else ""
        trigger = TRIGGER_NONE
        if direct:
            trigger = TRIGGER_DM
        elif self._mentions_bot(content):
            trigger = TRIGGER_MENTION
        elif self.user_id in [(await self._about(room, target))[0] for target in (replied, thread) if target]:
            trigger = TRIGGER_REPLY
        return InboundMessage(
            platform=NAME,
            workspace=self.workspace,
            conversation=room,
            message_id=message_id,
            event_id=event_id,
            sender=sender,
            text=content["body"],
            thread=thread,
            edited=edited,
            sender_is_bot=content.get("msgtype") == "m.notice" or sender in self.bot_user_ids,
            chat_type=chat_type,
            # A room whose state cannot be read counts as encrypted.
            encrypted=bool(info.get("encrypted", True)),
            trigger=trigger,
            files=files,
            # In a room the answer goes in the message's thread. In a direct room it stays where the message is.
            reply_thread=thread or ("" if direct else message_id),
            envelope=envelope(room, message_id, thread, trigger),
            side=self.commands.side(room, thread) if direct else "",
        )

    # ── outbound ─────────────────────────────────────────────────────────

    async def post(
        self, conversation: str, thread: str, text: str, *, ephemeral_to: str | None = None, workspace: str = ""
    ) -> str:
        """Send ``text`` as notices, in ``thread`` where one is given. Return the first event id.

        A text larger than one event holds goes in parts, in order. The budget is smaller in an encrypted
        room. Matrix has no private message, so ``ephemeral_to`` is refused.
        """
        if ephemeral_to:
            raise HelperError("ephemeral_unsupported", "Matrix has no message that only one person sees")
        return (await self.post_parts(conversation, thread, text))[0]

    async def post_parts(
        self,
        conversation: str,
        thread: str,
        text: str,
        *,
        reply_to: str = "",
        msgtype: str = "m.notice",
        mentions: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Send text in parts and return every event ID."""
        info = await self.call("room_info", room=conversation)
        sent: list[str] = []
        try:
            for part in render.RENDERER.parts(text, render.MESSAGE_BUDGET[bool(info["encrypted"])]):
                await self._send_text(
                    conversation, thread, part, sent=sent, reply_to=reply_to, msgtype=msgtype, mentions=mentions
                )
        except (HelperError, ConnectionError) as exc:
            error = exc if isinstance(exc, HelperError) else HelperError(protocol.RESTARTED, str(exc))
            if sent:
                raise PartialPostError(error, sent) from exc
            if isinstance(exc, HelperError):
                raise
            raise error from exc
        return sent

    async def _send_text(
        self,
        conversation: str,
        thread: str,
        part: RenderedPart[dict[str, str]],
        *,
        sent: list[str] | None = None,
        reply_to: str = "",
        msgtype: str = "m.notice",
        mentions: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Return every sent ID, including recursive retries after a server size refusal."""
        if sent is None:
            sent = []
        first = len(sent)
        actual_thread = thread or (sent[0] if sent and not reply_to else "")
        content: dict[str, Any] = {"msgtype": msgtype, **part.payload, "m.mentions": dict(mentions or {}) if not sent else {}}
        if actual_thread:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": actual_thread}
        elif reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        try:
            event_id = await self.send_event(conversation, "m.room.message", content)
        except HelperError as exc:
            size = render.RENDERER.measure(part.markdown) if exc.code == "M_TOO_LARGE" else 0
            halves = render.RENDERER.parts(part.markdown, size // 2) if size else []
            if len(halves) < 2:
                raise
            logger.info("matrix server refused %d bytes as too large: sending them in %d parts", size, len(halves))
            for half in halves:
                await self._send_text(
                    conversation, thread, half, sent=sent, reply_to=reply_to, msgtype=msgtype, mentions=mentions
                )
            return sent[first:]
        sent.append(event_id)
        self._remember(event_id, self.user_id, actual_thread)
        return sent[first:]

    async def send_event(
        self, conversation: str, event_type: str, content: Mapping[str, Any], *, tries: int = SEND_TRIES
    ) -> str:
        """Send one event. A rate limit waits as long as the server asks, and the retry keeps the transaction id.

        After ``tries`` attempts the rate limit is raised, with the wait the server asked for.
        """
        result = await self._retrying("send", tries, room=conversation, type=event_type, content=dict(content))
        event_id = result.get("event_id")
        if not isinstance(event_id, str) or not EVENT_RE.fullmatch(event_id):
            raise HelperError("helper_invalid_response", "send response event_id must be a Matrix event ID starting with $")
        return event_id

    async def redact(self, conversation: str, event_id: str) -> None:
        """Remove one event of the bot's. A rate limit waits as for a send."""
        await self._retrying("redact", SEND_TRIES, room=conversation, event_id=event_id)

    async def _retrying(self, op: str, tries: int, **fields: Any) -> dict[str, Any]:
        """One request with a transaction id, tried again after each rate limit, ``tries`` times in all."""
        txn_id = f"belle-{uuid.uuid4().hex}"
        for attempt in range(1, tries + 1):
            if self._helper is None:
                raise HelperError(protocol.NOT_READY, "matrix is not started")
            try:
                return await self._helper.request(op, txn_id=txn_id, **fields)
            except HelperError as exc:
                if exc.code != protocol.RATE_LIMITED or attempt == tries:
                    raise
                await asyncio.sleep((exc.retry_after_ms or 1000) / 1000)
        raise AssertionError("the loop returns or raises")

    async def react(self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = "") -> None:
        """Add or remove the bot's reaction ``name``: a shortcode of the core emoji table, or an emoji character."""
        key = reaction_key(name)
        mine = (conversation, message_id, normalize_reaction_key(key))
        if remove:
            reaction = self._reactions.pop(mine, "")
            if reaction:
                await self.redact(conversation, reaction)
            return
        if mine not in self._reactions:
            relates = {"rel_type": "m.annotation", "event_id": message_id, "key": key}
            self._reactions[mine] = await self.send_event(conversation, "m.reaction", {"m.relates_to": relates})

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        """Show what became of a message with the reactions the settings name, and typing while its turn runs."""
        if state in TYPING_STARTS:
            self._typing_start(inbound)
        elif state in TYPING_ENDS:
            self._typing_stop(inbound)
        await react_marks(self.react, inbound.conversation, inbound.message_id, state, lambda key: self._settings.emoji.get(key, ""))

    def _typing_start(self, inbound: InboundMessage) -> None:
        room = inbound.conversation
        if not self._settings.typing_indicator or self._stopping:
            return
        self._typing.setdefault(room, set()).add(inbound.message_id)
        if room not in self._typing_tasks:
            self._typing_tasks[room] = asyncio.create_task(self._typing_loop(room), name="matrix-typing")

    def _typing_stop(self, inbound: InboundMessage) -> None:
        room = inbound.conversation
        turns = self._typing.get(room, set())
        turns.discard(inbound.message_id)
        if turns or room not in self._typing_tasks:
            return
        self._typing.pop(room, None)
        self._typing_tasks.pop(room).cancel()
        done = asyncio.create_task(self._call("typing", room=room, typing=False))
        self._background.add(done)
        done.add_done_callback(self._background.discard)

    async def _typing_loop(self, room: str) -> None:
        """Typing in a room while a turn of it runs: each notice lasts 30 s, and is sent again every 15 s."""
        while await self._call("typing", room=room, typing=True, timeout_ms=TYPING_TIMEOUT_MS) is not None:
            await asyncio.sleep(TYPING_REPEAT_SECONDS)

    # ── what a running turn shows ────────────────────────────────────────

    @property
    def shows(self) -> Mapping[str, str]:
        """Each kind of event of a running turn this connector shows as configured, with its setting."""
        return self.surfaces.shows

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Show one event of a running turn: its reply as it is written, or a tool call on its card."""
        await self.surfaces.progress(inbound, event)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End what was shown of a turn. Return whether the reply on screen became ``final``."""
        return await self.surfaces.conclude(inbound, state, final)

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str = "") -> str:
        """Ask a question as a poll in ``thread``. Return the poll's event id."""
        return await self.questions.ask(conversation, thread, question)

    async def _load_file(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Read one file of a message, decrypted by the helper, as the ``data:`` URI a turn and the second brain take."""
        found = entry[_FILE]
        try:
            data = await download(self.call, found, self.max_download_bytes)
        except FileOverLimit as over:
            raise AttachmentUnreadable(str(over), f"{FILE_REASON_TOO_LARGE}, {over.limit} bytes") from None
        except HelperError as exc:
            raise AttachmentUnreadable(str(exc), FILE_REASON_DOWNLOAD_FAILED) from None
        logger.info("matrix file: loaded %s (%d bytes)", found["name"], len(data))
        return {"name": found["name"], "uri": f"data:{found['mimetype']};base64,{base64.b64encode(data).decode('ascii')}"}

    async def fetch_files(self, inbound: InboundMessage) -> tuple[Mapping[str, Any], ...]:
        """Read the file of a message. One not read is left out, and a notice in its thread says why."""

        async def notify(notice: str) -> None:
            try:
                await self.post(inbound.conversation, inbound.reply_thread or "", notice)
            except HelperError as exc:
                logger.warning("matrix could not say which files were not read: %s", exc)

        return await fetch_message_files(
            [dict(entry) for entry in inbound.files if entry.get(_FILE)],
            self._load_file,
            name=lambda entry: str(entry["name"]),
            budget_seconds=FILE_BUDGET_SECONDS,
            logger=logger,
            label="Matrix file",
            failed_reason=FILE_REASON_DOWNLOAD_FAILED,
            conversation=inbound.conversation,
            notify=notify,
        )

    def render_events(self, held: Mapping[str, Any] | None, started_by: InboundEvent | None = None) -> str:
        """Write what a turn is told of events: the one that started it, and those its conversation kept."""
        return render_events(held, started_by)

    def toolkits(self, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
        """The tools that post, react, read, download, search, find and pin, each reading its turn through ``metadata_provider``."""
        return [MatrixResourcesToolkit(self, metadata_provider)]

    async def resolve_content_attachment(self, ref: str, metadata: Mapping[str, Any]) -> dict[str, str]:
        """Read a file for a content tool, by the read rule of the turn that ``metadata`` describes."""
        return await MatrixToolkit(self, lambda: metadata).content_attachment(ref)

    def request_metadata(self, inbound: InboundMessage) -> dict[str, Any]:
        """The message a turn's tool calls belong to: its account, its room, its thread, its id and its sender."""
        return {
            METADATA_ACCOUNT: self.workspace,
            METADATA_ROOM: inbound.conversation,
            METADATA_CHAT_TYPE: inbound.chat_type,
            METADATA_MESSAGE: inbound.message_id,
            METADATA_ROOT: inbound.thread,
            METADATA_USER: inbound.sender,
        }

    # ── decisions ────────────────────────────────────────────────────────

    def decision_facts(self, inbound: InboundMessage) -> dict[str, Any]:
        """What the decision gate is told of a message: its author, and the bot by its user id."""
        return inbound_decision_facts(
            inbound, author=inbound.sender, bot_name=bot_identity(self.user_id, self.user_id), kinds=_CONVERSATION_KINDS
        )

    async def recent_messages(self, conversation: str, limit: int, before: str, *, workspace: str = "") -> list[dict[str, Any]]:
        """The messages of a room before the event ``before``, the bot's own included, oldest first.

        A message the bot holds no key for is an entry that says so.
        """
        read = await read_room(self.call, conversation, before=before, limit=max(1, int(limit)), after=None, names={}, bot=self.user_id)
        return [
            message_entry(
                record["author_user_id"],
                UNDECRYPTABLE_ENTRY if record.get("undecryptable") else record["text"],
                ts=record["message_id"],
                thread_ts=record["root_message_id"],
            )
            for record in read.records
        ]
