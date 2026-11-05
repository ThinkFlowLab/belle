"""Shared cursor validation keeps each tool boundary's contract."""

import pytest

from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.tool_support import decode_cursor, encode_cursor


@pytest.mark.parametrize("raw", ["", "=", "a", "_w", "bnVsbA", "e30", "WzFd", 1])
def test_cursor_refuses_invalid_encoding_or_shape(raw: object) -> None:
    with pytest.raises(Refused) as error:
        decode_cursor(raw, form="cursor source", count=2)
    assert error.value.code == "invalid_arguments"
    assert error.value.detail == "cursor source"


def test_cursor_limit_is_selected_by_the_tool() -> None:
    values = ["x" * 256, "ses-1234567890"]
    cursor = encode_cursor(*values)
    assert decode_cursor(cursor, form="schedule cursor", count=2) == values
    with pytest.raises(Refused) as error:
        decode_cursor(cursor, form="session cursor", count=2, max_chars=256)
    assert error.value.detail == "session cursor"
