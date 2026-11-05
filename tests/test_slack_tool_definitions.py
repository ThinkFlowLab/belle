"""Slack tool definitions remain usable without an agent harness."""

import importlib
import json

import pytest


@pytest.mark.parametrize("module, name", [
    ("bookmarks", "SlackBookmarkToolkit"),
    ("canvases", "SlackCanvasToolkit"),
    ("directory", "SlackDirectoryToolkit"),
    ("history", "SlackHistoryToolkit"),
    ("home_tab", "SlackHomeTabToolkit"),
    ("lists", "SlackListToolkit"),
    ("pins", "SlackPinToolkit"),
    ("post", "SlackPostToolkit"),
    ("reactions", "SlackReactionToolkit"),
    ("search", "SlackSearchToolkit"),
])
def test_definitions_describe_platform_actions(module, name):
    cls = getattr(importlib.import_module(f"belle_agent.platforms.slack.{module}"), name)
    toolkit = cls.__new__(cls)
    definitions = toolkit.get_tool_definitions()
    assert definitions
    assert len({definition["name"] for definition in definitions}) == len(definitions)
    for definition in definitions:
        assert callable(getattr(toolkit, definition["name"]))
        assert definition["description"]
        schema = definition["input_params"]
        assert schema["type"] == "object"
        assert set(schema.get("required", ())) <= schema.get("properties", {}).keys()
        assert json.loads(json.dumps(definition)) == definition
