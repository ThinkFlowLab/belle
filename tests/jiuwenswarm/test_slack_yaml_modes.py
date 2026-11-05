"""YAML 1.1 converts bare ``off`` to a boolean before Belle reads it."""

import yaml

from belle_agent.integrations.jiuwenswarm.scope_capabilities import _check_events
from belle_agent.platforms.slack.events import normalize_event_policy
from belle_agent.platforms.slack.scoped_config import _slack_layer0_triggers


def test_bare_off_disables_group_messages() -> None:
    value = yaml.safe_load("group_chat_mode: off")["group_chat_mode"]
    assert _slack_layer0_triggers(value) == frozenset()


def test_bare_off_does_not_discard_other_event_dispositions() -> None:
    events = yaml.safe_load("reaction: turn\nmember: off")
    assert _check_events(events) is None
    assert normalize_event_policy(events) == {"reaction": "turn", "member": "off"}
