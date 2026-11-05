"""Build JiuwenSwarm Slack workspaces from channel settings."""

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from belle_agent.attachments import MAX_FILE_BYTES
from belle_agent.integrations.jiuwenswarm.config import (
    describe_post_tool_reachability,
    load_belle_config,
    load_brain_ingest_channels,
    load_slack_scopes,
)
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    describe_slack_delivery_reachability,
)
from belle_agent.integrations.jiuwenswarm.slack_routing import validate_belle_slack_startup
from belle_agent.integrations.jiuwenswarm.slack_tool_config import slack_tool_config
from belle_agent.platforms.slack.channel_config import DEFAULT_THINKING_STATUS, SlackChannelConfig
from belle_agent.platforms.slack.config import (
    SLACK_WORKSPACES_KEY,
    normalize_slack_conf,
    resolve_acknowledge_mode,
    resolve_app_messages,
    resolve_app_messages_from,
    resolve_blockkit_tables_mode,
    resolve_command_name,
    resolve_decision_forward,
    resolve_optional_reply_mode,
    resolve_reaction_emoji,
    resolve_render_tables,
    resolve_reply_in_thread,
    resolve_sdk_log_level,
    resolve_seconds,
    resolve_session_surface,
    resolve_streaming_enabled,
)
from belle_agent.platforms.slack.scoped_config import (
    apply_scopes_to_slack_overrides,
    describe_configured_channels,
    resolve_group_chat_mode,
)

logger = logging.getLogger(__name__)


def configure_slack_workspaces(
    config: dict[str, Any],
    *,
    is_enabled: Callable[[dict[str, Any]], tuple[bool, str]],
) -> tuple[SlackChannelConfig | None, list[tuple[int, SlackChannelConfig]]]:
    """Return shared settings and enabled workspace settings."""
    all_workspaces = list(config.get(SLACK_WORKSPACES_KEY) or [])
    workspaces = []
    if config.get("enabled") is not None and not config["enabled"]:
        logger.info("[App] channels.slack.enabled = false, SlackChannel disabled")
    else:
        for index, workspace in enumerate(all_workspaces):
            enabled, reason = is_enabled(workspace)
            if enabled:
                workspaces.append((index, workspace))
            else:
                log = logger.warning if workspace.get("enabled") is True else logger.info
                log("[App] Belle platforms.slack.workspaces[%d]: %s; skipping", index, reason)

    validate_belle_slack_startup(workspaces)
    if not workspaces:
        return None, []

    scopes = load_slack_scopes()
    brain_all, brain_public, brain_channels = load_brain_ingest_channels()
    platform_override, conversation_overrides = apply_scopes_to_slack_overrides(
        config, scopes=scopes
    )

    shared = SlackChannelConfig(
        enabled=True,
        file_download_max_bytes=config.get("file_download_max_bytes", MAX_FILE_BYTES),
        bot_token="",
        app_token="",
        command_name=resolve_command_name(config),
        allow_from=config.get("allow_from") or [],
        allowed_channel_ids=config.get("allowed_channel_ids") or [],
        app_messages=resolve_app_messages(config),
        app_messages_from=resolve_app_messages_from(config),
        conversation_overrides=conversation_overrides,
        platform_override=platform_override,
        scopes=scopes,
        default_channel_id="",
        reply_in_thread=resolve_reply_in_thread(config),
        session=str(config.get("session") or "thread"),
        optional_reply_mode=resolve_optional_reply_mode(config),
        group_chat_mode=resolve_group_chat_mode(config.get("group_chat_mode")) or "mention",
        acknowledge_mode=resolve_acknowledge_mode(config),
        acknowledgement_text=str(config.get("acknowledgement_text") or "Received. Analyzing…").strip(),
        thinking_status=str(config.get("thinking_status", DEFAULT_THINKING_STATUS) or "").strip(),
        queued_status=str(config.get("queued_status") or "has queued a message").strip(),
        steered_status=str(config.get("steered_status") or "has steered a message into the running turn").strip(),
        session_surface=resolve_session_surface(config),
        acknowledgement_emoji=resolve_reaction_emoji(config.get("acknowledgement_emoji"), "eyes"),
        rejected_emoji=resolve_reaction_emoji(config.get("rejected_emoji"), "no_entry_sign"),
        queued_emoji=resolve_reaction_emoji(config.get("queued_emoji"), "hourglass_flowing_sand"),
        completed_emoji=resolve_reaction_emoji(config.get("completed_emoji"), ""),
        failed_emoji=resolve_reaction_emoji(config.get("failed_emoji"), "x"),
        stopped_emoji=resolve_reaction_emoji(config.get("stopped_emoji"), "black_square_for_stop"),
        decision_questions_file=config.get("decision_questions_file", ""),
        decision_forward=resolve_decision_forward(config),
        brain_ingest_channels=brain_channels,
        brain_ingest_all_channels=brain_all,
        brain_ingest_public_channels=brain_public,
        brain_react_pending=resolve_reaction_emoji(config.get("brain_react_pending"), "open_book"),
        brain_react_success=resolve_reaction_emoji(config.get("brain_react_success"), "brain"),
        brain_react_declined=resolve_reaction_emoji(config.get("brain_react_declined"), "x"),
        brain_react_failure=resolve_reaction_emoji(config.get("brain_react_failure"), "warning"),
        sdk_log_level=resolve_sdk_log_level(config),
        enable_streaming=resolve_streaming_enabled(config.get("enable_streaming")),
        blockkit_tables=resolve_blockkit_tables_mode(config),
        render_tables=resolve_render_tables(config),
        group_digital_avatar=bool(config.get("group_digital_avatar", False)),
        my_user_id=str(config.get("my_user_id") or "").strip(),
        principal_name=str(config.get("principal_name") or "").strip(),
        bot_name=str(config.get("bot_name") or "").strip(),
        enable_memory=bool(config.get("enable_memory", False)),
        activity_card=bool(config.get("activity_card", True)),
        activity_card_delay_seconds=resolve_seconds(config, "activity_card_delay_seconds", 5.0),
        activity_card_min_edit_seconds=resolve_seconds(config, "activity_card_min_edit_seconds", 10.0),
        home_tab_welcome=bool(config.get("home_tab_welcome", False)),
    )
    describe_configured_channels(shared)
    describe_post_tool_reachability(shared)

    resolved = []
    for index, workspace in workspaces:
        settings = replace(
            shared,
            app_id="" if len(all_workspaces) == 1 else f"workspace-{index + 1}",
            bot_token=str(workspace.get("bot_token") or "").strip(),
            app_token=str(workspace.get("app_token") or "").strip(),
            default_channel_id=str(workspace.get("default_channel_id") or "").strip(),
        )
        resolved.append((index, settings))
    return shared, resolved


async def build_slack_workspaces(
    config: dict[str, Any],
    *,
    is_enabled: Callable[[dict[str, Any]], tuple[bool, str]],
    cron_store: Any,
    heartbeat_target: str,
) -> tuple[SlackChannelConfig | None, list[tuple[int, SlackChannelConfig]]]:
    shared, workspaces = configure_slack_workspaces(config, is_enabled=is_enabled)
    for _, settings in workspaces:
        await describe_slack_delivery_reachability(
            settings, cron_store=cron_store, heartbeat_target=heartbeat_target
        )
    return shared, workspaces


def create_slack_channels(host_config: dict[str, Any], router: Any) -> list[Any]:
    """Build Belle's Slack channel instances for the JiuwenSwarm extension."""
    from belle_agent.integrations.jiuwenswarm.slack_im_adapter import SlackIMPlatformAdapter

    config = normalize_slack_conf(
        slack_tool_config({"channels": {"slack": host_config}}, load_belle_config())
    )

    def is_enabled(block: dict[str, Any]) -> tuple[bool, str]:
        enabled = block.get("enabled")
        if enabled is False:
            return False, "enabled = false"
        if not all(str(block.get(key) or "").strip() for key in ("bot_token", "app_token")):
            return False, "missing bot_token or app_token"
        return True, ""

    shared, workspaces = configure_slack_workspaces(config, is_enabled=is_enabled)
    if shared is None:
        return []
    if shared.group_digital_avatar and len(workspaces) > 1:
        raise ValueError("Belle platforms.slack.group_digital_avatar supports one workspace")
    adapter = None
    if shared.group_digital_avatar:
        if shared.my_user_id:
            adapter = SlackIMPlatformAdapter(
                my_user_id=shared.my_user_id,
                principal_name=shared.principal_name,
                bot_name=shared.bot_name,
            )
        else:
            logger.warning("Belle platforms.slack.group_digital_avatar needs my_user_id")
    channels = [
        SlackChannel(settings, router, im_platform_adapter=adapter)
        for _, settings in workspaces
    ]
    return channels
