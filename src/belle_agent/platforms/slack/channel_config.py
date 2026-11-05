"""Slack connector configuration values shared across runtime adapters."""

from dataclasses import dataclass, field

from belle_agent.attachments import MAX_FILE_BYTES
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack.config import (
    ACK_MODE_REACTION,
    APP_MESSAGES_DEFAULT,
    BLOCKKIT_TABLES_DEFAULT,
    DEFAULT_SDK_LOG_LEVEL,
    OPTIONAL_REPLY_MODE_DEFAULT,
    REPLY_IN_THREAD_DEFAULT,
    SESSION_SURFACE_DEFAULT,
)
from belle_agent.platforms.slack.scope_policy import DEFAULT_COMMAND_NAME
from belle_agent.platforms.slack.scoped_config import GROUP_MODE_MENTION, SlackChannelOverride
from belle_agent.platforms.slack.thread_status import _DEFAULT_QUEUED_STATUS, _DEFAULT_STEERED_STATUS
from belle_agent.scopes import Scope

DEFAULT_THINKING_STATUS = "is thinking…"


@dataclass
class SlackChannelConfig:
    """Belle Slack settings."""

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    file_download_max_bytes: int = MAX_FILE_BYTES
    allow_from: list[str] = field(default_factory=list)
    allowed_channel_ids: list[str] = field(default_factory=list)
    default_channel_id: str = ""
    session: str = "thread"

    # Configured identity, available before auth.test learns the team ID.
    app_id: str = ""
    # Must match the Slack app manifest.
    command_name: str = DEFAULT_COMMAND_NAME
    app_messages: str = APP_MESSAGES_DEFAULT
    # Use app IDs because bot IDs can change across installations.
    app_messages_from: tuple[str, ...] = ()
    conversation_overrides: dict[str, SlackChannelOverride] = field(
        default_factory=dict
    )
    platform_override: SlackChannelOverride = field(
        default_factory=SlackChannelOverride
    )
    # The connector settles sender-specific rules when a message arrives.
    scopes: tuple[Scope, ...] = ()
    # normalize_reply_in_thread also accepts legacy booleans.
    reply_in_thread: str = REPLY_IN_THREAD_DEFAULT
    # How a conversation whose delivery.reply is optional expresses silence.
    optional_reply_mode: str = OPTIONAL_REPLY_MODE_DEFAULT
    group_chat_mode: str = GROUP_MODE_MENTION
    acknowledge_mode: str = ACK_MODE_REACTION
    acknowledgement_text: str = 'Received. Analyzing…'
    thinking_status: str = DEFAULT_THINKING_STATUS
    queued_status: str = _DEFAULT_QUEUED_STATUS
    steered_status: str = _DEFAULT_STEERED_STATUS
    # The status API is workspace-wide.
    session_surface: str = SESSION_SURFACE_DEFAULT
    acknowledgement_emoji: str = 'eyes'
    rejected_emoji: str = 'no_entry_sign'
    queued_emoji: str = 'hourglass_flowing_sand'
    completed_emoji: str = ''
    failed_emoji: str = 'x'
    stopped_emoji: str = 'black_square_for_stop'
    # Optional JSON questions, loaded at startup.
    decision_questions_file: str = ""
    # Forward raw answers only when a decision model is configured.
    decision_forward: bool = True

    # Ingest sources and reactions. Corpus tools remain available elsewhere.
    brain_ingest_channels: frozenset[str] = frozenset()
    brain_ingest_all_channels: bool = False
    # Every public channel, on top of ``brain_ingest_channels``.
    brain_ingest_public_channels: bool = False
    brain_react_pending: str = 'open_book'
    brain_react_success: str = 'brain'
    brain_react_declined: str = 'x'
    brain_react_failure: str = 'warning'
    sdk_log_level: str = DEFAULT_SDK_LOG_LEVEL
    enable_streaming: bool = False
    blockkit_tables: str = BLOCKKIT_TABLES_DEFAULT
    render_tables: str = slack_blocks.RENDER_TABLES_DEFAULT
    # Most useful with group_chat_mode: all.
    group_digital_avatar: bool = False
    my_user_id: str = ""
    principal_name: str = ""
    bot_name: str = ""
    enable_memory: bool = False
    activity_card: bool = True
    activity_card_delay_seconds: float = 5.0
    activity_card_min_edit_seconds: float = 10.0
    # Publishes only on an empty Home tab.
    home_tab_welcome: bool = False
