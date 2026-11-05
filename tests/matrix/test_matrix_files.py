"""Files on Matrix: the inbound row, loading through the helper, download_file, content tools, and second brain ingest."""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import DOCUMENTS, scripted_second_brain
from peer import USER, Peer, Room
from scripted_backend import ScriptedBackend

from belle_agent.attachments import MAX_FILE_BYTES
from belle_agent.config import load_config
from belle_agent.host import host_from_config
from belle_agent.platforms.matrix import connector
from belle_agent.platforms.matrix.connector import FILE_SCHEME, MatrixPlatform
from belle_agent.platforms.matrix.settings import PASSPHRASE_ENV, MatrixSettings
from belle_agent.platforms.matrix.toolkit import METADATA_MESSAGE, METADATA_ROOM, METADATA_USER

LOUNGE, TOWN, OTHER = "!lounge:syn.example", "!town:syn.example", "!other:syn.example"
BOB, CAROL = "@bob:syn.example", "@carol:syn.example"
SECRET_FILE = {"url": "mxc://syn.example/enc", "key": {"kty": "oct", "key_ops": ["encrypt", "decrypt"], "alg": "A256CTR",
                                                        "k": "K", "ext": True}, "iv": "IV", "hashes": {"sha256": "H"}, "v": "v2"}


def file_content(name: str, *, encrypted: bool, size: int | None = None, mime: str = "text/markdown") -> dict:
    info = {"mimetype": mime, **({"size": size} if size is not None else {})}
    source = {"file": SECRET_FILE} if encrypted else {"url": "mxc://syn.example/plain"}
    return {"msgtype": "m.file", "body": name, "info": info, **source}


def timeline(room: str, content: dict, *, sender: str = BOB, event_id: str = "$f1") -> dict:
    return {"event": "timeline", "seq": 1, "room": room, "encrypted": True, "direct": False, "room_event": {
        "type": "m.room.message", "sender": sender, "event_id": event_id, "origin_server_ts": int(time.time() * 1000),
        "content": content}}


@pytest.fixture
def peer() -> Peer:
    found = Peer(max_upload_bytes=50 * 1024 * 1024)
    found.rooms.update({LOUNGE: Room(), TOWN: Room(encrypted=False, public=True), OTHER: Room()})
    found.members.update({
        LOUNGE: [{"user_id": u, "membership": "join"} for u in (USER, BOB)],
        TOWN: [{"user_id": u, "membership": "join"} for u in (USER, BOB, CAROL)],
        OTHER: [{"user_id": u, "membership": "join"} for u in (USER, CAROL)],
    })
    found.media.update({"mxc://syn.example/enc": b"# Secret plan\n\nlaunch at dawn\n", "mxc://syn.example/plain": b"plain bytes"})
    return found


@pytest.fixture
async def matrix(peer) -> MatrixPlatform:
    settings = MatrixSettings(homeserver="http://syn:8008", user_id=USER, store_dir="/s", store_passphrase="p")
    platform = MatrixPlatform(settings, helper_factory=peer.factory)
    await platform._open()
    return platform


async def test_a_file_message_carries_its_file_by_reference_and_says_whether_its_room_is_encrypted(matrix):
    secret = await matrix.inbound(timeline(LOUNGE, file_content("plan.md", encrypted=True)))
    plain = await matrix.inbound(timeline(TOWN, file_content("notes.md", encrypted=False), event_id="$f2"))

    assert secret.files[0]["name"] == "plan.md" and secret.files[0]["uri"] == f"{FILE_SCHEME}$f1"
    assert secret.encrypted and secret.chat_type == "private"
    assert not plain.encrypted and plain.chat_type == "public" and plain.files[0]["uri"] == f"{FILE_SCHEME}$f2"


async def test_a_file_is_loaded_decrypted_through_the_helper(matrix, peer):
    inbound = await matrix.inbound(timeline(LOUNGE, file_content("plan.md", encrypted=True)))

    (loaded,) = await matrix.fetch_files(inbound)

    assert loaded == {"name": "plan.md", "uri": "data:text/markdown;base64," + base64.b64encode(peer.media["mxc://syn.example/enc"]).decode()}
    (asked,) = [r for r in peer.requests if r["op"] == "download"]
    assert asked["file"] == SECRET_FILE and asked["max_bytes"] == MAX_FILE_BYTES


async def test_a_file_over_the_limit_is_not_read_and_its_sender_is_told_the_limit(peer):
    # The read limit is independent of the server's upload limit.
    peer.max_upload_bytes = 1000
    matrix = MatrixPlatform(MatrixSettings(homeserver="http://s", user_id=USER, store_dir="/s", store_passphrase="p",
                                           file_download_max_bytes=1000),
                            helper_factory=peer.factory)
    await matrix._open()
    claimed = await matrix.inbound(timeline(LOUNGE, file_content("big.pdf", encrypted=True, size=5000)))
    unsized = await matrix.inbound(timeline(TOWN, file_content("liar.bin", encrypted=False), event_id="$f2"))
    peer.media["mxc://syn.example/plain"] = b"x" * 1001

    assert await matrix.fetch_files(claimed) == () and await matrix.fetch_files(unsized) == ()

    # A size in the event is refused before any download. Without one, the helper checks the bytes.
    assert [r["url"] for r in peer.requests if r["op"] == "download"] == ["mxc://syn.example/plain"]
    notices = [r["content"]["body"] for r in peer.sent()]
    assert len(notices) == 2 and all("over the size limit this bot will download, 1000 bytes" in n for n in notices)


async def test_server_upload_limit_does_not_limit_file_reads(peer):
    peer.max_upload_bytes = 1
    matrix = MatrixPlatform(MatrixSettings(homeserver="http://s", user_id=USER, store_dir="/s", store_passphrase="p",
                                           file_download_max_bytes=8), helper_factory=peer.factory)
    await matrix._open()
    peer.media["mxc://syn.example/plain"] = b"12345678"
    inbound = await matrix.inbound(timeline(TOWN, file_content("exact.txt", encrypted=False, size=8)))

    (loaded,) = await matrix.fetch_files(inbound)

    assert loaded["uri"].endswith(base64.b64encode(b"12345678").decode())
    assert matrix.max_file_bytes == 1
    assert matrix.max_download_bytes == 8


@pytest.fixture
async def turn(matrix, peer):
    started = peer.add(LOUNGE, BOB, file_content("plan.md", encrypted=True))
    metadata = {METADATA_ROOM: LOUNGE, METADATA_MESSAGE: started, METADATA_USER: BOB}
    (toolkit,) = matrix.toolkits(lambda: metadata)

    async def call(name: str, **arguments):
        result = await getattr(toolkit, name)(**arguments)
        return json.loads(result) if isinstance(result, str) else result

    call.toolkit, call.started, call.metadata = toolkit, started, metadata
    return call


async def test_download_file_reads_the_file_a_message_of_the_turns_room_carries(turn):
    read = await turn("download_file", file_id=turn.started)

    assert read["ok"] and read["content"] == "# Secret plan\n\nlaunch at dawn\n"
    assert read["name"] == "plan.md" and read["chat_id"] == LOUNGE and read["message_id"] == turn.started


async def test_download_file_obeys_exact_configured_read_limit(turn, peer):
    turn.toolkit._account.max_download_bytes = 8
    peer.media["mxc://syn.example/plain"] = b"12345678"
    exact = peer.add(LOUNGE, BOB, file_content("exact.txt", encrypted=False, size=8))

    read = await turn("download_file", file_id=exact)
    assert read["content"] == "12345678"

    peer.media["mxc://syn.example/plain"] = b"123456789"
    oversized = peer.add(LOUNGE, BOB, file_content("large.txt", encrypted=False, size=9))
    refused = await turn("download_file", file_id=oversized)
    assert refused["error"] == "file_over_size_limit"
    assert "file_download_max_bytes (8 bytes)" in refused["detail"]


async def test_media_metadata_is_returned_without_exposing_encryption_keys(turn, peer):
    info = peer.events[turn.started]["content"]["info"]
    info.update(w=640, h=480, duration=1234, thumbnail_file=SECRET_FILE)

    read = await turn("read_conversation")
    (file,) = next(message for message in read["messages"] if message["message_id"] == turn.started)["files"]
    downloaded = await turn("download_file", file_id=turn.started)

    for record in (file, downloaded):
        assert (record["width_px"], record["height_px"], record["duration_ms"]) == (640, 480, 1234)
        assert record["thumbnail_available"] is True
        assert "key" not in json.dumps(record)


async def test_download_file_refuses_what_it_cannot_read_and_says_why(turn, peer):
    text = peer.add(LOUNGE, BOB, {"msgtype": "m.text", "body": "no file here"})
    hidden = peer.add(LOUNGE, BOB, {}, undecryptable=True)
    elsewhere = peer.add(OTHER, CAROL, file_content("carol.md", encrypted=True))

    assert (await turn("download_file", file_id=text))["error"] == "message_has_no_file"
    assert (await turn("download_file", file_id=hidden))["error"] == "message_undecryptable"
    # bob reads the lounge, and OTHER is carol's: its file would reach a reader who cannot read OTHER.
    refused = await turn("download_file", file_id=elsewhere, chat_id=OTHER)
    assert refused["error"] == "source_members_not_in_target"
    assert (await turn("download_file", file_id="plan.md"))["error"] == "message_id_malformed"
    assert not [r for r in peer.requests if r["op"] == "download"]


async def test_a_content_tool_reads_a_file_of_the_turns_room_by_its_message_id(turn, matrix):
    found = await matrix.resolve_content_attachment(turn.started, turn.metadata)

    assert base64.b64decode(found["document"]) == b"# Secret plan\n\nlaunch at dawn\n"
    assert found["document_mime_type"] == "text/markdown"
    with pytest.raises(ValueError, match="attachment_ref: message_id_malformed"):
        await matrix.resolve_content_attachment("report.pdf", turn.metadata)


async def test_a_read_lists_the_file_of_a_message_with_the_id_download_file_takes(turn):
    read = await turn("read_conversation")

    (record,) = [m for m in read["messages"] if m["message_id"] == turn.started]
    assert record["files"] == [{"file_id": turn.started, "name": "plan.md", "mimetype": "text/markdown", "size_bytes": None}]


# ── the second brain on Matrix ───────────────────────────────────────────

ENCRYPTED_PUBLIC = "!square:syn.example"


@pytest.mark.parametrize(
    ("channels", "ingested"),
    [
        # public selects the public room without encryption only.
        ("[public]", {TOWN}),
        # * leaves the encrypted rooms out too. Only their own ids select them.
        ('["*"]', {TOWN}),
        (f'[public, "{LOUNGE}"]', {TOWN, LOUNGE}),
    ],
)
async def test_a_file_is_ingested_where_the_selection_names_its_room_and_encrypted_rooms_only_by_id(
    monkeypatch, tmp_path, channels, ingested
):
    monkeypatch.setenv(PASSPHRASE_ENV, "p")
    peer = Peer()
    monkeypatch.setattr(connector, "Helper", peer.factory)
    peer.rooms.update({TOWN: Room(encrypted=False, public=True), ENCRYPTED_PUBLIC: Room(public=True), LOUNGE: Room()})
    text = DOCUMENTS["lamp-rotation.md"].encode()
    peer.media.update({"mxc://syn.example/plain": text, "mxc://syn.example/enc": text})
    path = tmp_path / "config.yaml"
    path.write_text(
        "platforms:\n  matrix:\n    homeserver: http://matrix.invalid\n    user_id: '@belle:syn.example'\n"
        "    typing_indicator: false\n"
        f"second_brain: {{enabled: true, corpus_dir: {tmp_path / 'corpus'}, ingest: {{channels: {channels}}}}}\n"
    )
    host = host_from_config(load_config(path), ScriptedBackend(), state_dir=tmp_path / "state", second_brain=scripted_second_brain())
    await host.start()
    platform = host.platforms["matrix"]
    try:
        for number, room in enumerate((TOWN, ENCRYPTED_PUBLIC, LOUNGE)):
            content = file_content(f"lamp-{number}.md", encrypted=room != TOWN)
            await platform.handle(timeline(room, content, event_id=f"$doc{number}"))
        for _ in range(100):
            marked = {r["room"] for r in peer.sent("m.reaction") if r["content"]["m.relates_to"]["key"] == "\N{BRAIN}"}
            if marked == ingested:
                break
            await asyncio.sleep(0.02)
        assert marked == ingested
        # Nothing of an unselected room is downloaded.
        assert len([r for r in peer.requests if r["op"] == "download"]) == len(ingested)
    finally:
        await host.stop()
