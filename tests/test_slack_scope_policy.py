import yaml

from belle_agent.platforms.slack.scope_policy import (
    TIERS,
    render_manifest,
    scopes_for_tier,
)


def test_scope_tiers_are_nested():
    previous = set()
    for tier in TIERS:
        scopes = set(scopes_for_tier(tier))
        assert previous <= scopes
        previous = scopes


def test_manifest_respects_names():
    manifest = yaml.safe_load(render_manifest(TIERS[-1], command_name="example", bot_name="Example"))
    assert manifest["display_information"]["name"] == "Example"
    assert manifest["features"]["slash_commands"][0]["command"] == "/example"
