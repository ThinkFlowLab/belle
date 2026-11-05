"""The files of a message, read for its turn, and the file tool: what each loads, and what each refuses."""

import base64
import json
from dataclasses import replace

import pytest
from test_mattermost_inbound import CHANNEL, POST, Started
from test_mattermost_tools import BOB, LOUNGE, OUTSIDE, QUIET, Turn
from transport import TOKEN, URL, FakeRest, posted

from belle_agent.attachments import FILE_REASON_TOO_LARGE
from belle_agent.backend import ToolContent
from belle_agent.platforms.mattermost.settings import MattermostSettings

NOTES, PICTURE, GONE = "n" * 26, "i" * 26, "g" * 26


@pytest.fixture
async def started():
    running = Started(FakeRest())
    await running.connector.start(running.receive)
    yield running
    await running.connector.stop()


def _share(rest: FakeRest, *names: tuple[str, str, bytes, str]) -> dict:
    """A ``posted`` event whose post carries these files, with their records in its metadata as the server sends them."""
    infos = [rest.file(file_id, CHANNEL, name, data, mime, post=POST) for file_id, name, data, mime in names]
    return posted(POST, file_ids=[info["id"] for info in infos], metadata={"files": infos})


async def test_a_message_carries_its_files_by_reference_and_reads_them_only_when_asked(started):
    record = await started.one(_share(started.rest, (NOTES, "notes.md", b"# Notes\n", "text/markdown")))

    assert [entry["name"] for entry in record.files] == ["notes.md"]
    assert not started.rest.calls("GET", "/files")
    (loaded,) = await started.connector.fetch_files(record)
    assert loaded == {"name": "notes.md", "uri": "data:text/markdown;base64," + base64.b64encode(b"# Notes\n").decode()}


async def test_a_file_over_the_ceiling_is_not_read_and_its_sender_is_told_why(started):
    started.connector._settings = replace(started.connector._settings, file_download_max_bytes=8)
    record = await started.one(_share(started.rest, (NOTES, "notes.md", b"0123456789", "text/plain"),
                                      (PICTURE, "a.png", b"\x89PNG", "image/png")))

    loaded = await started.connector.fetch_files(record)

    assert [entry["name"] for entry in loaded] == ["a.png"]
    # Refused on the size the server states, before a byte of it is read.
    assert [call.path for call in started.rest.calls("GET", "/files")] == [f"/files/{NOTES}/info", f"/files/{PICTURE}/info", f"/files/{PICTURE}"]
    (notice,) = started.rest.calls("POST", "/posts")
    assert notice.body == {
        "channel_id": CHANNEL, "root_id": POST,
        "message": f"Received 1 attachment (notes.md) but could not read it: {FILE_REASON_TOO_LARGE}.",
    }


async def test_inbound_file_uses_raised_download_budget(started):
    started.connector._settings = replace(started.connector._settings, file_download_max_bytes=12)
    record = await started.one(_share(started.rest, (NOTES, "notes.md", b"0123456789", "text/plain")))

    (loaded,) = await started.connector.fetch_files(record)
    assert loaded["name"] == "notes.md"


async def test_a_file_the_server_does_not_give_is_left_out_with_the_reason(started):
    record = await started.one(_share(started.rest, (NOTES, "notes.md", b"text", "text/plain")))
    del started.rest.files[NOTES]

    assert await started.connector.fetch_files(record) == ()
    (notice,) = started.rest.calls("POST", "/posts")
    assert notice.body["message"].endswith("the server holds no such file that the bot can see.")


# ── the tool ──────────────────────────────────────────────────────────


async def test_a_text_file_of_this_channel_comes_back_as_text_with_where_it_was_shared(turn):
    turn.rest.file(NOTES, QUIET, "notes.md", b"# Notes\n", "text/markdown", post=POST)
    turn.came_from(QUIET)

    result = await turn.call("download_file", file_id=NOTES)

    assert result == {
        "ok": True, "file_id": NOTES, "name": "notes.md", "mimetype": "text/markdown", "size_bytes": 8,
        "chat_id": QUIET, "message_id": POST, "created_iso_utc": "2027-01-15T08:00:00Z", "content": "# Notes\n",
    }


async def test_download_file_returns_native_image_metadata_after_audience_check(turn):
    info = turn.rest.file(PICTURE, QUIET, "view.png", b"image", "image/png", post=POST)
    info.update(extension="png", width=640, height=480, has_preview_image=True)
    turn.came_from(QUIET)

    result = await turn.call("download_file", file_id=PICTURE)

    assert (result["file_id"], result["message_id"]) == (PICTURE, POST)
    assert (result["extension"], result["width_px"], result["height_px"], result["has_preview_image"]) == (
        "png", 640, 480, True,
    )


async def test_any_other_file_comes_back_as_content_beside_its_record(turn):
    turn.rest.file(PICTURE, QUIET, "a.png", b"\x89PNG\x00", "image/png")
    turn.came_from(QUIET)

    result = await turn.tools.download_file(file_id=PICTURE)

    assert isinstance(result, ToolContent) and json.loads(result.text)["mimetype"] == "image/png"
    assert [(part.mime, part.data) for part in result.parts] == [("image/png", b"\x89PNG\x00")]


async def test_a_file_of_a_channel_this_one_s_readers_cannot_read_is_refused(turn):
    turn.rest.file(NOTES, QUIET, "notes.md", b"secret")
    turn.came_from(LOUNGE, chat_type="O")

    result = await turn.call("download_file", file_id=NOTES)

    assert (result["ok"], result["error"]) == (False, "source_is_public_and_target_is_not")
    # Refused on the file's record: its bytes are never read.
    assert [call.path for call in turn.rest.calls("GET", "/files")] == [f"/files/{NOTES}/info"]
    # Control: from the private channel, the file of an open channel of the same team is read.
    turn.rest.file(PICTURE, OUTSIDE, "plan.txt", b"open plan")
    turn.came_from(QUIET)
    assert (await turn.call("download_file", file_id=PICTURE))["content"] == "open plan"


async def test_a_file_over_the_size_limit_is_refused_whole(turn):
    budgeted = Turn(turn.rest, MattermostSettings(URL, TOKEN, file_download_max_bytes=8))
    await budgeted.connector.start(lambda record: None)
    turn.rest.file(NOTES, QUIET, "long.txt", b"0123456789")
    budgeted.came_from(QUIET)

    result = await budgeted.call("download_file", file_id=NOTES)

    assert (result["ok"], result["error"], result["file_id"]) == (False, "file_over_size_limit", NOTES)
    assert "content" not in result
    assert "file_download_max_bytes (8 bytes)" in result["detail"]
    assert [call.path for call in turn.rest.calls("GET", f"/files/{NOTES}")] == [f"/files/{NOTES}/info"]
    await budgeted.connector.stop()


async def test_a_file_at_the_configured_byte_limit_is_read_whole(turn):
    budgeted = Turn(turn.rest, MattermostSettings(URL, TOKEN, file_download_max_bytes=8))
    await budgeted.connector.start(lambda record: None)
    turn.rest.file(NOTES, QUIET, "exact.txt", b"12345678")
    budgeted.came_from(QUIET)

    result = await budgeted.call("download_file", file_id=NOTES)

    assert result["content"] == "12345678"
    await budgeted.connector.stop()


async def test_a_text_over_the_configured_budget_is_refused_whole_and_names_the_key(turn):
    budgeted = Turn(turn.rest, MattermostSettings(URL, TOKEN, history_max_total_chars=1_000))
    await budgeted.connector.start(lambda record: None)
    turn.rest.file(NOTES, QUIET, "long.txt", b"x" * 1_001)
    turn.rest.file(PICTURE, QUIET, "short.txt", b"x" * 1_000)
    budgeted.came_from(QUIET)

    refused = await budgeted.call("download_file", file_id=NOTES)
    read = await budgeted.call("download_file", file_id=PICTURE)

    assert (refused["error"], "content" in refused) == ("file_text_over_limit", False)
    assert "over the 1000 one result may hold in this deployment (history_max_total_chars)" in refused["detail"]
    assert read["content"] == "x" * 1_000
    await budgeted.connector.stop()


async def test_an_unknown_file_and_a_malformed_id_are_refused_precisely(turn):
    turn.came_from(QUIET)

    assert (await turn.call("download_file", file_id=GONE))["error"] == "not_found"
    malformed = await turn.call("download_file", file_id="null")
    assert malformed["error"] == "file_id_malformed" and "26 letters and digits" in malformed["detail"]


async def test_a_read_lists_each_file_of_a_message_with_the_id_the_tool_takes(turn):
    info = turn.rest.file(NOTES, QUIET, "notes.md", b"# Notes\n", "text/markdown")
    turn.rest.post(POST, QUIET, BOB, "the notes", file_ids=[NOTES], metadata={"files": [info]})
    turn.came_from(QUIET)

    (message,) = (await turn.call("read_conversation"))["messages"]

    assert message["files"] == [{"file_id": NOTES, "name": "notes.md", "mimetype": "text/markdown", "size_bytes": 8}]


# ── a content tool's attachment ──────────────────────────────────────────


async def test_a_content_tool_reads_a_file_by_the_read_rule_of_download_file(turn):
    turn.rest.file(NOTES, QUIET, "notes.md", b"secret", "text/markdown")
    turn.came_from(QUIET)

    read = await turn.connector.resolve_content_attachment(NOTES, turn.metadata)

    assert read == {"document": base64.b64encode(b"secret").decode(), "document_mime_type": "text/markdown"}
    turn.came_from(LOUNGE, chat_type="O")
    with pytest.raises(ValueError, match="^attachment_ref: source_is_public_and_target_is_not: "):
        await turn.connector.resolve_content_attachment(NOTES, turn.metadata)


async def test_a_content_tool_s_unknown_or_malformed_attachment_is_refused_precisely(turn):
    turn.came_from(QUIET)

    with pytest.raises(ValueError, match="^attachment_ref: not_found: "):
        await turn.connector.resolve_content_attachment(GONE, turn.metadata)
    with pytest.raises(ValueError, match="^attachment_ref: file_id_malformed: attachment_ref is not a Mattermost file id"):
        await turn.connector.resolve_content_attachment("null", turn.metadata)

