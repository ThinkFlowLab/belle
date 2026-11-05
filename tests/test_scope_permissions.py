"""Scoped permission behavior without a host application."""

import pytest

from belle_agent.integrations.openjiuwen.scopes import permissions
from belle_agent.scopes import ChannelCapabilities, compile_scopes, register_channel
from belle_agent.scopes.capabilities import restore_registry, snapshot_registry


@pytest.fixture
def channel():
    previous = snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"permissions": None},
            attended=False,
        )
    )
    yield
    restore_registry(previous)


@pytest.fixture
def narrow(monkeypatch):
    def apply(base, override):
        return {**base, "tools": {**base.get("tools", {}), **override}}

    monkeypatch.setattr(permissions, "_narrow_permissions", lambda: apply)


def test_unattended_ask_becomes_a_denial(channel, narrow):
    scopes = compile_scopes([{"match": {"channel": "demo"}, "permissions": {"tools": {"bash": "ask"}}}])

    assert permissions.denied_tool_level(scopes, "bash", channel="demo") == "deny"


def test_narrowing_prunes_approval_override(channel, narrow):
    scopes = compile_scopes([{"match": {"channel": "demo"}, "permissions": {"tools": {"bash": "deny"}}}])
    base = {
        "tools": {"bash": "allow", "write_file": "allow"},
        "approval_overrides": [{"id": "probe", "tools": ["bash", "write_file"]}],
    }

    result = permissions.narrow_config_for_scopes(base, scopes, channel="demo")

    assert result["tools"] == {"bash": "deny", "write_file": "allow"}
    assert result["approval_overrides"] == [{"id": "probe", "tools": ["write_file"]}]
    assert base["approval_overrides"][0]["tools"] == ["bash", "write_file"]


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"tool": {"bash": "deny"}}, "scopes[0].permissions.tool is not a permissions setting. The settings are tools"),
        ({"tools": {"bash": "dney"}}, "scopes[0].permissions.tools.bash='dney' is not one of allow/ask/deny"),
    ],
)
def test_a_typo_in_a_restriction_stops_the_start_rather_than_lifting_it(channel, refusal, section, expected):
    assert expected in refusal([{"match": {"channel": "demo"}, "permissions": section}])
