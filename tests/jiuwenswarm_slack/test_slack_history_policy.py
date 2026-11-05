"""Connector metadata stamping for Slack audience policy."""

from typing import Any

from belle_agent.platforms.slack.history_policy import (
    METADATA_ENFORCE_AUDIENCE_KEY,
)


def test_one_function_stamps_belle_policy(monkeypatch: Any) -> None:
    from belle_agent.integrations.jiuwenswarm.slack_channel import (
        slack_audience_request_metadata,
    )
    import belle_agent.integrations.jiuwenswarm.slack_routing as routing

    monkeypatch.setattr(
        routing,
        "belle_slack_policy",
        lambda: {"enforce_audience": True},
    )
    stamped = slack_audience_request_metadata()
    assert stamped == {METADATA_ENFORCE_AUDIENCE_KEY: True}


def test_policy_is_read_for_each_new_turn(monkeypatch: Any) -> None:
    from belle_agent.integrations.jiuwenswarm.slack_channel import (
        slack_audience_request_metadata,
    )
    import belle_agent.integrations.jiuwenswarm.slack_routing as routing

    policy = {"enforce_audience": False}
    monkeypatch.setattr(routing, "belle_slack_policy", lambda: policy)
    assert slack_audience_request_metadata()[METADATA_ENFORCE_AUDIENCE_KEY] is False
    policy["enforce_audience"] = True
    assert slack_audience_request_metadata()[METADATA_ENFORCE_AUDIENCE_KEY] is True


def test_slack_channel_config_does_not_copy_belle_policy() -> None:
    from belle_agent.integrations.jiuwenswarm.slack_channel import SlackChannelConfig

    config = SlackChannelConfig(enabled=True)
    for key in ("enforce_audience", "history_never_read", "history_exempt_members"):
        assert not hasattr(config, key)


def test_belle_config_path_comes_from_environment(monkeypatch: Any, tmp_path: Any) -> None:
    import belle_agent.integrations.jiuwenswarm.config as belle_config
    import belle_agent.integrations.jiuwenswarm.slack_routing as routing

    path = tmp_path / "belle.yaml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("BELLE_AGENT_CONFIG", str(path))
    seen: list[Any] = []
    monkeypatch.setattr(belle_config, "load_config", lambda path: seen.append(path) or {"platforms": {"slack": {}}})

    routing.belle_slack_policy()
    monkeypatch.delenv("BELLE_AGENT_CONFIG")
    routing.belle_slack_policy()

    assert seen == [path, None]


def test_bad_belle_config_path_is_not_treated_as_default(monkeypatch: Any) -> None:
    import pytest
    from belle_agent.integrations.jiuwenswarm.slack_routing import belle_slack_policy

    monkeypatch.setenv("BELLE_AGENT_CONFIG", "")
    with pytest.raises(ValueError, match="BELLE_AGENT_CONFIG"):
        belle_slack_policy()


def test_real_belle_file_is_loaded_and_invalid_file_fails(tmp_path: Any, monkeypatch: Any) -> None:
    import pytest
    from belle_agent.config import BelleConfigError
    from belle_agent.integrations.jiuwenswarm.slack_routing import belle_slack_policy

    path = tmp_path / "belle.yaml"
    monkeypatch.setenv("BELLE_AGENT_CONFIG", str(path))
    path.write_text("platforms:\n  slack:\n    enforce_audience: false\n")
    assert belle_slack_policy()["enforce_audience"] is False

    path.write_text("platforms:\n  slack:\n    enforce_audience: 'false'\n")
    with pytest.raises(BelleConfigError):
        belle_slack_policy()


def test_channel_metadata_reads_belle_policy(monkeypatch: Any) -> None:
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect

    policy = {"enforce_audience": True}
    monkeypatch.setattr(slack_connect, "belle_slack_policy", lambda: policy)
    channel = slack_connect.SlackChannel(slack_connect.SlackChannelConfig(enabled=True), RobotMessageRouter())
    assert channel.get_metadata().extra["enforce_audience"] is True
    policy["enforce_audience"] = False
    assert channel.get_metadata().extra["enforce_audience"] is False


def test_slack_startup_checks_belle_only_for_enabled_workspaces(monkeypatch: Any) -> None:
    import pytest
    import belle_agent.integrations.jiuwenswarm.slack_routing as routing

    def invalid_policy() -> Any:
        raise ValueError("invalid Belle config")

    monkeypatch.setattr(routing, "belle_slack_policy", invalid_policy)
    routing.validate_belle_slack_startup([])
    with pytest.raises(ValueError, match="invalid Belle config"):
        routing.validate_belle_slack_startup([(0, {})])
