"""The launcher starts a host from one configuration file, fails by naming the cause, and stops cleanly."""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import pytest
from scripted_backend import ScriptedBackend

from belle_agent import launcher
from belle_agent.backend import BackendError
from belle_agent.config import BelleConfigError, load_config
from belle_agent.host import host_from_config
from belle_agent.platforms.loopback.control import request
from belle_agent.scopes import capabilities as scope_capabilities

LOOPBACK = "platforms: {loopback: {enabled: true}}\nbackend: opencode\nbackends: {opencode: {url: 'http://opencode.test:4096'}}\n"
SLACK = LOOPBACK.replace("{loopback: {enabled: true}}", "{slack: {enforce_audience: true}}")
SLACK_EXAMPLE = Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"
#: The built-in OpenCode tools that reach the server's secrets, as measured against a real provider.


@pytest.fixture
def slack(monkeypatch):
    """Slack as the fake transport, and synthetic tokens in the environment."""
    from slack_kit import SlackKit

    # A host registers its platform's scope capabilities for the process. What was there is put back.
    registry = scope_capabilities.snapshot_registry()
    yield SlackKit(monkeypatch)
    scope_capabilities.restore_registry(registry)


def _config(tmp_path, text: str = LOOPBACK) -> dict:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_the_backend_key_selects_a_backend_and_its_password_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "from-the-environment")

    backend = launcher.create_backend(_config(tmp_path), tmp_path)

    assert backend.capabilities().backend == "opencode"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("platforms: {loopback: {enabled: true}}\n", "backend must name a backend"),
        ("platforms: {loopback: {enabled: true}}\nbackend: nosuch\n", "backend=nosuch names no backend"),
        ("platforms: {loopback: {enabled: true}}\nbackend: opencode\n", "opencode must be a mapping"),
        (LOOPBACK, "environment variable OPENCODE_SERVER_PASSWORD, which is not set"),
        (LOOPBACK.replace("{url:", "{password: hunter2, url:"), "a secret is never written in the configuration"),
        (LOOPBACK.replace("{url:", "{relay: {token: t}, url:"), "a secret is never written in the configuration"),
        (LOOPBACK.replace("http://", "http://opencode:hunter2@"), "url holds credentials"),
        (LOOPBACK.replace("{url:", "{addres: x, url:"), "unknown keys: addres"),
        (LOOPBACK.replace("http://opencode.test:4096", "${UNSET_OPENCODE_URL}"), "url must be the http address"),
    ],
)
def test_a_configuration_that_cannot_start_a_backend_says_why(tmp_path, monkeypatch, text, message):
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    if "password" in text or "token" in text or "@" in text or "addres" in text:
        monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "set")

    with pytest.raises(BelleConfigError, match=message) as raised:
        launcher.create_backend(_config(tmp_path, text), tmp_path)
    assert "hunter2" not in str(raised.value)


def test_a_second_brain_enabled_by_default_requires_its_backend(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setitem(sys.modules, "belle_agent.second_brain.default_backend", None)

    with pytest.raises(BelleConfigError, match="second brain backend cannot be loaded"):
        launcher.second_brain_backend(config)
    assert launcher.second_brain_backend(_config(tmp_path, LOOPBACK + "second_brain: {enabled: false}\n")) is None


@pytest.mark.parametrize("backend", ["opencode", "docker_agent"])
def test_external_harnesses_use_the_shared_second_brain_backend(backend):
    from belle_agent.second_brain.default_backend import SECOND_BRAIN_BACKEND

    assert launcher.second_brain_backend({"backend": backend, "second_brain": {"enabled": True}}) is SECOND_BRAIN_BACKEND


def test_main_reports_a_start_that_fails_in_one_line_and_exits_nonzero(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(LOOPBACK, encoding="utf-8")
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)

    with caplog.at_level(logging.CRITICAL):
        status = launcher.main(["--config", str(path), "--state-dir", str(tmp_path)])

    assert status == 1
    (record,) = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert "belle cannot start" in record.getMessage() and "OPENCODE_SERVER_PASSWORD" in record.getMessage()
    assert record.exc_info is None


class _Unready(ScriptedBackend):
    async def start(self, tools) -> None:
        raise BackendError("the backend is unreachable")

    async def close(self) -> None:
        self.closed = True


async def test_a_backend_that_cannot_start_stops_the_launcher_and_is_closed(tmp_path, monkeypatch):
    backend = _Unready()
    monkeypatch.setattr(launcher, "create_backend", lambda config, state_dir: backend)

    with pytest.raises(BackendError, match="the backend is unreachable"):
        await launcher.serve(_config(tmp_path), tmp_path)
    assert backend.closed


async def test_the_launcher_serves_until_it_is_told_to_stop_and_then_closes_everything(tmp_path, monkeypatch):
    control = tmp_path / "control.sock"
    backend = ScriptedBackend()
    closed = []
    monkeypatch.setattr(backend, "close", lambda: closed.append(True) or asyncio.sleep(0))
    monkeypatch.setattr(launcher, "create_backend", lambda config, state_dir: backend)
    serving = asyncio.create_task(launcher.serve(_config(tmp_path, LOOPBACK.replace("{enabled: true}", f"{{enabled: true, control: {control}}}")), tmp_path))
    async with asyncio.timeout(5):
        while not control.exists():
            await asyncio.sleep(0.01)

    # Driven by hand, as the command does it: a direct message in, and its answer in the log.
    message = {"conversation": "D-U1", "sender": "U1", "text": "hello", "chat_type": "dm", "trigger": "dm"}
    outcome = await asyncio.to_thread(request, str(control), {"inject": message})
    assert outcome["outcome"].startswith("dispatched:dm")
    async with asyncio.timeout(5):
        while not (log := (await asyncio.to_thread(request, str(control), {"log": True}))["log"]):
            await asyncio.sleep(0.01)
    assert [(action["kind"], action["conversation"]) for action in log] == [("post", "D-U1")]

    os.kill(os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(5):
        await serving

    assert closed == [True]
    assert not control.exists()


def test_main_refuses_to_start_on_slack_without_its_tokens_and_names_the_variable(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(SLACK, encoding="utf-8")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "from-the-environment")
    for variable in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    with caplog.at_level(logging.CRITICAL):
        status = launcher.main(["--config", str(path), "--state-dir", str(tmp_path)])

    assert status == 1
    (record,) = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert "belle cannot start" in record.getMessage() and "SLACK_BOT_TOKEN, SLACK_APP_TOKEN are not set" in record.getMessage()


def test_main_refuses_a_slack_token_of_the_wrong_kind_without_showing_it(tmp_path, monkeypatch, caplog, slack):
    path = tmp_path / "config.yaml"
    path.write_text(SLACK.replace("{enforce_audience: true}", "{enforce_audience: true, bot_token: '${BELLE_TEST_BOT_TOKEN}'}"), encoding="utf-8")
    monkeypatch.setenv("BELLE_TEST_BOT_TOKEN", "xapp-99-in-the-file")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "from-the-environment")

    with caplog.at_level(logging.CRITICAL):
        status = launcher.main(["--config", str(path), "--state-dir", str(tmp_path)])

    assert status == 1
    message = caplog.records[-1].getMessage()
    assert "Belle config platforms.slack.bot_token does not hold a token" in message and "xapp-99" not in message


async def test_the_launcher_serves_on_slack_until_it_is_told_to_stop(tmp_path, monkeypatch, slack):
    backend = ScriptedBackend()
    monkeypatch.setattr(launcher, "create_backend", lambda config, state_dir: backend)
    serving = asyncio.create_task(launcher.serve(_config(tmp_path, SLACK), tmp_path))
    async with asyncio.timeout(5):
        while not (slack.sockets and slack.sockets[-1].connected):
            await asyncio.sleep(0.01)
    socket = slack.sockets[-1]

    # Driven as Slack drives it: a direct message in, and its answer posted through the web client.
    message = {"type": "message", "channel": "D0DIRECT01", "channel_type": "im", "user": "U0ASKER001", "text": "hello", "ts": "1710000001.000100"}
    assert (await socket.app.deliver(message)).startswith("dispatched:dm")
    async with asyncio.timeout(5):
        while not socket.web.payloads("chat_postMessage"):
            await asyncio.sleep(0.01)
    assert socket.web.payload("chat_postMessage")["channel"] == "D0DIRECT01"

    os.kill(os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(5):
        await serving

    assert backend.closed and socket.closed


def test_the_slack_example_sets_nothing_this_backend_ignores(tmp_path, monkeypatch, caplog, slack):
    for variable, value in (
        ("BELLE_BACKEND", "opencode"),
        ("OPENCODE_SERVER_PASSWORD", "from-the-environment"),
        ("OPENCODE_URL", "http://opencode.test:4096"),
        ("BELLE_TOOLS_URL", "http://belle.test:18300/mcp"),
    ):
        monkeypatch.setenv(variable, value)
    config = load_config(SLACK_EXAMPLE)
    backend = launcher.create_backend(config, tmp_path)

    with caplog.at_level(logging.WARNING):
        host = host_from_config(config, backend, state_dir=tmp_path)
        host._warn_about_unapplied_settings(backend.capabilities())

    assert host.platform.name == "slack"
    assert not [record.getMessage() for record in caplog.records], "the shipped example must start without a warning"



async def test_one_launcher_serves_two_platforms_and_names_both(tmp_path, monkeypatch, slack, caplog):
    control = tmp_path / "control.sock"
    backend = ScriptedBackend()
    monkeypatch.setattr(launcher, "create_backend", lambda config, state_dir: backend)
    both = LOOPBACK.replace("{loopback: {enabled: true}}", f"{{loopback: {{enabled: true, control: {control}}}, slack: {{enforce_audience: true}}}}")
    with caplog.at_level(logging.INFO, logger="belle_agent.launcher"):
        serving = asyncio.create_task(launcher.serve(_config(tmp_path, both), tmp_path))
        async with asyncio.timeout(5):
            while not (control.exists() and slack.sockets and slack.sockets[-1].connected):
                await asyncio.sleep(0.01)
        socket = slack.sockets[-1]

        message = {"conversation": "D-U1", "sender": "U1", "text": "hello", "chat_type": "dm", "trigger": "dm"}
        assert (await asyncio.to_thread(request, str(control), {"inject": message}))["outcome"].startswith("dispatched:dm")
        direct = {"type": "message", "channel": "D0DIRECT01", "channel_type": "im", "user": "U0ASKER001", "text": "hello", "ts": "1710000001.000100"}
        assert (await socket.app.deliver(direct)).startswith("dispatched:dm")
        async with asyncio.timeout(5):
            while not (socket.web.payloads("chat_postMessage") and (await asyncio.to_thread(request, str(control), {"log": True}))["log"]):
                await asyncio.sleep(0.01)

        os.kill(os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(5):
            await serving

    assert "belle is serving: platform=loopback,slack backend=opencode" in caplog.text
    assert socket.web.payload("chat_postMessage")["channel"] == "D0DIRECT01"
    assert backend.closed and socket.closed and not control.exists()
