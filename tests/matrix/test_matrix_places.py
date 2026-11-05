"""Matrix place checks use the account's helper and joined rooms."""

from peer import USER, Peer, Room

from belle_agent.platforms.base import PlaceCheck
from belle_agent.platforms.matrix.connector import MatrixPlatform
from belle_agent.platforms.matrix.settings import MatrixSettings

ROOM = "!room:syn.example"
TARGET = "!target:syn.example"
ALICE = "@alice:syn.example"
BOB = "@bob:syn.example"


def platform(peer: Peer) -> MatrixPlatform:
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p")
    connector = MatrixPlatform(settings, helper_factory=peer.factory)
    connector._helper, connector.user_id = peer, USER
    return connector


async def test_matrix_place_and_thread_must_be_joined_and_found() -> None:
    peer = Peer(rooms={ROOM: Room()})
    peer.joined = {ROOM}
    matrix = platform(peer)
    event = peer.add(ROOM, ALICE, {"body": "root"})
    assert await matrix.check_place(ROOM, "") == PlaceCheck(True, True, chat_type="private")
    assert await matrix.check_place(ROOM, "new") == PlaceCheck(True, True, chat_type="private")
    assert await matrix.check_place(ROOM, event) == PlaceCheck(True, True, chat_type="private")
    assert not (await matrix.check_place(ROOM, "$missing")).exists
    assert not (await matrix.check_place(TARGET, "")).exists


async def test_matrix_place_type_comes_from_current_room_info() -> None:
    peer = Peer(rooms={ROOM: Room(public=True)})
    matrix = platform(peer)
    assert (await matrix.check_place(ROOM, "")).chat_type == "public"
    peer.rooms[ROOM].public = False
    peer.rooms[ROOM].direct = True
    peer.rooms[ROOM].joined_members = 2
    assert (await matrix.check_place(ROOM, "")).chat_type == "direct"
    peer.rooms[ROOM].public = True
    assert (await matrix.check_place(ROOM, "")).chat_type == "public"


async def test_matrix_readers_and_newcomers_use_joined_members() -> None:
    peer = Peer(rooms={ROOM: Room(), TARGET: Room()})
    peer.members[ROOM] = [{"user_id": USER, "membership": "join"}, {"user_id": ALICE, "membership": "join"}]
    peer.members[TARGET] = [*peer.members[ROOM], {"user_id": BOB, "membership": "invite"}]
    matrix = platform(peer)
    assert await matrix.can_read(ALICE, ROOM)
    assert not await matrix.can_read(BOB, TARGET)
    assert await matrix.newcomers(ROOM, TARGET) == ()
    peer.members[TARGET][-1]["membership"] = "join"
    assert await matrix.newcomers(ROOM, TARGET) == (BOB,)
    peer.rooms[TARGET].public = True
    assert await matrix.newcomers(ROOM, TARGET) is None


async def test_matrix_direct_message_uses_helper() -> None:
    peer = Peer()
    matrix = platform(peer)
    room = await matrix.open_direct_message(ALICE)
    assert room == peer.dms[ALICE]
    assert matrix.mention(ALICE) == ALICE
