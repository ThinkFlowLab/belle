"""Test workspace matching with a synthetic channel."""

from typing import Any

import pytest

from belle_agent.scopes import (
    AXIS_CHAT_TYPE,
    AXIS_WORKSPACE,
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
    """A platform that says which installation a request arrived from."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "workspace", "chat", "chat_type", "user"}),
            sections={"delivery": None, "agent": None},
            identity_keys=("user",),
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
        )
    )


@pytest.fixture
def single_workspace_channel(registry):
    """A platform that is in one installation, or cannot say which."""
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


def test_a_workspace_selects_every_conversation_in_it(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].match.workspace == "T_ACME"
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME")
    assert scopes[0].selects(channel="demo", chat="D9", workspace="T_ACME")
    assert not scopes[0].selects(channel="demo", chat="C1", workspace="T_OTHER")


def test_a_caller_with_no_workspace_gets_the_unnamed_answer(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="C1")
    assert not scopes[0].selects(channel="demo", chat="C1", workspace="")


def test_the_workspace_is_matched_exactly(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="C1", workspace="t_acme")
    assert not scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME ")


def test_a_rule_naming_no_workspace_applies_to_every_workspace(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [{"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}}], warn
    )

    assert warnings == []
    assert scopes[0].match.workspace is None
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME")
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_OTHER")
    # Including the request with no workspace: the axis is not
    # written, so there is nothing for it to fail closed on.
    assert scopes[0].selects(channel="demo", chat="C1")


def test_a_workspace_and_a_conversation_in_one_rule_both_apply(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "workspace": "T_ACME",
                    "chat": "C_SHARED",
                },
                "delivery": {"prompt": "our half of the shared channel"},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C_SHARED", workspace="T_ACME")
    assert not scopes[0].selects(channel="demo", chat="C_SHARED", workspace="T_GUEST")
    assert not scopes[0].selects(channel="demo", chat="C_OTHER", workspace="T_ACME")


def test_a_role_in_the_same_rule_does_not_drop_the_workspace(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "workspace": "T_ACME",
                    "role": "admin",
                },
                "delivery": {"prompt": "admins, in the ACME workspace"},
            }
        ],
        warn,
        people={"boss": {"demo": "U_BOSS"}},
        roles={"admin": ["boss"]},
    )

    assert warnings == []
    assert scopes[0].match.workspace == "T_ACME"
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME", user="U_BOSS")
    # The half the drop would give away: an admin in the other workspace is not
    # an admin in this one, and the rule names this one.
    assert not scopes[0].selects(
        channel="demo", chat="C1", workspace="T_OTHER", user="U_BOSS"
    )
    assert scopes[0].layer == (1, 1, 0, 1)


def test_the_axis_is_described(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME", "chat": "C1"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Coarsest first, which is the order the axes are declared and compared in.
    assert (
        scopes[0].match.describe() == "{channel: demo, workspace: T_ACME, chat: C1}"
    )


# Layers


def test_a_workspace_rule_outranks_a_rule_naming_the_platform_alone(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "acme"},
            },
            {"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}},
        ],
        warn,
    )

    # Written second and still folded first: the platform-wide rule is the layer
    # below whichever order the two appear in.
    ordered = matching_scopes(scopes, channel="demo", chat="C1", workspace="T_ACME")
    assert warnings == []
    assert [scope.section("delivery")["prompt"] for scope in ordered] == [
        "anywhere",
        "acme",
    ]


def test_naming_the_conversation_as_well_outranks_the_workspace_alone(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME", "chat": "C1"},
                "delivery": {"prompt": "this room, our side"},
            },
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "our workspace"},
            },
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1", workspace="T_ACME")
    assert warnings == []
    assert [scope.section("delivery")["prompt"] for scope in ordered] == [
        "our workspace",
        "this room, our side",
    ]


def test_the_workspace_outranks_a_rule_naming_the_conversation_alone(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "our workspace"},
            },
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"prompt": "this room, whoever it reaches"},
            },
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1", workspace="T_ACME")
    assert warnings == []
    assert [scope.layer for scope in ordered] == [(1, 0, 2, 0), (1, 1, 0, 0)]
    assert ordered[-1].section("delivery")["prompt"] == "our workspace"


# Shapes the loader refuses


def test_a_list_of_workspaces_is_refused_as_an_id_would_be(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo", "workspace": ["T_ACME", "T_OTHER"]}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].match.workspace=['T_ACME', 'T_OTHER'] is not text" in error


def test_an_empty_workspace_is_refused(demo_channel, refusal):
    # An empty id would match a request the connector gave no workspace for,
    # which is the case ``selects`` fails closed on.
    error = refusal([{"match": {"channel": "demo", "workspace": "  "}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].match.workspace is empty. Write one value" in error


def test_a_workspace_with_no_value_is_refused(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo", "workspace": None}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].match.workspace=None is not text" in error


def test_a_workspace_still_needs_the_platform_that_issued_it(refusal):
    error = refusal([{"match": {"workspace": "T_ACME"}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].match.workspace=T_ACME names a workspace but no channel" in error
    assert "Add channel to the match block" in error


# The capability


def test_a_platform_that_cannot_say_which_workspace_is_told_so(
    single_workspace_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "flat", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert any(
        "does not say which workspace a request came from" in line
        for line in warnings
    )
    assert scopes and not scopes[0].selects(channel="flat", chat="C1")


def test_a_workspace_rule_is_not_called_redundant_with_the_connector_block(
    warn, warnings
):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="layered",
            axes=frozenset({"channel", "workspace", "chat"}),
            sections={"agent": frozenset({"model_name"})},
            layer0_keys={"agent": {"model_name": "model_name"}},
        )
    )
    try:
        scopes = _compile(
            [
                {
                    "match": {"channel": "layered", "workspace": "T_ACME"},
                    "agent": {"model_name": "a-model"},
                }
            ],
            warn,
            channels_config={"layered": {"model_name": "the-connector-default"}},
        )
    finally:
        caps.restore_registry(snapshot)

    assert warnings == []
    assert scopes[0].section("agent") == {"model_name": "a-model"}


def test_the_workspace_reaches_the_fold_a_caller_actually_performs(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "acme"},
            },
            {"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}},
        ],
        warn,
    )

    assert warnings == []
    assert compose_section(
        scopes, channel="demo", chat="C1", workspace="T_ACME"
    ) == {"prompt": "acme"}
    assert compose_section(
        scopes, channel="demo", chat="C1", workspace="T_OTHER"
    ) == {"prompt": "anywhere"}
    assert compose_section(scopes, channel="demo", chat="C1") == {
        "prompt": "anywhere"
    }


def test_the_axis_is_one_of_the_supported_ones(demo_channel, warn, warnings):
    from belle_agent.scopes import SUPPORTED_AXES, SUPPORTED_MATCH_KEYS

    assert AXIS_WORKSPACE == "workspace"
    assert AXIS_WORKSPACE in SUPPORTED_AXES
    assert AXIS_WORKSPACE in SUPPORTED_MATCH_KEYS
    assert ScopeMatch(channel="demo", workspace="T1").describe() == (
        "{channel: demo, workspace: T1}"
    )
