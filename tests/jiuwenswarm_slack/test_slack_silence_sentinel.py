"""Test optional replies and the ``NO_REPLY`` delivery contract.

Only a reply made of the sentinel is withheld. The broader detector logs
descriptions of silence without suppressing them. Addressing the bot removes
the silence instruction but keeps the sentinel filter active.
"""

import asyncio
import time
from typing import Any

import pytest
from belle_agent.platforms.slack import activity as slack_activity
from belle_agent.platforms.slack import streaming as slack_streaming
from belle_agent.platforms.slack.scoped_config import (
    apply_scopes_to_slack_overrides,
    describe_configured_channels,
)
from belle_agent.scopes import (
    REPLY_DEFAULT,
    REPLY_OPTIONAL,
    REPLY_REQUIRED,
    compile_scopes,
)

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.silence import REPLY_OPTIONAL_KEY, SILENCE_UNSTORED_KEY
from belle_agent.integrations.jiuwenswarm import slack_stream_turn
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    NO_REPLY_INSTRUCTION,
    NO_REPLY_SENTINEL,
    SlackChannel,
    SlackChannelConfig,
    is_no_reply_sentinel,
    looks_like_a_declined_reply,
)

ASKER = "U0ASKER001"
BOT = "U0BOTUSER0"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"
# Reads every message and may decline to answer.
OPTIONAL = "C0OPTIONAL"
# Reads every message and owes an answer to each, which is the default.
REQUIRED = "C0REQUIRED"
# Answers only when addressed, and may still decline. The pair with OPTIONAL is
# the point of the key: which trigger woke a turn and whether that turn owes an
# answer are separate questions.
ADDRESSED_ONLY = "C0MENTION1"
THREAD = "1710000001.000100"

# Descriptions of silence are delivered and logged, not suppressed.
DESCRIBED_SILENCE = "(no reply)"
DESCRIBED_SILENCE_AT_LENGTH = (
    "(No reply needed; nothing to add.)"
)

_SCOPES = (
    {
        "match": {"channel": "slack", "chat": OPTIONAL},
        "delivery": {"mode": ["all"], "reply": REPLY_OPTIONAL},
    },
    {
        "match": {"channel": "slack", "chat": REQUIRED},
        "delivery": {"mode": ["all"]},
    },
    {
        "match": {"channel": "slack", "chat": ADDRESSED_ONLY},
        "delivery": {"mode": ["mention"], "reply": REPLY_OPTIONAL},
    },
)


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


@pytest.fixture(autouse=True)
def _fast_streaming(monkeypatch):
    """Collapse the debounce and the per-channel spacing a preview waits out."""
    monkeypatch.setattr(slack_stream_turn, "_STREAM_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_streaming, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(slack_stream_turn, "_STREAM_APPEND_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_streaming, "_STREAM_APPEND_MIN_INTERVAL_SECONDS", 0.0)


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "warning", record)
    monkeypatch.setattr(slack_activity.logger, "warning", record)
    return recorded


@pytest.fixture
def notes(monkeypatch) -> list[str]:
    """Every INFO line, for the one that says the turn stayed quiet."""
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "info", record)
    monkeypatch.setattr(slack_activity.logger, "info", record)
    return recorded


class _RecordingSlackClient:
    """Just enough Slack to see what was posted, edited, deleted and reacted."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        self.deleted.append((kwargs.get("channel", ""), kwargs.get("ts", "")))
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        """Refuse the streaming methods, as a workspace without them does.

        That refusal is what puts the preview on the documented second rung of
        the ladder: the reply is shown with edits instead, in a message posted
        with chat.postMessage, which deletes as it is. A native stream must be
        stopped before Slack deletes it; ``_StreamingSlackClient`` covers that.
        """
        raise RuntimeError(f"{method} is not available in this workspace")

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.added.append(
            (
                kwargs.get("channel", ""),
                kwargs.get("timestamp", ""),
                kwargs.get("name", ""),
            )
        )
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed.append(
            (
                kwargs.get("channel", ""),
                kwargs.get("timestamp", ""),
                kwargs.get("name", ""),
            )
        )
        return {"ok": True}


def _config(entries: Any = _SCOPES, **overrides: Any) -> SlackChannelConfig:
    """A config built the way ``app_gateway`` builds one, from written scopes."""
    group_chat_mode = overrides.pop("group_chat_mode", "mention")
    optional_reply_mode = overrides.pop("optional_reply_mode", "sentinel_unstored")
    scopes = compile_scopes(list(entries), warn=lambda *a: None)
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": group_chat_mode}, scopes=scopes
    )
    return SlackChannelConfig(
        enabled=True,
        allow_from=[ASKER],
        group_chat_mode=group_chat_mode,
        conversation_overrides=per_chat,
        platform_override=platform,
        scopes=scopes,
        optional_reply_mode=optional_reply_mode,
        **overrides,
    )


def _channel(
    entries: Any = _SCOPES, **overrides: Any
) -> tuple[SlackChannel, list[Message]]:
    channel = SlackChannel(_config(entries, **overrides), RobotMessageRouter())
    channel._running = True
    channel._identity.bot_user_id = BOT
    channel._client = _RecordingSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _room_event(
    *, channel_id: str = OPTIONAL, ts: str, text: str = "shipping it now"
) -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": channel_id,
        "user": ASKER,
        "text": text,
        "ts": ts,
        "thread_ts": THREAD,
    }


async def _post(
    channel: SlackChannel, event: dict, event_id: str, *, trigger: str = "all"
) -> str:
    return await channel._handle_slack_event(
        event, {"event_id": event_id, "team_id": TEAM}, is_dm=False, trigger=trigger
    )


def _reply_to(request: Message, content: str) -> Message:
    """The answer to ``request``, addressed the way the request addressed itself.

    Holds the request's own metadata, which is where the connector recorded
    whether this turn was offered the contract. The gateway merges request
    metadata into every response it builds, so this is the shape a reply arrives
    in.
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


async def _answer(
    channel: SlackChannel,
    received: list[Message],
    content: str,
    *,
    channel_id: str = OPTIONAL,
    trigger: str = "all",
    text: str = "shipping it now",
    ts: str = "1710000010.000100",
) -> list[dict[str, Any]]:
    """Run one message through, answer it, and return what was posted."""
    await _post(
        channel,
        _room_event(channel_id=channel_id, ts=ts, text=text),
        f"Ev{ts}",
        trigger=trigger,
    )
    channel._client.posts.clear()
    await channel.send(_reply_to(received[-1], content))
    return channel._client.posts


async def _settle() -> None:
    """Let the debounced preview writes run."""
    for _ in range(10):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# The detector, which decides a log line and is loose
# --------------------------------------------------------------------------


def test_descriptions_of_silence_are_noticed_by_the_detector():
    assert looks_like_a_declined_reply(DESCRIBED_SILENCE)
    assert looks_like_a_declined_reply(DESCRIBED_SILENCE_AT_LENGTH)


@pytest.mark.parametrize(
    "reply",
    [
        f"{NO_REPLY_SENTINEL} {NO_REPLY_SENTINEL}",
        f"The answer is 42. {NO_REPLY_SENTINEL}",
        f"Write {NO_REPLY_SENTINEL} on its own line.",
        "NO REPLY",
        "no-reply",
        "[no reply]",
        "(nothing to say)",
        "Nothing to add.",
        "no reply needed",
    ],
)
def test_a_turn_reaching_for_any_of_these_shapes_is_counted(reply):
    assert looks_like_a_declined_reply(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Two checks failed on the last run.",
        "I updated the handler (nothing to add to the tests) and pushed.",
        (
            "The deploy is green; there is nothing to add to the report beyond"
            " the two failures listed above, which are both known flakes."
        ),
        "",
    ],
)
def test_prose_that_merely_uses_the_words_is_not_counted(reply):
    assert not looks_like_a_declined_reply(reply)


def test_the_matcher_and_the_detector_disagree_on_purpose():
    for reply in (DESCRIBED_SILENCE, "Nothing to add.", "no-reply"):
        assert looks_like_a_declined_reply(reply)
        assert not is_no_reply_sentinel(reply)


# --------------------------------------------------------------------------
# delivery.reply, and which conversations it reaches
# --------------------------------------------------------------------------


def test_a_conversation_nobody_wrote_the_key_for_owes_an_answer():
    channel, _ = _channel()

    assert REPLY_DEFAULT == REPLY_REQUIRED
    assert not channel._channel_reply_is_optional(REQUIRED)
    assert not channel._channel_reply_is_optional("C0UNNAMED1")


def test_a_conversation_the_key_licenses_may_decline():
    channel, _ = _channel()

    assert channel._channel_reply_is_optional(OPTIONAL)


def test_the_licence_is_independent_of_what_wakes_the_turn():
    channel, _ = _channel()

    assert channel._channel_triggers(OPTIONAL) == channel._channel_triggers(REQUIRED)
    assert channel._channel_reply_is_optional(OPTIONAL)
    assert not channel._channel_reply_is_optional(REQUIRED)
    assert channel._channel_reply_is_optional(ADDRESSED_ONLY)


def test_a_conversation_rule_beats_a_platform_rule():
    channel, _ = _channel(
        (
            {"match": {"channel": "slack"}, "delivery": {"reply": REPLY_OPTIONAL}},
            {
                "match": {"channel": "slack", "chat": REQUIRED},
                "delivery": {"reply": REPLY_REQUIRED},
            },
        )
    )

    assert channel._channel_reply_is_optional(OPTIONAL)
    assert not channel._channel_reply_is_optional(REQUIRED)


def test_a_word_the_vocabulary_does_not_hold_is_refused(refusal):
    error = refusal([{"match": {"channel": "slack", "chat": OPTIONAL}, "delivery": {"reply": "sometimes"}}])

    assert "scopes[0].delivery.reply='sometimes' is not one of required, optional" in error


def test_a_rule_naming_a_sender_is_refused(refusal):
    error = refusal(
        [
            {
                "match": {"channel": "slack", "chat": OPTIONAL, "user": [ASKER]},
                "delivery": {"reply": REPLY_OPTIONAL},
            }
        ]
    )

    assert "scopes[0].delivery.reply cannot be in a rule that matches on user" in error


def test_a_direct_message_is_never_licensed_to_stay_silent():
    channel, _ = _channel(
        ({"match": {"channel": "slack"}, "delivery": {"reply": REPLY_OPTIONAL}},)
    )

    assert not channel._channel_reply_is_optional(DM)


def test_only_the_conversations_that_wrote_the_key_are_reached():
    channel, _ = _channel()
    conversations = (
        OPTIONAL,
        REQUIRED,
        ADDRESSED_ONLY,
        "C0NEVERNAMED",
        "G0PRIVATE001",
        DM,
    )

    reached = [
        chat for chat in conversations if channel._channel_reply_is_optional(chat)
    ]

    assert reached == [OPTIONAL, ADDRESSED_ONLY]


def test_the_startup_summary_names_a_conversation_that_may_decline():
    channel, _ = _channel()

    summary = describe_configured_channels(channel.config)

    assert f"{OPTIONAL} " in summary
    assert f"reply={REPLY_OPTIONAL}" in summary
    # The room that wrote nothing is named without the key, because an unset key
    # is already the default rather than a departure from it.
    assert f"{REQUIRED} mode=[all] prompt=none model=default via=" in summary


# --------------------------------------------------------------------------
# The instruction
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_instruction_reaches_a_licensed_conversation():
    channel, received = _channel()

    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")

    assert NO_REPLY_INSTRUCTION in received[0].params["content"]
    assert received[0].metadata[REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_the_instruction_stays_out_of_a_conversation_that_owes_an_answer():
    channel, received = _channel()

    await _post(
        channel, _room_event(channel_id=REQUIRED, ts="1710000010.000100"), "Ev01"
    )

    assert NO_REPLY_SENTINEL not in received[0].params["content"]
    assert REPLY_OPTIONAL_KEY not in received[0].metadata


@pytest.mark.asyncio
async def test_the_instruction_sits_below_an_operator_prompt_rather_than_replacing_it():
    channel, received = _channel(
        (
            {
                "match": {"channel": "slack", "chat": OPTIONAL},
                "delivery": {
                    "mode": ["all"],
                    "reply": REPLY_OPTIONAL,
                    "prompt": "Answer in French.",
                },
            },
        )
    )

    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")

    content = received[0].params["content"]
    assert content.endswith(f"Answer in French.\n\n{NO_REPLY_INSTRUCTION}")


# --------------------------------------------------------------------------
# A message that addressed the bot
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat, addressed",
    [(OPTIONAL, False), (OPTIONAL, True), (REQUIRED, False), (REQUIRED, True)],
)
async def test_the_offer_and_the_matcher_are_settled_separately(chat, addressed):
    channel, received = _channel()
    licensed = chat == OPTIONAL

    posts = await _answer(
        channel,
        received,
        NO_REPLY_SENTINEL,
        channel_id=chat,
        trigger="mention" if addressed else "all",
        text=f"<@{BOT}> what broke?" if addressed else "shipping it now",
    )

    assert (NO_REPLY_INSTRUCTION in received[-1].params["content"]) is (
        licensed and not addressed
    )
    assert received[-1].metadata.get(REPLY_OPTIONAL_KEY, False) is licensed
    assert [post["text"] for post in posts] == ([] if licensed else [NO_REPLY_SENTINEL])


@pytest.mark.asyncio
async def test_a_message_opening_with_a_mention_is_offered_no_contract():
    channel, received = _channel()

    await _post(
        channel,
        _room_event(ts="1710000010.000100", text=f"<@{BOT}> what broke?"),
        "Ev01",
        trigger="mention",
    )

    assert NO_REPLY_SENTINEL not in received[0].params["content"]
    assert received[0].metadata[REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_the_mention_is_read_before_it_is_stripped_from_the_text():
    channel, received = _channel()

    await _post(
        channel,
        _room_event(ts="1710000010.000100", text=f"<@{BOT}> what broke?"),
        "Ev01",
        trigger="mention",
    )

    content = received[0].params["content"]
    assert f"<@{BOT}>" not in content
    assert content.startswith("what broke?")
    assert NO_REPLY_INSTRUCTION not in content


@pytest.mark.asyncio
async def test_a_mention_further_into_a_message_is_a_reference_and_stays_optional():
    channel, received = _channel()

    await _post(
        channel,
        _room_event(ts="1710000010.000100", text=f"ask <@{BOT}> if you like"),
        "Ev01",
    )

    assert NO_REPLY_INSTRUCTION in received[0].params["content"]
    assert received[0].metadata[REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_an_addressed_message_answered_with_the_token_posts_nothing(warnings):
    channel, received = _channel()

    posts = await _answer(
        channel,
        received,
        NO_REPLY_SENTINEL,
        trigger="mention",
        text=f"<@{BOT}> what broke?",
    )

    assert posts == []
    assert warnings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addressed, stamped",
    [(True, "addressed=yes"), (False, "addressed=no")],
)
async def test_the_withheld_line_says_whether_the_bot_was_addressed(
    addressed, stamped, notes
):
    channel, received = _channel()

    posts = await _answer(
        channel,
        received,
        NO_REPLY_SENTINEL,
        trigger="mention" if addressed else "all",
        text=f"<@{BOT}> what broke?" if addressed else "shipping it now",
    )

    assert posts == []
    written = [line for line in notes if "nothing posted" in line]
    assert len(written) == 1
    assert stamped in written[0]


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        NO_REPLY_SENTINEL,
        NO_REPLY_SENTINEL.lower(),
        f"`{NO_REPLY_SENTINEL}`",
        f"({NO_REPLY_SENTINEL})",
        f"{NO_REPLY_SENTINEL}.",
        f"​ {NO_REPLY_SENTINEL} ​",
        f"{NO_REPLY_SENTINEL}\x00\x00",
        f"{NO_REPLY_SENTINEL} \x00{NO_REPLY_SENTINEL}",
    ],
)
async def test_the_token_posts_nothing(reply):
    channel, received = _channel()

    assert await _answer(channel, received, reply) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [DESCRIBED_SILENCE, DESCRIBED_SILENCE_AT_LENGTH])
async def test_a_reply_describing_its_own_silence_is_posted_and_counted(
    reply, warnings
):
    channel, received = _channel()

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]
    assert any(repr(reply) in line for line in warnings)


@pytest.mark.asyncio
async def test_a_real_answer_ending_in_the_token_is_posted_whole(warnings):
    channel, received = _channel()
    reply = f"Deploy is green, two checks pending.\n\n{NO_REPLY_SENTINEL}"

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]
    assert any(repr(reply) in line and "near miss" in line for line in warnings)


@pytest.mark.asyncio
async def test_a_reply_discussing_the_token_in_prose_is_posted_and_counted(warnings):
    channel, received = _channel()
    reply = f"Write {NO_REPLY_SENTINEL} on its own line when you have nothing to add."

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]
    assert any("near miss" in line for line in warnings)


@pytest.mark.asyncio
async def test_tokens_with_meaningful_text_between_them_are_posted():
    channel, received = _channel()
    reply = f"{NO_REPLY_SENTINEL}栈{NO_REPLY_SENTINEL}"

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]


@pytest.mark.asyncio
async def test_an_ordinary_reply_is_posted_unchanged_and_says_nothing_extra(warnings):
    channel, received = _channel()

    posts = await _answer(channel, received, "Two checks failed on the last run.")

    assert [post["text"] for post in posts] == ["Two checks failed on the last run."]
    assert warnings == []


@pytest.mark.asyncio
async def test_a_conversation_that_owes_an_answer_posts_the_token_as_text(warnings):
    channel, received = _channel()

    posts = await _answer(
        channel, received, NO_REPLY_SENTINEL, channel_id=REQUIRED
    )

    assert [post["text"] for post in posts] == [NO_REPLY_SENTINEL]
    assert warnings == []


@pytest.mark.asyncio
async def test_unstored_key_alone_does_not_license_slack_silence():
    channel, received = _channel()
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    reply = _reply_to(received[0], NO_REPLY_SENTINEL)
    reply.metadata.pop(REPLY_OPTIONAL_KEY)
    reply.metadata[SILENCE_UNSTORED_KEY] = True

    assert not channel._reply_was_optional(reply, OPTIONAL)
    reply.metadata[REPLY_OPTIONAL_KEY] = "true"
    assert not channel._reply_was_optional(reply, OPTIONAL)
    reply.metadata[REPLY_OPTIONAL_KEY] = True
    assert not channel._reply_was_optional(reply, REQUIRED)
    assert not channel._reply_was_optional(reply, DM)


@pytest.mark.asyncio
async def test_a_reply_with_no_text_still_posts_nothing():
    """The path this feature sits beside, unchanged."""
    channel, received = _channel()

    assert await _answer(channel, received, "") == []


@pytest.mark.asyncio
async def test_a_reply_that_never_named_a_licensed_turn_is_posted():
    channel, _ = _channel()
    reply = Message(
        id="cron-1",
        type="event",
        channel_id="slack",
        session_id=f"slack_{TEAM}_{OPTIONAL}",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": EventType.CHAT_FINAL.value,
            "content": NO_REPLY_SENTINEL,
        },
        event_type=EventType.CHAT_FINAL,
        metadata={"slack_channel_id": OPTIONAL},
    )

    await channel.send(reply)

    assert [post["text"] for post in channel._client.posts] == [NO_REPLY_SENTINEL]


# --------------------------------------------------------------------------
# What a withheld reply leaves behind
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_withheld_reply_is_said_once_in_the_log(notes):
    channel, received = _channel()

    await _answer(channel, received, NO_REPLY_SENTINEL)

    written = [line for line in notes if NO_REPLY_SENTINEL in line and OPTIONAL in line]
    assert len(written) == 1
    assert "nothing posted" in written[0]


@pytest.mark.asyncio
async def test_a_silent_turn_is_marked_exactly_like_one_that_replied():
    silent, silent_received = _channel()
    silent_posts = await _answer(
        silent, silent_received, NO_REPLY_SENTINEL, ts="1710000010.000100"
    )

    spoken, spoken_received = _channel()
    spoken_posts = await _answer(
        spoken, spoken_received, "Here is the answer.", ts="1710000020.000100"
    )

    def marks(client: _RecordingSlackClient) -> tuple[list[str], list[str]]:
        return (
            [added[2] for added in client.added],
            [removed[2] for removed in client.removed],
        )

    assert marks(silent._client) == marks(spoken._client)
    assert marks(silent._client) == (["eyes"], ["eyes"])
    assert [added[0] for added in silent._client.added] == [OPTIONAL]
    assert silent_posts == []
    assert spoken_posts != []


@pytest.mark.asyncio
async def test_a_withheld_reply_takes_its_streamed_preview_off_the_screen(notes):
    channel, received = _channel(enable_streaming=True)
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    await channel.send(_delta(request, f"{NO_REPLY_SENTINEL}\n"))
    await _settle()
    preview = list(channel._client.posts)
    channel._client.posts.clear()

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert len(preview) == 1
    assert channel._client.posts == []
    assert channel._client.deleted == [(OPTIONAL, "1710000099.000001")]
    assert any("preview deleted" in line for line in notes)


class _StreamingSlackClient(_RecordingSlackClient):
    """Slack with native streams, refusing to delete one that is still open."""

    def __init__(
        self, *, stop_error: Exception | None = None, ended_by_slack: bool = False
    ) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.open_streams: set[str] = set()
        self._stop_error = stop_error
        self._ended_by_slack = ended_by_slack

    async def chat_startStream(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append("startStream")
        self.open_streams.add("1710000099.000001")
        return {"ts": "1710000099.000001"}

    async def chat_appendStream(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append("appendStream")
        return {"ok": True}

    async def chat_stopStream(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append("stopStream")
        if self._ended_by_slack:
            self.open_streams.clear()
        if self._stop_error is not None:
            raise self._stop_error
        self.open_streams.discard(kwargs.get("ts", ""))
        return {"ok": True}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append("delete")
        if kwargs.get("ts") in self.open_streams:
            raise RuntimeError("cant_delete_message")
        return await super().chat_delete(**kwargs)


async def _withhold_a_native_preview(client: _StreamingSlackClient) -> SlackChannel:
    channel, received = _channel(enable_streaming=True)
    channel._client = client
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    await channel.send(_delta(request, f"{NO_REPLY_SENTINEL}\n"))
    await _settle()
    assert client.calls == ["startStream"]
    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))
    return channel


@pytest.mark.asyncio
async def test_a_withheld_reply_stops_its_native_stream_before_deleting_it(notes):
    client = _StreamingSlackClient()
    await _withhold_a_native_preview(client)

    assert client.calls == ["startStream", "stopStream", "delete"]
    assert client.deleted == [(OPTIONAL, "1710000099.000001")]
    assert client.posts == []
    assert any("preview deleted" in line for line in notes)


@pytest.mark.asyncio
async def test_a_stream_slack_already_ended_is_still_deleted(warnings):
    client = _StreamingSlackClient(
        stop_error=RuntimeError("message_not_in_streaming_state"),
        ended_by_slack=True,
    )
    await _withhold_a_native_preview(client)

    assert client.deleted == [(OPTIONAL, "1710000099.000001")]
    assert not any("could not be deleted" in line for line in warnings)


@pytest.mark.asyncio
async def test_a_preview_left_on_screen_names_why_its_stream_did_not_stop(warnings):
    client = _StreamingSlackClient(stop_error=RuntimeError("internal_error"))
    await _withhold_a_native_preview(client)

    assert client.deleted == []
    [warning] = [line for line in warnings if "could not be deleted" in line]
    assert "cant_delete_message" in warning
    assert "stream_stop=internal_error" in warning


@pytest.mark.asyncio
async def test_a_delivered_reply_keeps_the_preview_it_was_written_into():
    channel, received = _channel(enable_streaming=True)
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    await channel.send(_delta(request, "Deploy is \n"))
    await _settle()
    await channel.send(_reply_to(request, "Deploy is green."))

    assert channel._client.deleted == []
    assert [update["text"] for update in channel._client.updates][-1] == (
        "Deploy is green."
    )


@pytest.mark.asyncio
async def test_a_withheld_reply_leaves_no_open_stream_behind():
    channel, received = _channel(enable_streaming=True)
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    await channel.send(_delta(request, f"{NO_REPLY_SENTINEL}\n"))
    await _settle()
    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert channel._streams == {}


def _delta(request: Message, content: str) -> Message:
    """One token of a reply being written, addressed like the reply itself."""
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": EventType.CHAT_DELTA.value, "content": content},
        event_type=EventType.CHAT_DELTA,
        metadata=dict(request.metadata or {}),
    )


# --------------------------------------------------------------------------
# The activity card a silent turn leaves behind, and does not
# --------------------------------------------------------------------------


def _tool_call(request: Message, call_id: str = "call-1") -> Message:
    """One ordinary tool call, addressed like the reply to ``request``.

    Any tracked work will do -- the card exists for the turn, not for the tool
    -- and one call is the cheapest way to put a card on screen.
    """
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": EventType.CHAT_TOOL_CALL.value,
            "tool_call": {
                "name": "read_file",
                "arguments": {"path": "/etc/hosts"},
                "tool_call_id": call_id,
            },
        },
        event_type=EventType.CHAT_TOOL_CALL,
        metadata=dict(request.metadata or {}),
    )


def _error_final(request: Message, error: str) -> Message:
    """The turn dying, shaped as the gateway shapes it: ``payload["error"]``."""
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=False,
        payload={"event_type": EventType.CHAT_ERROR.value, "error": error},
        event_type=EventType.CHAT_ERROR,
        metadata=dict(request.metadata or {}),
    )


async def _turn_with_a_card(
    channel_id: str = OPTIONAL,
    ts: str = "1710000010.000100",
    **overrides: Any,
) -> tuple[SlackChannel, Message]:
    """A licensed turn that has done tracked work, with its card on screen."""
    channel, received = _channel(
        activity_card_delay_seconds=0.0,
        activity_card_min_edit_seconds=0.0,
        **overrides,
    )
    await _post(channel, _room_event(channel_id=channel_id, ts=ts), f"Ev{ts}")
    request = received[-1]
    await channel.send(_tool_call(request))
    await _settle()
    assert [post.get("channel") for post in channel._client.posts] == [channel_id]
    channel._client.posts.clear()
    channel._client.updates.clear()
    return channel, request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        NO_REPLY_SENTINEL,
        f"`{NO_REPLY_SENTINEL}`",
        "",
        "   \n\t ",
    ],
)
async def test_a_silent_turn_takes_its_activity_card_off_the_screen(reply, notes):
    channel, request = await _turn_with_a_card()

    await channel.send(_reply_to(request, reply))

    assert channel._client.deleted == [(OPTIONAL, "1710000099.000001")]
    assert channel._client.posts == []
    assert channel._client.updates == []
    assert channel._activity_records == {}
    assert any("activity card deleted" in line for line in notes)


@pytest.mark.asyncio
async def test_the_card_is_deleted_rather_than_settled_and_then_deleted():
    channel, request = await _turn_with_a_card()

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert len(channel._client.updates) == 0
    assert len(channel._client.deleted) == 1


@pytest.mark.asyncio
async def test_a_turn_that_replied_keeps_the_card_it_settled():
    channel, request = await _turn_with_a_card()

    await channel.send(_reply_to(request, "Two checks failed on the last run."))

    assert channel._client.deleted == []
    assert len(channel._client.updates) == 1
    assert [post["text"] for post in channel._client.posts] == [
        "Two checks failed on the last run."
    ]


@pytest.mark.asyncio
async def test_a_silent_turn_that_never_posted_a_card_makes_no_call():
    channel, received = _channel(
        activity_card_delay_seconds=30.0, activity_card_min_edit_seconds=0.0
    )
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]
    await channel.send(_tool_call(request))
    await _settle()
    assert channel._client.posts == []

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert channel._client.posts == []
    assert channel._client.updates == []
    assert channel._client.deleted == []
    assert channel._activity_records == {}


@pytest.mark.asyncio
async def test_a_refused_delete_leaves_a_settled_card_and_warns(warnings):
    channel, request = await _turn_with_a_card()

    async def refuse(**kwargs: Any) -> dict[str, bool]:
        raise RuntimeError("cant_delete_message")

    channel._client.chat_delete = refuse

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert channel._client.deleted == []
    assert len(channel._client.updates) == 1
    assert channel._client.posts == []
    assert any("could not be deleted" in line for line in warnings)


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_its_card_and_the_failure_is_not_reposted():
    channel, request = await _turn_with_a_card()

    await channel.send(_error_final(request, "[181001] model call failed"))

    assert channel._client.deleted == []
    assert channel._client.posts == []
    assert len(channel._client.updates) == 1
    assert "model call failed" in str(channel._client.updates[-1])


@pytest.mark.asyncio
async def test_no_error_event_can_ask_for_a_discard():
    channel, received = _channel()
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    assert channel._turn_says_nothing(_reply_to(request, NO_REPLY_SENTINEL), None)
    assert not channel._turn_says_nothing(
        _error_final(request, NO_REPLY_SENTINEL), None
    )
    assert not channel._turn_says_nothing(_error_final(request, ""), None)


@pytest.mark.asyncio
async def test_a_record_carrying_a_failure_refuses_the_delete_on_its_own_account():
    channel, request = await _turn_with_a_card()
    key = channel._activity_key(_reply_to(request, ""), None)
    record = channel._activity_records[key]
    record.failed = True
    record.harness_error = "[181001] model call failed"

    assert not await channel._discard_activity_card(key, record, reason="silence")
    assert channel._client.deleted == []
    assert channel._activity_records[key] is record


@pytest.mark.asyncio
async def test_a_stopped_turn_takes_its_card_down_and_the_terminal_adds_nothing():
    channel, request = await _turn_with_a_card()
    key = channel._activity_key(_reply_to(request, ""), None)
    record = channel._activity_records[key]
    record.stop_requested = True
    await channel._settle_stopped_card(record)
    assert len(channel._client.deleted) == 1
    assert channel._client.updates == []

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert len(channel._client.deleted) == 1
    assert channel._client.updates == []
    assert channel._activity_records[key].stopped


@pytest.mark.asyncio
async def test_a_room_that_never_licensed_a_silence_keeps_its_card():
    channel, request = await _turn_with_a_card(channel_id=REQUIRED)

    await channel.send(_reply_to(request, ""))

    assert channel._client.deleted == []
    assert len(channel._client.updates) == 1
