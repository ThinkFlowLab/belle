"""What the Slack connector logs when a file is loaded or read, and when a turn's card is posted and settled.

A live run is confirmed from these lines. Each check reads them from the
logger that writes them, and holds that no line carries a file's content.
"""

import contextlib
import logging

from test_slack_connector import ASKER, ROOM, TS, _message, build_world, connected, slack  # noqa: F401
from test_slack_files import ELSEWHERE, NOTES, _turn, _world, bodies  # noqa: F401
from test_slack_turn_surfaces import CARD, _asked, _called, _cards, _result, clock  # noqa: F401

from belle_agent.platforms.base import MARK_DONE
from belle_agent.platforms.slack import connector
from belle_agent.platforms.slack.file_transfer import FILE_REASON_DOWNLOAD_FAILED, SlackAttachmentError


@contextlib.contextmanager
def _lines(caplog, name: str):
    """The lines one logger writes, read from that logger itself: it may not hand them up."""
    logger = logging.getLogger(name)
    previous = logger.level, logger.propagate
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield lambda: [(record.levelname, record.getMessage()) for record in caplog.records if record.name == name]
    finally:
        logger.removeHandler(caplog.handler)
        logger.level, logger.propagate = previous


async def test_a_file_loaded_into_a_turn_is_logged_and_one_that_is_not_says_why(connected, monkeypatch, caplog):  # noqa: F811
    platform, socket, received = await connected()

    async def fetch(url, mimetype, *, bot_token, allowed_hosts):
        if "broken" in url:
            raise SlackAttachmentError("HTTP 500", FILE_REASON_DOWNLOAD_FAILED)
        return bytearray(b"# Notes\n")

    monkeypatch.setattr(connector, "fetch_attachment_bytes", fetch)
    files = [
        {"id": "F0001", "name": "notes.md", "mimetype": "text/markdown", "url_private": "https://files.slack.com/f/notes.md"},
        {"id": "F0002", "name": "broken.pdf", "mimetype": "application/pdf", "url_private": "https://files.slack.com/f/broken.pdf"},
    ]
    await socket.app.deliver(_message("two files", subtype="file_share", files=files))

    with _lines(caplog, "belle_agent.platforms.slack.connector") as lines:
        await platform.fetch_files(received[0])

    assert ("INFO", f"slack file: loaded notes.md (8 bytes) from {ROOM} file_id=F0001") in lines()
    assert ("WARNING", f"slack file: not loaded broken.pdf from {ROOM} reason={FILE_REASON_DOWNLOAD_FAILED}") in lines()
    assert "# Notes" not in caplog.text


async def test_a_file_read_by_the_tool_is_logged_and_one_that_is_refused_says_why(build_world, bodies, caplog):  # noqa: F811
    world, _ = await _world(build_world, bodies)

    with _lines(caplog, "belle_agent.platforms.slack.files") as lines:
        await _turn(world, ("download_slack_file", {"file_id": NOTES}), ("download_slack_file", {"file_id": ELSEWHERE}))

    assert lines() == [
        ("INFO", f"slack file: read notes.txt (18 bytes) in {ROOM} file_id={NOTES}"),
        ("WARNING", f"slack file: not read file_id={ELSEWHERE} in {ROOM} reason=source_members_cannot_read_file"),
    ]
    assert "line one" not in caplog.text


async def test_a_card_is_logged_when_it_is_posted_and_when_it_is_settled(connected, clock, caplog):  # noqa: F811
    platform, socket, _ = await connected(CARD)
    web, asked = socket.web, _asked()

    with _lines(caplog, "belle_agent.platforms.slack.turn_activity") as lines:
        await platform.progress(asked, _called("react_to_message", "c1"))
        await platform.progress(asked, _result("react_to_message", "c1", '{"ok": true}'))
        await clock.advance(5)
        (card,) = _cards(web)
        await platform.conclude(asked, MARK_DONE)

    assert lines() == [
        ("INFO", f"slack activity card: posted conversation={ROOM} ts={card['ts']}"),
        ("INFO", f"slack activity card: settled conversation={ROOM} ts={card['ts']} state={MARK_DONE} card=kept"),
    ]
    assert ASKER not in caplog.text and TS not in caplog.text
