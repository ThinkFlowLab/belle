"""The protocol schema file against ``protocol.py``: each line passes, and each message has a line."""

import json
from pathlib import Path

import pytest

from belle_agent.platforms.matrix import protocol

SCHEMA = Path(__file__).parents[2] / "src" / "belle_agent" / "platforms" / "matrix" / "helper" / "protocol.jsonl"


def lines() -> list[dict]:
    return [json.loads(line) for line in SCHEMA.read_text().splitlines() if line.strip()]


def test_every_line_is_a_message_of_this_protocol_and_every_message_has_a_line():
    ops: dict[int, str] = {}
    events: set[str] = set()
    for message in lines():
        assert protocol.decode(protocol.encode(message)) == message
        if "op" in message:
            protocol.check_request(message)
            ops[message["id"]] = message["op"]
        else:
            protocol.check_from_helper(message, ops)
            if "event" in message:
                events.add(message["event"])
            else:
                assert message["id"] in ops, f"answer {message['id']} follows no request"
    assert set(ops.values()) == set(protocol.REQUESTS)
    assert events == set(protocol.EVENTS)


def test_the_hello_line_names_this_protocol():
    assert lines()[0] == {**lines()[0], "event": "hello", "protocol": protocol.PROTOCOL}


@pytest.mark.parametrize(
    ("message", "named"),
    [
        ({"id": 1, "op": "send", "room": "!r:s", "type": "m.room.message", "content": {}}, "missing txn_id"),
        ({"id": 1, "op": "ack", "seq": 1, "extra": 2}, "unknown extra"),
        ({"id": -1, "op": "ack", "seq": 1}, "id must be"),
        ({"id": 1, "op": "fly"}, "op must be one of start"),
    ],
)
def test_a_request_that_does_not_fit_is_refused_naming_the_field(message, named):
    with pytest.raises(protocol.ProtocolError, match=named):
        protocol.check_request(message)


def test_a_result_with_a_field_the_schema_does_not_show_is_refused():
    with pytest.raises(protocol.ProtocolError, match="unknown room"):
        protocol.check_from_helper({"id": 3, "ok": True, "result": {"event_id": "$e", "room": "!r"}}, {3: "send"})
    with pytest.raises(protocol.ProtocolError, match="unknown extra"):
        protocol.check_from_helper({"event": "alert", "kind": "x", "message": "y", "extra": 1}, {})
