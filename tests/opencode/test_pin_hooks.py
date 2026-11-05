"""Every hook the relay names fires on the pinned build.

The plugin API accepts an unknown hook name without error, so a name is proven
only by a run in which the hook fires. This check runs on every image build
and on every pin change, and fails for each name with no round trip.
"""

import os
import re
from pathlib import Path

import pytest
from drive import mount, turn
from standin import call

RELAY = Path(__file__).parents[2] / os.environ.get("OCB_RELAY", "src/belle_agent/integrations/opencode/plugin/relay.ts")


def relay_hooks(source: str) -> list[str]:
    """The hook names a relay source registers, in order."""
    return re.findall(r"""ctx\.\w+\.hook\(\s*["']([^"']+)["']""", source)


def silent_hooks(source: str, fired: set[str]) -> list[str]:
    return [name for name in relay_hooks(source) if name not in fired]


def test_the_relay_names_its_hooks():
    assert relay_hooks(RELAY.read_text()) == ["prompt", "context", "execute.before", "evaluate", "http.response"]


def test_a_name_that_never_fires_is_reported():
    source = RELAY.read_text() + '\n    await ctx.session.hook("prompt.before", async () => {})\n'
    fired = set(relay_hooks(RELAY.read_text()))
    assert silent_hooks(source, fired) == ["prompt.before"]
    assert silent_hooks(source, set()) == [*relay_hooks(RELAY.read_text()), "prompt.before"]


@pytest.mark.integration
def test_every_hook_the_relay_names_fires(client, events, relay, model, mcp):
    mount(client, "pin")
    session = client.session()
    # `response` asks the relay for this turn's provider response bodies.
    relay.answer.set("prompt", session, {"response": True})
    relay.answer.set("http.response", session, {})
    # One tool call: `execute.before` and `evaluate` fire once each.
    assert turn(client, events, session, call("pin_echo", token="PIN"))["type"] == "session.execution.succeeded"
    assert [c["params"]["arguments"] for c in mcp.calls("echo")][-1] == {"token": "PIN"}
    fired = {hook for _, hook, event in relay.hits if event.get("sessionID") == session}
    assert silent_hooks(RELAY.read_text(), fired) == [], (
        "the relay registers a hook that never fired on this build: an unknown hook name is accepted silently"
    )
