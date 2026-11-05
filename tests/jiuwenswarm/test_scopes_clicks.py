"""Test click gates, unidentified clickers, and connector support."""

from typing import Any

import pytest

from belle_agent.scopes import (
    CLICK_APPROVE,
    CLICK_STOP,
    SECTION_CLICKS,
    SUPPORTED_SECTIONS,
    ChannelCapabilities,
    ClickRule,
    click_rule,
    compile_scopes,
    scoped_chats,
)
from belle_agent.scopes import capabilities as caps
from belle_agent.integrations.jiuwenswarm.scopes import register_host_channels


@pytest.fixture(autouse=True)
def host_channels(registry):
    register_host_channels()

PEOPLE = {
    "alice": {"clicky": "U_ALICE"},
    "bob": {"clicky": "U_BOB"},
    "carol": {"elsewhere": "X_CAROL"},
}
ROLES = {"operator": ["alice"], "member": ["alice", "bob"], "offsite": ["carol"]}


@pytest.fixture(autouse=True)
def clicky(registry):
    """Register synthetic channels with and without clicker identities."""
    registry(
        ChannelCapabilities(
            channel="clicky",
            axes=frozenset({"channel", "chat", "user"}),
            sections={"delivery": None, SECTION_CLICKS: None},
            identity_keys=("user",),
        )
    )
    registry(
        ChannelCapabilities(
            # Renders buttons without naming the clicker.
            channel="anonymous",
            axes=frozenset({"channel", "chat"}),
            sections={SECTION_CLICKS: None},
        )
    )
    registry(
        # Renders none. The other half of the distinction.
        ChannelCapabilities(
            channel="silent",
            axes=frozenset({"channel"}),
            sections={"permissions": None},
        )
    )


def _compile(entry: dict[str, Any], warn, **kw: Any):
    return compile_scopes([entry], people=PEOPLE, roles=ROLES, warn=warn, **kw)


def _refused(entry: dict[str, Any], refusal) -> str:
    return refusal([entry], people=PEOPLE, roles=ROLES)


# The section


def test_clicks_is_a_section_the_loader_knows(warn, warnings):
    # The loader recognizes the clicks section as a supported feature.
    assert SECTION_CLICKS in SUPPORTED_SECTIONS

    scopes = _compile(
        {"match": {"channel": "clicky", "chat": "C1"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    assert scopes
    assert not [line for line in warnings if "is not part of a rule" in line]


def test_a_role_settles_to_the_ids_it_holds_on_this_platform(warn):
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "member"}}},
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert rule == ClickRule(users=("U_ALICE", "U_BOB"), roles=("member",))
    assert rule.permits("U_ALICE")
    assert not rule.permits("U_CHARLIE")


def test_user_and_role_are_one_axis_and_they_or(warn):
    # The same union the match axis performs. AND would mean "alice, but
    # only if also an operator", which nobody would write on purpose.
    scopes = _compile(
        {
            "match": {"channel": "clicky"},
            "clicks": {"approve": {"user": ["U_BOB"], "role": "operator"}},
        },
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert rule.users == ("U_ALICE", "U_BOB")


def test_both_gestures_are_settled_independently(warn):
    scopes = _compile(
        {
            "match": {"channel": "clicky"},
            "clicks": {"approve": {"role": "operator"}, "stop": {"role": "member"}},
        },
        warn,
    )

    assert click_rule(scopes, channel="clicky", kind=CLICK_APPROVE).users == ("U_ALICE",)
    assert click_rule(scopes, channel="clicky", kind=CLICK_STOP).users == (
        "U_ALICE",
        "U_BOB",
    )


def test_a_gesture_nobody_renders_is_refused(refusal):
    error = _refused({"match": {"channel": "clicky"}, "clicks": {"undo": {"role": "operator"}}}, refusal)

    assert "scopes[0].clicks.undo is not a clicks setting. The settings are approve, queue_drop, stop" in error


# Composition: the map merges per gesture, a clause replaces


def test_a_narrower_rule_naming_one_gesture_leaves_the_other_alone(warn):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "clicky"},
                "clicks": {"approve": {"role": "operator"}, "stop": {"role": "member"}},
            },
            {
                "match": {"channel": "clicky", "chat": "C1"},
                "clicks": {"stop": {"role": "operator"}},
            },
        ],
        people=PEOPLE,
        roles=ROLES,
        warn=warn,
    )

    assert click_rule(scopes, channel="clicky", chat="C1").users == ("U_ALICE",)
    assert click_rule(scopes, channel="clicky", chat="C1", kind=CLICK_STOP).users == (
        "U_ALICE",
    )
    assert click_rule(scopes, channel="clicky", chat="C2", kind=CLICK_STOP).users == (
        "U_ALICE",
        "U_BOB",
    )


def test_a_later_clause_replaces_rather_than_widening_the_one_above(warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "member"}}},
            {
                "match": {"channel": "clicky", "chat": "C1"},
                "clicks": {"approve": {"user": ["U_BOB"]}},
            },
        ],
        people=PEOPLE,
        roles=ROLES,
        warn=warn,
    )

    assert click_rule(scopes, channel="clicky", chat="C1").users == ("U_BOB",)
    assert click_rule(scopes, channel="clicky", chat="C9").users == (
        "U_ALICE",
        "U_BOB",
    )


def test_a_clicks_rule_never_opts_a_conversation_into_being_answered(warn):
    scopes = _compile(
        {
            "match": {"channel": "clicky", "chat": "C_LOCKED"},
            "clicks": {"approve": {"role": "operator"}},
        },
        warn,
    )

    assert scopes
    assert scoped_chats(scopes, channel="clicky") == ()


# No rule, no change


def test_no_clicks_rule_settles_to_none_rather_than_to_nobody(warn):
    """``None`` means no rule; an empty rule means nobody. Not the same thing."""
    scopes = _compile(
        {"match": {"channel": "clicky"}, "delivery": {"mode": ["mention"]}}, warn
    )

    assert click_rule(scopes, channel="clicky") is None


def test_a_rule_on_one_conversation_leaves_the_others_on_layer_zero(warn):
    scopes = _compile(
        {
            "match": {"channel": "clicky", "chat": "C1"},
            "clicks": {"approve": {"role": "operator"}},
        },
        warn,
    )

    assert click_rule(scopes, channel="clicky", chat="C1") is not None
    assert click_rule(scopes, channel="clicky", chat="C2") is None


# A rule matches and the clicker cannot be identified


def test_a_click_carrying_no_id_is_refused(warn):
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "member"}}},
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert not rule.permits("")
    assert not rule.permits(None)
    assert not rule.permits("   ")


def test_a_role_nobody_here_is_in_admits_nobody_here(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "offsite"}}},
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert rule == ClickRule(users=(), roles=("offsite",))
    assert not rule.permits("U_ALICE")
    assert any(
        "admits nobody on clicky" in line and "is refused" in line for line in warnings
    )


def test_an_undeclared_role_is_refused(refusal):
    error = _refused({"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "opeartor"}}}, refusal)

    assert "scopes[0].clicks.approve names opeartor, which is not a valid role under roles" in error
    assert "The valid roles are member, offsite, operator" in error


def test_an_empty_clause_is_refused_rather_than_read_as_everyone(refusal):
    error = _refused({"match": {"channel": "clicky"}, "clicks": {"approve": {}}}, refusal)

    assert "scopes[0].clicks.approve={} is not a mapping with user or role" in error


def test_a_clause_that_is_not_a_mapping_is_refused(refusal):
    error = _refused({"match": {"channel": "clicky"}, "clicks": {"approve": ["U_ALICE"]}}, refusal)

    assert "scopes[0].clicks.approve=['U_ALICE'] is not a mapping with user or role" in error


def test_not_is_not_a_way_of_naming_who_may_click(refusal):
    # "anyone but these people may approve" is the permissive default clicks
    # exists to close, written the long way round.
    error = _refused(
        {
            "match": {"channel": "clicky"},
            "clicks": {"approve": {"not": {"user": ["U_BOB"]}}},
        },
        refusal,
    )

    assert "scopes[0].clicks.approve.not is not a clicker selector. Write user or role" in error


# No surface is not no identity


def test_a_channel_that_renders_no_buttons_reports_no_effect(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "silent"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    assert scopes == ()
    assert any("clicks has no effect" in line for line in warnings)
    # Not the other warning: there is no click to refuse here.
    assert not any("is refused rather than gated" in line for line in warnings)


def test_a_channel_that_renders_buttons_and_names_nobody_refuses_every_click(
    warn, warnings
):
    scopes = _compile(
        {"match": {"channel": "anonymous"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    rule = click_rule(scopes, channel="anonymous")
    assert rule is not None
    assert not rule.permits("U_ALICE")
    assert any("is refused rather than gated" in line for line in warnings)
    assert not any("clicks has no effect" in line for line in warnings)


def test_the_pseudo_channels_render_none_and_say_so():
    for pseudo in ("__cron__", "__heartbeat__"):
        declared = caps.channel_capabilities(pseudo)
        assert not declared.reads(SECTION_CLICKS)


# The section is about the conversation, not about the sender


def test_a_rule_that_also_names_a_sender_cannot_gate_a_click(refusal):
    error = _refused(
        {
            "match": {"channel": "clicky", "chat": "C1", "user": ["U_ALICE"]},
            "clicks": {"approve": {"role": "operator"}},
        },
        refusal,
    )

    assert "scopes[0].clicks cannot be in a rule that matches on user" in error
    assert "has no effect on a rule that also names a sender" in error


def test_the_rule_does_not_move_with_the_clicker(warn):
    """Composed per conversation, so the same answer whoever is asking."""
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    assert click_rule(scopes, channel="clicky").users == ("U_ALICE",)
