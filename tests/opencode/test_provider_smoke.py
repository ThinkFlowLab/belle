"""Provider smoke: a plain turn and one MCP tool call, on the real provider.

Run with OCB_PROVIDER=1, beside OpenCode on the real provider. The key is in
the OpenCode container only. Nothing here reads, prints or stores it: the checks
read events, the stored session and the MCP server's log.
"""

import secrets
import socket

import pytest
from drive import mount, text, turn

pytestmark = pytest.mark.provider


def test_a_plain_turn_on_the_provider(client, events, relay):
    session = client.session()
    end = turn(client, events, session, "Reply with the single word ready.", timeout=180)
    assert end["type"] == "session.execution.succeeded", end
    assert text(events.of(session)).strip()
    assert relay.count("prompt", session) == 1 and relay.count("context", session) >= 1


def test_the_model_calls_an_mcp_tool_hosted_in_the_belle_container(client, events, relay, mcp):
    mount(client, "belle")
    token = "SMOKE_" + secrets.token_hex(4)
    session = client.session()
    end = turn(
        client,
        events,
        session,
        f"Call the tool belle_echo once with the token {token}. Then tell me exactly what it returned.",
        timeout=240,
    )
    assert end["type"] == "session.execution.succeeded", end
    calls = [c for c in mcp.calls("echo") if c["params"]["arguments"].get("token") == token]
    assert len(calls) == 1, [c["params"] for c in mcp.calls()]
    assert calls[0]["params"]["_meta"]["ai.opencode/sessionID"] == session
    assert relay.count("execute.before", session) == 1 and relay.count("evaluate", session) == 1


def test_only_the_opencode_container_has_an_outbound_route():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
