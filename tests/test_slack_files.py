"""Slack files on the connector: a file read into a turn, and a file sent out, on the fake transport.

A turn of the generic host shares no directory with the process that holds
the Slack connection. ``download_slack_file`` therefore returns a text
file's content in its result, and ``send_slack_file`` takes the content in
the call or the id of a Slack file the conversation may read. The gate a
file is read through, and the rule a file is sent under, are those of the
history and post toolkits, checked where they live.
"""

import base64
import json
import logging
from pathlib import Path

import pytest
from conformance_kit import ModelTurn
from slack_transport import BOT_TOKEN, BOT_USER, button, click_body
from test_slack_connector import ASKER, BYSTANDER, OTHER_ROOM, ROOM, TS, _message, build_world, slack  # noqa: F401

from belle_agent.backend import ContentPart, ToolContent
from belle_agent.platforms.slack import files as slack_files
from belle_agent.platforms.slack.history import SlackHistoryToolkit
from belle_agent.tools.host import APPROVE, AWAITING_CONFIRMATION, REJECT

SENDING = """
platforms: {slack: {enforce_audience: true, send_file_allowed: true}}
scopes:
  - match: {channel: slack}
    delivery: {mode: [all]}
"""
NOT_SENDING = SENDING.replace(", send_file_allowed: true", "")

NOTES = "F0NOTES001"
PICTURE = "F0PICTURE1"
ELSEWHERE = "F0ELSEWHR1"
PRIVATE_ROOM = "C0PRIVATE9"


def _record(file_id: str, name: str, mimetype: str, body: bytes, shared_in: str) -> dict:
    return {
        "id": file_id, "name": name, "title": name, "mimetype": mimetype, "filetype": name.rsplit(".", 1)[-1],
        "user": ASKER, "size": len(body), "created": 1710000000,
        "url_private_download": f"https://files.slack.com/files-pri/T1-{file_id}/download/{name}",
        "permalink": f"https://synthetic-workspace.slack.com/files/{ASKER}/{file_id}/{name}",
        "shares": {"private": {shared_in: [{"ts": "1710000000.000200"}]}},
    }


@pytest.fixture
def bodies(monkeypatch) -> "_Bodies":
    """What Slack's file host holds, by URL, served in place of the byte transfer. Each fetch is recorded."""
    held: dict[str, bytes] = {}
    fetched: list[str] = []

    async def stream(self, url: str, *, mimetype: str, destination: Path) -> int:
        assert self._bot_token == BOT_TOKEN and url.startswith("https://files.slack.com/")
        fetched.append(url)
        destination.write_bytes(held[url])
        return len(held[url])

    monkeypatch.setattr(SlackHistoryToolkit, "_stream_url_to_disk", stream)
    return _Bodies(held, fetched)


class _Bodies:
    def __init__(self, held: dict[str, bytes], fetched: list[str]) -> None:
        self.held, self.fetched = held, fetched

    def share(self, web, file_id: str, name: str, mimetype: str, body: bytes, shared_in: str = ROOM) -> None:
        record = _record(file_id, name, mimetype, body, shared_in)
        web.files[file_id] = record
        self.held[record["url_private_download"]] = body


async def _world(build_world, bodies, settings: str = SENDING):  # noqa: F811
    world = await build_world(settings)
    web = world.platform.web
    web.members.update({ROOM: [ASKER, BOT_USER], OTHER_ROOM: [ASKER, BYSTANDER, BOT_USER], PRIVATE_ROOM: [BYSTANDER, BOT_USER]})
    bodies.share(web, NOTES, "notes.txt", "text/plain", b"line one\nline two\n")
    bodies.share(web, PICTURE, "chart.png", "image/png", b"\x89PNG\r\n\x1a\n\x00\x00binary")
    bodies.share(web, ELSEWHERE, "secret.txt", "text/plain", b"not for this room\n", shared_in=PRIVATE_ROOM)
    return world, web


async def _turn(world, *calls, ts: str = TS, raw: bool = False) -> list:
    """Run one turn that makes ``calls``, and return each call's result: parsed, or as the backend is handed it."""
    world.kit.results.clear()
    world.kit.play(ModelTurn(final="done", calls=calls))
    await world.platform.app.deliver(_message("files please", ts=ts))
    await world.host.idle()
    return list(world.kit.results) if raw else [json.loads(result) for result in world.kit.results]


def _history(world):
    """The toolkit that serves ``download_slack_file``."""
    tools = world.host.tools._served["slack"].values()
    return next(method for _, method, _ in tools if method.__name__ == "download_slack_file").__self__


# ── a file read into a turn ──────────────────────────────────────────────


async def test_a_text_file_shared_in_the_conversation_comes_back_as_content(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)

    (read,) = await _turn(world, ("download_slack_file", {"file_id": NOTES}))

    assert read["ok"] is True and read["content"] == "line one\nline two\n"
    assert (read["name"], read["mimetype"], read["chat_id"]) == ("notes.txt", "text/plain", ROOM)
    # The model is given no path, and this process keeps no copy once the call has answered.
    assert "path" not in read
    assert list(Path(_history(world)._scratch.name).iterdir()) == []
    assert bodies.fetched == [web.files[NOTES]["url_private_download"]]


async def test_an_image_comes_back_as_content_beside_the_result(build_world, bodies, caplog):  # noqa: F811
    world, web = await _world(build_world, bodies)
    picture = bodies.held[web.files[PICTURE]["url_private_download"]]

    with caplog.at_level(logging.DEBUG):
        (result,) = await _turn(world, ("download_slack_file", {"file_id": PICTURE}), raw=True)

    # The backend receives the bytes with their type, and the result gives no path.
    assert isinstance(result, ToolContent)
    assert result.parts == (ContentPart(mime="image/png", data=picture, name="chart.png"),)
    said = json.loads(result.text)
    assert (said["ok"], said["mimetype"], said["chat_id"]) == (True, "image/png", ROOM)
    assert "path" not in said and "content" not in said
    # This process keeps no copy, and no log line holds the content in any spelling.
    assert list(Path(_history(world)._scratch.name).iterdir()) == []
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert base64.b64encode(picture).decode() not in logged and "binary" not in logged and "chart.png" in logged


async def test_an_image_is_not_held_to_the_budget_of_a_text(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies, SENDING.replace("send_file_allowed: true", "history_max_total_chars: 1000"))
    bodies.share(web, "F0BIGPIC01", "big.png", "image/png", b"\x89PNG\r\n\x1a\n\x00" + b"\xfe" * 5000)

    (result,) = await _turn(world, ("download_slack_file", {"file_id": "F0BIGPIC01"}), raw=True)

    assert isinstance(result, ToolContent) and len(result.parts[0].data) == 5009


async def test_a_type_the_backend_does_not_hand_to_its_model_is_refused_with_what_it_takes(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)
    bodies.share(web, "F0REPORT01", "report.pdf", "application/pdf", b"%PDF-1.4\n\x00\xff binary")

    (refused,) = await _turn(world, ("download_slack_file", {"file_id": "F0REPORT01"}))

    assert (refused["ok"], refused["error"]) == (False, "tool_content_not_passed")
    assert "application/pdf" in refused["detail"] and "image/*" in refused["detail"]
    # The file's permalink is still given, which is how a person opens it.
    file_record = json.loads(refused["result_without_content"])
    assert file_record["permalink"].endswith("report.pdf")
    assert "content_attached" not in file_record


async def test_an_image_the_conversation_may_not_read_is_refused_by_the_gate_first(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)
    bodies.share(web, "F0HIDDEN01", "hidden.png", "image/png", b"\x89PNG\x00hidden", shared_in=PRIVATE_ROOM)

    refused, control = await _turn(
        world, ("download_slack_file", {"file_id": "F0HIDDEN01"}), ("download_slack_file", {"file_id": PICTURE}), raw=True
    )

    assert json.loads(refused)["error"] == "source_members_cannot_read_file"
    # Only the image shared here was fetched, and only it came back as content.
    assert isinstance(control, ToolContent) and len(bodies.fetched) == 1 and PICTURE in bodies.fetched[0]


async def test_a_file_the_conversation_may_not_read_is_refused_before_any_byte_moves(build_world, bodies):  # noqa: F811
    world, _ = await _world(build_world, bodies)

    refused, control = await _turn(
        world, ("download_slack_file", {"file_id": ELSEWHERE}), ("download_slack_file", {"file_id": NOTES})
    )

    assert refused["error"] == "source_members_cannot_read_file" and "content" not in refused
    # The refusal is an absent fetch beside a present one: only the file shared here was fetched.
    assert control["ok"] is True and len(bodies.fetched) == 1 and NOTES in bodies.fetched[0]


async def test_a_text_longer_than_one_result_may_hold_is_refused_whole(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies, SENDING.replace("send_file_allowed: true", "history_max_total_chars: 1000"))
    bodies.share(web, "F0LONG0001", "long.txt", "text/plain", b"x" * 1001)

    (refused,) = await _turn(world, ("download_slack_file", {"file_id": "F0LONG0001"}))

    assert refused["error"] == "slack_file_text_over_limit" and "1000" in refused["detail"] and "content" not in refused


# ── a file sent out ──────────────────────────────────────────────────────


async def test_content_the_model_passes_is_uploaded_where_the_turn_came_from(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)

    (sent,) = await _turn(
        world,
        ("send_slack_file", {"filename": "report.md", "content": "# Report\n", "reply_to_message_id": TS, "comment": "As asked."}),
    )

    assert sent == {"ok": True, "chat_id": ROOM, "file_id": "F0UPLOAD01", "filename": "report.md", "size_bytes": 9}
    assert web.uploads == [
        {"channel": ROOM, "content": b"# Report\n", "filename": "report.md", "thread_ts": TS, "initial_comment": "As asked."}
    ]


async def test_multi_file_upload_checks_sources_and_preserves_native_options(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)

    (sent,) = await _turn(world, ("send_slack_file", {"file_uploads": [
        {"filename": "report.md", "content": "# Report", "title": "Report", "snippet_type": "markdown"},
        {"file_id": PICTURE, "alt_text": "A chart"},
    ]}))

    assert sent["ok"] and len(sent["file_ids"]) == 2
    assert web.uploads[0]["file_uploads"][0] == {
        "filename": "report.md", "content": b"# Report", "title": "Report", "snippet_type": "markdown",
    }
    assert web.uploads[0]["file_uploads"][1]["filename"] == "chart.png"
    assert web.uploads[0]["file_uploads"][1]["alt_txt"] == "A chart"

    (refused,) = await _turn(world, ("send_slack_file", {"file_uploads": [
        {"filename": "safe.txt", "content": "safe"}, {"file_id": ELSEWHERE},
    ]}), ts="1700000000.000777")
    assert refused["error"] == "source_members_cannot_read_file"
    assert len(web.uploads) == 1


async def test_bytes_the_model_produced_are_uploaded_from_base64(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)
    data = bytes(range(256))

    sent, malformed = await _turn(
        world,
        ("send_slack_file", {"filename": "table.bin", "content_base64": base64.b64encode(data).decode()}),
        ("send_slack_file", {"filename": "table.bin", "content_base64": "not base64!"}),
    )

    assert sent["ok"] is True and sent["size_bytes"] == 256
    assert malformed["error"] == "content_base64_malformed"
    assert [upload["content"] for upload in web.uploads] == [data]


async def test_a_slack_file_the_conversation_may_read_is_sent_as_a_copy(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)

    picture, refused = await _turn(
        world, ("send_slack_file", {"file_id": PICTURE}), ("send_slack_file", {"file_id": ELSEWHERE})
    )

    # A file that cannot be read as text can still be passed on: its bytes go from Slack to Slack.
    assert (picture["ok"], picture["filename"]) == (True, "chart.png")
    assert refused["error"] == "source_members_cannot_read_file"
    assert [(upload["channel"], upload["filename"]) for upload in web.uploads] == [(ROOM, "chart.png")]
    assert web.uploads[0]["content"].startswith(b"\x89PNG")


async def test_a_file_in_the_models_own_workspace_is_out_of_reach_and_the_tool_says_so(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)

    by_path, nothing, both = await _turn(
        world,
        ("send_slack_file", {"filename": "out.csv", "path": "/workspace/out.csv"}),
        ("send_slack_file", {"filename": "out.csv"}),
        ("send_slack_file", {"filename": "out.csv", "content": "a,b\n", "file_id": NOTES}),
    )

    assert by_path["error"] == "file_path_unreachable" and "shares no directory" in by_path["detail"]
    assert nothing["error"] == both["error"] == "file_source_required"
    assert web.uploads == []


async def test_a_file_over_the_size_limit_is_not_uploaded(build_world, bodies, monkeypatch):  # noqa: F811
    world, web = await _world(build_world, bodies)
    monkeypatch.setattr(slack_files, "MAX_FILE_BYTES", 8)

    over, control = await _turn(
        world,
        ("send_slack_file", {"filename": "big.txt", "content": "123456789"}),
        ("send_slack_file", {"filename": "small.txt", "content": "12345678"}),
    )

    assert over["error"] == "file_over_size_limit"
    assert control["ok"] is True and [upload["filename"] for upload in web.uploads] == ["small.txt"]


async def test_the_tool_is_offered_only_where_the_operator_allows_it(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies, NOT_SENDING)

    (refused,) = await _turn(world, ("send_slack_file", {"filename": "report.md", "content": "# Report\n"}))

    assert refused["error"] == "unknown_tool" and web.uploads == []
    names = {definition["name"] for definition in world.host.tools.definitions("")}
    assert "send_slack_file" not in names and "download_slack_file" in names


async def test_a_file_sent_to_new_readers_is_put_to_a_person_first(build_world, bodies):  # noqa: F811
    world, web = await _world(build_world, bodies)
    elsewhere = ("send_slack_file", {"filename": "report.md", "content": "# Report\n", "chat_id": OTHER_ROOM})

    (waiting,) = await _turn(world, elsewhere)

    assert waiting["error"] == AWAITING_CONFIRMATION and BYSTANDER in waiting["detail"]
    assert web.uploads == []
    question = next(payload for payload in web.payloads("chat_postMessage") if payload.get("blocks"))
    ts = next(sent_ts for _, payload, sent_ts in web.sent if payload is question)
    body = click_body(user=ASKER, channel=ROOM, message_ts=ts, payload=question)

    assert (await world.platform.app.click(body, button(question["blocks"], value=APPROVE))).startswith("answered")

    assert [(upload["channel"], upload["filename"]) for upload in web.uploads] == [(OTHER_ROOM, "report.md")]

    # Control: the same file, refused by the person, is not sent.
    await _turn(world, elsewhere, ts="1710000009.000100")
    question = [payload for payload in web.payloads("chat_postMessage") if payload.get("blocks")][-1]
    ts = next(sent_ts for _, payload, sent_ts in web.sent if payload is question)
    body = click_body(user=ASKER, channel=ROOM, message_ts=ts, payload=question)
    assert (await world.platform.app.click(body, button(question["blocks"], value=REJECT))).startswith("answered")
    assert len(web.uploads) == 1
