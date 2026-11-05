"""Reusable Slack admission decisions without a harness runtime."""

from types import SimpleNamespace

import pytest

from belle_agent.platforms.slack.admission import (
    APP_MESSAGES_ALL,
    APP_MESSAGES_LISTED,
    APP_MESSAGES_NONE,
    AdmissionState,
    PendingEvent,
    SlackMessageOrigin,
    SlackTurnInitiator,
    app_messages_note,
    click_identity,
    event_gate,
    event_identity,
    is_reply_to_bot,
    message_route,
    non_user_content_outcome,
)


def test_message_admission_and_addressing():
    assert is_reply_to_bot({"ts": "2", "thread_ts": "1", "parent_user_id": "U-BOT"}, "U-BOT")
    assert not is_reply_to_bot({"ts": "1", "thread_ts": "1", "parent_user_id": "U-BOT"}, "U-BOT")
    assert non_user_content_outcome(
        {"bot_id": "B-OTHER", "app_id": "A-OTHER", "user": "U-SENDER"},
        mode=APP_MESSAGES_LISTED, app_ids=frozenset({"A-ALLOWED"}), bot_id="B-BOT", bot_user_id="U-BOT",
    ) == "ignored:app-not-in-app_messages_from (A-OTHER)"
    assert non_user_content_outcome(
        {"bot_id": "B-BOT", "user": "U-SENDER"},
        mode=APP_MESSAGES_LISTED, app_ids=frozenset({"A-ALLOWED"}), bot_id="B-BOT", bot_user_id="U-BOT",
    ) == "ignored:posted-by-this-bot"
    assert app_messages_note(
        {"bot_id": "B-OTHER", "app_id": "A-ALLOWED"},
        mode=APP_MESSAGES_LISTED, app_ids=frozenset({"A-ALLOWED"}),
    ) == " app_messages=listed app_id=A-ALLOWED"


@pytest.mark.parametrize("slack_user", ["USLACK", "USLACKBOT"])
def test_slack_posts_as_an_app_and_its_direct_message_starts_nothing(slack_user):
    def outcome(channel_type: str, mode: str) -> str | None:
        # As Slack sent it when a person removed the bot from a channel.
        event = {"user": slack_user, "channel_type": channel_type, "text": "You have been removed from #general by <@U1>"}
        return non_user_content_outcome(event, mode=mode, app_ids=frozenset(), bot_id="B-BOT", bot_user_id="U-BOT")

    for mode in (APP_MESSAGES_NONE, APP_MESSAGES_LISTED, APP_MESSAGES_ALL):
        assert outcome("im", mode) == "ignored:direct-message-from-slack"
    for mode in (APP_MESSAGES_NONE, APP_MESSAGES_LISTED):
        assert outcome("channel", mode) == "ignored:posted-by-slack"
    assert outcome("channel", APP_MESSAGES_ALL) is None
    # Control: a person's direct message is user content.
    assert non_user_content_outcome(
        {"user": "U-PERSON", "channel_type": "im"}, mode=APP_MESSAGES_NONE, app_ids=frozenset(), bot_id="B-BOT",
        bot_user_id="U-BOT",
    ) is None


def test_trigger_priority_and_nonmessage_identity_gate():
    event = {"ts": "2", "thread_ts": "1", "parent_user_id": "U-BOT", "files": [{"id": "F1"}]}
    triggers = {"reply", "url", "has_file"}
    assert message_route(triggers, event, "https://example.com", bot_user_id="U-BOT", leading_mention=False,
                         mentions_bot_id=False, ingesting=True) == ("reply", "")
    assert message_route(triggers, event, "", bot_user_id="U-BOT", leading_mention=True,
                         mentions_bot_id=False, ingesting=False) == ("", "ignored:leading-bot-mention-handled-as-app_mention")
    assert event_gate({"type": "reaction_added", "user": "U1", "item": {"channel": "C1"}}, "U-BOT") == (
        "U1", "C1", ""
    )
    assert event_gate({"type": "reaction_added", "user": "U-BOT", "item": {"channel": "C1"}}, "U-BOT")[-1] == (
        "ignored:done-by-this-bot"
    )


def test_event_identifiers_omit_content():
    identity = event_identity({"type": "message", "channel": "C1", "text": "private text"}, {"event_id": "Ev1"})
    assert "channel=C1" in identity and "event_id=Ev1" in identity
    assert "private text" not in identity
    reaction = {"type": "reaction_added", "item": {"channel": "C1", "ts": "1710000000.000100"}}
    pin = {"type": "pin_added", "channel_id": "C1", "item": {"ts": "1710000000.000100"}}
    member = {"type": "member_joined_channel", "channel": "C1"}
    for event in (reaction, pin, member):
        assert "channel=C1" in event_identity(event, {})
    assert "ts=1710000000.000100" in event_identity(reaction, {})
    assert "ts=1710000000.000100" in event_identity(pin, {})
    assert "ts=-" in event_identity(member, {})
    assert "channel_type=-" in event_identity(reaction, {})
    assert "subtype=-" in event_identity(reaction, {})
    click = click_identity({"channel": {"id": "C1"}, "user": {"id": "U1"}}, {"action_id": "approve"})
    assert "channel=C1" in click and "action_id=approve" in click


def test_queued_messages_expire_and_keep_older_promises_at_capacity():
    warnings = []
    warn = lambda text, *args: warnings.append(text % args)
    first = SimpleNamespace(session_id="s1", queued_at=1.0)
    second = SimpleNamespace(session_id="s1", queued_at=2.0)
    state = AdmissionState()
    assert state.queue_message("s1", first, max_per_session=1, warn=warn)
    assert not state.queue_message("s1", second, max_per_session=1, warn=warn)
    assert state.queued_messages["s1"] == [first]
    state.prune_messages(cutoff=1.5, max_sessions=2, warn=warn)
    assert state.queued_messages == {}
    assert any("which is the cap" in message for message in warnings)
    assert any("never reported that it ended" in message for message in warnings)


def test_pending_events_count_once_and_keep_arrival_order():
    first = SimpleNamespace(session_id="s1", arrival_seq=1, queued_at=1.0)
    second = SimpleNamespace(session_id="s1", arrival_seq=2, queued_at=2.0)
    state = AdmissionState(admitting_events={"s1": {2: second}})
    assert state.hold_event("s1", second, max_per_session=2, max_total=2)
    assert state.pending_count() == 1
    assert state.hold_event("s1", first, max_per_session=2, max_total=2)
    assert state.pending_events["s1"] == [first, second]
    assert state.pending_count() == 2
    assert state.prune_events(cutoff=1.5) == [first]
    assert state.pending_events["s1"] == [second]


def test_pending_event_is_platform_data():
    entry = PendingEvent(
        event={"type": "reaction_added"}, body={}, family="reaction",
        chat_id="C1", chat_type="channel", actor_id="U1", record={},
        session_id="s1", event_id="E1", anchor_ts="1", arrival_seq=1,
    )
    assert entry.queued_at > 0
    assert not entry.loss_logged


def test_admission_state_preserves_queue_order_and_reservations():
    state = AdmissionState()
    first = SimpleNamespace(session_id="s1", queued_at=1.0)
    second = SimpleNamespace(session_id="s1", queued_at=2.0)
    assert state.queue_message("s1", first, max_per_session=2, warn=lambda *_: None)
    assert state.queue_message("s1", second, max_per_session=2, warn=lambda *_: None)
    assert not state.can_reserve("s1", turn_running=False, awaiting_answer=False)
    assert state.take_message("s1") is first
    assert state.take_message("s1") is second
    assert state.can_reserve("s1", turn_running=False, awaiting_answer=False)
    assert state.next_arrival() == 1
    assert state.next_arrival() == 2
    earlier = SimpleNamespace(session_id="s1", arrival_seq=1, queued_at=1.0)
    later = SimpleNamespace(session_id="s1", arrival_seq=2, queued_at=2.0)
    state.track_admitting("s1", earlier)
    assert state.hold_event("s1", later, max_per_session=2, max_total=2)
    assert state.take_pending_event("s1") is None
    assert state.should_hold(
        "s1", 2, reserved=False, turn_running=False, awaiting_answer=False,
    )
    state.finish_admission("s1", 1, False)
    assert state.take_pending_event("s1") is later
    assert state.event_admissions == {"s1"}
    state.event_admissions.discard("s1")
    state.clear()
    assert state.pending_count() == 0
    assert state.event_admissions == set()
    assert state.next_arrival() == 3


def test_queue_decision():
    state = AdmissionState()
    assert not state.must_queue_message(
        "s1", mid_turn="queue", turn_running=False, event_turn_active=False,
    )
    state.event_admissions.add("s1")
    assert state.must_queue_message(
        "s1", mid_turn="steer", turn_running=False, event_turn_active=False,
    )


def test_turn_initiators_expire_and_cannot_retire_a_replacement():
    state = AdmissionState()
    warnings = []
    first = SlackTurnInitiator("U1", "r1", True, "im", started_at=1)
    state.remember_turn("s", first, cap=2, timeout=10, warn=warnings.append)
    state.event_turn_sessions["s"] = "r1"
    replacement = SlackTurnInitiator("U2", "r2", True, "im")
    state.remember_turn("s", replacement, cap=2, timeout=10, warn=warnings.append)
    assert state.event_turn_sessions == {}
    state.forget_turn("s", "r1")
    assert state.turn_initiator("s", timeout=10) is replacement
    state.forget_turn("s", "r2")
    assert state.turn_initiator("s", timeout=10) is None
    assert SlackMessageOrigin(is_dm=True).status_moves_the_reader
    assert not SlackMessageOrigin(is_dm=True, reply_thread_ts="1").status_moves_the_reader


def test_the_core_state_is_the_same_state_under_a_neutral_name():
    from belle_agent.admission.state import AdmissionState as CoreState

    def refusal(state):
        warnings = []
        entry = SimpleNamespace(session_id="s1", queued_at=1.0)
        state.queue_message("s1", entry, max_per_session=1, warn=lambda text, *args: warnings.append(text % args))
        state.queue_message("s1", entry, max_per_session=1, warn=lambda text, *args: warnings.append(text % args))
        return warnings[0]

    assert issubclass(AdmissionState, CoreState)
    assert refusal(AdmissionState()).startswith("Slack refused a queued message")
    assert refusal(CoreState(label="loopback")).startswith("loopback refused a queued message")
