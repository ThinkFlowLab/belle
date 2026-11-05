"""Unit tests for the Slack connector's Block Kit affordance.

Kept out of ``test_slack_channel.py`` so the Block Kit surface can be read, and
reverted, on its own.
"""

import json
import logging
import time
from typing import Any

import pytest
from belle_agent.platforms.slack import delivery as slack_delivery
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack import config as slack_config
from belle_agent.platforms.slack import streaming as slack_streaming
from belle_agent.platforms.slack.config import resolve_blockkit_tables_mode

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
    SlackDeliveryError,
    resolve_render_tables,
)

MARKER = slack_streaming._SLACK_BLOCKS_MARKER
OFF_MARKER = slack_streaming._SLACK_BLOCKS_OFF_MARKER

TABLE = "\n".join(
    [
        "| Name | Count |",
        "| --- | --- |",
        "| alpha | 1 |",
        "| beta | 2 |",
    ]
)


def _channel(**settings) -> SlackChannel:
    return SlackChannel(
        SlackChannelConfig(enabled=True, **settings), RobotMessageRouter()
    )


def _message(content: str) -> Message:
    return Message(
        id="response-1",
        type="event",
        channel_id="slack",
        session_id="slack_T1_C1_1710000000.000100",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": content},
        event_type=EventType.CHAT_FINAL,
        metadata={"slack_channel_id": "C1"},
    )


class _RecordingClient:
    """Fake Slack client capturing every posted or edited payload."""

    def __init__(self, *, fail_from: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_from = fail_from

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        if self._fail_from is not None and len(self.calls) > self._fail_from:
            raise RuntimeError("channel_not_found")
        return {"ts": f"1710000099.{len(self.calls):06d}"}

    chat_update = chat_postMessage


async def _send(channel: SlackChannel, content: str) -> _RecordingClient:
    client = _RecordingClient()
    channel._client = client
    await channel.send(_message(content))
    return client


# --- the operator-level policy -------------------------------------------


def test_the_default_mode_renders_a_table_without_being_asked() -> None:
    assert SlackChannelConfig().blockkit_tables == slack_connect.BLOCKKIT_TABLES_AUTO
    assert _channel()._blockkit_tables_mode() == slack_connect.BLOCKKIT_TABLES_AUTO


def test_the_three_defaults_agree() -> None:
    """The dataclass, the raw-config reader and the runtime fallback are one value."""
    assert SlackChannelConfig().blockkit_tables == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert resolve_blockkit_tables_mode({}) == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert (
        _channel(blockkit_tables="nonsense")._blockkit_tables_mode()
        == slack_connect.BLOCKKIT_TABLES_DEFAULT
    )


def test_the_request_marker_is_named_nowhere_the_model_can_see_it() -> None:
    import pathlib

    import jiuwenswarm

    prompts = pathlib.Path(jiuwenswarm.__file__).resolve().parent / "agents"
    assert prompts.is_dir()
    naming_it = [
        path
        for path in prompts.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
        and slack_streaming._SLACK_BLOCKS_MARKER
        in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert naming_it == []


def test_every_documented_mode_resolves_to_itself() -> None:
    for mode in slack_connect.BLOCKKIT_TABLES_MODES:
        assert _channel(blockkit_tables=mode)._blockkit_tables_mode() == mode


def test_mode_is_case_and_whitespace_insensitive() -> None:
    assert (
        _channel(blockkit_tables="  AUTO ")._blockkit_tables_mode()
        == slack_connect.BLOCKKIT_TABLES_AUTO
    )


def test_an_unknown_mode_warns_and_falls_back_to_the_default(slack_logs) -> None:
    channel = _channel(blockkit_tables="sometimes")
    assert channel._blockkit_tables_mode() == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert "blockkit_tables" in slack_logs.text


def test_an_empty_mode_falls_back_without_warning(slack_logs) -> None:
    channel = _channel(blockkit_tables="")
    assert channel._blockkit_tables_mode() == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert "blockkit_tables" not in slack_logs.text


def test_the_mode_is_reported_in_the_channel_metadata() -> None:
    extra = _channel(blockkit_tables="auto").get_metadata().extra
    assert extra["blockkit_tables"] == slack_connect.BLOCKKIT_TABLES_AUTO


# --- which table block a table becomes ------------------------------------


def test_the_three_defaults_for_the_table_rendering_agree() -> None:
    """The dataclass, the raw-config reader and the runtime fallback are one value."""
    assert SlackChannelConfig().render_tables == slack_blocks.RENDER_TABLES_DEFAULT
    assert resolve_render_tables({}) == slack_blocks.RENDER_TABLES_DEFAULT
    assert _channel()._render_tables_mode() == slack_blocks.RENDER_TABLES_DEFAULT


def test_the_default_is_the_block_that_cannot_cost_a_reply_its_table() -> None:
    assert slack_blocks.RENDER_TABLES_DEFAULT == "data_table"


def test_every_documented_rendering_resolves_to_itself() -> None:
    for mode in slack_blocks.RENDER_TABLES_MODES:
        assert resolve_render_tables({"render_tables": mode}) == mode
        assert _channel(render_tables=mode)._render_tables_mode() == mode


def test_the_rendering_is_case_and_whitespace_insensitive() -> None:
    assert (
        resolve_render_tables({"render_tables": "  DATA_TABLE "})
        == slack_blocks.RENDER_TABLES_DATA
    )


def test_an_unquoted_off_survives_yaml_reading_it_as_false() -> None:
    assert (
        resolve_render_tables({"render_tables": False})
        == slack_blocks.RENDER_TABLES_OFF
    )
    assert (
        resolve_render_tables({"render_tables": True})
        == slack_blocks.RENDER_TABLES_DEFAULT
    )


def test_an_unknown_rendering_warns_and_falls_back(slack_logs) -> None:
    channel = _channel(render_tables="fancy")
    assert channel._render_tables_mode() == slack_blocks.RENDER_TABLES_DEFAULT
    assert "render_tables" in slack_logs.text


def test_an_empty_rendering_falls_back_without_warning(slack_logs) -> None:
    assert _channel(render_tables="")._render_tables_mode() == (
        slack_blocks.RENDER_TABLES_DEFAULT
    )
    assert "render_tables" not in slack_logs.text


def test_the_rendering_is_reported_in_the_channel_metadata() -> None:
    extra = _channel(render_tables="basic").get_metadata().extra
    assert extra["render_tables"] == slack_blocks.RENDER_TABLES_BASIC


@pytest.mark.asyncio
async def test_basic_keeps_a_long_table_a_plain_table() -> None:
    """The basic mode keeps a long table as plain table text."""
    rows = "\n".join(f"| r{index} | {index} |" for index in range(25))
    content = f"| Name | Count |\n| --- | --- |\n{rows}"
    client = await _send(_channel(render_tables="basic"), content)

    assert [block["type"] for block in client.calls[0]["blocks"]] == ["table"]


@pytest.mark.asyncio
async def test_data_table_makes_even_a_two_row_table_interactive() -> None:
    client = await _send(_channel(render_tables="data_table"), f"Totals:\n\n{TABLE}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        "data_table",
    ]


@pytest.mark.asyncio
async def test_off_posts_the_table_as_the_text_it_was_written_as() -> None:
    """No table block, and no other rendering either: the reply is plain text."""
    client = await _send(_channel(render_tables="off"), f"Totals:\n\n{TABLE}")

    assert client.calls[0].get("blocks") in (None, [])
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_off_is_about_tables_and_leaves_a_chart_alone() -> None:
    content = (
        "Totals:\n\n"
        f"{TABLE}\n\n"
        "```mermaid\npie title Split\n  \"a\" : 1\n  \"b\" : 2\n```\n"
    )
    client = await _send(_channel(render_tables="off"), content)

    types = [block["type"] for block in client.calls[0]["blocks"]]
    assert "data_visualization" in types
    assert "table" not in types
    assert "data_table" not in types
    assert "| alpha | 1 |" in json.dumps(client.calls[0]["blocks"])


@pytest.mark.asyncio
async def test_a_table_too_large_for_basic_is_posted_as_text() -> None:
    rows = "\n".join(
        f"| r{index} | {index} |"
        for index in range(slack_blocks.MAX_PLAIN_TABLE_ROWS + 5)
    )
    content = f"| Name | Count |\n| --- | --- |\n{rows}"

    plain = await _send(_channel(render_tables="basic"), content)
    assert plain.calls[0].get("blocks") in (None, [])
    assert "| r99 | 99 |" in plain.calls[0]["text"]

    interactive = await _send(_channel(render_tables="data_table"), content)
    assert [block["type"] for block in interactive.calls[0]["blocks"]] == [
        "data_table"
    ]


# --- what a hand-written fence may hold ----------------------------------


BUTTON_BLOCK = {
    "type": "section",
    "text": {"type": "mrkdwn", "text": "Approve this?"},
    "accessory": {
        "type": "button",
        "text": {"type": "plain_text", "text": "Approve"},
        "action_id": "forged_approve",
    },
}


def _blockkit_fence(payload: Any) -> str:
    return "```blockkit\n" + json.dumps(payload) + "\n```"


@pytest.mark.asyncio
async def test_a_fence_forwards_a_section_block() -> None:
    fence = _blockkit_fence({"type": "section", "text": {"type": "mrkdwn", "text": "hi"}})
    client = await _send(_channel(), f"Notes follow.\n\n{fence}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        "section",
    ]


@pytest.mark.asyncio
async def test_a_forged_approval_prompt_is_refused_by_default() -> None:
    """A section accessory cannot inject a button into a reply."""
    fence = _blockkit_fence(BUTTON_BLOCK)
    client = await _send(_channel(), f"Please confirm.\n\n{fence}")

    assert "blocks" not in client.calls[0]
    assert "forged_approve" in client.calls[0]["text"]


# --- the per-message affordance ------------------------------------------


# --- the send path -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prose_answer_sends_no_blocks_at_all() -> None:
    """The byte-identical requirement: an unrelated answer must not gain a key."""
    client = await _send(_channel(), "A plain answer with *bold* and a bullet.")

    assert len(client.calls) == 1
    assert client.calls[0] == {
        "channel": "C1",
        "text": "A plain answer with *bold* and a bullet.",
    }


@pytest.mark.asyncio
async def test_a_table_needs_no_marker_by_default() -> None:
    """Detection is on by default; writing a table is the request."""
    client = await _send(_channel(), f"Totals:\n\n{TABLE}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        slack_blocks.RENDER_TABLES_DEFAULT,
    ]
    # The text field still holds the whole answer, for notifications.
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_marker_mode_still_waits_to_be_asked() -> None:
    """The mode is kept, and still does nothing until something emits the marker."""
    client = await _send(_channel(blockkit_tables="marker"), f"Totals:\n\n{TABLE}")

    assert "blocks" not in client.calls[0]
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_the_marker_turns_a_table_into_blocks_with_a_text_fallback() -> None:
    client = await _send(_channel(), f"{MARKER}\n\n## Totals\n\n{TABLE}")

    call = client.calls[0]
    assert [block["type"] for block in call["blocks"]] == ["section", "data_table"]
    # The heading went through the existing normaliser, not a second one.
    assert call["blocks"][0]["text"] == {"type": "mrkdwn", "text": "*Totals*"}
    assert [[cell["text"] for cell in row] for row in call["blocks"][1]["rows"]] == [
        ["Name", "Count"],
        ["alpha", "1"],
        ["beta", "2"],
    ]
    # Short enough that the pager it just earned never appears, which is what
    # keeps the escalation from making a two-row table worse to read.
    assert call["blocks"][1]["page_size"] == 2

    # text is always supplied: it is the notification and the accessible copy.
    assert call["text"].startswith("*Totals*")
    assert "| alpha | 1 |" in call["text"]
    assert MARKER not in call["text"]


@pytest.mark.asyncio
async def test_the_marker_on_an_answer_with_no_table_changes_nothing() -> None:
    client = await _send(_channel(), f"{MARKER}\n\nNo table here.")

    assert "blocks" not in client.calls[0]
    assert client.calls[0]["text"] == "No table here."


@pytest.mark.asyncio
async def test_auto_mode_renders_a_table_nobody_asked_about() -> None:
    client = await _send(_channel(blockkit_tables="auto"), f"Totals:\n\n{TABLE}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        slack_blocks.RENDER_TABLES_DEFAULT,
    ]


@pytest.mark.asyncio
async def test_auto_mode_honours_a_per_message_opt_out() -> None:
    client = await _send(_channel(blockkit_tables="auto"), f"{OFF_MARKER}\n\n{TABLE}")

    assert "blocks" not in client.calls[0]
    assert OFF_MARKER not in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_off_mode_ignores_the_marker_but_still_strips_it() -> None:
    client = await _send(_channel(blockkit_tables="off"), f"{MARKER}\n\n{TABLE}")

    assert "blocks" not in client.calls[0]
    assert MARKER not in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_reply_that_is_only_a_marker_is_a_no_op() -> None:
    channel = _channel()
    client = _RecordingClient()
    channel._client = client

    await channel.send(_message(MARKER))

    assert client.calls == []


@pytest.mark.asyncio
async def test_a_delivery_that_used_blocks_says_so_in_the_log(slack_logs) -> None:
    await _send(_channel(), f"{MARKER}\n\n{TABLE}")

    lines = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno == logging.INFO
    ]
    assert lines == [
        "[SlackChannel] delivered text: channel=C1 ts=1710000099.000001 "
        "chunk=1/1 mode=post+blocks"
    ]


# --- limits on the send path ---------------------------------------------


@pytest.mark.asyncio
async def test_a_table_over_slacks_limits_is_delivered_as_text() -> None:
    """Blocks cannot be chunked, so the text path keeps the content intact."""
    rows = "\n".join(f"| r{index} | {index} |" for index in range(200))
    client = await _send(
        _channel(), f"{MARKER}\n| Name | Count |\n| --- | --- |\n{rows}"
    )

    assert len(client.calls) == 1
    assert "blocks" not in client.calls[0]
    assert "r199" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_every_chunk_of_a_split_reply_is_within_the_block_limits() -> None:
    filler = "\n\n".join(["prose " * 200] * 40)
    client = await _send(_channel(), f"{MARKER}\n\n{TABLE}\n\n{filler}\n\n{TABLE}")

    for call in client.calls:
        blocks = call.get("blocks")
        if blocks is None:
            continue
        assert len(blocks) <= slack_blocks.MAX_BLOCKS_PER_MESSAGE
        for block in blocks:
            if block["type"] == "section":
                assert (
                    len(block["text"]["text"])
                    <= slack_blocks.MAX_SECTION_TEXT_LENGTH
                )


# --- one budget per message ----------------------------------------------


def _wide_table(tag: str, rows: int = 100, width: int = 110) -> str:
    """A table whose cells total well under Slack's per-message character cap."""
    body = "\n".join(f"| {tag}{index:03d}{'.' * width} | {index} |" for index in range(rows))
    return f"| Name | Count |\n| --- | --- |\n{body}"


def test_the_two_tables_are_each_under_the_cap_and_over_it_together() -> None:
    """The premise of the two tests below, asserted rather than assumed."""
    one = slack_blocks.render_blocks(_wide_table("a"))
    assert one is not None, "a single table must render, or the pair proves nothing"
    characters = sum(
        len(cell["text"])
        for block in one
        if block["type"] in {"table", "data_table"}
        for row in block["rows"]
        for cell in row
    )
    assert characters < slack_blocks.MAX_TABLE_CHARACTERS_PER_MESSAGE
    assert 2 * characters > slack_blocks.MAX_TABLE_CHARACTERS_PER_MESSAGE


@pytest.mark.asyncio
async def test_two_tables_in_one_message_lose_the_rendering_together() -> None:
    """The cliff: the budget is shared, so a breach degrades both tables at once."""
    client = await _send(_channel(), f"{_wide_table('a')}\n\n{_wide_table('b')}")

    assert len(client.calls) == 1
    assert "blocks" not in client.calls[0]
    assert "| a000" in client.calls[0]["text"] and "| b000" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_the_same_two_tables_render_once_a_boundary_separates_them() -> None:
    content = (
        f"brief\n{slack_streaming._SLACK_THREAD_DETAILS_MARKER}\n"
        f"{_wide_table('a')}\n{slack_streaming._SLACK_THREAD_DETAILS_MARKER}\n"
        f"{_wide_table('b')}"
    )
    client = await _send(_channel(), content)

    assert len(client.calls) == 3
    assert "blocks" not in client.calls[0]
    for call in client.calls[1:]:
        assert [block["type"] for block in call["blocks"]] == ["data_table"]


@pytest.mark.asyncio
async def test_a_boundary_does_not_rescue_a_single_table_over_the_row_limit() -> None:
    """The row limit is per table, so splitting the message cannot help it."""
    rows = "\n".join(f"| r{index} | {index} |" for index in range(200))
    table = f"| Name | Count |\n| --- | --- |\n{rows}"
    client = await _send(
        _channel(), f"brief\n{slack_streaming._SLACK_THREAD_DETAILS_MARKER}\n{table}"
    )

    assert len(client.calls) == 2
    assert "blocks" not in client.calls[1]
    assert "r199" in client.calls[1]["text"]


@pytest.mark.asyncio
async def test_a_ragged_table_is_delivered_rather_than_raising() -> None:
    ragged = "| A | B |\n| --- | --- |\n| only-one |\n| x | y | z |"
    client = await _send(_channel(), f"{MARKER}\n\n{ragged}")

    rows = [
        [cell["text"] for cell in row]
        for row in client.calls[0]["blocks"][0]["rows"]
    ]
    assert rows == [["A", "B"], ["only-one", ""], ["x", "y"]]


# --- the delivery contract -----------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_blocks_post_raises_like_any_other_delivery() -> None:
    channel = _channel()
    channel._client = _RecordingClient(fail_from=0)

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(f"{MARKER}\n\n{TABLE}"))

    assert excinfo.value.channel_id == "C1"
    assert excinfo.value.chunks_sent == 0
    assert "channel_not_found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_partial_blocks_delivery_still_reports_its_progress() -> None:
    channel = _channel()
    channel._client = _RecordingClient(fail_from=1)
    content = f"{MARKER}\n\n{TABLE}\n\n{'filler ' * 6000}"

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(content))

    assert excinfo.value.chunks_sent == 1
    assert excinfo.value.chunks_total > 1


# --- a rendering Slack refuses -------------------------------------------


class _SlackApiError(RuntimeError):
    """Shaped like ``slack_sdk.errors.SlackApiError``: a payload on ``response``."""

    def __init__(self, code: str) -> None:
        super().__init__(
            "The request to the Slack API failed. "
            "(url: https://slack.com/api/chat.postMessage, status: 200) "
            f"The server responded with: {{'ok': False, 'error': '{code}'}}"
        )
        self.response = {"ok": False, "error": code}


class _BlockRefusingClient:
    """Accepts a payload only once it holds no ``blocks``."""

    def __init__(self, code: str = "invalid_blocks", *, always: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._code = code
        self._always = always

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        if self._always or "blocks" in kwargs:
            raise _SlackApiError(self._code)
        return {"ts": f"1710000099.{len(self.calls):06d}"}

    chat_update = chat_postMessage


@pytest.mark.parametrize(
    "code", ["invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long"]
)
@pytest.mark.asyncio
async def test_a_refused_rendering_is_delivered_as_plain_text(code: str) -> None:
    """The text in the refused payload was always enough to deliver the message."""
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient(code)
    channel._client = client

    await channel.send(_message(f"Totals:\n\n{TABLE}"))

    assert len(client.calls) == 2
    assert "blocks" in client.calls[0]
    assert "blocks" not in client.calls[1]
    # The retry still holds the whole chunk, so nothing of the answer is
    # lost; what is new is the line under it saying the formatting was refused.
    assert client.calls[1]["text"].startswith(client.calls[0]["text"])
    assert "alpha" in client.calls[1]["text"]
    assert slack_delivery._BLOCK_FAILURE_DATA_NOTICE in client.calls[1]["text"]


@pytest.mark.asyncio
async def test_a_refused_rendering_is_logged_at_warning(slack_logs) -> None:
    channel = _channel(blockkit_tables="auto")
    channel._client = _BlockRefusingClient("msg_blocks_too_long")

    await channel.send(_message(f"Totals:\n\n{TABLE}"))

    warnings = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    line = warnings[0]
    assert line.startswith(
        "Slack rejected the Block Kit rendering (msg_blocks_too_long)"
    )
    # Slack said nothing beyond the code here, and the line says so rather than
    # leaving the operator to wonder whether it was dropped.
    assert "no detail" in line
    # And the payload is still identified, by the block types it was made of.
    assert "data_table" in line
    assert line.endswith("delivering this chunk as plain text")


@pytest.mark.asyncio
async def test_the_fallback_reports_the_delivery_as_text_not_blocks(
    slack_logs,
) -> None:
    channel = _channel(blockkit_tables="auto")
    channel._client = _BlockRefusingClient()

    await channel.send(_message(f"Totals:\n\n{TABLE}"))

    lines = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno == logging.INFO
    ]
    assert lines == [
        "[SlackChannel] delivered text: channel=C1 ts=1710000099.000002 "
        "chunk=1/1 mode=post"
    ]


@pytest.mark.parametrize("code", ["channel_not_found", "invalid_auth"])
@pytest.mark.asyncio
async def test_an_error_that_is_not_about_the_blocks_still_fails(code: str) -> None:
    """Retrying these without blocks would only delay the error the caller needs."""
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient(code, always=True)
    channel._client = client

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(f"Totals:\n\n{TABLE}"))

    assert code in str(excinfo.value)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_the_rendering_is_dropped_once_and_not_retried_forever() -> None:
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient("invalid_blocks", always=True)
    channel._client = client

    with pytest.raises(SlackDeliveryError):
        await channel.send(_message(f"Totals:\n\n{TABLE}"))

    # One attempt with the rendering, one without, and then it gives up.
    assert len(client.calls) == 2
    assert "blocks" in client.calls[0]
    assert "blocks" not in client.calls[1]


@pytest.mark.asyncio
async def test_a_reply_that_never_had_blocks_is_not_retried() -> None:
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient("invalid_blocks", always=True)
    channel._client = client

    with pytest.raises(SlackDeliveryError):
        await channel.send(_message("Just prose, no table."))

    assert len(client.calls) == 1


def test_only_the_block_specific_errors_are_recognised() -> None:
    for code in ("invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long"):
        assert slack_delivery.rejected_blocks_error(_SlackApiError(code)) == code
    for code in ("channel_not_found", "invalid_auth", "ratelimited", "msg_too_long"):
        assert slack_delivery.rejected_blocks_error(_SlackApiError(code)) is None
    # An exception with no Slack payload at all must not be mistaken for one.
    assert slack_delivery.rejected_blocks_error(RuntimeError("boom")) is None


# --- streaming -----------------------------------------------------------


def test_a_streamed_snapshot_stays_text_only() -> None:
    """A table rewritten once a second is worse to watch than the same text."""
    snapshot = _channel()._stream_snapshot(f"{MARKER}\n\n{TABLE}")

    assert isinstance(snapshot, str)
    assert MARKER not in snapshot
    assert "| alpha | 1 |" in snapshot


@pytest.mark.asyncio
async def test_the_closing_rewrite_of_a_streamed_reply_carries_the_blocks() -> None:
    channel = _channel(enable_streaming=True)
    client = _RecordingClient()
    channel._client = client
    final = _message(f"{MARKER}\n\n{TABLE}")

    for fragment in (f"{MARKER}\n\n", TABLE[:20], TABLE[20:]):
        await channel.send(
            Message(
                id=final.id,
                type="event",
                channel_id="slack",
                session_id=final.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": fragment},
                event_type=EventType.CHAT_DELTA,
                metadata={"slack_channel_id": "C1"},
            )
        )
    await channel.send(final)

    # The message is opened and edited as text; only the closing rewrite,
    # which is the delivery send() is contractually responsible for, has blocks.
    assert "blocks" not in client.calls[0]
    assert [block["type"] for block in client.calls[-1]["blocks"]] == ["data_table"]
    # An edit of the streamed message, not a second copy posted underneath it.
    assert client.calls[-1]["ts"] == "1710000099.000001"


class TestResolveBlockkitTablesMode:
    """The config value survives YAML's boolean coercion of the mode names."""

    def test_absent_key_uses_the_default_mode(self):
        assert resolve_blockkit_tables_mode({}) == "auto"

    def test_explicit_mode_names_pass_through(self):
        for mode in ("off", "marker", "auto"):
            assert resolve_blockkit_tables_mode({"blockkit_tables": mode}) == mode

    def test_unquoted_off_still_means_off(self):
        # YAML 1.1 parses off/no/false as False before this is reached. Reading
        # the value as a string would resolve it to the default, which renders --
        # enabling the feature the author wrote the word to disable.
        assert resolve_blockkit_tables_mode({"blockkit_tables": False}) == "off"

    def test_unquoted_on_resolves_to_the_default_rather_than_a_guess(self):
        # True names no single mode: it says "enabled" without saying which.
        assert resolve_blockkit_tables_mode({"blockkit_tables": True}) == "auto"

    def test_unknown_value_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING, logger=slack_config.logger.name):
            assert resolve_blockkit_tables_mode({"blockkit_tables": "tables"}) == "auto"
        assert "blockkit_tables" in caplog.text

    def test_case_and_padding_are_ignored(self):
        assert resolve_blockkit_tables_mode({"blockkit_tables": "  OFF "}) == "off"


@pytest.mark.asyncio
async def test_a_table_cut_by_the_streamed_chunk_ceiling_posts_as_pipes() -> None:
    channel = _channel(enable_streaming=True)
    client = _RecordingClient()
    channel._client = client
    rows = "\n".join(
        f"| row{index:03d} | {'note ' * 12}{index} |" for index in range(100)
    )
    content = f"*Roster*\n| Name | Count |\n| --- | --- |\n{rows}"

    final = _message(content)
    await channel.send(
        Message(
            id=final.id,
            type="event",
            channel_id="slack",
            session_id=final.session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "prose "},
            event_type=EventType.CHAT_DELTA,
            metadata={"slack_channel_id": "C1"},
        )
    )
    await channel.send(final)

    # The head of the table is cut off at the ceiling and renders; the tail
    # begins mid-table, has no delimiter row above it, and is posted as pipes.
    # The opening of the stream, the rewrite that closes it, and one more post.
    assert len(client.calls) == 3
    head, tail = client.calls[-2:]
    assert [block["type"] for block in head["blocks"]] == ["section", "data_table"]
    assert "blocks" not in tail
    assert tail["text"].startswith("| row")
    assert "| row099 |" in tail["text"]


# --- the advanced tier through the connector -----------------------------


CHART_FENCE = "\n".join(
    [
        "```blockkit",
        '{"type": "data_visualization", "title": "Reviews",',
        ' "chart": {"type": "pie",',
        '  "segments": [{"label": "merged", "value": 7}]}}',
        "```",
    ]
)


@pytest.mark.asyncio
async def test_a_blockkit_fence_reaches_the_channel_as_a_chart() -> None:
    client = await _send(_channel(), f"Seven merged this week.\n\n{CHART_FENCE}")

    assert len(client.calls) == 1
    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        "data_visualization",
    ]


@pytest.mark.asyncio
async def test_the_operator_kill_switch_covers_the_advanced_tier_too() -> None:
    """``off`` has to keep meaning "this channel gets plain text"."""
    client = await _send(
        _channel(blockkit_tables="off"), f"Seven merged.\n\n{CHART_FENCE}"
    )

    assert "blocks" not in client.calls[0]
    assert "blockkit" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_the_notification_text_is_whatever_prose_the_author_wrote() -> None:
    client = await _send(_channel(), f"Seven merged this week.\n\n{CHART_FENCE}")

    assert "Seven merged this week." in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_fence_only_reply_never_sends_the_raw_payload_as_text() -> None:
    client = await _send(_channel(), CHART_FENCE)

    assert client.calls[0]["blocks"]
    assert not client.calls[0]["text"].startswith("```blockkit")
    assert "{" not in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_prose_plus_a_fence_keeps_the_prose_byte_for_byte() -> None:
    """The ordinary path, unmodified: this is the case that must not regress."""
    content = f"Seven merged this week.\n\n{CHART_FENCE}"
    client = await _send(_channel(), content)

    assert client.calls[0]["text"] == content


IMAGE_BLOCK_WITH_TITLE = {
    "type": "image",
    "image_url": "https://example.com/cat.gif",
    "alt_text": "a cat batting at a box",
    "title": {"type": "plain_text", "text": "The office cat, mid-heist"},
}
IMAGE_BLOCK_ALT_TEXT_ONLY = {
    "type": "image",
    "image_url": "https://example.com/cat.gif",
    "alt_text": "a cat batting at a box",
}


@pytest.mark.asyncio
async def test_an_image_blocks_title_becomes_the_fallback() -> None:
    fence = _blockkit_fence(IMAGE_BLOCK_WITH_TITLE)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "The office cat, mid-heist"


@pytest.mark.asyncio
async def test_an_image_blocks_alt_text_is_used_when_there_is_no_title() -> None:
    fence = _blockkit_fence(IMAGE_BLOCK_ALT_TEXT_ONLY)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "a cat batting at a box"


@pytest.mark.asyncio
async def test_a_section_blocks_text_becomes_the_fallback() -> None:
    section = {"type": "section", "text": {"type": "mrkdwn", "text": "Approve this?"}}
    fence = _blockkit_fence(section)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "Approve this?"


@pytest.mark.asyncio
async def test_a_header_blocks_text_becomes_the_fallback() -> None:
    header = {"type": "header", "text": {"type": "plain_text", "text": "Weekly digest"}}
    fence = _blockkit_fence(header)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "Weekly digest"


ACTIONS_ONLY_BLOCK = {
    "type": "actions",
    "elements": [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve"},
            "action_id": "approve",
        }
    ],
}


@pytest.mark.asyncio
async def test_a_payload_yielding_nothing_usable_gets_the_generic_string() -> None:
    """A fence-only control with no title, ``alt_text``, section or header text."""
    fence = _blockkit_fence(ACTIONS_ONLY_BLOCK)
    client = await _send(_channel(), fence)

    assert "blocks" not in client.calls[0]
    assert fence in client.calls[0]["text"]


# --- the basic tier through the connector --------------------------------


MERMAID_FENCE = "\n".join(
    [
        "```mermaid",
        "pie showData",
        "    title Reviews this week",
        '    "merged" : 7',
        '    "open" : 3',
        "```",
    ]
)


@pytest.mark.asyncio
async def test_a_mermaid_pie_reaches_the_channel_as_a_chart() -> None:
    """Nothing beside the fence asked for it: the language is the whole request."""
    client = await _send(_channel(), f"Seven merged.\n\n{MERMAID_FENCE}")

    chart = client.calls[0]["blocks"][-1]
    assert chart["type"] == "data_visualization"
    assert chart["chart"]["type"] == "pie"


@pytest.mark.asyncio
async def test_a_slack_raw_fence_is_delivered_as_the_source() -> None:
    """The escape hatch, for an author showing what the diagram is written as."""
    shown = MERMAID_FENCE.replace("```mermaid", "```slack-raw")
    client = await _send(_channel(), f"Here is the diagram.\n\n{shown}")

    assert "blocks" not in client.calls[0]
    assert "pie showData" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_bare_fence_is_delivered_as_the_source_too() -> None:
    shown = MERMAID_FENCE.replace("```mermaid", "```")
    client = await _send(_channel(), f"Here is the diagram.\n\n{shown}")

    assert "blocks" not in client.calls[0]
    assert "pie showData" in client.calls[0]["text"]


# --- what the reply's marker escalates ------------------------------------
#
# The marker forces data-table rendering for a Markdown table. Mermaid fences
# are rendered according to their language marker.


@pytest.mark.asyncio
async def test_the_marker_forces_a_data_table_a_channel_configured_basic(
) -> None:
    plain = await _send(_channel(render_tables="basic"), f"Totals:\n\n{TABLE}")
    asked = await _send(
        _channel(render_tables="basic"), f"{MARKER}\n\nTotals:\n\n{TABLE}"
    )

    assert plain.calls[0]["blocks"][-1]["type"] == "table"
    assert asked.calls[0]["blocks"][-1]["type"] == "data_table"


@pytest.mark.asyncio
async def test_off_outranks_the_marker() -> None:
    client = await _send(
        _channel(render_tables="off"), f"{MARKER}\n\nTotals:\n\n{TABLE}"
    )

    assert client.calls[0].get("blocks") in (None, [])
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_forced_data_table_is_still_the_same_table() -> None:
    client = await _send(_channel(), f"{MARKER}\n\n## Totals\n\n{TABLE}")

    block = client.calls[0]["blocks"][-1]
    assert [[cell["text"] for cell in row] for row in block["rows"]] == [
        ["Name", "Count"],
        ["alpha", "1"],
        ["beta", "2"],
    ]
    # ``data_table`` requires a caption where ``table`` has none, and it comes
    # from the prose above rather than from a word invented here.
    assert block["caption"] == "Totals"
    # The numeric column is handed over as numbers so the client sorts it as
    # numbers rather than as strings.
    assert [row[1]["type"] for row in block["rows"][1:]] == [
        "raw_number",
        "raw_number",
    ]


@pytest.mark.asyncio
async def test_the_marker_does_not_reach_the_fence_in_either_direction() -> None:
    shown = MERMAID_FENCE.replace("```mermaid", "```slack-raw")
    asked = await _send(_channel(), f"{MARKER}\n\nSeven merged.\n\n{shown}")
    unasked = await _send(_channel(), f"Seven merged.\n\n{MERMAID_FENCE}")

    assert "blocks" not in asked.calls[0]
    assert "pie showData" in asked.calls[0]["text"]
    assert unasked.calls[0]["blocks"][-1]["type"] == "data_visualization"


@pytest.mark.asyncio
async def test_the_off_marker_withdraws_the_whole_reply_including_a_fence() -> None:
    """Declining is still per reply, and a fence is not exempt from it."""
    client = await _send(
        _channel(blockkit_tables="auto"),
        f"{OFF_MARKER}\n\n{TABLE}\n\n{MERMAID_FENCE}",
    )

    assert "blocks" not in client.calls[0]


@pytest.mark.asyncio
async def test_the_operator_kill_switch_outranks_the_reply_and_the_fence() -> None:
    """``off`` means this channel gets plain text, whatever a reply asked for."""
    client = await _send(
        _channel(blockkit_tables="off"), f"{MARKER}\n\n{TABLE}\n\n{MERMAID_FENCE}"
    )

    assert "blocks" not in client.calls[0]
    assert MARKER not in client.calls[0]["text"]
