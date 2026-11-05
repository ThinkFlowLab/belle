"""Operator questions preserve defaults and guard dependent rules."""

import ast
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import jiuwenswarm

from belle_agent.platforms.slack import decision_questions as dq


def load(tmp_path, value):
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return dq.load_questions(path)


@pytest.fixture
def connector(tmp_path, monkeypatch):
    from belle_agent import typed_decision
    from jiuwenswarm.common import config
    from belle_agent.integrations.jiuwenswarm import slack_channel as sc
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(typed_decision, "build_client", lambda _: AsyncMock(name="client"))
    original = sc.SlackEventDedupStore.__init__
    monkeypatch.setattr(sc.SlackEventDedupStore, "__init__", lambda self, path=None, **kw: original(self, tmp_path / "dedup.json", **kw))
    return sc.SlackChannel(sc.SlackChannelConfig(decision_questions_file=str(tmp_path / "questions.json")), RobotMessageRouter())


def test_connector_loads_relative_path_once(connector, tmp_path):
    load(tmp_path, {"claim": None})
    gate = connector._shadow_decision_gate()
    assert "claim" not in gate._questions
    load(tmp_path, {})
    assert connector._shadow_decision_gate() is gate
    assert "claim" not in gate._questions


def test_connector_logs_effective_question_changes_once(connector, tmp_path, caplog):
    from belle_agent.integrations.jiuwenswarm import slack_decisions

    changed_claim = dq.QUESTIONS["claim"].as_request_value()
    changed_claim["instructions"] = "PRIVATE INSTRUCTION"
    load(tmp_path, {
        "audience": dq.QUESTIONS["audience"].as_request_value(),
        "claim": changed_claim,
        "deserves_emoji": None,
        "custom\nquestion": {"type": "noul", "instructions": "PRIVATE INSTRUCTION"},
    })
    with caplog.at_level(logging.INFO, logger=slack_decisions.logger.name):
        gate = connector._shadow_decision_gate()
        assert connector._shadow_decision_gate() is gate
    messages = [
        record.getMessage() for record in caplog.records
        if record.name == slack_decisions.logger.name and "decision question " in record.getMessage()
    ]
    assert messages == [
        '[SlackChannel] decision question replaced: "claim"',
        '[SlackChannel] decision question added: "custom\\nquestion"',
        '[SlackChannel] decision question disabled: "deserves_emoji"',
    ]
    assert all("PRIVATE INSTRUCTION" not in message for message in messages)


@pytest.mark.parametrize("contents", ["invalid SECRET", "disable_all"])
def test_invalid_or_empty_config_disables_only_gate(connector, tmp_path, caplog, contents):
    if contents == "disable_all":
        load(tmp_path, dict.fromkeys(dq.QUESTIONS))
    else:
        (tmp_path / "questions.json").write_text(contents)
    with caplog.at_level(logging.INFO):
        assert connector._shadow_decision_gate() is None
        assert connector._shadow_decision_gate() is None
    assert connector._decision_gate is False
    assert "decision disabled" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("raw,expected", [
    (None, True), (False, False), (True, True),
    ("true", True), ("false", True), (1, True),
])
def test_forward_setting_requires_a_boolean(raw, expected, caplog):
    from belle_agent.platforms.slack.config import resolve_decision_forward

    assert resolve_decision_forward({"decision_forward": raw}) is expected
    from belle_agent.integrations.jiuwenswarm.slack_channel import (
        SlackChannelConfig,
    )

    assert SlackChannelConfig(decision_forward=expected).decision_forward is expected
    if raw is not None and not isinstance(raw, bool):
        assert "must be a boolean" in caplog.text


def test_forward_setting_defaults_on():
    from belle_agent.platforms.slack.channel_config import SlackChannelConfig
    from belle_agent.platforms.slack.config import resolve_decision_forward

    assert resolve_decision_forward({}) is True
    assert SlackChannelConfig().decision_forward is True


def test_gateway_passes_forward_setting_to_slack_connector():
    """The gateway builds SlackChannelConfig explicitly; guard this seam."""
    from belle_agent.integrations.jiuwenswarm import slack_gateway

    gateway = Path(slack_gateway.__file__)
    tree = ast.parse(gateway.read_text(encoding="utf-8"))
    constructors = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SlackChannelConfig"
    ]
    assert len(constructors) == 1
    configured = {
        keyword.arg: keyword.value for keyword in constructors[0].keywords
    }
    forwarding = configured["decision_forward"]
    assert isinstance(forwarding, ast.Call)
    assert isinstance(forwarding.func, ast.Name)
    assert forwarding.func.id == "resolve_decision_forward"


async def test_start_loads_questions_before_sdk_and_ignores_later_edits(connector, tmp_path, monkeypatch):
    from belle_agent.integrations.jiuwenswarm import slack_channel as sc

    class StopBeforeNetwork(Exception):
        pass

    connector.config.enabled = True
    connector.config.bot_token = "test-token"
    connector.config.app_token = "test-app-token"
    load(tmp_path, {"claim": None})
    def stop(**kwargs):
        assert isinstance(connector._decision_gate, dq.ShadowDecisionGate)
        assert "claim" not in connector._decision_gate._questions
        raise StopBeforeNetwork

    monkeypatch.setattr(sc, "AsyncApp", stop)
    with pytest.raises(StopBeforeNetwork):
        await connector.start()
    load(tmp_path, {})
    assert "claim" not in connector._shadow_decision_gate()._questions
