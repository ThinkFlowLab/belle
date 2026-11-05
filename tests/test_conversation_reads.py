import pytest

from belle_agent.tools.reads import DEFAULT_MESSAGES, MAX_MESSAGES, ReadWindowRefused, read_window


@pytest.mark.parametrize("count", [True, 0, -1, 1.5, "20", MAX_MESSAGES + 1])
def test_read_window_refuses_invalid_message_counts(count):
    with pytest.raises(ReadWindowRefused) as refused:
        read_window(None, count)
    assert refused.value.code == "max_messages_invalid"
    assert f"1 to {MAX_MESSAGES}" in refused.value.detail


@pytest.mark.parametrize("count", [1, MAX_MESSAGES])
def test_read_window_preserves_the_requested_message_count(count):
    assert read_window(None, count) == (None, count)


def test_read_window_uses_the_default_only_when_omitted():
    assert read_window(None, None) == (None, DEFAULT_MESSAGES)
