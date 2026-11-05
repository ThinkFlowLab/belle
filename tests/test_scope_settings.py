"""Composed settings work with a platform's own modes and event kinds."""

from functools import partial

import pytest

from belle_agent.admission.policy import normalize_event_policy
from belle_agent.scopes import Scope, ScopeMatch
from belle_agent.scopes.settings import (
    ConversationOverride,
    compose_overrides,
    sections_as_override,
    settled_override,
)

convert = partial(
    sections_as_override,
    parse_mode=frozenset,
    parse_events=partial(normalize_event_policy, kinds={"update"}, turn_kinds={"update"}),
)


def test_cache_composes_signed_modes_and_retains_named_empty_settings():
    scopes = (
        Scope(ScopeMatch(channel="demo"), {"delivery": {"mode": ["+document"], "events": {"update": "context"}}}),
        Scope(ScopeMatch(channel="demo", chat="room"), {"delivery": {"mode": ["-document", "+answer"]}}),
        Scope(ScopeMatch(channel="demo", chat="limited"), {"agent": {"subagents": []}}),
        Scope(ScopeMatch(channel="other", chat="elsewhere"), {"delivery": {"prompt": "elsewhere"}}),
    )
    platform, chats = compose_overrides(scopes, channel="demo", layer0={"mode": {"ping"}}, convert=convert)
    assert platform.mode == frozenset({"ping", "document"})
    assert chats["room"].mode == frozenset({"ping", "answer"})
    assert chats["room"].events == {"update": "context"}
    assert set(chats) == {"room", "limited"}


def test_cached_empty_values_override_without_remerging_event_families():
    platform = ConversationOverride(
        prompt="base", mode=frozenset({"ping"}), events={"update": "context"}, skills={"use": ["base"]}
    )
    result = settled_override(
        (),
        "room",
        channel="demo",
        layer0={},
        convert=convert,
        platform_override=platform,
        conversation_overrides={"room": ConversationOverride(prompt="", mode=frozenset(), events={}, skills={})},
    )
    assert result.prompt == ""
    assert result.mode == frozenset()
    assert result.events == {}
    assert result.skills == {}


@pytest.mark.parametrize(
    "axis,value,facts",
    [
        ("users", ("person",), {"user_id": "person"}),
        ("chat_type", ("room",), {"chat_type": "room"}),
        ("workspace", "installation", {"workspace": "installation"}),
    ],
)
def test_contextual_settings_recompose_without_leaking_into_missing_identity(axis, value, facts):
    scopes = (
        Scope(
            ScopeMatch(channel="demo"),
            {"delivery": {"mode": ["+answer"]}, "agent": {"model_name": "base", "subagents": ["+helper"]}},
        ),
        Scope(ScopeMatch(channel="demo", **{axis: value}), {"agent": {"model_name": "selected"}}),
    )
    layer0 = {"mode": {"ping"}, "subagents": {"delivery-only"}}
    platform, chats = compose_overrides(scopes, channel="demo", layer0=layer0, convert=convert)
    assert platform.mode == frozenset({"ping", "answer"})
    kwargs = {
        "channel": "demo",
        "layer0": layer0,
        "convert": convert,
        "platform_override": platform,
        "conversation_overrides": chats,
    }
    contextual = settled_override(scopes, "room", **kwargs, **facts)
    assert contextual.model_name == "selected"
    assert contextual.mode == frozenset({"ping", "answer"})
    assert contextual.subagents == frozenset({"helper"})
    assert settled_override(scopes, "room", **kwargs).model_name == "base"
