"""``platforms.matrix.accounts``: several accounts in one Belle, each with its own helper, store and secrets."""

import asyncio
import json
import logging
import time

import pytest
from peer import Peer, Peers, Room

from belle_agent.config import BelleConfigError
from belle_agent.platforms import installations
from belle_agent.platforms.base import enabled_platforms
from belle_agent.platforms.installations import InstallationUnknown
from belle_agent.platforms.matrix import connector, create_platform, operator
from belle_agent.platforms.matrix.accounts import ACCOUNT_UNKNOWN, MatrixAccounts
from belle_agent.platforms.matrix.helper import HelperError
from belle_agent.platforms.matrix.settings import read_accounts
from belle_agent.platforms.matrix.toolkit import METADATA_ACCOUNT, METADATA_MESSAGE, METADATA_ROOM, METADATA_USER

WORK, HOME = "@belle:work.example", "@belle:home.example"
ALICE = "@alice:work.example"
ROOM = "!lounge:work.example"


def entry(name: str, user_id: str, **more) -> dict:
    return {"name": name, "homeserver": f"http://{name}:8008", "user_id": user_id,
            "store_passphrase_env": f"PASS_{name.upper()}", **more}


def config(*entries: dict, **section) -> dict:
    return {"platforms": {"matrix": {"accounts": list(entries), **section}}}


@pytest.fixture
def secrets(monkeypatch):
    for name in ("WORK", "HOME"):
        monkeypatch.setenv(f"PASS_{name}", f"passphrase-{name}")


def test_each_account_has_its_own_store_and_secrets(secrets, monkeypatch, tmp_path):
    key = tmp_path / "home.key"
    key.write_text("home-key\n")
    monkeypatch.setenv("KEY_HOME_FILE", str(key))
    monkeypatch.setenv("PASSWORD_WORK", "work-password")
    work, home = read_accounts(
        config(entry("work", WORK, password_env="PASSWORD_WORK"), entry("Home", HOME, recovery_key_env="KEY_HOME"),
               device_name="bot"),
        tmp_path,
    )
    assert (work.account, work.store_dir, work.store_passphrase, work.password, work.recovery_key) == (
        "work", str(tmp_path / "matrix" / "work"), "passphrase-WORK", "work-password", "")
    assert (home.account, home.store_dir, home.recovery_key, home.device_name) == ("Home", str(tmp_path / "matrix" / "Home"), "home-key", "bot")
    assert (home.where, home.passphrase_env, home.password_env) == ("platforms.matrix.accounts[1]", "PASS_HOME", "")


@pytest.mark.parametrize(
    ("section", "named"),
    [
        (config(entry("work", WORK), entry("WORK", HOME)), r"accounts\[1\]\.name is the name of platforms.matrix.accounts\[0\]"),
        (config(entry("work", WORK), entry("home", WORK)), r"accounts\[1\]\.user_id is the user_id of"),
        (config(entry("work", WORK, store_dir="/s"), entry("home", HOME, store_dir="/s/")), r"accounts\[1\]\.store_dir is the store_dir"),
        (config(entry("work", WORK, store_passphrase_env="not a name")), r"accounts\[0\]\.store_passphrase_env must be the name of an"),
        (config({**entry("work", WORK), "store_passphrase_env": None}), r"store_passphrase_env must be the name of an environment variable"),
        (config(entry("work", WORK, token="x")), r"accounts\[0\] has unknown keys: token"),
        (config(entry("a b", WORK)), r"accounts\[0\]\.name must be 1 to 32 letters"),
        (config(entry("work", WORK), homeserver="http://s"), "platforms.matrix.homeserver is set beside platforms.matrix.accounts"),
        (config({**entry("work", WORK), "homeserver": ""}), r"accounts\[0\] cannot start: some of its homeserver"),
        (config(entry("work", WORK, enabled=True, store_passphrase_env="UNSET")), r"accounts\[0\] cannot start: enabled is true, and UNSET is not set"),
        (config(entry("work", WORK, enabled=False)), "holds no account that is on"),
        ({"platforms": {"matrix": {"accounts": "work"}}}, "accounts must be a list of mappings"),
    ],
)
def test_a_list_that_does_not_fit_is_refused_by_name(secrets, monkeypatch, tmp_path, section, named):
    monkeypatch.delenv("UNSET", raising=False)
    with pytest.raises(BelleConfigError, match=named):
        read_accounts(section, tmp_path)


def test_the_section_is_on_while_one_account_is_on(secrets, monkeypatch):
    off = config({"name": "work", "store_passphrase_env": "UNSET"}, entry("home", HOME, enabled=False))
    monkeypatch.delenv("UNSET", raising=False)
    with pytest.raises(BelleConfigError, match="an account of platforms.matrix.accounts whose homeserver, user_id and store passphrase resolve is not set"):
        enabled_platforms(off)
    assert enabled_platforms(config(entry("work", WORK), entry("home", HOME, enabled=False))) == ["matrix"]


@pytest.fixture
def peers(secrets, monkeypatch) -> Peers:
    found = Peers(Peer(user=WORK), Peer(user=HOME))
    monkeypatch.setattr(connector, "Helper", found.factory)
    for peer in found.peers.values():
        peer.rooms[ROOM] = Room(encrypted=False, joined_members=3)
    return found


def message(sender: str, body: str, event_id: str, *, mention: str = "") -> dict:
    content: dict = {"msgtype": "m.text", "body": body}
    if mention:
        content["m.mentions"] = {"user_ids": [mention]}
    return {"event": "timeline", "seq": 1, "room": ROOM, "encrypted": False, "direct": False,
            "room_event": {"type": "m.room.message", "sender": sender, "event_id": event_id,
                           "origin_server_ts": int(time.time() * 1000), "content": content}}


async def test_each_account_hands_on_its_messages_under_its_own_name_and_acts_with_its_own_helper(peers, tmp_path):
    platform = create_platform(config(entry("work", WORK), entry("home", HOME), typing_indicator=False), tmp_path)
    assert isinstance(platform, MatrixAccounts)
    received = []

    async def on_inbound(record):
        received.append(record)
        return "admitted"

    await platform.start(on_inbound)
    try:
        assert platform.direct_chat_types == frozenset({"direct"})
        assert platform.command_prefix("work") == "!belle"
        work, home = platform.members.values()
        await work.handle(message(ALICE, "to work", "$w1", mention=WORK))
        await home.handle(message(ALICE, "to home", "$h1", mention=HOME))
        assert [(record.workspace, record.text, record.trigger) for record in received] == [
            ("work", "to work", "mention"), ("home", "to home", "mention")]
        await platform.post(ROOM, "", "answer from home", workspace="home")
        assert [sent["content"]["body"] for sent in peers.peers[HOME].sent()] == ["answer from home"]
        assert peers.peers[WORK].sent() == []
        with pytest.raises(InstallationUnknown, match="no served Matrix account has the workspace default"):
            await platform.post(ROOM, "", "x", workspace="default")
        assert platform.request_metadata(received[1])[METADATA_ACCOUNT] == "home"
    finally:
        await platform.stop()


async def test_a_tool_call_acts_with_the_account_of_its_turn_and_with_no_other(peers, tmp_path):
    platform = create_platform(config(entry("work", WORK), entry("home", HOME)), tmp_path)
    await platform.start(lambda record: asyncio.sleep(0, "admitted"))
    metadata: dict = {}
    (toolkit,) = platform.toolkits(lambda: metadata)
    try:
        for name, user in (("work", WORK), ("home", HOME)):
            started = peers.peers[user].add(ROOM, ALICE, {"msgtype": "m.text", "body": "hi"})
            metadata.update({METADATA_ACCOUNT: name, METADATA_ROOM: ROOM, METADATA_MESSAGE: started, METADATA_USER: ALICE})
            assert json.loads(await toolkit.post_message(text=f"posted by {name}"))["ok"]
        assert [sent["content"]["body"] for sent in peers.peers[WORK].sent()] == ["posted by work"]
        assert [sent["content"]["body"] for sent in peers.peers[HOME].sent()] == ["posted by home"]
        metadata[METADATA_ACCOUNT] = "elsewhere"
        refused = json.loads(await toolkit.post_message(text="x"))
        assert refused["error"] == ACCOUNT_UNKNOWN and "elsewhere" in refused["detail"]
    finally:
        await platform.stop()


async def test_an_account_that_cannot_start_is_named_and_the_others_are_served(peers, monkeypatch, tmp_path, caplog):
    request = peers.peers[HOME].request

    async def refuse(op, **fields):
        if op == "start":
            raise HelperError("helper_start_refused", "the store passphrase is wrong", reason="store_open_failed")
        return await request(op, **fields)

    monkeypatch.setattr(peers.peers[HOME], "request", refuse)
    platform = create_platform(config(entry("work", WORK), entry("home", HOME)), tmp_path)
    with caplog.at_level(logging.ERROR, logger="belle_agent.platforms.matrix.accounts"):
        await platform.start(lambda record: asyncio.sleep(0, "admitted"))
    try:
        assert list(platform.serving) == ["work"]
        assert "matrix account home is NOT SERVED: matrix cannot start: the store passphrase is wrong. Check PASS_HOME" in caplog.text
    finally:
        await platform.stop()
    monkeypatch.setattr(peers.peers[WORK], "request", refuse)
    with pytest.raises(BelleConfigError, match="no account of platforms.matrix.accounts can be served"):
        await create_platform(config(entry("work", WORK), entry("home", HOME)), tmp_path).start(lambda record: asyncio.sleep(0, ""))


async def test_an_account_whose_homeserver_does_not_answer_is_served_once_it_answers(peers, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(installations, "RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(installations, "RETRY_MAX_SECONDS", 0.01)
    request = peers.peers[HOME].request
    attempts = []

    async def silent(op, **fields):
        if op == "start" and len(attempts) < 3:
            attempts.append(op)
            raise HelperError("helper_start_refused", "error sending request: connection refused", reason="homeserver_unreachable")
        return await request(op, **fields)

    monkeypatch.setattr(peers.peers[HOME], "request", silent)
    platform = create_platform(config(entry("work", WORK), entry("home", HOME)), tmp_path)
    with caplog.at_level(logging.INFO, logger="belle_agent.platforms.matrix.accounts"):
        await platform.start(lambda record: asyncio.sleep(0, "admitted"))
        try:
            assert "matrix account home is NOT CONNECTED: the Matrix homeserver did not answer" in caplog.text
            await asyncio.wait_for(_served(platform, "home"), 5)
            assert len(attempts) == 3 and not platform.connecting
            assert "matrix account home is served now" in caplog.text
            await platform.post(ROOM, "", "answer from home", workspace="home")
            assert [sent["content"]["body"] for sent in peers.peers[HOME].sent()] == ["answer from home"]
        finally:
            await platform.stop()


async def _served(platform, name: str) -> None:
    while name not in platform.serving:
        await asyncio.sleep(0.01)


async def test_the_start_fails_when_no_homeserver_answers(peers, monkeypatch, tmp_path):
    async def silent(op, **fields):
        raise HelperError("helper_start_refused", "error sending request: connection refused", reason="homeserver_unreachable")

    for peer in peers.peers.values():
        monkeypatch.setattr(peer, "request", silent)
    with pytest.raises(BelleConfigError, match="no account of platforms.matrix.accounts can be served") as raised:
        await create_platform(config(entry("work", WORK), entry("home", HOME)), tmp_path).start(lambda record: asyncio.sleep(0, ""))
    assert "work: the Matrix homeserver did not answer" in str(raised.value)
    assert "home: the Matrix homeserver did not answer" in str(raised.value)


async def test_the_platform_stops_for_good_once_every_account_has(peers, tmp_path):
    platform = create_platform(config(entry("work", WORK), entry("home", HOME)), tmp_path)
    await platform.start(lambda record: asyncio.sleep(0, "admitted"))
    waiting = asyncio.create_task(platform.wait_down())
    work, home = platform.members.values()
    work._gone("work went down")
    await asyncio.sleep(0)
    assert not waiting.done()
    home._gone("home went down")
    assert await asyncio.wait_for(waiting, 1) == "work: work went down; home: home went down"
    await platform.stop()


def test_an_operator_command_names_the_account_where_the_section_lists_several(secrets, tmp_path):
    listed = config(entry("work", WORK), entry("home", HOME))
    assert operator._settings(listed, tmp_path, "home").user_id == HOME
    with pytest.raises(BelleConfigError, match="name the account with --account, one of work, home"):
        operator._settings(listed, tmp_path, None)
    with pytest.raises(BelleConfigError, match="lists no accounts: leave --account out"):
        operator._settings({"platforms": {"matrix": {"homeserver": "http://s", "user_id": WORK}}}, tmp_path, "work")


@pytest.mark.parametrize(("new_device", "device"), [
    (True, "its new device PEERDEVICE is logged out and its store removed"),
    (False, "its device PEERDEVICE, which its store held before this start, stays on the account"),
])
async def test_two_entries_the_homeserver_logs_into_one_account_are_not_both_served(
    secrets, monkeypatch, tmp_path, caplog, new_device, device
):
    # A homeserver matches a localpart without regard to case, so both entries log in to the account WORK.
    same = Peer(user=WORK, new_device=new_device)
    same.rooms[ROOM] = Room(encrypted=False, joined_members=3)
    found = Peers(same)
    found.peers["@Belle:work.example"] = same
    monkeypatch.setattr(connector, "Helper", found.factory)
    platform = create_platform(config(entry("work", WORK), entry("home", "@Belle:work.example")), tmp_path)
    with caplog.at_level(logging.ERROR, logger="belle_agent.platforms.matrix.accounts"):
        await platform.start(lambda record: asyncio.sleep(0, "admitted"))
    try:
        assert list(platform.serving) == ["work"]
        assert (
            f"matrix account home is NOT SERVED: it logged in as {WORK}, which account work already serves, so both"
            f" entries name one account and each would answer the same message. Remove one of them; {device}"
        ) in caplog.text
        # Only a device this start made is logged out: the one a store held before cannot be made again.
        assert same.logged_out is new_device
    finally:
        await platform.stop()
