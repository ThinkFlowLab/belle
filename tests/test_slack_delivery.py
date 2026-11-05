"""Delivery behavior not covered by connector integration tests."""

import logging

from belle_agent.platforms.slack.delivery import short_echo_tail


def test_short_echo_tail_repairs_text_but_not_complete_blocks() -> None:
    kwargs = {
        "response": {"message": {"text": "first"}},
        "channel_id": "C1",
        "response_ts": "1.0",
        "sent_text": "first second",
        "update_ts": "",
        "logger": logging.getLogger(__name__),
    }

    assert short_echo_tail(**kwargs) == " second"
    assert short_echo_tail(**kwargs, had_blocks=True) == ""
    assert short_echo_tail(**{**kwargs, "response": {"message": {"text": "second"}}}) == ""
