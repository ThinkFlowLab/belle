"""Resolve Belle paths without creating directories."""

import os
from dataclasses import dataclass
from pathlib import Path

from belle_agent.config import BelleConfigError


@dataclass(frozen=True)
class BellePaths:
    config_dir: Path
    config_file: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path | None
    config_file_explicit: bool


def _override(name: str) -> Path | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    if not raw.strip():
        raise BelleConfigError(f"{name} must name an absolute path")
    try:
        path = Path(raw).expanduser()
    except RuntimeError as exc:
        raise BelleConfigError(f"{name} must name an absolute path") from exc
    if not path.is_absolute():
        raise BelleConfigError(f"{name} must name an absolute path")
    return path


def _xdg_base(name: str) -> Path | None:
    raw = os.getenv(name)
    path = Path(raw) if raw else None
    return path if path is not None and path.is_absolute() else None


def resolve_paths() -> BellePaths:
    """Resolve directory overrides and select Belle's config file."""
    home = Path.home()
    config_dir_override = _override("BELLE_AGENT_CONFIG_DIR")
    config_file_override = _override("BELLE_AGENT_CONFIG_FILE")
    config_dir = config_dir_override or (_xdg_base("XDG_CONFIG_HOME") or home / ".config") / "belle-agent"
    data_dir = (
        _override("BELLE_AGENT_DATA_DIR") or (_xdg_base("XDG_DATA_HOME") or home / ".local/share") / "belle-agent"
    )
    state_dir = (
        _override("BELLE_AGENT_STATE_DIR") or (_xdg_base("XDG_STATE_HOME") or home / ".local/state") / "belle-agent"
    )
    cache_dir = _override("BELLE_AGENT_CACHE_DIR") or (_xdg_base("XDG_CACHE_HOME") or home / ".cache") / "belle-agent"
    runtime_base = _xdg_base("XDG_RUNTIME_DIR")
    runtime_dir = _override("BELLE_AGENT_RUNTIME_DIR") or (
        runtime_base / "belle-agent" if runtime_base is not None else None
    )

    legacy = None
    if config_file_override is not None:
        config_file = config_file_override
    elif config_dir_override is not None:
        config_file = config_dir / "config.yaml"
    elif (legacy := os.getenv("BELLE_AGENT_CONFIG")) is not None:
        if not legacy.strip():
            raise BelleConfigError("BELLE_AGENT_CONFIG must name a YAML file")
        config_file = Path(legacy).expanduser().absolute()
    else:
        config_file = config_dir / "config.yaml"

    return BellePaths(
        config_dir=config_dir,
        config_file=config_file,
        data_dir=data_dir,
        state_dir=state_dir,
        cache_dir=cache_dir,
        runtime_dir=runtime_dir,
        config_file_explicit=(
            config_file_override is not None or config_dir_override is not None or legacy is not None
        ),
    )


def selected_config_file() -> Path | None:
    """Return the file to load, or None for missing implicit config."""
    paths = resolve_paths()
    return paths.config_file if paths.config_file_explicit or paths.config_file.exists() else None
