"""The core settles tool levels without a host library."""

import pytest

from belle_agent.scopes import ChannelCapabilities, compile_scopes, strictest_tool_levels


@pytest.fixture
def channels(registry):
    for name, attended in (("demo", True), ("unwatched", False)):
        registry(ChannelCapabilities(channel=name, axes=frozenset({"channel", "chat"}), sections={"permissions": None}, attended=attended))


def _scope(tools, *, channel="demo", chat=None):
    match = {"channel": channel, **({"chat": chat} if chat else {})}
    return {"match": match, "permissions": {"tools": tools}}


CASES = [
    ([_scope({"bash": "ask"}), _scope({"bash": "deny"}, chat="C1")], "demo", "C1", {"bash": "deny"}),
    ([_scope({"bash": "deny"}), _scope({"bash": "ask"}, chat="C1")], "demo", "C1", {"bash": "deny"}),
    ([_scope({"bash": "ask"}), _scope({"write_file": "deny"}, chat="C1")], "demo", "C1", {"bash": "ask", "write_file": "deny"}),
    ([_scope({"bash": "deny"}, chat="C1")], "demo", "C2", {}),
    ([_scope({"bash": "ask"}, channel="unwatched")], "unwatched", None, {"bash": "deny"}),
    ([{"match": {}, "permissions": {"tools": {"bash": "ask"}}}], "tui", None, {"bash": "ask"}),
]


@pytest.mark.parametrize(("rules", "channel", "chat", "expected"), CASES)
def test_the_strictest_matching_level_wins_in_any_order(channels, rules, channel, chat, expected):
    for ordered in (rules, rules[::-1]):
        scopes = compile_scopes(ordered, warn=lambda *_: None)
        assert strictest_tool_levels(scopes, channel=channel, chat=chat) == expected


@pytest.mark.parametrize(("rules", "channel", "chat", "expected"), CASES)
def test_the_core_fold_agrees_with_the_openjiuwen_narrowing(channels, rules, channel, chat, expected):
    pytest.importorskip("openjiuwen")
    from belle_agent.integrations.openjiuwen.scopes.permissions import settled_tool_levels

    scopes = compile_scopes(rules, warn=lambda *_: None)
    assert strictest_tool_levels(scopes, channel=channel, chat=chat) == settled_tool_levels(scopes, channel=channel, chat=chat)
