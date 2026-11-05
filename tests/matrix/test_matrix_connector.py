"""The platform on the stand-in helper: start and its refusals, inbound messages, acknowledgements, post."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import TRIGGER_DM, InboundMessage, PlatformUnreachable, enabled_platforms
from belle_agent.platforms.matrix.connector import MatrixPlatform, MatrixStartRefused
from belle_agent.platforms.matrix.settings import MatrixSettings, matrix_settings

FAKE = str(Path(__file__).parent / "fake_helper.py")
PASSPHRASE = "pass-SECRET-7f3a"
BELLE = "@belle:syn.example"


def platform(**fields) -> MatrixPlatform:
    settings = {"homeserver": "http://syn:8008", "user_id": BELLE, "store_dir": "/state/matrix/default",
                "store_passphrase": PASSPHRASE, "helper_path": FAKE, "recovery_key": "key-SECRET", **fields}
    return MatrixPlatform(MatrixSettings(**settings))


async def wait_for(condition, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


async def test_a_direct_message_reaches_the_host_then_is_acknowledged_and_the_invite_joined(monkeypatch, tmp_path, caplog):
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_HELPER_LOG", str(log))
    caplog.set_level(logging.DEBUG)
    received: list[InboundMessage] = []

    async def on_inbound(record):
        # Acknowledgement waits for the host: nothing is acked while this runs.
        assert not any(json.loads(line).get("seq") == 2 for line in log.read_text().splitlines())
        received.append(record)
        return "admitted"

    matrix = platform()
    await matrix.start(on_inbound)
    await wait_for(lambda: sum('"ack"' in line for line in log.read_text().splitlines()) == 3)
    await matrix.stop()
    [message] = received
    assert message == InboundMessage(platform="matrix", workspace="default", conversation="!dm:syn.example",
                                     message_id="$hello", event_id="$hello", sender="@alice:syn.example",
                                     text="hello belle", chat_type="direct", encrypted=True, trigger=TRIGGER_DM, reply_thread="",
                                     envelope="[chat_id: !dm:syn.example, message_id: $hello]")
    ops = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(r["op"], r.get("room") or r.get("seq")) for r in ops[1:]] == [
        ("join", "!dm:syn.example"), ("ack", 1), ("room_info", "!dm:syn.example"), ("ack", 2), ("ack", 3), ("shutdown", None)]
    assert PASSPHRASE not in caplog.text and "key-SECRET" not in caplog.text


@pytest.mark.parametrize(
    ("fields", "named"),
    [({"store_passphrase": "wrong"}, "MATRIX_STORE_PASSPHRASE"), ({"recovery_key": "", "password": "pw"}, "MATRIX_RECOVERY_KEY_FILE")],
)
async def test_a_refused_start_names_what_to_set_and_stops_the_helper(fields, named):
    matrix = platform(**fields)
    # The launcher stops with the message of a configuration error, and no traceback.
    with pytest.raises(BelleConfigError, match=named):
        await matrix.start(lambda record: asyncio.sleep(0))


async def test_a_homeserver_that_does_not_answer_fails_the_start_and_a_later_start_connects(monkeypatch):
    monkeypatch.setenv("FAKE_HELPER_MODE", "unreachable")
    matrix = platform()
    # Not a refusal: nothing is to fix in the configuration, and the helper is closed so that a start can come again.
    with pytest.raises(PlatformUnreachable, match="the Matrix homeserver did not answer: error sending request") as raised:
        await matrix.start(lambda record: asyncio.sleep(0))
    assert not isinstance(raised.value, MatrixStartRefused) and matrix._helper is None

    monkeypatch.setenv("FAKE_HELPER_MODE", "normal")
    await matrix.start(lambda record: asyncio.sleep(0))
    assert matrix._helper is not None
    await matrix.stop()


async def test_an_event_the_host_cannot_take_is_logged_and_acknowledged(monkeypatch, tmp_path, caplog):
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_HELPER_LOG", str(log))

    async def refuse(record):
        raise RuntimeError("the host refused")

    matrix = platform()
    await matrix.start(refuse)
    await wait_for(lambda: sum('"ack"' in line for line in log.read_text().splitlines()) == 3)
    await matrix.stop()
    assert "matrix timeline event seq=2 raised" in caplog.text


async def test_a_missing_helper_names_the_setting():
    with pytest.raises(MatrixStartRefused, match="platforms.matrix.helper_path names /nonexistent"):
        await platform(helper_path="/nonexistent").start(lambda record: asyncio.sleep(0))


async def test_a_rolled_back_store_stops_the_platform(monkeypatch, caplog):
    monkeypatch.setenv("FAKE_HELPER_MODE", "alert")
    matrix = platform()
    await matrix.start(lambda record: asyncio.sleep(0))
    await wait_for(lambda: matrix._helper is None)
    assert "matrix stops: duplicate one-time key" in caplog.text


async def test_post_sends_a_notice_in_its_thread_and_refuses_a_private_message(monkeypatch, tmp_path):
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_HELPER_LOG", str(log))
    matrix = platform()
    await matrix.start(lambda record: asyncio.sleep(0, "admitted"))
    assert (await matrix.post("!dm:syn.example", "$root", "an answer")).startswith("$sent")
    with pytest.raises(Exception, match="ephemeral_unsupported"):
        await matrix.post("!dm:syn.example", "", "x", ephemeral_to="@alice:syn.example")
    await matrix.stop()
    [send] = [r for r in map(json.loads, log.read_text().splitlines()) if r["op"] == "send"]
    assert send["content"] == {"msgtype": "m.notice", "body": "an answer", "format": "org.matrix.custom.html",
                               "formatted_body": "<p>an answer</p>", "m.mentions": {}, "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}


def test_settings_read_secrets_from_the_environment_or_a_file_never_both(monkeypatch, tmp_path):
    section = {"platforms": {"matrix": {"homeserver": "http://syn:8008", "user_id": BELLE}}}
    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "p")
    key = tmp_path / "recovery.key"
    key.write_text("the-key\n")
    monkeypatch.setenv("MATRIX_RECOVERY_KEY_FILE", str(key))
    settings = matrix_settings(section, tmp_path)
    assert (settings.recovery_key, settings.store_dir) == ("the-key", str(tmp_path / "matrix" / "default"))
    assert "the-key" not in repr(settings) and "'p'" not in repr(settings)
    monkeypatch.setenv("MATRIX_RECOVERY_KEY", "other")
    with pytest.raises(BelleConfigError, match="set MATRIX_RECOVERY_KEY or MATRIX_RECOVERY_KEY_FILE, not both"):
        matrix_settings(section, tmp_path)


@pytest.mark.parametrize(
    ("section", "named"),
    [
        ({"user_id": BELLE}, "platforms.matrix.homeserver is not set"),
        ({"homeserver": "syn:8008", "user_id": BELLE}, "must be an http or https URL"),
        ({"homeserver": "http://s", "user_id": "belle"}, "must be a full user id"),
        ({"homeserver": "http://s", "user_id": BELLE, "token": "x"}, "unknown keys: token"),
        ({"homeserver": "http://s", "user_id": BELLE, "store_dir": "/s", "recovery_key_output": "/s/key"}, "outside store_dir"),
        ({"homeserver": "http://s", "user_id": BELLE, "failed_emoji": "boom"}, "failed_emoji must be a shortcode of the table GLYPH_SHORTCODES"),
        ({"homeserver": "http://s", "user_id": BELLE, "catch_up_seconds": -1}, "catch_up_seconds must be a whole number"),
        ({"homeserver": "http://s", "user_id": BELLE, "bot_user_ids": ["relay"]}, "bot_user_ids must list full user ids"),
        ({"homeserver": "http://s", "user_id": BELLE, "typing_indicator": "yes"}, "typing_indicator must be true or false"),
        ({"homeserver": "http://s", "user_id": BELLE, "file_download_max_bytes": 0}, "file_download_max_bytes must be a positive integer"),
        ({"homeserver": "http://s", "user_id": BELLE, "file_upload_max_bytes": True}, "file_upload_max_bytes must be a positive integer"),
        ({"homeserver": "http://s", "user_id": BELLE, "membership_history_max_events": 0}, "membership_history_max_events must be a positive integer"),
    ],
)
def test_settings_refuse_a_value_that_does_not_fit_by_name(monkeypatch, tmp_path, section, named):
    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "p")
    with pytest.raises(BelleConfigError, match=named):
        matrix_settings({"platforms": {"matrix": section}}, tmp_path)


def test_the_section_turns_on_when_its_credentials_resolve(monkeypatch):
    config = {"platforms": {"matrix": {"homeserver": "http://s", "user_id": BELLE}}}
    monkeypatch.delenv("MATRIX_STORE_PASSPHRASE", raising=False)
    with pytest.raises(BelleConfigError, match="MATRIX_STORE_PASSPHRASE is not set"):
        enabled_platforms(config)
    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "p")
    assert enabled_platforms(config) == ["matrix"]


def test_the_store_defaults_to_the_state_directory_the_host_is_given(monkeypatch, tmp_path):
    from belle_agent.host import host_from_config

    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "p")
    monkeypatch.setenv("BELLE_AGENT_STATE_DIR", str(tmp_path / "elsewhere"))
    config = {"platforms": {"matrix": {"homeserver": "http://s", "user_id": BELLE}}}
    host = host_from_config(config, object(), state_dir=tmp_path / "given")
    assert host.platforms["matrix"]._settings.store_dir == str(tmp_path / "given" / "matrix" / "default")


def test_mark_emoji_take_a_shortcode_an_emoji_character_or_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "p")
    section = {"homeserver": "http://s", "user_id": BELLE, "acknowledgement_emoji": "\N{THUMBS UP SIGN}", "failed_emoji": "disabled"}
    emoji = matrix_settings({"platforms": {"matrix": section}}, tmp_path).emoji
    assert (emoji["acknowledgement_emoji"], emoji["failed_emoji"], emoji["queued_emoji"]) == (
        "\N{THUMBS UP SIGN}", "", "\N{HOURGLASS WITH FLOWING SAND}")


def test_the_example_configuration_lists_every_key_of_the_section(monkeypatch):
    from belle_agent.config import load_config
    from belle_agent.platforms.matrix import settings

    example = Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"
    monkeypatch.setenv("MATRIX_HOMESERVER", "http://s")
    monkeypatch.setenv("MATRIX_USER_ID", BELLE)
    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "p")
    config = load_config(example)
    assert set(config["platforms"]["matrix"]) == settings._KEYS
    assert matrix_settings(config, Path("/state")).store_dir == "/state/matrix/default"
