"""Preserve dispatch metadata when a Slack question resumes its turn.

The resumed turn keeps silence, root-posting, and trigger facts from the
original dispatch. It does not recompute scope from current settings.
"""

import time
from typing import Any

import pytest
from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from slack_test_events import permission_question_event

from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    NO_REPLY_SENTINEL,
    POST_AS_ROOT_KEY,
    SlackChannel,
    SlackChannelConfig,
)
from belle_agent.platforms.slack.scoped_config import TRIGGER_EVENT
from belle_agent.scopes import compile_scopes
from belle_agent.silence import REPLY_OPTIONAL_KEY

CHAT = "C1"
TEAM = "T1"
ASKER = "U0ASKER001"
# Whoever happened to be watching when the prompt went up. Routinely not the
# person who asked for the work, which is why the resume cannot be settled for
# them.
CLICKER = "U0CLICKER1"
BOT = "U-BOT"
QUESTION_ID = "chatcmpl-tool-0123456789abcdef"
BODY = {"event_id": "Ev1", "team_id": TEAM}
# The card the prompt was posted on, and the thread that card sits in. The pair
# is what a resume routes by when nothing outranks it.
CARD_TS = "1710000016.000100"
CARD_THREAD = "1710000001.000100"


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


class _RecordingSlackClient:
    """Just enough Slack to see what was posted and what was edited."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "user": {"id": kwargs.get("user", ""), "is_bot": False}}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        raise RuntimeError(f"{method} is not available in this workspace")


def _channel(
    policy: dict | None = None, *, reply: str = "optional", group_chat_mode: str = "all"
) -> tuple[SlackChannel, list[Message]]:
    """A conversation licensed to wake turns, and the requests it dispatches."""
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=[CHAT],
        group_chat_mode=group_chat_mode,
        conversation_overrides={
            CHAT: slack_connect.SlackChannelOverride(events=policy, reply=reply)
        },
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._identity.bot_user_id = BOT
    channel._client = _RecordingSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _member_event(user: str = "U-NEW") -> dict:
    """A membership change: the family with no message to anchor to."""
    return {
        "type": "member_joined_channel",
        "user": user,
        "channel": CHAT,
        "channel_type": "C",
        "team": TEAM,
        "event_ts": "1710000300.000400",
    }


def _message_event(text: str = "shipping it now") -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": CHAT,
        "user": ASKER,
        "text": text,
        "ts": "1710000010.000100",
        "thread_ts": CARD_THREAD,
    }


async def _woken_by_an_event(channel: SlackChannel) -> None:
    await channel._route_inbound_event(_member_event(), BODY)


async def _woken_by_a_message(channel: SlackChannel, text: str) -> None:
    await channel._handle_slack_event(
        _message_event(text), BODY, is_dm=False, trigger="all"
    )


async def _ask(channel: SlackChannel, request: Message) -> None:
    await channel.send(
        permission_question_event(request, chat_id=CHAT, question_id=QUESTION_ID)
    )


async def _approve(
    channel: SlackChannel, *, thread_ts: str = CARD_THREAD, question_id: str = QUESTION_ID
) -> None:
    """Somebody presses Approve on the card, from inside its thread."""
    await channel._dispatch_answer(
        channel._pending_questions[question_id],
        values=["approve"],
        body={
            "container": {
                "channel_id": CHAT,
                "message_ts": CARD_TS,
                "thread_ts": thread_ts,
            },
            "channel": {"id": CHAT},
        },
        user_id=CLICKER,
    )


async def _interrupt_and_resume(channel: SlackChannel, received: list[Message]) -> Message:
    """Pause the latest turn on a permission prompt and approve it."""
    await _ask(channel, received[-1])
    channel._client.posts.clear()
    await _approve(channel)
    return received[-1]


def _reply_to(request: Message, content: str) -> Message:
    """The answer to ``request``, holding the metadata the request held.

    The gateway merges request metadata into every response it builds, so this
    is the shape a reply arrives in -- and the whole of why a fact dropped on
    the way in cannot be recovered on the way out.
    """
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": EventType.CHAT_FINAL.value, "content": content},
        event_type=EventType.CHAT_FINAL,
        metadata=dict(request.metadata or {}),
    )


# ----------------------------------------------------------------------
# Optional-reply license survives a paused turn.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resumed_turn_that_says_nothing_posts_nothing() -> None:
    """End to end: approve a prompt, then decline to answer."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.id.startswith("slack-answer-")
    await channel.send(_reply_to(resumed, NO_REPLY_SENTINEL))

    assert channel._client.posts == []


@pytest.mark.asyncio
async def test_a_resume_holds_the_contract_its_turn_was_dispatched_with() -> None:
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    assert received[0].metadata[REPLY_OPTIONAL_KEY] is True
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata[REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_a_resume_uses_the_initiators_agent_scope() -> None:
    channel, received = _channel(group_chat_mode="mention")
    channel.config.scopes = compile_scopes([
        {"match": {"channel": "slack", "chat": CHAT, "user": [ASKER]},
         "agent": {"expert": "asker-expert", "subagents": ["research_agent"],
                   "skills": {"available": ["ambient"], "required": ["ambient"]}}},
        {"match": {"channel": "slack", "chat": CHAT, "user": [CLICKER]},
         "agent": {"expert": "clicker-expert", "subagents": [],
                   "skills": {"available": [], "required": []}}},
    ], warn=lambda *args: None)

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    first = received[0]
    resumed = await _interrupt_and_resume(channel, received)

    for request in (first, resumed):
        assert request.params["agent_template_name"] == "asker-expert"
        assert request.params["agent_subagents_available"] == ["research_agent"]
        assert request.params["agent_skills_available"] == ["ambient"]
        assert request.params["agent_skills_required"] == ["ambient"]
    assert resumed.user_id == CLICKER


@pytest.mark.asyncio
async def test_a_resume_is_not_granted_a_contract_its_turn_lacked() -> None:
    """A room that licensed nothing grants nothing on the way back.

    ``delivery.reply`` here is the default, so no turn in this conversation may
    decline. The dispatch stamps no key, the resume stamps none either, and the
    token is posted as the ordinary text it is. Nothing on the resume path can
    widen the contract: the key it carries is the dispatch's own answer, and the
    outbound reader requires the live config to agree with it besides.
    """
    channel, received = _channel(reply="required", group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    assert REPLY_OPTIONAL_KEY not in received[0].metadata
    resumed = await _interrupt_and_resume(channel, received)

    assert REPLY_OPTIONAL_KEY not in resumed.metadata
    # And the behaviour that key decides is unchanged: the token is text.
    await channel.send(_reply_to(resumed, NO_REPLY_SENTINEL))
    assert [post.get("text") for post in channel._client.posts] == [NO_REPLY_SENTINEL]


@pytest.mark.asyncio
async def test_a_resumed_addressed_turn_still_holds_the_matcher() -> None:
    """The matcher fact crosses the pause, and the offer's rule does not.

    The message named the bot, so its own prompt carried no fragment. What the
    room licensed is what the resume inherits, and a turn that writes the token
    after the approval has it honoured rather than posted.
    """
    channel, received = _channel(group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    assert received[0].metadata[REPLY_OPTIONAL_KEY] is True
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata[REPLY_OPTIONAL_KEY] is True
    # The address is diagnostic and crosses the pause with it, so the line
    # recording the silence reads as the addressed case it is.
    assert resumed.metadata[slack_connect.SLACK_ADDRESSED_KEY] is True
    await channel.send(_reply_to(resumed, NO_REPLY_SENTINEL))
    assert channel._client.posts == []


@pytest.mark.asyncio
async def test_a_turn_interrupted_twice_still_holds_the_contract() -> None:
    """The second prompt reads the entry the first resume opened."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    await _interrupt_and_resume(channel, received)
    await _ask(channel, received[-1])
    await _approve(channel, question_id=QUESTION_ID)

    assert received[-1].metadata[REPLY_OPTIONAL_KEY] is True


# ----------------------------------------------------------------------
# post_as_root: where a resumed event turn answers
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resumed_event_turn_still_answers_at_the_channel_root() -> None:
    """The card was clicked inside a thread; the turn is still about the room."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    assert received[0].metadata[POST_AS_ROOT_KEY] is True
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata[POST_AS_ROOT_KEY] is True
    # The click's own thread is on the bag and is outranked, which is the whole
    # of what the key is for. Asked of the send path rather than of the ladder:
    # the ladder is free of the key by design, and ``_extract_delivery`` is the
    # one place it is applied.
    assert resumed.metadata["slack_thread_ts"] == CARD_THREAD
    reply = _reply_to(resumed, "Two people joined this week.")
    assert channel._extract_delivery(reply, None) == (CHAT, "")


@pytest.mark.asyncio
async def test_a_resumed_message_turn_is_not_moved_to_the_root() -> None:
    """A turn with a message behind it never held the key and does not gain one."""
    channel, received = _channel(group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    assert POST_AS_ROOT_KEY not in received[0].metadata
    resumed = await _interrupt_and_resume(channel, received)

    assert POST_AS_ROOT_KEY not in resumed.metadata
    reply = _reply_to(resumed, "Shipped.")
    assert channel._extract_delivery(reply, None) == (CHAT, CARD_THREAD)


# ----------------------------------------------------------------------
# slack_trigger: what woke the turn being resumed
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_names_the_trigger_that_woke_its_turn() -> None:
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata["slack_trigger"] == TRIGGER_EVENT


@pytest.mark.asyncio
async def test_a_resumed_message_turn_names_its_own_trigger() -> None:
    channel, received = _channel(group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata["slack_trigger"] == received[0].metadata["slack_trigger"]


# ----------------------------------------------------------------------
# the record in between
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_question_holds_what_the_dispatch_decided() -> None:
    """The pending question is the only place the two ends are both in reach."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    await _ask(channel, received[-1])

    pending = channel._pending_questions[QUESTION_ID]
    assert pending.initiator.silence_is_honoured is True
    assert pending.initiator.post_as_root is True
    assert pending.initiator.trigger == TRIGGER_EVENT
    # Nothing addresses the bot with a membership change, and the field says so
    # rather than being left to the reader of a log line to guess.
    assert pending.initiator.addressed is False


@pytest.mark.asyncio
async def test_a_question_with_no_initiator_on_record_claims_nothing() -> None:
    """An entry that aged out leaves a resume exactly as bare as it was."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    channel._admission.turn_initiators.clear()
    await _ask(channel, received[-1])

    pending = channel._pending_questions[QUESTION_ID]
    assert pending.initiator is None

    await _approve(channel)
    resumed = received[-1]
    assert REPLY_OPTIONAL_KEY not in resumed.metadata
    assert POST_AS_ROOT_KEY not in resumed.metadata
    assert "slack_trigger" not in resumed.metadata
