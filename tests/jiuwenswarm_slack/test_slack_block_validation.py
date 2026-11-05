"""Tests for Slack Block Kit preflight and delivery fallback."""

import json
import logging
import time
from typing import Any

import pytest
from belle_agent.platforms.slack import delivery as slack_delivery
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack import questions as slack_questions
from belle_agent.platforms.slack.delivery import block_failure_text, should_validate_blocks, validation_errors

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)

SMALL_TABLE = "\n".join(
    [
        "| Name | Count |",
        "| --- | --- |",
        "| alpha | 1 |",
        "| beta | 2 |",
    ]
)
TABLE = "\n\n".join([SMALL_TABLE] * 12)

FENCE = "\n".join(
    [
        "```blockkit",
        json.dumps([{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]),
        "```",
    ]
)

# One refusal as Slack reports it: an opaque top-level code, and an ``errors``
# array whose pointer is the only part that says which element was wrong.
REFUSAL = {
    "ok": False,
    "error": "invalid_blocks",
    "errors": [
        {
            "pointer": "/blocks/0/text",
            "code": "invalid_type",
            "message": "must be an object",
            "constraint": "type",
        }
    ],
}


class _SlackApiErrorDouble(Exception):
    """What slack_sdk raises for any reply that is not ``ok``.

    Standing in for ``SlackApiError`` rather than importing it, because what the
    connector reads off the exception is the ``response`` attribute and nothing
    else -- and a double makes that dependency visible instead of inherited.
    """

    def __init__(self, response: Any) -> None:
        super().__init__("The request to the Slack API failed.")
        self.response = response


class _ValidatingClient:
    """Fake client recording every send and answering ``blocks.validate``.

    ``verdict`` is what the validator does: ``None`` approves, a mapping is
    returned as a refusal the way slack_sdk delivers one -- by raising -- and an
    exception instance is raised as itself, which is how a timeout or an
    unavailable method arrives.
    """

    def __init__(self, verdict: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.validated: list[dict[str, Any]] = []
        self._verdict = verdict

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"ts": f"1710000099.{len(self.calls):06d}"}

    chat_update = chat_postMessage

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "blocks.validate"
        self.validated.append(kwargs)
        if isinstance(self._verdict, BaseException):
            raise self._verdict
        if self._verdict is not None:
            raise _SlackApiErrorDouble(self._verdict)
        return {"ok": True}


def _channel(**settings: Any) -> SlackChannel:
    return SlackChannel(
        SlackChannelConfig(enabled=True, blockkit_tables="auto", **settings),
        RobotMessageRouter(),
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


async def _send(channel: SlackChannel, content: str) -> _ValidatingClient:
    await channel.send(_message(content))
    return channel._client


# --- the policy, which is about cost and never about correctness ----------


SMALL_CARD = [{"type": "section", "text": {"type": "mrkdwn", "text": "row"}}]


class TestShouldValidateBlocks:
    def test_an_empty_payload_is_never_validated(self):
        """An empty list removes blocks from chat.update."""
        assert not should_validate_blocks([], slack_blocks.BLOCK_KIND_FENCE)
        assert not should_validate_blocks(None, slack_blocks.BLOCK_KIND_FENCE)

    def test_risky_skips_a_small_payload_from_a_typed_builder(self):
        assert not should_validate_blocks(
            SMALL_CARD, slack_blocks.BLOCK_KIND_CHROME
        )
        assert not should_validate_blocks(
            SMALL_CARD, slack_blocks.BLOCK_KIND_DATA
        )

    @pytest.mark.parametrize(
        "kind",
        [
            slack_blocks.BLOCK_KIND_FENCE,
            slack_blocks.BLOCK_KIND_INTERACTIVE,
            slack_blocks.BLOCK_KIND_UNKNOWN,
        ],
    )
    def test_risky_pays_for_the_kinds_that_earn_it(self, kind: str):
        assert should_validate_blocks(SMALL_CARD, kind)

    def test_risky_pays_for_a_large_payload_whatever_built_it(self):
        many = SMALL_CARD * slack_delivery._RISKY_BLOCK_COUNT
        assert should_validate_blocks(many, slack_blocks.BLOCK_KIND_DATA)

    def test_risky_pays_for_a_long_payload_of_few_blocks(self):
        wide = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "x" * slack_delivery._RISKY_BLOCK_CHARACTERS,
                },
            }
        ]
        assert should_validate_blocks(wide, slack_blocks.BLOCK_KIND_DATA)


# --- reading Slack's verdict ----------------------------------------------


def test_an_approval_is_no_errors() -> None:
    assert validation_errors({"ok": True}) == []


def test_a_refusal_keeps_the_pointer() -> None:
    assert validation_errors(REFUSAL)[0]["pointer"] == "/blocks/0/text"


def test_a_refusal_with_no_detail_still_counts_as_a_refusal() -> None:
    assert validation_errors({"ok": False, "error": "invalid_blocks"}) == [
        {"code": "invalid_blocks"}
    ]


@pytest.mark.parametrize("code", ["ratelimited", "invalid_auth", "unknown_method"])
def test_a_failure_that_is_not_about_the_blocks_is_not_a_refusal(code: str) -> None:
    """Only a verdict on the payload may cost a message its rendering."""
    assert validation_errors({"ok": False, "error": code}) == []


def test_an_unrecognised_code_with_pointers_is_still_a_refusal() -> None:
    """A pointered errors array belongs to schema validation and nothing else."""
    errors = validation_errors(
        {"ok": False, "error": "invalid_arguments", "errors": [{"pointer": "/blocks/1"}]}
    )
    assert errors == [{"pointer": "/blocks/1"}]


# --- a payload Slack accepts ----------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_payload_is_sent_exactly_as_it_was_built() -> None:
    channel = _channel()
    channel._client = _ValidatingClient()

    client = await _send(channel, f"Totals:\n\n{TABLE}")

    assert len(client.validated) == 1
    assert len(client.calls) == 1
    assert client.calls[0]["blocks"][-1]["type"] == "data_table"
    assert client.calls[0]["text"] == f"Totals:\n\n{TABLE}"
    body = client.validated[0]["data"]
    assert set(body) == {"blocks"}
    assert json.loads(body["blocks"])[-1]["type"] == "data_table"


@pytest.mark.asyncio
async def test_a_payload_the_policy_skips_is_never_validated() -> None:
    channel = _channel()
    channel._client = _ValidatingClient(REFUSAL)

    client = await _send(channel, f"Totals:\n\n{SMALL_TABLE}")

    assert client.validated == []
    assert "blocks" in client.calls[0]


# --- a payload Slack refuses, per kind ------------------------------------


@pytest.mark.asyncio
async def test_a_refused_table_arrives_as_data_with_one_line_of_explanation() -> None:
    channel = _channel()
    channel._client = _ValidatingClient(REFUSAL)

    client = await _send(channel, f"Totals:\n\n{TABLE}")

    assert len(client.calls) == 1
    assert "blocks" not in client.calls[0]
    text = client.calls[0]["text"]
    # The rows the reader asked for are still there, and the notice is one line
    # under them rather than a replacement for them.
    assert TABLE in text
    assert text.endswith(slack_delivery._BLOCK_FAILURE_DATA_NOTICE)
    # Their own message is not shown Slack's diagnostics: a refused table cost
    # them formatting, and a JSON pointer is not theirs to act on.
    assert "/blocks/" not in text


@pytest.mark.asyncio
async def test_a_refused_chart_rebuilds_the_numbers_the_text_never_carried() -> None:
    """The case the notice alone would not cover: text that is a stub."""
    content = "\n".join(
        [
            "Split",
            "",
            "```mermaid",
            "pie",
            '    "alpha" : 30',
            '    "beta" : 70',
            "```",
        ]
    ) + "\n\n" + TABLE
    channel = _channel()
    channel._client = _ValidatingClient(REFUSAL)

    client = await _send(channel, content)

    text = client.calls[0]["text"]
    assert "| alpha | 30 |" in text
    assert "| beta | 70 |" in text
    assert slack_delivery._BLOCK_FAILURE_DATA_NOTICE in text


@pytest.mark.asyncio
async def test_a_refused_fence_comes_back_as_source_with_the_pointer() -> None:
    channel = _channel()
    # The fence is the second block: the prose above it is the first.
    channel._client = _ValidatingClient(
        {
            "ok": False,
            "error": "invalid_blocks",
            "errors": [
                {
                    "pointer": "/blocks/1/text",
                    "code": "invalid_type",
                    "message": "must be an object",
                }
            ],
        }
    )

    client = await _send(channel, f"Look:\n\n{FENCE}")

    text = client.calls[0]["text"]
    assert slack_delivery._BLOCK_FAILURE_FENCE_NOTICE in text
    # The pointer is the whole value of validating: a refused send says
    # "invalid_blocks" and never which element it meant.
    assert "`/blocks/1/text`" in text
    assert "must be an object" in text
    assert "invalid_type" in text
    # Only the block the pointer names is quoted back. The author's own fence
    # is already above in their own words; repeating the whole compiled payload
    # under it would bury the one element that was wrong.
    quoted = json.loads(text.rsplit("```", 2)[-2])
    assert quoted == [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    assert text.rstrip().endswith("```")


def test_the_whole_payload_is_quoted_when_no_pointer_names_a_block() -> None:
    """What the post-hoc backstop has to work with: a bare error code."""
    blocks = [{"type": "section"}, {"type": "divider"}]
    assert slack_delivery.pointed_blocks(blocks, [{"code": "invalid_blocks"}]) == blocks
    assert slack_delivery.pointed_blocks(
        blocks, [{"pointer": "/blocks/9/text"}]
    ) == blocks


def test_a_pointer_selects_its_block_once_however_many_errors_name_it() -> None:
    blocks = [{"type": "section"}, {"type": "divider"}]
    errors = [{"pointer": "/blocks/1/type"}, {"pointer": "/blocks/1/text"}]
    assert slack_delivery.pointed_blocks(blocks, errors) == [{"type": "divider"}]


@pytest.mark.asyncio
async def test_a_refused_control_says_so_and_shows_no_json() -> None:
    channel = _channel()
    client = _ValidatingClient(REFUSAL)
    channel._client = client

    sent, _, _ = await channel._post_text(
        channel_id="C1",
        text="Approve deleting /tmp?",
        thread_ts="",
        blocks=[
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "slack_answer",
                        "text": {"type": "plain_text", "text": "Yes"},
                        "value": "req-1|0|yes",
                    }
                ],
            }
        ],
        block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
        block_text_route=slack_questions.text_route("permission_interrupt", slack_connect._INTERRUPT_RESUME_SOURCES),
    )

    assert sent
    text = client.calls[0]["text"]
    assert "Approve deleting /tmp?" in text
    assert slack_delivery._BLOCK_FAILURE_INTERACTIVE_NOTICE.rstrip("_") in text
    assert "Reply in this thread" in text
    # Never the payload. A button's value is this connector's internals, and a
    # broken control is a functional failure rather than a display one.
    assert "action_id" not in text
    assert "req-1" not in text
    assert "/blocks/" not in text


def test_a_standalone_approval_names_no_route_it_has_not_got() -> None:
    """Only an interrupt-sourced question is answerable by typing."""
    assert slack_questions.text_route("permission_interrupt", slack_connect._INTERRUPT_RESUME_SOURCES)
    assert slack_questions.text_route("chat_user_answer", slack_connect._INTERRUPT_RESUME_SOURCES) == ""
    text = block_failure_text(
        text="Approve?",
        blocks=[{"type": "actions", "elements": []}],
        kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
        errors=list(REFUSAL["errors"]),
        text_route="",
    )
    assert text.endswith(slack_delivery._BLOCK_FAILURE_INTERACTIVE_NOTICE)


@pytest.mark.asyncio
async def test_a_refusal_is_logged_with_the_pointer_for_the_operator(
    slack_logs,
) -> None:
    channel = _channel()
    channel._client = _ValidatingClient(REFUSAL)

    await _send(channel, f"Totals:\n\n{TABLE}")

    warnings = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno >= logging.WARNING
    ]
    assert any("/blocks/0/text" in message for message in warnings)


# --- a validator that cannot answer ---------------------------------------


@pytest.mark.parametrize(
    "verdict",
    [
        TimeoutError("timed out"),
        RuntimeError("connection reset"),
        _SlackApiErrorDouble({"ok": False, "error": "ratelimited"}),
        _SlackApiErrorDouble({"ok": False, "error": "unknown_method"}),
    ],
    ids=["timeout", "transport", "ratelimited", "unavailable"],
)
@pytest.mark.asyncio
async def test_a_validator_that_cannot_answer_changes_nothing(verdict) -> None:
    """"Could not validate" and "valid" are the same answer at the call site."""
    channel = _channel()
    channel._client = _ValidatingClient(verdict)

    client = await _send(channel, f"Totals:\n\n{TABLE}")

    assert len(client.validated) == 1
    assert len(client.calls) == 1
    assert client.calls[0]["blocks"][-1]["type"] == "data_table"
    assert client.calls[0]["text"] == f"Totals:\n\n{TABLE}"


@pytest.mark.asyncio
async def test_an_unanswerable_validator_does_not_warn(slack_logs) -> None:
    """Nothing degraded, so nothing an operator has to look at."""
    channel = _channel()
    channel._client = _ValidatingClient(RuntimeError("connection reset"))

    await _send(channel, f"Totals:\n\n{TABLE}")

    assert not [
        record for record in slack_logs.records if record.levelno >= logging.WARNING
    ]


# --- the backstop ---------------------------------------------------------


class _RefusingClient(_ValidatingClient):
    """A client that approves at validation time and refuses at send time.

    Which is not a contradiction and is why the post-hoc path stays: the two
    calls are different endpoints, and only one of them is the authority on
    whether a message is delivered.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.refusals = 0

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        if "blocks" in kwargs:
            self.refusals += 1
            raise _SlackApiErrorDouble({"ok": False, "error": "invalid_blocks"})
        return {"ts": f"1710000099.{len(self.calls):06d}"}

    chat_update = chat_postMessage


@pytest.mark.asyncio
async def test_a_payload_refused_after_validation_passed_still_degrades() -> None:
    channel = _channel()
    channel._client = _RefusingClient()

    client = await _send(channel, f"Totals:\n\n{TABLE}")

    assert client.refusals == 1
    assert len(client.calls) == 2
    assert "blocks" not in client.calls[1]
    # The whole chunk is delivered, with the reader told why it looks plain.
    assert TABLE in client.calls[1]["text"]
    assert slack_delivery._BLOCK_FAILURE_DATA_NOTICE in client.calls[1]["text"]


@pytest.mark.asyncio
async def test_the_backstop_works_without_preflight() -> None:
    channel = _channel()
    channel._client = _RefusingClient()

    client = await _send(channel, f"Totals:\n\n{SMALL_TABLE}")

    assert client.validated == []
    assert len(client.calls) == 2
    assert SMALL_TABLE in client.calls[1]["text"]


# --- taking a refused rendering off the payload ---------------------------
#
# Two paths reach it -- validation refusing a payload before it is sent, and
# Slack refusing one it was sent -- and what they have to do differs by call:
# chat.update takes blocks away only when the field is present and empty, and
# chat.postMessage has nothing on screen to take away. The rule is one function
# for that reason, and these are its tests.


def test_an_edit_empties_the_blocks_rather_than_removing_them() -> None:
    """The field stays, holding nothing. Omitting it is what leaves them up."""
    kwargs: dict[str, Any] = {"channel": "C1", "text": "hi", "blocks": [{"type": "divider"}]}

    slack_delivery.drop_refused_blocks(kwargs, update_ts="1710000000.000100")

    assert kwargs["blocks"] == []


def test_a_fresh_post_takes_the_field_off_entirely() -> None:
    kwargs: dict[str, Any] = {"channel": "C1", "text": "hi", "blocks": [{"type": "divider"}]}

    slack_delivery.drop_refused_blocks(kwargs, update_ts="")

    assert "blocks" not in kwargs


def test_dropping_blocks_twice_is_not_an_error() -> None:
    """The post-hoc path can run over a payload the pre-send path already cleared."""
    kwargs: dict[str, Any] = {"channel": "C1", "text": "hi"}

    slack_delivery.drop_refused_blocks(kwargs, update_ts="")

    assert "blocks" not in kwargs


class _RefusingEditClient(_ValidatingClient):
    """Refuses any payload that still holds a rendering, on either call.

    Distinct from ``_RefusingClient``, which refuses on the presence of the
    field: an edit that has had its rendering taken away still holds it,
    empty, and that payload is the one under test here.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.refusals = 0

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs.get("blocks"):
            self.refusals += 1
            raise _SlackApiErrorDouble({"ok": False, "error": "invalid_blocks"})
        return {"ts": "1710000099.000001"}

    chat_update = chat_postMessage


@pytest.mark.asyncio
async def test_a_rendering_refused_on_an_edit_is_emptied_not_dropped() -> None:
    """The defect this pins: ``del`` on an edit leaves the old blocks on screen.

    Reached in service through the activity card, a question being closed and a
    streamed reply's closing rewrite -- every path that passes ``update_ts``.
    """
    channel = _channel()
    channel._client = _RefusingEditClient()

    sent, _, error = await channel._post_text(
        channel_id="C1",
        text="Totals:",
        thread_ts="",
        update_ts="1710000000.000100",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
        block_kind=slack_blocks.BLOCK_KIND_DATA,
    )

    assert sent is True
    assert error == ""
    assert channel._client.refusals == 1
    assert len(channel._client.calls) == 2
    # Present and empty: the message loses its rendering rather than keeping it
    # beside a notice saying it is gone.
    assert channel._client.calls[1]["blocks"] == []


class _EchoingRefusingClient(_ValidatingClient):
    """Refuses the rendering, then echoes back the text the *first* call held.

    Which is what a Slack that stored a truncated message looks like from here:
    the degraded payload is longer than the chunk, so an echo of the chunk alone
    is short.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.first_text = ""

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs.get("blocks"):
            self.first_text = kwargs["text"]
            raise _SlackApiErrorDouble({"ok": False, "error": "invalid_blocks"})
        return {
            "ts": "1710000099.000001",
            "message": {"text": self.first_text},
        }

    chat_update = chat_postMessage


@pytest.mark.asyncio
async def test_the_echo_is_checked_against_the_text_that_was_actually_sent(
    slack_logs,
) -> None:
    """The post-hoc path lengthens the text, so it has to rebind it.

    Written only into ``kwargs``, the local the echo check reads stays at the
    original chunk -- which is exactly what a truncating Slack echoes back, so
    the check compares a string against itself and the one path that grew the
    payload is the one path that stopped detecting a short store.
    """
    channel = _channel()
    channel._client = _EchoingRefusingClient()

    await channel._post_text(
        channel_id="C1",
        text=f"Totals:\n\n{TABLE}",
        thread_ts="",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
        block_kind=slack_blocks.BLOCK_KIND_DATA,
    )

    assert [
        record
        for record in slack_logs.records
        if "Slack stored less text than was sent" in record.getMessage()
    ]


# --- what the operator is left with when a control is refused --------------
#
# The reader's notice on this path says only that there is nothing to click,
# and it stays that way: the payload is the connector's internals rather than
# anything the reader wrote, and a pointer into it is not theirs to act on. The
# log is the other half of that trade, and it is the half that has to be
# complete -- the refusal, Slack's code, Slack's own words, and enough of the
# payload to find the element they were about.


# A question asking for a date, a start time and a headcount, as the connector
# builds it: the prompt, a divider, and one ``input`` block per field.
QUESTION_BLOCKS: list[dict[str, Any]] = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "Event details:"}},
    {"type": "divider"},
    {
        "type": "input",
        "block_id": "qi.0",
        "label": {"type": "plain_text", "text": "Date"},
        "element": {"type": "datepicker", "action_id": "qi.0"},
    },
    {
        "type": "input",
        "block_id": "qi.1",
        "label": {"type": "plain_text", "text": "Start time"},
        "element": {"type": "timepicker", "action_id": "qi.1"},
    },
    {
        "type": "input",
        "block_id": "qi.2",
        "label": {"type": "plain_text", "text": "Number of people"},
        "element": {"type": "number_input", "action_id": "qi.2", "min_value": "1"},
    },
]

# Slack's validation response for the payload, including its error pointer,
# code, and message.
NUMBER_INPUT_REFUSAL = {
    "ok": False,
    "error": "invalid_blocks",
    "errors": [
        {
            "pointer": "/4/element",
            "code": "missing_field",
            "message": "required field is missing",
            "constraint": "required",
        }
    ],
}


def _refusal_line(logs, needle: str) -> str:
    lines = [
        record.getMessage()
        for record in logs.records
        if record.levelno >= logging.WARNING and needle in record.getMessage()
    ]
    assert len(lines) == 1, f"expected one {needle!r} line, got {lines}"
    return lines[0]


@pytest.mark.asyncio
async def test_a_refused_control_records_slacks_own_words(slack_logs) -> None:
    """The one thing that would have named the bad element, and it was dropped."""
    channel = _channel()
    channel._client = _ValidatingClient(NUMBER_INPUT_REFUSAL)

    sent, _, _ = await channel._post_text(
        channel_id="C1",
        text="Event details:",
        thread_ts="",
        blocks=[dict(block) for block in QUESTION_BLOCKS],
        block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
        block_text_route=slack_questions.text_route("ask_user_interrupt", slack_connect._INTERRUPT_RESUME_SOURCES),
    )
    assert sent

    line = _refusal_line(slack_logs, "before it was sent")
    # The refusal, and Slack's code for it.
    assert "invalid_blocks" in line
    # Slack's own verdict: where, what rule, and in Slack's words.
    assert "/4/element" in line
    assert "missing_field" in line
    assert "required field is missing" in line
    # Enough of the payload to find the element without the payload.
    assert "element=number_input" in line
    assert "action_id=qi.2" in line


@pytest.mark.asyncio
async def test_a_refused_control_still_tells_the_reader_only_that(slack_logs) -> None:
    """Fix the log, leave the notice alone."""
    channel = _channel()
    client = _ValidatingClient(NUMBER_INPUT_REFUSAL)
    channel._client = client

    await channel._post_text(
        channel_id="C1",
        text="Event details:",
        thread_ts="",
        blocks=[dict(block) for block in QUESTION_BLOCKS],
        block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
        block_text_route=slack_questions.text_route("ask_user_interrupt", slack_connect._INTERRUPT_RESUME_SOURCES),
    )

    text = client.calls[0]["text"]
    assert slack_delivery._BLOCK_FAILURE_INTERACTIVE_NOTICE.rstrip("_") in text
    assert "number_input" not in text
    assert "missing_field" not in text
    assert "/4/element" not in text


@pytest.mark.asyncio
async def test_the_refusal_log_carries_no_word_of_the_message(slack_logs) -> None:
    """These logs already record message content; this line must not add to it."""
    channel = _channel()
    channel._client = _ValidatingClient(NUMBER_INPUT_REFUSAL)

    await channel._post_text(
        channel_id="C1",
        text="Event details:",
        thread_ts="",
        blocks=[dict(block) for block in QUESTION_BLOCKS],
        block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
        block_text_route="",
    )

    line = _refusal_line(slack_logs, "before it was sent")
    for written in ("Number of people", "Start time", "Event details"):
        assert written not in line


@pytest.mark.asyncio
async def test_a_refused_send_records_what_slack_put_in_the_metadata(
    slack_logs,
) -> None:
    """The backstop path has no errors array, and Slack's words are still there."""

    class _RefusingOnceClient(_ValidatingClient):
        def __init__(self) -> None:
            super().__init__()
            self.refused = False

        async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
            if not self.refused and kwargs.get("blocks"):
                self.refused = True
                raise _SlackApiErrorDouble(
                    {
                        "ok": False,
                        "error": "invalid_blocks",
                        "response_metadata": {
                            "messages": [
                                (
                                    "invalid_blocks "
                                    "[json-pointer:/blocks/4/element] "
                                    "[ERROR] missing required field: element"
                                )
                            ]
                        },
                    }
                )
            return await super().chat_postMessage(**kwargs)

    channel = _channel()
    channel._client = _RefusingOnceClient()

    sent, _, _ = await channel._post_text(
        channel_id="C1",
        text="Event details:",
        thread_ts="",
        blocks=[dict(block) for block in QUESTION_BLOCKS],
        block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
        block_text_route="",
    )
    assert sent

    line = _refusal_line(slack_logs, "Slack rejected the Block Kit rendering")
    assert "invalid_blocks" in line
    assert "missing required field: element" in line
    assert "element=number_input" in line
    assert "action_id=qi.2" in line


def test_a_send_refusal_gives_up_its_pointer() -> None:
    errors = slack_delivery.send_refusal_errors(
        _SlackApiErrorDouble(
            {
                "ok": False,
                "error": "invalid_blocks",
                "response_metadata": {
                    "messages": ["invalid_blocks [json-pointer:/blocks/1/text] [ERROR] nope"]
                },
            }
        )
    )

    assert [entry["pointer"] for entry in errors] == ["/blocks/1/text"]
    assert "nope" in errors[0]["message"]


def test_a_send_refusal_with_no_pointer_keeps_the_sentence() -> None:
    errors = slack_delivery.send_refusal_errors(
        _SlackApiErrorDouble(
            {
                "ok": False,
                "error": "invalid_blocks",
                "response_metadata": {"messages": ["invalid_blocks"]},
            }
        )
    )

    assert errors == [{"pointer": "", "message": "invalid_blocks"}]


def test_a_refusal_with_no_metadata_says_nothing_rather_than_guessing() -> None:
    assert slack_delivery.send_refusal_errors(_SlackApiErrorDouble({"ok": False})) == []
    assert slack_delivery.send_refusal_errors(RuntimeError("reset")) == []


def test_the_top_level_code_survives_a_pointered_refusal() -> None:
    """``validation_errors`` answers with the array, and drops the code with it."""
    assert slack_delivery.validation_refusal_code(NUMBER_INPUT_REFUSAL) == "invalid_blocks"
    assert slack_delivery.validation_refusal_code({"ok": True}) == ""


def test_a_described_block_names_its_element_and_nothing_it_displays() -> None:
    described = slack_delivery.describe_refused_blocks(
        QUESTION_BLOCKS, NUMBER_INPUT_REFUSAL["errors"]
    )

    assert described.startswith("/4 input")
    assert "element=number_input" in described
    assert "action_id=qi.2" in described
    assert "Number of people" not in described


def test_a_refusal_with_no_pointer_describes_the_shape_of_the_payload() -> None:
    described = slack_delivery.describe_refused_blocks(
        QUESTION_BLOCKS, [{"code": "invalid_blocks"}]
    )

    assert "no pointer" in described
    assert "section,divider,input,input,input" in described


def test_a_button_row_is_described_without_its_encoded_value() -> None:
    blocks = [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "slack_answer",
                    "text": {"type": "plain_text", "text": "Yes"},
                    "value": "req-1|0|yes",
                }
            ],
        }
    ]

    described = slack_delivery.describe_refused_blocks(blocks, [{"pointer": "/0"}])

    assert "elements=button" in described
    assert "req-1" not in described
    assert "Yes" not in described


def test_a_long_validator_message_is_clamped_before_it_reaches_the_log() -> None:
    described = slack_delivery.describe_validation_errors(
        [{"pointer": "/0", "code": "invalid_type", "message": "x" * 900}]
    )

    assert len(described) < 400
    assert "invalid_type" in described
