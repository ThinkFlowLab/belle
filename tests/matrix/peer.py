"""An in-process stand-in for the helper: the surface of ``Helper``, the messages of the protocol, no process.

It holds rooms, events and what was sent, and checks each request and each answer against
``protocol.py``. ``delay`` makes each answer wait, as a server does: a peer that answers at once
hides an ordering defect.
"""

import asyncio
import base64
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from belle_agent.platforms.matrix import protocol
from belle_agent.platforms.matrix.helper import HelperError

USER = "@belle:syn.example"


@dataclass
class Room:
    encrypted: bool = True
    public: bool = False
    direct: bool = False
    joined_members: int = 3
    name: str | None = None
    alias: str | None = None


@dataclass
class Peer:
    """The helper as the platform sees it. ``requests`` is every request, in order, without its id."""

    on_event: Callable[[dict[str, Any]], None] = print
    delay: float = 0.0
    rooms: dict[str, Room] = field(default_factory=dict)
    #: Each event the peer can read back, by id: those sent, and those a check puts here.
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    #: An error code to answer each ``send`` with, once each, before sending succeeds.
    send_errors: list[tuple[str, int | None]] = field(default_factory=list)
    #: The most bytes of ``body`` plus ``formatted_body`` a sent event may hold, or 0 for no limit.
    max_bytes: int = 0
    #: Each room's events, oldest first, as ``messages`` and ``relations`` read them. A sent event joins its room.
    timeline: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: The events the peer holds no key for, by id.
    undecryptable: set[str] = field(default_factory=set)
    #: Each room's joined and invited members, as ``members`` gives them. Without an entry: the bot alone.
    members: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: The direct room with each person, by user id.
    dms: dict[str, str] = field(default_factory=dict)
    #: The rooms the account joined, or ``None`` for every room.
    joined: set[str] | None = None
    #: Each file's bytes, by its mxc URI, as ``download`` reads them: plain, or decrypted where the event gives a ``file``.
    media: dict[str, bytes] = field(default_factory=dict)
    #: The server's upload limit, as ``start`` answers it, or ``None`` where the server does not say.
    max_upload_bytes: int | None = None
    #: Each state event of a room, by room, type and state key, as ``state_get`` gives it.
    state: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    #: The rooms where ``state_put`` is refused with ``M_FORBIDDEN``, as a server refuses a power level too low.
    state_forbidden: set[str] = field(default_factory=set)
    #: The server's user directory, as ``user_directory`` searches it: ``user_id`` and ``display_name``.
    directory: list[dict[str, Any]] = field(default_factory=list)
    #: The server's public room directory, as ``public_rooms`` pages it: ``PublicRoomsChunk`` objects.
    public_directory: list[dict[str, Any]] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    hierarchy: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    hello: dict[str, Any] = field(default_factory=lambda: {"event": "hello", "protocol": protocol.PROTOCOL})
    #: The account the peer is the helper of.
    user: str = USER
    #: Whether ``start`` logs in as a new device, which ``log_out`` may undo, or restores the device of a store.
    new_device: bool = False
    #: Whether ``log_out`` logged the new device out.
    logged_out: bool = False
    #: Where the requests of several peers go too, in the order made, each with its peer.
    journal: list[tuple["Peer", dict[str, Any]]] | None = None
    exited: "asyncio.Future[int] | None" = None
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def factory(self, path: str, *, max_line_bytes: int, on_event: Callable[[dict[str, Any]], None]) -> "Peer":
        """Stand in for the ``Helper`` class: the platform's ``helper_factory``."""
        self.on_event = on_event
        return self

    async def open(self) -> dict[str, Any]:
        self.exited = asyncio.get_running_loop().create_future()
        return self.hello

    def room(self, room: str) -> Room:
        return self.rooms.setdefault(room, Room(direct=room.startswith(("D-", "!dm"))))

    def emit(self, event: dict[str, Any]) -> None:
        protocol.check_from_helper(event, {})
        self.on_event(event)

    def add(self, room: str, sender: str, content: dict[str, Any], *, event_type: str = "m.room.message",
            undecryptable: bool = False, at: float | None = None) -> str:
        """Put an event in a room's timeline, as the newest, and return its id."""
        event_id = f"$t{next(self._ids)}"
        event = {"type": event_type, "sender": sender, "event_id": event_id, "content": content,
                 "origin_server_ts": int((at if at is not None else time.time()) * 1000)}
        if undecryptable:
            event = {**event, "type": "m.room.encrypted", "content": {"algorithm": "m.megolm.v1.aes-sha2", "session_id": "S"}}
            self.undecryptable.add(event_id)
        self.timeline.setdefault(room, []).append(event)
        self.events[event_id] = event
        return event_id

    def room_of(self, event_id: str) -> str | None:
        return next((room for room, events in self.timeline.items() if any(e["event_id"] == event_id for e in events)), None)

    def _page(self, room: str, events: list[dict[str, Any]], start: int, limit: int) -> dict[str, Any]:
        """Events before index ``start``, newest first, and the token of the page after."""
        low = max(0, start - limit)
        chunk = [{"encrypted": self.room(room).encrypted, "undecryptable": e["event_id"] in self.undecryptable, "room_event": e}
                 for e in reversed(events[low:start])]
        return {"chunk": chunk, **({"next": str(low)} if low > 0 else {})}

    def sent(self, event_type: str = "m.room.message") -> list[dict[str, Any]]:
        return [r for r in self.requests if r["op"] == "send" and r["type"] == event_type]

    async def request(self, op: str, **fields: Any) -> dict[str, Any]:
        number = next(self._ids)
        try:
            protocol.check_request({"id": number, "op": op, **fields})
        except protocol.ProtocolError as exc:
            raise HelperError(protocol.BAD_REQUEST, str(exc)) from exc
        self.requests.append({"op": op, **fields})
        if self.journal is not None:
            self.journal.append((self, self.requests[-1]))
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = self._answer(number, op, fields)
        protocol.check_from_helper(answer, {number: op})
        if not answer["ok"]:
            error = answer["error"]
            raise HelperError(error["code"], error["message"], retry_after_ms=error.get("retry_after_ms"))
        return dict(answer["result"])

    def _answer(self, number: int, op: str, fields: dict[str, Any]) -> dict[str, Any]:
        def ok(**result: Any) -> dict[str, Any]:
            return {"id": number, "ok": True, "result": result}

        def error(code: str, message: str, retry_after_ms: int | None = None) -> dict[str, Any]:
            body: dict[str, Any] = {"code": code, "message": message}
            if retry_after_ms is not None:
                body["retry_after_ms"] = retry_after_ms
            return {"id": number, "ok": False, "error": body}

        if op == "start":
            limit = {} if self.max_upload_bytes is None else {"max_upload_bytes": self.max_upload_bytes}
            return ok(user_id=self.user, device_id="PEERDEVICE", new_device=self.new_device, backup="enabled", **limit)
        if op == "log_out":
            if not self.new_device:
                return error("helper_bad_request", "log_out: the store held this device before the start, so it stays")
            self.logged_out = True
            return ok()
        if op == "download":
            uri = fields["url"] if "url" in fields else fields["file"]["url"]
            if uri not in self.media:
                return error("M_NOT_FOUND", "Media not found")
            data = self.media[uri]
            if "max_bytes" in fields and len(data) > fields["max_bytes"]:
                return error(protocol.TOO_LARGE, f"the file is {len(data)} bytes, over the limit of {fields['max_bytes']} bytes")
            return ok(data=base64.b64encode(data).decode("ascii"))
        if op == "upload":
            uri = f"mxc://syn.example/up{number}"
            self.media[uri] = base64.b64decode(fields["data"])
            # An encrypted room gets an EncryptedFile. The peer keeps the plain bytes under its url.
            return ok(file={"url": uri, "v": "v2"}) if self.room(fields["room"]).encrypted else ok(url=uri)
        if op == "room_info":
            if self.joined is not None and fields["room"] not in self.joined:
                return error(protocol.NOT_JOINED, f"room {fields['room']} is not a joined room of this account")
            return ok(**self._info(fields["room"]))
        if op == "rooms":
            return ok(rooms=[self._info(room) for room in self.rooms if self.joined is None or room in self.joined])
        if op == "search":
            term = fields["search_term"].casefold()
            keys = fields.get("keys") or ["content.body", "content.name", "content.topic"]
            def matches(room, event):
                if self.room(room).encrypted or (self.joined is not None and room not in self.joined):
                    return False
                for field, value in (("rooms", room), ("senders", event["sender"]), ("types", event["type"])):
                    if fields.get(field) and value not in fields[field]:
                        return False
                    if value in fields.get("not_" + field, []):
                        return False
                return any(term in str((event.get("content") or {}).get(key.removeprefix("content."), "")).casefold().split()
                           for key in keys)
            hits = [e for room, events in self.timeline.items() for e in events if matches(room, e)]
            hits.sort(key=lambda e: -e["origin_server_ts"])
            start = int(fields.get("from", 0))
            page = hits[start:start + fields["limit"]]
            chunk = []
            for event in page:
                room = self.room_of(event["event_id"])
                hit = {"rank": 1.0, "room_event": {**event, "room_id": room}}
                if fields.get("before_limit") or fields.get("after_limit"):
                    timeline = sorted(self.timeline[room], key=lambda item: item["origin_server_ts"])
                    index = timeline.index(event)
                    before = timeline[max(0, index - fields.get("before_limit", 0)):index]
                    after = timeline[index + 1:index + 1 + fields.get("after_limit", 0)]
                    hit["context"] = {
                        "events_before": before,
                        "events_after": after,
                    }
                    if fields.get("include_profile"):
                        hit["context"]["profile_info"] = {
                            item["sender"]: {"displayname": item["sender"]} for item in [*before, event, *after]
                        }
                chunk.append(hit)
            more = {"next": str(start + len(page))} if start + len(page) < len(hits) else {}
            return ok(chunk=chunk, count=len(hits), **more)
        if op == "user_directory":
            term = fields["search_term"].casefold()
            found = [u for u in self.directory if term in u["user_id"].casefold() or term in str(u.get("display_name", "")).casefold()]
            return ok(results=found[:fields["limit"]], limited=len(found) > fields["limit"])
        if op == "public_rooms":
            if fields.get("server") and fields["server"] not in ("syn.example", "remote.example"):
                return error("M_NOT_FOUND", "Remote directory unavailable")
            term = str(fields.get("search_term", "")).casefold()
            found = [r for r in self.public_directory
                     if term in str(r.get("name", "")).casefold() or term in str(r.get("canonical_alias", "")).casefold()]
            start = int(fields.get("from", 0))
            page = found[start:start + fields["limit"]]
            more = {"next": str(start + len(page))} if start + len(page) < len(found) else {}
            return ok(chunk=page, total=len(found), **more)
        if op == "alias_resolve":
            room = self.aliases.get(fields["alias"])
            if not room:
                return error("M_NOT_FOUND", "Room alias not found")
            return ok(room=room, servers=[fields["alias"].split(":", 1)[1]],
                      joined=self.joined is None or room in self.joined)
        if "room" in fields and op not in ("leave", "join") and self.joined is not None and fields["room"] not in self.joined:
            return error(protocol.NOT_JOINED, f"room {fields['room']} is not a joined room of this account")
        if op == "hierarchy":
            found = self.hierarchy.get(fields["room"], [])
            start = int(fields.get("from", 0))
            page = found[start:start + fields["limit"]]
            more = {"next": str(start + len(page))} if start + len(page) < len(found) else {}
            return ok(rooms=page, **more)
        if op == "event":
            found = self.events.get(fields["event_id"])
            if found is None or self.room_of(fields["event_id"]) not in (None, fields["room"]):
                return error("M_NOT_FOUND", "Event not found.")
            return ok(encrypted=self.room(fields["room"]).encrypted, undecryptable=fields["event_id"] in self.undecryptable,
                      room_event=found)
        if op == "context":
            found = self.events.get(fields["event_id"])
            if found is None or self.room_of(fields["event_id"]) != fields["room"]:
                return error("M_NOT_FOUND", "Event not found.")
            events = sorted(self.timeline[fields["room"]], key=lambda item: item["origin_server_ts"])
            index = events.index(found)
            before_count = fields["limit"] // 2
            after_count = fields["limit"] - before_count
            def record(event):
                return {"encrypted": self.room(fields["room"]).encrypted,
                        "undecryptable": event["event_id"] in self.undecryptable, "room_event": event}
            return ok(event=record(found), before=[record(item) for item in events[max(0, index-before_count):index]],
                      after=[record(item) for item in events[index+1:index+1+after_count]])
        if op == "messages":
            events = self.timeline.get(fields["room"], [])
            start = len(events)
            if "from" in fields:
                start = int(fields["from"])
            elif "before_event" in fields:
                ids = [e["event_id"] for e in events]
                if fields["before_event"] not in ids:
                    return error("M_NOT_FOUND", "Event not found.")
                start = ids.index(fields["before_event"])
            return ok(**self._page(fields["room"], events, start, fields["limit"]))
        if op == "relations":
            related = [e for e in self.timeline.get(fields["room"], [])
                       if (e.get("content") or {}).get("m.relates_to", {}).get("rel_type") == fields["rel_type"]
                       and e["content"]["m.relates_to"].get("event_id") == fields["event_id"]]
            start = int(fields["from"]) if "from" in fields else len(related)
            return ok(**self._page(fields["room"], related, start, fields["limit"]))
        if op == "state_get":
            found = self.state.get((fields["room"], fields["type"], fields["state_key"]))
            if found is None and fields["type"] == "m.room.member":
                # A member with no membership event of a check's own joined at the epoch.
                member = next((m for m in self.members.get(fields["room"], []) if m["user_id"] == fields["state_key"]), None)
                if member is not None:
                    found = {"type": "m.room.member", "state_key": member["user_id"], "sender": member["user_id"],
                             "event_id": f"$member-{member['user_id']}", "origin_server_ts": 1,
                             "content": {"membership": member.get("membership", "join")}}
            return ok(event=found) if found is not None else ok()
        if op == "state_put":
            if fields["room"] in self.state_forbidden:
                return error("M_FORBIDDEN", "You don't have permission to post that to the room.")
            event_id = f"$state{number}"
            self.state[(fields["room"], fields["type"], fields["state_key"])] = {
                "type": fields["type"], "state_key": fields["state_key"], "sender": self.user, "event_id": event_id,
                "origin_server_ts": int(time.time() * 1000), "content": fields["content"]}
            return ok(event_id=event_id)
        if op == "members":
            return ok(members=self.members.get(fields["room"], [{"user_id": self.user, "membership": "join"}]))
        if op == "dm_room":
            room = self.dms.get(fields["user"])
            if room is None and not fields["create"]:
                return ok(created=False)
            created = room is None
            if created:
                room = self.dms[fields["user"]] = f"!dm-{fields['user'][1:].split(':')[0]}:syn.example"
                self.rooms[room] = Room(direct=True, joined_members=1)
                self.members[room] = [{"user_id": self.user, "membership": "join"}, {"user_id": fields["user"], "membership": "invite"}]
            return ok(room=room, created=created)
        if op in ("send", "redact"):
            if op == "send" and self.send_errors:
                code, retry = self.send_errors.pop(0)
                return error(code, "refused by the peer", retry)
            content = fields.get("content", {})
            if op == "send" and self.max_bytes and sum(len(str(content.get(k, "")).encode()) for k in ("body", "formatted_body")) > self.max_bytes:
                return error("M_TOO_LARGE", "event too large")
            event_id = f"$peer{number}"
            self.events[event_id] = {"type": fields.get("type", "m.room.redaction"), "sender": self.user, "event_id": event_id,
                                     "content": content, "origin_server_ts": int(time.time() * 1000)}
            timeline = self.timeline.setdefault(fields["room"], [])
            if op == "redact":
                timeline[:] = [e for e in timeline if e["event_id"] != fields["event_id"]]
            else:
                timeline.append(self.events[event_id])
            return ok(event_id=event_id)
        return ok()

    def _info(self, room: str) -> dict[str, Any]:
        found = self.room(room)
        names = {key: value for key, value in (("name", found.name), ("alias", found.alias)) if value}
        return {"room": room, "encrypted": found.encrypted, "public": found.public, "direct": found.direct,
                "joined_members": found.joined_members, **names}

    async def close(self) -> int | None:
        if self.exited is not None and not self.exited.done():
            self.exited.set_result(0)
        return 0


class Peers:
    """Stand in for the ``Helper`` class of several accounts: each helper serves the peer of the account it starts."""

    def __init__(self, *peers: Peer) -> None:
        self.peers = {peer.user: peer for peer in peers}
        #: The requests of every peer, in the order made.
        self.journal: list[tuple[Peer, dict[str, Any]]] = []
        for peer in peers:
            peer.journal = self.journal

    def factory(self, path: str, *, max_line_bytes: int, on_event: Callable[[dict[str, Any]], None]) -> "_Routed":
        return _Routed(self.peers, on_event)


class _Routed:
    """One helper process of ``Peers``: its ``start`` names the account, and so the peer of every later request."""

    def __init__(self, peers: dict[str, Peer], on_event: Callable[[dict[str, Any]], None]) -> None:
        self._peers, self._on_event = peers, on_event
        self._peer: Peer | None = None
        self.hello: dict[str, Any] = {"event": "hello", "protocol": protocol.PROTOCOL}

    @property
    def exited(self) -> "asyncio.Future[int] | None":
        return self._peer.exited if self._peer is not None else None

    async def open(self) -> dict[str, Any]:
        return self.hello

    async def request(self, op: str, **fields: Any) -> dict[str, Any]:
        if op == "start":
            self._peer = self._peers[fields["user_id"]]
            self._peer.on_event = self._on_event
            await self._peer.open()
        assert self._peer is not None, "the connector sends start first"
        return await self._peer.request(op, **fields)

    async def close(self) -> int | None:
        return await self._peer.close() if self._peer is not None else 0
