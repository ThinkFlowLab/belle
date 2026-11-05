"""The silence rail stores only messages that the connector may show."""

import asyncio
from typing import Any

import pytest

pytest.importorskip("openjiuwen")

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs

from belle_agent.integrations.openjiuwen.rails.silence_rail import SilenceRail
from belle_agent.silence import NO_REPLY_SENTINEL, REPLY_OPTIONAL_KEY, SILENCE_UNSTORED_KEY

NEAR_MISS = f"{NO_REPLY_SENTINEL}cancelled"


class _FakeContext:
    """The two ``ModelContext`` methods the ReAct loop uses on this path."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def add_messages(self, message: Any) -> None:
        self.messages.append(message)

    def get_messages(self) -> list[Any]:
        return list(self.messages)


async def _react_loop(
    rail: SilenceRail,
    responses: list[AssistantMessage],
) -> tuple[_FakeContext, dict[str, Any] | None]:
    """Replay the ReAct loop's model-call step for each response in order.

    The order is upstream's: fire AFTER_MODEL_CALL, read a force-finish request,
    and append the assistant message only when there is none. A response with
    tool calls is followed by its tool messages, which is how a turn's work
    reaches the conversation.
    """
    context = _FakeContext()
    ctx = AgentCallbackContext(agent=None)
    result: dict[str, Any] | None = None
    for response in responses:
        ctx.inputs = ModelCallInputs(messages=[], tools=None)
        ctx.inputs.response = response
        await rail.after_model_call(ctx)
        finish = ctx.consume_force_finish()
        if finish is not None:
            result = finish.result
            break
        await context.add_messages(
            AssistantMessage(
                content=response.content,
                tool_calls=response.tool_calls,
                reasoning_content=response.reasoning_content,
            )
        )
        if not response.tool_calls:
            result = {"output": response.content, "result_type": "answer"}
            break
        for call in response.tool_calls:
            await context.add_messages(
                ToolMessage(tool_call_id=call.id, content="done")
            )
    return context, result


def _rail(metadata: dict[str, Any] | None) -> SilenceRail:
    return SilenceRail(lambda: metadata)


_LICENSED = {REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: True}


def _reading_a_document() -> AssistantMessage:
    """The tool call a quiet turn still makes. Silence is not idleness."""
    return AssistantMessage(
        content="",
        reasoning_content="the thread names a document; read it",
        tool_calls=[
            ToolCall(
                id="call_1",
                type="function",
                name="read_slack_document",
                arguments="{}",
            )
        ],
    )


@pytest.mark.asyncio
async def test_a_silence_only_reply_never_reaches_the_stored_conversation():
    context, result = await _react_loop(
        _rail(_LICENSED),
        [AssistantMessage(content=NO_REPLY_SENTINEL)],
    )

    assert context.get_messages() == []
    # The connector receives what it received before, so it withholds the reply
    # the way it already does.
    assert result == {"output": NO_REPLY_SENTINEL, "result_type": "answer"}


@pytest.mark.asyncio
async def test_the_quiet_turn_keeps_its_tool_call_and_its_reasoning():
    context, _ = await _react_loop(
        _rail(_LICENSED),
        [_reading_a_document(), AssistantMessage(content=NO_REPLY_SENTINEL)],
    )

    kept = context.get_messages()
    assert [type(message).__name__ for message in kept] == [
        "AssistantMessage",
        "ToolMessage",
    ]
    assert [call.name for call in kept[0].tool_calls] == ["read_slack_document"]
    assert kept[0].reasoning_content == "the thread names a document; read it"
    assert kept[1].tool_call_id == "call_1"


@pytest.mark.asyncio
async def test_an_ordinary_reply_is_stored_unchanged():
    context, result = await _react_loop(
        _rail(_LICENSED),
        [AssistantMessage(content="the build broke on the lint step")],
    )

    kept = context.get_messages()
    assert [message.content for message in kept] == [
        "the build broke on the lint step"
    ]
    assert result == {
        "output": "the build broke on the lint step",
        "result_type": "answer",
    }


@pytest.mark.asyncio
async def test_a_near_miss_is_stored_because_the_connector_posts_it():
    context, result = await _react_loop(
        _rail(_LICENSED), [AssistantMessage(content=NEAR_MISS)]
    )

    assert [message.content for message in context.get_messages()] == [NEAR_MISS]
    assert result == {"output": NEAR_MISS, "result_type": "answer"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {REPLY_OPTIONAL_KEY: True},
        {SILENCE_UNSTORED_KEY: True},
        {REPLY_OPTIONAL_KEY: "true", SILENCE_UNSTORED_KEY: True},
        {SILENCE_UNSTORED_KEY: False},
        {SILENCE_UNSTORED_KEY: "true"},
    ],
    ids=["absent", "empty", "reply-only", "unstored-only", "string-license", "false", "not-a-boolean"],
)
async def test_without_both_silence_keys_the_turn_is_stored_as_it_was(metadata):
    """Incomplete permission leaves the turn on the normal storage path."""
    context, _ = await _react_loop(
        _rail(metadata), [AssistantMessage(content=NO_REPLY_SENTINEL)]
    )

    assert [message.content for message in context.get_messages()] == [
        NO_REPLY_SENTINEL
    ]


@pytest.mark.asyncio
async def test_a_response_carrying_tool_calls_is_never_the_one_dropped():
    """It is not a final response: the loop runs those tools next."""
    silent_call = AssistantMessage(
        content=NO_REPLY_SENTINEL,
        tool_calls=[
            ToolCall(
                id="call_1",
                type="function",
                name="add_slack_reaction",
                arguments="{}",
            )
        ],
    )

    context, _ = await _react_loop(_rail(_LICENSED), [silent_call])

    assert [type(message).__name__ for message in context.get_messages()] == [
        "AssistantMessage",
        "ToolMessage",
    ]


@pytest.mark.asyncio
async def test_a_failing_metadata_reader_licenses_nothing():
    def explode() -> dict[str, Any]:
        raise RuntimeError("no request bound")

    context, _ = await _react_loop(
        SilenceRail(explode), [AssistantMessage(content=NO_REPLY_SENTINEL)]
    )

    assert [message.content for message in context.get_messages()] == [
        NO_REPLY_SENTINEL
    ]


@pytest.mark.asyncio
async def test_a_message_written_during_the_turn_keeps_the_turn_running():
    """Steering means the turn has more to do, so it stays on the usual path.

    A force finish here would end the turn and drop what somebody just wrote.
    The message is stored instead, and the turn answers again.
    """
    queue = asyncio.Queue()
    queue.put_nowait("and check staging too")
    ctx = AgentCallbackContext(agent=None)
    ctx.bind_steering_queue(queue)
    ctx.inputs = ModelCallInputs(response=AssistantMessage(content=NO_REPLY_SENTINEL))

    await _rail(_LICENSED).after_model_call(ctx)

    assert ctx.consume_force_finish() is None
