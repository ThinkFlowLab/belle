"""Sharing access asks during its originating turn."""

import json

import pytest

from belle_agent.backend import VERDICT_ASK, ToolCall
from belle_agent.platforms.slack.canvases import SlackCanvasToolkit
from belle_agent.platforms.slack.lists import SlackListToolkit
from belle_agent.tools.host import BoundTurn, ToolHost


@pytest.mark.parametrize(
    ("toolkit_type", "name", "resource_arg"),
    [
        (SlackCanvasToolkit, "share_slack_canvas", "canvas_id"),
        (SlackListToolkit, "share_slack_list", "list_id"),
    ],
)
async def test_share_asks_and_expired_approval_cannot_run(toolkit_type, name, resource_arg):
    host = ToolHost([toolkit_type()])
    turn = BoundTurn("turn-1")
    host.bind("conversation", turn)
    call = ToolCall(
        conversation="conversation", name=name, call_id="call-1",
        arguments={resource_arg: "F123456789", "access_level": "read", "user_ids": ["U123456789"]},
    )

    verdict = await host.authorize(call)
    assert verdict.action == VERDICT_ASK
    assert all(value in verdict.question.text for value in ("read", "F123456789", "U123456789"))

    host.release("conversation", "turn-1")
    result = json.loads(await host.run_confirmed(call, turn))
    assert result["error"] == "turn_ended"


@pytest.mark.parametrize(
    ("toolkit_type", "name"),
    [(SlackCanvasToolkit, "share_slack_canvas"), (SlackListToolkit, "share_slack_list")],
)
async def test_invalid_share_target_does_not_ask(toolkit_type, name):
    toolkit = toolkit_type()
    assert await toolkit.widening_question(name, {"user_ids": ["bad"]}) is None
