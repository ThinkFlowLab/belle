"""The Matrix platform under the host: what a person sees when the queue is full, and a helper that keeps exiting."""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import ModelTurn
from matrix_kit import MatrixKit
from peer import Room
from scripted_backend import ScriptedBackend

from belle_agent import launcher
from belle_agent.config import load_config
from belle_agent.host import MAX_QUEUED_PER_KEY, host_from_config
from belle_agent.platforms.base import TRIGGER_DM, InboundMessage
from belle_agent.platforms.matrix import connector
from belle_agent.platforms.matrix.settings import PASSPHRASE_ENV

DM = "!dm:syn.example"
FAKE = str(Path(__file__).parent / "fake_helper.py")


def dm(number: int) -> dict:
    return {"event": "timeline", "seq": number, "room": DM, "encrypted": True, "direct": True, "room_event": {
        "type": "m.room.message", "sender": "@alice:syn.example", "event_id": f"$m{number}",
        "origin_server_ts": int(time.time() * 1000), "content": {"msgtype": "m.text", "body": f"quick {number}"}}}


async def test_quick_direct_messages_past_the_queue_get_the_rejected_reaction_and_no_notice(monkeypatch, tmp_path):
    kit = MatrixKit(monkeypatch)
    kit.peer.rooms[DM] = Room(direct=True, joined_members=2)
    path = tmp_path / "config.yaml"
    path.write_text("platforms:\n  matrix:\n    homeserver: http://matrix.invalid\n    user_id: '@belle:syn.example'\n"
                    "    typing_indicator: false\n")
    backend = ScriptedBackend()
    backend.play(ModelTurn(final="done", hold=True))
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    platform = host.platforms["matrix"]
    try:
        outcomes = [await platform.handle(dm(number)) for number in range(1, MAX_QUEUED_PER_KEY + 4)]
        assert [o.split(":")[0] for o in outcomes] == ["dispatched"] + ["queued"] * MAX_QUEUED_PER_KEY + ["refused"] * 2
        reactions = {(r["content"]["m.relates_to"]["event_id"], r["content"]["m.relates_to"]["key"]) for r in kit.peer.sent("m.reaction")}
        refused = {f"$m{n}" for n in (MAX_QUEUED_PER_KEY + 2, MAX_QUEUED_PER_KEY + 3)}
        assert {(event_id, "\N{NO ENTRY SIGN}") for event_id in refused} <= reactions
        assert not {key for event_id, key in reactions if event_id not in refused and key == "\N{NO ENTRY SIGN}"}
        assert kit.peer.sent() == []
    finally:
        backend.release()
        await host.stop()


async def test_a_helper_that_keeps_exiting_leaves_matrix_down_and_the_other_platforms_answering(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(connector, "RESTART_MAX_DELAY_SECONDS", 0.01)
    monkeypatch.setenv("FAKE_HELPER_MODE", "die")
    monkeypatch.setenv(PASSPHRASE_ENV, "p")
    monkeypatch.setenv("MATRIX_RECOVERY_KEY", "k")
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms:\n  loopback: {{enabled: true}}\n  matrix:\n    homeserver: http://matrix.invalid\n"
                    f"    user_id: '@belle:syn.example'\n    helper_path: {FAKE}\n")
    backend = ScriptedBackend()
    backend.play(ModelTurn(final="still here"))
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    try:
        await asyncio.wait_for(host.platforms["matrix"]._tasks[1], 20)
        loopback = host.platforms["loopback"]
        message = InboundMessage(platform="loopback", workspace="local", conversation="D-U1", message_id="m1",
                                 sender="U1", text="hello", chat_type="dm", trigger=TRIGGER_DM)
        assert (await loopback.inject(message)).startswith("dispatched")
        await host.idle()
        assert [entry.text for entry in loopback.log if entry.kind == "post"] == ["still here"]
    finally:
        await host.stop()
    assert "matrix stays down: the helper exited 5 times within 300 s. The other platforms go on" in caplog.text


@pytest.fixture
def dying(monkeypatch, tmp_path):
    """A launcher whose Matrix helper does as ``FAKE_HELPER_MODE`` says, with the scripted backend."""
    monkeypatch.setattr(connector, "RESTART_MAX_DELAY_SECONDS", 0.01)
    monkeypatch.setenv(PASSPHRASE_ENV, "p")
    monkeypatch.setenv("MATRIX_RECOVERY_KEY", "k")
    backend = ScriptedBackend()
    started = backend.start

    async def slow_start(tools):
        # A real backend's start waits on its server: the platforms start after it.
        await asyncio.sleep(0.2)
        await started(tools)

    monkeypatch.setattr(backend, "start", slow_start)
    monkeypatch.setattr(launcher, "create_backend", lambda config, state_dir: backend)

    def config(extra: str = "") -> dict:
        path = tmp_path / "config.yaml"
        path.write_text(f"backend: scripted\nsecond_brain: {{enabled: false}}\nplatforms:\n{extra}  matrix:\n    homeserver: http://matrix.invalid\n"
                        f"    user_id: '@belle:syn.example'\n    helper_path: {FAKE}\n")
        return load_config(path)

    return config


@pytest.mark.parametrize(("mode", "reason"), [
    ("die", "matrix: the helper exited 5 times within 300 s"),
    ("alert", "matrix: duplicate one-time key"),
])
async def test_a_launcher_whose_only_platform_stops_for_good_exits_with_1(dying, monkeypatch, tmp_path, caplog, mode, reason):
    monkeypatch.setenv("FAKE_HELPER_MODE", mode)
    with caplog.at_level(logging.CRITICAL, logger="belle_agent.launcher"):
        code = await asyncio.wait_for(launcher.serve(dying(), tmp_path), 20)
    assert code == 1
    assert f"belle stops: every platform has stopped for good, so the process exits with 1. {reason}" in caplog.text


async def test_a_launcher_with_another_platform_up_serves_on_after_matrix_stops(dying, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_HELPER_MODE", "alert")
    control = tmp_path / "control.sock"
    serving = asyncio.create_task(launcher.serve(dying(f"  loopback: {{enabled: true, control: {control}}}\n"), tmp_path))
    async with asyncio.timeout(10):
        while not control.exists():
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.5)
    assert not serving.done()
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(serving, 10) == 0


AMBIENT = "!ambient:syn.example"


def said(number: int, text: str) -> dict:
    return {"event": "timeline", "seq": number, "room": AMBIENT, "encrypted": True, "direct": False, "room_event": {
        "type": "m.room.message", "sender": "@alice:syn.example", "event_id": f"$a{number}",
        "origin_server_ts": int(time.time() * 1000), "content": {"msgtype": "m.text", "body": text}}}


async def test_a_turn_that_may_stay_silent_shows_its_card_only_once_it_has_posted(monkeypatch, tmp_path):
    kit = MatrixKit(monkeypatch)
    kit.peer.rooms[AMBIENT] = Room(joined_members=3)
    path = tmp_path / "config.yaml"
    path.write_text("platforms:\n  matrix:\n    homeserver: http://matrix.invalid\n    user_id: '@belle:syn.example'\n"
                    "    typing_indicator: false\n"
                    "scopes:\n  - match: {channel: matrix}\n    delivery: {mode: [all], reply: optional}\n")
    backend = ScriptedBackend()
    backend.play(
        ModelTurn(calls=(("read_conversation", {}),)),
        ModelTurn(calls=(("read_conversation", {}), ("post_message", {"text": "the tide turns at four"}))),
    )
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    platform = host.platforms["matrix"]
    shown: list[tuple[str, str]] = []

    async def progress(inbound, event):
        shown.append((event.kind, event.data["name"], len(kit.peer.sent())))

    monkeypatch.setattr(platform.surfaces, "progress", progress)
    try:
        await platform.handle(said(1, "small talk"))
        await host.idle()
        assert shown == [] and kit.peer.sent() == []
        await platform.handle(said(2, "when does the tide turn?"))
        await host.idle()
        # Every tool event of the turn, handed over once the post is in the room.
        assert [(kind, name) for kind, name, _ in shown] == [
            ("tool_called", "read_conversation"), ("tool_result", "read_conversation"),
            ("tool_called", "post_message"), ("tool_result", "post_message"),
        ]
        assert {posts for _, _, posts in shown} == {1}
    finally:
        await host.stop()


async def test_a_turn_that_may_stay_silent_and_posts_only_elsewhere_shows_no_card(monkeypatch, tmp_path):
    other = "!other:syn.example"
    kit = MatrixKit(monkeypatch)
    kit.peer.rooms[AMBIENT] = Room(joined_members=3)
    kit.peer.rooms[other] = Room(joined_members=3)
    path = tmp_path / "config.yaml"
    path.write_text("platforms:\n  matrix:\n    homeserver: http://matrix.invalid\n    user_id: '@belle:syn.example'\n"
                    "    typing_indicator: false\n"
                    "scopes:\n  - match: {channel: matrix}\n    delivery: {mode: [all], reply: optional}\n")
    backend = ScriptedBackend()
    backend.play(ModelTurn(calls=(("post_message", {"text": "the tide turns at four", "chat_id": other}),)))
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    platform = host.platforms["matrix"]
    shown: list[str] = []

    async def progress(inbound, event):
        shown.append(event.kind)

    monkeypatch.setattr(platform.surfaces, "progress", progress)
    try:
        await platform.handle(said(1, "tell the other room when the tide turns"))
        await host.idle()
        assert [event["room"] for event in kit.peer.sent()] == [other]
        # The turn stayed silent in its own room, so no card comes up there.
        assert shown == []
    finally:
        await host.stop()
