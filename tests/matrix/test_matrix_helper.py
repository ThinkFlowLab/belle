"""The helper's lifecycle, on a stand-in process that speaks the protocol on fd 3."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from belle_agent.platforms.matrix import protocol
from belle_agent.platforms.matrix.helper import Helper, HelperError

FAKE = str(Path(__file__).parent / "fake_helper.py")


def helper(events: list | None = None, *, limit: int = 1 << 20, stop_seconds: float = 2.0) -> Helper:
    return Helper(FAKE, max_line_bytes=limit, on_event=(events if events is not None else []).append, stop_seconds=stop_seconds)


START = {"homeserver": "http://h", "user_id": "@belle:syn.example", "store_dir": "/s", "device_name": "belle",
         "store_passphrase": "pass-SECRET-1"}


async def test_the_helper_gets_its_socket_as_fd_3_and_answers_on_it(monkeypatch, tmp_path):
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_HELPER_LOG", str(log))
    events: list = []
    process = helper(events)
    hello = await process.open()
    assert hello["protocol"] == protocol.PROTOCOL
    result = await process.request("start", **START)
    assert result == {"user_id": "@belle:syn.example", "device_id": "FAKEDEVICE", "new_device": False, "backup": "enabled"}
    for _ in range(50):
        if len(events) == 3:
            break
        await asyncio.sleep(0.05)
    assert [e["event"] for e in events] == ["invite", "timeline", "timeline"]
    assert await process.request("send", room="!r:s", type="m.room.message", content={"body": "x"}, txn_id="t1") == {
        "event_id": "$sent2"
    }
    assert await process.close() == 0
    assert [json.loads(line)["op"] for line in log.read_text().splitlines()] == ["start", "send", "shutdown"]


async def test_a_helper_of_another_protocol_is_refused_naming_both(monkeypatch):
    monkeypatch.setenv("FAKE_HELPER_MODE", "badproto")
    with pytest.raises(HelperError, match=f"protocol 99 and Belle speaks {protocol.PROTOCOL}"):
        await helper().open()


async def test_a_missing_binary_is_an_error_at_open():
    with pytest.raises(FileNotFoundError):
        await Helper("/nonexistent/belle-matrix-helper", max_line_bytes=1024, on_event=print).open()


async def test_a_request_is_checked_before_it_is_sent():
    process = helper()
    await process.open()
    with pytest.raises(HelperError, match="missing txn_id") as refused:
        await process.request("send", room="!r:s", type="m.room.message", content={})
    assert refused.value.code == protocol.BAD_REQUEST
    await process.close()


async def test_a_request_above_the_line_limit_is_refused_before_it_is_sent():
    process = helper(limit=4096)
    await process.open()
    with pytest.raises(HelperError, match="above the line limit of 4096"):
        await process.request("upload", room="!r:s", mime="text/plain", data="A" * 5000)
    assert not process.exited.done()
    await process.close()


async def test_an_exit_fails_each_pending_request_with_helper_restarted(monkeypatch, caplog):
    monkeypatch.setenv("FAKE_HELPER_MODE", "crash")
    process = helper()
    await process.open()
    caplog.set_level(logging.INFO, logger="belle_agent.platforms.matrix.helper")
    with pytest.raises(HelperError) as failed:
        await process.request("send", room="!r:s", type="m.room.message", content={"body": "x"}, txn_id="t1")
    assert failed.value.code == protocol.RESTARTED
    assert await process.exited == 3
    assert "fake helper crashes on send" in caplog.text


async def test_close_escalates_to_sigkill_when_the_helper_ignores_shutdown_and_sigterm(monkeypatch, caplog):
    monkeypatch.setenv("FAKE_HELPER_MODE", "stubborn")
    process = helper(stop_seconds=0.5)
    await process.open()
    caplog.set_level(logging.WARNING, logger="belle_agent.platforms.matrix.helper")
    assert await process.close() == -9
    assert "sending SIGTERM" in caplog.text and "sending SIGKILL" in caplog.text


async def test_a_request_answered_late_still_resolves(monkeypatch):
    """The stand-in answers after a delay, as a real server does: the fake that answered at once hid a defect on Mattermost."""
    monkeypatch.setenv("FAKE_HELPER_MODE", "slow")
    monkeypatch.setenv("FAKE_HELPER_DELAY", "0.3")
    process = helper()
    await process.open()
    first, second = await asyncio.gather(process.request("join", room="!a:s"), process.request("join", room="!b:s"))
    assert first == second == {}
    await process.close()
