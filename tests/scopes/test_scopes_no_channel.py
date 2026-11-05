"""Values of a rule with no channel, checked against every registered channel."""

from pathlib import Path

import pytest
import yaml

from belle_agent.platforms.discord.capabilities import DISCORD_CAPABILITIES
from belle_agent.platforms.loopback import LOOPBACK_CAPABILITIES
from belle_agent.platforms.matrix.capabilities import MATRIX_CAPABILITIES
from belle_agent.platforms.mattermost.capabilities import MATTERMOST_CAPABILITIES
from belle_agent.platforms.slack.scope_capabilities import slack_capabilities
from belle_agent.scopes import capabilities as caps
from belle_agent.scopes import CLICK_APPROVE, click_rule, compile_scopes

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"
ALL = (LOOPBACK_CAPABILITIES, slack_capabilities(), MATTERMOST_CAPABILITIES, MATRIX_CAPABILITIES, DISCORD_CAPABILITIES)


@pytest.fixture
def only(registry):
    def declare(*declarations):
        caps.restore_registry({})
        for declaration in declarations:
            registry(declaration)

    return declare


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"delivery": {"mode": ["mentoin"]}}, "scopes[0].delivery.mode names 'mentoin', which is not a trigger"),
        ({"delivery": {"mode": "mention"}}, "scopes[0].delivery.mode='mention' is one word"),
        ({"delivery": {"events": {"reaction": True}}}, "scopes[0].delivery.events sets reaction=True"),
    ],
)
def test_a_slack_value_with_no_channel_is_checked_as_slack_checks_it(only, refusal, section, expected):
    only(*ALL)

    error = refusal([section])

    assert expected in error
    assert "A rule with no channel applies on slack" in error


def test_a_shared_value_is_checked_on_a_rule_with_no_channel(only, refusal):
    only(*ALL)

    error = refusal([{"agent": {"skills": {"requird": ["x"]}}}])

    assert "scopes[0].agent.skills names unsupported skill settings ['requird']" in error
    assert "A rule with no channel" not in error


def test_a_shared_value_is_checked_with_no_channel_registered(only, refusal):
    only()

    assert "scopes[0].agent.expert=' x' must be one expert package name" in refusal([{"agent": {"expert": " x"}}])


def test_a_value_every_channel_accepts_compiles_unchanged(only, warn, warnings):
    only(*ALL)

    scopes = compile_scopes([{"delivery": {"mode": ["mention", "url"]}, "agent": {"skills": {"required": ["a"]}}}], warn=warn)

    assert warnings == []
    assert dict(scopes[0].section("delivery")) == {"mode": ["mention", "url"]}
    assert dict(scopes[0].section("agent")) == {"skills": {"required": ["a"]}}


def test_the_example_compiles_the_same_with_every_channel_registered(only, warn):
    rules = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["scopes"]
    only()
    alone = compile_scopes(rules, warn=warn)
    only(*ALL)

    assert compile_scopes(rules, warn=warn) == alone


def test_a_click_role_with_no_channel_is_refused(only, refusal):
    only(*ALL)

    error = refusal(
        [{"clicks": {"approve": {"role": "operator"}}}],
        people={"alice": {"slack": "U_ALICE"}},
        roles={"operator": ["alice"]},
    )

    assert "scopes[0].clicks.approve.role=operator names a role but the match names no channel" in error
    assert "Add channel to the match block" in error


def test_a_click_user_with_no_channel_compiles(only, warn):
    only(*ALL)

    scopes = compile_scopes([{"clicks": {"approve": {"user": ["U_ALICE"]}}}], warn=warn)

    assert click_rule(scopes, channel="slack", kind=CLICK_APPROVE).users == ("U_ALICE",)
