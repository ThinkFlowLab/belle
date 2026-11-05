"""Committed Slack app manifests and the policy that generates them."""

import importlib.util
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from belle_agent.platforms.slack import scope_policy as slack_scope_policy
from belle_agent.platforms.slack.scope_policy import (
    AGENT_DESCRIPTION_MAX,
    AGENT_VIEW_TIER,
    COMMAND_REQUIREMENTS,
    DEFAULT_COMMAND_NAME,
    EVENT_REQUIREMENTS,
    METHOD_OPTION_REQUIREMENTS,
    METHOD_REQUIREMENTS,
    TIER_GATED,
    TIER_NAMES,
    TIER_SUMMARIES,
    TIERS,
    agent_description,
    agent_view_for_tier,
    command_names,
    commands_for_tier,
    events_for_tier,
    every_requirement,
    manifest_filename,
    render_manifest,
    scopes_for_tier,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_DIR = _REPO_ROOT / "src" / "belle_agent" / "platforms" / "slack" / "manifests"
_GENERATOR = _REPO_ROOT / "scripts" / "generate_slack_manifest.py"


def _generator_outputs() -> dict[Path, str]:
    spec = importlib.util.spec_from_file_location("generate_slack_manifest", _GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.outputs()


@pytest.fixture(scope="module")
def manifests() -> dict[int, dict]:
    return {
        tier: yaml.safe_load((_MANIFEST_DIR / manifest_filename(tier)).read_text(encoding="utf-8")) for tier in TIERS
    }


def _bot_scopes(manifest: dict) -> set[str]:
    return set(manifest["oauth_config"]["scopes"]["bot"])


def _bot_events(manifest: dict) -> set[str]:
    return set(manifest["settings"]["event_subscriptions"]["bot_events"])


def _agent_view(manifest: dict) -> dict | None:
    return manifest["features"].get("agent_view")


def _slash_commands(manifest: dict) -> dict[str, dict]:
    entries = manifest["features"].get("slash_commands") or []
    return {entry["command"]: entry for entry in entries}


def _all_required_scopes() -> set[str]:
    required: set[str] = set()
    for requirement in every_requirement():
        required |= set(requirement.scopes)
    return required


def test_manifests_are_up_to_date() -> None:
    """The committed files are exactly what the generator produces now."""
    for path, expected in _generator_outputs().items():
        assert path.exists(), f"{path} is missing; run scripts/generate_slack_manifest.py"
        actual = path.read_text(encoding="utf-8")
        assert actual == expected, (
            f"{path.name} is stale. Run scripts/generate_slack_manifest.py and"
            " commit the result; do not edit a generated manifest by hand."
        )


def test_one_manifest_per_tier() -> None:
    written = {path.name for path in _generator_outputs()}
    on_disk = {path.name for path in _MANIFEST_DIR.glob("*.yaml")}
    assert written == on_disk, (
        "the manifest directory holds files the generator does not own, or is"
        f" missing one it does: generator={sorted(written)} on disk={sorted(on_disk)}"
    )


def test_top_tier_covers_every_scope_the_policy_asks_for(manifests: dict[int, dict]) -> None:
    missing = sorted(_all_required_scopes() - _bot_scopes(manifests[TIERS[-1]]))
    assert not missing, "the top tier does not ask for scopes the policy module requires: " + ", ".join(missing)


def test_top_tier_asks_for_nothing_the_policy_does_not(manifests: dict[int, dict]) -> None:
    """A scope nothing needs is a permission every install holds for no reason."""
    extra = sorted(_bot_scopes(manifests[TIERS[-1]]) - _all_required_scopes())
    assert not extra, "the top tier asks for scopes nothing needs: " + ", ".join(extra)


def test_tiers_nest(manifests: dict[int, dict]) -> None:
    """Choosing a lower tier is always a narrowing and never a different app."""
    for lower, higher in pairwise(TIERS):
        assert _bot_scopes(manifests[lower]) <= _bot_scopes(manifests[higher]), (
            f"tier {lower} asks for a scope tier {higher} does not"
        )
        assert _bot_events(manifests[lower]) <= _bot_events(manifests[higher]), (
            f"tier {lower} subscribes to an event tier {higher} does not"
        )
        # A feature block and not a grant, so the scope comparison above says
        # nothing about it. Without this line a rung could declare the agent
        # surface while the rung above it dropped the block, and every other
        # assertion here would still pass.
        if _agent_view(manifests[lower]) is not None:
            assert _agent_view(manifests[higher]) is not None, (
                f"tier {lower} declares features.agent_view and tier {higher}"
                " does not, so the ladder is not a narrowing"
            )


def test_each_manifest_matches_its_tier(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        assert _bot_scopes(manifests[tier]) == set(scopes_for_tier(tier))
        assert _bot_events(manifests[tier]) == set(events_for_tier(tier))


def test_each_manifest_declares_the_commands_of_its_tier(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        assert sorted(_slash_commands(manifests[tier])) == commands_for_tier(tier)


def test_every_declared_command_carries_its_scope(manifests: dict[int, dict]) -> None:
    """A command without ``commands`` is refused when the app is saved."""
    for tier in TIERS:
        scopes = _bot_scopes(manifests[tier])
        ids = {name: key for key, name in command_names().items()}
        for name in _slash_commands(manifests[tier]):
            command = COMMAND_REQUIREMENTS[ids[name]]
            missing = sorted(set(command.requirement.scopes) - scopes)
            assert not missing, f"tier {tier} declares {name} without {', '.join(missing)}"


def test_commands_need_no_request_url(manifests: dict[int, dict]) -> None:
    """Socket Mode delivers a command on the WebSocket, as it does an event."""
    for tier in TIERS:
        for name, entry in _slash_commands(manifests[tier]).items():
            assert "url" not in entry, f"tier {tier} gives {name} a request URL"
            assert len(name) <= 32 and name.startswith("/")


def test_every_subscribed_event_carries_its_scope(manifests: dict[int, dict]) -> None:
    """A subscription without its scope is accepted by Slack and never delivered."""
    for tier in TIERS:
        scopes = _bot_scopes(manifests[tier])
        for event in sorted(_bot_events(manifests[tier])):
            missing = sorted(set(EVENT_REQUIREMENTS[event].scopes) - scopes)
            assert not missing, f"tier {tier} subscribes to {event} without {', '.join(missing)}"


# ── settings that are not scopes ────────────────────────────────────────────


def test_socket_mode_and_no_request_url(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        settings = manifests[tier]["settings"]
        assert settings["socket_mode_enabled"] is True
        assert "request_url" not in settings["event_subscriptions"]


def test_no_manifest_describes_distribution(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        manifest = manifests[tier]
        assert manifest["settings"].get("org_deploy_enabled") is False
        assert manifest["settings"].get("token_rotation_enabled") is False
        oauth = manifest["oauth_config"]
        assert "redirect_urls" not in oauth
        assert "user" not in oauth["scopes"]


def test_a_renamed_command_reaches_every_line_that_names_it() -> None:
    text = render_manifest(1, "acme-bot")
    assert "    - command: /acme-bot" in text
    assert "/acme-bot (a slash command typed in Slack)" in text
    assert "the /acme-bot slash command" in text
    assert f"/{DEFAULT_COMMAND_NAME}" not in text
    assert commands_for_tier(1, "acme-bot") == ["/acme-bot"]


def test_a_renamed_bot_reaches_every_line_that_names_it() -> None:
    text = render_manifest(1, bot_name="Acme Bot")
    assert "# Slack app manifest for Acme Bot -- tier 1, connector." in text
    assert "  name: Acme Bot" in text
    assert "  description: Acme Bot agent runtime, connected over Socket Mode." in text
    assert "    display_name: Acme Bot" in text
    assert "      description: Run a Acme Bot command" in text
    assert "Acme Bot connects an agent runtime to this workspace" in text


# ── the agent surface ─────────────────────────────────────────────


def test_only_the_agent_view_tier_declares_the_surface(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        present = _agent_view(manifests[tier]) is not None
        assert present is agent_view_for_tier(tier), (
            f"tier {tier} {'declares' if present else 'omits'} features.agent_view"
            f" and agent_view_for_tier says {agent_view_for_tier(tier)}"
        )
    assert _agent_view(manifests[AGENT_VIEW_TIER]) is not None


def test_the_agent_surface_and_its_scope_travel_together(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        has_block = _agent_view(manifests[tier]) is not None
        has_scope = "assistant:write" in _bot_scopes(manifests[tier])
        assert has_block is has_scope, (
            f"tier {tier} declares agent_view={has_block} and holds assistant:write={has_scope}"
        )


def test_the_agent_surface_needs_the_messages_tab(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        if _agent_view(manifests[tier]) is None:
            continue
        app_home = manifests[tier]["features"]["app_home"]
        assert app_home["messages_tab_enabled"] is True
        assert app_home["messages_tab_read_only_enabled"] is False


def test_the_agent_description_is_present_and_within_slacks_limit(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        block = _agent_view(manifests[tier])
        if block is None:
            continue
        description = block["agent_description"]
        assert isinstance(description, str) and description.strip()
        assert len(description) <= AGENT_DESCRIPTION_MAX


def test_a_long_bot_name_is_refused_rather_than_trimmed() -> None:
    assert len(agent_description()) <= AGENT_DESCRIPTION_MAX
    with pytest.raises(ValueError):
        agent_description("x" * AGENT_DESCRIPTION_MAX)
    with pytest.raises(ValueError):
        render_manifest(AGENT_VIEW_TIER, bot_name="x" * AGENT_DESCRIPTION_MAX)


def test_the_agent_surface_declares_nothing_it_cannot_fill(manifests: dict[int, dict]) -> None:
    for tier in TIERS:
        block = _agent_view(manifests[tier])
        if block is None:
            continue
        assert set(block) == {"agent_description"}, (
            f"tier {tier} declares agent_view fields nothing fills: {sorted(set(block) - {'agent_description'})}"
        )


def test_a_renamed_bot_reaches_the_agent_description() -> None:
    text = render_manifest(AGENT_VIEW_TIER, bot_name="Acme Bot")
    assert "    agent_description: Acme Bot answers questions" in text


def test_every_tier_has_a_name_and_a_summary() -> None:
    for tier in TIERS:
        assert TIER_NAMES[tier].strip()
        assert TIER_SUMMARIES[tier].strip()
    assert len({TIER_NAMES[tier] for tier in TIERS}) == len(TIERS)


def test_the_gated_rung_is_the_top_one() -> None:
    assert TIERS[-1] == TIER_GATED
    assert AGENT_VIEW_TIER == TIER_GATED


def test_the_gated_summary_says_pasting_can_fail(manifests: dict[int, dict]) -> None:
    summary = TIER_SUMMARIES[TIER_GATED]
    assert "agent_view" in summary
    assert "assistant:write" in summary
    text = render_manifest(TIER_GATED)
    for fragment in ("agent feature", "Messages tab"):
        assert fragment in text, f"the gated manifest never says {fragment!r}"


def test_an_unknown_tier_is_refused() -> None:
    with pytest.raises(ValueError):
        render_manifest(max(TIERS) + 1)


def test_every_classified_option_belongs_to_a_classified_method() -> None:
    stray = sorted(method for method, _option in METHOD_OPTION_REQUIREMENTS if method not in METHOD_REQUIREMENTS)
    assert not stray, "METHOD_OPTION_REQUIREMENTS names methods METHOD_REQUIREMENTS does not classify: " + ", ".join(
        stray
    )


def test_an_option_never_buys_its_scope_below_its_own_tier() -> None:
    tiers = slack_scope_policy.scope_tiers()
    for (method, option), requirement in METHOD_OPTION_REQUIREMENTS.items():
        for scope in requirement.scopes:
            assert tiers[scope] >= requirement.tier, (
                f"{method} {option} puts {scope} at tier {tiers[scope]}, below"
                f" the tier {requirement.tier} it was classified at"
            )
            assert scope not in METHOD_REQUIREMENTS[method].scopes, (
                f"{scope} is on {method} itself as well, so every install that can call {method} holds it"
            )
