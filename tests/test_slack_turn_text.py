"""Turn context text shared by Slack hosts."""

from belle_agent.silence import NO_REPLY_INSTRUCTION
from belle_agent.platforms.slack.turn_text import (
    append_turn_advice,
    event_turn_text,
    message_turn_text,
)


def test_message_text_keeps_policy_after_user_text_and_advice():
    text = message_turn_text(
        "Please help", message_ts="2", thread_ts="1", trigger="mention",
        prompt="Room policy", buffered_events="[slack_events]", silence_instruction="Stay silent",
    )
    assert text == (
        "Please help\n\n[ts: 2, thread_ts: 1] [trigger: mention]"
        "\n\n[slack_events]\n\nRoom policy\n\nStay silent"
    )
    assert append_turn_advice(text, "Advice", suffixes=("Stay silent",)) == (
        text.removesuffix("\n\nStay silent") + "\n\nAdvice\n\nStay silent"
    )
    assert "Room policy\n\nRoom policy" not in message_turn_text(
        "Room policy", message_ts="2", thread_ts="", trigger="mention",
        prompt="Room policy", buffered_events="", silence_instruction="",
    )


def test_event_turn_text_keeps_trigger_and_silence_contract():
    text = event_turn_text(
        {"type": "reaction_added"}, anchor_ts="2", anchor_thread_ts="1",
        anchored=True, buffered_events="", prompt="Room policy",
        silence_instruction=NO_REPLY_INSTRUCTION,
    )
    assert text.startswith('[slack_event]\n{"type": "reaction_added"}')
    assert "[ts: 2, thread_ts: 1] [trigger: event]" in text
    assert "thread named above" in text
    assert text.endswith(NO_REPLY_INSTRUCTION)
