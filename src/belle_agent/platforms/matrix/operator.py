"""``python -m belle_agent.platforms.matrix``: the operator's commands for the bot account.

Each command starts the helper on the account's store, as Belle does, so stop
Belle first: the helper refuses a store that another process uses.

- ``devices`` lists the account's devices.
- ``delete-devices <device id> ...`` deletes devices with the account's password.
- ``reset-identity --yes`` replaces the account's cross-signing identity, deletes its
  key backup, and writes a new recovery key to ``recovery_key_output``.

Where the section lists ``accounts``, ``--account <name>`` names the account.
"""

import argparse
import asyncio
import datetime
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import BelleConfigError, load_config, platform_section
from belle_agent.paths import resolve_paths, selected_config_file
from belle_agent.platforms.matrix.connector import refusal, start_request
from belle_agent.platforms.matrix.helper import Helper, HelperError
from belle_agent.platforms.matrix.settings import (
    KEY_ACCOUNTS,
    KEY_PATH,
    MAX_LINE_BYTES,
    SECTION,
    MatrixSettings,
    matrix_settings,
    read_accounts,
)

#: The start refusals that end a reset as it should: the new key is written.
RESET_DONE = frozenset({"identity_reset", "bootstrapped"})


def _device_line(device: dict[str, Any]) -> str:
    seen = device.get("last_seen_ts")
    when = datetime.datetime.fromtimestamp(seen / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if seen else "-"
    return "\t".join([device["device_id"], when, device.get("display_name") or "-", "this store" if device["current"] else ""]).rstrip()


async def run(settings: MatrixSettings, command: str, device_ids: list[str]) -> int:
    """Start the helper, run one command, stop the helper. Return the exit code."""
    helper = Helper(settings.helper_path, max_line_bytes=MAX_LINE_BYTES, on_event=lambda event: None)
    try:
        await helper.open()
    except FileNotFoundError:
        print(f"{KEY_PATH}.helper_path names {settings.helper_path}, which is not an executable on PATH", file=sys.stderr)
        return 1
    try:
        fields = start_request(settings)
        if command == "reset-identity":
            fields["reset_identity"] = True
        try:
            await helper.request("start", **fields)
        except HelperError as exc:
            if command == "reset-identity" and exc.reason in RESET_DONE:
                keep = (
                    f"{settings.recovery_key_env} or {settings.recovery_key_env}_FILE" if settings.recovery_key_env
                    else f"the variable that {settings.where}.recovery_key_env names"
                )
                print(
                    f"The identity is new. Its recovery key is in {settings.recovery_key_output}. Keep it as"
                    f" {keep}, then start Belle. Each other device of the account needs it to recover."
                )
                return 0
            print(refusal(exc, settings), file=sys.stderr)
            return 1
        if command == "devices":
            for device in (await helper.request("devices"))["devices"]:
                print(_device_line(device))
        elif command == "delete-devices":
            await helper.request("delete_devices", devices=device_ids, password=settings.password)
            print(f"Deleted {len(device_ids)} device(s): {', '.join(device_ids)}")
        return 0
    except HelperError as exc:
        print(f"{command} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await helper.close()


def _settings(config: Mapping[str, Any], state_dir: Path, account: str | None) -> MatrixSettings:
    """The settings of the account that ``account`` names, or of the section's one account."""
    section = platform_section(config, SECTION)
    if not (isinstance(section, Mapping) and section.get(KEY_ACCOUNTS)):
        if account is not None:
            raise BelleConfigError(f"--account names {account}, and {KEY_PATH} lists no {KEY_ACCOUNTS}: leave --account out")
        return matrix_settings(config, state_dir)
    accounts = {settings.account: settings for settings in read_accounts(config, state_dir)}
    if account not in accounts:
        raise BelleConfigError(f"name the account with --account, one of {', '.join(accounts)}, the accounts of {KEY_PATH}.{KEY_ACCOUNTS} that are on")
    return accounts[account]


def main(arguments: list[str] | None = None) -> int:
    """Parse the command line, read the settings, and run the command. Return the exit code."""
    parser = argparse.ArgumentParser(prog="python -m belle_agent.platforms.matrix", description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="the Belle configuration file (default: BELLE_AGENT_CONFIG_FILE)")
    parser.add_argument("--state-dir", type=Path, help="where Belle keeps its state (default: BELLE_AGENT_STATE_DIR)")
    parser.add_argument("--account", help=f"the name of the account, where {KEY_PATH}.{KEY_ACCOUNTS} lists several")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", help="list the account's devices")
    delete = commands.add_parser("delete-devices", help="delete devices of the account, with its password")
    delete.add_argument("device_ids", nargs="+", metavar="DEVICE_ID")
    reset = commands.add_parser("reset-identity", help="make a new identity and recovery key, with the account's password")
    reset.add_argument("--yes", action="store_true", help="confirm: the old key backup is deleted, and each other device loses its signature")
    options = parser.parse_args(arguments)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if options.command == "reset-identity" and not options.yes:
        print("reset-identity needs --yes: it deletes the account's key backup, and each other device loses its signature", file=sys.stderr)
        return 2
    try:
        path = options.config or selected_config_file()
        if path is None:
            raise BelleConfigError("no configuration file: name one with --config")
        settings = _settings(load_config(path), options.state_dir or resolve_paths().state_dir, options.account)
    except BelleConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    if options.command != "devices" and not settings.password:
        where = f"{settings.password_env} or {settings.password_env}_FILE" if settings.password_env else (
            f"{settings.where}.password_env to the name of a variable that holds it"
        )
        print(f"{options.command} needs the account password: set {where}", file=sys.stderr)
        return 1
    if options.command == "reset-identity" and not settings.recovery_key_output:
        print(f"reset-identity needs {settings.where}.recovery_key_output: a new file outside the store for the new key", file=sys.stderr)
        return 1
    return asyncio.run(run(settings, options.command, getattr(options, "device_ids", [])))
