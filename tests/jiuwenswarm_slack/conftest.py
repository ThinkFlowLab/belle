"""Shared Slack test fixtures."""

import asyncio
import logging

import pytest


@pytest.fixture
def cron_hooks(monkeypatch):
    from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

    from belle_agent.integrations.jiuwenswarm.extensions.agent_plugin import extension
    from jiuwenswarm.extensions.registry import ExtensionRegistry

    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    asyncio.run(extension.register_extensions(registry))
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)


@pytest.fixture
def isolated_slack_dedup_store(tmp_path, monkeypatch):
    from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect

    monkeypatch.setattr(
        slack_connect,
        "get_slack_dedup_path",
        lambda: tmp_path / "slack_seen_events.json",
    )


@pytest.fixture
def slack_logs(caplog):
    """Capture connector records even when its logger does not propagate."""
    from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect

    from belle_agent.platforms.slack import config as slack_config
    from belle_agent.platforms.slack import reply_delivery, thread_status

    loggers = (
        slack_connect.logger,
        slack_config.logger,
        reply_delivery.logger,
        thread_status.logger,
    )
    state = [(logger.level, logger.propagate) for logger in loggers]
    for logger in loggers:
        logger.addHandler(caplog.handler)
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=logger.name)
    try:
        yield caplog
    finally:
        for logger, (level, propagate) in zip(loggers, state):
            logger.removeHandler(caplog.handler)
            logger.propagate = propagate
            logger.setLevel(level)
