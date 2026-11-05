"""Generic event buffering with caller-defined families."""

from belle_agent.admission.buffer import DROP_OVERFLOW, InboundEventBuffer


def test_caller_controls_families_and_limits():
    buffer = InboundEventBuffer(
        limits={"update": 2, "join": 0}, ttl_seconds=60, now=lambda: 1.0,
    )
    buffer.record("chat", "other", {"n": 0})
    buffer.record("chat", "join", {"n": 0})
    for index in range(3):
        buffer.record("chat", "update", {"n": index})

    assert buffer.fold("chat") == {
        "chat_id": "chat",
        "events": [{"n": 1}, {"n": 2}],
        "dropped": {"update": {DROP_OVERFLOW: 1}},
    }
