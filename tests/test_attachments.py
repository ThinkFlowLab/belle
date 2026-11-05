"""The shared loading of a message's files: one deadline, and a reason for each file left out."""

import asyncio
import json
import logging

import pytest

from belle_agent.attachments import (
    FILE_REASON_TIMED_OUT,
    AttachmentUnreadable,
    FileSourceRefused,
    FileTextOverLimit,
    fetch_message_files,
    file_content,
    load_attachments,
    outgoing_file,
    unreadable_file_notice,
)
from belle_agent.backend import ToolContent

FAILED = "the download failed"


async def _load(files, download, budget=5.0):
    return await load_attachments(
        files, download, name=lambda info: info["name"], budget_seconds=budget,
        logger=logging.getLogger("test"), label="file", failed_reason=FAILED,
    )


async def test_each_file_left_out_has_the_reason_its_failure_gives():
    async def download(info):
        if info["name"] == "refused.pdf":
            raise AttachmentUnreadable("HTTP 403", "the server refused the download")
        if info["name"] == "broken.png":
            raise OSError("connection reset")
        return {"name": info["name"]}

    loaded, failures = await _load([{"name": "a.md"}, {"name": "refused.pdf"}, {"name": "broken.png"}], download)

    assert loaded == [{"name": "a.md"}]
    assert failures == [("refused.pdf", "the server refused the download"), ("broken.png", FAILED)]


async def test_a_file_past_the_deadline_is_left_out_as_timed_out_and_the_next_one_too():
    async def download(info):
        await asyncio.sleep(1)
        return {"name": info["name"]}

    loaded, failures = await _load([{"name": "slow.bin"}, {"name": "next.bin"}], download, budget=0.05)

    assert loaded == []
    assert failures == [("slow.bin", FILE_REASON_TIMED_OUT), ("next.bin", FILE_REASON_TIMED_OUT)]


async def _fetch(files, download, notify):
    return await fetch_message_files(
        files, download, name=lambda info: info["name"], budget_seconds=5.0, logger=logging.getLogger("test"),
        label="Some file", failed_reason=FAILED, conversation="C1", notify=notify,
    )


async def test_a_message_s_files_come_back_and_its_sender_is_told_once_for_each_reason(caplog):
    told = []

    async def notify(notice):
        told.append(notice)

    async def download(info):
        if info["name"].startswith("big"):
            raise AttachmentUnreadable("too big", "the file is too large")
        if info["name"] == "broken.png":
            raise OSError("connection reset")
        return {"name": info["name"]}

    names = ["a.md", "big1.bin", "broken.png", "big2.bin"]
    with caplog.at_level(logging.WARNING, logger="test"):
        loaded = await _fetch([{"name": name} for name in names], download, notify)

    assert loaded == ({"name": "a.md"},)
    assert told == [
        unreadable_file_notice(["big1.bin", "big2.bin"], "the file is too large"),
        unreadable_file_notice(["broken.png"], FAILED),
    ]
    assert "some file: not loaded big1.bin from C1 reason=the file is too large" in caplog.messages


async def test_a_message_whose_files_all_load_tells_its_sender_nothing():
    told = []

    async def notify(notice):
        told.append(notice)

    async def download(info):
        return {"name": info["name"]}

    assert await _fetch([{"name": "a.md"}, {"name": "b.md"}], download, notify) == ({"name": "a.md"}, {"name": "b.md"})
    assert told == []


def test_the_notice_names_at_most_five_files_and_counts_the_rest():
    notice = unreadable_file_notice([f"f{index}" for index in range(7)], "the download failed")

    assert notice == "Received 7 attachments (f0, f1, f2, f3, f4 (+2 more)) but could not read them: the download failed."


def test_a_text_file_comes_back_as_text_and_any_other_file_beside_its_record():
    record = {"name": "notes.md", "mimetype": "text/markdown"}

    assert json.loads(file_content(record, b"# Notes\n", name="notes.md", max_chars=100)) == {
        **record, "content": "# Notes\n"
    }
    picture = file_content({"name": "a.png", "mimetype": "image/png"}, b"\x89PNG\x00", name="a.png", max_chars=100)
    assert isinstance(picture, ToolContent) and json.loads(picture.text) == {"name": "a.png", "mimetype": "image/png"}
    assert [(part.mime, part.data, part.name) for part in picture.parts] == [("image/png", b"\x89PNG\x00", "a.png")]


def test_a_text_longer_than_a_result_holds_is_refused_whole():
    with pytest.raises(FileTextOverLimit) as over:
        file_content({"name": "long.txt"}, b"x" * 11, name="long.txt", max_chars=10)

    assert over.value.chars == 11


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ((None, None, None, "a.txt"), "file_source_required"),
        (("text", None, "F1", "a.txt"), "file_source_required"),
        (("text", None, None, ""), "filename_required"),
        ((None, "not base64!", None, "a.bin"), "content_base64_malformed"),
    ],
)
def test_a_file_to_send_given_in_no_usable_form_is_refused_with_its_reason(arguments, code):
    with pytest.raises(FileSourceRefused) as refused:
        outgoing_file(*arguments)
    assert refused.value.code == code


def test_a_file_to_send_is_its_text_its_decoded_bytes_or_a_file_named_by_id():
    assert outgoing_file("héllo", None, None, "a.txt") == "héllo".encode()
    assert outgoing_file(None, "AAE=", None, "a.bin") == b"\x00\x01"
    assert outgoing_file(None, None, "F1", "") is None
    assert outgoing_file("", None, None, "empty.txt", allow_empty=True) == b""


async def test_a_log_prefix_names_the_files_left_out_apart_from_the_loader_s_label(caplog):
    async def notify(notice):
        pass

    async def download(info):
        raise AttachmentUnreadable("no", "the server refused the download")

    with caplog.at_level(logging.WARNING, logger="test"):
        await fetch_message_files(
            [{"name": "a.pdf"}], download, name=lambda info: info["name"], budget_seconds=5.0,
            logger=logging.getLogger("test"), label="Some attachment", failed_reason=FAILED,
            conversation="C1", notify=notify, log_prefix="some file",
        )

    assert caplog.messages == [
        "Some attachment 'a.pdf' not read: no",
        "some file: not loaded a.pdf from C1 reason=the server refused the download",
    ]
