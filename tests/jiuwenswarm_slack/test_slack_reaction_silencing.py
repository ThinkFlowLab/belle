"""Each of the six state marks must be removable on its own.

The connector marks a turn's progress with six reactions -- seen, refused,
waiting, finished, broken, halted -- and until ``resolve_reaction_emoji`` there
was no way to drop one of them. Blanking a key hands back the default, so the
only switch was ``acknowledge_mode: off``, which takes all six away together
with the thread statuses. An operator who wanted the completion tick gone had
to give up the queue hourglass and the failure cross with it.

These tests pin the word that drops one mark, that blank still means "use the
default" rather than joining it, and that a silenced mark leaves the call sites
around it behaving as they document -- in particular that a silenced ending
still clears the acknowledgement, so a finished message is left bare rather
than wearing "still running" for good.
"""

from belle_agent.platforms.slack import thread_status

from typing import Any

import pytest

from slack_test_events import reaction_coordinates as _coordinates

from belle_agent.platforms.base import MARK_DONE, MARK_FAILED, MARK_QUEUED, MARK_STARTED
from belle_agent.platforms.slack.admission import SlackMessageOrigin
from belle_agent.platforms.slack.config import REACTION_DISABLED, resolve_reaction_emoji

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
CHANNEL = "C0CHANNEL1"
MESSAGE_TS = "1710000001.000100"
THREAD_TS = "1710000001.000100"


class _RecordingSlackClient:
    """Just enough Slack to see which reactions were attempted."""

    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.api_calls: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": "1710000009.000100"}

    async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
        self.api_calls.append({"api_method": api_method, **kwargs})
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.added.append(_coordinates(kwargs))
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed.append(_coordinates(kwargs))
        return {"ok": True}



def _channel(**overrides: Any) -> tuple[SlackChannel, _RecordingSlackClient]:
    # The subject here is the line assistant.threads.setStatus draws, so the
    # surface is pinned. Under the default word, auto, a config whose wording is
    # the shipped wording probes for agents.sessions.setStatus, which draws four
    # fixed lifecycle words and none of the text below.
    overrides.setdefault("session_surface", "assistant")
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=[ASKER], **overrides),
        RobotMessageRouter(),
    )
    channel._running = True
    client = _RecordingSlackClient()
    channel._client = client
    return channel, client


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # Absent and value-less both mean "nothing was decided here". The
        # template ships every key with a value, but a config upgrade can leave
        # one bare, and that must not silence a mark by accident.
        (None, "eyes"),
        ("", "eyes"),
        ("   ", "eyes"),
        # Blank is deliberately still the default and not the off switch. It is
        # what queued_status and steered_status beside these keys already mean
        # by blank, and changing it would move an existing config's behaviour
        # under an operator who had blanked a key expecting the default back.
        ("heavy_check_mark", "heavy_check_mark"),
        ("  white_check_mark  ", "white_check_mark"),
        # The one word that drops the mark.
        (REACTION_DISABLED, ""),
        ("  disabled  ", ""),
        # Cased differently it is an emoji name and not the word: Slack emoji
        # names are lower-case, so nothing here is being helpfully corrected.
        ("Disabled", "Disabled"),
        # The escape for a workspace whose own custom emoji is named disabled.
        # The word is matched before _add_reaction strips the colons, so the
        # colon form still asks for the emoji.
        (":disabled:", ":disabled:"),
    ],
)
def test_resolve_reaction_emoji(configured: Any, expected: str) -> None:
    assert resolve_reaction_emoji(configured, "eyes") == expected


def test_resolve_reaction_emoji_reads_every_key_the_same_way() -> None:
    """One word for all six, not a special case for the tick.

    The complaint that produced this key was about ``completed_emoji``, but a
    mechanism that only answered that one would have to be argued again the
    next time a workspace finds a different mark wrong.
    """
    for default in (
        "eyes",
        "no_entry_sign",
        "hourglass_flowing_sand",
        "heavy_check_mark",
        "x",
        "black_square_for_stop",
    ):
        assert resolve_reaction_emoji(REACTION_DISABLED, default) == ""
        assert resolve_reaction_emoji(None, default) == default


def test_reaction_disabled_is_not_a_yaml_boolean() -> None:
    """The word has to survive the config file unquoted.

    ``channels.slack`` is read as YAML 1.1, which resolves a bare ``off`` to
    the boolean false. A word an operator could only write in quotes would be
    the one spelling of "turn this off" the file cannot hold plainly, which is
    why ``history`` and ``write`` already spell their narrowest word this way.
    """
    yaml = pytest.importorskip("yaml")
    loaded = yaml.safe_load(f"completed_emoji: {REACTION_DISABLED}\n")
    assert loaded == {"completed_emoji": REACTION_DISABLED}


@pytest.mark.asyncio
async def test_silenced_ending_still_clears_the_acknowledgement() -> None:
    """A finished message is left bare, not left claiming to be running.

    The shared state planner removes the acknowledgement and adds the ending. With
    the ending silenced the remove still has to happen: leaving the "seen" mark
    standing would say an answer is still coming, for good, on every turn the
    bot ever completes -- which is worse than the tick the operator asked to be
    rid of.
    """
    channel, client = _channel(completed_emoji=REACTION_DISABLED)

    await channel._react_for_state(CHANNEL, MESSAGE_TS, MARK_DONE)

    assert client.removed == [(CHANNEL, MESSAGE_TS, "eyes")]
    assert client.added == []


@pytest.mark.asyncio
async def test_silencing_one_ending_leaves_the_others_alone() -> None:
    """The tick goes and the cross stays -- that is the whole point."""
    channel, client = _channel(completed_emoji=REACTION_DISABLED)

    await channel._react_for_state(CHANNEL, MESSAGE_TS, MARK_FAILED)

    assert (CHANNEL, MESSAGE_TS, "x") in client.added


@pytest.mark.asyncio
async def test_silenced_acknowledgement_still_sets_the_thread_status() -> None:
    """Dropping the mark drops the mark and nothing else.

    The thread status rides on ``acknowledge_mode``, not on whether a reaction
    was drawn, so an operator who silences the "seen" emoji keeps the line
    Slack draws in the thread while the turn runs.
    """
    channel, client = _channel(acknowledgement_emoji=REACTION_DISABLED)

    await channel._acknowledge_request(
        SlackMessageOrigin(
            channel_id=CHANNEL,
            message_ts=MESSAGE_TS,
            reply_thread_ts=THREAD_TS,
            root_thread_ts=THREAD_TS,
        )
    )

    assert client.added == []
    assert [
        call
        for call in client.api_calls
        if call["api_method"] == thread_status._ASSISTANT_SET_STATUS_METHOD
    ]


@pytest.mark.asyncio
async def test_silenced_queue_mark_is_neither_added_nor_removed() -> None:
    """A disabled queue emoji never reaches Slack, while acknowledgement still does."""
    channel, client = _channel(queued_emoji=REACTION_DISABLED)
    await channel._react_for_state(CHANNEL, MESSAGE_TS, MARK_QUEUED)
    await channel._react_for_state(CHANNEL, MESSAGE_TS, MARK_STARTED)

    assert client.added == [(CHANNEL, MESSAGE_TS, "eyes")]
    assert client.removed == []
