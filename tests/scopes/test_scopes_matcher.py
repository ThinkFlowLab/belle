"""Test scope matching and layer order."""

from typing import Any

import pytest

from belle_agent.scopes import (
    ChannelCapabilities,
    Scope,
    ScopeMatch,
    compile_scopes,
    matching_scopes,
)


@pytest.fixture
def demo_channel(registry):
    """A channel that reads everything, so a test can isolate one rule."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "user"}),
            sections={"delivery": None, "agent": None},
            # Supply sender IDs for user-axis matching.
            identity_keys=("user",),
        )
    )


def _compile(entries: Any, warn, **kwargs: Any) -> tuple[Scope, ...]:
    return compile_scopes(entries, warn=warn, **kwargs)


# Shape of a match


def test_an_absent_axis_matches_anything(demo_channel, warn, warnings):
    scopes = _compile([{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}], warn)

    assert scopes[0].selects(channel="demo", chat="C1")
    assert scopes[0].selects(channel="demo", chat=None)
    assert not scopes[0].selects(channel="web", chat="C1")
    assert warnings == []


def test_a_match_with_no_criteria_applies_everywhere(warn, warnings):
    scopes = _compile([{"match": {}, "delivery": {"mode": ["all"]}}], warn)

    assert scopes[0].selects(channel="demo", chat="C1")
    assert scopes[0].selects(channel="web", chat=None)
    assert scopes[0].layer == (0, 0, 0, 0)
    assert warnings == []


def test_axes_and_rather_than_or(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert scopes[0].selects(channel="demo", chat="C1")
    assert not scopes[0].selects(channel="demo", chat="C2")
    assert not scopes[0].selects(channel="web", chat="C1")


def test_ids_are_matched_exactly(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="c1")
    assert not scopes[0].selects(channel="demo", chat="C1 ")


def test_surrounding_whitespace_in_an_id_is_settled_at_load(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": " demo ", "chat": " C1 "}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert scopes[0].match == ScopeMatch(channel="demo", chat="C1")


# Layering


def test_specificity_decides_the_layer(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
        ],
        warn,
    )

    assert [scope.layer for scope in scopes] == [(1, 0, 0, 0), (1, 0, 2, 0)]


def test_a_more_specific_scope_wins_even_when_written_first(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1")
    assert [scope.layer for scope in ordered] == [(1, 0, 0, 0), (1, 0, 2, 0)]


def test_within_one_layer_the_later_entry_wins(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}},
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1")
    assert [scope.index for scope in ordered] == [0, 1]


def test_a_scope_that_does_not_select_is_not_returned(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo", "chat": "C2"}, "delivery": {"mode": ["reply"]}},
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1")
    assert [scope.match.chat for scope in ordered] == ["C1"]


def test_a_scope_with_nothing_in_the_section_asked_for_is_skipped(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}], warn
    )

    assert matching_scopes(scopes, channel="demo", section="agent") == ()


def test_the_two_sections_of_one_rule_are_kept_apart(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["all"]},
                "agent": {"model_name": "m"},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].section("delivery") == {"mode": ["all"]}
    assert scopes[0].section("agent") == {"model_name": "m"}


# Shapes that are not rules


def test_scopes_absent_is_no_scopes(warn, warnings):
    assert compile_scopes(None, warn=warn) == ()
    assert warnings == []


def test_scopes_that_is_not_a_list_is_refused(refusal):
    assert "scopes={'match': {}} is not a list of rules" in refusal({"match": {}})


def test_an_entry_that_is_not_a_mapping_is_refused(demo_channel, refusal):
    error = refusal(["nonsense", {"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0]='nonsense' is not a rule" in error


def test_an_unknown_top_level_key_in_a_rule_is_refused(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo"}, "delivry": {}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].delivry is not a rule key" in error


def test_a_match_that_is_not_a_mapping_is_refused(refusal):
    assert "scopes[0].match='slack' is not a mapping" in refusal([{"match": "slack", "delivery": {"mode": ["all"]}}])


def test_an_unquoted_numeric_id_is_refused_rather_than_matching_nothing(demo_channel, refusal):
    error = refusal([{"match": {"chat": 12345}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].match.chat=12345 is not text. Quote the value" in error


def test_an_empty_axis_value_is_refused(demo_channel, refusal):
    assert "scopes[0].match.chat is empty" in refusal([{"match": {"chat": "  "}, "delivery": {"mode": ["all"]}}])


def test_a_rule_whose_sections_all_dropped_out_is_not_kept(demo_channel, warn):
    assert _compile([{"match": {"channel": "demo"}}], warn) == ()


# The identity axis


def test_a_user_axis_selects_only_the_senders_it_names(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U1", "U2"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U1")
    assert scopes[0].selects(channel="demo", chat="C9", user="U2")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U3")


def test_user_and_role_are_one_axis_so_a_not_does_not_add_a_layer(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U1"]},
                "delivery": {"prompt": "positive"},
            },
            {
                "match": {
                    "channel": "demo",
                    "chat": "C1",
                    "user": ["U1"],
                    "not": {"user": ["U2"]},
                },
                "delivery": {"prompt": "positive and negative"},
            },
        ],
        warn,
    )

    assert [scope.layer for scope in scopes] == [(1, 0, 2, 2), (1, 0, 2, 2)]


def test_a_not_on_its_own_still_constrains_the_identity_axis(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"prompt": "everyone"},
            },
            {
                "match": {
                    "channel": "demo",
                    "chat": "C1",
                    "not": {"user": ["U_admin"]},
                },
                "delivery": {"prompt": "everyone but the admin"},
            },
        ],
        warn,
    )

    assert [scope.layer for scope in scopes] == [(1, 0, 2, 0), (1, 0, 2, 1)]
    assert scopes[1].selects(channel="demo", chat="C1", user="U_other")
    assert not scopes[1].selects(channel="demo", chat="C1", user="U_admin")


def test_a_positive_and_a_negative_together_are_the_team_minus_one(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "user": ["U1", "U2", "U3"],
                    "not": {"user": ["U2"]},
                },
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U1")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U2")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U9")


def test_an_unidentified_sender_matches_no_positive_and_escapes_no_negative(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U1"]},
                "delivery": {"prompt": "named"},
            },
            {
                "match": {"channel": "demo", "not": {"user": ["U1"]}},
                "delivery": {"prompt": "restricted"},
            },
        ],
        warn,
    )

    for absent in (None, "", "   "):
        assert not scopes[0].selects(channel="demo", chat="C1", user=absent)
        assert scopes[1].selects(channel="demo", chat="C1", user=absent)


def test_ids_in_an_identity_list_are_settled_at_load(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": [" U2 ", "U1", "U1"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Stripped, deduplicated and sorted. A list is a set: its order means no
    # meaning, so settling one keeps two spellings of the same rule equal.
    assert scopes[0].match.users == ("U1", "U2")
    assert warnings == []


def test_a_chat_bearing_scope_outranks_a_user_bearing_one_either_way_round(
    demo_channel, warn, warnings
):
    user_first = _compile(
        [
            {"match": {"channel": "demo", "user": ["U1"]}, "delivery": {"prompt": "u"}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
        ],
        warn,
    )
    chat_first = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
            {"match": {"channel": "demo", "user": ["U1"]}, "delivery": {"prompt": "u"}},
        ],
        warn,
    )

    assert [scope.layer for scope in user_first] == [(1, 0, 0, 2), (1, 0, 2, 0)]
    assert [scope.layer for scope in chat_first] == [(1, 0, 2, 0), (1, 0, 0, 2)]

    def _last(scopes):
        return matching_scopes(scopes, channel="demo", chat="C1", user="U1")[-1]

    assert _last(user_first).section("delivery") == {"prompt": "c"}
    assert _last(chat_first).section("delivery") == {"prompt": "c"}


def test_naming_both_a_conversation_and_a_sender_outranks_naming_either(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U1"]},
                "delivery": {"prompt": "both"},
            },
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "chat"}},
            {"match": {"channel": "demo", "user": ["U1"]}, "delivery": {"prompt": "user"}},
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1", user="U1")
    assert [scope.layer for scope in ordered] == [(1, 0, 0, 2), (1, 0, 2, 0), (1, 0, 2, 2)]
    assert ordered[-1].section("delivery") == {"prompt": "both"}


def test_a_caller_with_no_sender_gets_the_unidentified_answer(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "chat"}},
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U1"]},
                "delivery": {"prompt": "user"},
            },
        ],
        warn,
    )

    assert matching_scopes(scopes, channel="demo", chat="C1") == (scopes[0],)
    assert len(matching_scopes(scopes, channel="demo", chat="C1", user="U1")) == 2


# Identity shapes that are refused


def _refused_match(refusal, match: dict) -> str:
    return refusal([{"match": match, "delivery": {"mode": ["all"]}}])


def test_a_bare_id_where_a_list_is_wanted_is_refused(demo_channel, refusal):
    error = _refused_match(refusal, {"channel": "demo", "user": "U1"})

    assert "scopes[0].match.user='U1' is one id. Write a list of ids: [U1]" in error


def test_an_empty_identity_list_is_refused_rather_than_meaning_everyone(demo_channel, refusal):
    error = _refused_match(refusal, {"channel": "demo", "user": []})

    assert "scopes[0].match.user is an empty list. Write at least one id" in error


def test_an_unquoted_numeric_id_in_an_identity_list_is_refused(demo_channel, refusal):
    error = _refused_match(refusal, {"channel": "demo", "user": [12345]})

    assert "scopes[0].match.user=[12345] is not a list of ids. Quote each id" in error


def test_a_not_that_is_not_a_mapping_is_refused(demo_channel, refusal):
    error = _refused_match(refusal, {"channel": "demo", "not": ["U1"]})

    assert "scopes[0].match.not=['U1'] is not a mapping with user or role" in error


def test_an_empty_not_is_refused(demo_channel, refusal):
    assert "scopes[0].match.not={} is not a mapping with user or role" in _refused_match(
        refusal, {"channel": "demo", "not": {}}
    )


@pytest.mark.parametrize("axis", ["chat", "channel"])
def test_a_not_on_a_non_identity_axis_is_refused(demo_channel, refusal, axis):
    error = _refused_match(refusal, {"channel": "demo", "not": {axis: ["C2"]}})

    assert f"scopes[0].match.not.{axis} is not a sender selector. not takes user or role only" in error


def test_a_not_on_a_role_no_config_declares_is_refused(demo_channel, refusal):
    error = _refused_match(refusal, {"channel": "demo", "not": {"role": "admin"}})

    assert "scopes[0].match names admin, which is not a valid role under roles" in error


def test_a_sender_named_without_a_platform_is_refused(demo_channel, refusal):
    error = _refused_match(refusal, {"user": ["U1"]})

    assert "scopes[0].match names senders (U1) but no channel" in error


@pytest.mark.parametrize(
    ("match", "message"),
    [
        ({"chat": "C1", "workspace": "T1", "user": ["U1"]}, "names a conversation"),
        ({"workspace": "T1", "user": ["U1"]}, "names a workspace"),
        ({"user": ["U1"]}, "names senders"),
    ],
)
def test_missing_channel_reports_the_first_specific_selector(match, message, refusal, warnings):
    entries = _refused_match(refusal, match).splitlines()[1:]

    assert len(entries) == 1
    assert message in entries[0]
    assert warnings == []


def test_an_axis_that_is_not_an_axis_at_all_is_refused(demo_channel, refusal):
    error = _refused_match(refusal, {"team": "T1"})

    assert "scopes[0].match.team is not a match key. The match keys are" in error


def test_a_section_the_channel_does_not_read_is_reported_as_ineffective(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo"},
                "permissions": {"tools": {"bash": "deny"}},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section("permissions") == {}
    assert any("does not read the permissions section" in line for line in warnings)


def test_supported_sections():
    from belle_agent.scopes import SUPPORTED_SECTIONS

    assert SUPPORTED_SECTIONS == ("delivery", "agent", "permissions", "clicks")


def test_the_agent_section_is_stored_rather_than_warned_about(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [{"match": {"channel": "demo"}, "agent": {"model_name": "m"}}], warn
    )

    # A rule with only the agent axis remains a complete rule.
    assert len(scopes) == 1
    assert scopes[0].section("agent") == {"model_name": "m"}
    assert warnings == []
