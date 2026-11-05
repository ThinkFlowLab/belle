import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from belle_agent.integrations.jiuwenswarm.slack_routing import slack_history_metadata_for_cron_job
from belle_agent.integrations.jiuwenswarm.slack_tool_context import slack_place_fields
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn


@pytest.fixture(autouse=True)
def _belle_turn_fields(monkeypatch):
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    registry.register_turn_envelope_fields(slack_place_fields)
    monkeypatch.setattr(ExtensionRegistry, "current_instance", lambda: registry)


def _envelope(rendered: str) -> dict:
    return json.loads(rendered[rendered.index("{"):])


def _turn(**overrides) -> UserTurn:
    values = {"text": "hello", "channel": "slack", "language": "en", "files": {}}
    values.update(overrides)
    return UserTurn(**values)


# ── the Slack conversation and thread a turn runs in ──────────────────────────

# Invented ids in the shapes Slack uses. A conversation id starts with ``C`` for
# a channel and ``D`` for a direct message; a ts is seconds and microseconds.
_ROOM = "C0FAKEROOM"
_DIRECT = "D0FAKEDIRECT"
_MESSAGE_TS = "1700000000.000100"
_THREAD_TS = "1699999000.000200"
_WORKSPACE = "T0FAKEWORKSPACE"
_ASKER = "U0FAKEASKER"


def _slack_metadata(**overrides) -> dict:
    """Request metadata in the shape the Slack connector stamps on a turn.

    Values invented, key names the connector's own. The audience keys are here
    because every inbound Slack turn carries them, and the envelope must not
    repeat them.
    """
    base = {
        "user_id": _ASKER,
        "slack_event_id": "Ev0FAKEEVENT",
        "slack_team_id": _WORKSPACE,
        "slack_channel_id": _ROOM,
        "slack_channel_type": "channel",
        "slack_user_id": _ASKER,
        "slack_message_ts": _MESSAGE_TS,
        "message_ts": _MESSAGE_TS,
        "slack_thread_ts": "",
        "slack_trigger": "mention",
        "slack_enforce_audience": True,
    }
    base.update(overrides)
    return base


def _place(channel: str = "slack", **overrides) -> dict | None:
    """The ``slack`` block of a rendered envelope, or ``None`` when absent."""
    metadata = overrides.pop("metadata", None)
    if metadata is None:
        metadata = _slack_metadata(**overrides)
    rendered = _turn(channel=channel, metadata=metadata).render()
    return _envelope(rendered).get("slack")


def test_a_channel_message_states_its_conversation_and_its_thread():
    # A message posted inside a thread: the connector settles the thread root as
    # the place this turn answers in, and both ids reach the model.
    assert _place(slack_thread_ts=_THREAD_TS) == {
        "chat_id": _ROOM,
        "thread_ts": _THREAD_TS,
    }


def test_a_top_level_message_states_its_conversation_and_no_thread():
    # ``reply_in_thread`` off at channel root: the connector settles no thread.
    place = _place(slack_thread_ts="")

    assert place == {"chat_id": _ROOM}
    # Absence rather than an empty string, which would read as a thread whose id
    # went missing on the way here.
    assert "thread_ts" not in place


def test_a_top_level_message_that_opens_a_thread_states_that_thread():
    # ``reply_in_thread`` on at channel root: the connector settles the message's
    # own ts as the thread, and that is the value stated.
    assert _place(slack_thread_ts=_MESSAGE_TS) == {
        "chat_id": _ROOM,
        "thread_ts": _MESSAGE_TS,
    }


def test_a_direct_message_states_its_conversation():
    assert _place(
        slack_channel_id=_DIRECT,
        slack_channel_type="im",
        slack_thread_ts="",
    ) == {"chat_id": _DIRECT}


def test_an_event_woken_turn_states_its_conversation_and_its_thread():
    # An event anchored on a message answers in that message's thread.
    anchored = _place(
        slack_thread_ts=_THREAD_TS,
        slack_trigger="event",
        belle_reply_optional=True,
    )
    assert anchored == {"chat_id": _ROOM, "thread_ts": _THREAD_TS}

    # A membership event has no message to anchor on, so the turn answers at the
    # channel's top level and no thread is settled for it.
    unanchored = _place(
        slack_thread_ts="",
        slack_trigger="event",
        slack_post_as_root=True,
        belle_reply_optional=True,
    )
    assert unanchored == {"chat_id": _ROOM}


def test_a_cron_run_states_the_conversation_its_job_belongs_to():
    # Built by the scheduler's own helper, so the test reads the ids the runtime
    # settles rather than ids a test wrote.
    threaded = SimpleNamespace(
        id="job-1",
        origin_channel_id="slack",
        session_id=f"slack_{_WORKSPACE}_{_ROOM}_{_THREAD_TS}",
    )
    assert _place("__cron__", metadata=slack_history_metadata_for_cron_job(threaded)) == {
        "chat_id": _ROOM,
        "thread_ts": _THREAD_TS,
    }

    # A job created for a whole channel has no thread. A scheduled run also has
    # no asker, and nothing in the block stands in for one.
    channel_wide = SimpleNamespace(
        id="job-2",
        origin_channel_id="slack",
        session_id=f"slack_{_WORKSPACE}_{_ROOM}",
    )
    place = _place("__cron__", metadata=slack_history_metadata_for_cron_job(channel_wide))
    assert place == {"chat_id": _ROOM}
    assert "thread_ts" not in place


def test_the_two_ids_are_the_settled_values_character_for_character():
    # Trailing zeroes in a ts and a mixed-case conversation id are the two ways a
    # reformatted value would show. Both are carried through unchanged, and both
    # appear in the rendered prompt as themselves.
    room = "C0FaKeRoOm10"
    thread_ts = "1700000000.000010"
    rendered = _turn(
        channel="slack",
        metadata=_slack_metadata(slack_channel_id=room, slack_thread_ts=thread_ts),
    ).render()

    assert _envelope(rendered)["slack"] == {"chat_id": room, "thread_ts": thread_ts}
    assert f'"chat_id": "{room}"' in rendered
    assert f'"thread_ts": "{thread_ts}"' in rendered


def test_the_envelope_states_the_place_and_nothing_else_about_the_request():
    rendered = _turn(channel="slack", metadata=_slack_metadata()).render()

    assert set(_envelope(rendered)["slack"]) == {"chat_id"}
    # The workspace and asker are request metadata for tools, not the model.
    for withheld in (_WORKSPACE, _ASKER):
        assert withheld not in rendered
    assert "slack_enforce_audience" not in rendered


def test_a_turn_no_slack_path_settled_states_no_conversation():
    # The channel id is written by the transport. A conversation id arriving on
    # any other channel was not settled by Slack, so the envelope states none.
    assert _place("web") is None
    # A scheduled run states nothing either until the scheduler marks the stamp
    # as its own: an unmarked one could be any request carrying a stray id.
    assert _place("__cron__", metadata={"slack_channel_id": _ROOM}) is None
    # And a Slack turn that names no conversation has no place to state.
    assert _place(slack_channel_id="") is None
