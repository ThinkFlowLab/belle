"""``channels.slack.reply_in_thread`` is three words over four room kinds.

``none`` opens a thread nowhere. ``multi`` opens one in a public channel, a
private channel and a group direct message. ``all`` adds the one-to-one direct
message. ``multi`` is the default, so an upgrade changes nothing.

The word never reaches a message that already sits in a thread. The sender put
it there, so the answer goes there under every word, including ``none``.

The key was a boolean and every config written so far holds one. A ``true``
reads as ``multi`` and a ``false`` as ``none``, with no warning, because a
boolean is the old spelling rather than a mistake.

What motivated the third word: Slack moves a reader into a thread when the bot
sets a status on it. A one-to-one direct message gets a status on its root, and
under ``multi`` the answer is posted outside that thread, so the reader waits in
an empty thread while the reply appears behind them. ``all`` puts the reply
where the status already sent them.
"""

import logging
from pathlib import Path
from typing import Any

import pytest
import jiuwenswarm
import yaml

from belle_agent.platforms.slack import config as slack_config
from belle_agent.platforms.slack.config import REPLY_IN_THREAD_DEFAULT, REPLY_IN_THREAD_WORDS, resolve_reply_in_thread

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
    normalize_reply_in_thread,
)

ASKER = "U0ASKER001"
TEAM = "T0TESTTEAM"

#: One conversation of each kind Slack names, with the id prefix it really uses.
#: ``D`` marks the one-to-one direct message and is the only prefix that states
#: a kind by itself. ``C`` and ``G`` are shared by the other three.
ROOMS = {
    "channel": "C0PUBLIC01",
    "group": "G0PRIVATE1",
    "mpim": "G0GROUPDM1",
    "im": "D0DIRECT01",
}

#: Which kinds each word opens a thread in. The whole table this module pins.
THREADED_KINDS = {
    "none": frozenset(),
    "multi": frozenset({"channel", "group", "mpim"}),
    "all": frozenset({"channel", "group", "mpim", "im"}),
}

TEMPLATE = (
    Path(jiuwenswarm.__file__).resolve().parent
    / "resources"
    / "config.yaml"
)

pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")


class _RecordingSlackClient:
    """Just enough Slack to let a turn finish."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000199.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        return {"ts": kwargs.get("ts", "")}

    async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}


def _channel(**overrides: Any) -> tuple[SlackChannel, list[Message]]:
    overrides.setdefault("allow_from", [ASKER])
    # Every kind of room has to reach the branch, so nothing is filtered out
    # before it: "all" answers a channel message that names nobody.
    overrides.setdefault("group_chat_mode", "all")
    overrides.setdefault("session_surface", "off")
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **overrides), RobotMessageRouter()
    )
    channel._running = True
    channel._client = _RecordingSlackClient()
    dispatched: list[Message] = []
    channel.on_message(dispatched.append)
    return channel, dispatched


def _event(kind: str, ts: str, thread_ts: str = "") -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel_type": kind,
        "channel": ROOMS[kind],
        "user": ASKER,
        "text": "sweep the repos",
        "ts": ts,
    }
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


async def _route(channel: SlackChannel, event: dict[str, Any], event_id: str) -> str:
    """Through the router, so the router decides what counts as a direct message.

    Calling ``_handle_slack_event`` would let the test assert its own answer to
    the question under test. Only ``_route_message_event`` knows which payloads
    take the direct-message path.
    """
    return await channel._route_message_event(
        event, {"event_id": event_id, "team_id": TEAM}
    )


# --------------------------------------------------------------------------
# the table: three words against four kinds
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("word", sorted(THREADED_KINDS))
@pytest.mark.parametrize("kind", sorted(ROOMS))
async def test_each_word_threads_the_kinds_it_names(word: str, kind: str) -> None:
    """A root-level message, one word, one kind of room.

    ``slack_thread_ts`` routes the reply. It holds the message's own ``ts``
    where a thread is opened and is empty where the answer goes to the top of
    the room.
    """
    channel, dispatched = _channel(reply_in_thread=word)
    ts = "1710000100.000100"

    await _route(channel, _event(kind, ts), f"Ev-{word}-{kind}")

    assert dispatched, f"{word}/{kind} was not dispatched"
    expected = ts if kind in THREADED_KINDS[word] else ""
    assert dispatched[0].metadata["slack_thread_ts"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(ROOMS))
async def test_a_group_dm_is_threaded_where_a_one_to_one_is_not(kind: str) -> None:
    """The split the words are named for, stated as one assertion.

    ``multi`` is not "not a direct message". It is "more than two people", and
    a group direct message holds more than two. The router sends only
    ``channel_type: im`` down the direct-message path, which is what makes an
    ``mpim`` reach the branch beside the channels.
    """
    channel, dispatched = _channel(reply_in_thread="multi")
    ts = "1710000100.000200"

    await _route(channel, _event(kind, ts), f"EvM-{kind}")

    assert dispatched
    threaded = bool(dispatched[0].metadata["slack_thread_ts"])
    assert threaded is (kind != "im")


# --------------------------------------------------------------------------
# the arm no word may reach
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(ROOMS))
async def test_a_threaded_message_is_answered_in_its_thread_under_none(
    kind: str,
) -> None:
    """``none`` is about opening a thread, not about avoiding one.

    The sender chose this thread. Answering outside it would strand the reply
    in a room the sender had already left.
    """
    channel, dispatched = _channel(reply_in_thread="none")
    thread = "1710000100.000300"

    await _route(
        channel, _event(kind, "1710000100.000400", thread_ts=thread), f"EvT-{kind}"
    )

    assert dispatched
    assert dispatched[0].metadata["slack_thread_ts"] == thread


# --------------------------------------------------------------------------
# the shape every config written so far holds
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("written", "word"), [(True, "multi"), (False, "none")]
)
@pytest.mark.parametrize("kind", sorted(ROOMS))
async def test_a_boolean_routes_exactly_as_its_word_does(
    written: bool, word: str, kind: str
) -> None:
    """A boolean is the old spelling of two of the three words."""
    channel, dispatched = _channel(reply_in_thread=written)
    ts = "1710000100.000500"

    await _route(channel, _event(kind, ts), f"EvB-{written}-{kind}")

    assert dispatched
    expected = ts if kind in THREADED_KINDS[word] else ""
    assert dispatched[0].metadata["slack_thread_ts"] == expected


@pytest.mark.parametrize(("written", "word"), [(True, "multi"), (False, "none")])
def test_a_boolean_resolves_without_a_warning(
    written: bool, word: str, slack_logs
) -> None:
    """Every existing config holds one, so a boolean is not a mistake."""
    slack_logs.clear()
    with slack_logs.at_level(logging.WARNING, logger=slack_config.logger.name):
        assert resolve_reply_in_thread({"reply_in_thread": written}) == word
    assert [
        record for record in slack_logs.records if "reply_in_thread" in record.getMessage()
    ] == []


# --------------------------------------------------------------------------
# a word nobody recognises
# --------------------------------------------------------------------------


def test_an_unrecognised_word_warns_and_falls_to_none(slack_logs) -> None:
    """The narrow end, as ``app_messages`` does it.

    A typo costs one thread fewer than the operator asked for, which the room
    shows at once. Falling to the wide end would open a thread in a
    conversation the operator never named, and only the reader would see it.
    """
    slack_logs.clear()
    with slack_logs.at_level(logging.WARNING, logger=slack_config.logger.name):
        assert resolve_reply_in_thread({"reply_in_thread": "mutli"}) == "none"

    warnings = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno >= logging.WARNING and "reply_in_thread" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "mutli" in warnings[0]
    for word in REPLY_IN_THREAD_WORDS:
        assert word in warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(ROOMS))
async def test_an_unrecognised_word_opens_no_thread(kind: str) -> None:
    """The fallback has to reach the branch, not only the resolver."""
    channel, dispatched = _channel(reply_in_thread="mutli")
    ts = "1710000100.000600"

    await _route(channel, _event(kind, ts), f"EvU-{kind}")

    assert dispatched
    assert dispatched[0].metadata["slack_thread_ts"] == ""


def test_an_absent_or_empty_value_is_the_default_and_says_nothing(slack_logs) -> None:
    """Not writing the key is not a mistake either."""
    slack_logs.clear()
    with slack_logs.at_level(logging.WARNING, logger=slack_config.logger.name):
        assert resolve_reply_in_thread({}) == REPLY_IN_THREAD_DEFAULT
        assert resolve_reply_in_thread(None) == REPLY_IN_THREAD_DEFAULT
        assert resolve_reply_in_thread({"reply_in_thread": None}) == (
            REPLY_IN_THREAD_DEFAULT
        )
        assert resolve_reply_in_thread({"reply_in_thread": "  "}) == (
            REPLY_IN_THREAD_DEFAULT
        )
    assert [
        record for record in slack_logs.records if "reply_in_thread" in record.getMessage()
    ] == []


def test_the_normaliser_answers_every_shape_the_same_way() -> None:
    """The reader runs where the word is used, so a hand-built config reaches it."""
    for word in REPLY_IN_THREAD_WORDS:
        assert normalize_reply_in_thread(word) == word
        assert normalize_reply_in_thread(f" {word.upper()} ") == word
    assert normalize_reply_in_thread(True) == "multi"
    assert normalize_reply_in_thread(False) == "none"
    assert normalize_reply_in_thread(None) == REPLY_IN_THREAD_DEFAULT
    assert normalize_reply_in_thread("mutli") == "none"


# --------------------------------------------------------------------------
# what ships
# --------------------------------------------------------------------------


def test_upstream_template_boolean_keeps_multi_reply_mode() -> None:
    shipped = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    written = shipped["channels"]["slack"]["reply_in_thread"]
    assert written is True
    assert resolve_reply_in_thread(shipped["channels"]["slack"]) == "multi"
