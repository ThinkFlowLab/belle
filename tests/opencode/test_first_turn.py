"""One turn across the two containers with the stand-in model."""

import json
import socket

import pytest
from drive import text, turn
from standin import said

pytestmark = pytest.mark.integration


def test_a_stand_in_turn_crosses_the_two_containers(client, events, relay, model):
    session = client.session()
    end = turn(client, events, session, "hello SAY=FIRST_TURN")
    assert end["type"] == "session.execution.succeeded"
    assert text(events.of(session)) == "FIRST_TURN"
    requests = [
        entry for entry in model.log if entry["session"] == session and entry["body"] in model.requests(session)
    ]
    assert len(requests) == 1
    assert requests[0]["headers"]["x-opencode-session-id"] == session
    assert "authorization" not in requests[0]["headers"]
    with open(model.log_path) as handle:
        assert all("authorization" not in json.loads(line)["headers"] for line in handle)


def test_a_relay_hook_round_trip_crosses_the_network(client, events, relay, model):
    session = client.session()
    relay.answer.set("prompt", session, {"text": "hello SAY=ANSWER_APPLIED"})
    assert turn(client, events, session, "hello SAY=unchanged")["type"] == "session.execution.succeeded"
    # The hook ran in the OpenCode container; this process, in the Belle container, answered it.
    assert relay.count("prompt", session) == 1
    assert relay.unauthorized == []
    assert text(events.of(session)) == "ANSWER_APPLIED"
    assert said(model.requests(session)[0], "user")[-1] == "hello SAY=ANSWER_APPLIED"


def test_the_belle_container_has_no_outbound_route():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
