"""Preserve conversation kind when Slack actions resume an interrupted turn.

Click payloads omit the kind. The connector uses the pending turn's kind or
Slack's recorded kind, and leaves it unset if neither is known.
"""

from typing import Any

import pytest
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from slack_test_events import permission_question_event

from belle_agent.integrations.jiuwenswarm import config as belle_config
from belle_agent.integrations.jiuwenswarm import slack_inbound
from belle_agent.integrations.jiuwenswarm import slack_command_handlers
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)
from belle_agent.platforms.slack.activity import _SlackActivityRecord

# A ``C`` id, which says nothing about whether the room is public or private:
# Slack issues ``C`` for both. Every test below puts a different kind behind it
# on purpose, because that is exactly what the id cannot rule out.
CHAT = "C0ROOM0001"
TEAM = "T1"
ASKER = "U0ASKER001"
CLICKER = "U0CLICKER1"
BOT = "U-BOT"
QUESTION_ID = "chatcmpl-tool-0123456789abcdef"
BODY = {"event_id": "Ev1", "team_id": TEAM}
CARD_TS = "1710000016.000100"


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


class _RecordingSlackClient:
    """Just enough Slack for a card to go up and a button to come back."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        return {"ts": str(kwargs.get("ts") or "")}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        raise RuntimeError(f"{method} is not available in this workspace")


def _channel() -> tuple[SlackChannel, list[Message]]:
    """A conversation every message wakes, and the requests it dispatches."""
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=[CHAT],
        group_chat_mode="all",
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._identity.bot_user_id = BOT
    channel._client = _RecordingSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _message_event(chat_type: str) -> dict:
    """A message Slack has stated the kind of.

    ``channel_type`` is the field the whole repair turns on: it is what
    ``_learn_chat_type`` keeps and what ``_conversation_chat_type`` reads back
    for the payloads -- a click among them -- that carry no kind of their own.
    """
    return {
        "type": "message",
        "channel_type": chat_type,
        "channel": CHAT,
        "user": ASKER,
        "text": "shipping it now",
        "ts": "1710000010.000100",
    }


async def _woken_by_a_message(channel: SlackChannel, chat_type: str) -> None:
    """Through the real door, so the kind is learned the way it is in service."""
    await channel._route_message_event(_message_event(chat_type), dict(BODY))


async def _ask(channel: SlackChannel, request: Message) -> None:
    await channel.send(
        permission_question_event(request, chat_id=CHAT, question_id=QUESTION_ID)
    )


def _click_body() -> dict:
    """A ``block_actions`` container, which states no ``channel_type``.

    That silence is the whole problem: the click names the conversation and
    leaves the kind for somebody else to know.
    """
    return {
        "container": {"channel_id": CHAT, "message_ts": CARD_TS},
        "channel": {"id": CHAT},
        "team": {"id": TEAM},
    }


async def _approve(channel: SlackChannel) -> None:
    await channel._dispatch_answer(
        channel._pending_questions[QUESTION_ID],
        values=["approve"],
        body=_click_body(),
        user_id=CLICKER,
    )


async def _interrupt_and_resume(
    channel: SlackChannel, received: list[Message]
) -> Message:
    await _ask(channel, received[-1])
    await _approve(channel)
    return received[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "later"), [("fast", "default"), ("", "fast")]
)
async def test_model_selection_survives_an_interrupted_turn(
    monkeypatch, initial: str, later: str
) -> None:
    configured = lambda: belle_config.ConfiguredModels(names=("default", "fast"))
    for module in (slack_inbound, slack_command_handlers):
        monkeypatch.setattr(module, "configured_models", configured)
    channel, received = _channel()
    channel._identity.team_id = TEAM
    if initial:
        channel._model_overrides[(TEAM, CHAT)] = initial
    expected = initial or "default"
    await _woken_by_a_message(channel, "channel")
    assert received[-1].params.get("model_name", "") == expected
    await _ask(channel, received[-1])
    assert channel._pending_questions[QUESTION_ID].initiator.model_name == expected

    channel._model_overrides[(TEAM, CHAT)] = later
    await _approve(channel)

    assert received[-1].params.get("model_name", "") == expected


def _forget_everything_but_the_question(channel: SlackChannel) -> None:
    """A restart between the prompt going up and the button being pressed.

    Both sources are in memory: the turn initiator the pending question read
    its kind off, and the conversation map. The pending question outlives them
    in this test only because there is no process boundary to cross; in service
    the same shape arrives as a click on a card an earlier process posted.
    """
    channel._admission.turn_initiators.clear()
    channel._chat_types.clear()


# ----------------------------------------------------------------------
# The resume: a click that carries a paused turn on
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "mpim"])
async def test_a_resume_reports_the_kind_its_turn_was_dispatched_with(
    kind: str,
) -> None:
    """A private channel and a group DM are not ``channel``.

    The id is a ``C`` in both, so the prefix reading answered ``channel`` for
    each. The interrupted turn was dispatched against a payload that said
    otherwise, and the resume is owed that answer.
    """
    channel, received = _channel()

    await _woken_by_a_message(channel, kind)
    assert received[0].metadata["slack_channel_type"] == kind

    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.id.startswith("slack-answer-")
    assert resumed.metadata["slack_channel_type"] == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "mpim"])
async def test_the_conversation_map_answers_when_the_record_holds_no_kind(
    kind: str,
) -> None:
    """The second source, and the reason it is second rather than absent.

    The turn initiator is gone -- a restart, or a question older than the entry
    -- but a message has been seen in this conversation since, so Slack's own
    word for it is still on the map. That is evidence, not a guess, and it is
    the only thing standing between this click and a wrong label.
    """
    channel, received = _channel()

    await _woken_by_a_message(channel, kind)
    await _ask(channel, received[-1])
    # The record only. The map survives, which is what separates this case from
    # the unknown one below.
    channel._admission.turn_initiators.clear()
    assert channel._pending_questions[QUESTION_ID].initiator.chat_type == kind
    channel._pending_questions[QUESTION_ID].initiator.chat_type = ""

    await _approve(channel)

    assert received[-1].metadata["slack_channel_type"] == kind


@pytest.mark.asyncio
async def test_the_record_outranks_the_map_when_the_two_disagree() -> None:
    """A turn does not change kind halfway through because somebody clicked.

    The turn started in a private channel and the channel was made public while
    the prompt sat there, so a later message taught the map ``channel`` while
    the record still says ``group``. The resume takes the record: it is the
    kind the interrupted turn was dispatched with, and every other fact stamped
    onto a resume -- the silence contract, the routing, the trigger -- is held
    the same way rather than settled again. A rule keyed on the kind therefore
    picks the same branch for the second half of a turn as for the first.
    """
    channel, received = _channel()

    await _woken_by_a_message(channel, "group")
    await _ask(channel, received[-1])
    channel._chat_types[CHAT] = "channel"

    await _approve(channel)

    assert received[-1].metadata["slack_channel_type"] == "group"


@pytest.mark.asyncio
async def test_a_resume_that_knows_no_kind_records_none() -> None:
    """Neither source knows, and nothing invents one.

    The word on the request is the floor the message and event dispatch paths
    already stand on: ``read_slack_conversation`` refuses an origin type it does
    not recognise, so an empty one would take the history tool away from every
    resumed turn in a conversation nothing has stated a kind for. It licences
    nothing on its own -- ``delivery.reply`` needs the contract key beside it,
    and that is copied off the record.

    What must not happen is the floor hardening into a claim. The initiator
    record the *next* interrupt would resume from is written empty, so a rule
    naming a kind reaches this turn no more the second time than the first.
    """
    channel, received = _channel()

    await _woken_by_a_message(channel, "group")
    await _ask(channel, received[-1])
    _forget_everything_but_the_question(channel)
    channel._pending_questions[QUESTION_ID].initiator.chat_type = ""

    await _approve(channel)
    resumed = received[-1]

    assert resumed.metadata["slack_channel_type"] == "channel"
    assert channel._admission.turn_initiators[resumed.session_id].chat_type == ""


@pytest.mark.asyncio
async def test_a_resume_hands_the_next_interrupt_the_kind_it_settled() -> None:
    """A resumed turn keeps its conversation type for later interrupts."""
    channel, received = _channel()

    await _woken_by_a_message(channel, "group")
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata["slack_channel_type"] == "group"
    assert channel._admission.turn_initiators[resumed.session_id].chat_type == "group"


# ----------------------------------------------------------------------
# The stop button: a click that ends a turn rather than carrying it on
# ----------------------------------------------------------------------


def _claimed(channel_id: str = CHAT) -> _SlackActivityRecord:
    """The record a stop claims before it sends the cancel."""
    return _SlackActivityRecord(
        request_id="req-1", channel_id=channel_id, thread_ts="",
        stop_requested=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "mpim"])
async def test_a_cancel_reports_the_conversations_stated_kind(kind: str) -> None:
    channel, received = _channel()

    await _woken_by_a_message(channel, kind)
    await channel._dispatch_stop(
        _click_body(),
        _claimed(),
        session_id=received[-1].session_id,
        user_id=CLICKER,
    )

    assert received[-1].id.startswith("slack-stop-")
    assert received[-1].metadata["slack_channel_type"] == kind


@pytest.mark.asyncio
async def test_a_cancel_that_knows_no_kind_says_nothing_about_it() -> None:
    """No floor here, because nothing on this path stands on one.

    A cancel carries no history metadata, starts no turn and reads no history,
    so the reason the dispatch paths spend a last resort is absent and the
    honest answer is the one ``_conversation_chat_type`` gives: unknown.
    """
    channel, received = _channel()

    await _woken_by_a_message(channel, "group")
    session_id = received[-1].session_id
    channel._chat_types.clear()

    await channel._dispatch_stop(
        _click_body(),
        _claimed(),
        session_id=session_id,
        user_id=CLICKER,
    )

    assert received[-1].metadata["slack_channel_type"] == ""


@pytest.mark.asyncio
async def test_a_cancel_in_a_direct_message_is_still_a_direct_message() -> None:
    """The one prefix that does answer, and the reason the fix is not a revert.

    ``D`` names a one-to-one direct message and names nothing else, so
    ``chat_type_from_chat_id`` reads it off the id with nothing on the map at
    all. Removing the prefix reading outright would have cost this.
    """
    channel, received = _channel()

    await _woken_by_a_message(channel, "group")
    session_id = received[-1].session_id
    channel._chat_types.clear()

    await channel._dispatch_stop(
        {"container": {"channel_id": "D0DM00001"}, "team": {"id": TEAM}},
        _claimed("D0DM00001"),
        session_id=session_id,
        user_id=CLICKER,
    )

    assert received[-1].metadata["slack_channel_type"] == "im"
