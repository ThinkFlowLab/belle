"""Build the adapter from ``backends.docker_agent`` in a Belle configuration.

The section says where the server is and where Docker Agent reaches this
process. The two secrets come from the environment only: the server's bearer
token, and the token the relay hooks send. Neither is accepted in the
configuration file, and neither is logged.
"""

import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from belle_agent.config import BelleConfigError, expand_env, listener_settings
from belle_agent.integrations.docker_agent.adapter import MAX_REQUEST_BYTES, DockerAgentBackend

SECTION = "docker_agent"
#: The server's bearer token, which `docker-agent serve api` takes as `--auth-token`.
TOKEN_ENV = "DOCKER_AGENT_AUTH_TOKEN"
#: The token every relay hook of the agent YAML sends to Belle's hook endpoint.
RELAY_TOKEN_ENV = "BELLE_RELAY_TOKEN"

_KEYS = frozenset({"url", "namespace", "tools", "relay", "max_request_bytes"})
_SECRETS = {"token": TOKEN_ENV, "auth_token": TOKEN_ENV, "relay_token": RELAY_TOKEN_ENV}


def create_backend(config: Mapping[str, Any], state_dir: Path) -> DockerAgentBackend:
    """Build the adapter a configuration describes, keeping its state in ``state_dir``. It is not started."""
    where = f"backends.{SECTION}"
    backends = config.get("backends")
    section = backends.get(SECTION) if isinstance(backends, Mapping) else None
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config {where} must be a mapping that names the server's url")
    relay = section.get("relay", {})
    if not isinstance(relay, Mapping):
        raise BelleConfigError(f"Belle config backends.{SECTION}.relay must be a mapping of bind, port")
    for key, variable in _SECRETS.items():
        if key in section or key in relay:
            raise BelleConfigError(
                f"Belle config {where} holds a {key}: a secret is never written in the configuration."
                f" Set the environment variable {variable}"
            )
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise BelleConfigError(f"Belle config {where} has unknown keys: {', '.join(unknown)}")
    url = expand_env(str(section.get("url") or "")).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise BelleConfigError(
            f"Belle config {where}.url must be the http address of the Docker Agent server, with no secret in it"
        )
    largest = expand_env(str(section.get("max_request_bytes") or "")).strip() or str(MAX_REQUEST_BYTES)
    if not largest.isdigit() or int(largest) < 1:
        raise BelleConfigError(f"Belle config {where}.max_request_bytes must be a number of bytes")
    tokens = {name: os.environ.get(name, "") for name in (TOKEN_ENV, RELAY_TOKEN_ENV)}
    missing = [name for name, value in tokens.items() if not value]
    if missing:
        raise BelleConfigError(f"Docker Agent's tokens are read from the environment: {', '.join(missing)} is not set")
    return DockerAgentBackend(
        url,
        tokens[TOKEN_ENV],
        relay_token=tokens[RELAY_TOKEN_ENV],
        state_dir=state_dir,
        namespace=expand_env(str(section.get("namespace") or "")),
        max_request_bytes=int(largest),
        **listener_settings(section, "tools", where),
        **listener_settings(section, "relay", where),
    )
