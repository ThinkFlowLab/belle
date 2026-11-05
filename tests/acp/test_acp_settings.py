"""``backends.acp`` is read once, and each error names the key and the form it expects."""

from pathlib import Path

import pytest

from belle_agent.config import BelleConfigError, load_config
from belle_agent.integrations.acp.settings import read_settings

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"
PROFILE = {
    "socket": "/run/acp/agent.sock",
    "cwd": "/work",
    "connection": "shared",
    "tools_mount": "session",
    "binding": {"meta": "ai.opencode/sessionID"},
}


def config(changes=None, **section):
    """A configuration with the profile ``oc``, changed by ``changes``, and ``section`` beside it."""
    return {"backends": {"acp": {"profile": "oc", "profiles": {"oc": {**PROFILE, **(changes or {})}}, **section}}}


def test_a_complete_profile_is_read_with_its_defaults():
    settings = read_settings(config(tools={"bind": "0.0.0.0", "port": 18300, "url": "http://belle:18300/mcp"}))
    profile = settings.profile
    assert (profile.socket, profile.cwd, profile.connection, profile.tools_mount) == (
        "/run/acp/agent.sock",
        "/work",
        "shared",
        "session",
    )
    assert (profile.binding_meta, profile.binding_header, profile.tool_name_format) == (
        "ai.opencode/sessionID",
        "",
        "{name}",
    )
    assert (settings.tools_bind, settings.tools_port, settings.tools_url) == (
        "0.0.0.0",
        18300,
        "http://belle:18300/mcp",
    )
    assert settings.tools_token == ""  # a new one at each start


def test_the_token_is_read_from_the_variable_the_configuration_names(monkeypatch):
    monkeypatch.setenv("BELLE_TOOLS_TOKEN", "s3cret")
    assert read_settings(config(tools_token="${BELLE_TOOLS_TOKEN}")).tools_token == "s3cret"
    monkeypatch.delenv("BELLE_TOOLS_TOKEN")
    assert read_settings(config(tools_token="${BELLE_TOOLS_TOKEN}")).tools_token == ""  # a new one at each start
    with pytest.raises(BelleConfigError, match="tools_token is needed by tools_mount: agent"):
        read_settings(config({"tools_mount": "agent"}, tools_token="${BELLE_TOOLS_TOKEN}"))


@pytest.mark.parametrize(
    ("profile", "section", "named"),
    [
        ({"socket": "relative.sock"}, {}, "socket must be an absolute path"),
        ({"cwd": "work"}, {}, "cwd must be an absolute path"),
        ({"connection": "pooled"}, {}, "connection must be shared or per_conversation"),
        ({"tools_mount": "both"}, {}, "tools_mount must be session, agent or none"),
        (
            {"tools_mount": "none"},
            {},
            "binding is required by tools_mount: session or agent, and refused with tools_mount: none",
        ),
        ({"binding": {"meta": "a", "header": "b"}}, {}, "binding must be a mapping with one key"),
        ({"binding": {"header": "X-Belle-Turn"}}, {}, "binding.header needs connection: per_conversation"),
        ({"connection": "per_conversation"}, {}, "binding.meta needs connection: shared"),
        ({"start_timeout_seconds": 0}, {}, "start_timeout_seconds must be a number of seconds above 0"),
        ({"remembered_requests": 2.5}, {}, "remembered_requests must be a whole number"),
        ({"cancel_timeout_seconds": float("nan")}, {}, "cancel_timeout_seconds must be a number of seconds above 0"),
        ({"permission_timeout_seconds": "inf"}, {}, "permission_timeout_seconds must be a number of seconds above 0"),
        ({"remembered_requests": float("inf")}, {}, "remembered_requests must be a whole number"),
        ({"tool_name_format": "belle_{name}_{other}"}, {}, "tool_name_format must hold {name} once"),
        ({"tool_name_format": "belle"}, {}, "tool_name_format must hold {name} once"),
        ({"retries": 3}, {}, "has unknown keys: retries"),
        ({"tools_mount": "agent"}, {}, "tools_token is needed by tools_mount: agent"),
        ({}, {"profile": "da"}, "profile names 'da', which is not in backends.acp.profiles"),
        ({}, {"verbose": True}, "has unknown keys: verbose"),
    ],
)
def test_a_value_that_does_not_fit_is_refused_by_name(profile, section, named):
    with pytest.raises(BelleConfigError, match=named.replace("{", r"\{").replace("}", r"\}")):
        read_settings(config(profile, **section))


def test_a_profile_without_a_mount_has_no_binding_and_needs_no_token():
    unbound = {key: value for key, value in PROFILE.items() if key != "binding"}
    settings = read_settings(
        {"backends": {"acp": {"profile": "oc", "profiles": {"oc": {**unbound, "tools_mount": "none"}}}}}
    )
    assert (settings.profile.tools_mount, settings.profile.binding_meta, settings.profile.binding_header) == (
        "none",
        "",
        "",
    )
    assert settings.tools_token == ""
    # A mount needs its binding.
    with pytest.raises(BelleConfigError, match="binding is required by tools_mount"):
        read_settings({"backends": {"acp": {"profile": "oc", "profiles": {"oc": unbound}}}})


def test_a_profile_that_is_not_selected_is_checked_too():
    broken = {**PROFILE, "connection": "pooled"}
    with pytest.raises(BelleConfigError, match=r"backends\.acp\.profiles\.spare\.connection must be shared"):
        read_settings(config(profiles={"oc": PROFILE, "spare": broken}))


def test_a_missing_key_is_named():
    with pytest.raises(BelleConfigError, match="needs cwd$"):
        read_settings(
            {
                "backends": {
                    "acp": {
                        "profile": "oc",
                        "profiles": {"oc": {"socket": "/s", "connection": "shared", "tools_mount": "session"}},
                    }
                }
            }
        )


@pytest.mark.parametrize("profile", ["opencode", "docker_agent"])
def test_the_example_configuration_reads_for_each_profile(monkeypatch, profile):
    for name, value in {
        "BELLE_ACP_PROFILE": profile,
        "BELLE_ACP_SOCKET": "/run/belle-acp/agent.sock",
        "BELLE_TOOLS_TOKEN": "s3cret",
        "BELLE_TOOLS_PORT": "18300",
        "BELLE_TOOLS_URL": "http://belle:18300/mcp",
    }.items():
        monkeypatch.setenv(name, value)
    settings = read_settings(load_config(EXAMPLE))
    assert (settings.profile.name, settings.tools_token, settings.tools_port) == (profile, "s3cret", 18300)


def test_the_example_configuration_reads_the_opencode_profile_without_a_token(monkeypatch):
    monkeypatch.setenv("BELLE_ACP_PROFILE", "opencode")
    monkeypatch.setenv("BELLE_ACP_SOCKET", "/run/belle-acp/agent.sock")
    monkeypatch.delenv("BELLE_TOOLS_TOKEN", raising=False)
    monkeypatch.setenv("BELLE_TOOLS_URL", "http://belle:18300/mcp")
    settings = read_settings(load_config(EXAMPLE))
    assert (settings.profile.name, settings.tools_token) == ("opencode", "")
