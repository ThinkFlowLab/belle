"""Standalone Slack List and canvas calls use an injected installation."""

import asyncio
import json
import logging
from typing import Any

from belle_agent.platforms.slack.bookmarks import _read_refusal_json
from belle_agent.platforms.slack.canvases import SlackCanvasToolkit
from belle_agent.platforms.slack.json_toolkit import slack_refusal_json
from belle_agent.platforms.slack.lists import SlackListToolkit


class Workspaces:
    async def settings_for(self, team_id: str) -> dict[str, str]:
        assert team_id == "T1"
        return {"bot_token": "xoxb-test"}


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def api_call(self, method: str, json: Any = None) -> dict[str, Any]:
        payload = dict(json or {})
        self.calls.append((method, payload))
        if method == "slackLists.items.list":
            return {"ok": True, "items": [{"id": "Rec123456"}]}
        return {"ok": True, "canvas_id": "F1234ABCD"}


def test_list_read_uses_supplied_installation() -> None:
    client = Client()
    toolkit = SlackListToolkit(metadata={"slack_team_id": "T1"}, client=client, workspaces=Workspaces())

    result = json.loads(asyncio.run(toolkit.read_slack_list("F1234ABCD")))

    assert result["ok"] is True
    assert result["items"] == [{"id": "Rec123456"}]
    assert client.calls[0][0] == "slackLists.items.list"


def test_canvas_create_uses_supplied_installation() -> None:
    client = Client()
    toolkit = SlackCanvasToolkit(metadata={"slack_team_id": "T1"}, client=client, workspaces=Workspaces())

    result = json.loads(asyncio.run(toolkit.write_slack_canvas(title="Runbook", markdown="Hello")))

    assert result["canvas_id"] == "F1234ABCD"
    assert client.calls == [
        ("canvases.create", {"title": "Runbook", "document_content": {"type": "markdown", "markdown": "Hello"}})
    ]


def test_canvas_refuses_direct_message_before_api_call() -> None:
    client = Client()
    toolkit = SlackCanvasToolkit(metadata={"slack_team_id": "T1"}, client=client, workspaces=Workspaces())

    result = json.loads(asyncio.run(toolkit.write_slack_canvas(chat_id="D123456789")))

    assert result["error"] == "chat_id_is_a_direct_message"
    assert client.calls == []


def test_refusals_preserve_resource_fields_and_logging(caplog) -> None:
    result = json.loads(
        slack_refusal_json(logging.getLogger(__name__), "canvas", "denied", "No accès", canvas_id="F1", chat_id="")
    )
    assert result == {"ok": False, "error": "denied", "detail": "No accès", "canvas_id": "F1"}
    assert "slack canvas refused: denied -- No accès" in caplog.text


def test_failed_bookmark_read_keeps_empty_array(caplog) -> None:
    result = json.loads(_read_refusal_json("denied", "No access", chat_id="C1"))
    assert result == {"ok": False, "error": "denied", "detail": "No access", "chat_id": "C1", "bookmarks": []}
    assert json.loads(_read_refusal_json("denied")) == {"ok": False, "error": "denied", "bookmarks": []}
    assert "slack bookmarks read refused: denied -- No access" in caplog.text
