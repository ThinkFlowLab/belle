"""Portable Slack toolkits accept an installation resolver from the host."""

import asyncio
import json

from belle_agent.platforms.slack.directory import SlackDirectoryToolkit
from belle_agent.platforms.slack.pins import SlackPinToolkit


class Workspaces:
    async def settings_for(self, team_id: str) -> dict[str, str]:
        assert team_id == "T1"
        return {"bot_token": "xoxb-test"}


class PinClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def pins_add(self, **kwargs: str) -> dict[str, bool]:
        self.calls.append(kwargs)
        return {"ok": True}


def test_pin_uses_the_supplied_installation_and_request_conversation() -> None:
    client = PinClient()
    toolkit = SlackPinToolkit(
        metadata={"slack_team_id": "T1", "slack_channel_id": "C1"},
        client=client,
        workspaces=Workspaces(),
    )

    result = json.loads(asyncio.run(toolkit.pin_message("1758123456.123456")))

    assert result == {
        "ok": True,
        "chat_id": "C1",
        "message_id": "1758123456.123456",
        "pinned": True,
    }
    assert client.calls == [{"channel": "C1", "timestamp": "1758123456.123456"}]


class RefusingDirectoryClient:
    async def users_list(self, **kwargs: object) -> dict[str, object]:
        return {"ok": False, "error": "missing_scope"}


def test_directory_uses_the_hosts_scope_policy_for_refusals() -> None:
    toolkit = SlackDirectoryToolkit(
        metadata={"slack_team_id": "T1"},
        client=RefusingDirectoryClient(),
        workspaces=Workspaces(),
        scopes_for_kind=lambda kind: ("users:read",) if kind == "person" else (),
    )

    result = json.loads(asyncio.run(toolkit.find_by_name("Ada", "person")))

    assert result["error"] == "missing_scope"
    assert "users:read" in result["detail"]


def test_directory_reports_its_manifest_scopes_without_host_policy() -> None:
    toolkit = SlackDirectoryToolkit(
        metadata={"slack_team_id": "T1"},
        client=RefusingDirectoryClient(),
        workspaces=Workspaces(),
    )

    result = json.loads(asyncio.run(toolkit.find_by_name("Ada", "person")))

    assert result["error"] == "missing_scope"
    assert "users:read" in result["detail"]
