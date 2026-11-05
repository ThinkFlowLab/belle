"""The operator's commands on the stand-in helper: devices, their deletion, an identity reset, and their refusals."""

import json
from pathlib import Path

import pytest

from belle_agent.platforms.matrix.operator import main

FAKE = str(Path(__file__).parent / "fake_helper.py")


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms:\n  matrix:\n    homeserver: http://syn:8008\n    user_id: '@belle:syn.example'\n"
        f"    helper_path: {FAKE}\n    recovery_key_output: {tmp_path / 'new.key'}\n"
    )
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_HELPER_LOG", str(log))
    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "pass-SECRET")
    monkeypatch.setenv("MATRIX_RECOVERY_KEY", "key-SECRET")
    for name in ("MATRIX_PASSWORD", "MATRIX_PASSWORD_FILE", "MATRIX_RECOVERY_KEY_FILE", "MATRIX_STORE_PASSPHRASE_FILE"):
        monkeypatch.delenv(name, raising=False)
    return path, log


def run(path: Path, *arguments: str) -> int:
    return main(["--config", str(path), "--state-dir", str(path.parent / "state"), *arguments])


def requests(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_devices_lists_each_device_and_marks_the_store_s_own(config, capsys):
    path, log = config
    assert run(path, "devices") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["FAKEDEVICE\t2026-10-17T22:55:30Z\tbelle\tthis store", "STALE\t-\t-"]
    assert [r["op"] for r in requests(log)] == ["start", "devices", "shutdown"]


def test_delete_devices_sends_the_password_and_refuses_without_one(config, capsys, monkeypatch):
    path, log = config
    assert run(path, "delete-devices", "STALE") == 1
    assert "MATRIX_PASSWORD" in capsys.readouterr().err
    assert not log.exists()
    monkeypatch.setenv("MATRIX_PASSWORD", "pw-SECRET")
    assert run(path, "delete-devices", "STALE") == 0
    delete = requests(log)[1]
    assert (delete["op"], delete["devices"], delete["password"]) == ("delete_devices", ["STALE"], "pw-SECRET")
    assert "pw-SECRET" not in capsys.readouterr().out


def test_reset_identity_needs_yes_then_asks_the_helper_and_names_the_new_key(config, capsys, monkeypatch, tmp_path):
    path, log = config
    monkeypatch.setenv("MATRIX_PASSWORD", "pw-SECRET")
    assert run(path, "reset-identity") == 2
    assert "--yes" in capsys.readouterr().err
    assert run(path, "reset-identity", "--yes") == 0
    assert requests(log)[0]["reset_identity"] is True
    assert str(tmp_path / "new.key") in capsys.readouterr().out


def test_a_store_in_use_is_refused_with_the_setting_named(config, capsys, monkeypatch):
    path, _ = config
    monkeypatch.setenv("MATRIX_STORE_PASSPHRASE", "locked")
    assert run(path, "devices") == 1
    assert "platforms.matrix.store_dir" in capsys.readouterr().err
