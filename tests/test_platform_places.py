"""Optional place checks used before unattended delivery."""

from belle_agent.audience import Audience
from belle_agent.platforms.base import InboundMessage, PlaceCheck
from belle_agent.platforms.loopback import LoopbackPlatform


async def test_loopback_checks_place_membership_and_thread() -> None:
    platform = LoopbackPlatform()
    platform.audiences[("", "room")] = Audience(False, frozenset({"alice"}), False)
    assert await platform.check_place("missing", "") == PlaceCheck(False, False, "conversation missing does not exist")
    assert await platform.check_place("room", "") == PlaceCheck(True, False, "bot is not a member of room")
    platform.joined.add(("", "room"))
    assert await platform.check_place("room", "new") == PlaceCheck(True, True)
    assert not (await platform.check_place("room", "missing")).exists
    platform.threads.add(("", "room", "root"))
    assert await platform.check_place("room", "root") == PlaceCheck(True, True)


async def test_loopback_audience_and_direct_message() -> None:
    platform = LoopbackPlatform()
    platform.audiences.update({
        ("", "origin"): Audience(False, frozenset({"alice"}), False),
        ("", "target"): Audience(False, frozenset({"alice", "bob"}), False),
        ("", "public"): Audience(True, frozenset(), False),
    })
    assert await platform.can_read("alice", "origin")
    assert not await platform.can_read("bob", "origin")
    assert await platform.can_read("bob", "public")
    assert await platform.newcomers("origin", "target") == ("bob",)
    assert await platform.newcomers("origin", "public") is None
    assert await platform.newcomers("target", "origin") == ()
    direct = await platform.open_direct_message("bob")
    assert direct == await platform.open_direct_message("bob")
    assert await platform.check_place(direct, "") == PlaceCheck(True, True, chat_type="dm")
    assert platform.mention("bob") == "bob"


async def test_loopback_audience_uses_workspace_with_conversation_id() -> None:
    platform = LoopbackPlatform()
    platform.audiences.update({
        ("W1", "C1"): Audience(False, frozenset({"alice"}), False),
        ("W2", "C1"): Audience(False, frozenset({"bob"}), False),
    })

    assert await platform.can_read("alice", "C1", workspace="W1")
    assert not await platform.can_read("alice", "C1", workspace="W2")
    assert await platform.can_read("bob", "C1", workspace="W2")
    assert await platform.newcomers("C1", "C1", workspace="W1") == ()


async def test_loopback_place_uses_observed_chat_type() -> None:
    platform = LoopbackPlatform()
    platform.audiences[("", "room")] = Audience(False, frozenset({"alice"}), False)
    platform.joined.add(("", "room"))

    async def received(record: InboundMessage) -> str:
        return "ok"

    await platform.start(received)
    await platform.inject(InboundMessage("loopback", "", "room", "m1", "alice", "hi", chat_type="channel"))
    assert (await platform.check_place("room", "")).chat_type == "channel"
