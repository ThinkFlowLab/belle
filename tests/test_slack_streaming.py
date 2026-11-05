"""Focused checks for Belle's Slack message surfaces."""

import logging

import pytest

from belle_agent.platforms.slack.streaming import (
    SlackStreamState,
    SlackSurfaceIO,
    _SlackStreamingSurface,
)


@pytest.mark.asyncio
async def test_native_surface_appends_only_complete_new_lines() -> None:
    calls = []

    async def stream_call(method, **kwargs):
        calls.append((method, kwargs))
        return {"ts": "1.1"}

    async def unused_post(**kwargs):
        raise AssertionError("native stream must not post an edit preview")

    async def wait_for_slot(channel_id, interval):
        assert channel_id == "C1"

    io = SlackSurfaceIO(
        post_text=unused_post,
        call_stream_method=stream_call,
        wait_for_slot=wait_for_slot,
        render_snapshot=str,
        logger=logging.getLogger(__name__),
    )
    surface = _SlackStreamingSurface(io, "C1", "1.0", recipient_user_id="U1", recipient_team_id="T1")

    assert not surface.ready_to_open("unfinished")
    assert surface.ready_to_open("first\nsecond")
    handle = await surface.open("first\nsecond")
    await surface.write(handle, "first\nsecond", 1)
    await surface.write(handle, "first\nsecond\n", 2)

    assert surface.sent_text == "first\nsecond\n"
    assert [method for method, _ in calls] == ["chat_startStream", "chat_appendStream"]
    assert calls[1][1]["chunks"] == [{"type": "markdown_text", "text": "second\n"}]


def test_stream_state_bounds_recipients_and_ignores_placeholder_team() -> None:
    state = SlackStreamState()
    state.remember_recipient("first", "U1", "T1", cap=1)
    state.remember_recipient("second", "U2", "T2", cap=1)

    assert state.recipients == {"second": ("U2", "T2")}
    assert state.recipient("slack_default_C1_1.1", {}) == ("", "")
    assert state.recipient("slack_T3_C1_U3", {}) == ("U3", "T3")
