"""Record outbound actions and accept injected inbound records."""

import asyncio
import contextlib
import itertools
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent.audience import Audience, audience_newcomers, write_permitted
from belle_agent.backend import (
    APPROVAL,
    EVENT_TEXT_DELTA,
    PROGRESS_EVENTS,
    ContentPart,
    Question,
    ToolContent,
    TurnEvent,
)
from belle_agent.message_decisions import KIND_DIRECT, KIND_PRIVATE
from belle_agent.platforms.base import (
    EventKind,
    InboundAnswer,
    InboundDrop,
    InboundEvent,
    InboundMessage,
    InboundStop,
    OnInbound,
    PlaceCheck,
    PlatformAffordances,
)
from belle_agent.platforms.decision_facts import inbound_decision_facts
from belle_agent.platforms.loopback import control
from belle_agent.platforms.marks import reaction_changes
from belle_agent.scopes import AXIS_CHAT_TYPE, PER_CONVERSATION, ChannelCapabilities

NAME = "loopback"

POST_MESSAGE = "post_message"
REACT_TO_MESSAGE = "react_to_message"
READ_FILE = "read_file"

CHAT_TYPE_DM = "dm"
CHAT_TYPE_CHANNEL = "channel"

KIND_POST = "post"
KIND_EPHEMERAL = "ephemeral"
KIND_REACTION = "reaction"
KIND_QUESTION = "question"
KIND_APPROVAL = "approval"

METADATA_CONVERSATION_KEY = "loopback_conversation"
METADATA_WORKSPACE_KEY = "loopback_workspace"
METADATA_THREAD_KEY = "loopback_thread"
METADATA_MESSAGE_KEY = "loopback_message_id"
METADATA_SENDER_KEY = "loopback_sender"

LOOPBACK_CAPABILITIES = ChannelCapabilities(
    channel=NAME,
    axes=frozenset({"channel", "chat", "chat_type", "user", "workspace"}),
    sections={
        "delivery": frozenset({"mode", "prompt", "mid_turn", "session", "reply", "events"}),
        "agent": frozenset({"model_name", "expert", "subagents", "skills"}),
        "permissions": None,
        "clicks": frozenset({"approve", "stop", "queue_drop"}),
    },
    # ``ask`` renders an approval prompt, so an ask stays an ask.
    attended=True,
    identity_keys=("user",),
    axis_values={AXIS_CHAT_TYPE: frozenset({CHAT_TYPE_DM, CHAT_TYPE_CHANNEL})},
    axis_restrictions={
        "delivery.session": PER_CONVERSATION,
        "delivery.reply": PER_CONVERSATION,
        "delivery.events": PER_CONVERSATION,
    },
)

#: The kinds of event an injected record may name. A visit is nothing a turn could answer.
EVENT_KINDS: Mapping[str, EventKind] = {
    "reaction": EventKind(held=30, turns=True, anchored=True),
    "pin": EventKind(held=10, turns=True, anchored=True, waits=True),
    "member": EventKind(held=20, turns=True, waits=True),
    "app_home": EventKind(held=5),
}


@dataclass(frozen=True)
class Outbound:
    """One action this platform was asked to perform."""

    kind: str
    conversation: str
    thread: str = ""
    text: str = ""
    message_id: str = ""
    #: The one person an ephemeral post is shown to.
    to: str = ""
    #: The reaction name.
    name: str = ""
    removed: bool = False
    question: Question | None = None
    #: The workspace the host named for the action, or empty where a tool acted.
    workspace: str = ""


def _refusal(code: str, detail: str) -> str:
    return json.dumps({"ok": False, "error": code, "detail": detail})


PlaceKey = tuple[str, str]
MessageKey = tuple[str, str, str]


class LoopbackToolkit:
    """Post in the origin or a confirmed destination. React in the origin."""

    def __init__(self, platform: "LoopbackPlatform", metadata_provider: Callable[[], Mapping[str, Any]]) -> None:
        self._platform = platform
        self._metadata_provider = metadata_provider

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": POST_MESSAGE,
                "description": (
                    "Post one message in this conversation or a confirmed destination. "
                    "The result includes chat_id and message_id."
                ),
                "input_params": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string", "description": "Message text."},
                        "chat_id": {
                            "type": "string",
                            "description": "Omit for this conversation. Another confirmable conversation requires approval.",
                        },
                        "reply_to_message_id": {
                            "type": "string",
                            "description": "Message ID to reply to in its thread. Omit it to post where this turn began.",
                        },
                    },
                },
            },
            {
                "name": REACT_TO_MESSAGE,
                "description": "Add or remove one reaction on a message in this conversation, without posting.",
                "input_params": {
                    "type": "object",
                    "required": ["emoji"],
                    "properties": {
                        "emoji": {"type": "string", "description": "Reaction name."},
                        "message_id": {
                            "type": "string",
                            "description": "Omit it for the message that started this turn.",
                        },
                        "remove": {"type": "boolean", "default": False, "description": "Remove the reaction."},
                    },
                },
            },
            {
                "name": READ_FILE,
                "description": "Return one file this platform holds, by its name. Its content comes beside the text.",
                "input_params": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string", "description": "The file's name."}},
                },
            },
        ]

    async def widening_question(self, name: str, arguments: Mapping[str, Any]) -> str | None:
        """Say what a person must agree to before a post leaves the conversation its turn came from."""
        metadata, origin = self._origin()
        workspace = str(metadata.get(METADATA_WORKSPACE_KEY) or "")
        target = str(arguments.get("chat_id") or "").strip()
        if name != POST_MESSAGE or target in ("", origin) or (workspace, target) not in self._platform.confirmable:
            return None
        return f"This would be posted in {target}, which is read by people who cannot read this conversation. Post it there?"

    def _origin(self) -> tuple[Mapping[str, Any], str]:
        metadata = self._metadata_provider()
        return metadata, str(metadata.get(METADATA_CONVERSATION_KEY) or "").strip()

    async def post_message(self, text: str, chat_id: str | None = None, reply_to_message_id: str | None = None) -> str:
        """Post one message, and say where it landed."""
        if not isinstance(text, str):
            return _refusal("text_invalid", "text must be a string")
        for field, value in (("chat_id", chat_id), ("reply_to_message_id", reply_to_message_id)):
            if value is not None and not isinstance(value, str):
                return _refusal(f"{field}_invalid", f"{field} must be a string")
        metadata, origin = self._origin()
        workspace = str(metadata.get(METADATA_WORKSPACE_KEY) or "")
        if not origin:
            return _refusal("trusted_conversation_context_required", "this call belongs to no conversation")
        target = str(chat_id or "").strip() or origin
        # A conversation a person may agree to: reaching here means the host asked, and the answer was yes.
        if target != origin and (workspace, target) not in self._platform.confirmable:
            return _refusal("outside_this_conversation", "this platform posts only where the turn came from")
        if not text.strip():
            return _refusal("nothing_to_post", "no text was given")
        body = text
        requested = str(reply_to_message_id or "").strip()
        if requested:
            thread = self._platform.reply_thread(workspace, target, requested)
            if thread is None:
                return _refusal(
                    "reply_target_not_found",
                    f"reply_to_message_id {requested!r} does not belong to workspace {workspace!r}, "
                    f"conversation {target!r}",
                )
        elif target == origin:
            thread = str(metadata.get(METADATA_THREAD_KEY) or "")
        else:
            thread = ""
        message_id = await self._platform.post(target, thread, body, workspace=workspace)
        return json.dumps({
            "ok": True,
            "chat_id": target,
            "message_id": message_id,
            "part_message_ids": [message_id],
            "parts": 1,
            "thread_id": thread,
        })

    async def read_file(self, name: str) -> str | ToolContent:
        """Return one file this platform holds, as content beside the text that names it."""
        held = self._platform.files.get(str(name or ""))
        if held is None:
            return _refusal("file_not_found", "this platform holds no file of that name")
        mime, data = held
        described = json.dumps({"ok": True, "name": name, "mimetype": mime, "size_bytes": len(data)})
        return ToolContent(text=described, parts=(ContentPart(mime=mime, data=data, name=name),))

    async def react_to_message(self, emoji: str, message_id: str | None = None, remove: bool = False) -> str:
        """Add or remove one reaction on one message in this conversation."""
        if not isinstance(emoji, str):
            return _refusal("emoji_invalid", "emoji must be a string")
        if message_id is not None and not isinstance(message_id, str):
            return _refusal("message_id_invalid", "message_id must be a string")
        if not isinstance(remove, bool):
            return _refusal("remove_invalid", "remove must be a boolean")
        metadata, origin = self._origin()
        if not origin:
            return _refusal("trusted_conversation_context_required", "this call belongs to no conversation")
        target = str(message_id or "").strip() or str(metadata.get(METADATA_MESSAGE_KEY) or "")
        name = str(emoji or "").strip().strip(":")
        if not target or not name:
            return _refusal("message_and_emoji_required", "a reaction needs a message and a name")
        workspace = str(metadata.get(METADATA_WORKSPACE_KEY) or "")
        if self._platform.reply_thread(workspace, origin, target) is None:
            return _refusal(
                "reaction_target_not_found",
                f"message_id {target!r} does not belong to workspace {workspace!r}, conversation {origin!r}",
            )
        await self._platform.react(
            origin, target, name, remove=bool(remove), workspace=workspace,
        )
        return json.dumps({"ok": True, "chat_id": origin, "message_id": target, "emoji": name, "removed": bool(remove)})


class LoopbackPlatform:
    """Deliver injected records to the host and log what the host asks for."""

    name = NAME
    posting_tool = POST_MESSAGE
    capabilities = LOOPBACK_CAPABILITIES
    #: No conversation here is public: the platform has no workspace to be open to.
    public_chat_types: frozenset[str] = frozenset()
    direct_chat_types = frozenset({CHAT_TYPE_DM})
    #: Every kind of progress event is kept. No setting turns one on.
    shows: Mapping[str, str] = {kind: NAME for kind in PROGRESS_EVENTS}
    event_kinds = EVENT_KINDS

    ephemeral_posts = True
    # In-process tests use the unnamed workspace; the control socket names local.
    workspaces: frozenset[str] = frozenset({"", control.WORKSPACE})
    #: Loopback records every question and post without a UI limit.
    affordances = PlatformAffordances(questions="present")

    def decision_facts(self, inbound: InboundMessage) -> dict[str, Any]:
        """Describe the original injected message through the shared decision contract."""
        return inbound_decision_facts(
            inbound, author=inbound.sender, bot_name="the assistant",
            kinds={CHAT_TYPE_DM: KIND_DIRECT, CHAT_TYPE_CHANNEL: KIND_PRIVATE},
        )

    def command_prefix(self, workspace: str) -> str:
        return "/belle"

    def __init__(self, control_path: str = "") -> None:
        #: Every outbound action, in the order it was asked for.
        self.log: list[Outbound] = []
        #: What the host said became of each message, as ``(message id, state)``, in order.
        self.marks: list[tuple[str, str]] = []
        #: Semantic reaction changes, using setting names as platform-independent emoji names.
        self.mark_changes: list[tuple[str, tuple[tuple[str, bool], ...]]] = []
        #: What each running turn reported, as ``(message id, event)``, in order.
        self.progressed: list[tuple[str, TurnEvent]] = []
        #: How the host ended what each turn showed, as ``(message id, state, final)``, in order.
        self.concluded: list[tuple[str, str, str]] = []
        # The messages whose reply was shown while it was written.
        self._replies: set[str] = set()
        #: The files ``read_file`` returns, by name, each as its media type and its bytes.
        self.files: dict[str, tuple[str, bytes]] = {}
        #: Workspace and conversation pairs a turn may post in once a person agrees. Every other is refused.
        self.confirmable: set[PlaceKey] = set()
        self.audiences: dict[PlaceKey, Audience] = {}
        self.joined: set[PlaceKey] = set()
        self.threads: set[tuple[str, str, str]] = set()
        self.direct_messages: dict[tuple[str, str], str] = {}
        self.chat_types: dict[PlaceKey, str] = {}
        self._message_threads: dict[MessageKey, str] = {}
        self._on_inbound: OnInbound | None = None
        self._message_ids = itertools.count(1)
        #: Where a person drives this platform by hand, or empty. See ``control``.
        self._control_path = control_path
        self._control: asyncio.AbstractServer | None = None

    def bind(self, host: Any) -> None:
        """This platform gives people no commands."""

    async def start(self, on_inbound: OnInbound) -> None:
        self._on_inbound = on_inbound
        if self._control_path:
            self._control = await control.serve(self._control_path, self._inject_by_hand, lambda: self.log)

    async def stop(self) -> None:
        self._on_inbound = None
        if self._control is not None:
            self._control.close()
            await self._control.wait_closed()
            self._control = None
            with contextlib.suppress(OSError):
                os.unlink(self._control_path)

    async def _inject_by_hand(self, fields: Mapping[str, Any]) -> str:
        chat_type = CHAT_TYPE_DM if fields.get("chat_type") == CHAT_TYPE_DM else CHAT_TYPE_CHANNEL
        if chat_type == CHAT_TYPE_DM:
            sender = str(fields["sender"])
            self.direct_messages[(control.WORKSPACE, sender)] = str(fields["conversation"])
            await self.open_direct_message(sender, workspace=control.WORKSPACE)
        return await self.inject(control.inbound(NAME, chat_type, fields))

    async def inject(self, record: InboundMessage | InboundAnswer | InboundStop | InboundDrop | InboundEvent) -> str:
        """Deliver one inbound record as a connected platform would. Return the host's outcome."""
        if self._on_inbound is None:
            return "dropped:platform-not-started"
        if isinstance(record, InboundMessage):
            key = (record.workspace, record.conversation)
            if record.chat_type in {CHAT_TYPE_DM, CHAT_TYPE_CHANNEL}:
                self.chat_types[key] = record.chat_type
            root = record.thread or record.message_id
            self.remember_message(record.workspace, record.conversation, record.message_id, root)
        elif isinstance(record, InboundEvent) and record.anchor:
            self.remember_message(
                record.workspace, record.conversation, record.anchor, record.anchor_thread or record.anchor,
            )
        return await self._on_inbound(record)

    def _record(self, kind: str, conversation: str, **fields: Any) -> str:
        message_id = fields.pop("message_id", "") or f"out-{next(self._message_ids)}"
        self.log.append(Outbound(kind=kind, conversation=conversation, message_id=message_id, **fields))
        if kind in {KIND_POST, KIND_EPHEMERAL, KIND_QUESTION, KIND_APPROVAL}:
            self.remember_message(
                str(fields.get("workspace") or ""), conversation, message_id, str(fields.get("thread") or ""),
            )
        return message_id

    def remember_message(self, workspace: str, conversation: str, message_id: str, thread: str) -> None:
        """Remember the destination and root of an inbound or outbound message."""
        key = (workspace, conversation)
        root = thread or message_id
        self._message_threads[(*key, message_id)] = root
        self._message_threads.setdefault((*key, root), root)
        self.threads.add((*key, root))

    def reply_thread(self, workspace: str, conversation: str, message_id: str) -> str | None:
        """Resolve a message only in the workspace and conversation that owns it."""
        return self._message_threads.get((workspace, conversation, message_id))

    async def post(
        self, conversation: str, thread: str, text: str, *, ephemeral_to: str | None = None, workspace: str = ""
    ) -> str:
        if ephemeral_to:
            return self._record(KIND_EPHEMERAL, conversation, thread=thread, text=text, to=ephemeral_to, workspace=workspace)
        return self._record(KIND_POST, conversation, thread=thread, text=text, workspace=workspace)

    async def react(
        self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = ""
    ) -> None:
        self._record(KIND_REACTION, conversation, message_id=message_id, name=name, removed=remove, workspace=workspace)

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str = "") -> str:
        kind = KIND_APPROVAL if question.kind == APPROVAL else KIND_QUESTION
        return self._record(kind, conversation, thread=thread, text=question.text, question=question, workspace=workspace)

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        self.marks.append((inbound.message_id, state))
        self.mark_changes.append((inbound.message_id, reaction_changes(state, lambda key: key)))

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        self.progressed.append((inbound.message_id, event))
        if event.kind == EVENT_TEXT_DELTA:
            self._replies.add(inbound.message_id)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """Keep how a turn's showing ended. A reply that was shown as it was written becomes the post of its final."""
        self.concluded.append((inbound.message_id, state, final))
        shown = inbound.message_id in self._replies
        self._replies.discard(inbound.message_id)
        if not (shown and final):
            return False
        thread = inbound.thread if inbound.reply_thread is None else inbound.reply_thread
        self._record(KIND_POST, inbound.conversation, thread=thread, text=final, workspace=inbound.workspace)
        return True

    async def fetch_files(self, inbound: InboundMessage) -> tuple[Mapping[str, Any], ...]:
        return inbound.files

    def render_events(self, held: Mapping[str, Any] | None, started_by: InboundEvent | None = None) -> str:
        parts = []
        if started_by is not None:
            # The ids the tools take, so the turn can answer the message the event is about.
            about = {"message_id": started_by.anchor, "thread": started_by.anchor_thread} if started_by.anchor else {}
            parts.append(f"[event]\n{json.dumps({**started_by.record, **about})}")
        if held:
            parts.append(f"[events]\n{json.dumps(held)}")
        return "\n\n".join(parts)

    def toolkits(self, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
        return [LoopbackToolkit(self, metadata_provider)]

    def request_metadata(self, inbound: InboundMessage) -> dict[str, Any]:
        return {
            METADATA_WORKSPACE_KEY: inbound.workspace,
            METADATA_CONVERSATION_KEY: inbound.conversation,
            METADATA_THREAD_KEY: inbound.thread,
            METADATA_MESSAGE_KEY: inbound.message_id,
            METADATA_SENDER_KEY: inbound.sender,
        }

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck:
        key = (workspace, conversation)
        if (workspace == control.WORKSPACE and conversation.startswith("D-") and len(conversation) > 2
                and thread in ("", "new")):
            return PlaceCheck(True, True, chat_type=CHAT_TYPE_DM)
        if key not in self.audiences:
            return PlaceCheck(False, False, f"conversation {conversation} does not exist")
        if thread not in ("", "new") and (*key, thread) not in self.threads:
            return PlaceCheck(False, key in self.joined, f"thread {thread} does not exist")
        if key not in self.joined:
            return PlaceCheck(True, False, f"bot is not a member of {conversation}", self.chat_types.get(key, ""))
        return PlaceCheck(True, True, chat_type=self.chat_types.get(key, ""))

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        if user and workspace == control.WORKSPACE and conversation == f"D-{user}":
            return True
        audience = self.audiences.get((workspace, conversation))
        return bool(audience and (audience.workspace_wide or user in audience.members))

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None:
        source, reached = self.audiences[(workspace, origin)], self.audiences[(workspace, target)]
        return () if write_permitted(source, reached) else audience_newcomers(reached, source)

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str:
        conversation = self.direct_messages.setdefault((workspace, user), f"dm:{user}")
        key = (workspace, conversation)
        self.audiences[key] = Audience(False, frozenset({user}), False)
        self.joined.add(key)
        self.chat_types[key] = CHAT_TYPE_DM
        return conversation

    def mention(self, user: str) -> str:
        return user
