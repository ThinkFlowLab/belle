"""Load Belle settings from one YAML file."""

import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


class BelleConfigError(ValueError):
    """The Belle configuration cannot be used."""


_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _expand_env(value: str) -> str:
    return _REFERENCE.sub(lambda match: os.getenv(match[1], ""), value)


#: Replace each ``${NAME}`` with that environment variable, or with nothing where it is unset.
#: Public for the settings this module does not read: a backend expands its own section with it.
expand_env = _expand_env


#: The listeners Belle serves, each with the keys its settings take: a backend's to its harness, and a
#: platform's to its server.
LISTENER_KEYS = {
    "tools": frozenset({"bind", "port", "url"}),
    "relay": frozenset({"bind", "port"}),
    "actions": frozenset({"bind", "port", "url"}),
}


#: The path of Belle's tool server: every ``tools.url`` ends in it.
TOOLS_PATH = "/mcp"
#: Binds that listen on every address, so that the bind names no address a client uses.
WILDCARD_BINDS = frozenset({"0.0.0.0", "::"})


def tools_authority(bind: str, url: str, field: str = "tools") -> str:
    """Check where clients reach the tool server, and return the ``Host`` they send there, or ``""`` without a url.

    The tool server admits only the ``Host`` values it knows, so a wildcard bind needs the url that clients use.
    ``field`` names the listener in messages.
    """
    if bind in WILDCARD_BINDS and not url:
        raise BelleConfigError(f"{field}.url must name the address clients use when the listener binds a wildcard address")
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise BelleConfigError(f"{field}.url must be an HTTP URL with a valid host and port") from exc
    if (
        parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
        or parsed.path != TOOLS_PATH or parsed.query or parsed.fragment
    ):
        raise BelleConfigError(f"{field}.url must be an HTTP URL ending in {TOOLS_PATH}, with no credentials, query, or fragment")
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def listener_settings(section: Mapping[str, Any], name: str, where: str) -> dict[str, Any]:
    """Read where the listener ``name`` of a section binds: ``<name>_bind``, ``<name>_port``, ``<name>_url``.

    ``where`` names the section in messages. With no entry the listener binds loopback on a free port.
    """
    settings = section.get(name) or {}
    if not isinstance(settings, Mapping) or not set(settings) <= LISTENER_KEYS[name]:
        raise BelleConfigError(f"Belle config {where}.{name} is a mapping of {', '.join(sorted(LISTENER_KEYS[name]))}")
    port = settings.get("port", 0)
    if isinstance(port, str):
        port = _expand_env(port)
        try:
            port = int(port) if port else 0
        except ValueError:
            raise BelleConfigError(f"Belle config {where}.{name}.port must be a port number") from None
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise BelleConfigError(f"Belle config {where}.{name}.port must be a port number")
    found: dict[str, Any] = {f"{name}_bind": _expand_env(str(settings.get("bind") or "")) or "127.0.0.1", f"{name}_port": port}
    if "url" in settings:
        found[f"{name}_url"] = _expand_env(str(settings["url"] or ""))
    if name == "tools":
        tools_authority(found["tools_bind"], found.get("tools_url", ""), f"Belle config {where}.tools")
    return found


_NULL = type(None)

#: Each top-level section Belle reads, with the types it takes.
SECTIONS: dict[str, tuple[tuple[type, ...], str]] = {
    "attached_sessions": ((dict, _NULL), "a mapping or null"),
    "backend": ((str, _NULL), "a backend name or null"),
    "backends": ((dict, _NULL), "a mapping of backend names to their sections, or null"),
    "control": ((dict,), "a mapping"),
    "models": ((dict, _NULL), "a mapping or null"),
    "people": ((dict, _NULL), "a mapping or null"),
    "platforms": ((dict, _NULL), "a mapping of platform names to their sections, or null"),
    "roles": ((dict, _NULL), "a mapping or null"),
    "scheduling": ((dict,), "a mapping"),
    "scopes": ((list, _NULL), "a list or null"),
    "second_brain": ((dict, _NULL), "a mapping or null"),
}


#: Each retired top-level key, with the place of its setting now.
_MOVED = {
    "decision": "The decision model goes under models: models.decision.entries",
    "platform": "A platform section turns its platform on: platforms.<name>.enabled",
}


def _check_sections(data: dict[str, Any]) -> None:
    """Refuse a top-level key that names no section, a section of the wrong type, and an unknown platform."""
    from belle_agent.platforms.base import platform_names  # The platform port imports this module.

    for key, value in data.items():
        if key in platform_names():
            raise BelleConfigError(f"Belle config {key} is not a section. Platform sections go under platforms: platforms.{key}")
        if key in _MOVED:
            raise BelleConfigError(f"Belle config {key} is not a section. {_MOVED[key]}")
        if key not in SECTIONS:
            raise BelleConfigError(f"Belle config {key} is not a section. Sections: {', '.join(sorted(SECTIONS))}")
        if not isinstance(value, SECTIONS[key][0]):
            raise BelleConfigError(f"Belle config {key} must be {SECTIONS[key][1]}")
    for name in data.get("platforms") or {}:
        if name not in platform_names():
            raise BelleConfigError(
                f"Belle config platforms.{name} names no platform. The platforms are {', '.join(platform_names())}"
            )


#: Each secret field of the shared sections, with an example variable. ``*`` is any key, ``[]`` any list item,
#: and ``{}`` in the example is the key that ``*`` matched. Platform packages add their own: ``SECRET_FIELDS``.
SECRET_FIELDS: dict[tuple[str, ...], str] = {
    ("models", "*", "entries", "[]", "api_key"): "{}_API_KEY",
    ("second_brain", "retrieval", "embeddings", "api_key"): "EMBEDDINGS_API_KEY",
    ("backends", "acp", "tools_token"): "BELLE_TOOLS_TOKEN",
}


def _found(
    node: Any, path: tuple[str, ...], where: str, matched: tuple[str, ...]
) -> Iterator[tuple[str, Any, tuple[str, ...]]]:
    """Yield the place, the value and the ``*`` keys of each value at ``path`` under ``node``."""
    if not path:
        yield where, node, matched
        return
    step, rest = path[0], path[1:]
    if step == "[]" and isinstance(node, list):
        for index, item in enumerate(node):
            yield from _found(item, rest, f"{where}[{index}]", matched)
    elif step == "*" and isinstance(node, dict):
        for key, item in node.items():
            yield from _found(item, rest, f"{where}.{key}", (*matched, str(key)))
    elif isinstance(node, dict) and step in node:
        yield from _found(node[step], rest, f"{where}.{step}" if where else step, matched)


def _check_secrets(data: dict[str, Any]) -> None:
    """Refuse a secret field that holds a value, not one ``${NAME}`` reference. The message never shows the value."""
    from belle_agent.platforms.base import platform_secret_fields  # The platform port imports this module.

    for path, example in {**SECRET_FIELDS, **platform_secret_fields()}.items():
        for where, value, matched in _found(data, path, "", ()):
            if value in (None, "") or (isinstance(value, str) and _REFERENCE.fullmatch(value)):
                continue
            variable = example.format(*(key.upper() for key in matched))
            raise BelleConfigError(f"Belle config {where} must be an environment reference such as ${{{variable}}}, not a literal value")


def platform_section(config: Mapping[str, Any], name: str) -> Any:
    """Return the section ``platforms.<name>`` of the settings, or None where they hold none.

    The caller checks its type.
    """
    platforms = config.get("platforms")
    return platforms.get(name) if isinstance(platforms, Mapping) else None


def model_entry(config: Mapping[str, Any], role: str, selected: str = "") -> Mapping[str, Any] | None:
    """Return one Belle model entry for a task role."""
    models = config.get("models") or {}
    group = models.get(role) if isinstance(models, Mapping) else None
    if not isinstance(group, Mapping):
        return None
    entries = group.get("entries") or []
    wanted = selected or group.get("default")
    if wanted:
        return next((entry for entry in entries if entry.get("id") == wanted), None)
    return entries[0] if len(entries) == 1 else None


def _validate_model_group(data: dict[str, Any], role: str, apis: set[str]) -> None:
    models = data.get("models") or {}
    if role not in models:
        return
    group = models[role]
    prefix = f"models.{role}"
    if not isinstance(group, dict):
        raise BelleConfigError(f"Belle config {prefix} must be a mapping")
    entries = group.get("entries")
    if not isinstance(entries, list):
        raise BelleConfigError(f"Belle config {prefix}.entries must be a list")
    ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BelleConfigError(f"Belle config {prefix}.entries must contain mappings")
        name = entry.get("id")
        if not isinstance(name, str) or not name.strip() or name in ids:
            raise BelleConfigError(f"Belle config {prefix}.entries needs unique nonempty ids")
        ids.add(name)
        if entry.get("api") not in apis:
            raise BelleConfigError(f"Belle config {prefix}.entries[{name}].api must be one of {sorted(apis)}")
        for key in ("api_base", "api_key", "model_name"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                raise BelleConfigError(f"Belle config {prefix}.entries[{name}].{key} must be a string or null")
            if value:
                entry[key] = _expand_env(value)
        if role == "decision":
            if entry.get("model_config_obj") is not None and not isinstance(entry["model_config_obj"], dict):
                raise BelleConfigError(f"Belle config {prefix}.entries[{name}].model_config_obj must be a mapping")
        elif entry.get("client_provider") is not None and not isinstance(entry["client_provider"], str):
            raise BelleConfigError(f"Belle config {prefix}.entries[{name}].client_provider must be a string")
        if role == "summarizer":
            if entry.get("client_provider") not in (None, "OpenAI"):
                raise BelleConfigError(
                    f"Belle config {prefix}.entries[{name}].client_provider must be OpenAI or absent"
                )
            _validate_http_url(entry.get("api_base"), f"{prefix}.entries[{name}].api_base")
    default = group.get("default")
    if default is not None and (not isinstance(default, str) or default not in ids):
        raise BelleConfigError(f"Belle config {prefix}.default must name an entry id")
    if role == "decision" and len(entries) > 1 and not default:
        raise BelleConfigError(f"Belle config {prefix}.default is required for multiple entries")


def _validate_http_url(value: Any, field: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        raise BelleConfigError(f"Belle config {field} must be an absolute HTTP URL without query or fragment")
    try:
        parts = urlsplit(value)
        valid = parts.scheme in {"http", "https"} and bool(parts.netloc) and not parts.query and not parts.fragment
    except ValueError:
        valid = False
    if not valid:
        raise BelleConfigError(f"Belle config {field} must be an absolute HTTP URL without query or fragment")


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Read Belle settings and check top-level types."""
    if path is None:
        return {"platforms": {"slack": {"enforce_audience": True}}}

    config_path = Path(path)
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise BelleConfigError(f"Cannot read Belle config: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise BelleConfigError(f"Invalid YAML in Belle config: {config_path}") from exc

    if not isinstance(data, dict):
        raise BelleConfigError("Belle config root must be a mapping")
    _check_sections(data)
    _check_secrets(data)
    from belle_agent.control.service import ControlSettings

    try:
        ControlSettings.from_config(data)
    except ValueError as exc:
        raise BelleConfigError(str(exc)) from exc
    from belle_agent.platforms.base import validate_platform_config

    validate_platform_config(data, config_path)
    ingest = (data.get("second_brain") or {}).get("ingest")
    summarizer = ingest.get("summarizer") if isinstance(ingest, dict) else None
    selectors: dict[str, str] = {}
    if isinstance(summarizer, dict):
        for key in ("api_base", "api_key", "client_provider"):
            if key in summarizer:
                raise BelleConfigError(
                    f"Belle config second_brain.ingest.summarizer.{key} is not a setting."
                    " The summarizer endpoint goes under models: models.summarizer.entries"
                )
        for key in ("model", "outline_model"):
            value = summarizer.get(key) or ""
            if not isinstance(value, str):
                raise BelleConfigError(f"Belle config second_brain.ingest.summarizer.{key} must be a string")
            if value:
                summarizer[key] = selectors[key] = _expand_env(value)
    selected = selectors.get("model", "")
    retrieval = (data.get("second_brain") or {}).get("retrieval")
    embeddings = retrieval.get("embeddings") if isinstance(retrieval, dict) else None
    if isinstance(embeddings, dict):
        for key in ("api_base", "api_key", "model"):
            if isinstance(embeddings.get(key), str):
                embeddings[key] = _expand_env(embeddings[key])
        _validate_http_url(embeddings.get("api_base"), "second_brain.retrieval.embeddings.api_base")
    models = data.get("models")
    _validate_model_group(data, "decision", {"openrouter", "openrouter_text_state", "openai", "scaledown"})
    _validate_model_group(data, "summarizer", {"chat_completions", "openrouter", "scaledown"})
    _validate_model_group(data, "extractor", {"openrouter", "scaledown"})
    _validate_model_group(data, "classifier", {"scaledown"})
    if models and "summarizer" in models:
        for key, value in selectors.items():
            if model_entry(data, "summarizer", value) is None:
                raise BelleConfigError(
                    f"Belle config second_brain.ingest.summarizer.{key} must name a models.summarizer entry id"
                )
        if model_entry(data, "summarizer", selected) is None and models["summarizer"]["entries"]:
            raise BelleConfigError("Belle config models.summarizer needs a default or second_brain.ingest.summarizer.model")
    from belle_agent.host.attached_sessions import attached_session_settings
    from belle_agent.scheduling.settings import validate_scheduling

    validate_scheduling(data)
    attached_session_settings(data)
    return data
