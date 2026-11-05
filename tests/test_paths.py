"""Belle path selection and config-file precedence."""

import pytest

from belle_agent.config import BelleConfigError, load_config
from belle_agent.paths import resolve_paths, selected_config_file


@pytest.fixture
def clean_paths(monkeypatch, tmp_path):
    for name in (
        "BELLE_AGENT_CONFIG_DIR",
        "BELLE_AGENT_CONFIG_FILE",
        "BELLE_AGENT_CONFIG",
        "BELLE_AGENT_DATA_DIR",
        "BELLE_AGENT_STATE_DIR",
        "BELLE_AGENT_CACHE_DIR",
        "BELLE_AGENT_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_defaults_do_not_create_directories(clean_paths):
    paths = resolve_paths()
    assert paths.config_dir == clean_paths / ".config/belle-agent"
    assert paths.config_file == paths.config_dir / "config.yaml"
    assert paths.data_dir == clean_paths / ".local/share/belle-agent"
    assert paths.state_dir == clean_paths / ".local/state/belle-agent"
    assert paths.cache_dir == clean_paths / ".cache/belle-agent"
    assert paths.runtime_dir is None
    assert not paths.config_file_explicit
    assert selected_config_file() is None
    assert load_config(selected_config_file()) == load_config(None)
    assert list(clean_paths.iterdir()) == []


def test_absolute_xdg_bases_and_relative_values(clean_paths, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(clean_paths / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(clean_paths / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(clean_paths / "xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(clean_paths / "xdg-cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(clean_paths / "xdg-runtime"))
    paths = resolve_paths()
    assert paths.config_dir == clean_paths / "xdg-config/belle-agent"
    assert paths.data_dir == clean_paths / "xdg-data/belle-agent"
    assert paths.state_dir == clean_paths / "xdg-state/belle-agent"
    assert paths.cache_dir == clean_paths / "xdg-cache/belle-agent"
    assert paths.runtime_dir == clean_paths / "xdg-runtime/belle-agent"

    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(key, "relative")
    assert resolve_paths().config_dir == clean_paths / ".config/belle-agent"
    assert resolve_paths().data_dir == clean_paths / ".local/share/belle-agent"
    assert resolve_paths().state_dir == clean_paths / ".local/state/belle-agent"
    assert resolve_paths().cache_dir == clean_paths / ".cache/belle-agent"
    assert resolve_paths().runtime_dir is None


def test_belle_overrides_select_paths(clean_paths, monkeypatch):
    for key, suffix in (
        ("CONFIG_DIR", "config"),
        ("CONFIG_FILE", "custom.yaml"),
        ("DATA_DIR", "data"),
        ("STATE_DIR", "state"),
        ("CACHE_DIR", "cache"),
        ("RUNTIME_DIR", "runtime"),
    ):
        monkeypatch.setenv(f"BELLE_AGENT_{key}", str(clean_paths / suffix))
    paths = resolve_paths()
    assert paths.config_dir == clean_paths / "config"
    assert paths.config_file == clean_paths / "custom.yaml"
    assert paths.data_dir == clean_paths / "data"
    assert paths.state_dir == clean_paths / "state"
    assert paths.cache_dir == clean_paths / "cache"
    assert paths.runtime_dir == clean_paths / "runtime"
    assert paths.config_file_explicit
    assert list(clean_paths.iterdir()) == []


@pytest.mark.parametrize(
    "key",
    [
        "CONFIG_DIR",
        "CONFIG_FILE",
        "DATA_DIR",
        "STATE_DIR",
        "CACHE_DIR",
        "RUNTIME_DIR",
    ],
)
@pytest.mark.parametrize("value", ["", "relative/path"])
def test_belle_overrides_reject_empty_or_relative_paths(clean_paths, monkeypatch, key, value):
    monkeypatch.setenv(f"BELLE_AGENT_{key}", value)
    with pytest.raises(BelleConfigError, match=f"BELLE_AGENT_{key}"):
        resolve_paths()


def test_tilde_override_is_absolute(clean_paths, monkeypatch):
    monkeypatch.setenv("BELLE_AGENT_DATA_DIR", "~/belle-data")
    assert resolve_paths().data_dir == clean_paths / "belle-data"


def test_config_selection_order_and_missing_explicit_files(clean_paths, monkeypatch):
    legacy = clean_paths / "legacy.yaml"
    directory = clean_paths / "operator-config"
    named = clean_paths / "named.yaml"
    monkeypatch.setenv("BELLE_AGENT_CONFIG", str(legacy))
    monkeypatch.setenv("BELLE_AGENT_CONFIG_DIR", str(directory))
    monkeypatch.setenv("BELLE_AGENT_CONFIG_FILE", str(named))
    assert selected_config_file() == named
    with pytest.raises(BelleConfigError, match="Cannot read Belle config"):
        load_config(selected_config_file())
    monkeypatch.delenv("BELLE_AGENT_CONFIG_FILE")
    assert selected_config_file() == directory / "config.yaml"
    with pytest.raises(BelleConfigError, match="Cannot read Belle config"):
        load_config(selected_config_file())
    monkeypatch.delenv("BELLE_AGENT_CONFIG_DIR")
    assert selected_config_file() == legacy
    with pytest.raises(BelleConfigError, match="Cannot read Belle config"):
        load_config(selected_config_file())
    monkeypatch.delenv("BELLE_AGENT_CONFIG")
    assert selected_config_file() is None


def test_implicit_config_file_is_loaded_when_present(clean_paths):
    path = clean_paths / ".config/belle-agent/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("platforms: {slack: {enforce_audience: false}}\n", encoding="utf-8")
    assert selected_config_file() == path
    assert load_config(selected_config_file())["platforms"]["slack"]["enforce_audience"] is False


def test_legacy_relative_config_path_still_works(clean_paths, monkeypatch):
    monkeypatch.chdir(clean_paths)
    monkeypatch.setenv("BELLE_AGENT_CONFIG", "legacy.yaml")
    assert selected_config_file() == clean_paths / "legacy.yaml"
