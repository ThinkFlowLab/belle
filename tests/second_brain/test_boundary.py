"""The corpus package has no harness imports."""

import ast
from pathlib import Path

from belle_agent.second_brain.paths import parse_ingest_channels, second_brain_config


def test_public_ingest_is_the_default_but_an_empty_list_disables_it():
    default = second_brain_config({})["ingest"]["channels"]
    with_other_ingest_settings = second_brain_config({"second_brain": {"ingest": {"link": {"enabled": False}}}})
    disabled = second_brain_config({"second_brain": {"ingest": {"channels": []}}})["ingest"]["channels"]

    assert parse_ingest_channels(default) == (False, True, frozenset())
    assert with_other_ingest_settings["ingest"] == {"channels": ["public"], "link": {"enabled": False}}
    assert parse_ingest_channels(disabled) == (False, False, frozenset())


def test_generic_second_brain_has_no_harness_imports():
    root = Path(__file__).resolve().parents[2] / "src/belle_agent/second_brain"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names = (node.module or "",)
            else:
                continue
            assert not any(name.split(".")[0] in {"openjiuwen", "jiuwenswarm"} for name in names), path


def test_only_the_jiuwenswarm_integration_imports_jiuwenswarm():
    """The second brain runs on any host. JiuwenSwarm's own services stay in its integration."""
    package = Path(__file__).resolve().parents[2] / "src/belle_agent"
    integration = package / "integrations/jiuwenswarm"
    for path in package.rglob("*.py"):
        if path.is_relative_to(integration):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            assert not any(name.split(".")[0] == "jiuwenswarm" for name in names), path
