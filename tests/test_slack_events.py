"""Slack event policy and buffering."""

import pytest

from belle_agent.admission.buffer import DROP_EXPIRED, DROP_OVERFLOW, InboundEventBuffer
from belle_agent.platforms.slack.events import (
    EVENT_ANCHORED_FAMILIES,
    EVENT_BUFFER_LIMITS,
    EVENT_BUFFER_TTL_SECONDS,
    EVENT_CONTEXT,
    EVENT_DISPOSITION_DEFAULT,
    EVENT_FAMILIES,
    EVENT_FAMILY_APP_HOME,
    EVENT_FAMILY_MEMBER,
    EVENT_FAMILY_PIN,
    EVENT_FAMILY_REACTION,
    EVENT_OFF,
    EVENT_TURN,
    EVENT_TURN_FAMILIES,
    EVENT_TURN_WITHHELD,
    EVENT_TYPE_FAMILIES,
    disposition_holds,
    event_actor_id,
    event_anchor,
    event_chat_id,
    event_disposition,
    event_family,
    family_anchors_on_a_message,
    normalize_event_policy,
    render_event,
    turn_withheld_reason,
)


def _buffer(**kwargs):
    kwargs.setdefault("limits", EVENT_BUFFER_LIMITS)
    kwargs.setdefault("ttl_seconds", EVENT_BUFFER_TTL_SECONDS)
    return InboundEventBuffer(**kwargs)


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reaction(emoji: str = "eyes", user: str = "U1", ts: str = "1710000000.000100"):
    return {
        "type": "reaction_added",
        "user": user,
        "reaction": emoji,
        "item": {"type": "message", "channel": "C1", "ts": ts},
        "item_user": "U-BOT",
        "event_ts": "1710000100.000200",
    }


def _pin(user: str = "U1"):
    return {
        "type": "pin_added",
        "user": user,
        "channel_id": "C1",
        "item": {
            "type": "message",
            "channel": "C1",
            "message": {"ts": "1710000000.000100", "text": "keep this"},
        },
        "event_ts": "1710000200.000300",
    }


def _member(user: str = "U1", kind: str = "member_joined_channel"):
    return {
        "type": kind,
        "user": user,
        "channel": "C1",
        "channel_type": "C",
        "team": "T1",
        "event_ts": "1710000300.000400",
    }


def _app_home(user: str = "U1", tab: str = "home", *, view: bool = True):
    """Return an App Home event with a DM channel and optional view."""
    event = {
        "type": "app_home_opened",
        "user": user,
        "channel": "D1",
        "event_ts": "1710000400.000500",
        "tab": tab,
    }
    if view:
        event["view"] = {
            "id": "V1",
            "team_id": "T1",
            "type": "home",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * 400}}],
            "state": {"values": {}},
            "hash": "1710000000.abcdefgh",
            "private_metadata": "",
        }
    return event


def test_every_family_covers_both_directions_of_its_state_change() -> None:
    by_family: dict[str, set[str]] = {family: set() for family in EVENT_FAMILIES}
    for event_type, family in EVENT_TYPE_FAMILIES.items():
        by_family[family].add(event_type)
    assert by_family == {
        EVENT_FAMILY_REACTION: {"reaction_added", "reaction_removed"},
        EVENT_FAMILY_PIN: {"pin_added", "pin_removed"},
        EVENT_FAMILY_MEMBER: {"member_joined_channel", "member_left_channel"},
        EVENT_FAMILY_APP_HOME: {"app_home_opened"},
    }


def test_unset_means_off_for_every_family() -> None:
    assert EVENT_DISPOSITION_DEFAULT == EVENT_OFF
    for family in EVENT_FAMILIES:
        assert event_disposition(None, family) == EVENT_OFF
        assert event_disposition({}, family) == EVENT_OFF


def test_a_family_set_to_context_is_the_only_one_that_buffers() -> None:
    policy = {EVENT_FAMILY_REACTION: EVENT_CONTEXT}
    assert event_disposition(policy, EVENT_FAMILY_REACTION) == EVENT_CONTEXT
    assert event_disposition(policy, EVENT_FAMILY_PIN) == EVENT_OFF


def test_normalize_settles_case_and_space_and_drops_what_it_does_not_know() -> None:
    assert normalize_event_policy(
        {"  Reaction ": " Context ", "pin": "off", "nonsense": "context", "member": "nonsense"}
    ) == {EVENT_FAMILY_REACTION: EVENT_CONTEXT, EVENT_FAMILY_PIN: EVENT_OFF}
    assert normalize_event_policy("context") is None


def test_turn_round_trips_through_the_normaliser_for_the_families_that_take_it() -> None:
    assert normalize_event_policy({" Reaction ": " Turn ", "pin": "TURN"}) == {
        EVENT_FAMILY_REACTION: EVENT_TURN,
        EVENT_FAMILY_PIN: EVENT_TURN,
    }
    policy = normalize_event_policy({"reaction": "turn"})
    assert event_disposition(policy, EVENT_FAMILY_REACTION) == EVENT_TURN
    assert event_disposition(policy, EVENT_FAMILY_PIN) == EVENT_OFF


def test_the_three_state_change_families_take_turn() -> None:
    assert set(EVENT_TURN_FAMILIES) == {
        EVENT_FAMILY_REACTION,
        EVENT_FAMILY_PIN,
        EVENT_FAMILY_MEMBER,
    }
    for family in EVENT_TURN_FAMILIES:
        assert disposition_holds(family, EVENT_TURN)
        assert normalize_event_policy({family: "turn"}) == {family: EVENT_TURN}
        assert event_disposition({family: EVENT_TURN}, family) == EVENT_TURN


def test_app_home_takes_off_and_context_and_nothing_else() -> None:
    assert disposition_holds(EVENT_FAMILY_APP_HOME, EVENT_OFF)
    assert disposition_holds(EVENT_FAMILY_APP_HOME, EVENT_CONTEXT)
    assert not disposition_holds(EVENT_FAMILY_APP_HOME, EVENT_TURN)

    assert normalize_event_policy({"app_home": "context"}) == {EVENT_FAMILY_APP_HOME: EVENT_CONTEXT}
    assert normalize_event_policy({"app_home": "off"}) == {EVENT_FAMILY_APP_HOME: EVENT_OFF}
    assert event_disposition({EVENT_FAMILY_APP_HOME: EVENT_CONTEXT}, EVENT_FAMILY_APP_HOME) == EVENT_CONTEXT

    assert normalize_event_policy({"app_home": "turn"}) == {}
    assert event_disposition({EVENT_FAMILY_APP_HOME: EVENT_TURN}, EVENT_FAMILY_APP_HOME) == EVENT_OFF


def test_turn_withheld_reason_names_the_missing_destination() -> None:
    reason = turn_withheld_reason(EVENT_FAMILY_APP_HOME)
    assert reason == EVENT_TURN_WITHHELD[EVENT_FAMILY_APP_HOME]
    assert "no message to answer" in reason
    assert "splits on a payload field" in reason
    assert "tab" in reason

    assert turn_withheld_reason("star") != reason
    assert "nothing says where a turn woken by one of these would post" in turn_withheld_reason("star")


def test_the_word_still_needs_a_family_with_a_destination() -> None:
    assert not disposition_holds("star", EVENT_TURN)
    assert event_disposition({"star": EVENT_TURN}, "star") == EVENT_OFF


def test_only_the_message_families_anchor_on_a_message() -> None:
    assert set(EVENT_ANCHORED_FAMILIES) == {EVENT_FAMILY_REACTION, EVENT_FAMILY_PIN}
    assert family_anchors_on_a_message(EVENT_FAMILY_REACTION)
    assert family_anchors_on_a_message(EVENT_FAMILY_PIN)
    assert not family_anchors_on_a_message(EVENT_FAMILY_MEMBER)
    assert not family_anchors_on_a_message(EVENT_FAMILY_APP_HOME)
    assert EVENT_FAMILY_APP_HOME not in EVENT_TURN_FAMILIES


def test_a_reaction_anchors_on_the_message_it_names() -> None:
    ts, thread_ts = event_anchor(_reaction(), family=EVENT_FAMILY_REACTION)
    assert ts == "1710000000.000100"
    assert thread_ts == ts


def test_a_pin_anchors_on_the_message_nested_inside_its_item() -> None:
    ts, thread_ts = event_anchor(_pin(), family=EVENT_FAMILY_PIN)
    assert ts == "1710000000.000100"
    assert thread_ts == ts


def test_an_anchor_inside_a_thread_keeps_that_thread() -> None:
    event = _pin()
    event["item"]["message"]["thread_ts"] = "1709000000.000001"
    ts, thread_ts = event_anchor(event, family=EVENT_FAMILY_PIN)
    assert (ts, thread_ts) == ("1710000000.000100", "1709000000.000001")


def test_an_event_naming_no_message_has_no_anchor() -> None:
    on_a_file = {
        "type": "reaction_added",
        "user": "U1",
        "reaction": "eyes",
        "item": {"type": "file", "channel": "C1", "file": "F1"},
    }
    assert event_anchor(on_a_file, family=EVENT_FAMILY_REACTION) == ("", "")
    assert event_anchor(_member(), family=EVENT_FAMILY_MEMBER) == ("", "")
    assert event_anchor(_app_home(), family=EVENT_FAMILY_APP_HOME) == ("", "")


def test_a_reaction_renders_with_the_settled_field_names() -> None:
    record = render_event(_reaction(), family=EVENT_FAMILY_REACTION)
    assert record == {
        "family": "reaction",
        "event_type": "reaction_added",
        "event_ts_iso_utc": "2024-03-09T16:01:40.000200Z",
        "author_user_id": "U1",
        "author_name": "U1",
        "emoji_name": "eyes",
        "ts": "1710000000.000100",
        "ts_iso_utc": "2024-03-09T16:00:00.000100Z",
    }


def test_author_fields_name_the_actor_not_the_author_of_the_item() -> None:
    record = render_event(_reaction(user="U-REACTOR"), family=EVENT_FAMILY_REACTION)
    assert record["author_user_id"] == "U-REACTOR"
    assert event_actor_id(_reaction(user="U-REACTOR")) == "U-REACTOR"


def test_a_pin_finds_the_message_ts_one_level_deeper() -> None:
    record = render_event(_pin(), family=EVENT_FAMILY_PIN)
    assert record["ts"] == "1710000000.000100"
    assert record["event_type"] == "pin_added"
    assert "emoji_name" not in record


def test_a_membership_change_carries_no_message_identifier() -> None:
    record = render_event(_member(), family=EVENT_FAMILY_MEMBER)
    assert "ts" not in record
    assert "ts_iso_utc" not in record
    assert record["author_user_id"] == "U1"


def test_an_app_home_opening_renders_with_the_tab_it_was_opened_on() -> None:
    for tab in ("home", "messages"):
        record = render_event(_app_home(tab=tab), family=EVENT_FAMILY_APP_HOME)
        assert record == {
            "family": "app_home",
            "event_type": "app_home_opened",
            "event_ts_iso_utc": "2024-03-09T16:06:40.000500Z",
            "author_user_id": "U1",
            "author_name": "U1",
            "tab": tab,
        }


def test_a_tab_the_payload_did_not_name_is_absent_rather_than_guessed() -> None:
    bare = {k: v for k, v in _app_home().items() if k != "tab"}
    record = render_event(bare, family=EVENT_FAMILY_APP_HOME)
    assert "tab" not in record
    assert record["event_type"] == "app_home_opened"


def test_is_author_bot_is_written_only_when_the_payload_states_it() -> None:
    assert "is_author_bot" not in render_event(_reaction(), family=EVENT_FAMILY_REACTION)
    marked = render_event({**_reaction(), "bot_id": "B1"}, family=EVENT_FAMILY_REACTION)
    assert marked["is_author_bot"] is True


def test_the_conversation_is_read_from_whichever_spelling_slack_used() -> None:
    assert event_chat_id(_reaction()) == "C1"
    assert event_chat_id(_pin()) == "C1"
    assert event_chat_id(_member()) == "C1"
    assert event_chat_id(_app_home()) == "D1"


def test_event_family_names_only_the_types_this_connector_reads() -> None:
    assert event_family(_reaction()) == EVENT_FAMILY_REACTION
    assert event_family(_app_home()) == EVENT_FAMILY_APP_HOME
    assert event_family({"type": "star_added"}) is None
    assert event_family({"type": "app_home_closed"}) is None


def test_nothing_held_folds_to_nothing() -> None:
    assert _buffer().fold("C1") is None


def test_a_fold_reports_the_conversation_and_every_record_oldest_first() -> None:
    clock = _Clock()
    buffer = _buffer(now=clock)
    buffer.record("C1", EVENT_FAMILY_REACTION, {"family": "reaction", "n": 1})
    clock.advance(1)
    buffer.record("C1", EVENT_FAMILY_MEMBER, {"family": "member", "n": 2})
    clock.advance(1)
    buffer.record("C1", EVENT_FAMILY_REACTION, {"family": "reaction", "n": 3})

    payload = buffer.fold("C1")

    assert payload == {
        "chat_id": "C1",
        "events": [
            {"family": "reaction", "n": 1},
            {"family": "member", "n": 2},
            {"family": "reaction", "n": 3},
        ],
    }
    assert buffer.fold("C1") is None


def test_the_buffer_is_per_conversation() -> None:
    buffer = _buffer()
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})
    buffer.record("C2", EVENT_FAMILY_REACTION, {"n": 2})
    assert buffer.fold("C1") == {"chat_id": "C1", "events": [{"n": 1}]}
    assert buffer.fold("C2") == {"chat_id": "C2", "events": [{"n": 2}]}


def test_a_reaction_flood_cannot_evict_a_membership_change() -> None:
    buffer = _buffer(limits={EVENT_FAMILY_REACTION: 3, EVENT_FAMILY_PIN: 3, EVENT_FAMILY_MEMBER: 3})
    buffer.record("C1", EVENT_FAMILY_MEMBER, {"family": "member", "n": "join"})
    for index in range(50):
        buffer.record("C1", EVENT_FAMILY_REACTION, {"family": "reaction", "n": index})

    payload = buffer.fold("C1")

    assert {"family": "member", "n": "join"} in payload["events"]
    reactions = [e for e in payload["events"] if e["family"] == "reaction"]
    assert len(reactions) == 3


def test_overflow_drops_the_oldest_and_says_how_many() -> None:
    buffer = _buffer(limits={EVENT_FAMILY_REACTION: 2})
    for index in range(5):
        buffer.record("C1", EVENT_FAMILY_REACTION, {"n": index})

    payload = buffer.fold("C1")

    assert payload["events"] == [{"n": 3}, {"n": 4}]
    assert payload["dropped"] == {EVENT_FAMILY_REACTION: {DROP_OVERFLOW: 3}}


def test_an_event_older_than_the_ttl_is_dropped_and_counted() -> None:
    clock = _Clock()
    buffer = _buffer(ttl_seconds=3600.0, now=clock)
    for index in range(3):
        buffer.record("C1", EVENT_FAMILY_REACTION, {"n": index})
    clock.advance(3601)
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": "fresh"})

    payload = buffer.fold("C1")

    assert payload["events"] == [{"n": "fresh"}]
    assert payload["dropped"] == {EVENT_FAMILY_REACTION: {DROP_EXPIRED: 3}}


def test_a_quiet_room_expires_at_the_fold_as_well_as_on_arrival() -> None:
    clock = _Clock()
    buffer = _buffer(ttl_seconds=3600.0, now=clock)
    buffer.record("C1", EVENT_FAMILY_PIN, {"n": "stale"})
    clock.advance(172800)

    payload = buffer.fold("C1")

    assert payload == {
        "chat_id": "C1",
        "events": [],
        "dropped": {EVENT_FAMILY_PIN: {DROP_EXPIRED: 1}},
    }


def test_a_drop_count_is_reported_once_and_then_cleared() -> None:
    buffer = _buffer(limits={EVENT_FAMILY_REACTION: 1})
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 0})
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})

    assert buffer.fold("C1")["dropped"] == {EVENT_FAMILY_REACTION: {DROP_OVERFLOW: 1}}
    assert buffer.fold("C1") is None


def test_records_are_never_coalesced() -> None:
    buffer = _buffer(limits={EVENT_FAMILY_REACTION: 30})
    for index in range(20):
        buffer.record("C1", EVENT_FAMILY_REACTION, {"emoji_name": "eyes", "author_user_id": "U1"})
    assert len(buffer.fold("C1")["events"]) == 20


def test_forget_drops_one_conversation_or_all_of_them() -> None:
    buffer = _buffer()
    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})
    buffer.record("C2", EVENT_FAMILY_REACTION, {"n": 2})
    buffer.forget("C1")
    assert buffer.fold("C1") is None
    assert buffer.fold("C2") is not None

    buffer.record("C1", EVENT_FAMILY_REACTION, {"n": 1})
    buffer.record("C2", EVENT_FAMILY_REACTION, {"n": 2})
    buffer.forget()
    assert buffer.fold("C1") is None
    assert buffer.fold("C2") is None


@pytest.mark.parametrize("family", list(EVENT_FAMILIES))
def test_the_shipped_bound_is_positive_for_every_family(family: str) -> None:
    assert EVENT_BUFFER_LIMITS[family] > 0


def test_the_app_home_bound_is_the_tightest_and_holds_the_recent_entries() -> None:
    assert EVENT_BUFFER_LIMITS[EVENT_FAMILY_APP_HOME] == 5
    assert EVENT_BUFFER_LIMITS[EVENT_FAMILY_APP_HOME] == min(EVENT_BUFFER_LIMITS.values())

    buffer = _buffer()
    for index in range(9):
        buffer.record("D1", EVENT_FAMILY_APP_HOME, {"n": index})

    folded = buffer.fold("D1")
    assert folded is not None
    assert [record["n"] for record in folded["events"]] == [4, 5, 6, 7, 8]
    assert folded["dropped"] == {EVENT_FAMILY_APP_HOME: {DROP_OVERFLOW: 4}}



def test_disposition_read_requires_an_exact_slack_family_key() -> None:
    assert event_disposition({" Reaction ": EVENT_CONTEXT}, EVENT_FAMILY_REACTION) == EVENT_OFF
    assert event_disposition({EVENT_FAMILY_REACTION: " Context "}, EVENT_FAMILY_REACTION) == EVENT_CONTEXT
