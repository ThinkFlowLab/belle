"""Read the ``platforms.matrix`` section of a Belle configuration, and the secrets from the environment.

The section names one account, or lists several under ``accounts``. Secrets never come from the
configuration file. Each is read from a variable, or from the file that the variable of the same
name with ``_FILE`` names, and never both.
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from belle_agent.attachments import MAX_FILE_BYTES
from belle_agent.config import BelleConfigError, expand_env, platform_section
from belle_agent.emoji import GLYPH_SHORTCODES, VARIATION_SELECTORS
from belle_agent.identifiers import id_list

logger = logging.getLogger(__name__)

SECTION = "matrix"
KEY_PATH = f"platforms.{SECTION}"
HOMESERVER_ENV = "MATRIX_HOMESERVER"
USER_ID_ENV = "MATRIX_USER_ID"
PASSPHRASE_ENV = "MATRIX_STORE_PASSPHRASE"
PASSWORD_ENV = "MATRIX_PASSWORD"
RECOVERY_KEY_ENV = "MATRIX_RECOVERY_KEY"
#: The name of the section's one account, which is each conversation's ``workspace``.
WORKSPACE = "default"
#: The list of accounts. Each entry is one account, with its own helper, store and secrets.
KEY_ACCOUNTS = "accounts"
#: The keys of one entry of ``accounts``. Every other key of the section applies to each account.
ACCOUNT_KEYS = frozenset({
    "name", "enabled", "homeserver", "user_id", "store_dir", "recovery_key_output",
    "store_passphrase_env", "password_env", "recovery_key_env",
})
#: An account's name, which is the workspace of each of its conversations.
ACCOUNT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
_VARIABLE_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
#: The secret each ``*_env`` key of an entry names a variable for, and the variable of the section's one account.
_SECRET_KEYS = {"store_passphrase_env": PASSPHRASE_ENV, "password_env": PASSWORD_ENV, "recovery_key_env": RECOVERY_KEY_ENV}
DEFAULT_HELPER = "belle-matrix-helper"
#: The minimum helper socket line: a 50 MiB file in base64, plus room for metadata.
MAX_LINE_BYTES = 50 * 1024 * 1024 * 4 // 3 + 64 * 1024
#: The reaction each mark key adds, by default: a shortcode of ``belle_agent.emoji``. ``disabled`` or empty adds none.
MARK_EMOJI = {
    "acknowledgement_emoji": "eyes",
    "rejected_emoji": "no_entry_sign",
    "late_emoji": "alarm_clock",
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
_GLYPHS = {name: glyph for glyph, name in GLYPH_SHORTCODES.items()}
CATCH_UP_SECONDS = 300
#: What a running turn shows, each with its default: its reply as it is written, and its activity card.
SURFACES: Mapping[str, Any] = {
    "enable_streaming": False,
    "activity_card": True,
    "activity_card_delay_seconds": 5.0,
    "activity_card_min_edit_seconds": 10.0,
}
#: The most characters of text one tool result holds.
TEXT_BUDGET_KEY = "history_max_total_chars"
TEXT_BUDGET = 200_000
MEMBERSHIP_HISTORY_MAX_EVENTS = 20
_KEYS = frozenset({
    "enabled", "homeserver", "user_id", "device_name", "store_dir", "recovery_key_output", "helper_path", KEY_ACCOUNTS,
    "allow_from", "allowed_channel_ids", "bot_user_ids", "catch_up_seconds", "typing_indicator", TEXT_BUDGET_KEY,
    "file_download_max_bytes", "file_upload_max_bytes", "membership_history_max_events",
    "command_name",
    *MARK_EMOJI,
    *SURFACES,
    # Read by the host's decision gate.
    "decision_questions_file", "decision_forward",
})


def secret(name: str) -> str:
    """The value of ``name``, or the content of the file ``<name>_FILE`` names. Refuse both, and an unreadable file."""
    value = os.environ.get(name, "").strip()
    path = os.environ.get(f"{name}_FILE", "").strip()
    if value and path:
        raise BelleConfigError(f"Belle config {KEY_PATH}: set {name} or {name}_FILE, not both")
    if not path:
        return value
    try:
        return Path(path).read_text().strip()
    except OSError as exc:
        raise BelleConfigError(f"Belle config {KEY_PATH}: {name}_FILE names {path}, which cannot be read: {exc.strerror}") from exc


@dataclass(frozen=True)
class MatrixSettings:
    """The account, its store, and the helper that serves it."""

    homeserver: str
    user_id: str
    store_dir: str
    store_passphrase: str = field(repr=False)
    #: The account's name, which is the workspace of each of its conversations.
    account: str = WORKSPACE
    #: Where the account is in the configuration, as messages name its keys.
    where: str = KEY_PATH
    #: The variable of each secret, as a message names it.
    passphrase_env: str = PASSPHRASE_ENV
    password_env: str = PASSWORD_ENV
    recovery_key_env: str = RECOVERY_KEY_ENV
    password: str = field(default="", repr=False)
    recovery_key: str = field(default="", repr=False)
    recovery_key_output: str = ""
    device_name: str = "belle"
    helper_path: str = DEFAULT_HELPER
    #: Who may invite the bot into a room. Empty: anybody.
    allow_from: frozenset[str] = frozenset()
    #: Accounts whose messages are a bot's, beside any ``m.notice``.
    bot_user_ids: frozenset[str] = frozenset()
    #: A message older than this when it reaches Belle, after downtime or a late decryption, gets no turn.
    catch_up_seconds: int = CATCH_UP_SECONDS
    typing_indicator: bool = True
    #: The most characters of text one tool result holds. A longer text file is refused whole.
    max_text_chars: int = TEXT_BUDGET
    membership_history_max_events: int = MEMBERSHIP_HISTORY_MAX_EVENTS
    file_download_max_bytes: int = MAX_FILE_BYTES
    file_upload_max_bytes: int = MAX_FILE_BYTES
    #: The reaction key of each mark key, or empty for none.
    emoji: Mapping[str, str] = field(default_factory=lambda: {key: _emoji({}, key) for key in MARK_EMOJI})
    #: The word of the command, after ``!`` or ``/``.
    command_name: str = "belle"
    enable_streaming: bool = False
    activity_card: bool = True
    activity_card_delay_seconds: float = 5.0
    activity_card_min_edit_seconds: float = 10.0


def reaction_key(name: str) -> str:
    """The key of the reaction a name gives: a shortcode of the core emoji table, or an emoji character as is."""
    if name in _GLYPHS:
        return _GLYPHS[name]
    if name.strip() and not any(char.isascii() for char in name):
        return name
    raise ValueError(
        f"must be a shortcode of the table GLYPH_SHORTCODES in belle_agent/emoji.py, such as eyes, or an emoji"
        f" character, not {name!r}"
    )


def normalize_reaction_key(key: str) -> str:
    """Remove presentation selectors before comparing reaction keys."""
    return "".join(char for char in key if char not in VARIATION_SELECTORS)


def _emoji(section: Mapping[str, Any], key: str) -> str:
    """The reaction key a mark key names, or empty for none."""
    value = section.get(key, MARK_EMOJI[key])
    if value in ("", NO_EMOJI, None):
        return ""
    try:
        return reaction_key(value if isinstance(value, str) else repr(value))
    except ValueError as exc:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{key} {exc}, or {NO_EMOJI}") from None


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


def _user_ids(section: Mapping[str, Any], key: str) -> frozenset[str]:
    ids = id_list(section.get(key))
    wrong = [user for user in ids if not (user.startswith("@") and ":" in user)]
    if wrong:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{key} must list full user ids such as @alice:example.org, not {wrong[0]!r}")
    return frozenset(ids)


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = platform_section(config, SECTION) or {}
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config {KEY_PATH} must be a mapping")
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise BelleConfigError(f"Belle config {KEY_PATH} has unknown keys: {', '.join(unknown)}. Known: {', '.join(sorted(_KEYS))}")
    return section


def _text(source: Mapping[str, Any], key: str, default: str = "") -> str:
    value = source.get(key)
    return expand_env(str(value)).strip() if value not in (None, "") else default


def _shared(section: Mapping[str, Any]) -> dict[str, Any]:
    """The settings of the section that apply to each account."""
    catch_up = section.get("catch_up_seconds", CATCH_UP_SECONDS)
    if isinstance(catch_up, bool) or not isinstance(catch_up, int) or catch_up < 0:
        raise BelleConfigError(f"Belle config {KEY_PATH}.catch_up_seconds must be a whole number of seconds, 0 or more")
    typing_indicator = section.get("typing_indicator", True)
    if not isinstance(typing_indicator, bool):
        raise BelleConfigError(f"Belle config {KEY_PATH}.typing_indicator must be true or false")
    budget = section.get(TEXT_BUDGET_KEY, TEXT_BUDGET)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{TEXT_BUDGET_KEY} must be a whole number of characters, 1 or more")
    file_limits = {}
    for key in ("file_download_max_bytes", "file_upload_max_bytes"):
        value = section.get(key, MAX_FILE_BYTES)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise BelleConfigError(f"Belle config {KEY_PATH}.{key} must be a positive integer")
        file_limits[key] = value
    membership_limit = section.get("membership_history_max_events", MEMBERSHIP_HISTORY_MAX_EVENTS)
    if type(membership_limit) is not int or membership_limit < 1:
        raise BelleConfigError(f"Belle config {KEY_PATH}.membership_history_max_events must be a positive integer")
    command_name = _text(section, "command_name", "belle")
    if not re.fullmatch(r"[a-z0-9_-]+", command_name):
        raise BelleConfigError(
            f"Belle config {KEY_PATH}.command_name must be lower-case letters, digits, _ or -, such as belle, not {command_name!r}"
        )
    return {
        "device_name": _text(section, "device_name", "belle"),
        "helper_path": _text(section, "helper_path", DEFAULT_HELPER),
        # The host reads allow_from too, and owns its form.
        "allow_from": frozenset(id_list(section.get("allow_from"))),
        "bot_user_ids": _user_ids(section, "bot_user_ids"),
        "catch_up_seconds": catch_up,
        "typing_indicator": typing_indicator,
        "max_text_chars": budget,
        "membership_history_max_events": membership_limit,
        **file_limits,
        "emoji": {key: _emoji(section, key) for key in MARK_EMOJI},
        "command_name": command_name,
        **{key: _surface(section, key) for key in SURFACES},
    }


def _account(
    source: Mapping[str, Any], where: str, name: str, secrets: Mapping[str, str], state_dir: Path | None, shared: Mapping[str, Any]
) -> MatrixSettings:
    """One account at ``where``. ``secrets`` names the variable of each ``*_env`` key."""
    homeserver, user_id = _text(source, "homeserver"), _text(source, "user_id")
    passphrase_env = secrets["store_passphrase_env"]
    passphrase = secret(passphrase_env)
    for value, label in ((homeserver, f"{where}.homeserver"), (user_id, f"{where}.user_id"), (passphrase, passphrase_env)):
        if not value:
            raise BelleConfigError(f"Belle config {where} cannot start: {label} is not set")
    if not homeserver.startswith(("http://", "https://")):
        raise BelleConfigError(f"Belle config {where}.homeserver must be an http or https URL, not {homeserver!r}")
    if not (user_id.startswith("@") and ":" in user_id):
        raise BelleConfigError(f"Belle config {where}.user_id must be a full user id such as @belle:example.org, not {user_id!r}")
    store_dir = _text(source, "store_dir")
    if not store_dir:
        if state_dir is None:
            raise BelleConfigError(f"Belle config {where}.store_dir is not set, and Belle has no state directory")
        store_dir = str(state_dir / "matrix" / name)
    output = _text(source, "recovery_key_output")
    if output and Path(output).resolve().is_relative_to(Path(store_dir).resolve()):
        raise BelleConfigError(f"Belle config {where}.recovery_key_output must be outside store_dir ({store_dir})")
    return MatrixSettings(
        homeserver=homeserver,
        user_id=user_id,
        store_dir=store_dir,
        store_passphrase=passphrase,
        account=name,
        where=where,
        passphrase_env=passphrase_env,
        password_env=secrets.get("password_env", ""),
        recovery_key_env=secrets.get("recovery_key_env", ""),
        password=secret(secrets["password_env"]) if "password_env" in secrets else "",
        recovery_key=secret(secrets["recovery_key_env"]) if "recovery_key_env" in secrets else "",
        recovery_key_output=output,
        **shared,
    )


def matrix_settings(config: Mapping[str, Any], state_dir: Path | None = None) -> MatrixSettings:
    """Read the section's one account. Refuse an unknown key, and a required value that does not resolve, by name."""
    section = _section(config)
    if section.get(KEY_ACCOUNTS):
        raise BelleConfigError(f"Belle config {KEY_PATH} lists {KEY_ACCOUNTS}: name the account to use")
    return _account(section, KEY_PATH, WORKSPACE, _SECRET_KEYS, state_dir, _shared(section))


@dataclass(frozen=True)
class AccountEntry:
    """One entry of ``accounts``: its name, its variables, and whether it is on."""

    #: Where the entry is, as messages name its keys.
    where: str
    name: str
    #: The configuration of the entry, as written.
    source: Mapping[str, Any]
    #: The variable each ``*_env`` key of the entry names.
    secrets: Mapping[str, str]
    #: Empty where the entry is on, else why it is off.
    off: str


def account_entries(section: Mapping[str, Any]) -> list[AccountEntry]:
    """Each entry of ``accounts``, checked, and whether it is on. Refuse a list that does not fit, with what to correct.

    An entry is on when its homeserver, its user id and its store passphrase resolve. ``enabled: false`` turns it
    off, and ``enabled: true`` makes a missing one an error, as is an entry with some of the three and not all.
    """
    listed = section.get(KEY_ACCOUNTS)
    where = f"{KEY_PATH}.{KEY_ACCOUNTS}"
    if not isinstance(listed, list) or not listed or any(not isinstance(entry, Mapping) for entry in listed):
        raise BelleConfigError(f"Belle config {where} must be a list of mappings, one for each account")
    beside = [key for key in ("homeserver", "user_id", "store_dir", "recovery_key_output") if _text(section, key)]
    if beside:
        raise BelleConfigError(
            f"Belle config {KEY_PATH}.{beside[0]} is set beside {where}. Each account names its own {beside[0]}: remove one"
        )
    entries: list[AccountEntry] = []
    seen: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(listed):
        at = f"{where}[{index}]"
        unknown = sorted(set(entry) - ACCOUNT_KEYS)
        if unknown:
            raise BelleConfigError(
                f"Belle config {at} has unknown keys: {', '.join(unknown)}. An account takes {', '.join(sorted(ACCOUNT_KEYS))}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not ACCOUNT_NAME_RE.match(name):
            raise BelleConfigError(
                f"Belle config {at}.name must be 1 to 32 letters, digits, - and _, starting with a letter or a digit,"
                " such as work"
            )
        enabled = entry.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise BelleConfigError(f"Belle config {at}.enabled must be true, false or empty")
        secrets: dict[str, str] = {}
        for key, variable in _SECRET_KEYS.items():
            value = entry.get(key)
            if value is None and key != "store_passphrase_env":
                continue
            if not isinstance(value, str) or not _VARIABLE_RE.match(value):
                raise BelleConfigError(
                    f"Belle config {at}.{key} must be the name of an environment variable, such as {variable}_WORK."
                    f" Belle reads the secret from it, or from the file that {variable}_WORK_FILE names"
                )
            secrets[key] = value
        homeserver, user_id = _text(entry, "homeserver"), _text(entry, "user_id")
        unique = {
            # Names are compared without case: each is a directory of the state directory.
            "name": name.casefold(),
            "user_id": user_id,
            "store_dir": str(Path(_text(entry, "store_dir")).resolve()) if _text(entry, "store_dir") else "",
            "recovery_key_output": str(Path(_text(entry, "recovery_key_output")).resolve()) if _text(entry, "recovery_key_output") else "",
        }
        for key, value in unique.items():
            if value and (key, value) in seen:
                raise BelleConfigError(
                    f"Belle config {at}.{key} is the {key} of {seen[key, value]} as well. Each entry is an account of its"
                    " own, with a store of its own"
                )
            seen[key, value] = at
        passphrase_env = secrets["store_passphrase_env"]
        missing = [
            label
            for label, resolves in (
                (f"{at}.homeserver", bool(homeserver)),
                (f"{at}.user_id", bool(user_id)),
                (passphrase_env, bool(os.environ.get(passphrase_env, "").strip() or os.environ.get(f"{passphrase_env}_FILE", "").strip())),
            )
            if not resolves
        ]
        off = ""
        if enabled is False:
            off = "enabled is false"
        elif missing and (enabled or len(missing) < 3):
            turned_on = "enabled is true" if enabled else "some of its homeserver, user_id and store passphrase are set"
            raise BelleConfigError(
                f"Belle config {at} cannot start: {turned_on}, and {' and '.join(missing)}"
                f" {'is' if len(missing) == 1 else 'are'} not set. Set {'it' if len(missing) == 1 else 'them'},"
                f" or set {at}.enabled: false"
            )
        elif missing:
            off = f"{' and '.join(missing)} are not set"
        entries.append(AccountEntry(at, name, entry, secrets, off))
    return entries


def read_accounts(config: Mapping[str, Any], state_dir: Path | None = None) -> list[MatrixSettings]:
    """The settings of each entry of ``accounts`` that is on, in the order written."""
    section = _section(config)
    shared = _shared(section)
    found = []
    for entry in account_entries(section):
        if entry.off:
            logger.info("Belle config %s (%s) is off and is not connected: %s", entry.where, entry.name, entry.off)
            continue
        found.append(_account(entry.source, entry.where, entry.name, entry.secrets, state_dir, shared))
    if not found:
        raise BelleConfigError(f"Belle config {KEY_PATH}.{KEY_ACCOUNTS} holds no account that is on")
    return found
