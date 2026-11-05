"""Combine Belle settings with JiuwenSwarm channel settings."""

import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.config import BelleConfigError, load_config
from belle_agent.integrations.jiuwenswarm.slack_tool_config import slack_tool_config
from belle_agent.integrations.openjiuwen.scopes.permissions import settled_tool_levels
from belle_agent.models import ConfiguredModels
from belle_agent.paths import selected_config_file
from belle_agent.platforms.slack.config import (
    KEY_OPTIONAL_REPLY_MODE,
    OPTIONAL_REPLY_POST_TOOL,
    OPTIONAL_REPLY_SENTINEL,
    SLACK_WORKSPACES_KEY,
    normalize_optional_reply_mode,
    normalize_reply_in_thread,
)
from belle_agent.platforms.slack.post import POST_MESSAGE
from belle_agent.platforms.slack.scoped_config import SlackScopeConfig, settled_override
from belle_agent.scopes import (
    LEVEL_ALLOW,
    REPLY_OPTIONAL,
    SECTION_AGENT,
    SECTION_DELIVERY,
    SECTION_PERMISSIONS,
    Scope,
    compile_scopes,
    scoped_chats,
)
from belle_agent.second_brain.paths import ALL_CHANNELS, PUBLIC_CHANNELS, parse_ingest_channels, second_brain_config

logger = logging.getLogger(__name__)


def load_belle_config() -> dict[str, Any]:
    """Read the selected Belle file, or use defaults when none is configured."""
    return load_config(selected_config_file())


def check_belle_config() -> None:
    """Refuse a Belle setting that Belle does not apply on JiuwenSwarm.

    Each Belle extension calls this first when JiuwenSwarm loads it, so every JiuwenSwarm process refuses at start.
    """
    from belle_agent.host.attached_sessions import attached_session_settings

    if attached_session_settings(load_belle_config()).expire_after_idle_hours is not None:
        raise BelleConfigError(
            "Belle config attached_sessions.expire_after_idle_hours must be disabled on JiuwenSwarm: "
            "Belle does not yet delete the session of a JiuwenSwarm chat place"
        )


def configured_models() -> ConfiguredModels:
    """Read resolvable model names and aliases.

    Return no known model names on read failure so callers leave overrides unchanged.
    """
    try:
        from jiuwenswarm.extensions.sdk.host_config import get_default_models

        names: list[str] = []
        aliases: list[str] = []
        alias_pairs: list[tuple[str, str]] = []
        for entry in get_default_models():
            if not isinstance(entry, Mapping):
                continue
            mcc = entry.get("model_client_config")
            mcc = mcc if isinstance(mcc, Mapping) else {}
            name = str(mcc.get("model_name") or "").strip()
            if not name:
                # The adapter skips a nameless entry outright, so there is
                # nothing here a request could ask for.
                continue
            if name not in names:
                names.append(name)
            alias = str(entry.get("alias") or "").strip()
            if alias and alias != name and alias not in aliases:
                aliases.append(alias)
                alias_pairs.append((alias, name))
        return ConfiguredModels(tuple(names), tuple(aliases), tuple(alias_pairs))
    except Exception:
        logger.warning(
            "channels.slack could not read models.defaults to check the model"
            " names written in scopes; leaving them unchecked",
            exc_info=True,
        )
        return ConfiguredModels()


def load_slack_scopes() -> tuple[Scope, ...]:
    """Compile Belle scopes with JiuwenSwarm's channel settings."""
    try:
        from jiuwenswarm.extensions.sdk.host_config import get_config

        data = get_config()
    except Exception:
        logger.warning(
            "channels.slack could not read Belle scopes; no scope"
            " applies and every channel follows channels.slack alone",
            exc_info=True,
        )
        return ()
    if not isinstance(data, Mapping):
        return ()
    belle = load_belle_config()
    channels = data.get("channels")
    channels = dict(channels) if isinstance(channels, Mapping) else {}
    channels["slack"] = slack_tool_config(data, belle)
    from belle_agent.integrations.jiuwenswarm.scopes import register_host_channels  # It imports this module.

    register_host_channels()
    return compile_scopes(
        belle.get("scopes"),
        channels_config=channels,
        people=belle.get("people"),
        roles=belle.get("roles"),
    )


#: The wildcard entry in ``second_brain.ingest.channels``.
BRAIN_ALL_CHANNELS = ALL_CHANNELS

#: The entry in ``second_brain.ingest.channels`` for every public channel.
#: Not a Slack id -- those are upper case -- so it cannot shadow one.
BRAIN_PUBLIC_CHANNELS = PUBLIC_CHANNELS

def load_brain_ingest_channels() -> tuple[bool, bool, frozenset[str]]:
    """Return ``(all, public, ids)`` from ``second_brain.ingest.channels``."""
    try:
        section = second_brain_config(load_belle_config())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "second_brain.ingest.channels could not be read (%s); ingesting"
            " from no conversation",
            exc,
        )
        return False, False, frozenset()
    if not section["enabled"]:
        return False, False, frozenset()
    ingest = section.get("ingest")
    raw = ingest.get("channels") if isinstance(ingest, Mapping) else None
    return parse_ingest_channels(raw, warn=logger.warning)



def slack_default_channel_id_from_config(
    config: Mapping[str, Any] | None = None,
) -> str:
    """Find a fallback destination before ``SlackChannelConfig`` exists.

    Return the top-level fallback first, then the first workspace fallback.
    This answers whether a destination exists. The channel instance selects the workspace.
    Return an empty string after a read failure so callers can warn.
    """
    mapping: Any = config
    if mapping is None:
        try:
            from jiuwenswarm.extensions.sdk.host_config import get_config

            mapping = get_config() or {}
        except Exception as exc:  # noqa: BLE001 - a warning may not break a start.
            logger.warning(
                "channels.slack.default_channel_id could not be read, treating"
                " it as unset: %s",
                exc,
            )
            return ""
    slack = slack_tool_config(mapping, load_belle_config())
    raw = slack.get("default_channel_id") if isinstance(slack, Mapping) else None
    fallback = str(raw or "").strip()
    if fallback:
        return fallback
    blocks = slack.get(SLACK_WORKSPACES_KEY) if isinstance(slack, Mapping) else None
    for block in blocks if isinstance(blocks, (list, tuple)) else ():
        if not isinstance(block, Mapping):
            continue
        candidate = str(block.get("default_channel_id") or "").strip()
        if candidate:
            return candidate
    return ""


def slack_agent_request_params(
    config: SlackScopeConfig,
    channel_id: str,
    *,
    user_id: str = "",
    chat_type: str = "",
    workspace: str = "",
) -> dict[str, str | list[str]]:
    """Put the settled expert and scoped capabilities on this request."""
    override = settled_override(
        config, channel_id, user_id=user_id, chat_type=chat_type, workspace=workspace
    )
    # An omitted name keeps the runtime's current template. Slack settles the
    # scope on each turn, so send an explicit clear when no rule selects one.
    params: dict[str, str | list[str]] = {
        "agent_template_name": override.expert or "",
    }
    if override.subagents is not None:
        params["agent_subagents_available"] = sorted(override.subagents)
    if override.skills is not None:
        for key, param in (
            ("available", "agent_skills_available"),
            ("unavailable", "agent_skills_unavailable"),
            ("required", "agent_skills_required"),
        ):
            if key in override.skills:
                params[param] = list(override.skills[key])
    return params


def describe_post_tool_reachability(config: Any) -> tuple[str, ...]:
    """Report conversations where post-tool delivery cannot answer."""
    if normalize_optional_reply_mode(config.optional_reply_mode) != OPTIONAL_REPLY_POST_TOOL:
        return ()

    warnings = [
        (
            f"Belle platforms.slack.{KEY_OPTIONAL_REPLY_MODE}"
            f"={OPTIONAL_REPLY_POST_TOOL} posts a reply through {POST_MESSAGE},"
            " which sends a message where the call names it and refuses a call"
            " that names nowhere. Each turn in a conversation that permits"
            " silence therefore settles whether its answer sits in a thread or"
            " at that conversation's top level, and each turn is given the ids"
            " both arguments take. channels.slack.reply_in_thread"
            f" ({normalize_reply_in_thread(config.reply_in_thread)}) stops"
            f" deciding that. Use {OPTIONAL_REPLY_SENTINEL} wherever"
            " reply_in_thread must decide it"
        )
    ]
    if config.enable_streaming:
        warnings.append(
            f"Belle platforms.slack.{KEY_OPTIONAL_REPLY_MODE}={OPTIONAL_REPLY_POST_TOOL}"
            " and Belle platforms.slack.enable_streaming=true do not combine: a reply"
            " the connector withholds is streamed into the conversation first"
            " and deleted afterwards, so the text is on screen for as long as"
            f" the turn runs. Set enable_streaming to false, or use"
            f" {OPTIONAL_REPLY_SENTINEL}"
        )

    named = scoped_chats(
        config.scopes,
        channel="slack",
        sections=(SECTION_DELIVERY, SECTION_AGENT, SECTION_PERMISSIONS),
    )
    for chat in ("", *named):
        if settled_override(config, chat).reply != REPLY_OPTIONAL:
            continue
        level = settled_tool_levels(
            config.scopes, channel="slack", chat=chat or None
        ).get(POST_MESSAGE, LEVEL_ALLOW)
        if level == LEVEL_ALLOW:
            continue
        where = chat or "every chat no scope names"
        warnings.append(
            f"Belle platforms.slack.{KEY_OPTIONAL_REPLY_MODE}={OPTIONAL_REPLY_POST_TOOL}"
            f" withholds this connector's own reply in {where}, where"
            f" delivery.reply is {REPLY_OPTIONAL}, and a scope sets"
            f" permissions.tools.{POST_MESSAGE}={level} there. A turn in that"
            " conversation has no way to answer. That silence looks the same as"
            " one it chose"
        )

    for message in warnings:
        logger.warning(message)
    return tuple(warnings)
