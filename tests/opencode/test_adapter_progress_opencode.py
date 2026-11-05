"""What a running turn reports, on OpenCode itself with the stand-in model: a subagent, and a reply written in deltas."""

import pytest
from standin import call

from belle_agent.backend import EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, SUBAGENT
from belle_agent.host import OUTCOME_POSTED_FINAL

pytestmark = pytest.mark.adapter


async def test_a_subagent_is_reported_by_its_type_and_its_child_session_tools_are_forwarded(world):
    child = "child task " + call("belle_react_to_message", emoji="eyes")
    message = world.direct("go " + call("subagent", agent="general", description="a child", prompt=child))

    await world.send(message)

    tools = [
        (event.kind, event.data.get("name"), event.data.get(SUBAGENT), event.data.get("call_id"))
        for _, event in world.platform.progressed
        if event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT)
    ]
    # Parent subagent call, then the child's tool under the same call_id for peek attribution.
    assert len(tools) >= 2
    assert tools[0][:3] == (EVENT_TOOL_CALLED, "subagent", "general")
    parent_call = tools[0][3]
    nested = [row for row in tools if row[1] == "react_to_message"]
    assert nested, f"expected child tool under parent call; got {tools}"
    assert all(row[2] is None and row[3] == parent_call for row in nested)
    assert tools[-1][:3] == (EVENT_TOOL_RESULT, "subagent", "general")
    assert [action.name for action in world.platform.log if action.kind == "reaction"] == ["eyes"]


async def test_the_deltas_of_a_reply_are_in_order_and_join_into_its_final(world):
    message = world.direct("SAY=the-tide-is-out-and-the-ferry-waits")

    await world.send(message)

    written = "".join(event.text for _, event in world.platform.progressed if event.kind == EVENT_TEXT_DELTA)
    assert written == "the-tide-is-out-and-the-ferry-waits"
    assert [post.text for post in world.posts()] == [written]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]
