"""Serve Mattermost conversations through JiuwenSwarm's AgentServer."""

from collections.abc import Mapping
from typing import Any

from belle_agent.config import load_config
from belle_agent.host import host_from_config
from belle_agent.integrations.jiuwenswarm.channel_turns import JiuwenSwarmTurns
from belle_agent.integrations.jiuwenswarm.config import configured_models
from belle_agent.integrations.jiuwenswarm.conversation_channel import ConversationChannel, channel_config
from belle_agent.paths import resolve_paths, selected_config_file
from belle_agent.platforms.mattermost.capabilities import NAME
from belle_agent.platforms.mattermost.toolkit import METADATA_CHANNEL, METADATA_USER


def mattermost_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Keep Mattermost settings and disable the channel host's Second Brain."""
    return channel_config(config, NAME)


class MattermostChannel(ConversationChannel):
    """Serve Belle's Mattermost host through the Gateway."""

    name = NAME

    def __init__(self, config: Mapping[str, Any], router: Any) -> None:
        super().__init__(router)
        self.turns = JiuwenSwarmTurns(
            self._route, channel=NAME, chat_key=METADATA_CHANNEL, user_key=METADATA_USER, models=configured_models
        )
        self.host = host_from_config(config, self.turns, state_dir=resolve_paths().state_dir)


def create_mattermost_channels(host_config: dict[str, Any], router: Any) -> list[MattermostChannel]:
    """Create one Gateway channel from Belle's Mattermost settings."""
    return [MattermostChannel(mattermost_config(load_config(selected_config_file())), router)]
