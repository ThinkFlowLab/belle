"""JiuwenSwarm path and connector integration for the Belle dedup store."""

import pytest
from belle_agent.integrations.jiuwenswarm.slack_dedup import get_slack_dedup_path
from belle_agent.platforms.slack.slack_dedup import SlackEventDedupStore

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)


def test_default_path_lives_beside_the_other_gateway_state():
    assert get_slack_dedup_path().parent.name == "gateway"
    assert get_slack_dedup_path().name == "slack_seen_events.json"


@pytest.mark.asyncio
async def test_connector_does_not_reprocess_a_retry_after_restart(tmp_path):
    """End to end: same event, same workspace, brand-new SlackChannel."""
    path = tmp_path / "seen.json"
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "text": "<@U-BOT> summarize this",
        "ts": "1710000000.000100",
    }
    body = {"event_id": "Ev1", "team_id": "T1"}

    def _make() -> tuple[SlackChannel, list]:
        channel = SlackChannel(
            SlackChannelConfig(enabled=True),
            RobotMessageRouter(),
            dedup_store=SlackEventDedupStore(path),
        )
        channel._running = True
        channel._identity.bot_user_id = "U-BOT"
        received: list = []
        channel.on_message(received.append)
        return channel, received

    before, received_before = _make()
    await before._handle_app_mention(event, body)
    assert len(received_before) == 1

    # Restart: new channel, new store object, same file. Slack redelivers.
    after, received_after = _make()
    await after._handle_app_mention(event, body)
    assert received_after == [], "a restart re-answered an already-handled message"
