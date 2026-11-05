"""Read the ``platforms.discord`` section of a Belle configuration.

The bot token is read from the environment and from nowhere else. It is never accepted in the
configuration file and never logged.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from belle_agent.attachments import MAX_FILE_BYTES
from belle_agent.config import BelleConfigError
from belle_agent.platforms.discord.ids import SNOWFLAKE_FORM, SNOWFLAKE_RE

SECTION = "discord"
#: Where the section is in the configuration, as messages name its keys.
KEY_PATH = f"platforms.{SECTION}"
#: The variable that holds the bot's token.
TOKEN_ENV = "DISCORD_BOT_TOKEN"
#: The privileged gateway intents, each with its default. Each needs its toggle in the developer portal.
PRIVILEGED: Mapping[str, bool] = {"message_content": True, "guild_members": False}
#: The other switches, each with its default. ``reply_in_thread`` false answers a channel message beside it.
SWITCHES: Mapping[str, bool] = {
    "reply_in_thread": True, "typing_indicator": True, "enable_streaming": False, "activity_card": True,
    "allow_role_mentions": False, "allow_everyone_mentions": False, "allow_tts": False,
}
#: How a running turn's activity card is timed, each in seconds with its default: how long a turn runs before
#: its card is posted, and the least time between two edits of it.
CARD_SECONDS: Mapping[str, float] = {"activity_card_delay_seconds": 5.0, "activity_card_min_edit_seconds": 10.0}
#: Operator resource limits for queued events, pending questions and question lifetime in seconds.
LIMITS: Mapping[str, int] = {
    "pending_events_max": 1024,
    "pending_questions_max": 256,
    "question_expiry_seconds": 86400,
    "file_download_max_bytes": MAX_FILE_BYTES,
}
#: The reaction each mark key adds, by default: a unicode emoji, or ``name:id`` for a custom one.
#: ``disabled`` adds none.
MARK_EMOJI: Mapping[str, str] = {
    "acknowledgement_emoji": "\N{EYES}",
    "rejected_emoji": "\N{NO ENTRY SIGN}",
    "queued_emoji": "\N{HOURGLASS WITH FLOWING SAND}",
    "completed_emoji": "disabled",
    "failed_emoji": "\N{CROSS MARK}",
    "stopped_emoji": "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}",
    "brain_react_pending": "\N{OPEN BOOK}",
    "brain_react_success": "\N{BRAIN}",
    "brain_react_declined": "\N{CROSS MARK}",
    "brain_react_failure": "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}",
}
NO_EMOJI = "disabled"
_CUSTOM_EMOJI_NAME = re.compile(r"[A-Za-z0-9_]{2,}\Z")
#: The host reads ``allow_from``, ``allowed_channel_ids`` and the decision gate's keys from the section.
_HOST_KEYS = frozenset({"allow_from", "allowed_channel_ids", "decision_questions_file", "decision_forward"})
_KEYS = frozenset({"enabled", "member_event_channels", *PRIVILEGED, *SWITCHES, *CARD_SECONDS, *LIMITS, *MARK_EMOJI, *_HOST_KEYS})
_SECRETS = frozenset({"token", "bot_token", "access_token"})


@dataclass(frozen=True)
class DiscordSettings:
    """The bot's token and what the gateway connection asks for."""

    token: str = field(repr=False)
    bot: str = ""
    #: Ambient channels: without it, a guild message carries text only where it mentions the bot.
    message_content: bool = True
    #: Member events and the members of a channel, as audience checks read them.
    guild_members: bool = False
    #: Optional per-guild channels for member events when a system channel is unusable.
    member_event_channels: Mapping[str, str] = field(default_factory=dict)
    #: A channel message is answered in a thread started on it; false answers it in the channel.
    reply_in_thread: bool = True
    typing_indicator: bool = True
    #: A reply shown as it is written, in a message Belle edits; and the card of the tools a turn runs.
    enable_streaming: bool = False
    activity_card: bool = True
    #: These privileged sends require both operator consent and Discord permissions at the destination.
    allow_role_mentions: bool = False
    allow_everyone_mentions: bool = False
    allow_tts: bool = False
    activity_card_delay_seconds: float = 5.0
    activity_card_min_edit_seconds: float = 10.0
    pending_events_max: int = LIMITS["pending_events_max"]
    pending_questions_max: int = LIMITS["pending_questions_max"]
    question_expiry_seconds: int = LIMITS["question_expiry_seconds"]
    file_download_max_bytes: int = LIMITS["file_download_max_bytes"]
    #: The reaction of each mark key, or empty where the key adds none.
    emoji: Mapping[str, str] = field(default_factory=lambda: {key: _emoji({}, key) for key in MARK_EMOJI})


def read_settings(section: Mapping[str, Any], environ: Mapping[str, str] = os.environ) -> DiscordSettings:
    """Read the section and the environment. Refuse what does not fit, naming the key and its form."""
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config {KEY_PATH} must be a mapping")
    secrets = sorted(_SECRETS & set(section))
    if secrets:
        raise BelleConfigError(
            f"Belle config {KEY_PATH}.{secrets[0]} is refused: the bot token is read from {TOKEN_ENV} only"
        )
    unknown = sorted(str(key) for key in section if key not in _KEYS)
    if unknown:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{unknown[0]} is not a setting. The settings are {', '.join(sorted(_KEYS))}")
    token = environ.get(TOKEN_ENV, "")
    if not token:
        raise BelleConfigError(f"Belle config {KEY_PATH} needs the bot token in {TOKEN_ENV}")
    if token != token.strip():
        raise BelleConfigError(f"Belle config {KEY_PATH} token in {TOKEN_ENV} must have no surrounding whitespace")
    switches = {}
    for key, default in {**PRIVILEGED, **SWITCHES}.items():
        value = section.get(key)
        if value is not None and not isinstance(value, bool):
            raise BelleConfigError(f"Belle config {KEY_PATH}.{key} must be true or false")
        switches[key] = default if value is None else value
    seconds = {}
    for key, fallback in CARD_SECONDS.items():
        value = section.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise BelleConfigError(f"Belle config {KEY_PATH}.{key} must be a number of seconds, 0 or more")
        seconds[key] = fallback if value is None else float(value)
    limits = {}
    for key, fallback in LIMITS.items():
        value = section.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise BelleConfigError(f"Belle config {KEY_PATH}.{key} must be a whole number, 1 or more")
        limits[key] = fallback if value is None else value
    emoji = {key: _emoji(section, key) for key in MARK_EMOJI}
    member_event_channels = section.get("member_event_channels", {})
    if not isinstance(member_event_channels, Mapping):
        raise BelleConfigError(f"Belle config {KEY_PATH}.member_event_channels must map Discord guild IDs to channel IDs")
    for guild_id, channel_id in member_event_channels.items():
        if not isinstance(guild_id, str) or not SNOWFLAKE_RE.fullmatch(guild_id):
            raise BelleConfigError(f"Belle config {KEY_PATH}.member_event_channels key must be a Discord guild ID ({SNOWFLAKE_FORM})")
        if not isinstance(channel_id, str) or not SNOWFLAKE_RE.fullmatch(channel_id):
            raise BelleConfigError(f"Belle config {KEY_PATH}.member_event_channels[{guild_id}] must be a Discord channel ID ({SNOWFLAKE_FORM})")
    return DiscordSettings(
        token=token, bot="", emoji=emoji, member_event_channels=dict(member_event_channels), **switches,
        activity_card_delay_seconds=seconds["activity_card_delay_seconds"],
        activity_card_min_edit_seconds=seconds["activity_card_min_edit_seconds"],
        pending_events_max=limits["pending_events_max"],
        pending_questions_max=limits["pending_questions_max"],
        question_expiry_seconds=limits["question_expiry_seconds"],
        file_download_max_bytes=limits["file_download_max_bytes"],
    )


def _emoji(section: Mapping[str, Any], key: str) -> str:
    value = section.get(key)
    if value is None:
        value = MARK_EMOJI[key]
    if not isinstance(value, str) or not value or len(value) > 64 or any(char.isspace() for char in value):
        raise BelleConfigError(
            f"Belle config {KEY_PATH}.{key} must be a nonempty bare Unicode value without spaces (64 characters max), "
            f"a custom emoji as name:id, or {NO_EMOJI}; Discord checks bare values when reacting"
        )
    name, separator, emoji_id = value.partition(":")
    if separator and (not _CUSTOM_EMOJI_NAME.fullmatch(name) or not SNOWFLAKE_RE.fullmatch(emoji_id)):
        raise BelleConfigError(
            f"Belle config {KEY_PATH}.{key} custom emoji must use name:ID; name has at least 2 letters, digits or underscores and ID has {SNOWFLAKE_FORM}"
        )
    return "" if value == NO_EMOJI else value


KEY_BOTS = "bots"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_ENV = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


def bot_entries(section: Mapping[str, Any]) -> list[Mapping[str, str]]:
    """Validate names and environment references before resolving credentials."""
    entries = section.get(KEY_BOTS)
    if not isinstance(entries, list) or not entries:
        raise BelleConfigError(f"Belle config {KEY_PATH}.bots must be a nonempty list")
    names: set[str] = set()
    refs: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{KEY_PATH}.bots[{index}]"
        if not isinstance(entry, Mapping) or set(entry) != {"name", "token_env"}:
            raise BelleConfigError(f"Belle config {where} must contain only name and token_env; tokens belong in the environment")
        name, ref = entry["name"], entry["token_env"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise BelleConfigError(f"Belle config {where}.name must contain letters, digits, underscores or hyphens, starting with a letter or digit")
        if not isinstance(ref, str) or not _ENV.fullmatch(ref):
            raise BelleConfigError(f"Belle config {where}.token_env must name an environment variable, such as DISCORD_BELLE_TOKEN")
        if name in names:
            raise BelleConfigError(f"Belle config {where}.name duplicates another bot name")
        if ref in refs:
            raise BelleConfigError(f"Belle config {where}.token_env duplicates another bot reference")
        names.add(name)
        refs.add(ref)
    return entries


def read_bots(section: Mapping[str, Any], environ: Mapping[str, str] = os.environ) -> list[DiscordSettings]:
    """Resolve independent named bots; refuse duplicate credential values without displaying them."""
    from dataclasses import replace

    entries = bot_entries(section)
    common = {key: value for key, value in section.items() if key != KEY_BOTS}
    found = []
    tokens: set[str] = set()
    for entry in entries:
        token = environ.get(entry["token_env"], "")
        if not token or not token.strip():
            raise BelleConfigError(f"Belle config {KEY_PATH}.bots needs the bot token in {entry['token_env']}")
        if token != token.strip():
            raise BelleConfigError(f"Belle config {KEY_PATH}.bots token environment value must have no surrounding whitespace")
        if token in tokens:
            raise BelleConfigError(f"Belle config {KEY_PATH}.bots has duplicate bot tokens")
        tokens.add(token)
        found.append(replace(read_settings(common, {TOKEN_ENV: token}), bot=entry["name"]))
    return found
