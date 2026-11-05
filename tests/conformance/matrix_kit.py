"""The Matrix platform's kit for the conformance suite.

The platform under test is the Matrix connector, built from its section like
any other. The helper is the in-process peer of ``tests/matrix/peer.py``: a
check's inbound record becomes the timeline event the helper would hand over,
and the log is read from the requests the connector made of the peer. No
process starts and no server is reached.

The kit turns the connector's reactions and typing off. With the defaults, a
message that starts a turn gets a reaction, so no check could find a
conversation untouched. ``tests/matrix`` checks the marks.
"""

import dataclasses
import itertools
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1] / "matrix"))

from conformance_kit import with_section
from peer import USER, Peer, Peers, Room

from belle_agent.backend import APPROVAL, Question, read_data_uri
from belle_agent.emoji import GLYPH_SHORTCODES, VARIATION_SELECTORS
from belle_agent.host import ConversationHost
from belle_agent.platforms.base import (
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_REPLY,
    InboundAnswer,
    InboundEvent,
    InboundMessage,
    InboundStop,
)
from belle_agent.platforms.loopback.platform import Outbound
from belle_agent.platforms.matrix import connector, surfaces
from belle_agent.platforms.matrix.questions import POLL_RESPONSE, POLL_START
from belle_agent.platforms.matrix.settings import MARK_EMOJI, PASSPHRASE_ENV
from belle_agent.tools.host import APPROVE, REJECT

NAME = "matrix"
#: What the connector adds to a turn's text: the ids its tools take, and the trigger.
_ENVELOPE = re.compile(r"\n\n\[chat_id: [^\]]*\]( \[trigger: \w+\])?\Z")
#: The checks' own chat types, in Matrix's words.
_CHAT_TYPES = {"dm": "direct", "channel": "private"}
_SEQ = itertools.count(1)
#: How the text of an activity card starts.
_CARD_TITLES = ("**Working", "**Turn ")


class MatrixDriver:
    """Deliver records as the helper would, and read back what the connector asked the helper to do.

    With several accounts, a record goes to the account its workspace names, or to the first, and each
    action in the log names the workspace of the account that made it.
    """

    def __init__(self, host: ConversationHost, peers: Peers) -> None:
        self._platform = host.platforms[NAME]
        members = getattr(self._platform, "members", {"": self._platform})
        #: The connector and the peer of each account, by its name. The first serves any other workspace.
        self._accounts = {name: (member, peers.peers[member._settings.user_id]) for name, member in members.items()}
        self._peers = peers
        self._named = len(self._accounts) > 1
        #: Each question the connector asked, by the event id of its poll.
        self._questions: dict[str, Question] = {}
        ask = self._platform.ask

        async def remember(conversation: str, thread: str, question: Question, **where: Any) -> str:
            poll = await ask(conversation, thread, question, **where)
            self._questions[poll] = question
            return str(poll)

        self._platform.ask = remember
        #: What the host handed the connector of each running turn, and how it ended what the connector showed.
        self.progressed: list[tuple[str, Any]] = []
        self.concluded: list[tuple[str, str, str]] = []
        progress, conclude = self._platform.progress, self._platform.conclude

        async def handed(inbound: InboundMessage, event: Any) -> None:
            self.progressed.append((inbound.message_id, event))
            await progress(inbound, event)

        async def ended(inbound: InboundMessage, state: str, final: str = "") -> bool:
            self.concluded.append((inbound.message_id, state, final))
            return bool(await conclude(inbound, state, final))

        self._platform.progress, self._platform.conclude = handed, ended

    def _at(self, workspace: str) -> tuple[Any, Peer]:
        return self._accounts.get(workspace) or next(iter(self._accounts.values()))

    @property
    def log(self) -> list[Outbound]:
        seen: list[Outbound] = []
        names = {id(peer): name for name, (_member, peer) in self._accounts.items()}
        for peer, request in self._peers.journal:
            workspace = names[id(peer)] if self._named else ""
            if request["op"] == "redact":
                # A message taken down is no message.
                seen = [entry for entry in seen if entry.kind == "reaction" or entry.message_id != request["event_id"]]
                continue
            if request["op"] != "send":
                continue
            content = request["content"]
            relates = content.get("m.relates_to") or {}
            if relates.get("rel_type") == "m.replace":
                # An edit changes the text of what was sent, as a client shows it.
                body = content["m.new_content"]["body"]
                kind = "activity" if body.startswith(_CARD_TITLES) else None
                seen = [dataclasses.replace(entry, text=body, kind=kind or entry.kind)
                        if entry.message_id == relates["event_id"] else entry for entry in seen]
                continue
            if request["type"] == "m.reaction":
                # The log names a reaction as the checks do: by its shortcode.
                key = "".join(char for char in relates["key"] if char not in VARIATION_SELECTORS)
                name = GLYPH_SHORTCODES.get(key, relates["key"])
                seen.append(Outbound("reaction", request["room"], message_id=relates["event_id"], name=name, workspace=workspace))
            elif request["type"] in ("m.room.message", POLL_START):
                thread = relates.get("event_id", "") if relates.get("rel_type") == "m.thread" else ""
                sent = next((event_id for event_id, event in peer.events.items() if event["content"] is content), "")
                if (question := self._questions.get(sent)) is not None:
                    kind = "approval" if question.kind == APPROVAL else "question"
                    seen.append(Outbound(kind, request["room"], thread=thread, text=question.text, message_id=sent, question=question,
                                         workspace=workspace))
                elif content.get("m.mentions") == {} and relates.get("m.in_reply_to"):
                    # What the connector says of a person's act, in reply to it: who answered a poll, or why a vote
                    # or a stop did not count.
                    seen.append(Outbound("note", request["room"], thread=thread, text=content["body"], message_id=sent, workspace=workspace))
                elif request["type"] == "m.room.message":
                    kind = "activity" if content["body"].startswith(_CARD_TITLES) else "post"
                    seen.append(Outbound(kind, request["room"], thread=thread, text=content["body"], message_id=sent, workspace=workspace))
        return seen

    async def _vote(self, record: InboundAnswer) -> str:
        """Vote on a poll as a person does."""
        platform, peer = self._at(record.workspace)
        # A vote relates to an event of its own room. A question asked elsewhere gets a poll of the bot's there with no record.
        poll = next((poll for poll, question in self._questions.items()
                     if question.id == record.question_id and peer.room_of(poll) == record.conversation), "")
        if not poll:
            answers = [{"id": "0", "org.matrix.msc1767.text": "Approve"}, {"id": "1", "org.matrix.msc1767.text": "Reject"}]
            poll = peer.add(record.conversation, peer.user, {POLL_START: {"answers": answers}}, event_type=POLL_START)
            options: tuple[str, ...] = (APPROVE, REJECT)
        else:
            options = self._questions[poll].options
        value = (APPROVE if record.answer.approved else REJECT) if record.answer.approved is not None else record.answer.values[0]
        content = {"m.relates_to": {"rel_type": "m.reference", "event_id": poll}, POLL_RESPONSE: {"answers": [str(options.index(value))]}}
        event = {"event": "timeline", "seq": next(_SEQ), "room": record.conversation, "encrypted": True, "direct": False,
                 "room_event": {"type": POLL_RESPONSE, "sender": record.sender, "event_id": f"$vote{next(_SEQ)}",
                                "origin_server_ts": int(time.time() * 1000), "content": content}}
        return str(await platform.handle(event))

    async def _stop(self, record: InboundStop) -> str:
        """React with ⏹️ to the message whose turn is to stop, as a person does."""
        platform, _peer = self._at(record.workspace)
        content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": record.message_id, "key": surfaces.STOP_REACTION}}
        event = {"event": "timeline", "seq": next(_SEQ), "room": record.conversation, "encrypted": False, "direct": False,
                 "room_event": {"type": "m.reaction", "sender": record.sender, "event_id": f"$stop{next(_SEQ)}",
                                "origin_server_ts": int(time.time() * 1000), "content": content}}
        return str(await platform.handle(event))

    async def _event(self, record: InboundEvent) -> str:
        """The room event a server sends for a reaction, a member or a pin. A bot's own carries the bot's id."""
        platform, peer = self._at(record.workspace)
        actor = peer.user if record.actor_is_bot else record.actor
        direct = record.direct or record.chat_type == "dm"
        peer.rooms.setdefault(record.conversation, Room(encrypted=direct, direct=direct, joined_members=2 if direct else 3))
        if record.anchor:
            relates = {"rel_type": "m.thread", "event_id": record.anchor_thread} if record.anchor_thread not in ("", record.anchor) else {}
            peer.events.setdefault(record.anchor, {"type": "m.room.message", "sender": "@someone:syn.example",
                                                         "event_id": record.anchor, "content": {"body": "x", "m.relates_to": relates}})
        state: dict[str, Any] = {}
        if record.kind == "reaction":
            event_type, content = "m.reaction", {"m.relates_to": {"rel_type": "m.annotation", "event_id": record.anchor, "key": "\N{PARTY POPPER}"}}
        elif record.kind == "member":
            event_type, content = "m.room.member", {"membership": "join"}
            state = {"state_key": actor, "unsigned": {"prev_content": {"membership": "leave"}}}
        elif record.kind == "pin":
            event_type, content = "m.room.pinned_events", {"pinned": [record.anchor] if record.anchor else []}
            state = {"state_key": "", "unsigned": {"prev_content": {"pinned": []}}}
        else:
            raise NotImplementedError(f"the Matrix connector reports no {record.kind} event")
        event = {"event": "timeline", "seq": next(_SEQ), "room": record.conversation, "encrypted": False, "direct": direct,
                 "room_event": {"type": event_type, "sender": actor, "event_id": record.event_id,
                                "origin_server_ts": int(time.time() * 1000), "content": content, **state}}
        return str(await platform.handle(event))

    async def inject(self, record: Any) -> str:
        if isinstance(record, InboundAnswer):
            return await self._vote(record)
        if isinstance(record, InboundStop):
            return await self._stop(record)
        if isinstance(record, InboundEvent):
            return await self._event(record)
        if not isinstance(record, InboundMessage):
            raise NotImplementedError(f"the Matrix connector receives no {type(record).__name__} yet")
        platform, peer = self._at(record.workspace)
        direct = record.trigger == TRIGGER_DM or record.chat_type in ("dm", "direct")
        # A room of the checks is not encrypted, so that the second brain's * and public entries can select it.
        peer.rooms.setdefault(
            record.conversation,
            Room(encrypted=direct, public=record.chat_type == "public", direct=direct, joined_members=2 if direct else 3),
        )
        content: dict[str, Any] = {"msgtype": "m.notice" if record.sender_is_bot else "m.text", "body": record.text}
        if len(record.files) > 1:
            raise NotImplementedError("a Matrix message carries one file")
        for entry in record.files:
            mime, data = read_data_uri(str(entry["uri"]))
            uri = f"mxc://syn.example/{len(peer.media)}"
            peer.media[uri] = data
            content = {"msgtype": "m.file", "body": str(entry["name"]), "url": uri, "info": {"mimetype": mime, "size": len(data)}}
        if record.trigger == TRIGGER_MENTION:
            content["m.mentions"] = {"user_ids": [peer.user]}
        if record.thread:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": record.thread}
            author = peer.user if record.trigger == TRIGGER_REPLY else "@someone:syn.example"
            peer.events.setdefault(record.thread, {"type": "m.room.message", "sender": author, "event_id": record.thread,
                                                         "content": {"body": "root"}})
        event_id = record.message_id
        if record.edited:
            peer.events.setdefault(record.message_id, {"type": "m.room.message", "sender": record.sender,
                                                             "event_id": record.message_id, "content": dict(content)})
            content = {"body": f"* {record.text}", "m.new_content": content,
                       "m.relates_to": {"rel_type": "m.replace", "event_id": record.message_id}}
            event_id = record.event_id or f"{record.message_id}-edit"
        event = {"event": "timeline", "seq": next(_SEQ), "room": record.conversation, "encrypted": True, "direct": direct,
                 "room_event": {"type": "m.room.message", "sender": record.sender, "event_id": event_id,
                                "origin_server_ts": int(time.time() * 1000), "content": content}}
        return str(await platform.handle(event))


class MatrixKit:
    name = NAME
    #: The settings that turn on all the connector shows of a running turn. The card is due at once, and the
    #: spacing of its edits is taken out: a check reads what is shown, not when.
    shows = "platforms: {matrix: {enable_streaming: true, activity_card: true, activity_card_delay_seconds: 0, activity_card_min_edit_seconds: 0}}\n"

    def __init__(self, monkeypatch: Any) -> None:
        self.peer = Peer()
        self.peers = Peers(self.peer)
        monkeypatch.setenv(PASSPHRASE_ENV, "conformance-passphrase")
        monkeypatch.setattr(connector, "Helper", self.peer.factory)
        monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.0)

    def configure(self, settings: str) -> str:
        text = re.sub(r"\bloopback\b", NAME, settings)
        text = re.sub(r"\bchat_type: (dm|channel)\b", lambda found: f"chat_type: {_CHAT_TYPES[found[1]]}", text)
        defaults = {"homeserver": "http://matrix.invalid", "user_id": USER, "typing_indicator": False,
                    **{key: "disabled" for key in MARK_EMOJI}}
        return with_section(text, NAME, defaults=defaults)

    def attach(self, host: ConversationHost) -> MatrixDriver:
        return MatrixDriver(host, self.peers)

    def said(self, text: str) -> str:
        return _ENVELOPE.sub("", text)

    def widens(self, platform: Any, origin: str, target: str) -> None:
        raise NotImplementedError(
            "a check names its conversations C1 and C2, which are no Matrix room ids: the posting tool refuses"
            " chat_id C2 as malformed, so the post a person agrees to would never be made"
        )

    def holds(self, platform: Any, name: str, mime: str, data: bytes) -> tuple[str, dict[str, Any]]:
        uri = f"mxc://syn.example/{len(self.peer.media)}"
        self.peer.media[uri] = data
        self.peer.rooms.setdefault("C1", Room(encrypted=False))
        event_id = self.peer.add("C1", "@someone:syn.example", {"msgtype": "m.file", "body": name, "url": uri, "info": {"mimetype": mime}})
        return "download_file", {"file_id": event_id}


class MatrixAccountsKit(MatrixKit):
    """Two accounts behind one connector, as ``platforms.matrix.accounts`` lists them.

    The accounts are named ``W1`` and ``W2``, so a check's workspace ``W1`` is the first account and ``W2`` the
    second. Each account has a helper of its own, with event ids of its own, so that nothing a check reads can
    come from the wrong account by chance.
    """

    ACCOUNTS = (("W1", USER), ("W2", "@belle:two.example"))

    def __init__(self, monkeypatch: Any) -> None:
        super().__init__(monkeypatch)
        self.peers = Peers(self.peer, Peer(user=self.ACCOUNTS[1][1], _ids=itertools.count(1_000_001)))
        monkeypatch.setattr(connector, "Helper", self.peers.factory)

    def configure(self, settings: str) -> str:
        accounts = [
            {"name": name, "homeserver": f"http://{name.lower()}.invalid", "user_id": user, "store_passphrase_env": PASSPHRASE_ENV}
            for name, user in self.ACCOUNTS
        ]
        return with_section(super().configure(settings), NAME, defaults={}, forced={"homeserver": "", "user_id": "", "accounts": accounts})
