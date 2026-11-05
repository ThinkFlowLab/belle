"""Read ``backends.acp`` of a Belle configuration: the selected harness profile and the tool server.

A profile is configuration data only: where the relay's socket is, the session's working directory, whether
each conversation gets its own connection, how Belle's tools reach the harness, and how a tool call names its
conversation. This module imports no ACP library, so a configuration error is reported without one.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent.config import BelleConfigError, expand_env, listener_settings

SECTION = "acp"
SHARED = "shared"
PER_CONVERSATION = "per_conversation"
#: Belle sends an HTTP mount of its tool server in each ``session/new``.
MOUNT_SESSION = "session"
#: The harness's own configuration mounts Belle's tool server.
MOUNT_AGENT = "agent"
#: Belle offers the harness no tool, for an agent that can neither take a mount nor bind a call to a conversation.
MOUNT_NONE = "none"

_KEYS = frozenset({"profile", "profiles", "tools", "tools_token"})
_PROFILE_KEYS = frozenset(
    {
        "socket",
        "cwd",
        "connection",
        "tools_mount",
        "binding",
        "start_timeout_seconds",
        "cancel_timeout_seconds",
        "permission_timeout_seconds",
        "remembered_requests",
        "tool_name_format",
    }
)
_REQUIRED = ("socket", "cwd", "connection", "tools_mount")


@dataclass(frozen=True)
class Profile:
    """One harness behind one relay socket. Each limit is the operator's resource setting."""

    name: str
    #: The relay's Unix socket. Belle connects to it and starts no process.
    socket: str
    #: The working directory of each ACP session, absolute as ACP requires.
    cwd: str
    #: ``shared``: one connection holds every conversation's session. ``per_conversation``: one connection, and so
    #: one agent process behind the relay, for each conversation. Its first line is the conversation's binding.
    connection: str
    #: ``session`` or ``agent``: who mounts Belle's tool server in the harness.
    tools_mount: str
    #: The ``_meta`` key of a ``tools/call`` that holds the ACP session id, or empty.
    binding_meta: str = ""
    #: The request header of a ``tools/call`` that holds the connection's binding line, or empty.
    binding_header: str = ""
    #: How the harness names one of Belle's tools to the model, with ``{name}`` for the tool's own name.
    tool_name_format: str = "{name}"
    #: How long the agent may take to answer ``initialize``, its process start included.
    start_timeout: float = 60.0
    #: How long a turn may take to end after ``session/cancel``. Past it the outcome is reported unknown.
    cancel_timeout: float = 10.0
    #: How long a permission request waits for a person. Past it the request is answered ``cancelled``.
    permission_timeout: float = 900.0
    #: How many request ids the state file keeps, for duplicates and recovery.
    remembered_requests: int = 1024


@dataclass(frozen=True)
class Settings:
    """The selected profile and where Belle serves its tools."""

    profile: Profile
    tools_bind: str
    tools_port: int
    #: Where the harness reaches the tool server. Empty: the address the server listens on.
    tools_url: str
    #: The tool server's bearer token. Empty: a new random token at each start.
    tools_token: str


def _number(section: Mapping[str, Any], key: str, where: str, default: float, *, whole: bool = False) -> Any:
    raw = section.get(key, default)
    if isinstance(raw, str):
        raw = expand_env(raw).strip() or default
    try:
        value = int(raw) if whole else float(raw)
    except (TypeError, ValueError, OverflowError):
        value = 0
    if isinstance(raw, bool) or not math.isfinite(value) or value <= 0 or (whole and isinstance(raw, float)):
        kind = "a whole number, 1 or more" if whole else "a number of seconds above 0"
        raise BelleConfigError(f"Belle config {where}.{key} must be {kind}")
    return value


def _text(section: Mapping[str, Any], key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not expand_env(value).strip():
        raise BelleConfigError(f"Belle config {where}.{key} must be a non-empty string")
    return expand_env(value).strip()


def _profile(name: str, section: Any, where: str) -> Profile:
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config {where} must be a mapping of {', '.join(_REQUIRED)}")
    unknown = sorted(set(section) - _PROFILE_KEYS)
    if unknown:
        raise BelleConfigError(f"Belle config {where} has unknown keys: {', '.join(unknown)}")
    missing = [key for key in _REQUIRED if key not in section]
    if missing:
        raise BelleConfigError(f"Belle config {where} needs {', '.join(missing)}")
    socket, cwd = _text(section, "socket", where), _text(section, "cwd", where)
    for key, path in (("socket", socket), ("cwd", cwd)):
        if not os.path.isabs(path):
            raise BelleConfigError(f"Belle config {where}.{key} must be an absolute path, not {path!r}")
    connection = _text(section, "connection", where)
    if connection not in (SHARED, PER_CONVERSATION):
        raise BelleConfigError(f"Belle config {where}.connection must be {SHARED} or {PER_CONVERSATION}")
    mount = _text(section, "tools_mount", where)
    if mount not in (MOUNT_SESSION, MOUNT_AGENT, MOUNT_NONE):
        raise BelleConfigError(
            f"Belle config {where}.tools_mount must be {MOUNT_SESSION}, {MOUNT_AGENT} or {MOUNT_NONE}"
        )
    if (mount == MOUNT_NONE) == ("binding" in section):
        # A binding names the conversation of a tool call, so it belongs with a mount and only with one.
        raise BelleConfigError(
            f"Belle config {where}.binding is required by tools_mount: {MOUNT_SESSION} or {MOUNT_AGENT},"
            f" and refused with tools_mount: {MOUNT_NONE}"
        )
    bound = {"meta": "", "header": ""}
    if mount != MOUNT_NONE:
        binding = section["binding"]
        if not isinstance(binding, Mapping) or len(binding) != 1 or set(binding) - set(bound):
            raise BelleConfigError(f"Belle config {where}.binding must be a mapping with one key: meta or header")
        kind = next(iter(binding))
        bound[kind] = _text(binding, kind, f"{where}.binding")
        if kind == "header" and connection != PER_CONVERSATION:
            # One agent process has one binding line. Shared by conversations, it would bind each to the same one.
            raise BelleConfigError(f"Belle config {where}.binding.header needs connection: {PER_CONVERSATION}")
        if kind == "meta" and connection == PER_CONVERSATION:
            # Session IDs cannot distinguish calls from independent agent processes, including during setup.
            raise BelleConfigError(
                f"Belle config {where}.binding.meta needs connection: {SHARED};"
                f" use binding.header with connection: {PER_CONVERSATION}"
            )
    names = section.get("tool_name_format", Profile.tool_name_format)
    rest = names.replace("{name}", "", 1) if isinstance(names, str) else "{"
    if "{" in rest or "}" in rest or "{name}" not in names:
        raise BelleConfigError(f"Belle config {where}.tool_name_format must hold {{name}} once and no other field")
    return Profile(
        name=name,
        tool_name_format=names,
        socket=socket,
        cwd=cwd,
        connection=connection,
        tools_mount=mount,
        binding_meta=bound["meta"],
        binding_header=bound["header"],
        start_timeout=_number(section, "start_timeout_seconds", where, Profile.start_timeout),
        cancel_timeout=_number(section, "cancel_timeout_seconds", where, Profile.cancel_timeout),
        permission_timeout=_number(section, "permission_timeout_seconds", where, Profile.permission_timeout),
        remembered_requests=_number(section, "remembered_requests", where, Profile.remembered_requests, whole=True),
    )


def read_settings(config: Mapping[str, Any]) -> Settings:
    """Read and check ``backends.acp``. Each error names the key and the form it expects."""
    where = f"backends.{SECTION}"
    backends = config.get("backends")
    section = backends.get(SECTION) if isinstance(backends, Mapping) else None
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config {where} must be a mapping that names a profile and its profiles")
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise BelleConfigError(f"Belle config {where} has unknown keys: {', '.join(unknown)}")
    profiles = section.get("profiles")
    if not isinstance(profiles, Mapping) or not profiles:
        raise BelleConfigError(f"Belle config {where}.profiles must be a mapping of profile names to profiles")
    # Each profile, so that a broken one is refused before a deployment selects it.
    checked = {str(key): _profile(str(key), value, f"{where}.profiles.{key}") for key, value in profiles.items()}
    name = _text(section, "profile", where)
    if name not in checked:
        raise BelleConfigError(f"Belle config {where}.profile names {name!r}, which is not in {where}.profiles")
    profile = checked[name]
    # load_config refuses a literal token: SECRET_FIELDS lists this field.
    token = expand_env(str(section.get("tools_token") or ""))
    if profile.tools_mount == MOUNT_AGENT and not token:
        # The harness reads the token from its own environment, so it cannot learn a token drawn at each start.
        raise BelleConfigError(f"Belle config {where}.tools_token is needed by tools_mount: {MOUNT_AGENT}")
    tools = listener_settings(section, "tools", where)
    return Settings(
        profile=profile,
        tools_bind=tools["tools_bind"],
        tools_port=tools["tools_port"],
        tools_url=tools.get("tools_url", ""),
        tools_token=token,
    )
