"""Silence detection must not discard a real answer."""

import pytest

from belle_agent.silence import (
    NO_REPLY_SENTINEL,
    REPLY_OPTIONAL_KEY,
    SILENCE_UNSTORED_KEY,
    is_no_reply_sentinel,
    silence_is_unstored,
)


@pytest.mark.parametrize(
    "reply",
    [
        NO_REPLY_SENTINEL,
        "no_reply",
        "`NO_REPLY`.",
        "[ NO_REPLY ]",
        "(NO_REPLY)",
        '"NO_REPLY"',
        "'NO_REPLY'!",
        " \x00NO_REPLY\n",
        "\u200bNO_REPLY\ufeff",
        "\u2060 NO_REPLY \u2060",
        "NO_REPLY\n\x00no_reply",
    ],
)
def test_whole_token_replies_are_silent(reply: str) -> None:
    assert is_no_reply_sentinel(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "no reply",
        "NO_REPLYNO_REPLY",
        "NO_REPLY栈NO_REPLY",
        "NO_REPLY\x00actual text",
        "actual text\x00NO_REPLY",
        "The answer is 42. NO_REPLY",
        "NO_REPLY\nand one more thing",
    ],
)
def test_other_replies_remain_visible(reply: str) -> None:
    assert not is_no_reply_sentinel(reply)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: True}, True),
        ({SILENCE_UNSTORED_KEY: True}, False),
        ({REPLY_OPTIONAL_KEY: True}, False),
        ({REPLY_OPTIONAL_KEY: "true", SILENCE_UNSTORED_KEY: True}, False),
        ({REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: False}, False),
        ({REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: "true"}, False),
        ({"slack_silence_unstored": True}, False),
        ({"slack_reply_optional": True, SILENCE_UNSTORED_KEY: True}, False),
        ({}, False),
        (None, False),
        ("not metadata", False),
    ],
)
def test_unstored_silence_requires_explicit_true(metadata: object, expected: bool) -> None:
    assert silence_is_unstored(metadata) is expected
