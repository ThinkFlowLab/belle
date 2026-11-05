"""Host configuration for standalone Slack posting."""

import json

from belle_agent.platforms.slack.history import SlackWorkspaceClients
from belle_agent.platforms.slack.history_policy import METADATA_ENFORCE_AUDIENCE_KEY
from belle_agent.platforms.slack.post import SlackPostToolkit


async def test_post_uses_injected_bot_name_resolver() -> None:
    clients = SlackWorkspaceClients(
        config_loader=lambda: {"bot_token": "test", "bot_name": "  Ada  "}
    )
    toolkit = SlackPostToolkit(
        workspaces=clients,
        bot_name_resolver=lambda value: str(value).strip().upper(),
    )

    await toolkit._load_settings({"slack_team_id": "T1"})
    assert toolkit._bot_name == "ADA"


def test_requester_attribution_stays_in_context() -> None:
    toolkit = SlackPostToolkit()
    note = toolkit._attribution_note("U_REQUESTER", "U_BOT")
    text, blocks = toolkit._attributed("hello", None, note)

    assert blocks[0] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Posted by <@U_BOT> on behalf of <@U_REQUESTER>"}],
    }
    assert text.startswith(note)


async def test_partial_post_reports_the_part_slack_accepted() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_postMessage(self, **kwargs):
            self.calls += 1
            return {"ok": True, "ts": "1710000000.000001"} if self.calls == 1 else {
                "ok": False, "error": "channel_not_found",
            }

    client = Client()
    toolkit = SlackPostToolkit(
        metadata={"slack_channel_id": "C0HERE", METADATA_ENFORCE_AUDIENCE_KEY: False},
        client=client,
        workspaces=SlackWorkspaceClients(config_loader=lambda: {"bot_token": "test"}),
    )
    text = "\n\n".join("paragraph " + "x" * 2000 for _ in range(30))

    result = json.loads(await toolkit.post_message(text=text))

    assert result["ok"] is False
    assert result["error"] == "channel_not_found"
    assert result["part_message_ids"] == [result["message_id"]] == ["1710000000.000001"]
    assert result["parts"] == 1 and result["thread_id"] == ""
    assert client.calls == 2


async def test_post_refuses_a_slack_response_without_a_message_id() -> None:
    class Client:
        async def chat_postMessage(self, **kwargs):
            return {"ok": True}

    toolkit = SlackPostToolkit(
        metadata={"slack_channel_id": "C0HERE", METADATA_ENFORCE_AUDIENCE_KEY: False},
        client=Client(),
        workspaces=SlackWorkspaceClients(config_loader=lambda: {"bot_token": "test"}),
    )

    result = json.loads(await toolkit.post_message(text="hello"))

    assert result["ok"] is False and result["error"] == "invalid_response"
    assert "message_id" not in result
    assert "could not be confirmed" in result["detail"]


async def test_single_part_post_reports_one_part() -> None:
    class Client:
        async def chat_postMessage(self, **kwargs):
            return {"ok": True, "ts": "1710000000.000001"}

    toolkit = SlackPostToolkit(
        metadata={"slack_channel_id": "C0HERE", METADATA_ENFORCE_AUDIENCE_KEY: False},
        client=Client(),
        workspaces=SlackWorkspaceClients(config_loader=lambda: {"bot_token": "test"}),
    )

    result = json.loads(await toolkit.post_message(text="hello"))

    assert result["ok"] and result["parts"] == 1
    assert result["message_id"] == "1710000000.000001" and result["thread_id"] == ""
    assert "part_message_ids" not in result


async def test_native_post_options_reach_slack_without_identity_override() -> None:
    class Client:
        sent = None

        async def chat_postMessage(self, **kwargs):
            self.sent = kwargs
            return {"ok": True, "ts": "1710000000.000001"}

    client = Client()
    toolkit = SlackPostToolkit(
        metadata={"slack_channel_id": "C0HERE", METADATA_ENFORCE_AUDIENCE_KEY: False},
        client=client,
        workspaces=SlackWorkspaceClients(config_loader=lambda: {"bot_token": "test"}),
    )
    result = json.loads(await toolkit.post_message(
        markdown_text="**Hello**", attachments=[{"text": "Details"}],
        metadata={"event_type": "example", "event_payload": {"id": 1}},
        parse="none", link_names=False, unfurl_app_links=True,
    ))

    assert result["ok"]
    assert client.sent["markdown_text"] == "**Hello**"
    assert "text" not in client.sent and "blocks" not in client.sent
    assert client.sent["attachments"] == [{"text": "Details"}]
    assert client.sent["metadata"]["event_type"] == "example"
    assert client.sent["parse"] == "none" and client.sent["link_names"] is False
    assert client.sent["unfurl_app_links"] is True


async def test_native_post_rejects_conflict_before_call() -> None:
    class Client:
        async def chat_postMessage(self, **kwargs):
            raise AssertionError("Slack must not be called")

    toolkit = SlackPostToolkit(
        metadata={"slack_channel_id": "C0HERE", METADATA_ENFORCE_AUDIENCE_KEY: False},
        client=Client(),
        workspaces=SlackWorkspaceClients(config_loader=lambda: {"bot_token": "test"}),
    )
    result = json.loads(await toolkit.post_message(markdown_text="Hello", text="Hello"))
    assert result["error"] == "markdown_text_conflict"
    result = json.loads(await toolkit.post_message(markdown_text="Hello", as_requester=True))
    assert result["error"] == "markdown_text_placement_invalid"


async def test_native_edit_can_clear_attachments_and_metadata() -> None:
    class Client:
        sent = None

        async def chat_update(self, **kwargs):
            self.sent = kwargs
            return {"ok": True, "ts": kwargs["ts"]}

    client = Client()
    toolkit = SlackPostToolkit(
        metadata={"slack_channel_id": "C0HERE", METADATA_ENFORCE_AUDIENCE_KEY: False},
        client=client,
        workspaces=SlackWorkspaceClients(config_loader=lambda: {"bot_token": "test"}),
    )
    result = json.loads(await toolkit.edit_message(
        message_id="1710000000.000001", markdown_text="**Updated**",
        attachments=[], metadata={}, parse="full",
    ))
    assert result["ok"]
    assert client.sent["markdown_text"] == "**Updated**"
    assert "text" not in client.sent
    assert client.sent["attachments"] == [] and client.sent["metadata"] == {}
    assert client.sent["parse"] == "full"
