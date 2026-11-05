"""Build a Discord bot invite and optionally check its token."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from belle_agent.config import BelleConfigError, load_config, platform_section
from belle_agent.paths import selected_config_file
from belle_agent.platforms.discord.client import USER_AGENT
from belle_agent.platforms.discord.connector import registered_toolkits
from belle_agent.platforms.discord.settings import TOKEN_ENV, DiscordSettings, bot_entries, read_settings

_TOKEN = "validation-only"
_TOOL_PERMISSIONS = {
    # Discord permission flags. Keep this list tied to the capabilities the toolkit registers.
    "post_message": (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15) | (1 << 16) | (1 << 18) | (1 << 35) | (1 << 37) | (1 << 38) | (1 << 49),
    "read_discord_conversation": (1 << 10) | (1 << 16) | (1 << 20),
    "find_by_name": (1 << 10) | (1 << 16) | (1 << 20),
    "download_discord_attachment": (1 << 10) | (1 << 16) | (1 << 20),
    "search_discord_guild": (1 << 10) | (1 << 16) | (1 << 20),
    "react_to_message": (1 << 6) | (1 << 10) | (1 << 16) | (1 << 18),
    "read_discord_reactions": (1 << 10) | (1 << 16),
    "read_pinned_messages": (1 << 10) | (1 << 16),
    "pin_message": (1 << 10) | (1 << 16) | (1 << 51),
    "send_discord_file": (1 << 10) | (1 << 11) | (1 << 15) | (1 << 16) | (1 << 18) | (1 << 35) | (1 << 38),
    "edit_message": (1 << 10) | (1 << 16) | (1 << 18),
    "delete_message": (1 << 10) | (1 << 16),
    "list_discord_threads": (1 << 10) | (1 << 16) | (1 << 34),
    "list_discord_thread_members": (1 << 10) | (1 << 16),
    "read_discord_poll": (1 << 10) | (1 << 16) | (1 << 20),
    "end_discord_poll": (1 << 10) | (1 << 16),
    "create_discord_thread": (1 << 10) | (1 << 16) | (1 << 35) | (1 << 36) | (1 << 38),
    "update_discord_thread": (1 << 10) | (1 << 16) | (1 << 34) | (1 << 38),
    "change_discord_thread_member": (1 << 10) | (1 << 16) | (1 << 34) | (1 << 38),
    "forward_discord_message": (1 << 10) | (1 << 11) | (1 << 16),
    "crosspost_discord_message": (1 << 10) | (1 << 11) | (1 << 16),
    "set_discord_thread_pinned": (1 << 10) | (1 << 16) | (1 << 34),
    "list_discord_scheduled_events": 1 << 10,
    "get_discord_scheduled_event": 1 << 10,
    "list_discord_event_users": 1 << 10,
    "list_discord_soundboard_sounds": 1 << 10,
    "get_discord_soundboard_sound": 1 << 10,
    "list_discord_default_sounds": 0,
}


def invite_url(application_id: str, permissions: int) -> str:
    """Return a Discord OAuth URL for a validated application id and permission bitfield."""
    if not re.fullmatch(r"[0-9]{1,24}", application_id):
        raise ValueError("application_id must contain 1 to 24 decimal digits")
    query = urllib.parse.urlencode({"client_id": application_id, "scope": "bot applications.commands", "permissions": str(permissions)})
    return f"https://discord.com/oauth2/authorize?{query}"


def requested_permissions(settings: DiscordSettings, tool_names: set[str]) -> int:
    """Calculate grants for registered Discord tools and enabled presentation features."""
    unknown = tool_names - _TOOL_PERMISSIONS.keys()
    if unknown:
        raise ValueError(f"no Discord invite permission map for registered tool: {min(unknown)}")
    permissions = 0
    for name in tool_names:
        permissions |= _TOOL_PERMISSIONS[name]
    if settings.allow_role_mentions or settings.allow_everyone_mentions:
        permissions |= 1 << 17
    if settings.allow_tts:
        permissions |= 1 << 12
    return permissions


def _registered_tools(settings: DiscordSettings) -> set[str]:
    """Read names from every Discord toolkit's actual tool definitions."""
    platform = SimpleNamespace(_settings=settings)
    return {str(tool["name"]) for toolkit in registered_toolkits(platform, dict)
            for tool in toolkit.get_tool_definitions()}


def _permissions_report(permissions: int) -> list[str]:
    names = (
        (10, "View Channels"), (11, "Send Messages"), (16, "Read Message History"), (6, "Add Reactions"),
        (12, "Send TTS Messages"), (14, "Embed Links"), (15, "Attach Files"),
        (17, "Mention Everyone"), (20, "Connect"),
        (34, "Manage Threads"), (35, "Create Public Threads"), (36, "Create Private Threads"),
        (38, "Send Messages in Threads"), (49, "Send Polls"), (51, "Pin Messages"),
    )
    return [name for bit, name in names if permissions & (1 << bit)]


def _verify_token(token: str) -> str:
    """Return the bot identity from Discord; errors intentionally omit response bodies and headers."""
    request = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Discord rejected the selected bot token (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError("Could not reach Discord to verify the bot token") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Discord returned an invalid bot identity response") from None
    if not isinstance(data, dict) or not data.get("id") or not data.get("username") or data.get("bot") is not True:
        raise ValueError("Discord response did not identify a bot account")
    return f"{data['username']} (bot id {data['id']})"


def _settings(config: Mapping[str, Any], bot: str | None = None) -> DiscordSettings:
    section = platform_section(config, "discord") or {}
    if not isinstance(section, Mapping):
        raise BelleConfigError("Belle config platforms.discord must be a mapping")
    if "bots" in section:
        entries = bot_entries(section)
        if bot not in {entry["name"] for entry in entries}:
            raise BelleConfigError("Discord install helper requires --bot naming a configured bot")
        section = {key: value for key, value in section.items() if key != "bots"}
    elif bot is not None:
        raise BelleConfigError("--bot requires platforms.discord.bots")
    return read_settings(section, {TOKEN_ENV: _TOKEN})


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-id", required=True, help="Application ID from Discord's Developer Portal")
    parser.add_argument("--config", type=Path, help="Belle YAML config (default: selected Belle config)")
    parser.add_argument("--verify-token", action="store_true", help=f"check {TOKEN_ENV} with Discord's API")
    parser.add_argument("--bot", help="Bot name from platforms.discord.bots")
    options = parser.parse_args(arguments)
    try:
        config_path = options.config or selected_config_file()
        config = load_config(config_path)
        settings = _settings(config, options.bot)
        token_env = TOKEN_ENV
        if options.bot:
            token_env = next(
                entry["token_env"] for entry in bot_entries(platform_section(config, "discord") or {})
                if entry["name"] == options.bot
            )
        permissions = requested_permissions(settings, _registered_tools(settings))
        url = invite_url(options.application_id, permissions)
        print(f"Invite URL: {url}")
        grants = _permissions_report(permissions)
        print("Requested bot permissions: " + (", ".join(grants) if grants else "none"))
        intents = []
        if settings.message_content:
            intents.append("Message Content")
        members = "Server Members (REST audience checks"
        members += " and Gateway member events)" if settings.guild_members else "; Gateway member events are off)"
        intents.append(members)
        print("Developer Portal → Bot → Privileged Gateway Intents: " + (", ".join(intents) if intents else "none"))
        print("A server administrator must approve this invite. Channel overwrites can still deny bot actions.")
        print("Members need Use Application Commands in the server/channel to run /belle.")
        if options.verify_token:
            token = os.environ.get(token_env, "").strip()
            if not token:
                raise ValueError(f"{token_env} is not set; invite generation did not validate a token")
            print(f"Verified bot: {_verify_token(token)}")
        else:
            print(f"Token status: not checked (set {token_env} and pass --verify-token to check it)")
        return 0
    except (BelleConfigError, OSError, ValueError) as exc:
        print(f"Discord install helper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
