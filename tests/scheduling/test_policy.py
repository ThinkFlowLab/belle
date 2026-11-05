"""Scheduling delivery allow-rule checks."""

from belle_agent.scheduling.address import Address
from belle_agent.scheduling.policy import allowed_by, delivery_refusal_detail, parse_delivery_rules
from belle_agent.scheduling.settings import SchedulingSettings

ORIGIN = Address("slack", "team-a", "channel-a", "")


def test_default_rule_allows_same_workspace_only() -> None:
    rules = parse_delivery_rules(SchedulingSettings().delivery)
    assert allowed_by(rules, ORIGIN, Address("slack", "team-a", "channel-b", "")) == rules[0]
    assert allowed_by(rules, ORIGIN, Address("slack", "team-b", "channel-b", "")) is None
    assert allowed_by(rules, ORIGIN, Address("matrix", "", "room", "")) is None


def test_same_is_a_list_of_fields() -> None:
    rules = parse_delivery_rules([{"from": {"workspace": "same"}, "to": {"workspace": "same", "same": ["platform"]}}])
    source = Address("slack", "same", "a", "")
    assert allowed_by(rules, source, Address("slack", "same", "b", "")) == rules[0]
    assert allowed_by(rules, source, Address("matrix", "same", "b", "")) is None


def test_refusal_names_the_nearest_rule() -> None:
    rules = parse_delivery_rules([
        {"from": {"platform": "mattermost"}, "to": {"platform": "matrix"}},
        {"from": {"platform": "slack"}, "to": {"platform": "slack", "same": ["workspace"]}},
    ])
    target = Address("slack", "team-b", "channel-b", "")
    detail = delivery_refusal_detail(rules, ORIGIN, target)
    assert "slack/team-a/channel-a" in detail and "slack/team-b/channel-b" in detail
    assert "target workspace to match the origin workspace" in detail
    assert "scheduling." not in detail


def test_empty_list_refuses_everything() -> None:
    assert allowed_by(parse_delivery_rules([]), ORIGIN, ORIGIN) is None
    assert "allows no delivery" in delivery_refusal_detail((), ORIGIN, ORIGIN)
