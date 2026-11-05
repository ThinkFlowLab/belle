"""The edit, removal, pin and file tools on the fake server: where each call lands, and what it refuses."""

import asyncio
import base64
import json

import pytest
from test_mattermost_tools import ALICE, LOUNGE, OUTSIDE, PAIR, QUIET, Turn
from transport import BOT_USER, TOKEN, URL

from belle_agent.platforms.mattermost import client as client_module
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.settings import MattermostSettings

OWN, THEIRS = "x" * 26, "y" * 26
TIME_LIMIT = (400, {"id": "api.post.update_post.permissions_time_limit.app_error", "status_code": 400})


# ── edit_message and delete_message ─────────────────────────────────────


async def test_an_edit_replaces_the_bot_s_own_message_and_another_author_s_is_refused(turn):
    turn.came_from(QUIET)
    turn.rest.post(OWN, QUIET, BOT_USER, "draft")
    turn.rest.post(THEIRS, QUIET, ALICE, "mine")
    turn.rest.routes[("PUT", f"/posts/{THEIRS}/patch")] = (200, {"id": THEIRS})

    edited = await turn.call("edit_message", message_id=OWN, text="final")
    refused = await turn.call("edit_message", message_id=THEIRS, text="changed")

    assert edited == {"ok": True, "chat_id": QUIET, "message_id": OWN, "permalink": f"http://chat.invalid/team-tttt/pl/{OWN}"}
    assert turn.rest.posts[OWN]["message"] == "final"
    assert refused["error"] == "message_not_own"
    assert turn.rest.posts[THEIRS]["message"] == "mine"
    assert turn.rest.calls("PUT", f"/posts/{THEIRS}/patch") == []


async def test_an_edit_past_the_server_s_limit_is_refused_with_the_limit(turn):
    turn.came_from(QUIET)
    turn.rest.post(OWN, QUIET, BOT_USER, "old")
    turn.rest.client_config["PostEditTimeLimit"] = "300"
    turn.rest.routes[("PUT", f"/posts/{OWN}/patch")] = TIME_LIMIT

    refused = await turn.call("edit_message", message_id=OWN, text="new")

    assert refused["error"] == "edit_time_limit_passed"
    assert "for 300 seconds after it is posted (PostEditTimeLimit)" in refused["detail"]


async def test_an_edit_longer_than_one_message_and_an_empty_edit_are_refused_before_any_change(turn):
    turn.came_from(QUIET)
    turn.rest.post(OWN, QUIET, BOT_USER, "old")
    turn.rest.client_config["MaxPostSize"] = "10"

    too_long = await turn.call("edit_message", message_id=OWN, text="x" * 11)
    empty = await turn.call("edit_message", message_id=OWN, text="  ")

    assert too_long["error"] == "edit_text_too_long" and "at most 10 characters" in too_long["detail"]
    assert empty["error"] == "nothing_to_post" and "delete_message" in empty["detail"]
    assert turn.rest.calls("PUT", "/posts") == []


async def test_an_edit_of_a_message_of_another_channel_needs_its_chat_id_and_asks_where_it_reaches_new_readers(turn):
    turn.came_from(PAIR)
    turn.rest.post(OWN, QUIET, BOT_USER, "old")

    unnamed = await turn.call("edit_message", message_id=OWN, text="new")
    question = await turn.tools.widening_question("edit_message", {"message_id": OWN, "text": "new", "chat_id": QUIET})
    turn.metadata["mattermost_user_id"] = ""
    unattended = await turn.call("edit_message", message_id=OWN, text="new", chat_id=QUIET)

    assert unnamed["error"] == "message_not_found"
    assert "@bob" in question
    assert unattended["error"] == "write_target_widens_audience"
    assert turn.rest.posts[OWN]["message"] == "old"


async def test_a_removal_takes_the_bot_s_own_message_and_refuses_another_author_s(turn):
    turn.came_from(QUIET)
    turn.rest.post(OWN, QUIET, BOT_USER, "oops")
    turn.rest.post(THEIRS, QUIET, ALICE, "mine")
    turn.rest.routes[("DELETE", f"/posts/{THEIRS}")] = (200, {"status": "OK"})

    deleted = await turn.call("delete_message", message_id=OWN)
    refused = await turn.call("delete_message", message_id=THEIRS)

    assert deleted == {"ok": True, "chat_id": QUIET, "message_id": OWN, "deleted": True}
    assert OWN not in turn.rest.posts and THEIRS in turn.rest.posts
    assert refused["error"] == "message_not_own"
    assert turn.rest.calls("DELETE", f"/posts/{THEIRS}") == []


async def test_edit_and_delete_refuse_a_post_without_an_author(turn):
    turn.came_from(QUIET)
    turn.rest.post(OWN, QUIET, "", "unknown author")
    turn.rest.routes[("PUT", f"/posts/{OWN}/patch")] = (200, {"id": OWN})
    turn.rest.routes[("DELETE", f"/posts/{OWN}")] = (200, {"status": "OK"})

    edited = await turn.call("edit_message", message_id=OWN, text="changed")
    deleted = await turn.call("delete_message", message_id=OWN)

    assert edited["error"] == deleted["error"] == "message_not_own"
    assert turn.rest.calls("PUT", f"/posts/{OWN}/patch") == []
    assert turn.rest.calls("DELETE", f"/posts/{OWN}") == []


# ── pin_message ──────────────────────────────────────────────────────────


async def test_a_pin_and_an_unpin_of_a_message_of_this_conversation(turn):
    turn.came_from(QUIET)
    turn.rest.post(THEIRS, QUIET, ALICE, "keep this")
    turn.rest.post(OWN, LOUNGE, ALICE, "elsewhere")

    pinned = await turn.call("pin_message", message_id=THEIRS)
    unpinned = await turn.call("pin_message", message_id=THEIRS, remove=True)
    elsewhere = await turn.call("pin_message", message_id=OWN)

    assert pinned == {"ok": True, "chat_id": QUIET, "message_id": THEIRS, "pinned": True}
    assert unpinned["pinned"] is False and turn.rest.posts[THEIRS]["is_pinned"] is False
    assert elsewhere["error"] == "message_not_found"


async def test_a_pin_past_the_edit_limit_is_refused_with_the_limit(turn):
    turn.came_from(QUIET)
    turn.rest.post(THEIRS, QUIET, ALICE, "old")
    turn.rest.routes[("POST", f"/posts/{THEIRS}/pin")] = TIME_LIMIT

    refused = await turn.call("pin_message", message_id=THEIRS)

    assert refused["error"] == "edit_time_limit_passed" and "its pin included" in refused["detail"]


async def test_cross_channel_pin_requires_requester_and_checks_message_location(turn):
    turn.came_from(PAIR)
    turn.rest.post(THEIRS, QUIET, ALICE, "keep")
    question = await turn.tools.widening_question("pin_message", {"message_id": THEIRS, "chat_id": QUIET})
    turn.metadata["mattermost_user_id"] = ""

    refused = await turn.call("pin_message", message_id=THEIRS, chat_id=QUIET)
    wrong_channel = await turn.call("pin_message", message_id=THEIRS, chat_id=LOUNGE, remove=True)

    assert "@bob" in question
    assert refused["error"] == "write_target_widens_audience"
    assert wrong_channel["error"] == "message_not_found"
    assert not turn.rest.posts[THEIRS].get("is_pinned", False)


# ── send_file ────────────────────────────────────────────────────────────


async def test_a_file_is_uploaded_into_the_thread_and_shared_in_one_post(turn):
    turn.came_from(QUIET)
    turn.rest.post(THEIRS, QUIET, ALICE, "root")

    sent = await turn.call("send_file", filename="notes.md", content="# Notes\n", reply_to_message_id=THEIRS, comment="here")

    (upload,) = turn.rest.calls("POST", "/files")
    assert upload.body == {"channel_id": QUIET, "filename": "notes.md", "bytes": 8}
    assert upload.headers["authorization"] == f"Bearer {TOKEN}"
    post = turn.rest.posts[sent["message_id"]]
    assert (post["root_id"], post["message"], post["file_ids"]) == (THEIRS, "here", [sent["file_id"]])
    assert turn.rest.files[sent["file_id"]][1] == b"# Notes\n"
    assert (sent["chat_id"], sent["filename"], sent["size_bytes"]) == (QUIET, "notes.md", 8)


async def test_post_can_reference_an_existing_authorized_file(turn):
    turn.came_from(QUIET)
    uploaded = await turn.call("send_file", filename="notes.md", content="notes")

    result = await turn.call("post_message", text="Reuse these notes", file_ids=[uploaded["file_id"]])

    assert result["ok"]
    assert turn.rest.posts[result["message_id"]]["file_ids"] == [uploaded["file_id"]]
    assert len(turn.rest.calls("POST", "/files")) == 1


async def test_file_upload_budget_accepts_exact_size_and_refuses_one_more(turn):
    budgeted = Turn(turn.rest, MattermostSettings(URL, TOKEN, file_upload_max_bytes=8))
    await budgeted.connector.start(lambda record: None)
    budgeted.came_from(QUIET)

    exact = await budgeted.call("send_file", filename="exact.txt", content="12345678")
    oversized = await budgeted.call("send_file", filename="large.txt", content="123456789")

    assert exact["ok"] is True
    assert oversized["error"] == "file_over_size_limit"
    assert "file_upload_max_bytes (8 bytes)" in oversized["detail"]
    assert len(turn.rest.calls("POST", "/files")) == 1
    await budgeted.connector.stop()


async def test_a_file_share_with_no_server_post_id_is_not_reported_as_success(turn):
    turn.came_from(QUIET)
    turn.rest.routes[("POST", "/posts")] = (201, {})

    result = await turn.call("send_file", filename="notes.md", content="# Notes\n")

    assert result["error"] == "bad_response" and "message_id" not in result
    assert result["orphaned_file_ids"] == [result["file_id"]]


async def test_a_file_of_this_conversation_is_sent_again_by_its_id_under_its_own_name(turn):
    turn.came_from(QUIET)
    turn.rest.file("f" * 26, QUIET, "plan.pdf", b"%PDF-1.4", mime="application/pdf")

    sent = await turn.call("send_file", file_id="f" * 26)

    assert sent["filename"] == "plan.pdf" and turn.rest.files[sent["file_id"]][1] == b"%PDF-1.4"


async def test_a_file_given_in_no_usable_form_or_over_the_server_s_limit_is_refused_with_the_reason(turn):
    turn.came_from(QUIET)
    turn.rest.client_config["MaxFileSize"] = "4"

    both = await turn.call("send_file", filename="a.txt", content="x", content_base64="eA==")
    over = await turn.call("send_file", filename="a.bin", content_base64=base64.b64encode(b"12345").decode())
    outside = await turn.call("send_file", filename="a.txt", content="x", chat_id=OUTSIDE)

    assert both["error"] == "file_source_required"
    assert over["error"] == "too_large" and "at most 4 bytes in one file (MaxFileSize)" in over["detail"]
    assert outside["error"] == "forbidden" and "member" in outside["detail"]
    assert not [post for post in turn.rest.posts.values() if post.get("file_ids")]


async def test_a_file_that_reaches_new_readers_is_put_to_the_requester_and_refused_without_one(turn):
    turn.came_from(PAIR)

    question = await turn.tools.widening_question("send_file", {"filename": "a.txt", "content": "x", "chat_id": QUIET})
    turn.metadata["mattermost_user_id"] = ""
    unattended = await turn.call("send_file", filename="a.txt", content="x", chat_id=QUIET)

    assert "@bob" in question
    assert unattended["error"] == "write_target_widens_audience"
    assert turn.rest.calls("POST", "/files") == []


async def test_the_client_reads_a_refusal_the_server_sends_before_the_body_ends():
    """A server that refuses an upload from its headers closes the connection without reading the body."""

    async def refuse_early(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        seen.append(head)
        body = b'{"id": "api.file.upload_file.too_large_detailed.app_error", "status_code": 413}'
        writer.write(
            b"HTTP/1.1 413 Request Entity Too Large\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        await writer.drain()
        writer.close()

    seen: list[bytes] = []
    server = await asyncio.start_server(refuse_early, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = MattermostClient(f"http://127.0.0.1:{port}", TOKEN)
    try:
        refused = None
        try:
            await client.upload("c" * 26, "big.bin", b"x" * (8 * 1024 * 1024))
        except MattermostError as exc:
            refused = exc
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert refused is not None
    assert (refused.code, refused.status, refused.server_id) == (
        "too_large", 413, "api.file.upload_file.too_large_detailed.app_error"
    )
    assert b"expect: 100-continue" in seen[0].lower()
    assert json.dumps(str(refused)).count(TOKEN) == 0


async def test_the_client_sends_the_body_when_the_server_reads_it_before_it_answers(monkeypatch):
    """A server that never answers 100 Continue gets the body after a wait and its refusal is read."""
    monkeypatch.setattr(client_module, "CONTINUE_WAIT_SECONDS", 0.05)

    async def refuse_after_body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(int(line.split(b":")[1]) for line in head.lower().split(b"\r\n") if line.startswith(b"content-length:"))
        seen.append((head, await reader.readexactly(length)))
        body = b'{"id": "api.context.permissions.app_error", "status_code": 403}'
        writer.write(
            b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        await writer.drain()
        writer.close()

    seen: list[tuple[bytes, bytes]] = []
    server = await asyncio.start_server(refuse_after_body, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = MattermostClient(f"http://127.0.0.1:{port}", TOKEN)
    try:
        with pytest.raises(MattermostError) as refused:
            await asyncio.wait_for(client.upload("c" * 26, "small.txt", b"hello"), 5)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    assert (refused.value.code, refused.value.status) == ("forbidden", 403)
    assert b"expect: 100-continue" in seen[0][0].lower()
    assert seen[0][1] == b"hello"
