"""Test match-axis restrictions on scoped settings."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from belle_agent.scopes import (
    ANY_KEY,
    AXIS_CHANNEL,
    AXIS_CHAT,
    AXIS_CHAT_TYPE,
    AXIS_ROLE,
    AXIS_USER,
    AXIS_WORKSPACE,
    MATCH_AXIS_RESTRICTIONS,
    PER_CONVERSATION,
    SECTION_AGENT,
    SECTION_CLICKS,
    SECTION_PERMISSIONS,
    SUPPORTED_AXES,
    AxisRestriction,
    ChannelCapabilities,
    compile_scopes,
    register_channel,
    restriction_in,
)
from belle_agent.scopes import capabilities as caps

_CAPABILITIES_LOGGER = "belle_agent.scopes.capabilities"
_SCHEMA_LOGGER = "belle_agent.scopes.schema"

PEOPLE = {"alice": {"restricted": "U_ALICE"}}
ROLES = {"operator": ["alice"]}


@contextmanager
def _captured(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Capture a logger that does not propagate."""
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


@pytest.fixture(autouse=True)
def restricted():
    """Register a synthetic channel for axis tests."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="restricted",
            # Populate workspace to test key restrictions, not missing axis data.
            axes=frozenset(
                {
                    AXIS_CHANNEL,
                    AXIS_WORKSPACE,
                    AXIS_CHAT,
                    AXIS_CHAT_TYPE,
                    AXIS_USER,
                }
            ),
            sections={
                "delivery": None,
                SECTION_AGENT: frozenset({"gated", "ungated"}),
                "permissions": None,
                SECTION_CLICKS: None,
            },
            identity_keys=("user",),
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
            axis_restrictions={
                f"{SECTION_AGENT}.gated": AxisRestriction.only_on(
                    AXIS_CHANNEL,
                    because="Who is asking is already settled per request",
                ),
            },
        )
    )
    yield snapshot
    caps.restore_registry(snapshot)


def _compile(entry: dict[str, Any], warn, **kw: Any):
    return compile_scopes([entry], people=PEOPLE, roles=ROLES, warn=warn, **kw)


def _refused(entry: dict[str, Any], refusal) -> str:
    return refusal([entry], people=PEOPLE, roles=ROLES)


# Accepting a legal rule, refusing an illegal one


def test_a_restricted_key_is_kept_on_the_axis_it_permits(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "restricted"}, "agent": {"gated": "on"}}, warn
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_AGENT) == {"gated": "on"}
    assert not [line for line in warnings if "cannot be in a rule" in line]


@pytest.mark.parametrize(
    ("match", "axis"),
    [
        ({"channel": "restricted", "chat": "C1"}, AXIS_CHAT),
        ({"channel": "restricted", "chat_type": "room"}, AXIS_CHAT_TYPE),
        ({"channel": "restricted", "user": ["U_ALICE"]}, AXIS_USER),
        ({"channel": "restricted", "not": {"user": ["U_ALICE"]}}, AXIS_USER),
    ],
)
def test_a_restricted_key_is_refused_on_an_axis_it_bars(match, axis, refusal):
    error = _refused({"match": match, "agent": {"gated": "on"}}, refusal)

    assert f"scopes[0].agent.gated cannot be in a rule that matches on {axis}" in error


def test_the_refusal_names_the_rule_the_key_and_the_permitted_axes(refusal):
    error = _refused(
        {
            "match": {"channel": "restricted", "chat": "C1"},
            "agent": {"gated": "on", "ungated": "fine"},
        },
        refusal,
    )

    (line,) = [entry for entry in error.splitlines() if "cannot be in a rule" in entry]
    # The rule, by the position an operator can count to in their file.
    assert line.startswith("scopes[0].agent.gated ")
    # The axis, by name.
    assert "matches on chat" in line
    # What to write instead, so the refusal is actionable without the design.
    assert "The permitted axes are channel" in line
    # And the reason the declaration gave, kept rather than composed here.
    assert "Who is asking is already settled per request" in line


def test_the_refusal_names_the_spelling_the_author_wrote(refusal):
    error = _refused(
        {"match": {"channel": "restricted", "role": "operator"}, "agent": {"gated": "x"}},
        refusal,
    )

    assert "matches on role" in error
    assert "matches on user" not in error


def test_every_barred_axis_is_named_rather_than_the_first(refusal):
    error = _refused(
        {
            "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
            "agent": {"gated": "on"},
        },
        refusal,
    )

    assert "matches on chat, user" in error


def test_an_append_is_restricted_exactly_as_the_key_it_appends_to(refusal):
    error = _refused(
        {"match": {"channel": "restricted", "chat": "C1"}, "agent": {"gated_append": "x"}},
        refusal,
    )

    assert "scopes[0].agent.gated_append cannot be in a rule that matches on chat" in error


def test_an_unrestricted_key_is_addressable_on_every_axis(warn, warnings):
    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
            "agent": {"ungated": "x"},
            "delivery": {"prompt": "Be terse."},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_AGENT) == {"ungated": "x"}
    assert scopes[0].section("delivery") == {"prompt": "Be terse."}
    assert not [line for line in warnings if "cannot be in a rule" in line]


# permissions: barred because its consumer is handed no sender


def test_permissions_is_kept_on_a_rule_addressing_the_conversation(warn, warnings):
    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1"},
            "permissions": {"tools": {"Bash": "deny"}},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_PERMISSIONS) == {"tools": {"Bash": "deny"}}
    assert not [line for line in warnings if "cannot be in a rule" in line]


@pytest.mark.parametrize(
    ("match", "axis"),
    [
        ({"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]}, AXIS_USER),
        ({"channel": "restricted", "chat": "C1", "role": "operator"}, AXIS_ROLE),
        (
            {"channel": "restricted", "chat": "C1", "not": {"user": ["U_ALICE"]}},
            AXIS_USER,
        ),
    ],
)
def test_permissions_is_refused_on_an_identity_axis(match, axis, refusal):
    error = _refused({"match": match, "permissions": {"tools": {"Bash": "deny"}}}, refusal)

    (line,) = [entry for entry in error.splitlines() if "cannot be in a rule" in entry]
    assert line.startswith(f"scopes[0].permissions cannot be in a rule that matches on {axis}")
    assert "is given the channel and the chat alone" in line
    assert "The permitted axes are channel, chat" in line


# The existing clicks bar, as one rule rather than two


def test_the_clicks_bar_is_a_row_in_the_shared_table(refusal):
    restriction = restriction_in(MATCH_AXIS_RESTRICTIONS, SECTION_CLICKS, "approve")

    assert restriction is not None
    assert restriction.allowed == frozenset({AXIS_CHANNEL, AXIS_CHAT})

    error = _refused(
        {
            "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
            "clicks": {"approve": {"user": ["U_ALICE"]}, "stop": {"user": ["U_ALICE"]}},
        },
        refusal,
    )

    refusals = [line for line in error.splitlines() if "cannot be in a rule" in line]
    # One line for the section, not one per gesture.
    assert len(refusals) == 1
    assert refusals[0].startswith("scopes[0].clicks cannot be in a rule that matches on user")
    # The row's own reason holds the sentence, so an operator reads the same
    # wording whichever mechanism refused it.
    assert "has no effect on a rule that also names a sender" in refusals[0]


# Where the workspace falls


@pytest.mark.parametrize(
    ("section", "body"),
    [
        (SECTION_PERMISSIONS, {"tools": {"Bash": "deny"}}),
        (SECTION_CLICKS, {"approve": {"user": ["U_ALICE"]}}),
    ],
)
def test_a_key_addressable_on_the_conversation_is_not_satisfied_by_the_workspace(section, body, refusal):
    error = _refused({"match": {"channel": "restricted", "workspace": "T_ACME"}, section: body}, refusal)

    (line,) = [entry for entry in error.splitlines() if "cannot be in a rule" in entry]
    assert line.startswith(f"scopes[0].{section} cannot be in a rule that matches on {AXIS_WORKSPACE}")
    assert "The permitted axes are channel, chat" in line


def test_the_workspace_was_barred_by_the_allow_list_and_by_nobody_editing_it():
    for section in (SECTION_CLICKS, SECTION_PERMISSIONS):
        restriction = restriction_in(MATCH_AXIS_RESTRICTIONS, section, ANY_KEY)
        assert restriction is not None
        assert restriction.allowed == frozenset({AXIS_CHANNEL, AXIS_CHAT})
        assert not restriction.permits(AXIS_WORKSPACE)
        assert restriction.refuses({AXIS_CHANNEL, AXIS_WORKSPACE}) == (
            AXIS_WORKSPACE,
        )


def test_an_unrestricted_key_may_still_be_addressed_on_the_workspace(warn, warnings):
    scopes = _compile(
        {
            "match": {"channel": "restricted", "workspace": "T_ACME"},
            "delivery": {"prompt": "the ACME workspace"},
            SECTION_AGENT: {"ungated": "fine"},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_AGENT) == {"ungated": "fine"}
    assert not [line for line in warnings if "cannot be in a rule" in line]


def test_a_clicks_rule_on_the_conversation_alone_is_untouched(warn, warnings):
    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1"},
            "clicks": {"approve": {"user": ["U_ALICE"]}},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_CLICKS)["approve"].users == ("U_ALICE",)
    assert not [line for line in warnings if "cannot be in a rule" in line]


# Two tables, intersected


def test_a_channel_cannot_widen_what_the_schema_bars(refusal):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="overreaching",
            axes=frozenset({AXIS_CHANNEL, AXIS_CHAT, AXIS_USER}),
            sections={SECTION_CLICKS: None},
            identity_keys=("user",),
            axis_restrictions={
                f"{SECTION_CLICKS}.{ANY_KEY}": AxisRestriction.only_on(
                    AXIS_CHANNEL, AXIS_CHAT, AXIS_USER
                )
            },
        )
    )
    try:
        error = _refused(
            {
                "match": {"channel": "overreaching", "user": ["U_ALICE"]},
                "clicks": {"approve": {"user": ["U_ALICE"]}},
            },
            refusal,
        )
    finally:
        caps.restore_registry(snapshot)

    (line,) = [entry for entry in error.splitlines() if "cannot be in a rule" in entry]
    assert line.startswith("scopes[0].clicks cannot be in a rule that matches on user")
    assert "has no effect on a rule that also names a sender" in line


def test_a_channel_may_narrow_what_the_schema_leaves_open(refusal):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="narrowing",
            axes=frozenset({AXIS_CHANNEL, AXIS_CHAT}),
            sections={SECTION_CLICKS: None},
            axis_restrictions={
                f"{SECTION_CLICKS}.{ANY_KEY}": AxisRestriction.only_on(AXIS_CHANNEL)
            },
        )
    )
    try:
        error = _refused(
            {
                "match": {"channel": "narrowing", "chat": "C1"},
                "clicks": {"approve": {"user": ["U_ALICE"]}},
            },
            refusal,
        )
    finally:
        caps.restore_registry(snapshot)

    assert "scopes[0].clicks cannot be in a rule that matches on chat" in error


def test_a_section_wide_entry_and_a_per_key_one_both_apply():
    table = {
        f"{SECTION_AGENT}.{ANY_KEY}": AxisRestriction.only_on(AXIS_CHANNEL, AXIS_CHAT),
        f"{SECTION_AGENT}.gated": AxisRestriction.only_on(AXIS_CHANNEL, AXIS_USER),
    }

    settled = restriction_in(table, SECTION_AGENT, "gated")

    assert settled is not None
    assert settled.allowed == frozenset({AXIS_CHANNEL})


# Fail closed


def test_an_allow_list_bars_an_axis_nobody_has_permitted():
    restriction = AxisRestriction.only_on(AXIS_CHANNEL)

    for axis in SUPPORTED_AXES:
        assert restriction.permits(axis) is (axis == AXIS_CHANNEL)
    assert restriction.permits("an_axis_invented_next_year") is False


def test_an_empty_allow_list_bars_every_addressed_rule(refusal):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="sealed",
            axes=frozenset({AXIS_CHANNEL}),
            sections={SECTION_AGENT: frozenset({"gated"})},
            axis_restrictions={f"{SECTION_AGENT}.gated": AxisRestriction()},
        )
    )
    try:
        error = _refused({"match": {"channel": "sealed"}, "agent": {"gated": "x"}}, refusal)
    finally:
        caps.restore_registry(snapshot)

    assert "The permitted axes are nothing" in error


@pytest.mark.parametrize("axis", [5, "", "   ", None])
def test_a_malformed_declaration_refuses_to_be_built(axis):
    with pytest.raises(ValueError):
        AxisRestriction.only_on(axis)  # type: ignore[arg-type]


def test_an_axis_name_that_is_not_an_axis_is_reported_and_fails_closed(refusal):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="typoed",
            axes=frozenset({AXIS_CHANNEL}),
            sections={SECTION_AGENT: frozenset({"gated"})},
            axis_restrictions={
                f"{SECTION_AGENT}.gated": AxisRestriction.only_on("chanel")
            },
        )
    )
    try:
        with _captured(_SCHEMA_LOGGER) as records:
            error = _refused({"match": {"channel": "typoed"}, "agent": {"gated": "x"}}, refusal)
    finally:
        caps.restore_registry(snapshot)

    assert "cannot be in a rule that matches on channel" in error
    assert [
        record
        for record in records
        if "permits chanel, which is not an axis" in record.getMessage()
    ]


def test_a_restriction_on_a_key_the_channel_never_reads_is_reported():
    snapshot = caps.snapshot_registry()
    try:
        with _captured(_CAPABILITIES_LOGGER) as records:
            register_channel(
                ChannelCapabilities(
                    channel="dangling",
                    axes=frozenset({AXIS_CHANNEL}),
                    sections={SECTION_AGENT: frozenset({"declared"})},
                    axis_restrictions={
                        f"{SECTION_AGENT}.undeclared": AxisRestriction.only_on(
                            AXIS_CHANNEL
                        )
                    },
                )
            )
    finally:
        caps.restore_registry(snapshot)

    assert [
        record
        for record in records
        if "does not name undeclared among the agent keys it acts on"
        in record.getMessage()
    ]


# The default is what shipped


def test_the_shared_table_holds_exactly_the_rows_that_were_argued_for():
    assert set(MATCH_AXIS_RESTRICTIONS) == {
        f"{SECTION_CLICKS}.{ANY_KEY}",
        f"{SECTION_PERMISSIONS}.{ANY_KEY}",
    }


def test_no_key_that_already_existed_has_gained_a_restriction(restricted):
    already_writable = {
        "delivery.mode",
        "delivery.prompt",
        "delivery.mid_turn",
        "agent.model_name",
        "permissions.tools",
    }

    for name, declared in restricted.items():
        for entry in declared.axis_restrictions:
            assert entry not in already_writable, f"{name}: {entry}"


def test_a_config_using_every_section_is_unchanged(warn, warnings):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "restricted"},
                "delivery": {"mode": ["mention"]},
                "agent": {"ungated": "m"},
            },
            {
                "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
                "agent": {"ungated": "theirs"},
            },
            {
                # On the conversation, because that is the whole of what the
                # permission hook is given to settle it with.
                "match": {"channel": "restricted", "chat": "C1"},
                "permissions": {"tools": {"Bash": "ask"}},
            },
            {
                "match": {"channel": "restricted", "chat": "C1"},
                "clicks": {"stop": {"role": "operator"}},
            },
        ],
        people=PEOPLE,
        roles=ROLES,
        warn=warn,
    )

    assert len(scopes) == 4
    assert not [line for line in warnings if "cannot be in a rule" in line]


def test_a_per_conversation_key_is_kept_on_the_conversation_and_refused_on_a_sender():
    assert PER_CONVERSATION.refuses([AXIS_CHANNEL, AXIS_CHAT, AXIS_CHAT_TYPE]) == ()
    assert PER_CONVERSATION.refuses([AXIS_USER, AXIS_ROLE, AXIS_WORKSPACE]) == (AXIS_ROLE, AXIS_USER, AXIS_WORKSPACE)


def test_the_platforms_restrict_their_per_conversation_keys_with_the_shared_rule():
    from belle_agent.platforms.loopback.platform import LOOPBACK_CAPABILITIES
    from belle_agent.platforms.mattermost.capabilities import MATTERMOST_CAPABILITIES

    for capabilities in (LOOPBACK_CAPABILITIES, MATTERMOST_CAPABILITIES):
        for key in ("delivery.session", "delivery.reply", "delivery.events"):
            assert capabilities.axis_restrictions[key] is PER_CONVERSATION
