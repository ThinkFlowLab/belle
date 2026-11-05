"""Test conversation-kind matching and validation with a synthetic channel."""

from typing import Any

import pytest

from belle_agent.scopes import (
    AXIS_CHAT_TYPE,
    ChannelCapabilities,
    Scope,
    ScopeMatch,
    compile_scopes,
    compose_section,
    matching_scopes,
    register_channel,
)
from belle_agent.scopes import capabilities as caps


@pytest.fixture
def demo_channel(registry):
    """A platform with two kinds of conversation and nothing else to it."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "chat_type", "user"}),
            sections={"delivery": None, "agent": None},
            identity_keys=("user",),
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
        )
    )


@pytest.fixture
def typeless_channel(registry):
    """A platform that has conversations but no kinds of them."""
    registry(
        ChannelCapabilities(
            channel="flat",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
        )
    )


def _compile(entries: Any, warn, **kwargs: Any) -> tuple[Scope, ...]:
    return compile_scopes(entries, warn=warn, **kwargs)


# Matching


def test_a_kind_selects_every_conversation_of_that_kind(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes[0].match.chat_type == ("direct",)
    assert scopes[0].selects(channel="demo", chat="D1", chat_type="direct")
    assert scopes[0].selects(channel="demo", chat="D2", chat_type="direct")
    assert not scopes[0].selects(channel="demo", chat="C1", chat_type="room")
    assert warnings == []


def test_a_caller_with_no_kind_gets_the_unnamed_answer(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="C1")
    assert compose_section(scopes, channel="demo", chat="C1") == {}


def test_the_kind_is_matched_exactly(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="D1", chat_type="Direct")
    assert not scopes[0].selects(channel="demo", chat="D1", chat_type="direct ")


def test_a_kind_rule_sits_under_a_rule_naming_the_conversation(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "D1"},
                "delivery": {"prompt": "this one"},
            },
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "every direct message"},
            },
        ],
        warn,
    )

    # Written after and still folded first: the kind is coarser than the room
    # whichever order the two appear in.
    ordered = matching_scopes(scopes, channel="demo", chat="D1", chat_type="direct")
    assert [scope.section("delivery")["prompt"] for scope in ordered] == [
        "every direct message",
        "this one",
    ]
    assert warnings == []


def test_a_kind_rule_outranks_a_rule_naming_the_platform_alone(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "direct"},
            },
            {"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}},
        ],
        warn,
    )

    settled = compose_section(
        scopes, channel="demo", chat="D1", chat_type="direct"
    )
    assert settled == {"prompt": "direct"}
    assert warnings == []


def test_a_role_in_the_same_rule_does_not_drop_the_kind(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "chat_type": "direct",
                    "role": "admin",
                },
                "delivery": {"prompt": "admins, in direct messages"},
            }
        ],
        warn,
        people={"boss": {"demo": "U_BOSS"}},
        roles={"admin": ["boss"]},
    )

    assert warnings == []
    assert scopes[0].match.chat_type == ("direct",)
    assert scopes[0].selects(channel="demo", chat="D1", chat_type="direct", user="U_BOSS")
    # This rule matches direct messages for the named administrator.
    assert not scopes[0].selects(
        channel="demo", chat="C1", chat_type="room", user="U_BOSS"
    )
    assert scopes[0].layer == (1, 0, 1, 1)


def test_the_axis_is_described(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes[0].match.describe() == "{channel: demo, chat_type: room}"


# Several kinds in one rule


def test_a_list_of_kinds_selects_a_conversation_of_any_of_them(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "direct"]},
                "delivery": {"prompt": "both kinds"},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", chat_type="room")
    assert scopes[0].selects(channel="demo", chat="D1", chat_type="direct")


def test_a_list_does_not_reach_a_kind_outside_it(warn, warnings):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="three",
            axes=frozenset({"channel", "chat", "chat_type"}),
            sections={"delivery": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct", "huddle"})},
        )
    )
    try:
        scopes = _compile(
            [
                {
                    "match": {"channel": "three", "chat_type": ["room", "direct"]},
                    "delivery": {"prompt": "two of the three"},
                }
            ],
            warn,
        )
    finally:
        caps.restore_registry(snapshot)

    assert warnings == []
    assert not scopes[0].selects(channel="three", chat="H1", chat_type="huddle")
    assert scopes[0].selects(channel="three", chat="C1", chat_type="room")


def test_one_kind_in_a_list_is_the_scalar_rule_exactly(demo_channel, warn, warnings):
    scalar, listed = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "one"},
            },
            {
                "match": {"channel": "demo", "chat_type": ["direct"]},
                "delivery": {"prompt": "two"},
            },
        ],
        warn,
    )

    # Not merely equivalent in what they select: the same compiled match, so
    # there is one behaviour rather than two spellings that could drift.
    assert warnings == []
    assert scalar.match == listed.match


def test_naming_several_kinds_does_not_change_the_layer(demo_channel, warn, warnings):
    scalar, listed = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "one"},
            },
            {
                "match": {"channel": "demo", "chat_type": ["room", "direct"]},
                "delivery": {"prompt": "both"},
            },
        ],
        warn,
    )

    assert warnings == []
    assert scalar.match.specificity == listed.match.specificity == (1, 0, 1, 0)
    # And still between the platform-only rule and the one naming a room.
    assert (
        ScopeMatch(channel="demo").specificity
        < listed.match.specificity
        < ScopeMatch(channel="demo", chat="D1").specificity
    )


def test_duplicates_are_a_set_and_are_deduped_rather_than_refused(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "room"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # It names the set {room}, which says nothing wrong and selects exactly what
    # it reads as selecting. Refusing it would drop a rule for a spelling.
    assert warnings == []
    assert scopes[0].match.chat_type == ("room",)
    assert scopes[0].match.describe() == "{channel: demo, chat_type: room}"


def test_a_list_of_kinds_is_described_as_a_list(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "direct"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Sorted, because the order of a set means nothing and two spellings of one
    # rule must not describe themselves differently.
    assert scopes[0].match.describe() == "{channel: demo, chat_type: [direct, room]}"


def _refused_kinds(refusal, match: dict) -> str:
    return refusal([{"match": match, "delivery": {"mode": ["all"]}}])


def test_an_empty_list_of_kinds_is_refused_rather_than_read_as_every_kind(demo_channel, refusal):
    # An author who wrote the key meant to name a kind.
    error = _refused_kinds(refusal, {"channel": "demo", "chat_type": []})

    assert "scopes[0].match.chat_type is an empty list. Write at least one kind of conversation" in error


def test_a_bad_entry_in_a_list_is_refused(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat_type": ["room", "thread"]})

    assert "scopes[0].match.chat_type names thread, which demo does not have. The kinds of conversation on demo are direct, room" in error


def test_a_list_of_only_bad_entries_is_refused(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat_type": ["thread", "huddle"]})

    assert "scopes[0].match.chat_type names [huddle, thread], which demo does not have" in error


def test_a_list_holding_something_that_is_not_a_word_is_refused(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat_type": ["room", 7]})

    assert "scopes[0].match.chat_type=['room', 7] is not a list of kinds of conversation" in error


def test_an_empty_word_in_a_list_is_refused(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat_type": ["room", "  "]})

    assert "scopes[0].match.chat_type contains an empty kind of conversation" in error


def test_a_mapping_where_kinds_are_wanted_is_refused(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat_type": {"room": True}})

    assert "is not a kind of conversation or a list of kinds of conversation" in error


# The vocabulary


def test_a_kind_the_platform_does_not_have_is_refused(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat_type": "thread"})

    # The alternatives are named: only the declaration knows them.
    assert "scopes[0].match.chat_type names thread, which demo does not have" in error
    assert "direct, room" in error


def test_a_platform_with_no_kinds_is_told_so_rather_than_told_it_typed_one(
    typeless_channel, warn, warnings
):
    _compile(
        [
            {
                "match": {"channel": "flat", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Two different fixes, so two different sentences. Here there is no word
    # that would have worked, and listing an empty vocabulary would read as one.
    assert any(
        "does not say what kind of conversation" in line for line in warnings
    )
    assert not any("The kinds there are" in line for line in warnings)


def test_an_axis_without_a_declared_vocabulary_is_taken_as_written(warn, warnings):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="open",
            axes=frozenset({"channel", "chat_type"}),
            sections={"delivery": None},
        )
    )
    try:
        scopes = _compile(
            [
                {
                    "match": {"channel": "open", "chat_type": "whatever"},
                    "delivery": {"mode": ["all"]},
                }
            ],
            warn,
        )
    finally:
        caps.restore_registry(snapshot)

    assert warnings == []
    assert scopes[0].selects(channel="open", chat=None, chat_type="whatever")


# chat and chat_type in one rule


def test_chat_and_chat_type_together_are_refused_by_name(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat": "D1", "chat_type": "direct"})

    assert "scopes[0].match names both chat=D1 and chat_type=direct. Write only one of them" in error


def test_the_refusal_does_not_depend_on_the_two_disagreeing(demo_channel, refusal):
    error = refusal(
        [
            {
                "match": {"channel": "demo", "chat": "D1", "chat_type": "room"},
                "delivery": {"mode": ["all"]},
            },
            {
                "match": {"channel": "demo", "chat": "D1", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            },
        ]
    )

    assert "scopes[0].match names both" in error
    assert "scopes[1].match names both" in error


def test_a_list_of_kinds_beside_a_chat_is_refused_the_same_way(demo_channel, refusal):
    error = _refused_kinds(refusal, {"channel": "demo", "chat": "D1", "chat_type": ["room", "direct"]})

    assert "chat=D1 and chat_type=[direct, room]" in error


def test_a_kind_still_needs_the_channel_it_belongs_to(refusal):
    error = _refused_kinds(refusal, {"chat_type": "direct"})

    assert "scopes[0].match.chat_type=direct names a conversation but no channel" in error
    assert "Add channel to the match block" in error


def test_a_match_built_in_code_with_both_takes_the_finer_grain():
    both = ScopeMatch(channel="demo", chat="D1", chat_type=("direct",))

    assert both.specificity == (1, 0, 2, 0)
