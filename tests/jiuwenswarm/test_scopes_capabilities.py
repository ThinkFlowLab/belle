"""Capability declarations and warnings for unsupported scope settings."""

from typing import Any

import pytest

from belle_agent.scopes import (
    AXIS_CHAT_TYPE,
    MID_TURN_CANCEL,
    MID_TURN_DEFAULT,
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    MID_TURN_VALUES,
    SESSION_CHANNEL,
    SESSION_DEFAULT,
    SESSION_THREAD,
    SESSION_VALUES,
    ChannelCapabilities,
    channel_capabilities,
    compile_scopes,
    compose_section,
    known_channels,
    scoped_chats,
)
from belle_agent.scopes import capabilities as caps
from belle_agent.integrations.jiuwenswarm import config as belle_config
from belle_agent.integrations.jiuwenswarm.scopes import register_host_channels


@pytest.fixture(autouse=True)
def host_channels(registry):
    register_host_channels()


# The declaration itself


def test_a_declaration_cannot_be_edited_through_the_registry(registry):
    registry(
        ChannelCapabilities(
            channel="demo", axes=frozenset({"channel"}), sections={"delivery": None}
        )
    )
    declared = channel_capabilities("demo")

    with pytest.raises(TypeError):
        declared.sections["delivery"] = frozenset()  # type: ignore[index]


def test_no_declaration_means_not_opted_in():
    assert channel_capabilities("a-channel-nobody-wrote") is None


def test_the_pseudo_channels_ship_declared():
    # Cron and the heartbeat drive real turns and have no connector to hang a
    # declaration on, so theirs live in the registry module itself.
    assert "__cron__" in known_channels()
    assert "__heartbeat__" in known_channels()


def test_cron_is_declared_unattended():
    # Nothing reads this yet. It is recorded now because the answer is known
    # now, and a half-filled table is how a capability question gets asked twice.
    assert channel_capabilities("__cron__").attended is False


def test_web_is_one_conversation_and_says_so():
    assert channel_capabilities("web").axes == frozenset({"channel"})


# 1 -- unknown channel


@pytest.mark.parametrize("section", [{"delivery": {"mode": ["all"]}}, {"permissions": {"tools": {"bash": "deny"}}}])
def test_a_scope_naming_a_channel_that_has_not_declared_is_refused(refusal, section):
    error = refusal([{"match": {"channel": "carrier-pigeon"}, **section}])

    assert "scopes[0].match.channel=carrier-pigeon is not a platform or a connector" in error
    assert "__cron__" in error and "web" in error


def test_slack_scopes_know_the_host_channels(monkeypatch):
    # The gateway compiles scopes without the permission module that also registers them.
    caps.restore_registry({})
    belle = {"scopes": [{"match": {"channel": "web"}, "permissions": {"tools": {"bash": "deny"}}}]}
    monkeypatch.setattr("jiuwenswarm.extensions.sdk.host_config.get_config", lambda: {"channels": {}})
    monkeypatch.setattr(belle_config, "load_belle_config", lambda: belle)
    monkeypatch.setattr(belle_config, "slack_tool_config", lambda data, loaded: {})

    assert [scope.match.channel for scope in belle_config.load_slack_scopes()] == ["web"]


# 2 -- an axis the channel cannot populate


def test_a_chat_axis_on_a_channel_with_one_conversation_warns_it_will_never_match(
    warn, warnings
):
    compile_scopes(
        [{"match": {"channel": "web", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    assert any(
        "web does not identify a conversation" in line and "never match" in line
        for line in warnings
    ), warnings


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        ({"chat": "C1"}, "web does not identify a conversation"),
        ({"user": ["U1"]}, "web does not identify a sender"),
        ({"not": {"user": ["U1"]}}, "nobody is excluded"),
    ],
)
def test_a_permission_rule_on_an_axis_the_channel_cannot_fill_is_refused(refusal, match, expected):
    error = refusal([{"match": {"channel": "web", **match}, "permissions": {"tools": {"bash": "deny"}}}])

    assert expected in error


def test_matching_on_channel_alone_where_that_is_all_there_is_does_not_warn(warn, warnings):
    compile_scopes([{"match": {"channel": "web"}, "delivery": {"mode": ["all"]}}], warn=warn)

    assert warnings == []


def test_a_channel_that_declares_the_axis_is_not_warned_about(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    assert warnings == []


# 3 -- a section or key the channel ignores


def test_delivery_on_cron_warns_that_it_has_no_effect(warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "__cron__"}, "delivery": {"mode": ["all"]}}], warn=warn
    )

    assert any(
        "has no effect" in line and "__cron__ does not read the delivery section" in line
        for line in warnings
    ), warnings
    assert scopes == ()


def test_a_section_a_channel_declares_and_a_key_it_does_not_are_different_lines(
    registry, warn, warnings
):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"}), "agent": frozenset({"model_name"})},
        )
    )
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["all"], "model_name": "m"},
            }
        ],
        warn=warn,
    )

    assert any(
        "delivery.model_name has no effect" in line and "demo ignores it" in line
        for line in warnings
    ), warnings
    assert not any("does not read the delivery section" in line for line in warnings)
    assert compose_section(scopes, channel="demo") == {"mode": frozenset({"all"})}


def test_a_key_the_channel_ignores_warns_and_is_dropped(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"], "prompt": "hi"}}],
        warn=warn,
    )

    assert any(
        "delivery.prompt has no effect" in line
        and "demo ignores it" in line
        and "it reads are mode" in line
        for line in warnings
    ), warnings
    assert compose_section(scopes, channel="demo") == {"mode": frozenset({"all"})}


def test_an_append_key_is_checked_against_the_key_it_appends_to(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"prompt"})},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"prompt_append": "hi"}}], warn=warn
    )

    # prompt_append is not itself a declared key and must not have to be:
    # declaring prompt is declaring the thing being appended to.
    assert warnings == []


def test_a_channel_declaring_all_keys_never_warns_about_one(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo", axes=frozenset({"channel"}), sections={"delivery": None}
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"anything_at_all": "x"}}], warn=warn
    )

    assert warnings == []


def test_a_scope_with_no_channel_is_not_checked_against_any_declaration(warn, warnings):
    # There is no channel to ask. Guessing one would be the wrong answer for
    # every other channel the scope also applies to.
    compile_scopes([{"match": {}, "delivery": {"whatever": "x"}}], warn=warn)

    assert warnings == []


# 4 -- a key the connector block already sets


def _declare_layer0_channel(
    registry, *, axes=("channel",), key="mode", config_key="group_chat_mode"
):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset(axes),
            sections={"delivery": None},
            layer0_keys={"delivery": {key: config_key}},
        )
    )


def test_a_key_the_connector_block_already_sets_warns_about_two_homes(
    registry, warn, warnings
):
    _declare_layer0_channel(registry, axes=("channel", "chat"))
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert any(
        "channels.demo.group_chat_mode both set this" in line
        and "now governs nothing" in line
        for line in warnings
    ), warnings


def test_a_scope_naming_a_kind_is_not_redundant_with_the_connector_block(
    registry, warn, warnings
):
    _declare_layer0_channel(registry, axes=("channel", "chat", "chat_type"))
    compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "channel"},
                "delivery": {"mode": ["all"]},
            }
        ],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert not any("now governs nothing" in line for line in warnings), warnings


def test_a_scope_naming_a_conversation_is_not_redundant_with_the_connector_block(
    registry, warn, warnings
):
    _declare_layer0_channel(registry, axes=("channel", "chat"))
    compile_scopes(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert warnings == []


def test_the_key_still_applies_because_layering_is_the_feature(registry, warn):
    _declare_layer0_channel(registry)
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert compose_section(scopes, channel="demo") == {"mode": frozenset({"all"})}


@pytest.mark.parametrize("empty", [None, "", [], {}])
def test_a_connector_key_left_empty_is_not_a_second_home(registry, warn, warnings, empty):
    _declare_layer0_channel(registry, key="prompt", config_key="carrier_pigeon_prompt")
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"prompt": "hi"}}],
        channels_config={"demo": {"carrier_pigeon_prompt": empty}},
        warn=warn,
    )

    assert warnings == []


def test_a_key_with_no_connector_counterpart_is_not_warned_about(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo", axes=frozenset({"channel"}), sections={"delivery": None}
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert warnings == []


# A connector's own value checks, kept in the connector


def test_a_declared_validator_can_reject_a_value_without_the_loader_knowing_why(
    registry, refusal
):
    def only_known_models(value: Any) -> "str | None":
        if value in ("good-model",):
            return None
        return f"={value!r} is not a configured model"

    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None, "agent": None},
            validators={"agent.model_name": only_known_models},
        )
    )
    error = refusal(
        [
            {"match": {"channel": "demo"}, "agent": {"model_name": "good-model"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "agent": {"model_name": "typo"},
                "delivery": {"prompt": "kept"},
            },
        ]
    )

    assert "scopes[1].agent.model_name='typo' is not a configured model" in error
    assert "scopes[0]" not in error


def test_a_validator_is_keyed_by_section_so_the_same_key_can_differ(
    registry, warn, warnings
):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": None, "agent": None},
            validators={"agent.model_name": lambda value: "is refused here"},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"model_name": "anything"}}],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo") == {"model_name": "anything"}
    assert warnings == []


# A connector's second hook: what a value no request can reach


@pytest.fixture
def caution_calls() -> list[tuple[Any, Any]]:
    """Everything a caution was handed, in the order it was handed it."""
    return []


@pytest.fixture
def caution_channel(registry, caution_calls):
    """A platform with two kinds of conversation and one key worth a caution.

    The caution stands in for the shape the hook exists for: a value that is
    spelled correctly and is a thing the channel implements, addressed at a
    rule no request will arrive through. What that is on a real connector is
    the connector's business; what is generic is that a reason warns and the
    value stays.
    """

    def read_only_in_a_direct_message(value: Any, match: Any) -> "str | None":
        caution_calls.append((value, match))
        kinds = match.chat_type
        if not kinds or "direct" in kinds:
            return None
        return f" is read in a direct message, and this rule names {', '.join(kinds)}"

    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "chat_type"}),
            sections={"delivery": None, "agent": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
            cautions={"delivery.prompt": read_only_in_a_direct_message},
        )
    )
    return None


def test_a_caution_warns_and_keeps_the_value(caution_channel, warn, warnings):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"prompt": "hello tab", "mode": ["all"]},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1", chat_type="room") == {
        "prompt": "hello tab",
        "mode": frozenset({"all"}),
    }
    assert any("is read in a direct message" in line for line in warnings), warnings
    assert any("delivery.prompt" in line for line in warnings), warnings


def test_a_caution_is_handed_the_match_as_well_as_the_value(
    caution_channel, caution_calls, warn
):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert len(caution_calls) == 1
    value, match = caution_calls[0]
    assert value == "hello tab"
    assert match is scopes[0].match
    assert match.chat_type == ("room",)


def test_a_caution_that_returns_nothing_says_nothing(
    caution_channel, warn, warnings
):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="D1", chat_type="direct") == {
        "prompt": "hello tab"
    }
    assert warnings == []


def test_a_key_with_no_caution_registered_is_untouched(
    registry, caution_calls, warn, warnings
):
    registry(
        ChannelCapabilities(
            channel="quiet",
            axes=frozenset({"channel", "chat_type"}),
            sections={"delivery": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
        )
    )
    declared = channel_capabilities("quiet")

    scopes = compile_scopes(
        [
            {
                "match": {"channel": "quiet", "chat_type": "room"},
                "delivery": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert declared.caution("delivery", "prompt") is None
    assert compose_section(scopes, channel="quiet", chat="C1", chat_type="room") == {
        "prompt": "hello tab"
    }
    assert caution_calls == []
    assert warnings == []


def test_a_caution_is_keyed_by_section_like_a_validator(
    caution_channel, caution_calls, warn, warnings
):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "agent": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat_type="room", section="agent") == {
        "prompt": "hello tab"
    }
    assert caution_calls == []
    assert warnings == []


def test_a_caution_is_not_consulted_for_a_value_the_validator_refused(
    registry, caution_calls, refusal, warnings
):

    def refuse_everything(value: Any) -> "str | None":
        return "=is refused here"

    def never_reached(value: Any, match: Any) -> "str | None":
        caution_calls.append((value, match))
        return " should not have been asked"

    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat_type"}),
            sections={"delivery": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
            validators={"delivery.prompt": refuse_everything},
            cautions={"delivery.prompt": never_reached},
        )
    )
    error = refusal(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"prompt": "hello tab", "mode": ["all"]},
            }
        ]
    )

    assert "scopes[0].delivery.prompt=is refused here" in error
    assert caution_calls == []
    assert not any("should not have been asked" in line for line in warnings), warnings


# Delivery words owned by the scope schema.

_DELIVERY_WORDS = {
    "mid_turn": {
        "values": MID_TURN_VALUES,
        "default": MID_TURN_DEFAULT,
        "expected_default": MID_TURN_QUEUE,
        "base": MID_TURN_STEER,
        "later": MID_TURN_CANCEL,
        "unknown": "wait",
        "excluded": "follow_up",
    },
    "session": {
        "values": SESSION_VALUES,
        "default": SESSION_DEFAULT,
        "expected_default": SESSION_THREAD,
        "base": SESSION_CHANNEL,
        "later": SESSION_THREAD,
        "unknown": "workspace",
        "excluded": "user",
    },
}


@pytest.fixture(params=_DELIVERY_WORDS)
def delivery_word(request, registry):
    key = request.param
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": frozenset({"mode", key})},
        )
    )
    return key, _DELIVERY_WORDS[key]


def test_delivery_words_settle(delivery_word, warn, warnings):
    key, case = delivery_word
    for value in case["values"]:
        scopes = compile_scopes(
            [{"match": {"channel": "demo"}, "delivery": {key: value}}], warn=warn
        )
        assert compose_section(scopes, channel="demo") == {key: value}
    assert warnings == []


def test_first_delivery_word_is_the_default(delivery_word):
    _, case = delivery_word
    assert case["values"][0] == case["default"] == case["expected_default"]


def test_absent_delivery_word_stays_absent(delivery_word, warn):
    key, _ = delivery_word
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}], warn=warn
    )
    assert key not in compose_section(scopes, channel="demo")


def test_an_unknown_delivery_word_is_refused(delivery_word, refusal):
    key, case = delivery_word
    error = refusal(
        [
            {"match": {"channel": "demo"}, "delivery": {key: case["base"]}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {key: case["unknown"]},
            },
        ]
    )
    listed = ", ".join(case["values"])
    assert f"scopes[1].delivery.{key}={case['unknown']!r} is not one of {listed}" in error


def test_a_delivery_word_that_is_not_a_string_is_refused(delivery_word, refusal):
    key, case = delivery_word
    error = refusal([{"match": {"channel": "demo"}, "delivery": {key: [case["base"]]}}])
    listed = ", ".join(case["values"])
    assert f"scopes[0].delivery.{key}={[case['base']]!r} is not one of {listed}" in error


def test_delivery_word_normalizes_case_and_space(delivery_word, warn, warnings):
    key, case = delivery_word
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {key: f"  {case['base'].title()} "},
            }
        ],
        warn=warn,
    )
    assert compose_section(scopes, channel="demo") == {key: case["base"]}
    assert warnings == []


def test_an_appended_delivery_word_is_refused(delivery_word, refusal):
    key, case = delivery_word
    error = refusal(
        [
            {"match": {"channel": "demo"}, "delivery": {key: case["base"]}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {f"{key}_append": case["later"]},
            },
        ]
    )
    assert f"scopes[1].delivery.{key}_append has nothing to append to. Set {key} to one of" in error


def test_later_layer_replaces_delivery_word(delivery_word, warn):
    key, case = delivery_word
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {key: case["base"]}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {key: case["later"]},
            },
        ],
        warn=warn,
    )
    assert compose_section(scopes, channel="demo") == {key: case["base"]}
    assert compose_section(scopes, channel="demo", chat="C1") == {key: case["later"]}


@pytest.mark.parametrize(
    "key, value",
    [
        ("mid_turn", MID_TURN_STEER),
        ("session", SESSION_CHANNEL),
    ],
)
def test_undeclared_delivery_word_is_dropped(registry, warn, warnings, key, value):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {key: value}}], warn=warn
    )
    assert compose_section(scopes, channel="demo") == {}
    assert any(f"delivery.{key} has no effect" in line for line in warnings), warnings


def test_excluded_delivery_word_is_refused(delivery_word, refusal):
    key, case = delivery_word
    error = refusal([{"match": {"channel": "demo"}, "delivery": {key: case["excluded"]}}])
    assert f"scopes[0].delivery.{key}={case['excluded']!r} is not one of" in error


# A chat named without its channel


def test_a_chat_without_a_channel_is_refused_rather_than_left_unvalidated(refusal):
    error = refusal([{"match": {"chat": "C-X"}, "delivery": {"mode": ["not_a_trigger"]}}])

    assert "scopes[0].match.chat=C-X names a conversation but no channel. Add channel to the match block" in error


def test_naming_the_channel_alongside_the_chat_is_accepted(warn, warnings, registry):
    """The fix refuses one shape, not the axis."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo", "chat": "C-OK"}, "delivery": {"mode": ["mention"]}}],
        warn=warn,
    )

    assert len(scopes) == 1
    assert scoped_chats(scopes, channel="demo") == ("C-OK",)


def test_a_scope_with_neither_axis_is_untouched(warn, warnings):
    scopes = compile_scopes([{"match": {}, "delivery": {"mode": ["mention"]}}], warn=warn)

    assert len(scopes) == 1
    assert not any("without saying which channel" in line for line in warnings), warnings


# 2 -- the identity axis, which fails in both directions


def test_a_user_axis_on_a_channel_with_no_sender_warns_that_it_will_never_match(
    warn, warnings
):
    # web is one conversation with the bot and names nobody in it, which is why
    # it declares neither the chat axis nor any identity_keys.
    compile_scopes(
        [{"match": {"channel": "web", "user": ["U1"]}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    assert any(
        "web does not identify a sender" in line and "never match" in line
        for line in warnings
    ), warnings


def test_a_not_on_a_channel_with_no_sender_warns_that_it_exempts_nobody(warn, warnings):
    compile_scopes(
        [
            {
                "match": {"channel": "web", "not": {"user": ["U1"]}},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn=warn,
    )

    assert any(
        "nobody is excluded" in line and "written to exempt" in line
        for line in warnings
    ), warnings


def test_a_channel_that_declares_the_user_axis_is_not_warned_about(
    registry, warn, warnings
):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "user"}),
            sections={"delivery": None},
            identity_keys=("user",),
        )
    )
    compile_scopes(
        [
            {
                "match": {"channel": "demo", "user": ["U1"], "not": {"user": ["U2"]}},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn=warn,
    )

    assert warnings == []


def test_a_scope_naming_a_sender_still_applies_where_the_axis_is_unpopulated(
    warn, warnings
):
    scopes = compile_scopes(
        [
            {"match": {"channel": "web", "user": ["U1"]}, "delivery": {"prompt": "named"}},
            {
                "match": {"channel": "web", "not": {"user": ["U1"]}},
                "delivery": {"prompt": "restricted"},
            },
        ],
        warn=warn,
    )

    assert len(scopes) == 2
    assert compose_section(scopes, channel="web") == {"prompt": "restricted"}


# axes and identity_keys are two statements about one fact


def test_declaring_the_user_axis_without_identity_keys_is_reported(registry, caplog):
    with caplog.at_level("WARNING", logger=caps.logger.name):
        caps.logger.propagate = True
        try:
            registry(
                ChannelCapabilities(
                    channel="demo",
                    axes=frozenset({"channel", "user"}),
                    sections={"delivery": None},
                )
            )
        finally:
            caps.logger.propagate = False

    assert any("names no identity_keys" in record.message for record in caplog.records)


def test_naming_identity_keys_without_the_axis_is_reported(registry, caplog):
    with caplog.at_level("WARNING", logger=caps.logger.name):
        caps.logger.propagate = True
        try:
            registry(
                ChannelCapabilities(
                    channel="demo",
                    axes=frozenset({"channel"}),
                    sections={"delivery": None},
                    identity_keys=("open_id", "union_id"),
                )
            )
        finally:
            caps.logger.propagate = False

    assert any("does not declare the user axis" in record.message for record in caplog.records)


def test_the_shipped_pseudo_channels_agree_with_themselves(registry, caplog):
    with caplog.at_level("WARNING", logger=caps.logger.name):
        caps.logger.propagate = True
        try:
            for name in ("web", "__cron__", "__heartbeat__"):
                declared = channel_capabilities(name)
                assert declared is not None
                registry(declared)
        finally:
            caps.logger.propagate = False

    assert caplog.records == []
