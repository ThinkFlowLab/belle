"""Keep Belle workspace selection aligned with the Slack connector."""

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest
import jiuwenswarm
from belle_agent.platforms.slack.history_policy import slack_workspace_blocks

FLAT = {
    "bot_token": "xoxb-one",
    "app_token": "xapp-one",
    "search_enabled": True,
}
TWO = {
    "workspaces": [
        {"bot_token": "xoxb-a", "app_token": "xapp-a"},
        {"bot_token": "xoxb-b", "app_token": "xapp-b"},
    ],
}

# --- the reading agrees with the connector's own ----------------------------


@pytest.mark.parametrize(
    "raw",
    [
        FLAT,
        dict(FLAT, workspaces=[]),
        TWO,
        {"workspaces": [{"bot_token": "xoxb-a"}]},
        {},
    ],
)
def test_the_runtime_reads_the_same_installs_the_connector_builds(raw: Any) -> None:
    """One shape, two readers, pinned to each other.

    The runtime may not import the connector, so it has its own reading of
    ``channels.slack``. A test is the only thing that keeps the two from
    drifting, and a drift here is a tool serving a workspace the connector is
    not connected to.
    """
    from belle_agent.platforms.slack.config import (
        SLACK_WORKSPACES_KEY,
        normalize_slack_conf,
    )

    connector = normalize_slack_conf(raw)[SLACK_WORKSPACES_KEY]
    runtime = slack_workspace_blocks(raw)
    assert [str(block.get("bot_token") or "").strip() for block in connector] == [
        str(block.get("bot_token") or "").strip() for block in runtime
    ]


def test_gateway_lazy_slack_imports_resolve() -> None:
    gateway = Path(jiuwenswarm.__file__).resolve().parent / "gateway/app_gateway.py"
    for node in ast.walk(ast.parse(gateway.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "belle_agent.platforms.slack.config",
            "belle_agent.integrations.jiuwenswarm.slack_channel",
        }:
            module = importlib.import_module(node.module)
            for name in node.names:
                assert hasattr(module, name.name), f"{node.module}.{name.name}"
