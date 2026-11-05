"""The Matrix platform against a real server and the real helper, with the scripted backend.

``tests/conftest.py`` skips these checks unless ``MATRIX_TEST_HOMESERVER`` (the server's client API),
``MATRIX_TEST_REGISTRATION`` (a registration token of the server) and ``MATRIX_TEST_HELPER`` (the helper
binary) are set. Each check registers its own accounts with the token, under fresh names.
"""

import asyncio
import logging
import os
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import ModelTurn
from scripted_backend import ScriptedBackend

from belle_agent.config import load_config
from belle_agent.host import host_from_config
from belle_agent.platforms.matrix.helper import Helper, HelperError
from belle_agent.platforms.matrix.settings import PASSPHRASE_ENV, PASSWORD_ENV, RECOVERY_KEY_ENV

pytestmark = pytest.mark.matrix_server

URL = os.environ.get("MATRIX_TEST_HOMESERVER", "").rstrip("/")
TOKEN = os.environ.get("MATRIX_TEST_REGISTRATION", "")
HELPER = os.environ.get("MATRIX_TEST_HELPER", "")
#: How long a check waits for the bot to join or to answer.
WAIT_SECONDS = 60.0


async def register(name: str) -> tuple[str, str]:
    """Register an account with the registration token, through the user-interactive auth of the spec."""
    password = secrets.token_hex(16)
    body: dict[str, Any] = {"username": name, "password": password, "inhibit_login": True}
    async with httpx.AsyncClient(base_url=URL, timeout=30) as http:
        answer = await http.post("/_matrix/client/v3/register", json=body)
        while answer.status_code == 401:
            state = answer.json()
            flow = next(f["stages"] for f in state["flows"] if "m.login.registration_token" in f["stages"])
            stage = next(s for s in flow if s not in state.get("completed", []))
            auth = {"type": stage, "session": state["session"], **({"token": TOKEN} if stage == "m.login.registration_token" else {})}
            answer = await http.post("/_matrix/client/v3/register", json={**body, "auth": auth})
        assert answer.status_code == 200, answer.text
        return answer.json()["user_id"], password


class Person:
    """A person's client: the helper, which joins each invite and keeps each message it is handed."""

    def __init__(self, home: Path, user_id: str, password: str) -> None:
        self.home, self.user_id, self.password = home, user_id, password
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.helper: Helper | None = None
        self._pump: asyncio.Task[None] | None = None

    async def start(self) -> None:
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        key = self.home / "recovery.key"
        for _ in range(2):
            helper = Helper(HELPER, max_line_bytes=64 * 1024 * 1024, on_event=events.put_nowait)
            await helper.open()
            fields = {"homeserver": URL, "user_id": self.user_id, "store_dir": str(self.home / "store"), "device_name": "check",
                      "store_passphrase": "check", "password": self.password, "recovery_key_output": str(key)}
            try:
                await helper.request("start", **fields, **({"recovery_key": key.read_text().strip()} if key.exists() else {}))
            except HelperError as exc:
                await helper.close()
                if exc.reason == "bootstrapped":
                    continue
                raise
            self.helper = helper
            self._pump = asyncio.create_task(self._handle(events))
            return
        raise AssertionError("the person's helper did not start after its bootstrap")

    async def _handle(self, events: asyncio.Queue[dict[str, Any]]) -> None:
        assert self.helper is not None
        while True:
            event = await events.get()
            if event["event"] == "invite":
                await self.helper.request("join", room=event["room"])
            if "seq" in event:
                await self.helper.request("ack", seq=event["seq"])
            if event["event"] == "timeline" and event["room_event"].get("type") == "m.room.message":
                self.messages.put_nowait(event)

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
        if self.helper is not None:
            await self.helper.close()


async def bot_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_id: str, password: str, backend: ScriptedBackend) -> Any:
    """A host with the Matrix platform of the bot account, started: the first start makes its identity."""
    key = tmp_path / "bot-recovery.key"
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms:\n  matrix:\n    homeserver: {URL}\n    user_id: '{user_id}'\n    helper_path: {HELPER}\n"
                    f"    recovery_key_output: {key}\n    typing_indicator: false\n")
    monkeypatch.setenv(PASSPHRASE_ENV, "check")
    monkeypatch.setenv(PASSWORD_ENV, password)
    for _ in range(2):
        host = host_from_config(load_config(path), backend, state_dir=tmp_path / "bot")
        try:
            await host.start()
            return host
        except Exception as exc:
            await host.stop()
            if "a new identity was made" not in str(exc):
                raise
            monkeypatch.setenv(RECOVERY_KEY_ENV, key.read_text().strip())
    raise AssertionError("the bot did not start after its bootstrap")


async def test_an_encrypted_direct_message_is_answered_encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = uuid.uuid4().hex[:8]
    bot, bot_password = await register(f"belle-check-{run}")
    person_id, person_password = await register(f"person-check-{run}")
    backend = ScriptedBackend()
    backend.play(ModelTurn(final=f"an answer {run}"))
    host = await bot_host(tmp_path, monkeypatch, bot, bot_password, backend)
    person = Person(tmp_path / "person", person_id, person_password)
    try:
        await person.start()
        assert person.helper is not None
        room = (await person.helper.request("dm_room", user=bot, create=True))["room"]
        for _ in range(int(WAIT_SECONDS)):
            members = (await person.helper.request("members", room=room))["members"]
            if any(m["user_id"] == bot and m["membership"] == "join" for m in members):
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError(f"the bot did not join {room}: {members}")
        await person.helper.request("send", room=room, type="m.room.message", txn_id=uuid.uuid4().hex,
                                    content={"msgtype": "m.text", "body": f"hello {run}"})
        async with asyncio.timeout(WAIT_SECONDS):
            while (reply := await person.messages.get())["room_event"]["sender"] != bot:
                pass
        assert reply["encrypted"] and reply["room_event"]["content"]["body"] == f"an answer {run}"
        assert reply["room_event"]["content"]["msgtype"] == "m.notice"
    finally:
        await person.stop()
        await host.stop()


async def test_an_entry_that_logs_in_to_an_account_another_entry_serves_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from belle_agent.platforms.matrix import create_platform

    name = f"bot{uuid.uuid4().hex[:8]}"
    user_id, password = await register(name)
    monkeypatch.setenv(PASSPHRASE_ENV, "check")
    monkeypatch.setenv(PASSWORD_ENV, password)
    # The first start of the account makes its identity, and the second signs in with the recovery key it wrote.
    await (await bot_host(tmp_path, monkeypatch, user_id, password, ScriptedBackend())).stop()
    shared = {"homeserver": URL, "store_passphrase_env": PASSPHRASE_ENV, "password_env": PASSWORD_ENV,
              "recovery_key_env": RECOVERY_KEY_ENV}
    # The homeserver logs the second user id in to the same account: it matches the localpart without regard to case.
    accounts = [
        {"name": "first", "user_id": user_id, "store_dir": str(tmp_path / "bot" / "matrix" / "default"),
         "recovery_key_output": str(tmp_path / "first.key"), **shared},
        {"name": "second", "user_id": "@" + user_id[1:].replace(name, name.upper()), "store_dir": str(tmp_path / "second"),
         "recovery_key_output": str(tmp_path / "second.key"), **shared},
    ]
    platform = create_platform({"platforms": {"matrix": {"helper_path": HELPER, "accounts": accounts}}}, tmp_path)
    with caplog.at_level(logging.ERROR, logger="belle_agent.platforms.matrix.accounts"):
        await platform.start(lambda record: asyncio.sleep(0, "admitted"))
    try:
        assert list(platform.serving) == ["first"]
        # The second entry logged in, and to the first entry's account, rather than being refused at login.
        assert f"matrix account second is NOT SERVED: it logged in as {user_id}, which account first already serves" in caplog.text
        assert "its new device" in caplog.text and "is logged out and its store removed" in caplog.text
        # The second entry's new device is logged out, and its store removed: the account keeps the first device only.
        first = platform.serving["first"]
        devices = (await first._helper.request("devices"))["devices"]
        assert [device["device_id"] for device in devices] == [first.device_id]
        assert not (tmp_path / "second" / "session.json").exists()
    finally:
        await platform.stop()
