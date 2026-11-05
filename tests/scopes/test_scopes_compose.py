"""Composition by value type, and what layer 0 is and is not for."""

import pytest

from belle_agent.scopes import (
    ChannelCapabilities,
    compile_scopes,
    compose_section,
    compose_values,
    scoped_chats,
    signed_entries,
)


@pytest.fixture
def demo_channel(registry):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None, "agent": None},
        )
    )


@pytest.mark.parametrize(
    "layers,base,expected",
    [
        pytest.param([{"model_name": "a"}, {"model_name": "b"}], {}, {"model_name": "b"}, id="scalar-replaced"),
        pytest.param([{"prompt": "x"}], {"model_name": "a"}, {"prompt": "x"}, id="unused-base-absent"),
        pytest.param([{"prompt": "x"}, {"prompt": ""}], {}, {"prompt": ""}, id="empty-scalar-replaces"),
        pytest.param(
            [{"mode": ["mention", "reply"]}],
            {"mode": frozenset({"all"})},
            {"mode": frozenset({"mention", "reply"})},
            id="list-replaces",
        ),
        pytest.param([{"mode": []}], {"mode": frozenset({"mention"})}, {"mode": frozenset()}, id="empty-list-replaces"),
        pytest.param(
            [{"mode": ["+has_file"]}],
            {"mode": frozenset({"mention"})},
            {"mode": frozenset({"mention", "has_file"})},
            id="add-to-base",
        ),
        pytest.param(
            [{"mode": ["-mention"]}],
            {"mode": frozenset({"mention", "all"})},
            {"mode": frozenset({"all"})},
            id="remove-from-base",
        ),
        pytest.param(
            [{"mode": ["url"]}, {"mode": ["+has_file"]}],
            {"mode": frozenset({"mention"})},
            {"mode": frozenset({"url", "has_file"})},
            id="add-to-prior-layer",
        ),
        pytest.param(
            [{"mode": ["+has_file", "-mention"]}],
            {"mode": frozenset({"mention"})},
            {"mode": frozenset({"has_file"})},
            id="mixed-signs",
        ),
        pytest.param(
            [{"mode": ["-url"]}], {"mode": frozenset({"mention"})}, {"mode": frozenset({"mention"})}, id="remove-absent"
        ),
        pytest.param([{"mode": ["+url"]}], {}, {"mode": frozenset({"url"})}, id="add-without-base"),
    ],
)
def test_scalar_and_set_composition(layers, base, expected):
    assert compose_values(layers, base=base) == expected


def test_a_mixed_list_is_refused_at_load_and_never_reaches_composition(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo"}, "delivery": {"mode": ["mention", "+has_file"]}}])

    assert "scopes[0].delivery.mode=['mention', '+has_file'] mixes plain entries with +/- entries" in error
    assert "Sign all entries or none" in error


def test_a_mixed_list_refuses_the_config_even_beside_a_valid_key(demo_channel, refusal):
    error = refusal(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["mention", "+has_file"], "prompt": "keep me"},
            }
        ]
    )

    assert "scopes[0].delivery.mode" in error
    assert "prompt" not in error


@pytest.mark.parametrize(
    "value,expected",
    [
        (["a", "b"], None),
        ([], None),
        (["+a", "-b"], ("+a", "-b")),
        (["a", "+b"], ()),
        (["+a", "b"], ()),
        ("mention", None),
    ],
)
def test_signed_entries_classifies_a_list(value, expected):
    assert signed_entries(value) == expected


def test_a_sign_with_nothing_after_it_is_refused(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo"}, "delivery": {"mode": ["+"]}}])

    assert "scopes[0].delivery.mode=['+'] has a + or - with no name after it" in error


def test_a_list_of_non_names_is_refused(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo"}, "delivery": {"mode": [1, 2]}}])

    assert "scopes[0].delivery.mode=[1, 2] is not a list of names" in error


@pytest.mark.parametrize(
    "layers,base,expected",
    [
        pytest.param([{"prompt": "a"}, {"prompt_append": "b"}], {}, {"prompt": "a\n\nb"}, id="append-to-prior-layer"),
        pytest.param([{"prompt_append": "b"}], {"prompt": "a"}, {"prompt": "a\n\nb"}, id="append-to-base"),
        pytest.param([{"prompt_append": "b"}], {}, {"prompt": "b"}, id="append-alone"),
        pytest.param(
            [{"prompt": "new", "prompt_append": "more"}],
            {"prompt": "old"},
            {"prompt": "new\n\nmore"},
            id="replace-and-append",
        ),
        pytest.param(
            [{"tools": {"bash": "ask"}}, {"tools": {"write": "deny"}}],
            {},
            {"tools": {"bash": "ask", "write": "deny"}},
            id="merge-maps",
        ),
        pytest.param(
            [{"tools": {"write": "deny"}}],
            {"tools": {"bash": "ask"}},
            {"tools": {"bash": "ask", "write": "deny"}},
            id="merge-base-map",
        ),
    ],
)
def test_prose_and_map_composition(layers, base, expected):
    assert compose_values(layers, base=base) == expected


def test_a_non_text_append_is_refused_at_load(demo_channel, refusal):
    error = refusal([{"match": {"channel": "demo"}, "delivery": {"prompt_append": ["a"]}}])

    assert "scopes[0].delivery.prompt_append=['a'] is not text. Write the text to append to prompt" in error


# Composition through the matcher


def test_layers_compose_per_key_so_a_scope_need_not_restate_the_rest(demo_channel, warn):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["mention"], "prompt": "base"},
            },
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "hi"}},
        ],
        warn=warn,
    )

    composed = compose_section(scopes, channel="demo", chat="C1")
    assert composed == {"mode": frozenset({"mention"}), "prompt": "hi"}


def test_the_agent_section_composes_across_layers_like_any_other(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "agent": {"model_name": "base-model"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "agent": {"model_name": "pinned-model"},
            },
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1", section="agent") == {"model_name": "pinned-model"}
    assert compose_section(scopes, channel="demo", chat="C2", section="agent") == {"model_name": "base-model"}


def test_a_conversation_named_only_by_the_agent_section_still_inherits_delivery(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "agent": {"model_name": "pinned-model"},
            },
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1") == {"mode": frozenset({"mention"})}
    assert compose_section(scopes, channel="demo", chat="C1", section="agent") == {"model_name": "pinned-model"}


def test_the_two_sections_never_see_each_others_keys(demo_channel, warn):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"label": "for the connector"},
                "agent": {"label": "for the runtime"},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo") == {"label": "for the connector"}
    assert compose_section(scopes, channel="demo", section="agent") == {"label": "for the runtime"}


def test_a_conversation_no_scope_names_gets_the_platform_layer_only(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C2") == {"mode": frozenset({"mention"})}


def test_nothing_matching_settles_nothing(demo_channel, warn):
    scopes = compile_scopes(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    # An empty result leaves the caller's defaults unchanged.
    assert compose_section(scopes, channel="demo", chat="C9") == {}


def test_scoped_chats_names_the_conversations_a_connector_must_settle(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo", "chat": "C2"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
        ],
        warn=warn,
    )

    assert scoped_chats(scopes, channel="demo") == ("C1", "C2")


def test_scoped_chats_counts_a_conversation_named_only_by_the_agent_section(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo", "chat": "C2"}, "agent": {"model_name": "m"}},
        ],
        warn=warn,
    )

    assert scoped_chats(scopes, channel="demo") == ("C1", "C2")
    assert scoped_chats(scopes, channel="demo", sections=("agent",)) == ("C2",)
