"""Two Matrix accounts of one Belle in one room: what one account does there must not act on the other's turns.

Each account has its own helper, so the check plays the homeserver: what one account sends to the room
reaches the other as a timeline event, as it does when both accounts are members of that room.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import ModelTurn
from peer import Peer, Peers, Room
from scripted_backend import ScriptedBackend

from belle_agent.config import load_config
from belle_agent.host import host_from_config
from belle_agent.platforms.matrix import connector

WORK, HOME = "@belle:work.example", "@belle:home.example"
ALICE = "@alice:work.example"
ROOM = "!lounge:work.example"


@pytest.fixture
def peers(monkeypatch) -> Peers:
    for name in ("WORK", "HOME"):
        monkeypatch.setenv(f"PASS_{name}", f"passphrase-{name}")
    found = Peers(Peer(user=WORK), Peer(user=HOME))
    monkeypatch.setattr(connector, "Helper", found.factory)
    for peer in found.peers.values():
        peer.rooms[ROOM] = Room(encrypted=False, joined_members=3)
    return found


def config(tmp_path: Path, scopes: str = "") -> dict:
    accounts = "".join(
        f"      - {{name: {name}, homeserver: 'http://{name}.invalid', user_id: '{user}', store_passphrase_env: PASS_{name.upper()}}}\n"
        for name, user in (("work", WORK), ("home", HOME))
    )
    path = tmp_path / "config.yaml"
    path.write_text(f"second_brain: {{enabled: false}}\nplatforms:\n  matrix:\n    typing_indicator: false\n    accounts:\n{accounts}{scopes}")
    return load_config(path)


def timeline(sender: str, event_type: str, content: dict, event_id: str) -> dict:
    return {"event": "timeline", "seq": 1, "room": ROOM, "encrypted": False, "direct": False,
            "room_event": {"type": event_type, "sender": sender, "event_id": event_id,
                           "origin_server_ts": int(time.time() * 1000), "content": content}}


def to_both(text: str, event_id: str) -> dict:
    return timeline(ALICE, "m.room.message", {"msgtype": "m.text", "body": text, "m.mentions": {"user_ids": [WORK, HOME]}}, event_id)


def relayed(peer: Peer, key: str) -> dict:
    """The reaction ``peer`` sent with ``key``, as the room hands it to the other account."""
    (sent,) = [request for request in peer.sent("m.reaction") if request["content"]["m.relates_to"]["key"] == key]
    return timeline(peer.user, "m.reaction", sent["content"], f"$from-{peer.user}-{len(peer.requests)}")


async def running(backend: ScriptedBackend, count: int) -> None:
    async with asyncio.timeout(5):
        while len(backend._running) < count:
            await asyncio.sleep(0.01)


def turn_of(host, workspace: str):
    (turn,) = [turn for turn in host._active_work.values() if turn.inbound.workspace == workspace]
    return turn


async def test_one_accounts_stop_mark_does_not_stop_the_other_accounts_turn(peers, tmp_path):
    backend = ScriptedBackend()
    # The work account's turn waits on its own question, which another conversation's stop does not end.
    backend.play(ModelTurn(final="from work", ask=("Which?", ("a", "b"))), ModelTurn(final="from home", hold=True))
    host = host_from_config(config(tmp_path), backend, state_dir=tmp_path / "state")
    await host.start()
    matrix = host.platforms["matrix"]
    work, home = matrix.members["work"], matrix.members["home"]
    try:
        assert (await work.handle(to_both("both of you", "$ask"))).startswith("dispatched")
        await running(backend, 1)
        assert (await home.handle(to_both("both of you", "$ask"))).startswith("dispatched")
        await running(backend, 2)
        # The operator stops only the home account's turn, as the control socket and the TUI do.
        await host.stop_work(turn_of(host, "home").status_id, "running")
        async with asyncio.timeout(5):
            while not any(sent["content"]["m.relates_to"]["key"] == "\N{BLACK SQUARE FOR STOP}" for sent in peers.peers[HOME].sent("m.reaction")):
                await asyncio.sleep(0.01)
        stop_mark = relayed(peers.peers[HOME], "\N{BLACK SQUARE FOR STOP}")

        outcome = await work.handle(stop_mark)

        assert outcome == "ignored:done-by-an-app", outcome
        assert turn_of(host, "work").inbound.message_id == "$ask"
        assert peers.peers[WORK].sent() == []
        # Control: the person's own ⏹️ on the same message stops the work account's turn.
        stop = timeline(ALICE, "m.reaction", {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$ask",
                                                              "key": "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}"}}, "$stop")
        assert (await work.handle(stop)).startswith("stopped"), "a person's stop is still a stop"
    finally:
        backend.release()
        await host.stop()


async def test_one_accounts_marks_start_no_event_turn_for_the_other(peers, tmp_path):
    scopes = ("scopes:\n  - match: {channel: matrix}\n"
              "    delivery: {mode: [mention], reply: optional, events: {reaction: turn}}\n")
    backend = ScriptedBackend()
    backend.play(ModelTurn(final="answer", hold=True))
    host = host_from_config(config(tmp_path, scopes), backend, state_dir=tmp_path / "state")
    await host.start()
    matrix = host.platforms["matrix"]
    work, home = matrix.members["work"], matrix.members["home"]
    try:
        mention_home = timeline(ALICE, "m.room.message", {"msgtype": "m.text", "body": "home only",
                                                          "m.mentions": {"user_ids": [HOME]}}, "$ask")
        assert (await home.handle(mention_home)).startswith("dispatched")
        acknowledgement = relayed(peers.peers[HOME], "\N{EYES}")

        outcome = await work.handle(acknowledgement)

        assert outcome == "ignored:done-by-an-app", outcome
        # Control: a person's reaction on the same message starts the event turn the scope asks for.
        liked = timeline(ALICE, "m.reaction", {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$ask",
                                                               "key": "\N{THUMBS UP SIGN}"}}, "$like")
        assert (await work.handle(liked)).startswith(("dispatched", "queued"))
    finally:
        backend.release()
        await host.stop()


async def test_the_stop_mark_of_a_bot_that_bot_user_ids_lists_stops_no_turn(peers, tmp_path):
    outside = "@otherbot:work.example"
    backend = ScriptedBackend()
    backend.play(ModelTurn(final="from work", hold=True))
    host = host_from_config(config(tmp_path, f"    bot_user_ids: ['{outside}']\n"), backend, state_dir=tmp_path / "state")
    await host.start()
    work = host.platforms["matrix"].members["work"]
    try:
        assert (await work.handle(to_both("both of you", "$ask"))).startswith("dispatched")
        await running(backend, 1)
        mark = timeline(outside, "m.reaction", {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$ask",
                                                                "key": "\N{BLACK SQUARE FOR STOP}"}}, "$mark")

        assert await work.handle(mark) == "ignored:done-by-an-app"
        assert turn_of(host, "work").inbound.message_id == "$ask"
    finally:
        backend.release()
        await host.stop()
