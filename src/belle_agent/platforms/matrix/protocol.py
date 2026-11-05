"""The messages between Belle and the Matrix helper: one JSON object per line on a socket pair.

``helper/protocol.jsonl`` holds one example of each message. The tests of both sides
read it, so a field that one side adds fails both suites. ``helper/src/protocol.rs``
is the other half of this module.
"""

import json
from collections.abc import Mapping
from typing import Any

#: Increments with any change of a message shape. Belle and the helper ship together, so a
#: mismatch is a mixed build, and Belle refuses it.
PROTOCOL = 10

#: Each request op: its required fields, then its optional fields. ``id`` and ``op`` are on every request.
REQUESTS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "start": (
        frozenset({"homeserver", "user_id", "store_dir", "device_name", "store_passphrase"}),
        # reset_identity: an operator's identity reset, which writes a new recovery key to recovery_key_output.
        frozenset({"password", "recovery_key", "recovery_key_output", "reset_identity"}),
    ),
    "send": (frozenset({"room", "type", "content", "txn_id"}), frozenset()),
    "ack": (frozenset({"seq"}), frozenset()),
    "join": (frozenset({"room"}), frozenset()),
    "upload": (frozenset({"room", "mime", "data"}), frozenset()),
    # Exactly one of url and file. The helper refuses a file over max_bytes with TOO_LARGE.
    "download": (frozenset(), frozenset({"url", "file", "max_bytes"})),
    "redact": (frozenset({"room", "event_id", "txn_id"}), frozenset()),
    "event": (frozenset({"room", "event_id"}), frozenset()),
    "context": (frozenset({"room", "event_id", "limit"}), frozenset()),
    "room_info": (frozenset({"room"}), frozenset()),
    "alias_resolve": (frozenset({"alias"}), frozenset()),
    "hierarchy": (frozenset({"room", "limit"}), frozenset({"from", "max_depth", "suggested_only"})),
    "rooms": (frozenset(), frozenset()),
    # The server searches the joined rooms whose events it can read: never an encrypted room's text.
    "search": (
        frozenset({"search_term", "limit"}),
        frozenset({
            "from", "keys", "rooms", "not_rooms", "senders", "not_senders", "types", "not_types",
            "order_by", "before_limit", "after_limit", "include_profile",
        }),
    ),
    "user_directory": (frozenset({"search_term", "limit"}), frozenset()),
    "public_rooms": (frozenset({"limit"}), frozenset({"search_term", "from", "server"})),
    "state_get": (frozenset({"room", "type", "state_key"}), frozenset()),
    "state_put": (frozenset({"room", "type", "state_key", "content"}), frozenset()),
    "leave": (frozenset({"room"}), frozenset()),
    # timeout_ms is required with typing: true. The helper refuses it missing.
    "typing": (frozenset({"room", "typing"}), frozenset({"timeout_ms"})),
    # At most one of from and before_event. The helper refuses both.
    "messages": (frozenset({"room", "limit"}), frozenset({"from", "before_event"})),
    "relations": (frozenset({"room", "event_id", "rel_type", "limit"}), frozenset({"from"})),
    "members": (frozenset({"room"}), frozenset()),
    "dm_room": (frozenset({"user", "create"}), frozenset()),
    # The operator's commands. The helper refuses to remove the device of its own store.
    "devices": (frozenset(), frozenset()),
    "delete_devices": (frozenset({"devices", "password"}), frozenset()),
    # Undo a start that logged in as a new device: log it out and remove its store. The helper refuses another device.
    "log_out": (frozenset(), frozenset()),
    "shutdown": (frozenset(), frozenset()),
}

#: Each event: its fields. ``event`` is on every event.
EVENTS: Mapping[str, frozenset[str]] = {
    "hello": frozenset({"protocol", "helper_version", "sdk_version"}),
    "timeline": frozenset({"seq", "room", "encrypted", "direct", "room_event"}),
    "utd": frozenset({"room", "event_id", "sender", "session_id", "direct"}),
    "invite": frozenset({"seq", "room", "inviter"}),
    "alert": frozenset({"kind", "message"}),
}

#: What an ok answer carries, by the op it answers: required fields, then optional fields.
RESULTS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    # max_upload_bytes: the server's m.upload.size, absent where the server does not say. new_device: whether
    # this start logged in as a new device rather than restoring the device of its store.
    "start": (frozenset({"user_id", "device_id", "new_device", "backup"}), frozenset({"max_upload_bytes"})),
    "send": (frozenset({"event_id"}), frozenset()),
    "ack": (frozenset(), frozenset()),
    "join": (frozenset(), frozenset()),
    "upload": (frozenset(), frozenset({"url", "file"})),
    "download": (frozenset({"data"}), frozenset()),
    "redact": (frozenset({"event_id"}), frozenset()),
    "event": (frozenset({"encrypted", "undecryptable", "room_event"}), frozenset()),
    "context": (frozenset({"event", "before", "after"}), frozenset({"before_token", "after_token"})),
    "room_info": (frozenset({"room", "encrypted", "public", "direct", "joined_members"}), frozenset({"name", "alias"})),
    "alias_resolve": (frozenset({"room", "servers", "joined"}), frozenset()),
    "hierarchy": (frozenset({"rooms"}), frozenset({"next"})),
    # Each room as room_info gives it.
    "rooms": (frozenset({"rooms"}), frozenset()),
    # Each hit: rank, where the server gives one, and room_event, which holds room_id. count: the server's estimate.
    "search": (frozenset({"chunk"}), frozenset({"count", "next"})),
    "user_directory": (frozenset({"results", "limited"}), frozenset()),
    # Each room as the server's PublicRoomsChunk gives it. total: the server's estimate.
    "public_rooms": (frozenset({"chunk"}), frozenset({"next", "total"})),
    # event: absent where the room has no such state event.
    "state_get": (frozenset(), frozenset({"event"})),
    "state_put": (frozenset({"event_id"}), frozenset()),
    "leave": (frozenset(), frozenset()),
    "typing": (frozenset(), frozenset()),
    "messages": (frozenset({"chunk"}), frozenset({"next"})),
    "relations": (frozenset({"chunk"}), frozenset({"next"})),
    "members": (frozenset({"members"}), frozenset()),
    "dm_room": (frozenset({"created"}), frozenset({"room"})),
    # Each device: device_id and current, with display_name and last_seen_ts where the server gives them.
    "devices": (frozenset({"devices"}), frozenset()),
    "delete_devices": (frozenset(), frozenset()),
    "log_out": (frozenset(), frozenset()),
    "shutdown": (frozenset(), frozenset()),
}

ERROR_FIELDS = (frozenset({"code", "message"}), frozenset({"reason", "retry_after_ms"}))

#: The helper's own error codes. A homeserver ``errcode`` such as ``M_FORBIDDEN`` passes through as is.
BAD_REQUEST = "helper_bad_request"
NOT_READY = "helper_not_ready"
NOT_JOINED = "helper_not_joined"
START_REFUSED = "helper_start_refused"
FAILED = "helper_failed"
TOO_LARGE = "helper_too_large"
#: Belle's own: the helper ended before it answered.
RESTARTED = "helper_restarted"
#: The homeserver's rate limit, as the helper passes it on with ``retry_after_ms``.
RATE_LIMITED = "M_LIMIT_EXCEEDED"
#: The reason of a refused start that the homeserver did not answer, or answered that it cannot serve now.
START_UNREACHABLE = "homeserver_unreachable"

#: Alert kinds. ``store_rolled_back`` stops the platform.
ALERT_STORE_ROLLED_BACK = "store_rolled_back"
ALERT_BAD_LINE = "bad_line"


class ProtocolError(ValueError):
    """A line that is not a message of this protocol."""


def _check_fields(what: str, fields: set[str], required: frozenset[str], optional: frozenset[str]) -> None:
    missing = sorted(required - fields)
    unknown = sorted(fields - required - optional)
    if missing or unknown:
        parts = [f"missing {', '.join(missing)}" if missing else "", f"unknown {', '.join(unknown)}" if unknown else ""]
        raise ProtocolError(f"{what}: {'; '.join(p for p in parts if p)}")


def check_request(message: Mapping[str, Any]) -> None:
    """Refuse a request this protocol does not have, naming the field."""
    op = message.get("op")
    if not isinstance(op, str) or op not in REQUESTS:
        raise ProtocolError(f"op must be one of {', '.join(REQUESTS)}, not {op!r}")
    if not isinstance(message.get("id"), int) or isinstance(message.get("id"), bool) or message["id"] < 0:
        raise ProtocolError(f"{op}: id must be a non-negative integer")
    _check_fields(f"request {op}", set(message) - {"id", "op"}, *REQUESTS[op])


def check_from_helper(message: Mapping[str, Any], op_of: Mapping[int, str]) -> None:
    """Refuse a line from the helper that is no event and no answer of this protocol.

    ``op_of`` gives the op of each pending request by its id, so an answer's result is checked against its op.
    """
    if "event" in message:
        kind = message["event"]
        if kind not in EVENTS:
            raise ProtocolError(f"event must be one of {', '.join(EVENTS)}, not {kind!r}")
        _check_fields(f"event {kind}", set(message) - {"event"}, EVENTS[kind], frozenset())
        return
    _check_fields("answer", set(message), frozenset({"id", "ok"}), frozenset({"result", "error"}))
    request = message["id"]
    if message["ok"] is True:
        if "error" in message or not isinstance(message.get("result"), dict):
            raise ProtocolError(f"answer {request}: an ok answer has a result object and no error")
        op = op_of.get(request)
        if op is not None:
            _check_fields(f"result of {op}", set(message["result"]), *RESULTS[op])
    elif message["ok"] is False:
        if "result" in message or not isinstance(message.get("error"), dict):
            raise ProtocolError(f"answer {request}: a failed answer has an error object and no result")
        _check_fields(f"error of answer {request}", set(message["error"]), *ERROR_FIELDS)
    else:
        raise ProtocolError(f"answer {request}: ok must be true or false")


def encode(message: Mapping[str, Any]) -> bytes:
    """One line of the protocol."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    """One message from a line, refused where it is no JSON object."""
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise ProtocolError(f"a line from the helper is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("a line from the helper is not a JSON object")
    return message
