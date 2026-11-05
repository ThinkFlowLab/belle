"""Slack workspace configuration parsing."""

import logging
import os
from collections.abc import Mapping
from typing import Any

from belle_agent.config import BelleConfigError
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack.history_policy import KEY_WORKSPACES as SLACK_WORKSPACES_KEY
from belle_agent.platforms.slack.history_policy import SLACK_WORKSPACE_KEYS, id_list
from belle_agent.platforms.slack.scope_policy import DEFAULT_COMMAND_NAME, normalise_command_name

logger = logging.getLogger(__name__)


def normalize_slack_conf(raw: Any) -> dict[str, Any]:
    """Normalize one or many Slack workspace configurations."""
    if not isinstance(raw, Mapping):
        if raw is not None:
            logger.warning(
                "channels.slack is %s rather than a mapping; no Slack"
                " workspace is configured",
                type(raw).__name__,
            )
        return {SLACK_WORKSPACES_KEY: []}

    conf = dict(raw)
    fallback_channel = str(conf.get("default_channel_id") or "").strip()
    raw_blocks = conf.get(SLACK_WORKSPACES_KEY)

    # The shipped empty list keeps the single-workspace form available.
    if not raw_blocks:
        for key, env in (("bot_token", "SLACK_BOT_TOKEN"), ("app_token", "SLACK_APP_TOKEN")):
            conf[key] = str(conf.get(key) or os.getenv(env, "")).strip()
        conf[SLACK_WORKSPACES_KEY] = [_slack_workspace_block(conf, fallback_channel)]
        return conf

    if not isinstance(raw_blocks, (list, tuple)):
        logger.warning(
            "Belle platforms.slack.workspaces is %s rather than a list; no Slack"
            " workspace is configured",
            type(raw_blocks).__name__,
        )
        conf[SLACK_WORKSPACES_KEY] = []
        return conf

    blocks: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_blocks):
        if not isinstance(entry, Mapping):
            logger.warning(
                "Belle platforms.slack.workspaces[%d] is %s rather than a mapping;"
                " skipping it",
                index,
                type(entry).__name__,
            )
            continue
        blocks.append(_slack_workspace_block(entry, fallback_channel, index=index))
    conf[SLACK_WORKSPACES_KEY] = blocks

    # Warn only when the top-level pair differs from every workspace. This
    # also avoids warnings when the normalized mapping is read again.
    top_pair = (
        str(conf.get("bot_token") or "").strip(),
        str(conf.get("app_token") or "").strip(),
    )
    if any(top_pair) and not any(
        (block["bot_token"], block["app_token"]) == top_pair for block in blocks
    ):
        logger.warning(
            "Slack settings hold both Belle platforms.slack.workspaces and a"
            " top-level bot_token/app_token pair; the list is used and the"
            " top-level pair is ignored"
        )
    return conf


def _slack_workspace_block(
    raw: Mapping[str, Any],
    fallback_channel: str,
    *,
    index: int | None = None,
) -> dict[str, Any]:
    """Read one workspace and warn about unsupported keys in list entries."""
    block: dict[str, Any] = {
        "bot_token": str(raw.get("bot_token") or "").strip(),
        "app_token": str(raw.get("app_token") or "").strip(),
        "default_channel_id": (
            str(raw.get("default_channel_id") or "").strip() or fallback_channel
        ),
    }
    # An absent enabled key lets the caller infer it from credentials.
    if "enabled" in raw:
        block["enabled"] = raw["enabled"]
    if index is not None:
        stray = sorted(set(raw) - set(SLACK_WORKSPACE_KEYS))
        if stray:
            logger.warning(
                "Belle platforms.slack.workspaces[%d] holds %s, which a workspace"
                " block cannot set: a block holds credentials only, every other"
                " channels.slack key is one connector-wide value, and"
                " per-workspace behaviour is written as a scope. The listed"
                " keys are ignored",
                index,
                ", ".join(stray),
            )
    return block


def resolve_command_name(slack_conf: Mapping[str, Any]) -> str:
    """Resolve the command name. Invalid names prevent startup."""
    raw = slack_conf.get("command_name")
    if raw is None or (not isinstance(raw, bool) and not str(raw).strip()):
        return DEFAULT_COMMAND_NAME
    name = normalise_command_name(raw)
    if not name:
        logger.error(
            "[App] Belle platforms.slack.command_name=%r is not a name Slack accepts:"
            " 1 to 32 characters, lower case letters, digits, - and _ only."
            " A word YAML reads as a boolean (on, off, yes, no, true, false)"
            " has to be quoted to reach this key as a word",
            raw,
        )
    return name


# YAML 1.1 parses bare ``off`` as false. A custom :disabled: emoji still works.
REACTION_DISABLED = "disabled"


ACK_MODE_REACTION = "reaction"
ACK_MODE_TEXT = "text"
ACK_MODE_BOTH = "both"
ACK_MODE_OFF = "off"
ACKNOWLEDGE_MODES = (ACK_MODE_REACTION, ACK_MODE_TEXT, ACK_MODE_BOTH, ACK_MODE_OFF)


# How permitted silence is delivered.
KEY_OPTIONAL_REPLY_MODE = "optional_reply_mode"
OPTIONAL_REPLY_SENTINEL = "sentinel"
OPTIONAL_REPLY_SENTINEL_UNSTORED = "sentinel_unstored"
OPTIONAL_REPLY_POST_TOOL = "post_tool"
OPTIONAL_REPLY_MODE_WORDS = (
    OPTIONAL_REPLY_SENTINEL,
    OPTIONAL_REPLY_SENTINEL_UNSTORED,
    OPTIONAL_REPLY_POST_TOOL,
)
OPTIONAL_REPLY_MODE_DEFAULT = OPTIONAL_REPLY_POST_TOOL


# Table rendering modes: disabled, marker-only, or automatic.
BLOCKKIT_TABLES_OFF = "off"
BLOCKKIT_TABLES_MARKER = "marker"
BLOCKKIT_TABLES_AUTO = "auto"
BLOCKKIT_TABLES_MODES = (
    BLOCKKIT_TABLES_OFF,
    BLOCKKIT_TABLES_MARKER,
    BLOCKKIT_TABLES_AUTO,
)
BLOCKKIT_TABLES_DEFAULT = BLOCKKIT_TABLES_AUTO


# Internal delivery modes selected per reply; config only enables previews.
STREAMING_OFF = "off"
STREAMING_EDIT = "edit"
STREAMING_STREAM = "stream"
STREAMING_MODES = (STREAMING_OFF, STREAMING_EDIT, STREAMING_STREAM)
STREAMING_MODE_DEFAULT = STREAMING_OFF
STREAMING_MODE_ENABLED = STREAMING_STREAM


def settled_word(key: str, raw: Any, words: tuple[str, ...], default: str) -> str:
    """Return a permitted value or warn and use the default."""
    word = str(raw if raw is not None else "").strip().lower()
    if word in words:
        return word
    if word:
        logger.warning(
            "Belle platforms.slack.%s=%r is not one of %s; using %r",
            key, raw, "/".join(words), default,
        )
    return default


def resolve_blockkit_tables_mode(slack_conf: Mapping[str, Any]) -> str:
    """Resolve table mode, including YAML 1.1's boolean parsing of ``off``."""
    raw = slack_conf.get("blockkit_tables")
    if raw is None:
        return BLOCKKIT_TABLES_DEFAULT
    if isinstance(raw, bool):
        return BLOCKKIT_TABLES_OFF if raw is False else BLOCKKIT_TABLES_DEFAULT

    return settled_word(
        "blockkit_tables", raw, BLOCKKIT_TABLES_MODES, BLOCKKIT_TABLES_DEFAULT
    )


def resolve_streaming_mode(value: Any) -> str:
    """Read streaming as a mode. Unknown nonempty values enable it."""
    if value is None:
        return STREAMING_MODE_DEFAULT
    if isinstance(value, bool):
        return STREAMING_MODE_ENABLED if value else STREAMING_OFF

    text = str(value).strip().lower()
    if not text:
        return STREAMING_MODE_DEFAULT
    # Spelled out for a config assembled in Python or read from an environment
    # variable, neither of which YAML has coerced on the way in.
    if text in ("false", "0", "no", "off"):
        return STREAMING_OFF
    if text in ("true", "1", "yes", "on"):
        return STREAMING_MODE_ENABLED
    logger.warning(
        "Belle platforms.slack.enable_streaming=%r is not a boolean; reading it as "
        "enabled, since that is what every value of this key other than false "
        "has ever meant",
        value,
    )
    return STREAMING_MODE_ENABLED


def resolve_streaming_enabled(value: Any) -> bool:
    """Use the same parsing for the gateway's boolean setting."""
    return resolve_streaming_mode(value) != STREAMING_OFF


KEY_APP_MESSAGES = "app_messages"
KEY_APP_MESSAGES_FROM = "app_messages_from"
APP_MESSAGES_NONE = "none"
APP_MESSAGES_LISTED = "listed"
APP_MESSAGES_ALL = "all"
APP_MESSAGES_WORDS = (APP_MESSAGES_NONE, APP_MESSAGES_LISTED, APP_MESSAGES_ALL)
APP_MESSAGES_DEFAULT = APP_MESSAGES_NONE


def normalize_app_messages(raw: Any) -> str:
    """Return a valid app-message mode, or ``none``."""
    if isinstance(raw, bool) or raw is None:
        return APP_MESSAGES_DEFAULT
    word = str(raw).strip().lower()
    return word if word in APP_MESSAGES_WORDS else APP_MESSAGES_DEFAULT


def resolve_app_messages(slack_conf: Mapping[str, Any] | None) -> str:
    """Resolve app-message admission, warning on invalid values."""
    if not isinstance(slack_conf, Mapping):
        return APP_MESSAGES_DEFAULT
    raw = slack_conf.get(KEY_APP_MESSAGES)
    if raw is None:
        return APP_MESSAGES_DEFAULT
    word = normalize_app_messages(raw)
    if isinstance(raw, bool) or word != str(raw).strip().lower():
        logger.warning(
            "channels.slack.%s=%r is not one of %s; reading no app messages"
            " (%s)",
            KEY_APP_MESSAGES,
            raw,
            "/".join(APP_MESSAGES_WORDS),
            APP_MESSAGES_DEFAULT,
        )
    return word


def resolve_app_messages_from(
    slack_conf: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Return configured app IDs even when admission is off."""
    if not isinstance(slack_conf, Mapping):
        return ()
    return id_list(slack_conf.get(KEY_APP_MESSAGES_FROM))


KEY_SESSION_SURFACE = "session_surface"
SESSION_SURFACE_AUTO = "auto"
SESSION_SURFACE_AGENTS = "agents"
SESSION_SURFACE_ASSISTANT = "assistant"
SESSION_SURFACE_OFF = "off"
SESSION_SURFACE_WORDS = (
    SESSION_SURFACE_AUTO,
    SESSION_SURFACE_AGENTS,
    SESSION_SURFACE_ASSISTANT,
    SESSION_SURFACE_OFF,
)
# Auto preserves custom queue and steer status text when selecting the API.
SESSION_SURFACE_DEFAULT = SESSION_SURFACE_AUTO


def resolve_session_surface(slack_conf: Mapping[str, Any] | None) -> str:
    """Resolve the thread-status API, including YAML 1.1 ``off``."""
    if not isinstance(slack_conf, Mapping):
        return SESSION_SURFACE_DEFAULT
    raw = slack_conf.get(KEY_SESSION_SURFACE)
    if isinstance(raw, bool):
        return SESSION_SURFACE_DEFAULT if raw else SESSION_SURFACE_OFF
    if raw is None:
        return SESSION_SURFACE_DEFAULT
    return settled_word(
        KEY_SESSION_SURFACE, raw, SESSION_SURFACE_WORDS, SESSION_SURFACE_DEFAULT
    )


KEY_REPLY_IN_THREAD = "reply_in_thread"
REPLY_IN_THREAD_NONE = "none"
REPLY_IN_THREAD_MULTI = "multi"
REPLY_IN_THREAD_ALL = "all"
REPLY_IN_THREAD_WORDS = (
    REPLY_IN_THREAD_NONE,
    REPLY_IN_THREAD_MULTI,
    REPLY_IN_THREAD_ALL,
)
REPLY_IN_THREAD_DEFAULT = REPLY_IN_THREAD_MULTI
# Unknown words never open a new thread.
REPLY_IN_THREAD_FALLBACK = REPLY_IN_THREAD_NONE


def normalize_reply_in_thread(raw: Any) -> str:
    """Normalize a thread mode, including legacy boolean values."""
    if isinstance(raw, bool):
        return REPLY_IN_THREAD_MULTI if raw else REPLY_IN_THREAD_NONE
    if raw is None:
        return REPLY_IN_THREAD_DEFAULT
    word = str(raw).strip().lower()
    if not word:
        return REPLY_IN_THREAD_DEFAULT
    return word if word in REPLY_IN_THREAD_WORDS else REPLY_IN_THREAD_FALLBACK


def resolve_reply_in_thread(slack_conf: Mapping[str, Any] | None) -> str:
    """Resolve which root messages open threads. Invalid words use ``none``."""
    if not isinstance(slack_conf, Mapping):
        return REPLY_IN_THREAD_DEFAULT
    raw = slack_conf.get(KEY_REPLY_IN_THREAD)
    word = normalize_reply_in_thread(raw)
    if not isinstance(raw, bool) and raw is not None:
        stated = str(raw).strip().lower()
        if stated and stated not in REPLY_IN_THREAD_WORDS:
            logger.warning(
                "channels.slack.%s=%r is not one of %s; opening a thread"
                " nowhere (%s)",
                KEY_REPLY_IN_THREAD,
                raw,
                "/".join(REPLY_IN_THREAD_WORDS),
                REPLY_IN_THREAD_FALLBACK,
            )
    return word


def normalize_optional_reply_mode(raw: Any) -> str:
    """Return a permitted optional-reply word, saying nothing about a typo.

    The silent half of the pair, read on every turn.
    :func:`resolve_optional_reply_mode` is the half that warns, and it runs
    once at startup, so a misspelled word is reported one time rather than on
    each message.
    """
    word = str(raw if raw is not None else "").strip().lower()
    return word if word in OPTIONAL_REPLY_MODE_WORDS else OPTIONAL_REPLY_MODE_DEFAULT


def resolve_optional_reply_mode(slack_conf: Mapping[str, Any] | None) -> str:
    """Resolve how a permitted silence is expressed. Invalid words use the default."""
    if not isinstance(slack_conf, Mapping):
        return OPTIONAL_REPLY_MODE_DEFAULT
    raw = slack_conf.get(KEY_OPTIONAL_REPLY_MODE)
    # YAML 1.1 loads ``on`` and ``yes`` as ``True``, which names no word here.
    if isinstance(raw, bool) or raw is None:
        return OPTIONAL_REPLY_MODE_DEFAULT
    return settled_word(
        KEY_OPTIONAL_REPLY_MODE,
        raw,
        OPTIONAL_REPLY_MODE_WORDS,
        OPTIONAL_REPLY_MODE_DEFAULT,
    )


def opens_a_thread(word: str, *, is_direct_message: bool) -> bool:
    """Return whether a root reply opens a thread in this conversation."""
    if word == REPLY_IN_THREAD_ALL:
        return True
    return word == REPLY_IN_THREAD_MULTI and not is_direct_message


def resolve_render_tables(slack_conf: Mapping[str, Any]) -> str:
    """Resolve table rendering and warn about invalid modes."""
    raw = slack_conf.get("render_tables")
    if isinstance(raw, bool):
        return slack_blocks.render_tables_setting(slack_conf)
    return settled_word("render_tables", raw, slack_blocks.RENDER_TABLES_MODES, slack_blocks.RENDER_TABLES_DEFAULT)


def resolve_acknowledge_mode(slack_conf: Mapping[str, Any]) -> str:
    """Resolve acknowledgement mode."""
    if "acknowledge_requests" in slack_conf:
        raise ValueError("Belle platforms.slack.acknowledge_requests was removed; use acknowledge_mode")
    raw_mode = slack_conf.get("acknowledge_mode")
    if isinstance(raw_mode, bool):
        # True names no single mode, so use the default.
        return ACK_MODE_OFF if raw_mode is False else ACK_MODE_REACTION
    mode = str(raw_mode or "").strip().lower()
    if mode:
        return settled_word("acknowledge_mode", mode, ACKNOWLEDGE_MODES, ACK_MODE_REACTION)
    return ACK_MODE_REACTION


def resolve_reaction_emoji(configured: Any, default: str) -> str:
    """Return an emoji name or empty text when the operator disables reactions."""
    value = str(configured or "").strip()
    if not value:
        return default
    if value == REACTION_DISABLED:
        return ""
    return value


# SDK logging is configured separately from application logging.
DEFAULT_SDK_LOG_LEVEL = "WARNING"
SDK_LOG_LEVELS: tuple[str, ...] = (
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
)


def resolve_seconds(slack_conf: Mapping[str, Any], key: str, default: float) -> float:
    """Resolve a number of seconds, 0 or more. An absent key keeps the default."""
    value = slack_conf.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise BelleConfigError(f"Belle config platforms.slack.{key} must be a number of seconds, 0 or more")
    return float(value)


def resolve_decision_forward(slack_conf: Mapping[str, Any]) -> bool:
    """Resolve the advisory forwarding setting."""
    raw = slack_conf.get("decision_forward", True)
    if isinstance(raw, bool):
        return raw
    logger.warning("Belle platforms.slack.decision_forward must be a boolean; using true")
    return True


def resolve_sdk_log_level(slack_conf: Mapping[str, Any]) -> str:
    """Resolve the SDK log level, warning on invalid values."""
    raw = str(slack_conf.get("sdk_log_level") or "").strip().upper()
    if not raw:
        return DEFAULT_SDK_LOG_LEVEL
    if raw in SDK_LOG_LEVELS:
        return raw
    logger.warning(
        "Belle platforms.slack.sdk_log_level=%r is not one of %s; using %s",
        raw,
        "/".join(SDK_LOG_LEVELS),
        DEFAULT_SDK_LOG_LEVEL,
    )
    return DEFAULT_SDK_LOG_LEVEL
