"""Scopes that name a platform which is off in this process."""

import logging
from pathlib import Path

import pytest
import yaml

from belle_agent.platforms.discord.capabilities import DISCORD_CAPABILITIES
from belle_agent.platforms.loopback import LOOPBACK_CAPABILITIES
from belle_agent.platforms.matrix.capabilities import MATRIX_CAPABILITIES
from belle_agent.platforms.mattermost.capabilities import MATTERMOST_CAPABILITIES
from belle_agent.platforms.slack.scope_capabilities import slack_capabilities
from belle_agent.scopes import capabilities as caps
from belle_agent.scopes import compile_scopes, compose_section, schema

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"


@pytest.fixture
def only(registry, monkeypatch):
    """Register exactly the given declarations, and forget which platforms were reported."""
    monkeypatch.setattr(schema, "_REPORTED_OFF", set())

    def declare(*declarations):
        caps.restore_registry({})
        for declaration in declarations:
            registry(declaration)

    return declare


@pytest.fixture
def lines(caplog):
    caplog.set_level(logging.INFO, logger=schema.__name__)
    return caplog


def _off_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "is off in this process" in record.getMessage()]


def test_scopes_of_a_platform_that_is_off_give_one_line_and_no_warning(only, warn, warnings, lines):
    only(LOOPBACK_CAPABILITIES)
    rules = [
        {"match": {"channel": "mattermost"}, "delivery": {"mid_turn": "queue"}},
        {"match": {"channel": "mattermost", "chat_type": "D"}, "delivery": {"prompt": "hello"}},
    ]

    compile_scopes(rules, warn=warn)
    compile_scopes(rules, warn=warn)

    assert warnings == []
    assert _off_lines(lines) == [
        "scopes: the platform mattermost is off in this process, so 2 scopes with match.channel=mattermost do not apply"
    ]


@pytest.mark.parametrize("section", [{"delivery": {"mode": ["all"]}}, {"permissions": {"tools": {"bash": "deny"}}}])
def test_a_channel_that_names_no_platform_is_refused(only, refusal, lines, section):
    only(LOOPBACK_CAPABILITIES)

    error = refusal([{"match": {"channel": "slak"}, **section}])

    assert "scopes[0].match.channel=slak is not a platform or a connector. The channels are discord, loopback," in error
    assert _off_lines(lines) == []


@pytest.mark.parametrize(
    ("declaration", "off"),
    [
        (MATTERMOST_CAPABILITIES, {"slack", "matrix", "discord"}),
        (slack_capabilities(), {"mattermost", "matrix", "discord"}),
        (MATRIX_CAPABILITIES, {"slack", "mattermost", "discord"}),
        (DISCORD_CAPABILITIES, {"slack", "mattermost", "matrix"}),
    ],
)
def test_the_example_scopes_warn_nothing_on_one_platform_alone(only, warn, warnings, lines, declaration, off):
    only(declaration)

    scopes = compile_scopes(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["scopes"], warn=warn)

    assert warnings == []
    assert {line.split(",")[0] for line in _off_lines(lines)} == {f"scopes: the platform {name} is off in this process" for name in off}
    assert {scope.match.channel for scope in scopes} == {"slack", "mattermost", "matrix", "discord"}


@pytest.mark.parametrize(
    ("channel", "chat_type", "unavailable"),
    [
        ("mattermost", "D", ["slack-app-home", "slack-block-kit-rendering", "slack-daily-update"]),
        ("mattermost", "O", ["slack-app-home", "slack-block-kit-rendering", "slack-daily-update"]),
        ("matrix", "direct", ["slack-app-home", "slack-block-kit-rendering", "slack-daily-update"]),
        ("matrix", "public", ["slack-app-home", "slack-block-kit-rendering", "slack-daily-update"]),
        ("discord", "dm", ["slack-app-home", "slack-block-kit-rendering", "slack-daily-update"]),
        ("discord", "text", ["slack-app-home", "slack-block-kit-rendering", "slack-daily-update"]),
        ("slack", "im", None),
        ("slack", "channel", None),
    ],
)
def test_the_example_loads_command_help_in_dms_and_limits_slack_skills(only, warn, warnings, channel, chat_type, unavailable):
    only(MATTERMOST_CAPABILITIES, slack_capabilities(), MATRIX_CAPABILITIES, DISCORD_CAPABILITIES)
    scopes = compile_scopes(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["scopes"], warn=warn)

    skills = compose_section(scopes, channel=channel, chat_type=chat_type, section="agent")["skills"]

    assert warnings == []
    assert skills.get("unavailable") == unavailable
    assert "messaging-context" in skills["required"]
    assert ("belle-command-help" in skills["required"]) == (chat_type in {"D", "direct", "dm", "im"})
    assert ("slack-app-home" in skills["required"]) == (channel == "slack" and chat_type == "im")
