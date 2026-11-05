"""Host configuration for standalone Slack history."""

from belle_agent.platforms.slack.history import SlackHistoryToolkit, SlackWorkspaceClients


async def test_workspace_settings_follow_injected_config() -> None:
    config = {"bot_token": "first"}
    clients = SlackWorkspaceClients(config_loader=lambda: config)

    assert (await clients.settings_for(None))["bot_token"] == "first"
    config["bot_token"] = "second"
    assert (await clients.settings_for(None))["bot_token"] == "second"


async def test_history_limits_preserve_explicit_budgets_and_allow_zero_user_lookups() -> None:
    config = {"history_max_messages": 40, "history_max_user_lookups": 10}
    toolkit = SlackHistoryToolkit(
        client=object(),
        workspaces=SlackWorkspaceClients(config_loader=lambda: config),
        max_messages=-1,
        max_roots_scanned=100_000,
        max_api_calls=0,
        max_message_chars=0,
        max_total_chars=0,
        max_user_lookups=0,
    )
    await toolkit._load_settings()

    assert toolkit._max_messages == 1
    assert toolkit._max_roots_scanned == 100_000
    assert toolkit._max_api_calls == 1
    assert toolkit._max_message_chars == 1
    assert toolkit._max_total_chars == 1
    assert toolkit._max_user_lookups == 0
