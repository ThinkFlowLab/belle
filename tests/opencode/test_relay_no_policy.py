"""The relay plugin holds no policy: each hook forwards its event and applies the answer.

The rule: a hook handler branches on nothing but the outcome of its
round trip. This reads ``relay.ts`` and fails if a handler conditions on a
conversation, a requester, a tool name or message content, and checks that
every hook makes one round trip and that a failed round trip throws (fails
closed). It is a heuristic over the source, with a mutation self-check that it
catches a policy branch.
"""

import re
from pathlib import Path

RELAY = Path(__file__).parents[2] / "src/belle_agent/integrations/opencode/plugin/relay.ts"

# A handler is `ctx.<domain>.hook("<name>", async (event...) => { <body> })`.
_HOOK = re.compile(r'ctx\.\w+\.hook\(\s*["\'][^"\']+["\']\s*,\s*async\s*\([^)]*\)\s*=>\s*\{', re.DOTALL)

# A condition that is policy, not the outcome of the round trip: naming the
# requester or conversation, comparing the session or the tool to a literal,
# or matching message content against a literal.
_POLICY = [
    re.compile(r"\b(requester|conversation|sender|senderId|workspace|userId|channel)\b"),
    re.compile(r"event\.sessionID\s*[=!]=="),
    re.compile(r"event\.(tool|agent|model)\s*[=!]==\s*[\"'`]"),
    re.compile(r"event\.(prompt|messages|text)[^;{]*\b(includes|match|test|startsWith|endsWith)\s*\("),
]


def _bodies(source: str) -> list[str]:
    """The body of each hook handler, by brace matching from its opening ``{``."""
    bodies = []
    for match in _HOOK.finditer(source):
        depth, start = 1, match.end()
        index = start
        while depth and index < len(source):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            index += 1
        bodies.append(source[start : index - 1])
    return bodies


def _policy_hits(source: str) -> list[str]:
    return [body for body in _bodies(source) for pattern in _POLICY if pattern.search(body)]


def test_each_hook_makes_one_round_trip_and_holds_no_policy():
    source = RELAY.read_text()
    bodies = _bodies(source)
    assert len(bodies) == 5, "expected five hook handlers"
    for body in bodies:
        assert body.count("ask(") == 1, f"a hook handler must make exactly one round trip:\n{body}"
    assert _policy_hits(source) == [], "a hook handler branches on policy, not on its round trip"


def test_a_failed_round_trip_throws():
    source = RELAY.read_text()
    # The one `ask` helper throws unless the response is ok, so every hook fails closed.
    assert re.search(r"if\s*\(\s*!response\.ok\s*\)\s*throw", source), "the round trip must throw when it fails"


def test_the_check_catches_a_policy_branch():
    source = RELAY.read_text()
    # Inject a branch on the conversation into the first hook handler; the check must flag it.
    match = _HOOK.search(source)
    poisoned = source[: match.end()] + '\n      if (event.sessionID === "C1") return' + source[match.end() :]
    assert _policy_hits(poisoned), "the no-policy check failed to catch an injected policy branch"
