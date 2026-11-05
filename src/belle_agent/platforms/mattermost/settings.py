"""Read the ``platforms.mattermost`` section of a Belle configuration.

The section names one server, or lists several under ``servers``. A bot token
is read from the environment and from nowhere else. It is never accepted in
the configuration file and never logged.
"""

import logging
import os
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from belle_agent.attachments import MAX_FILE_BYTES
from belle_agent.config import BelleConfigError, expand_env, listener_settings, platform_section

logger = logging.getLogger(__name__)

SECTION = "mattermost"
#: Where the section is in the configuration, as messages name its keys.
KEY_PATH = f"platforms.{SECTION}"
#: The variable that holds the bot account's access token.
TOKEN_ENV = "MATTERMOST_BOT_TOKEN"
#: The variable the example configuration reads the server address from.
URL_ENV = "MATTERMOST_URL"
#: The variable that holds the slash command's token, which the server sends with each command.
COMMAND_TOKEN_ENV = "MATTERMOST_COMMAND_TOKEN"
#: The list of servers. A non-empty list replaces the section's own server: its ``url`` and ``actions``.
KEY_SERVERS = "servers"
#: The keys of one entry of ``servers``. Every other key of the section applies to each server.
SERVER_KEYS = frozenset({"name", "enabled", "url", "bot_token_env", "command_token_env", "actions"})
#: A server's name, which the workspace of each of its channels starts with.
SERVER_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,31}\Z")
_VARIABLE_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_URL_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: The reaction each mark key adds, by default. ``disabled`` or an empty value adds none.
MARK_EMOJI = {
    "acknowledgement_emoji": "eyes",
    "rejected_emoji": "no_entry_sign",
    "queued_emoji": "hourglass_flowing_sand",
    "completed_emoji": "disabled",
    "failed_emoji": "x",
    "stopped_emoji": "black_square_for_stop",
    "brain_react_pending": "open_book",
    "brain_react_success": "brain",
    "brain_react_declined": "x",
    "brain_react_failure": "warning",
}
NO_EMOJI = "disabled"
_EMOJI_RE = re.compile(r"\A[a-z0-9_+-]+\Z")
#: Every key of the section. The host reads ``allow_from``, ``allowed_channel_ids`` and the decision gate's
#: ``decision_questions_file`` and ``decision_forward``. ``enabled`` turns it on.
#: What a running turn shows, each with its default.
SURFACES: Mapping[str, Any] = {
    "enable_streaming": False,
    "activity_card": True,
    "activity_card_delay_seconds": 5.0,
    "activity_card_min_edit_seconds": 10.0,
    "typing_indicator": True,
}
#: The most characters of text that one result of a reading tool holds, as Slack's key of the same name.
TEXT_BUDGET_KEY = "history_max_total_chars"
TEXT_BUDGET_DEFAULT = 200_000
DIRECTORY_SCAN_MAX_PAGES_DEFAULT = 25
#: Pages of each channel's latest posts whose reactions are read back after a gap: a reaction can be on any post.
GAP_REACTION_SCAN_MAX_PAGES_DEFAULT = 1
_KEYS = frozenset({
    "enabled", "url", "actions", KEY_SERVERS, "allow_from", "allowed_channel_ids", "decision_questions_file", "decision_forward",
    TEXT_BUDGET_KEY, "directory_scan_max_pages", "audience_member_max_pages", "gap_reaction_scan_max_pages", "file_download_max_bytes", "file_upload_max_bytes", *MARK_EMOJI, *SURFACES,
})
_SECRETS = frozenset({"token", "bot_token", "access_token"})


@dataclass(frozen=True)
class MattermostSettings:
    """Where the server is, and the token the bot uses there."""

    url: str
    token: str = field(repr=False)
    #: The server's name where the section lists several, or empty for the section's one server.
    server: str = ""
    #: The variable the token was read from, as an error names it.
    token_env: str = TOKEN_ENV
    #: The reaction of each mark key, or empty where the key adds none.
    emoji: Mapping[str, str] = field(default_factory=lambda: {key: _emoji({}, key) for key in MARK_EMOJI})
    #: The address the server posts button clicks and the slash command to, or empty for no listener.
    actions_url: str = ""
    actions_bind: str = "127.0.0.1"
    actions_port: int = 0
    #: The slash command's token, or empty: every command is then refused.
    command_token: str = field(default="", repr=False)
    enable_streaming: bool = False
    activity_card: bool = True
    activity_card_delay_seconds: float = 5.0
    activity_card_min_edit_seconds: float = 10.0
    typing_indicator: bool = True
    history_max_total_chars: int = TEXT_BUDGET_DEFAULT
    directory_scan_max_pages: int = DIRECTORY_SCAN_MAX_PAGES_DEFAULT
    audience_member_max_pages: int = DIRECTORY_SCAN_MAX_PAGES_DEFAULT
    gap_reaction_scan_max_pages: int = GAP_REACTION_SCAN_MAX_PAGES_DEFAULT
    file_download_max_bytes: int = MAX_FILE_BYTES
    file_upload_max_bytes: int = MAX_FILE_BYTES


def _text_budget(section: Mapping[str, Any]) -> int:
    value = section.get(TEXT_BUDGET_KEY)
    if value is None:
        return TEXT_BUDGET_DEFAULT
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{TEXT_BUDGET_KEY} must be a positive integer")
    return value


def _emoji(section: Mapping[str, Any], key: str) -> str:
    value = section.get(key)
    if value is None:
        value = MARK_EMOJI[key]
    if not isinstance(value, str) or not (value == "" or _EMOJI_RE.match(value)):
        raise BelleConfigError(
            f"Belle config {KEY_PATH}.{key} must be an emoji name without colons, such as eyes, or {NO_EMOJI}"
        )
    return "" if value == NO_EMOJI else value


def _surface(section: Mapping[str, Any], key: str) -> Any:
    value = section.get(key)
    default = SURFACES[key]
    if value is None:
        return default
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise BelleConfigError(f"Belle config {KEY_PATH}.{key} must be true or false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{key} must be a number of seconds, 0 or more")
    return float(value)


def _actions(source: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Where the listener of button clicks and the slash command binds, and the address the server calls it at."""
    found = listener_settings(source, "actions", where)
    url = found.get("actions_url", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if url and (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment):
        raise BelleConfigError(
            f"Belle config {where}.actions.url must be the http or https address at which the server reaches"
            " Belle, such as http://belle:18400, or empty for no buttons and no slash command"
        )
    return {**found, "actions_url": url}


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = platform_section(config, SECTION)
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config {KEY_PATH} must be a mapping")
    secrets = sorted(_SECRETS & set(section))
    if secrets:
        raise BelleConfigError(
            f"Belle config {KEY_PATH} holds {', '.join(secrets)}: a secret is never written in the configuration."
            f" Set the environment variable {TOKEN_ENV}"
        )
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise BelleConfigError(f"Belle config {KEY_PATH} has unknown keys: {', '.join(unknown)}")
    return section


def _address(source: Mapping[str, Any], where: str, empty: str) -> str:
    """The server address at ``where``, checked. ``empty`` says what to set where there is none."""
    url = expand_env(str(source.get("url") or "")).strip().rstrip("/")
    if not url:
        raise BelleConfigError(f"Belle config {where}.url is empty. {empty}")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise BelleConfigError(
            f"Belle config {where}.url must be the http or https address of the server, such as"
            " https://chat.example.com"
        )
    if parsed.username or parsed.password:
        raise BelleConfigError(f"Belle config {where}.url holds credentials. Put the bot token in its environment variable")
    return url


def _shared(section: Mapping[str, Any]) -> dict[str, Any]:
    """The settings of the section that apply to every server."""
    directory_pages = section.get("directory_scan_max_pages", DIRECTORY_SCAN_MAX_PAGES_DEFAULT)
    if isinstance(directory_pages, bool) or not isinstance(directory_pages, int) or directory_pages < 1:
        raise BelleConfigError("Belle config platforms.mattermost.directory_scan_max_pages must be a positive integer")
    file_bytes = section.get("file_download_max_bytes", MAX_FILE_BYTES)
    if isinstance(file_bytes, bool) or not isinstance(file_bytes, int) or file_bytes < 1:
        raise BelleConfigError("Belle config platforms.mattermost.file_download_max_bytes must be a positive integer")
    upload_bytes = section.get("file_upload_max_bytes", MAX_FILE_BYTES)
    if isinstance(upload_bytes, bool) or not isinstance(upload_bytes, int) or upload_bytes < 1:
        raise BelleConfigError("Belle config platforms.mattermost.file_upload_max_bytes must be a positive integer")
    member_pages = section.get("audience_member_max_pages", DIRECTORY_SCAN_MAX_PAGES_DEFAULT)
    if isinstance(member_pages, bool) or not isinstance(member_pages, int) or member_pages < 1:
        raise BelleConfigError("Belle config platforms.mattermost.audience_member_max_pages must be a positive integer")
    reaction_pages = section.get("gap_reaction_scan_max_pages", GAP_REACTION_SCAN_MAX_PAGES_DEFAULT)
    if isinstance(reaction_pages, bool) or not isinstance(reaction_pages, int) or reaction_pages < 1:
        raise BelleConfigError("Belle config platforms.mattermost.gap_reaction_scan_max_pages must be a positive integer")
    return {
        "emoji": {key: _emoji(section, key) for key in MARK_EMOJI},
        **{key: _surface(section, key) for key in SURFACES},
        "history_max_total_chars": _text_budget(section),
        "directory_scan_max_pages": directory_pages,
        "file_download_max_bytes": file_bytes,
        "file_upload_max_bytes": upload_bytes,
        "audience_member_max_pages": member_pages,
        "gap_reaction_scan_max_pages": reaction_pages,
    }


def read_settings(config: Mapping[str, Any]) -> MattermostSettings:
    """Read the section's one server, or refuse the section with the key or the variable to correct."""
    section = _section(config)
    url = _address(section, KEY_PATH, f"Set it to the server's address, or set {URL_ENV} where the configuration reads it as ${{{URL_ENV}}}")
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise BelleConfigError(f"the Mattermost bot token is read from the environment variable {TOKEN_ENV}, which is not set")
    return MattermostSettings(
        url=url,
        token=token,
        **_actions(section, KEY_PATH),
        command_token=os.environ.get(COMMAND_TOKEN_ENV, "").strip(),
        **_shared(section),
    )


@dataclass(frozen=True)
class ServerEntry:
    """One entry of ``servers``: its name, and whether it is on."""

    #: Where the entry is, as messages name its keys.
    where: str
    name: str
    #: The configuration of the entry, as written.
    source: Mapping[str, Any]
    #: Empty where the entry is on, else why it is off.
    off: str


def _variable(entry: Mapping[str, Any], key: str, where: str, *, required: bool) -> str:
    value = entry.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not _VARIABLE_RE.match(value):
        raise BelleConfigError(
            f"Belle config {where}.{key} must be the name of an environment variable, such as MATTERMOST_BOT_TOKEN"
        )
    return value


def server_entries(section: Mapping[str, Any]) -> list[ServerEntry]:
    """Each entry of ``servers``, checked, and whether it is on. Refuse a list that does not fit with what to correct.

    An entry is on when its ``url`` and its bot token resolve. ``enabled: false`` turns it off, and
    ``enabled: true`` makes a missing one an error, as is an entry with one of the two and not the other.
    """
    listed = section.get(KEY_SERVERS)
    where = f"{KEY_PATH}.{KEY_SERVERS}"
    if not isinstance(listed, list) or any(not isinstance(entry, Mapping) for entry in listed):
        raise BelleConfigError(f"Belle config {where} must be a list of mappings, one for each server")
    beside = {"url": expand_env(str(section.get("url") or "")).strip(), "actions.url": _actions(section, KEY_PATH).get("actions_url")}
    for key, value in beside.items():
        if value:
            raise BelleConfigError(
                f"Belle config {KEY_PATH}.{key} is set beside {where}. Each server names its own {key}: remove one"
            )
    entries: list[ServerEntry] = []
    seen: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(listed):
        at = f"{where}[{index}]"
        secrets = sorted(_SECRETS & set(entry))
        if secrets:
            raise BelleConfigError(
                f"Belle config {at} holds {', '.join(secrets)}: a secret is never written in the configuration."
                f" Name the environment variable that holds it in {at}.bot_token_env"
            )
        unknown = sorted(set(entry) - SERVER_KEYS)
        if unknown:
            raise BelleConfigError(
                f"Belle config {at} has unknown keys: {', '.join(unknown)}. A server takes {', '.join(sorted(SERVER_KEYS))}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not SERVER_NAME_RE.match(name):
            raise BelleConfigError(
                f"Belle config {at}.name must be 1 to 32 lower-case letters, digits, - and _, starting with a letter"
                " or a digit, such as main"
            )
        enabled = entry.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise BelleConfigError(f"Belle config {at}.enabled must be true, false or empty")
        token_env = _variable(entry, "bot_token_env", at, required=True)
        _variable(entry, "command_token_env", at, required=False)
        listener = _actions(entry, at)
        url = expand_env(str(entry.get("url") or "")).strip().rstrip("/")
        unique = {
            "name": name,
            "bot_token_env": token_env,
            "url": url.lower(),
            # Each server reaches a listener of its own, which holds that server's buttons and command token.
            "actions.url": listener.get("actions_url", "").lower(),
            "actions.port": str(listener["actions_port"] or ""),
        }
        for key, value in unique.items():
            if value and (key, value) in seen:
                raise BelleConfigError(
                    f"Belle config {at}.{key} is the {key} of {seen[key, value]} as well. Each entry is a server"
                    " of its own, with a listener of its own"
                )
            seen[key, value] = at
        variable = _URL_VARIABLE_RE.fullmatch(str(entry.get("url") or "").strip())
        missing = [
            label
            for label, resolves in (
                (variable[1] if variable else f"{at}.url", bool(url)),
                (token_env, bool(os.environ.get(token_env, "").strip())),
            )
            if not resolves
        ]
        off = ""
        if enabled is False:
            off = "enabled is false"
        elif missing and (enabled or len(missing) == 1):
            turned_on = "enabled is true" if enabled else "one of its url and its bot token is set"
            raise BelleConfigError(
                f"Belle config {at} cannot start: {turned_on}, and {' and '.join(missing)}"
                f" {'is' if len(missing) == 1 else 'are'} not set. Set {'it' if len(missing) == 1 else 'them'},"
                f" or set {at}.enabled: false"
            )
        elif missing:
            off = f"{' and '.join(missing)} are not set"
        entries.append(ServerEntry(at, name, entry, off))
    return entries


def read_servers(config: Mapping[str, Any]) -> list[MattermostSettings]:
    """The settings of each server of ``servers`` that is on, in the order written."""
    section = _section(config)
    shared = _shared(section)
    found = []
    for entry in server_entries(section):
        if entry.off:
            logger.info("Belle config %s (%s) is off and is not connected: %s", entry.where, entry.name, entry.off)
            continue
        token_env = str(entry.source["bot_token_env"])
        command_env = str(entry.source.get("command_token_env") or "")
        found.append(
            MattermostSettings(
                url=_address(entry.source, entry.where, "Set it to the server's address"),
                token=os.environ[token_env].strip(),
                server=entry.name,
                token_env=token_env,
                **_actions(entry.source, entry.where),
                command_token=os.environ.get(command_env, "").strip() if command_env else "",
                **shared,
            )
        )
    return found
