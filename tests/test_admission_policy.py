"""Plain-facts session admission decisions."""

import pytest

from belle_agent.admission.policy import (
    disposition_holds,
    event_disposition,
    normalize_event_policy,
    selected_message_trigger,
    should_hold_event,
    should_queue_message,
)


@pytest.mark.parametrize(
    "mode,running,admitting,event_active,waiting,expected",
    [
        ("queue", False, False, False, False, False),
        ("queue", True, False, False, False, True),
        ("steer", True, False, False, False, False),
        ("steer", False, True, False, False, True),
        ("steer", True, False, True, False, True),
        ("cancel", False, False, False, True, False),
        ("queue", False, False, False, True, True),
    ],
)
def test_message_admission(mode, running, admitting, event_active, waiting, expected):
    assert should_queue_message(
        mode, turn_running=running, event_admitting=admitting,
        event_turn_active=event_active, messages_waiting=waiting,
    ) is expected


@pytest.mark.parametrize(
    "facts,expected",
    [
        ({}, False),
        ({"event_admitting": True}, True),
        ({"event_admitting": True, "reserved": True}, False),
        ({"earlier_event_admitting": True, "reserved": True}, True),
        ({"turn_running": True}, True),
        ({"awaiting_answer": True}, True),
        ({"messages_waiting": True}, True),
        ({"events_waiting": True}, True),
        ({"messages_waiting": True, "reserved": True}, False),
        ({"events_waiting": True, "reserved": True}, False),
    ],
)
def test_event_admission(facts, expected):
    defaults = dict.fromkeys(
        ("reserved", "event_admitting", "earlier_event_admitting", "turn_running",
         "awaiting_answer", "messages_waiting", "events_waiting"), False,
    )
    assert should_hold_event(**(defaults | facts)) is expected


@pytest.mark.parametrize("may_turn,expected", [(False, False), (True, True)])
def test_turn_requires_a_destination(may_turn, expected):
    assert disposition_holds("turn", may_turn=may_turn) is expected
    assert not disposition_holds("unknown", may_turn=may_turn)


def test_event_policy_normalization_uses_platform_declared_kinds():
    assert normalize_event_policy(
        {" MESSAGE ": " Turn ", "view": "turn", "unknown": "context", "closed": False},
        kinds={"message", "view", "closed"}, turn_kinds={"message"},
    ) == {"message": "turn", "closed": "off"}
    assert normalize_event_policy("context", kinds={"message"}, turn_kinds=set()) is None
    assert normalize_event_policy({}, kinds={"message"}, turn_kinds=set()) == {}


def test_core_disposition_read_still_normalizes_event_keys():
    assert event_disposition({" MESSAGE ": " CONTEXT "}, "message", may_turn=False) == "context"
    assert event_disposition({"message": "turn"}, "message", may_turn=False) == "off"


@pytest.mark.parametrize("triggers,reply,file,text,expected", [
    ({"all", "reply", "url", "has_file"}, True, True, "https://example.com", "all"),
    ({"reply", "url", "has_file"}, True, True, "https://example.com", "reply"),
    ({"reply", "url", "has_file"}, False, True, "<https://example.com|link>", "url"),
    ({"has_file"}, False, True, "", "has_file"),
    ({"reply", "url", "has_file"}, False, False, "plain text", ""),
    (set(), True, True, "https://example.com", ""),
])
def test_content_trigger_priority_uses_plain_message_facts(triggers, reply, file, text, expected):
    assert selected_message_trigger(triggers, text, reply_to_bot=reply, has_file=file) == expected
