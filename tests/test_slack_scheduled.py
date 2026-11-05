"""Slack's pending-message queue keeps audience and turn boundaries."""

import json
import time

from belle_agent.backend import VERDICT_ASK, ToolCall
from belle_agent.platforms.slack.history import SlackWorkspaceClients
from belle_agent.platforms.slack.history_policy import METADATA_ENFORCE_AUDIENCE_KEY, METADATA_TEAM_KEY
from belle_agent.platforms.slack.scheduled import SlackScheduledToolkit
from belle_agent.tools.host import BoundTurn, ToolHost


class Client:
    def __init__(self):
        self.calls = []

    async def chat_scheduleMessage(self, **kwargs):
        self.calls.append(("schedule", kwargs))
        return {"ok": True, "channel": kwargs["channel"], "scheduled_message_id": "Q123", "post_at": kwargs["post_at"]}

    async def chat_scheduledMessages_list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"ok": True, "scheduled_messages": [{"id": "Q123", "channel_id": kwargs["channel"], "text": "later"}],
                "response_metadata": {"next_cursor": "next" if not kwargs.get("cursor") else ""}}

    async def chat_deleteScheduledMessage(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"ok": True}


def _toolkit(client=None, *, team_id=""):
    client = client or Client()
    toolkit = SlackScheduledToolkit(
        metadata={"slack_channel_id": "C0HERE", METADATA_TEAM_KEY: team_id,
                  METADATA_ENFORCE_AUDIENCE_KEY: False},
        client=client,
        workspaces=SlackWorkspaceClients(config_loader=lambda: {"bot_token": "test"}),
    )
    return toolkit, client


async def test_schedule_returns_slack_queue_id_and_never_sends_metadata():
    toolkit, client = _toolkit()
    post_at = int(time.time()) + 120

    result = json.loads(await toolkit.schedule_slack_message(
        post_at, text="Later", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "Later"}}],
    ))

    assert result == {"ok": True, "chat_id": "C0HERE", "scheduled_message_id": "Q123", "post_at": post_at}
    assert client.calls[0][1]["channel"] == "C0HERE"
    assert "metadata" not in client.calls[0][1]


async def test_schedule_rejects_expired_time_and_content_conflict_before_api_call():
    toolkit, client = _toolkit()
    expired = json.loads(await toolkit.schedule_slack_message(int(time.time()) - 1, text="No"))
    conflict = json.loads(await toolkit.schedule_slack_message(
        int(time.time()) + 120, text="No", markdown_text="**No**",
    ))
    assert expired["error"] == "post_at_out_of_range"
    assert conflict["error"] == "markdown_text_conflict"
    assert client.calls == []


async def test_list_has_cursor_coverage_and_cancel_uses_slack_id():
    toolkit, client = _toolkit()
    first = json.loads(await toolkit.list_slack_scheduled_messages(limit=1))
    second = json.loads(await toolkit.list_slack_scheduled_messages(cursor=first["coverage"]["next_cursor"]))
    cancelled = json.loads(await toolkit.cancel_slack_scheduled_message("Q123"))

    assert first["coverage"] == {"complete": False, "next_cursor": "next"}
    assert second["coverage"] == {"complete": True, "next_cursor": ""}
    assert cancelled["cancelled"] is True
    assert client.calls[-1] == ("cancel", {"channel": "C0HERE", "scheduled_message_id": "Q123"})


async def test_list_uses_trusted_team_context_for_org_tokens():
    toolkit, client = _toolkit(team_id="T0HERE")
    assert json.loads(await toolkit.list_slack_scheduled_messages())["ok"]
    assert client.calls[0] == ("list", {"channel": "C0HERE", "team_id": "T0HERE"})


async def test_cancel_asks_and_an_expired_turn_cannot_cancel():
    toolkit, client = _toolkit()
    assert await toolkit.widening_question("cancel_slack_scheduled_message", {
        "scheduled_message_id": "Q123",
    }) == "Cancel scheduled Slack message Q123 in conversation C0HERE?"
    host = ToolHost([toolkit])
    turn = BoundTurn("turn-1")
    host.bind("room", turn)
    call = ToolCall(conversation="room", name="cancel_slack_scheduled_message", call_id="call-1",
                    arguments={"scheduled_message_id": "Q123"})

    assert (await host.authorize(call)).action == VERDICT_ASK
    host.release("room", "turn-1")
    assert json.loads(await host.run_confirmed(call, turn))["error"] == "turn_ended"
    assert client.calls == []
