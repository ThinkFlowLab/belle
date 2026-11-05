"""Read a Matrix room or one thread through the helper, into the records ``read_conversation`` returns.

The helper reads newest first and decrypts where it holds the key. An event it holds no key for
is a record with ``undecryptable`` set and no text, and it is counted: a message Belle could not
read is never left out. An edit applies only where the original's sender made it.
"""

import base64
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from belle_agent.platforms.matrix import protocol
from belle_agent.platforms.matrix.helper import HelperError
from belle_agent.tools.inputs import iso_utc

#: The events one helper read asks for.
PAGE = 100
#: The message types whose text is the message. Any other is named in the record.
TEXT_TYPES = frozenset({"m.text", "m.notice"})
#: The message types that carry one file.
FILE_TYPES = frozenset({"m.image", "m.file", "m.audio", "m.video"})

#: The history visibilities under which a person cannot read a room's messages from before their membership,
#: in a room without encryption. In an encrypted room the keys bound it: see ``visible_since``.
BOUNDED_VISIBILITIES = frozenset({"joined", "invited"})


class MembershipHistoryLimit(Exception):
    """The requester visibility bound needs more membership events."""


Call = Callable[..., Awaitable[dict[str, Any]]]


def attachment(content: Mapping[str, Any]) -> dict[str, Any] | None:
    """The file a message carries, or nothing: its name, type and size as the sender gave them, and its source.

    ``source`` is ``{"url": <mxc URI>}`` in a room without encryption, and ``{"file": <EncryptedFile>}``
    in an encrypted room. ``size_bytes`` is ``None`` where the sender gave no size.
    """
    if content.get("msgtype") not in FILE_TYPES:
        return None
    if isinstance(content.get("file"), Mapping):
        source: dict[str, Any] = {"file": dict(content["file"])}
    elif isinstance(content.get("url"), str) and content["url"]:
        source = {"url": content["url"]}
    else:
        return None
    info = content.get("info")
    info = info if isinstance(info, Mapping) else {}
    size = info.get("size")
    record = {
        "name": str(content.get("filename") or content.get("body") or "file"),
        "mimetype": str(info.get("mimetype") or "application/octet-stream"),
        "size_bytes": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
        "source": source,
    }
    for source_key, target_key in (("w", "width_px"), ("h", "height_px"), ("duration", "duration_ms")):
        value = info.get(source_key)
        if type(value) is int and value >= 0:
            record[target_key] = value
    if isinstance(info.get("thumbnail_url"), str) or isinstance(info.get("thumbnail_file"), Mapping):
        record["thumbnail_available"] = True
    return record


class FileOverLimit(Exception):
    """A file larger than the most bytes this bot reads of one file."""

    def __init__(self, limit: int, size: int | None) -> None:
        super().__init__(f"{size if size is not None else 'more than'} {limit} bytes")
        self.limit = limit
        self.size = size


async def download(call: Call, found: Mapping[str, Any], limit: int) -> bytes:
    """The bytes of a file that ``attachment`` found, decrypted by the helper. Raise ``FileOverLimit`` past ``limit``."""
    size = found["size_bytes"]
    if size is not None and size > limit:
        raise FileOverLimit(limit, size)
    try:
        result = await call("download", max_bytes=limit, **found["source"])
    except HelperError as exc:
        # The size in the event is the sender's word: the helper checks the bytes it got.
        if exc.code == protocol.TOO_LARGE:
            raise FileOverLimit(limit, None) from None
        raise
    return base64.b64decode(result["data"])


class NotInThread(LookupError):
    """``before`` names no message of the thread."""


@dataclass
class Read:
    """What one read found: its records oldest first, and what it could not show."""

    records: list[dict[str, Any]] = field(default_factory=list)
    #: Older messages remain past ``records``.
    more: bool = False
    #: Records whose event the helper holds no key for.
    undecryptable: int = 0
    #: Edits not applied because another person than the original's sender made them.
    edits_ignored: int = 0
    #: Messages the server says were edited, by an edit that the helper could not decrypt: the record shows the original.
    edits_unread: int = 0
    #: The server shows nothing before this instant (the room's history visibility), or ``None``.
    inaccessible_before: float | None = None
    #: Messages from before this instant were left out: the requester could not read them. ``None``: none left out.
    withheld_before: float | None = None


@dataclass
class _Edit:
    sender: str
    content: Mapping[str, Any]
    at: float


class _Collector:
    """Turn events, newest first, into records. Edits come before their original, and wait for it."""

    def __init__(self, names: Mapping[str, str], bot: str, bots: frozenset[str], since: float | None = None) -> None:
        self._names, self._bot, self._bots, self._since = names, bot, bots, since
        self.read = Read()
        self._edits: dict[str, _Edit] = {}

    def withholds(self, item: Mapping[str, Any]) -> bool:
        """Whether ``item`` is a message from before the requester could read the room. It is then noted, not added."""
        event = item.get("room_event") or {}
        at = int(event.get("origin_server_ts") or 0) / 1000
        if self._since is None or at >= self._since:
            return False
        if not item.get("undecryptable") and event.get("type") != "m.room.message":
            return False
        self.read.withheld_before = self._since
        return True

    def add(self, item: Mapping[str, Any]) -> None:
        if self.withholds(item):
            return
        event = item.get("room_event") or {}
        event_id, sender = str(event.get("event_id") or ""), str(event.get("sender") or "")
        at = int(event.get("origin_server_ts") or 0) / 1000
        if item.get("undecryptable"):
            self.read.undecryptable += 1
            self.read.records.append({**self._who(event_id, sender, at), "undecryptable": True, "text": ""})
            return
        content = event.get("content")
        if event.get("type") != "m.room.message" or not isinstance(content, Mapping) or "body" not in content:
            return  # Not a message, or a message that was removed.
        relates = content.get("m.relates_to")
        relates = relates if isinstance(relates, Mapping) else {}
        if relates.get("rel_type") == "m.replace":
            original, new = str(relates.get("event_id") or ""), content.get("m.new_content")
            if original and isinstance(new, Mapping) and original not in self._edits:
                self._edits[original] = _Edit(sender, new, at)
            return
        record = self._who(event_id, sender, at)
        edit = self._edits.pop(event_id, None) or _bundled_edit(event)
        if edit is None and _bundled(event):
            self.read.edits_unread += 1
            record["edit_not_decrypted"] = True
        elif edit is not None and edit.sender != sender:
            self.read.edits_ignored += 1
        elif edit is not None:
            content, record["edited_iso_utc"] = edit.content, iso_utc(edit.at)
        record["text"] = str(content.get("body") or "")
        if relates.get("rel_type") == "m.thread":
            record["root_message_id"] = str(relates.get("event_id") or "")
        reply = relates.get("m.in_reply_to")
        if isinstance(reply, Mapping) and reply.get("event_id") and not relates.get("is_falling_back"):
            record["reply_to_message_id"] = str(reply["event_id"])
        msgtype = str(content.get("msgtype") or "")
        if msgtype not in TEXT_TYPES:
            record["msgtype"] = msgtype
        if found := attachment(content):
            # A Matrix message carries one file, and the message's id finds it.
            record["files"] = [{"file_id": event_id, **{
                key: found[key] for key in ("name", "mimetype", "size_bytes", "width_px", "height_px",
                                             "duration_ms", "thumbnail_available") if key in found
            }}]
        record["is_author_bot"] = sender == self._bot or sender in self._bots or msgtype == "m.notice"
        self.read.records.append(record)

    def _who(self, event_id: str, sender: str, at: float) -> dict[str, Any]:
        return {
            "message_id": event_id,
            "root_message_id": "",
            "author_user_id": sender,
            "author_name": self._names.get(sender, ""),
            "is_author_bot": sender == self._bot or sender in self._bots,
            "created_iso_utc": iso_utc(at),
        }

    def done(self, limit: int, more: bool) -> Read:
        """The newest ``limit`` records, oldest first."""
        records = self.read.records
        self.read.more = more or len(records) > limit
        self.read.records = list(reversed(records[:limit]))
        self.read.undecryptable = sum(1 for record in self.read.records if record.get("undecryptable"))
        self.read.edits_unread = sum(1 for record in self.read.records if record.get("edit_not_decrypted"))
        return self.read


def _bundled(event: Mapping[str, Any]) -> Any:
    """The latest edit the server bundles with an event, as the server gives it, or nothing."""
    unsigned = event.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, Mapping) else None
    return relations.get("m.replace") if isinstance(relations, Mapping) else None


def _bundled_edit(event: Mapping[str, Any]) -> _Edit | None:
    """The latest edit the server bundles with an event, where its text can be read, or nothing.

    In an encrypted room the bundled edit is encrypted, and the helper does not decrypt it.
    """
    edit = _bundled(event)
    content = edit.get("content") if isinstance(edit, Mapping) else None
    new = content.get("m.new_content") if isinstance(content, Mapping) else None
    if not isinstance(edit, Mapping) or not isinstance(new, Mapping):
        return None
    return _Edit(str(edit.get("sender") or ""), new, int(edit.get("origin_server_ts") or 0) / 1000)


async def read_room(
    call: Call, room: str, *, before: str, limit: int, after: float | None, names: Mapping[str, str], bot: str,
    bots: frozenset[str] = frozenset(), since: float | None = None,
) -> Read:
    """The newest ``limit`` messages of a room before the message ``before``, after the instant ``after``.

    ``since``: the instant ``visible_since`` gives for the requester. The read stops at the first older message.
    """
    collector = _Collector(names, bot, bots, since)
    fields: dict[str, Any] = {"before_event": before} if before else {}
    oldest, saw_create = None, False
    while True:
        page = await call("messages", room=room, limit=PAGE, **fields)
        for item in page["chunk"]:
            event = item.get("room_event") or {}
            at = int(event.get("origin_server_ts") or 0) / 1000
            if after is not None and at <= after:
                return collector.done(limit, more=False)
            oldest, saw_create = at, saw_create or event.get("type") == "m.room.create"
            if collector.withholds(item):
                return collector.done(limit, more=False)
            if event.get("event_id") != before:
                collector.add(item)
            if len(collector.read.records) > limit:
                return collector.done(limit, more=True)
        if not page.get("next"):
            read = collector.done(limit, more=False)
            # The room's history ends without its first event: the server keeps the older events from the bot.
            read.inaccessible_before = None if saw_create or read.withheld_before is not None else oldest
            return read
        fields = {"from": page["next"]}


async def read_thread(
    call: Call, room: str, root: Mapping[str, Any], *, before: str, limit: int, after: float | None,
    names: Mapping[str, str], bot: str,
    bots: frozenset[str] = frozenset(), since: float | None = None,
) -> Read:
    """Read at most ``limit`` thread messages after ``after`` and before ``before``.

    ``since`` withholds older messages, as in ``read_room``. Raise ``NotInThread`` where ``before`` is no message of the thread.
    """
    collector = _Collector(names, bot, bots, since)
    root_id = str((root.get("room_event") or {}).get("event_id") or "")
    if before == root_id:
        return collector.done(limit, more=False)
    passed = not before
    fields: dict[str, Any] = {}
    while True:
        page = await call("relations", room=room, event_id=root_id, rel_type="m.thread", limit=PAGE, **fields)
        for item in page["chunk"]:
            if not passed:
                passed = (item.get("room_event") or {}).get("event_id") == before
                continue
            event = item.get("room_event") or {}
            if after is not None and int(event.get("origin_server_ts") or 0) / 1000 <= after:
                continue
            collector.add(item)
            if len(collector.read.records) > limit:
                return collector.done(limit, more=True)
        if not page.get("next"):
            break
        fields = {"from": page["next"]}
    if not passed:
        raise NotInThread(before)
    root_at = int((root.get("room_event") or {}).get("origin_server_ts") or 0) / 1000
    if after is None or root_at > after:
        collector.add(root)
    return collector.done(limit, more=False)


async def visible_since(call: Call, room: str, person: str, *, encrypted: bool, max_events: int = 20) -> float | None:
    """The instant from which ``person`` can read the messages of ``room``: ``None`` for every message, ``math.inf`` for none.

    In an encrypted room a person holds the keys of what was sent while they were joined, so the bound is the
    start of their membership. Without encryption, the room's history visibility decides: ``joined`` bounds by
    the join, ``invited`` by the invite, ``shared`` and ``world_readable`` show every message. The start is the
    earliest event of the person's latest unbroken run of membership events, followed by ``replaces_state``.
    Where the server gives no earlier event or no time, the later bound stands: it withholds more, never less.
    """
    visibility = (await call("state_get", room=room, type="m.room.history_visibility", state_key="")).get("event") or {}
    seen = str((visibility.get("content") or {}).get("history_visibility") or "shared")
    if not encrypted and seen not in BOUNDED_VISIBILITIES:
        return None
    counted = {"join", "invite"} if not encrypted and seen == "invited" else {"join"}
    event: Any = (await call("state_get", room=room, type="m.room.member", state_key=person)).get("event")
    start = math.inf
    for index in range(max_events):
        if not isinstance(event, Mapping) or (event.get("content") or {}).get("membership") not in counted:
            break
        start = int(event["origin_server_ts"]) / 1000 if event.get("origin_server_ts") else math.inf
        unsigned = event.get("unsigned")
        unsigned = unsigned if isinstance(unsigned, Mapping) else {}
        previous = unsigned.get("prev_content")
        earlier = unsigned.get("replaces_state")
        if not isinstance(previous, Mapping) or previous.get("membership") not in counted or not earlier or start == math.inf:
            break
        if index + 1 == max_events:
            raise MembershipHistoryLimit(max_events)
        try:
            event = (await call("event", room=room, event_id=str(earlier))).get("room_event")
        except HelperError:
            break
    return start


def withheld_coverage(since: float) -> dict[str, Any]:
    """What a read's coverage says of the messages it withheld from before the requester could read the room."""
    return {
        "before_requester_joined": "withheld",
        "requester_joined_iso_utc": iso_utc(since) if since != math.inf else None,
        "before_requester_joined_note": (
            "Messages from before the requester joined this room exist and were left out: Matrix did not let the"
            " requester read them (the room is encrypted, or its history visibility hides them). They are not"
            " counted and not shown, and this is not a sign that there are none. requester_joined_iso_utc is null"
            " where the requester is not a member."
        ),
    }
