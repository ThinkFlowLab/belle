"""Matrix: one account or several, each served by a helper process that holds its encryption."""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import expand_env, platform_section
from belle_agent.platforms.matrix.settings import KEY_ACCOUNTS, KEY_PATH, PASSPHRASE_ENV, SECTION, account_entries


def create_platform(config: Mapping[str, Any], state_dir: Path | None = None) -> Any:
    """Build the Matrix connector from the ``platforms.matrix`` section. A store defaults to a directory in ``state_dir``.

    The connector is imported here, not with the package.
    """
    section = platform_section(config, SECTION)
    if isinstance(section, Mapping) and section.get(KEY_ACCOUNTS):
        from belle_agent.platforms.matrix.accounts import create_accounts

        return create_accounts(config, state_dir)
    from belle_agent.platforms.matrix.connector import MatrixPlatform
    from belle_agent.platforms.matrix.settings import matrix_settings

    return MatrixPlatform(matrix_settings(config, state_dir))


def credentials(section: Mapping[str, Any]) -> dict[str, bool]:
    """Name each credential the section needs, and whether it resolves.

    A key is named by the variable it is read from, where the section reads it from one. A section that lists
    accounts needs one account that is on, and an entry that is on in part is refused here.
    """
    if section.get(KEY_ACCOUNTS):
        on = any(not entry.off for entry in account_entries(section))
        return {f"an account of {KEY_PATH}.{KEY_ACCOUNTS} whose homeserver, user_id and store passphrase resolve": on}
    names: dict[str, bool] = {}
    for key in ("homeserver", "user_id"):
        value = str(section.get(key) or "")
        variable = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", value.strip())
        names[variable[1] if variable else f"{KEY_PATH}.{key}"] = bool(expand_env(value).strip())
    names[PASSPHRASE_ENV] = bool(os.environ.get(PASSPHRASE_ENV, "").strip() or os.environ.get(f"{PASSPHRASE_ENV}_FILE", "").strip())
    return names


__all__ = ["SECTION", "create_platform", "credentials"]
