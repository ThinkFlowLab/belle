"""How a conversation that permits silence expresses it, under one setting.

``delivery.reply: optional`` says a room permits silence.
Belle's ``slack.optional_reply_mode`` says what form the permission takes, for
the whole connector. It takes three words. Each word selects one behaviour
and leaves the other words' behaviour alone.

Under ``sentinel`` the model writes a token and the connector withholds a reply
made of it. Under ``sentinel_unstored`` the reply is withheld the same way, and
a reply that is only the token is also kept out of the stored conversation.
Under ``post_tool`` there is no token: the connector posts no reply of its own,
and the model reaches the conversation by calling the posting tool, so silence
is the absence of a call.

The two behaviours are independent. ``post_tool`` changes delivery and leaves
storage alone. ``sentinel_unstored`` changes storage and leaves delivery alone.
The tests below pin both.

These properties are pinned here.

**The default is ``post_tool``.** An absent key, a bare ``true``
and an unrecognised word all read as ``post_tool``, and an unrecognised word is
reported once, by the side that read the config, rather than on every turn.

**Under ``post_tool`` the final text does not reach the conversation and the
turn is told so.** A turn told otherwise would answer into a void. The
``NO_REPLY`` fragment must also be gone, because a matcher that no longer looks
for the token posts it as text.

**A turn that produced text and posted nothing is warned about.** This mode
inverts the failure mode. Whoever reads the channel reports a bot that says too
much. A bot that answers nothing looks the same as a turn that chose silence,
so the log is the only place the difference is visible.

**What the posting tool can and cannot reach is reported at startup.** Under
``post_tool`` the tool is a room's only voice. A scope that denies it leaves the
room silent. The tool also requires a destination there and refuses a call that
names none, so each turn settles whether its answer sits in a thread and
``reply_in_thread`` stops deciding that.

**A turn can name either place.** ``chat_id`` names a conversation and
``reply_to_message_id`` names a thread inside the originating one. The tests
below pin both. Which argument expresses a destination is said on the tool's
card and nowhere else, so one test reads the instruction for the absence of
both names.

**Under ``sentinel_unstored`` a silence-only reply leaves no stored message.**
This keeps the silence sentinel out of conversation history and prevents it
from being mistaken for user-facing content.

The connector resolves the mode and stamps optional-reply and unstored-silence
metadata. ``SilenceRail`` requires both keys before dropping the final.

The rail may drop one message. **The turn's work stays.** A quiet turn may have
read a document, added a reaction or written to memory. Each of those is a tool
call with a message of its own, and the rail acts only on a response that
carries no tool call.

The loop order the rail depends on is upstream's: ``_railed_model_call`` fires
AFTER_MODEL_CALL, the loop then reads ``consume_force_finish``, and only after
that does it append the assistant message. ``_react_loop`` below replays that
order against the real callback context, so the force-finish signal under test
is upstream's own.

Warnings and info lines are captured by replacing the module logger's methods
rather than through ``caplog``: this project's loggers do not propagate, so
``caplog`` sees nothing under the pytest CI runs on.
"""

import json
import time
from pathlib import Path
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm import config as belle_config
from belle_agent.integrations.jiuwenswarm.config import describe_post_tool_reachability
from belle_agent.platforms.slack import config as slack_config
from belle_agent.platforms.slack.config import OPTIONAL_REPLY_MODE_WORDS, resolve_optional_reply_mode
import yaml

import jiuwenswarm.common.config as config_module
from belle_agent.integrations.jiuwenswarm.slack_post import (
    POST_MESSAGE,
    SlackPostToolkit,
)
from jiuwenswarm.common.schema.message import EventType, Message
from belle_agent.scopes import LEVEL_DENY, REPLY_OPTIONAL, compile_scopes
from belle_agent.platforms.slack.history_policy import (
    METADATA_WRITE_TARGET_REQUIRED_KEY,
)
from belle_agent.silence import (
    REPLY_OPTIONAL_KEY,
    NO_REPLY_SENTINEL,
    SILENCE_UNSTORED_KEY,
)
from belle_agent.platforms.slack.scoped_config import apply_scopes_to_slack_overrides
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    KEY_OPTIONAL_REPLY_MODE,
    NO_REPLY_INSTRUCTION,
    OPTIONAL_REPLY_POST_TOOL,
    OPTIONAL_REPLY_SENTINEL_UNSTORED,
    SlackChannel,
    SlackChannelConfig,
    normalize_optional_reply_mode,
    post_tool_instruction,
)
from belle_agent.platforms.slack.config import OPTIONAL_REPLY_MODE_DEFAULT, OPTIONAL_REPLY_SENTINEL

ASKER = "U0ASKER001"
BOT = "U0BOTUSER0"
TEAM = "T0TESTTEAM"
# Reads every message and may decline to answer.
OPTIONAL = "C0OPTIONAL"
# Reads every message and owes an answer to each, which is the default.
REQUIRED = "C0REQUIRED"
THREAD = "1710000001.000100"

# The near miss that made the token unusable as an exact match: the model wrote
# the token and kept typing. The connector posts this, so the turn is stored.
NEAR_MISS = f"{NO_REPLY_SENTINEL}cancelled"

_SCOPES: tuple[dict[str, Any], ...] = (
    {
        "match": {"channel": "slack", "chat": OPTIONAL},
        "delivery": {"mode": ["all"], "reply": REPLY_OPTIONAL},
    },
    {
        "match": {"channel": "slack", "chat": REQUIRED},
        "delivery": {"mode": ["all"]},
    },
)

# The same rooms, and a rule taking the posting tool away from the one that
# permits silence. Written on ``chat`` because a permissions rule is given the
# channel and the chat and nothing else.
_SCOPES_WITHOUT_THE_TOOL: tuple[dict[str, Any], ...] = (
    *_SCOPES,
    {
        "match": {"channel": "slack", "chat": OPTIONAL},
        "permissions": {"tools": {POST_MESSAGE: LEVEL_DENY}},
    },
)


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "warning", record)
    monkeypatch.setattr(slack_config.logger, "warning", record)
    monkeypatch.setattr(belle_config.logger, "warning", record)
    return recorded


@pytest.fixture
def notes(monkeypatch) -> list[str]:
    """Every INFO line, for the one that says the turn called the tool."""
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "info", record)
    return recorded


class _RecordingSlackClient:
    """Just enough Slack to see what the connector posted and deleted."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []

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

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        raise RuntimeError(f"{method} is not available in this workspace")


class _ToolWorkspace:
    """What the posting tool talks to, recording the one call it makes."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "url": "https://acme.slack.com/", "team_id": TEAM}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1710000200.000100", "channel": kwargs["channel"]}


def _config(entries: Any = _SCOPES, **overrides: Any) -> SlackChannelConfig:
    """A config built the way ``app_gateway`` builds one, from written scopes."""
    group_chat_mode = overrides.pop("group_chat_mode", "mention")
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


def _room_event(*, channel_id: str = OPTIONAL, ts: str, text: str) -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": channel_id,
        "user": ASKER,
        "text": text,
        "ts": ts,
        "thread_ts": THREAD,
    }


async def _post(channel: SlackChannel, event: dict, event_id: str) -> str:
    return await channel._handle_slack_event(
        event, {"event_id": event_id, "team_id": TEAM}, is_dm=False, trigger="all"
    )


def _reply_to(request: Message, content: str) -> Message:
    """The answer to ``request``, carrying the metadata the request carried."""
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


def _posting_tool_call(request: Message) -> Message:
    """One ``post_message`` call on the turn ``request`` started."""
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
                "name": POST_MESSAGE,
                "arguments": {"text": "the report is ready"},
                "tool_call_id": "call-post-1",
            },
        },
        event_type=EventType.CHAT_TOOL_CALL,
        metadata=dict(request.metadata or {}),
    )


def _posting_tool_result(request: Message, refusal: "str | None" = None) -> Message:
    """The result of that call, as the rail emits one.

    A refusal states ``ok: false`` with its code beside it, and
    ``_infer_tool_result_error`` reads that convention, so the rail flags the
    result and sets the three fields below. The flags are written here rather
    than inferred, because they are what the connector classifies on.
    """
    payload: dict[str, Any] = (
        {"ok": False, "error": refusal}
        if refusal
        else {"ok": True, "chat_id": OPTIONAL, "message_id": "1710000200.000100"}
    )
    result: dict[str, Any] = {
        "tool_name": POST_MESSAGE,
        "tool_call_id": "call-post-1",
        "result": json.dumps(payload),
    }
    if refusal:
        result.update(success=False, status="error", is_error=True)
    else:
        result["success"] = True
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": EventType.CHAT_TOOL_RESULT.value,
            "tool_result": result,
        },
        event_type=EventType.CHAT_TOOL_RESULT,
        metadata=dict(request.metadata or {}),
    )


async def _turn(
    channel: SlackChannel,
    received: list[Message],
    content: str,
    *,
    channel_id: str = OPTIONAL,
    ts: str = "1710000010.000100",
) -> list[dict[str, Any]]:
    """Run one message through, answer it, and return what the connector posted."""
    await _post(
        channel,
        _room_event(channel_id=channel_id, ts=ts, text="shipping it now"),
        f"Ev{ts}",
    )
    channel._client.posts.clear()
    await channel.send(_reply_to(received[-1], content))
    return channel._client.posts


# --------------------------------------------------------------------------
# The word, its default, and what an unrecognised one does

# --------------------------------------------------------------------------


def test_the_default_is_post_tool_and_is_what_an_absent_key_reads_as():
    assert OPTIONAL_REPLY_MODE_DEFAULT == OPTIONAL_REPLY_POST_TOOL
    assert resolve_optional_reply_mode({}) == OPTIONAL_REPLY_POST_TOOL
    assert resolve_optional_reply_mode(None) == OPTIONAL_REPLY_POST_TOOL
    assert SlackChannelConfig().optional_reply_mode == OPTIONAL_REPLY_POST_TOOL


@pytest.mark.parametrize(
    "word",
    [OPTIONAL_REPLY_SENTINEL, OPTIONAL_REPLY_SENTINEL_UNSTORED, OPTIONAL_REPLY_POST_TOOL],
)
def test_every_declared_word_is_accepted_as_written(word, warnings):
    assert resolve_optional_reply_mode({KEY_OPTIONAL_REPLY_MODE: word}) == word
    assert warnings == []


def test_an_unrecognised_word_falls_back_to_post_tool_and_is_reported_once(
    warnings,
):
    """The line names the key, the word that was written, and what was used.

    ``unstored`` is the probe because it is a plausible shortening of a word the
    key does take. The line must name the word written and the word used, or
    the two look alike.
    """
    settled = resolve_optional_reply_mode({KEY_OPTIONAL_REPLY_MODE: "unstored"})

    assert settled == OPTIONAL_REPLY_POST_TOOL
    assert len(warnings) == 1
    assert KEY_OPTIONAL_REPLY_MODE in warnings[0]
    assert "unstored" in warnings[0]
    assert OPTIONAL_REPLY_POST_TOOL in warnings[0]


def test_the_per_turn_reader_says_nothing_about_a_word_startup_reported(warnings):
    """A turn reads the word on every message and must not repeat the warning."""
    channel, _ = _channel(optional_reply_mode="shout")

    assert normalize_optional_reply_mode("shout") == OPTIONAL_REPLY_POST_TOOL
    assert channel._optional_reply_mode() == OPTIONAL_REPLY_POST_TOOL
    assert warnings == []


# --------------------------------------------------------------------------
# Sentinel remains selectable

# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentinel_withholds_the_token_and_posts_everything_else():
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_SENTINEL)

    assert await _turn(channel, received, NO_REPLY_SENTINEL) == []
    assert [
        post["text"]
        for post in await _turn(
            channel, received, "done", ts="1710000011.000100"
        )
    ] == ["done"]


@pytest.mark.asyncio
async def test_sentinel_offers_the_token_and_names_no_tool():
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_SENTINEL)
    await _post(
        channel,
        _room_event(ts="1710000012.000100", text="shipping it now"),
        "Ev12",
    )

    prompt = str(received[-1].params["content"])
    assert NO_REPLY_INSTRUCTION in prompt
    assert POST_MESSAGE not in prompt


# --------------------------------------------------------------------------
# Under post_tool, the connector posts nothing and says so

# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_post_tool_tells_the_turn_that_its_final_text_goes_nowhere():
    channel, received = _channel()
    await _post(
        channel,
        _room_event(ts="1710000013.000100", text="shipping it now"),
        "Ev13",
    )

    prompt = str(received[-1].params["content"])
    assert post_tool_instruction() in prompt
    assert NO_REPLY_SENTINEL not in prompt
    assert POST_MESSAGE in prompt


@pytest.mark.asyncio
async def test_post_tool_says_nothing_extra_where_a_reply_is_owed():
    """A room that owes an answer keeps it, so neither fragment belongs there."""
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(channel_id=REQUIRED, ts="1710000014.000100", text="status?"),
        "Ev14",
    )

    prompt = str(received[-1].params["content"])
    assert post_tool_instruction() not in prompt
    assert NO_REPLY_SENTINEL not in prompt


@pytest.mark.asyncio
async def test_post_tool_posts_no_final_text_of_its_own(warnings):
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)

    assert await _turn(channel, received, "the report is ready") == []


@pytest.mark.asyncio
async def test_post_tool_leaves_a_room_that_owes_an_answer_alone():
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)

    posted = await _turn(channel, received, "done", channel_id=REQUIRED)

    assert [post["text"] for post in posted] == ["done"]


# --------------------------------------------------------------------------
# The counter, which is the whole answer to "has it stopped working"

# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_that_produced_text_and_posted_nothing_is_warned_about(
    warnings,
):
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)

    await _turn(channel, received, "the report is ready")

    assert len(warnings) == 1
    line = warnings[0]
    assert POST_MESSAGE in line
    assert OPTIONAL in line
    assert received[-1].session_id in line
    # The text belongs to the conversation; its length stands in for it.
    assert "the report is ready" not in line
    assert "chars=19" in line


@pytest.mark.asyncio
async def test_a_turn_that_called_the_posting_tool_is_noted_and_not_warned_about(
    warnings, notes
):
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000015.000100", text="shipping it now"),
        "Ev15",
    )
    request = received[-1]
    await channel.send(_posting_tool_call(request))
    await channel.send(_reply_to(request, "posted it above"))

    assert channel._client.posts == []
    assert warnings == []
    assert any(f"the turn called {POST_MESSAGE}" in note for note in notes)


@pytest.mark.asyncio
async def test_a_refused_posting_call_warns_and_names_the_refusal(warnings, notes):
    """The third outcome, and the likeliest one now that a destination is required.

    Neither a silence the turn chose nor a message anybody read. A call that
    omits its destination is refused this way, so the code is what says which
    correction the turn needs.
    """
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000019.000100", text="shipping it now"),
        "Ev19",
    )
    request = received[-1]
    await channel.send(_posting_tool_call(request))
    await channel.send(_posting_tool_result(request, "chat_id_required"))
    await channel.send(_reply_to(request, "posted it above"))

    assert channel._client.posts == []
    assert len(warnings) == 1
    assert "was refused (chat_id_required)" in warnings[0]
    assert OPTIONAL in warnings[0]
    assert request.session_id in warnings[0]
    assert not [note for note in notes if "the turn called" in note]


@pytest.mark.asyncio
async def test_a_refusal_with_no_readable_code_still_reports_the_outcome(warnings):
    """The classification decides, so an unreadable payload loses the code only."""
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000022.000100", text="shipping it now"),
        "Ev22",
    )
    request = received[-1]
    refused = _posting_tool_result(request, "chat_id_required")
    refused.payload["tool_result"]["result"] = "not json at all"
    await channel.send(_posting_tool_call(request))
    await channel.send(refused)
    await channel.send(_reply_to(request, "posted it above"))

    assert len(warnings) == 1
    assert "was refused (refused)" in warnings[0]


@pytest.mark.asyncio
async def test_the_rails_classification_is_what_decides_a_refusal(warnings, notes):
    """The boundary, stated as a boundary.

    The connector does not read the payload to decide whether a call failed. It
    reads the rail's classification, and the payload only for the code behind
    one. So an unflagged result counts as delivered whatever its payload says.

    That is deliberate. A connector deciding the verdict for itself would hold a
    second answer to a question the activity card and every other reader already
    answer from the rail, and the two would drift. The cost is that a refusal
    reaches this line only where the rail classifies the ``ok`` convention as a
    failure.
    """
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000024.000100", text="shipping it now"),
        "Ev24",
    )
    request = received[-1]
    unflagged = _posting_tool_result(request, "chat_id_required")
    for flag in ("success", "status", "is_error"):
        unflagged.payload["tool_result"].pop(flag, None)
    await channel.send(_posting_tool_call(request))
    await channel.send(unflagged)
    await channel.send(_reply_to(request, "posted it above"))

    assert warnings == []
    assert any(f"the turn called {POST_MESSAGE}" in note for note in notes)


@pytest.mark.asyncio
async def test_a_delivered_posting_call_is_noted_once_its_result_arrives(
    warnings, notes
):
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000020.000100", text="shipping it now"),
        "Ev20",
    )
    request = received[-1]
    await channel.send(_posting_tool_call(request))
    await channel.send(_posting_tool_result(request))
    await channel.send(_reply_to(request, "posted it above"))

    assert warnings == []
    assert any(f"the turn called {POST_MESSAGE}" in note for note in notes)


@pytest.mark.asyncio
async def test_a_refused_turn_loses_its_activity_card_the_way_a_silent_one_does():
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000021.000100", text="shipping it now"),
        "Ev21",
    )
    request = received[-1]
    await channel.send(_posting_tool_call(request))
    await channel.send(_posting_tool_result(request, "not_in_channel"))

    assert channel._turn_says_nothing(_reply_to(request, "tried"), None) is True


@pytest.mark.asyncio
async def test_a_turn_that_delivered_and_was_refused_once_reports_the_delivery():
    """Something reached a reader, so the turn did not say nothing."""
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000023.000100", text="shipping it now"),
        "Ev23",
    )
    request = received[-1]
    await channel.send(_posting_tool_call(request))
    await channel.send(_posting_tool_result(request, "channel_not_found"))
    await channel.send(_posting_tool_result(request))

    assert channel._turn_says_nothing(_reply_to(request, "done"), None) is False


@pytest.mark.asyncio
async def test_the_activity_card_comes_down_only_for_a_turn_that_posted_nothing():
    """What the card reads is no longer the text, which is always withheld."""
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    await _post(
        channel,
        _room_event(ts="1710000016.000100", text="shipping it now"),
        "Ev16",
    )
    request = received[-1]
    quiet = _reply_to(request, "nothing needed here")

    assert channel._turn_says_nothing(quiet, None) is True

    await channel.send(_posting_tool_call(request))

    assert channel._turn_says_nothing(quiet, None) is False


# --------------------------------------------------------------------------
# What the model's own call reaches, with the metadata the connector stamped

# --------------------------------------------------------------------------


@pytest.fixture
def _bot_token(monkeypatch) -> None:
    """The token the posting toolkit reads when it is handed no client."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-test-token"}}},
    )


async def _stamped_metadata(channel: SlackChannel, received, ts: str) -> dict:
    """One message through the connector, and the metadata it stamped."""
    await _post(channel, _room_event(ts=ts, text="shipping it now"), f"Ev{ts}")
    return dict(received[-1].metadata or {})


@pytest.mark.asyncio
async def test_post_tool_asks_the_turn_to_name_where_its_answer_goes(_bot_token):
    """A call that names nowhere is refused rather than sent to the room's root.

    The connector settled a thread for this answer and stamped it, and the tool
    reads no thread off request metadata. Posting with no destination would
    therefore move an answer to a message that arrived inside a thread out to
    the top level of the room, which no setting ever made optional.
    """
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    metadata = await _stamped_metadata(channel, received, "1710000018.000100")

    assert metadata[METADATA_WRITE_TARGET_REQUIRED_KEY] is True
    assert metadata["slack_thread_ts"] == THREAD

    workspace = _ToolWorkspace()
    toolkit = SlackPostToolkit(metadata=metadata, client=workspace)
    result = json.loads(await toolkit.post_message(text="the report is ready"))

    assert result["ok"] is False
    assert result["error"] == "write_target_required"
    # The conversation to name, so that naming it costs no further call.
    assert result["chat_id"] == OPTIONAL
    assert workspace.posts == []


@pytest.mark.asyncio
async def test_a_tool_posted_answer_reaches_the_thread_the_turn_names(_bot_token):
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    metadata = await _stamped_metadata(channel, received, "1710000019.000100")

    workspace = _ToolWorkspace()
    toolkit = SlackPostToolkit(metadata=metadata, client=workspace)
    result = json.loads(
        await toolkit.post_message(
            text="the report is ready", reply_to_message_id=THREAD
        )
    )

    assert result["ok"] is True
    assert len(workspace.posts) == 1
    assert workspace.posts[0]["channel"] == OPTIONAL
    assert workspace.posts[0]["thread_ts"] == THREAD


@pytest.mark.asyncio
async def test_a_tool_posted_answer_reaches_the_conversation_the_turn_names(
    _bot_token,
):
    """An ordinary room-level post stays possible, by naming the room."""
    channel, received = _channel(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)
    metadata = await _stamped_metadata(channel, received, "1710000020.000100")

    workspace = _ToolWorkspace()
    toolkit = SlackPostToolkit(metadata=metadata, client=workspace)
    result = json.loads(
        await toolkit.post_message(text="the report is ready", chat_id=OPTIONAL)
    )

    assert result["ok"] is True
    assert [post["channel"] for post in workspace.posts] == [OPTIONAL]
    assert "thread_ts" not in workspace.posts[0]


def test_the_instruction_reads_as_settled():
    """Pinned verbatim, so a reword fails here and not in a conversation."""
    assert post_tool_instruction() == (
        "Your final response is not sent to anyone. The only way to reach a"
        f" reader is the {POST_MESSAGE} tool. Decide how many messages to send"
        " and where each one goes."
    )


def test_the_instruction_states_no_argument_rules():
    """Which argument names a destination is the card's to say, and only the card's.

    The instruction says that the turn decides where each message goes. The
    card says which argument expresses that, and the refusal says what a call
    left out. Two statements of one argument's rules, in two files, drift.
    """
    instruction = post_tool_instruction()

    for argument in ("chat_id", "reply_to_message_id"):
        assert argument not in instruction


@pytest.mark.parametrize(
    "word", [OPTIONAL_REPLY_SENTINEL, OPTIONAL_REPLY_SENTINEL_UNSTORED]
)
@pytest.mark.asyncio
async def test_the_sentinel_words_leave_the_default_destination_alone(
    word, _bot_token
):
    """A deployment on either word sees no change at all."""
    channel, received = _channel(optional_reply_mode=word)
    metadata = await _stamped_metadata(
        channel, received, f"17100000{22 + len(word) % 10}.000100"
    )

    assert METADATA_WRITE_TARGET_REQUIRED_KEY not in metadata

    workspace = _ToolWorkspace()
    toolkit = SlackPostToolkit(metadata=metadata, client=workspace)
    result = json.loads(await toolkit.post_message(text="the report is ready"))

    assert result["ok"] is True
    assert result["chat_id"] == OPTIONAL
    assert [post["channel"] for post in workspace.posts] == [OPTIONAL]


# --------------------------------------------------------------------------
# What post_tool cannot do, reported before any turn runs

# --------------------------------------------------------------------------


def _reported_about(reported: tuple[str, ...], phrase: str) -> list[str]:
    return [line for line in reported if phrase in line]


def test_where_a_tool_posted_answer_lands_is_reported(warnings):
    """Each turn settles it, which is what ``reply_in_thread`` stops doing."""
    config = _config(optional_reply_mode=OPTIONAL_REPLY_POST_TOOL)

    reported = describe_post_tool_reachability(config)

    assert len(reported) == 1
    assert "reply_in_thread" in reported[0]
    assert "top level" in reported[0]
    assert "refuses a call that names nowhere" in reported[0]
    assert reported[0] in warnings


def test_a_conversation_whose_scope_denies_the_posting_tool_is_named(warnings):
    config = _config(
        _SCOPES_WITHOUT_THE_TOOL, optional_reply_mode=OPTIONAL_REPLY_POST_TOOL
    )

    reported = describe_post_tool_reachability(config)
    named = _reported_about(reported, LEVEL_DENY)

    assert len(named) == 1
    assert OPTIONAL in named[0]
    assert POST_MESSAGE in named[0]
    assert named[0] in warnings


def test_a_denial_in_a_room_that_owes_an_answer_is_not_reported(warnings):
    """That room keeps the connector's reply, so the tool is not its only voice."""
    config = _config(
        (
            *_SCOPES,
            {
                "match": {"channel": "slack", "chat": REQUIRED},
                "permissions": {"tools": {POST_MESSAGE: LEVEL_DENY}},
            },
        ),
        optional_reply_mode=OPTIONAL_REPLY_POST_TOOL,
    )

    assert _reported_about(describe_post_tool_reachability(config), LEVEL_DENY) == []


def test_streaming_and_post_tool_are_reported_as_not_combining(warnings):
    config = _config(
        optional_reply_mode=OPTIONAL_REPLY_POST_TOOL, enable_streaming=True
    )

    reported = _reported_about(describe_post_tool_reachability(config), "enable_streaming")

    assert len(reported) == 1
    assert reported[0] in warnings


@pytest.mark.parametrize(
    "word", [OPTIONAL_REPLY_SENTINEL, OPTIONAL_REPLY_SENTINEL_UNSTORED]
)
def test_the_sentinel_words_are_checked_for_nothing_at_startup(word, warnings):
    config = _config(
        _SCOPES_WITHOUT_THE_TOOL,
        optional_reply_mode=word,
        enable_streaming=True,
    )

    assert describe_post_tool_reachability(config) == ()
    assert warnings == []


# --------------------------------------------------------------------------
# The vocabulary itself, and what the template writes beside the key

# --------------------------------------------------------------------------


def test_all_three_words_are_declared_and_read_as_themselves(warnings):
    """``post_tool`` is declared here and its behaviour is built elsewhere.

    A word the resolver refuses is a word an operator cannot write, so the
    vocabulary carries all three even while one of them changes nothing yet.
    """
    assert OPTIONAL_REPLY_MODE_WORDS == (
        OPTIONAL_REPLY_SENTINEL,
        OPTIONAL_REPLY_SENTINEL_UNSTORED,
        OPTIONAL_REPLY_POST_TOOL,
    )
    for word in OPTIONAL_REPLY_MODE_WORDS:
        assert resolve_optional_reply_mode({"optional_reply_mode": word}) == word
        shouted = {"optional_reply_mode": word.upper()}
        assert resolve_optional_reply_mode(shouted) == word
    assert warnings == []


def test_a_bare_true_names_no_word_and_is_not_guessed_at(warnings):
    """YAML 1.1 turns ``on`` and ``yes`` into ``True``, which names no word."""
    assert (
        resolve_optional_reply_mode({"optional_reply_mode": True})
        == OPTIONAL_REPLY_POST_TOOL
    )
    assert warnings == []


def test_the_shipped_template_writes_the_word_the_code_defaults_to():
    """Belle's example and the connector use the same silence default."""
    template = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml").read_text(encoding="utf-8")
    )
    shipped = template["platforms"]["slack"]

    assert "optional_reply_mode" not in shipped
    assert SlackChannelConfig().optional_reply_mode == OPTIONAL_REPLY_POST_TOOL


# --------------------------------------------------------------------------
# What the connector stamps, under each word

# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, stamped",
    [
        (OPTIONAL_REPLY_SENTINEL, False),
        (OPTIONAL_REPLY_SENTINEL_UNSTORED, True),
        (OPTIONAL_REPLY_POST_TOOL, False),
    ],
)
async def test_only_the_unstored_word_asks_the_runtime_for_anything(mode, stamped):
    """The key travels on a licensed turn, and only under its own word.

    ``post_tool`` stamps nothing here. Whatever that word goes on to do is its
    own change. This one asks the runtime for nothing under it.
    """
    channel, received = _channel(optional_reply_mode=mode)

    await _post(
        channel,
        _room_event(
            channel_id=OPTIONAL, ts="1710000010.000100", text="shipping it now"
        ),
        "Ev1710000010.000100",
    )

    assert received[0].metadata[REPLY_OPTIONAL_KEY] is True
    assert (SILENCE_UNSTORED_KEY in received[0].metadata) is stamped


@pytest.mark.asyncio
async def test_a_conversation_that_owes_an_answer_is_stamped_neither_key():
    channel, received = _channel(
        optional_reply_mode=OPTIONAL_REPLY_SENTINEL_UNSTORED
    )

    await _post(
        channel,
        _room_event(
            channel_id=REQUIRED, ts="1710000010.000100", text="shipping it now"
        ),
        "Ev1710000010.000100",
    )

    assert REPLY_OPTIONAL_KEY not in received[0].metadata
    assert SILENCE_UNSTORED_KEY not in received[0].metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [OPTIONAL_REPLY_SENTINEL, OPTIONAL_REPLY_SENTINEL_UNSTORED]
)
async def test_the_token_is_withheld_under_both_sentinel_words(mode):
    """Delivery does not change. The new word decides storage only."""
    channel, received = _channel(optional_reply_mode=mode)

    await _post(
        channel,
        _room_event(
            channel_id=OPTIONAL, ts="1710000010.000100", text="shipping it now"
        ),
        "Ev1710000010.000100",
    )
    channel._client.posts.clear()
    await channel.send(_reply_to(received[-1], NO_REPLY_SENTINEL))

    assert channel._client.posts == []


@pytest.mark.asyncio
async def test_a_near_miss_is_still_posted_under_the_unstored_word():
    channel, received = _channel(
        optional_reply_mode=OPTIONAL_REPLY_SENTINEL_UNSTORED
    )

    await _post(
        channel,
        _room_event(
            channel_id=OPTIONAL, ts="1710000010.000100", text="shipping it now"
        ),
        "Ev1710000010.000100",
    )
    channel._client.posts.clear()
    await channel.send(_reply_to(received[-1], NEAR_MISS))

    assert [post["text"] for post in channel._client.posts] == [NEAR_MISS]
