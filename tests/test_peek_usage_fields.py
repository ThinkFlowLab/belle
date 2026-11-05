"""Normalize EVENT_USAGE payloads for peek frames and Activity work rows."""

from belle_agent.progress import peek_usage_fields


def test_peek_usage_fields_reads_tokens_and_cost():
    assert peek_usage_fields({"tokens": {"input": 12, "output": 3}, "cost": 0.5}) == (12, 3, 0.5)


def test_peek_usage_fields_ignores_invalid_or_missing():
    assert peek_usage_fields({}) == (None, None, None)
    assert peek_usage_fields({"tokens": {"input": -1, "output": True}, "cost": -0.1}) == (None, None, None)
    assert peek_usage_fields({"tokens": "nope", "cost": "x"}) == (None, None, None)
    assert peek_usage_fields({"tokens": {"input": 1}, "cost": 2}) == (1, None, 2.0)
