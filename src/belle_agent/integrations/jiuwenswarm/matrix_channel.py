"""Serve Matrix conversations through JiuwenSwarm's AgentServer."""

import hashlib
from collections.abc import Mapping
from typing import Any

from belle_agent.config import load_config
from belle_agent.host import host_from_config
from belle_agent.integrations.jiuwenswarm.channel_turns import JiuwenSwarmTurns
from belle_agent.integrations.jiuwenswarm.config import configured_models
from belle_agent.integrations.jiuwenswarm.conversation_channel import ConversationChannel, channel_config
from belle_agent.paths import resolve_paths, selected_config_file
from belle_agent.platforms.matrix.capabilities import NAME
from belle_agent.platforms.matrix.toolkit import METADATA_ROOM, METADATA_USER


def matrix_session_id(conversation: str) -> str:
    """Hash Matrix conversation identifiers into stable JiuwenSwarm session IDs."""
    return f"matrix_{hashlib.sha256(conversation.encode()).hexdigest()}"


def matrix_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Keep Matrix settings and disable the channel host's Second Brain."""
    return channel_config(config, NAME)


class MatrixChannel(ConversationChannel):
    """Serve Belle's Matrix host through the Gateway."""

    name = NAME

    def __init__(self, config: Mapping[str, Any], router: Any) -> None:
        super().__init__(router)
        self.turns = JiuwenSwarmTurns(
            self._route, channel=NAME, chat_key=METADATA_ROOM, user_key=METADATA_USER,
            models=configured_models, session_id=matrix_session_id,
        )
        self.host = host_from_config(config, self.turns, state_dir=resolve_paths().state_dir)


def create_matrix_channels(host_config: dict[str, Any], router: Any) -> list[MatrixChannel]:
    """Create one Gateway channel from Belle's Matrix settings."""
    return [MatrixChannel(matrix_config(load_config(selected_config_file())), router)]
