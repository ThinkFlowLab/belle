"""Scope readers share Belle's configuration and validation."""

import pytest
from belle_agent.config import BelleConfigError

from belle_agent.integrations.jiuwenswarm import scope_permissions
from belle_agent.integrations.jiuwenswarm import config as belle_config


def test_slack_and_tool_permissions_read_the_same_belle_file(tmp_path, monkeypatch):
    import jiuwenswarm.common.config as config_module

    path = tmp_path / "belle.yaml"
    settings = {"channels": {}}
    monkeypatch.setenv("BELLE_AGENT_CONFIG", str(path))
    monkeypatch.setattr(config_module, "get_config", lambda: settings)
    scope_permissions.reset_cache()
    try:
        for user_id in ("U1", "U2"):
            path.write_text(
                "platforms: {slack: {enforce_audience: true}}\n"
                f"people: {{alice: {{slack: {user_id}}}}}\n"
                "roles: {operators: [alice]}\n"
                "scopes:\n"
                "  - match: {channel: slack, chat: C1, role: operators}\n"
                "    delivery: {prompt: Be concise.}\n",
                encoding="utf-8",
            )
            slack = belle_config.load_slack_scopes()
            runtime = scope_permissions.runtime_scopes()
            assert slack == runtime
            assert runtime[0].match.users == (user_id,)
    finally:
        scope_permissions.reset_cache()


def test_malformed_belle_scopes_fail_at_both_readers(tmp_path, monkeypatch):
    import jiuwenswarm.common.config as config_module

    path = tmp_path / "belle.yaml"
    path.write_text("platforms: {slack: {enforce_audience: true}}\nscopes: {}\n", encoding="utf-8")
    monkeypatch.setenv("BELLE_AGENT_CONFIG", str(path))
    monkeypatch.setattr(
        config_module, "get_config",
        lambda: {"channels": {}},
    )
    scope_permissions.reset_cache()
    with pytest.raises(BelleConfigError, match="scopes"):
        belle_config.load_slack_scopes()
    with pytest.raises(BelleConfigError, match="scopes"):
        scope_permissions.runtime_scopes()
