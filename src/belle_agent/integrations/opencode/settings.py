"""Build the adapter from ``backends.opencode`` in a Belle configuration.

The section says where the server is and where OpenCode reaches this process.
The two secrets are read from the environment and from nowhere else: the
server password, and the token the relay plugin sends. Neither is accepted in
the configuration file, and neither is logged.
"""

import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import BelleConfigError, expand_env, listener_settings
from belle_agent.integrations.opencode.adapter import OpenCodeBackend
from belle_agent.integrations.opencode.permissions import TOOL_LEVELS

SECTION = "opencode"
#: The password of the OpenCode server, which the server itself reads from a variable of this name.
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
#: The token the relay plugin sends with each hook, where the plugin is loaded.
RELAY_TOKEN_ENV = "BELLE_RELAY_TOKEN"

_KEYS = frozenset({"url", "username", "namespace", "tools", "relay", "ready_timeout_seconds", "deny_tools"})
_SECRETS = {"password": PASSWORD_ENV, "relay_token": RELAY_TOKEN_ENV, "token": RELAY_TOKEN_ENV}


def create_backend(config: Mapping[str, Any], state_dir: Path | None = None) -> OpenCodeBackend:
    """Build the adapter a configuration describes. It is not started.

    OpenCode's sessions hold what the adapter must remember, so it keeps nothing in ``state_dir``.
    """
    backends = config.get("backends")
    section = backends.get(SECTION) if isinstance(backends, Mapping) else None
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config backends.{SECTION} must be a mapping that names the server's url")
    relay = section.get("relay", {})
    if not isinstance(relay, Mapping):
        raise BelleConfigError(f"Belle config backends.{SECTION}.relay must be a mapping of bind, port")
    for key, variable in _SECRETS.items():
        if key in section or key in relay:
            raise BelleConfigError(
                f"Belle config backends.{SECTION} holds a {key}: a secret is never written in the configuration."
                f" Set the environment variable {variable}"
            )
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise BelleConfigError(f"Belle config backends.{SECTION} has unknown keys: {', '.join(unknown)}")
    url = expand_env(str(section.get("url") or "")).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BelleConfigError(f"Belle config backends.{SECTION}.url must be the http address of the OpenCode server")
    if parsed.username or parsed.password:
        raise BelleConfigError(f"Belle config backends.{SECTION}.url holds credentials. Set the environment variable {PASSWORD_ENV}")
    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        raise BelleConfigError(f"the OpenCode server password is read from the environment variable {PASSWORD_ENV}, which is not set")
    timeout = section.get("ready_timeout_seconds")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        raise BelleConfigError(f"Belle config backends.{SECTION}.ready_timeout_seconds must be a positive number")
    denied = section.get("deny_tools", [])
    if not isinstance(denied, list):
        raise BelleConfigError(f"Belle config backends.{SECTION}.deny_tools must be a list of OpenCode tool or permission names")
    unknown = [str(name) for name in denied if not isinstance(name, str) or name not in TOOL_LEVELS]
    if unknown:
        raise BelleConfigError(
            f"Belle config backends.{SECTION}.deny_tools holds names that it does not take: {', '.join(unknown)}."
            f" It takes these OpenCode tool or permission names: {', '.join(TOOL_LEVELS)}"
        )
    return OpenCodeBackend(
        url,
        password,
        username=str(section.get("username") or "opencode"),
        namespace=expand_env(str(section.get("namespace") or "")),
        relay_token=os.environ.get(RELAY_TOKEN_ENV, ""),
        ready_timeout=timeout,
        deny_tools=frozenset(denied),
        **listener_settings(section, "tools", f"backends.{SECTION}"),
        **listener_settings(section, "relay", f"backends.{SECTION}"),
    )
