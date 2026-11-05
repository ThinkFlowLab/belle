"""JiuwenSwarm model names used by Slack scopes."""

from pathlib import Path

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.integrations.jiuwenswarm import config as belle_config
from belle_agent.integrations.jiuwenswarm.config import configured_models
from belle_agent.integrations.jiuwenswarm.slack_tool_config import slack_tool_config

TEMPLATE = Path(__file__).resolve().parents[2] / "examples" / "config" / "belle-config.yaml"


@pytest.fixture(autouse=True)
def template_config(monkeypatch) -> None:
    """Read Belle's configuration from the example template, never from the home of whoever runs the tests."""
    monkeypatch.setenv("BELLE_AGENT_CONFIG_FILE", str(TEMPLATE))


def test_configured_models_matches_adapter_names_and_aliases(monkeypatch) -> None:
    from jiuwenswarm.common import config as host_config

    monkeypatch.setattr(
        host_config,
        "get_default_models",
        lambda: [
            {"model_client_config": {"model_name": "main"}, "alias": "quick"},
            {"model_client_config": {"model_name": "main"}, "alias": "fast"},
            {"model_client_config": {}},
            {"model_client_config": {"model_name": "backup"}},
        ],
    )
    models = configured_models()
    assert models.names == ("main", "backup")
    assert models.alias_pairs == (("quick", "main"), ("fast", "main"))
    assert models.accepts("quick")
    assert models.default == "main"
    assert not models.accepts("main#1")


def test_matrix_only_config_loads_without_slack(tmp_path, monkeypatch) -> None:
    config = tmp_path / "belle.yaml"
    config.write_text("platforms:\n  matrix:\n    accounts: []\n")
    monkeypatch.setenv("BELLE_AGENT_CONFIG_FILE", str(config))
    loaded = belle_config.load_belle_config()
    assert loaded == {"platforms": {"matrix": {"accounts": []}}}
    with pytest.raises(BelleConfigError, match="platforms.slack must be a mapping"):
        slack_tool_config({"channels": {"slack": {}}}, loaded)


def test_idle_expiry_of_attached_sessions_is_refused(tmp_path, monkeypatch) -> None:
    config = tmp_path / "belle.yaml"
    config.write_text("attached_sessions:\n  expire_after_idle_hours: 24\n")
    monkeypatch.setenv("BELLE_AGENT_CONFIG_FILE", str(config))
    with pytest.raises(BelleConfigError, match="expire_after_idle_hours must be disabled on JiuwenSwarm"):
        belle_config.check_belle_config()
    # Code that reads the config at run time does not refuse it: the refusal belongs to the start.
    assert belle_config.load_belle_config()["attached_sessions"] == {"expire_after_idle_hours": 24}
    config.write_text("attached_sessions:\n  expire_after_idle_hours: disabled\n")
    belle_config.check_belle_config()


def test_every_extension_checks_the_config_before_it_registers() -> None:
    import ast

    extensions = Path(belle_config.__file__).parent / "extensions"
    for path in sorted(extensions.glob("*/extension.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        (register,) = (node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "register_extensions")
        calls = [statement.value.func.id for statement in register.body
                 if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                 and isinstance(statement.value.func, ast.Name)]
        assert calls[:1] == ["check_belle_config"], path


def test_default_slack_channel_uses_first_workspace_fallback(monkeypatch) -> None:
    assert belle_config.slack_default_channel_id_from_config({}) == ""
    blocks = [
        {"bot_token": "b1", "app_token": "a1"},
        {"bot_token": "b2", "app_token": "a2", "default_channel_id": " C-WORKSPACE "},
    ]
    monkeypatch.setattr(belle_config, "load_belle_config", lambda: {"platforms": {"slack": {"workspaces": blocks}}})
    settings = {"channels": {"slack": {}}}
    assert belle_config.slack_default_channel_id_from_config(settings) == "C-WORKSPACE"
    settings["channels"]["slack"]["default_channel_id"] = "C-GLOBAL"
    assert belle_config.slack_default_channel_id_from_config(settings) == "C-GLOBAL"
    del settings["channels"]["slack"]["default_channel_id"]
    blocks[:] = ["invalid"]
    assert belle_config.slack_default_channel_id_from_config(settings) == ""


def test_default_slack_channel_reads_host_config(monkeypatch) -> None:
    from jiuwenswarm.common import config as host_config

    monkeypatch.setattr(host_config, "get_config", lambda: {"channels": {"slack": {"default_channel_id": " C-HOST "}}})
    assert belle_config.slack_default_channel_id_from_config() == "C-HOST"

    def unreadable():
        raise OSError("config is unavailable")

    monkeypatch.setattr(host_config, "get_config", unreadable)
    assert belle_config.slack_default_channel_id_from_config() == ""
