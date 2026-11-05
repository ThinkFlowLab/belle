"""Slack's scopes declaration, and what a written scope settles to.

Two halves. The declaration says which axes Slack populates and which settings
it reads in which section, and the shared loader refuses anything else against
it. The fold turns the compiled scopes into the two things the connector reads
per conversation: a platform-wide layer and a per-conversation map.

The property the whole shape turns on is pinned first: **with nothing written,
nothing is settled.** An operator with no scopes: block gets a connector that
follows channels.slack alone.

Warnings are captured by replacing the module logger's ``warning`` rather than
through ``caplog``: this project's loggers do not propagate, so ``caplog`` sees
nothing under the pytest CI runs on, and a test written against it would pass
locally and assert nothing where it matters.
"""

from pathlib import Path
from typing import Any

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.silence import REPLY_OPTIONAL_KEY

from belle_agent.integrations.jiuwenswarm import config as belle_config
from belle_agent.platforms.slack import scoped_config
from belle_agent.scopes import (
    AXIS_CHAT_TYPE,
    MID_TURN_CANCEL,
    MID_TURN_DEFAULT,
    MID_TURN_QUEUE,
    channel_capabilities,
    compile_scopes,
    compose_section,
)
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm import slack_command_handlers as slash_handlers
from belle_agent.integrations.jiuwenswarm import slack_inbound
from belle_agent.integrations.jiuwenswarm import scope_capabilities
from belle_agent.integrations.jiuwenswarm.config import slack_agent_request_params
from belle_agent.platforms.slack.scoped_config import (
    SlackChannelOverride,
    apply_scopes_to_slack_overrides,
    channel_triggers,
    describe_configured_channels,
    sections_as_override,
    settled_override,
    slack_scope_enables_channel,
)
from belle_agent.platforms.slack.identity import SlackWorkspaceIdentity
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)

@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "warning", record)
    monkeypatch.setattr(slash_handlers.logger, "warning", record)
    monkeypatch.setattr(scoped_config.logger, "warning", record)
    return recorded


@pytest.fixture
def any_model(monkeypatch):
    """Accept every model name, so a test can isolate something else."""
    monkeypatch.setattr(slash_handlers, "configured_models", lambda: belle_config.ConfiguredModels())
    monkeypatch.setattr(slack_inbound, "configured_models", lambda: belle_config.ConfiguredModels())
    monkeypatch.setattr(belle_config, "configured_models", lambda: belle_config.ConfiguredModels())
    return None


def _scopes(entries: Any, warn=None):
    return compile_scopes(entries, warn=warn or (lambda *a: None))


# --------------------------------------------------------------------------
# The declaration
# --------------------------------------------------------------------------


def test_slack_is_discovered_without_importing_the_connector():
    # The declaration module is found by filename and must not drag slack_sdk
    # into a process that has no Slack in it, so it may not import
    # slack_connect at module level.
    source = Path(scope_capabilities.__file__).read_text(encoding="utf-8")
    module_level = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "slack_connect" in line
    ]
    assert module_level == []
    assert channel_capabilities("slack") is not None


def test_slack_declares_the_conversation_axis():
    assert channel_capabilities("slack").populates("chat")


def test_slack_declares_a_sender_even_though_this_version_will_not_read_one():
    # The declaration states the truth about the connector. Which axes are
    # readable is a property of the matcher and is stated once, elsewhere.
    assert channel_capabilities("slack").populates("user")


def test_slack_declares_exactly_the_settings_the_carrier_holds():
    # Split across the two sections by who acts on them: the Connector consumes
    # delivery settings and puts agent settings on requests.
    declared = channel_capabilities("slack")
    assert declared.keys_for("delivery") == frozenset(
        {"mode", "prompt", "mid_turn", "session", "reply", "events"}
    )
    assert declared.keys_for("agent") == frozenset(
        {"model_name", "expert", "subagents", "skills"}
    )


def test_agent_skills_compose_per_subkey_and_preserve_explicit_empty_lists():
    scopes = _scopes([
        {"match": {"channel": "slack"}, "agent": {
            "subagents": ["research_agent"],
            "skills": {"available": ["ambient", "research"], "required": ["ambient"]},
        }},
        {"match": {"channel": "slack", "chat": "C-A"}, "agent": {
            "subagents": [], "skills": {"required": []},
        }},
    ])
    platform, resolved = apply_scopes_to_slack_overrides({}, scopes=scopes)
    config = SlackChannelConfig(
        scopes=scopes, platform_override=platform, conversation_overrides=resolved
    )
    params = slack_agent_request_params(config, "C-A")
    assert params == {
        "agent_template_name": "",
        "agent_subagents_available": [],
        "agent_skills_available": ["ambient", "research"],
        "agent_skills_required": [],
    }
    assert slack_agent_request_params(config, "C-B") == {
        "agent_template_name": "",
        "agent_subagents_available": ["research_agent"],
        "agent_skills_available": ["ambient", "research"],
        "agent_skills_required": ["ambient"],
    }
    assert slack_agent_request_params(SlackChannelConfig(), "C-A") == {
        "agent_template_name": "",
    }


def test_unavailable_skills_reach_the_request_and_replace_what_they_inherit():
    scopes = _scopes([
        {"match": {"channel": "slack"}, "agent": {"skills": {"unavailable": ["ambient"], "required": ["research"]}}},
        {"match": {"channel": "slack", "chat": "C-A"}, "agent": {"skills": {"unavailable": ["other"]}}},
    ])
    platform, resolved = apply_scopes_to_slack_overrides({}, scopes=scopes)
    config = SlackChannelConfig(scopes=scopes, platform_override=platform, conversation_overrides=resolved)

    assert slack_agent_request_params(config, "C-A") == {
        "agent_template_name": "",
        "agent_skills_unavailable": ["other"],
        "agent_skills_required": ["research"],
    }
    assert slack_agent_request_params(config, "C-B") == {
        "agent_template_name": "",
        "agent_skills_unavailable": ["ambient"],
        "agent_skills_required": ["research"],
    }


def test_expert_scope_selects_one_template_and_can_clear_it():
    scopes = _scopes([
        {"match": {"channel": "slack"}, "agent": {"expert": "scope-expert-cedar-probe"}},
        {"match": {"channel": "slack", "chat": "C-OTHER"}, "agent": {
            "expert": "scope-expert-lantern-probe",
        }},
        {"match": {"channel": "slack", "chat": "C-CLEAR"}, "agent": {"expert": ""}},
    ])
    platform, resolved = apply_scopes_to_slack_overrides({}, scopes=scopes)
    config = SlackChannelConfig(
        scopes=scopes, platform_override=platform, conversation_overrides=resolved
    )
    assert slack_agent_request_params(config, "C-BASE")["agent_template_name"] == (
        "scope-expert-cedar-probe"
    )
    assert slack_agent_request_params(config, "C-OTHER")["agent_template_name"] == (
        "scope-expert-lantern-probe"
    )
    assert slack_agent_request_params(config, "C-CLEAR")["agent_template_name"] == ""


@pytest.mark.parametrize("value", [None, False, [], {}, 42, " ", " expert "])
def test_invalid_expert_name_is_refused(value, refusal):
    error = refusal([
        {"match": {"channel": "slack"}, "agent": {"expert": "valid-expert"}},
        {"match": {"channel": "slack", "chat": "C-A"}, "agent": {"expert": value}},
    ])
    assert "scopes[1].agent.expert" in error
    assert "must be one expert package name" in error
    assert "scopes[0]" not in error


@pytest.mark.parametrize("value", [
    "research_agent", [""], [" research_agent"], ["+research_agent"],
    ["research_agent", "research_agent"],
])
def test_invalid_subagent_lists_are_refused(value, refusal):
    error = refusal([{"match": {"channel": "slack"}, "agent": {"subagents": value}}])
    assert "scopes[0].agent.subagents=" in error


@pytest.mark.parametrize("value", [
    [], {}, {"available": "ambient"}, {"required": ["+ambient"]},
    {"extra": []}, {"required": [1]},
])
def test_invalid_skill_settings_are_refused(value, refusal):
    error = refusal([{"match": {"channel": "slack"}, "agent": {"skills": value}}])
    assert "scopes[0].agent.skills" in error


def test_availability_scopes_do_not_opt_channels_into_slack():
    scopes = _scopes([{"match": {"channel": "slack", "chat": "C-NEW"},
                       "agent": {"expert": "test-expert", "subagents": [],
                                 "skills": {"available": []}}}])
    _, resolved = apply_scopes_to_slack_overrides({}, scopes=scopes)
    config = SlackChannelConfig(scopes=scopes, conversation_overrides=resolved)
    assert "C-NEW" in resolved
    assert not slack_scope_enables_channel(config, "C-NEW")
    assert slack_agent_request_params(config, "C-NEW") == {
        "agent_template_name": "test-expert",
        "agent_subagents_available": [], "agent_skills_available": [],
    }


@pytest.mark.asyncio
async def test_scoped_agent_params_reach_an_allowed_slack_turn():
    scopes = _scopes([{"match": {"channel": "slack", "chat": "C-NEW"},
                       "agent": {"expert": "test-expert", "subagents": [],
                                 "skills": {"required": ["ambient"]}}}])
    platform, resolved = apply_scopes_to_slack_overrides({}, scopes=scopes)
    config = SlackChannelConfig(
        enabled=True, allow_from=["U-ASKER"], allowed_channel_ids=["C-NEW"],
        acknowledge_mode="off", thinking_status="", scopes=scopes,
        platform_override=platform, conversation_overrides=resolved,
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    seen: list[Message] = []
    channel.on_message(seen.append)
    outcome = await channel._handle_slack_event(
        {"type": "message", "channel_type": "channel", "channel": "C-NEW",
         "user": "U-ASKER", "text": "<@BOT> hello", "ts": "1710000001.000100"},
        {"event_id": "Ev-scoped-agent", "team_id": "T-TEAM"},
        is_dm=False, trigger="mention",
    )
    assert outcome.startswith("dispatched:")
    assert seen[0].params["agent_template_name"] == "test-expert"
    assert seen[0].params["agent_subagents_available"] == []
    assert seen[0].params["agent_skills_required"] == ["ambient"]
    assert "agent_skills_available" not in seen[0].params


@pytest.mark.asyncio
async def test_availability_only_scope_does_not_bypass_channel_allowlist():
    scopes = _scopes([{"match": {"channel": "slack", "chat": "C-NEW"},
                       "agent": {"skills": {"available": []}}}])
    platform, resolved = apply_scopes_to_slack_overrides({}, scopes=scopes)
    config = SlackChannelConfig(
        enabled=True, allowed_channel_ids=["C-ELSEWHERE"], scopes=scopes,
        platform_override=platform, conversation_overrides=resolved,
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    seen: list[Message] = []
    channel.on_message(seen.append)
    outcome = await channel._handle_slack_event(
        {"type": "message", "channel_type": "channel", "channel": "C-NEW",
         "user": "U-ASKER", "text": "hello", "ts": "1710000002.000100"},
        {"event_id": "Ev-availability-only", "team_id": "T-TEAM"},
        is_dm=False, trigger="mention",
    )
    assert outcome == "ignored:channel-not-in-allowed_channel_ids"
    assert seen == []


def test_slack_opts_into_permissions_without_naming_its_keys():
    # The third section is neither consumed nor carried here -- it is resolved
    # in the runtime from the ids the request arrived with. Slack declares it
    # because the declaration is the opt-in, and leaves its keys to the shared
    # loader rather than keeping a second copy of the vocabulary.
    declared = channel_capabilities("slack")
    assert declared.reads("permissions")
    assert declared.keys_for("permissions") is None


def test_slack_is_attended_so_a_scope_may_ask():
    # An ask degrades to a deny where nobody can click. Slack renders
    # interactive approvals on this branch, so it does not get the degrade --
    # and this is the assertion that says the interactive path is a capability
    # of the deployment rather than a comment.
    from belle_agent.integrations.openjiuwen.scopes.permissions import settled_tool_levels

    assert channel_capabilities("slack").attended is True
    scopes = _scopes(
        [
            {
                "match": {"channel": "slack", "chat": "C0"},
                "permissions": {"tools": {"bash": "ask"}},
            }
        ],
        warn=lambda *a: None,
    )
    assert settled_tool_levels(scopes, channel="slack", chat="C0") == {"bash": "ask"}


def test_a_permissions_only_scope_does_not_open_a_slack_conversation():
    # The ids scoped_chats returns are what exempt a channel from
    # allowed_channel_ids. A rule written to take bash away must not hand out an
    # answer in a channel the operator never listed.
    from belle_agent.scopes import scoped_chats

    scopes = _scopes(
        [
            {
                "match": {"channel": "slack", "chat": "C-LOCKED"},
                "permissions": {"tools": {"bash": "deny"}},
            }
        ],
        warn=lambda *a: None,
    )
    assert scoped_chats(scopes, channel="slack") == ()


def test_a_model_name_written_under_delivery_is_reported_as_the_wrong_section():
    # Where a config written against the earlier shape lands. delivery is still
    # a section slack reads, so the line is about the key rather than about the
    # section, and the rest of the rule stands.
    said: list[str] = []
    scopes = _scopes(
        [
            {
                "match": {"channel": "slack"},
                "delivery": {"mode": ["all"], "model_name": "anything"},
            }
        ],
        warn=lambda m, *a: said.append(m % a),
    )
    assert any("delivery.model_name has no effect" in line for line in said), said
    assert compose_section(scopes, channel="slack") == {"mode": frozenset({"all"})}
    assert compose_section(scopes, channel="slack", section="agent") == {}


def test_the_wrong_section_warning_names_the_delivery_settings_that_are_left():
    said: list[str] = []
    _scopes(
        [{"match": {"channel": "slack"}, "delivery": {"model_name": "anything"}}],
        warn=lambda m, *a: said.append(m % a),
    )
    assert any(
        "it reads are events, mid_turn, mode, prompt" in line for line in said
    ), said


def test_slack_declares_no_layer_zero_key_for_the_model():
    # There is no channels.slack key that pins a model for every conversation,
    # so there is nothing for a scope to be a second home for. Declaring one
    # would fire the "two homes for one value" warning against a setting that
    # does not exist.
    assert channel_capabilities("slack").layer0_key("agent", "model_name") is None


def test_a_setting_slack_does_not_read_is_reported(warnings):
    said: list[str] = []
    _scopes(
        [{"match": {"channel": "slack"}, "delivery": {"reasoning_level": "high"}}],
        warn=lambda m, *a: said.append(m % a),
    )
    assert any("delivery.reasoning_level has no effect" in line for line in said), said


def test_a_work_mode_is_refused_because_a_session_locks_it_on_its_first_turn():
    said: list[str] = []
    scopes = _scopes(
        [{"match": {"channel": "slack"}, "agent": {"work_mode": "code"}}],
        warn=lambda m, *a: said.append(m % a),
    )
    assert scopes == ()
    assert any("agent.work_mode has no effect" in line for line in said), said
    # Sorted, and read as a prefix: the point of the assertion is that work_mode
    # is not in the list, not how long the list is.
    assert any("settings it reads are expert, model_name" in line for line in said), said


def test_a_bad_trigger_name_is_refused_through_the_declaration(refusal):
    error = refusal([{"match": {"channel": "slack"}, "delivery": {"mode": ["menshun"]}}])
    assert "scopes[0].delivery.mode names 'menshun', which is not a trigger" in error


def test_the_bad_trigger_refusal_names_the_triggers(refusal):
    error = refusal([{"match": {"channel": "slack"}, "delivery": {"mode": ["menshun"]}}])
    assert "The triggers are mention/reply/all/url/has_file" in error


def test_a_sign_is_stripped_before_a_trigger_name_is_checked():
    said: list[str] = []
    scopes = _scopes(
        [{"match": {"channel": "slack"}, "delivery": {"mode": ["+has_file"]}}],
        warn=lambda m, *a: said.append(m % a),
    )
    assert len(scopes) == 1
    assert said == []


def test_only_one_sign_is_stripped_because_only_one_is_applied(any_model, refusal):
    error = refusal([{"match": {"channel": "slack"}, "delivery": {"mode": ["++url"]}}])

    assert "names '++url', which is not a trigger" in error

    # And the set the fold would have produced is the one the refusal avoided.
    platform, _ = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [{"match": {"channel": "slack"}, "delivery": {"mode": ["+url"]}}]
        ),
    )
    assert platform.mode == frozenset({"mention", "url"})


def test_the_legacy_bare_string_spelling_is_refused_with_the_list_to_write(refusal):
    error = refusal([{"match": {"channel": "slack"}, "delivery": {"mode": "mention"}}])
    assert "scopes[0].delivery.mode='mention' is one word. Write a list of triggers: [mention]" in error


def test_a_model_name_is_checked_against_the_configured_models(monkeypatch, refusal):
    monkeypatch.setattr(
        belle_config,
        "configured_models",
        lambda: belle_config.ConfiguredModels(names=("good",)),
    )
    error = refusal([{"match": {"channel": "slack"}, "agent": {"model_name": "typo"}}])
    assert "scopes[0].agent.model_name='typo' is not one of the models configured in models.defaults (good)" in error


def test_an_unreadable_model_list_does_not_refuse_a_correct_setting(monkeypatch):
    # A config read that failed must not cost the operator a setting, which is
    # the one failure they cannot act on.
    monkeypatch.setattr(
        belle_config, "configured_models", lambda: belle_config.ConfiguredModels()
    )
    scopes = _scopes(
        [{"match": {"channel": "slack"}, "agent": {"model_name": "anything"}}]
    )
    assert compose_section(scopes, channel="slack", section="agent") == {
        "model_name": "anything"
    }


def test_group_chat_mode_is_declared_as_the_connector_setting_mode_sits_above():
    said: list[str] = []
    compile_scopes(
        [{"match": {"channel": "slack"}, "delivery": {"mode": ["all"]}}],
        channels_config={"slack": {"group_chat_mode": "mention"}},
        warn=lambda m, *a: said.append(m % a),
    )
    assert any(
        "Belle platforms.slack.group_chat_mode both set this" in line for line in said
    ), said


# --------------------------------------------------------------------------
# Nothing written changes nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scopes", [None, (), []])
def test_with_no_scopes_nothing_is_settled(scopes):
    platform, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"}, scopes=scopes
    )

    assert resolved == {}
    assert platform == SlackChannelOverride()


# --------------------------------------------------------------------------
# Reading composed sections back as the carrier
# --------------------------------------------------------------------------


def test_settled_sections_read_back_as_one_override():
    assert sections_as_override(
        {
            "delivery": {"mode": ["mention"], "prompt": "hi"},
            "agent": {"model_name": " m "},
        }
    ) == SlackChannelOverride(mode=frozenset({"mention"}), prompt="hi", model_name="m")


def test_a_model_name_settled_under_delivery_is_not_read_back():
    # The carrier spans two sections; it does not merge them. A value in the
    # section that no longer declares it has already been warned about and
    # dropped at load, and honouring it here would put it back.
    assert sections_as_override({"delivery": {"model_name": "m"}}).model_name is None


def test_a_bare_string_mode_never_becomes_a_set_of_letters():
    # frozenset("all") is {"a", "l"}, which would be an unreadable corruption
    # of a channel's triggers rather than an error anybody could see.
    assert sections_as_override({"delivery": {"mode": "all"}}).mode == frozenset(
        {"mention", "all"}
    )


# --------------------------------------------------------------------------
# Layering against a real Slack config
# --------------------------------------------------------------------------


def test_a_signed_mode_inherits_group_chat_mode(any_model):
    _, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "reply"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-NEW"},
                    "delivery": {"mode": ["+has_file"]},
                }
            ]
        ),
    )
    assert resolved["C-NEW"].mode == frozenset({"mention", "reply", "has_file"})


def test_a_removal_narrows_what_the_platform_default_answered(any_model):
    _, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "all"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-QUIET"},
                    "delivery": {"mode": ["-all"]},
                }
            ]
        ),
    )
    assert resolved["C-QUIET"].mode == frozenset({"mention"})


def test_a_platform_scope_is_the_layer_between_the_global_and_a_conversation(any_model):
    platform, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {"match": {"channel": "slack"}, "delivery": {"mode": ["all"], "prompt": "p"}},
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": ["mention"]},
                },
            ]
        ),
    )
    assert platform == SlackChannelOverride(mode=frozenset({"all"}), prompt="p")
    assert resolved["C-A"] == SlackChannelOverride(mode=frozenset({"mention"}), prompt="p")


def test_the_platform_layer_beats_group_chat_mode_for_a_channel_nobody_named(any_model):
    platform, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes([{"match": {"channel": "slack"}, "delivery": {"mode": ["all"]}}]),
    )
    config = SlackChannelConfig(
        conversation_overrides=resolved, platform_override=platform, group_chat_mode="mention"
    )
    assert channel_triggers(config, "C-UNNAMED", group_chat_mode="mention") == frozenset(
        {"all"}
    )


def test_a_named_conversation_still_beats_the_platform_layer(any_model):
    platform, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {"match": {"channel": "slack"}, "delivery": {"mode": ["all"]}},
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": ["mention"]},
                },
            ]
        ),
    )
    config = SlackChannelConfig(
        conversation_overrides=resolved, platform_override=platform, group_chat_mode="mention"
    )
    assert channel_triggers(config, "C-A", group_chat_mode="mention") == frozenset(
        {"mention"}
    )


def test_a_prompt_append_adds_to_a_scope_prompt(any_model):
    _, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {"match": {"channel": "slack"}, "delivery": {"prompt": "Answer in English."}},
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"prompt_append": "Be terse."},
                },
            ]
        ),
    )
    assert resolved["C-A"].prompt == "Answer in English.\n\nBe terse."


def test_a_platform_scope_pins_a_model_for_every_conversation(any_model):
    platform, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {"match": {"channel": "slack"}, "agent": {"model_name": "platform-model"}},
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "agent": {"model_name": "channel-model"},
                },
            ]
        ),
    )
    # Layer 1 for anything nobody named, layer 2 where somebody did. The same
    # cascade delivery gets, which is the point of composing per section rather
    # than per mechanism.
    assert platform.model_name == "platform-model"
    assert resolved["C-A"].model_name == "channel-model"


def test_a_scope_sets_the_model_without_disturbing_the_triggers(any_model):
    # Per key across sections as well as within one. A scope that speaks only
    # about agent must leave delivery exactly as the layer below left it.
    _, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": ["url"], "prompt": "from delivery"},
                },
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "agent": {"model_name": "m"},
                },
            ]
        ),
    )
    assert resolved["C-A"] == SlackChannelOverride(
        mode=frozenset({"url"}), prompt="from delivery", model_name="m"
    )


def test_a_conversation_named_only_by_an_agent_scope_is_settled(any_model):
    # It reaches the map at all only because scoped_chats spans both sections.
    # Per-section, this conversation would not have been iterated and the model
    # would have been dropped with nothing said about it.
    _, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-NEW"},
                    "agent": {"model_name": "m"},
                }
            ]
        ),
    )
    assert resolved["C-NEW"].model_name == "m"
    # And nothing else: delivery said nothing, so the connector's own chain
    # still decides what that conversation answers.
    assert resolved["C-NEW"].mode is None


def test_a_model_name_under_delivery_is_dropped_rather_than_applied(any_model):
    _, resolved = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": ["all"], "model_name": "wrong-section"},
                }
            ]
        ),
    )
    assert resolved["C-A"].mode == frozenset({"all"})
    assert resolved["C-A"].model_name is None


# --------------------------------------------------------------------------
# What the composition warns about
# --------------------------------------------------------------------------


def test_a_composed_mode_that_drops_mention_is_reported(warnings, any_model):
    apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": ["-mention", "+url"]},
                }
            ]
        ),
    )
    assert any(
        "name neither mention nor all" in line and "C-A" in line for line in warnings
    ), warnings


def test_a_composed_mode_of_all_alone_is_not_reported_as_dropping_mention(
    warnings, any_model
):
    apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": ["all"]},
                }
            ]
        ),
    )
    assert not any("mention" in line for line in warnings), warnings


def test_all_beside_mention_is_not_reported_as_redundant(warnings, any_model):
    apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": ["+all"]},
                }
            ]
        ),
    )
    assert not any("add nothing" in line for line in warnings), warnings


def test_a_composition_that_silences_a_conversation_is_reported(warnings, any_model):
    apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [
                {
                    "match": {"channel": "slack", "chat": "C-A"},
                    "delivery": {"mode": []},
                }
            ]
        ),
    )
    assert any("is now silent" in line for line in warnings), warnings


def test_a_platform_scope_that_drops_mention_is_reported(warnings, any_model):
    apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"},
        scopes=_scopes(
            [{"match": {"channel": "slack"}, "delivery": {"mode": ["url"]}}]
        ),
    )
    assert any(
        "the scope for channel slack" in line
        and "name neither mention nor all" in line
        for line in warnings
    ), warnings


# --------------------------------------------------------------------------
# The startup summary
# --------------------------------------------------------------------------


def test_the_summary_names_scopes_as_the_source_of_a_scoped_conversation():
    config = SlackChannelConfig(
        conversation_overrides={"C-A": SlackChannelOverride(prompt="p")},
        group_chat_mode="mention",
    )
    assert "via=scopes" in describe_configured_channels(config)
    assert "prompt=scopes" in describe_configured_channels(config)


def test_the_summary_says_what_an_unnamed_channel_follows_once_a_scope_replaces_it():
    # Naming group_chat_mode here would print the one value no longer in force,
    # and this line exists to be checked against what the operator meant.
    config = SlackChannelConfig(
        platform_override=SlackChannelOverride(mode=frozenset({"all"})),
        group_chat_mode="mention",
    )
    assert "follows the scope for channel slack: mode=[all]" in (
        describe_configured_channels(config)
    )


def test_the_summary_is_unchanged_when_no_scope_replaced_the_global():
    config = SlackChannelConfig(group_chat_mode="mention")
    assert "follows group_chat_mode=mention" in describe_configured_channels(config)


def test_the_summary_reports_what_a_platform_scope_settled_for_a_named_channel():
    config = SlackChannelConfig(
        platform_override=SlackChannelOverride(
            prompt="House rules apply.", model_name="deepseek-v3"
        ),
        conversation_overrides={"C-A": SlackChannelOverride(mode=frozenset({"all"}))},
        group_chat_mode="mention",
    )

    summary = describe_configured_channels(config)

    assert "prompt=scopes" in summary
    assert "model=deepseek-v3" in summary


# --------------------------------------------------------------------------
# A rule that names a sender, settled per message
# --------------------------------------------------------------------------


def _configured(entries: Any, *, group_chat_mode: str = "mention") -> SlackChannelConfig:
    """A config built the way ``app_gateway`` builds one, from written scopes."""
    scopes = _scopes(entries)
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": group_chat_mode}, scopes=scopes
    )
    return SlackChannelConfig(
        group_chat_mode=group_chat_mode,
        conversation_overrides=per_chat,
        platform_override=platform,
        scopes=scopes,
    )


def test_a_config_with_no_identity_rule_settles_exactly_as_before(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "delivery": {"mode": ["mention"]}},
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"prompt": "be terse"},
                "agent": {"model_name": "m"},
            },
        ]
    )

    for who in ("", "U_anyone", "U_someone_else"):
        override = settled_override(config, "C1", user_id=who)
        assert override.mode == frozenset({"mention"})
        assert override.prompt == "be terse"
        assert override.model_name == "m"


def test_the_platform_layer_is_still_read_per_key_for_a_conversation_scope(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "slack", "chat": "C1"}, "delivery": {"prompt": "p"}},
        ]
    )

    # The conversation named a prompt and nothing else, so the platform layer's
    # triggers still govern there. Pinned because settled_override is now what
    # performs that fallthrough for all three settings at once.
    override = settled_override(config, "C1")
    assert override.mode == frozenset({"all"})
    assert override.prompt == "p"


def test_a_rule_naming_a_sender_settles_only_for_that_sender(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack", "chat": "C1"}, "agent": {"model_name": "base"}},
            {
                "match": {"channel": "slack", "chat": "C1", "user": ["U1"]},
                "agent": {"model_name": "theirs"},
            },
        ]
    )

    assert settled_override(config, "C1", user_id="U1").model_name == "theirs"
    assert settled_override(config, "C1", user_id="U2").model_name == "base"
    # No sender is not everybody. A caller with none gets the layer below.
    assert settled_override(config, "C1").model_name == "base"


def test_a_not_narrows_everyone_except_the_people_it_names(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "delivery": {"mode": ["all"]}},
            {
                "match": {"channel": "slack", "chat": "C1", "not": {"user": ["U_admin"]}},
                "delivery": {"mode": ["mention"]},
            },
        ]
    )

    # Section 4.3's motivating case, end to end: the exemption is written into
    # the rule it exempts from, and the admin matches no narrowing scope.
    assert channel_triggers(
        config, "C1", group_chat_mode="mention", user_id="U_admin"
    ) == frozenset({"all"})
    assert channel_triggers(
        config, "C1", group_chat_mode="mention", user_id="U_other"
    ) == frozenset({"mention"})
    # And an unidentified sender is not exempt: the restriction holds where
    # nobody could be named, rather than lapsing.
    assert channel_triggers(config, "C1", group_chat_mode="mention") == frozenset(
        {"mention"}
    )


def test_a_signed_mode_on_an_identity_rule_still_inherits_layer_zero(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat": "C1", "user": ["U1"]},
                "delivery": {"mode": ["+has_file"]},
            }
        ],
        group_chat_mode="mention",
    )

    # The per-message fold has to be given the same layer 0 the load-time fold
    # was, or "+has_file" would settle to a set of one and silence @mentions
    # for that person alone.
    assert channel_triggers(
        config, "C1", group_chat_mode="mention", user_id="U1"
    ) == frozenset({"mention", "has_file"})


def test_a_platform_scope_naming_a_sender_reaches_a_conversation_nobody_named(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "user": ["U1"]},
                "delivery": {"prompt": "for U1 anywhere on slack"},
            }
        ]
    )

    # No scope names C9, so it has no entry in the settled map -- and the rule
    # still applies there, because it was written about the platform. The
    # per-message fold is given the real conversation id rather than None for
    # exactly this case.
    assert settled_override(config, "C9", user_id="U1").prompt == (
        "for U1 anywhere on slack"
    )
    assert settled_override(config, "C9", user_id="U2").prompt is None


def test_the_startup_summary_reports_the_unidentified_answer(any_model, warnings):
    config = _configured(
        [
            {"match": {"channel": "slack", "chat": "C1"}, "delivery": {"mode": ["all"]}},
            {
                "match": {"channel": "slack", "chat": "C1", "user": ["U1"]},
                "delivery": {"mode": ["mention"]},
            },
        ]
    )

    # It is written before anyone has spoken, so it can only report the layer
    # that does not depend on who is speaking. Stated here so the line is read
    # as what it is rather than as a claim about every message in C1.
    summary = describe_configured_channels(config)
    assert "C1" in summary
    assert channel_triggers(config, "C1", group_chat_mode="mention") == frozenset(
        {"all"}
    )


# --------------------------------------------------------------------------
# people: and roles: reaching the connector
# --------------------------------------------------------------------------


def _config_with_a_role() -> dict:
    return {
        "platforms": {"slack": {"group_chat_mode": "mention"}},
        "people": {
            "hana": {"slack": "U000000AAAA"},
            "boss": {"slack": "U000000BBBB"},
        },
        "roles": {"admin": ["hana", "boss"]},
        "scopes": [
            {
                "match": {
                    "channel": "slack",
                    "chat": "C000000BBBB",
                    "not": {"role": "admin"},
                },
                "delivery": {"prompt_append": "Ask an admin before acting."},
            }
        ],
    }


def test_the_loader_reads_people_and_roles_beside_the_scopes(monkeypatch, warnings):
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(config_module, "get_config", lambda: {"channels": {}})
    monkeypatch.setattr(belle_config, "load_belle_config", lambda: _config_with_a_role())

    scopes = belle_config.load_slack_scopes()

    # The connector's own entry point has to pass them: compiling without them
    # would leave every role undeclared and drop every scope naming one, for a
    # config that is perfectly good.
    assert warnings == []
    assert len(scopes) == 1
    assert scopes[0].match.not_users == ("U000000AAAA", "U000000BBBB")


def test_a_role_bearing_scope_is_folded_per_message(monkeypatch, any_model, warnings):
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(config_module, "get_config", lambda: {"channels": {}})
    monkeypatch.setattr(belle_config, "load_belle_config", lambda: _config_with_a_role())
    scopes = belle_config.load_slack_scopes()
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"}, scopes=scopes
    )
    config = SlackChannelConfig(
        scopes=scopes,
        platform_override=platform,
        conversation_overrides=per_chat,
        group_chat_mode="mention",
    )

    # A role constrains identity, so the per-message path is the one that runs
    # -- the settled map cannot hold an answer that depends on who is speaking.
    admin = settled_override(config, "C000000BBBB", user_id="U000000AAAA")
    everyone_else = settled_override(config, "C000000BBBB", user_id="U000000ZZZZ")

    assert admin.prompt is None
    assert everyone_else.prompt == "Ask an admin before acting."


def test_an_unidentified_sender_is_not_exempted_by_a_role(
    monkeypatch, any_model, warnings
):
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(config_module, "get_config", lambda: {"channels": {}})
    monkeypatch.setattr(belle_config, "load_belle_config", lambda: _config_with_a_role())
    scopes = belle_config.load_slack_scopes()
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"}, scopes=scopes
    )
    config = SlackChannelConfig(
        scopes=scopes,
        platform_override=platform,
        conversation_overrides=per_chat,
        group_chat_mode="mention",
    )

    # The settled map is composed with no sender, and for a "not" that is the
    # same answer the per-message fold gives: nobody is excluded, so the rule
    # applies. Restrictions hold where nobody could be identified.
    assert settled_override(config, "C000000BBBB").prompt == "Ask an admin before acting."


# delivery.mid_turn
# --------------------------------------------------------------------------


def test_a_conversation_nobody_wrote_a_rule_for_queues(any_model):
    channel = SlackChannel(SlackChannelConfig(enabled=True), slack_connect.RobotMessageRouter())

    # The whole contract of the default: an operator who writes nothing gets the
    # one value that cannot destroy a turn already running.
    assert channel._mid_turn_mode("C1", "U1") == MID_TURN_DEFAULT
    assert channel._mid_turn_mode("C1", "U1") == MID_TURN_QUEUE


def test_a_scope_settles_what_a_mid_turn_message_does(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"mid_turn": "queue"},
            }
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    assert channel._mid_turn_mode("C1", "U1") == MID_TURN_QUEUE
    # Every other conversation is untouched, which is what makes this a
    # per-conversation setting rather than a connector one. C2 reads the same
    # value here by coincidence -- queue is both what C1 asked for and what an
    # unwritten conversation gets -- so the assertion is on the default rather
    # than on the literal, and would still hold if the default moved.
    assert channel._mid_turn_mode("C2", "U1") == MID_TURN_DEFAULT


def test_a_platform_scope_settles_it_for_every_conversation(any_model):
    config = _configured(
        [{"match": {"channel": "slack"}, "delivery": {"mid_turn": "steer"}}]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    assert channel._mid_turn_mode("C-never-named", "U1") == slack_connect.MID_TURN_STEER


def test_a_conversation_overrides_the_platform_layer(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "delivery": {"mid_turn": "queue"}},
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"mid_turn": "cancel"},
            },
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    assert channel._mid_turn_mode("C1", "U1") == MID_TURN_CANCEL
    assert channel._mid_turn_mode("C2", "U1") == MID_TURN_QUEUE


def test_it_is_settled_per_key_like_everything_else(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "delivery": {"mid_turn": "steer"}},
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"prompt": "be terse"},
            },
        ]
    )

    # A conversation whose scope set only a prompt keeps the platform layer's
    # mid_turn rather than being read as having declined it.
    settled = settled_override(config, "C1")
    assert settled.prompt == "be terse"
    assert settled.mid_turn == slack_connect.MID_TURN_STEER


def test_a_rule_naming_a_sender_settles_it_for_that_person(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack", "chat": "C1"}, "delivery": {"mid_turn": "queue"}},
            {
                "match": {"channel": "slack", "chat": "C1", "user": ["U1"]},
                "delivery": {"mid_turn": "steer"},
            },
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    assert channel._mid_turn_mode("C1", "U1") == slack_connect.MID_TURN_STEER
    assert channel._mid_turn_mode("C1", "U2") == MID_TURN_QUEUE


def test_an_unrecognised_value_is_refused(any_model, refusal):
    error = refusal([{"match": {"channel": "slack"}, "delivery": {"mid_turn": "wait"}}])

    assert "scopes[0].delivery.mid_turn='wait' is not one of queue, cancel, steer" in error


def test_slack_declares_no_layer_zero_key_for_mid_turn():
    # No channels.slack key says what a mid-turn message does, so a scope is not
    # a second home for anything and declaring one would report a home an
    # operator could edit to no effect.
    assert channel_capabilities("slack").layer0_key("delivery", "mid_turn") is None


def test_slack_does_not_validate_the_value_itself():
    # The three words name mechanisms rather than Slack ids, so the schema owns
    # the vocabulary and a validator here would be a second copy of it.
    assert channel_capabilities("slack").validator("delivery", "mid_turn") is None


def test_the_summary_names_it_only_where_it_departs_from_the_default(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack", "chat": "C1"}, "delivery": {"mid_turn": "queue"}},
            {"match": {"channel": "slack", "chat": "C2"}, "delivery": {"prompt": "hi"}},
        ]
    )

    summary = describe_configured_channels(config)
    entries = [part for part in summary.split("; ") if " mode=[" in part]
    for_c1 = next(part for part in entries if " C1 " in f" {part} ")
    for_c2 = next(part for part in entries if part.startswith("C2 "))
    assert "mid_turn=queue" in for_c1
    # C2 settled nothing, and an unset key is absent rather than filled in with
    # the default, so nothing is printed for it. C1 asked for queue and is named
    # even though queue is now the default: the line reports what a scope
    # settled, not how it differs from the default.
    assert "mid_turn" not in for_c2


# --------------------------------------------------------------------------
# delivery.events
# --------------------------------------------------------------------------


def test_events_is_a_mapping_so_two_layers_each_name_a_family(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack"},
                "delivery": {"events": {"reaction": "context"}},
            },
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"pin": "context"}},
            },
        ]
    )

    assert settled_override(config, "C1").events == {
        "reaction": "context",
        "pin": "context",
    }
    # And the conversation that named nothing keeps only the platform layer.
    assert settled_override(config, "C9").events == {"reaction": "context"}


def test_a_conversation_can_turn_a_family_back_off(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack"},
                "delivery": {"events": {"reaction": "context", "member": "context"}},
            },
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"member": "off"}},
            },
        ]
    )

    assert settled_override(config, "C1").events == {
        "reaction": "context",
        "member": "off",
    }


def test_a_conversation_nobody_wrote_about_keeps_nothing(any_model):
    # Unset means off, so a deployment that never writes the key sees no change
    # whatever. Stated as the absence of the key rather than as a mapping of
    # offs, because the distinction is the contract: None means no layer spoke.
    config = _configured([{"match": {"channel": "slack"}, "delivery": {"mode": ["all"]}}])
    assert settled_override(config, "C1").events is None


def test_an_unknown_family_is_refused(any_model, refusal):
    error = refusal(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "context", "star": "context"}},
            }
        ]
    )

    assert "scopes[0].delivery.events names 'star'" in error
    assert "reaction/pin/member" in error


def test_a_disposition_that_is_not_one_of_the_three_is_refused(any_model, refusal):
    error = refusal(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "sometimes"}},
            }
        ]
    )

    assert "scopes[0].delivery.events sets reaction='sometimes'" in error
    # All three words named, so the operator's next edit is correct.
    assert "off or context or turn" in error


def test_turn_is_accepted_on_every_family_that_has_a_destination(
    warnings, any_model
):
    # Three of the four, each with a destination settled by what the event is
    # about: a reaction and a pin answer in the thread of the message they
    # name, a membership change answers at the top of the room it is about.
    # app_home has neither and is checked separately below.
    from belle_agent.platforms.slack.events import EVENT_TURN_FAMILIES

    wanted = {family: "turn" for family in sorted(EVENT_TURN_FAMILIES)}
    scopes = _scopes(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": dict(wanted)},
            }
        ],
        warn=lambda message, *args: warnings.append(message % args if args else message),
    )

    assert compose_section(scopes, channel="slack", chat="C1")["events"] == wanted
    assert warnings == []


def test_member_takes_turn_and_round_trips_to_the_connector(any_model):
    # A member event keeps its scope mapping through the connector's read gate.
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"member": "turn"}},
            }
        ]
    )

    assert settled_override(config, "C1").events == {"member": "turn"}


def test_app_home_takes_off_and_context_and_round_trips_to_the_connector(
    warnings, any_model
):
    # The two words this family has, written the way an operator writes them,
    # reaching the connector's read gate unchanged.
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat": "D1"},
                "delivery": {"events": {"app_home": "context", "reaction": "off"}},
            }
        ]
    )

    assert settled_override(config, "D1").events == {
        "app_home": "context",
        "reaction": "off",
    }


def test_the_word_still_needs_a_family_that_has_a_destination(any_model):
    from belle_agent.platforms.slack.events import (
        EVENT_FAMILIES,
        EVENT_FAMILY_APP_HOME,
        EVENT_TURN,
        EVENT_TURN_FAMILIES,
        disposition_holds,
    )

    assert set(EVENT_TURN_FAMILIES) < set(EVENT_FAMILIES)
    assert set(EVENT_FAMILIES) - set(EVENT_TURN_FAMILIES) == {EVENT_FAMILY_APP_HOME}
    assert not disposition_holds(EVENT_FAMILY_APP_HOME, EVENT_TURN)
    assert not disposition_holds("star", EVENT_TURN)


def test_turn_on_app_home_is_refused_and_the_refusal_says_why(any_model, refusal):
    error = refusal(
        [
            {
                "match": {"channel": "slack", "chat": "D1"},
                "delivery": {"events": {"reaction": "context", "app_home": "turn"}},
            }
        ]
    )

    assert "scopes[0].delivery.events sets app_home=turn" in error
    assert "cannot start a turn" in error
    # The reason, and it is this family's rather than a general one.
    assert "no message to answer" in error
    assert "splits on a payload field" in error
    assert "tab" in error
    # And what to write instead.
    assert "member" in error
    assert "Write context" in error


@pytest.mark.parametrize(
    "invalid",
    ["reaction", {"reaction": "later"}, {"app_home": "turn"}],
)
def test_an_invalid_event_rule_is_refused_beside_a_valid_one(invalid, any_model, refusal):
    error = refusal(
        [
            {"match": {"channel": "slack"}, "delivery": {"events": {"reaction": "context"}}},
            {"match": {"channel": "slack", "chat": "D1"}, "delivery": {"events": invalid}},
        ]
    )

    assert "scopes[1].delivery.events" in error
    assert "scopes[0]" not in error


def test_the_refusal_a_family_with_no_written_reason_would_get(
    any_model, monkeypatch, refusal
):
    from belle_agent.platforms.slack import events as slack_events_policy
    narrowed = frozenset({slack_events_policy.EVENT_FAMILY_REACTION})
    monkeypatch.setattr(slack_events_policy, "EVENT_TURN_FAMILIES", narrowed)
    monkeypatch.setattr(scope_capabilities, "EVENT_TURN_FAMILIES", narrowed)

    error = refusal(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "context", "member": "turn"}},
            }
        ]
    )

    assert "member=turn" in error
    assert "cannot start a turn" in error
    assert "nothing says where a turn woken by one of these would post" in error
    # And what to write instead.
    assert "reaction" in error
    assert "Write context" in error


def test_a_bare_word_where_a_mapping_is_wanted_is_refused(any_model, refusal):
    error = refusal(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": "context"},
            }
        ]
    )

    assert "scopes[0].delivery.events='context' is not a mapping of event families" in error


@pytest.mark.parametrize(
    "match",
    [
        {"channel": "slack", "chat": "C1", "user": ["U1"]},
        {"channel": "slack", "role": "operator"},
        {"channel": "slack", "chat": "C1", "not": {"user": ["U1"]}},
    ],
)
def test_events_on_an_identity_rule_is_refused(match, any_model, refusal):
    error = refusal(
        [{"match": match, "delivery": {"events": {"reaction": "context"}}}],
        people={"alice": {"slack": "U1"}},
        roles={"operator": ["alice"]},
    )

    assert "scopes[0].delivery.events cannot be in a rule that matches on" in error
    assert "{channel: slack}" in error


def test_events_on_the_conversation_alone_is_kept(any_model):
    scopes = _scopes(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "context"}},
            }
        ]
    )
    assert compose_section(scopes, channel="slack", chat="C1")["events"] == {
        "reaction": "context"
    }


def test_the_channel_reads_a_family_through_the_settled_override(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "context"}},
            }
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    assert channel._channel_event_disposition("C1", "reaction") == "context"
    assert channel._channel_event_disposition("C1", "pin") == "off"
    assert channel._channel_event_disposition("C9", "reaction") == "off"


# --------------------------------------------------------------------------
# app_home on a rule that can never see one
# --------------------------------------------------------------------------


def _warned(entries: Any, recorded: list[str]):
    return _scopes(
        entries,
        warn=lambda message, *args: recorded.append(message % args if args else message),
    )


@pytest.mark.parametrize("kind", ["channel", "group"])
def test_app_home_on_a_rule_that_is_not_a_dm_is_reported(kind, warnings, any_model):
    scopes = _warned(
        [
            {
                "match": {"channel": "slack", "chat_type": kind},
                "delivery": {"events": {"app_home": "context"}},
            }
        ],
        warnings,
    )

    said = " ".join(warnings)
    assert "app_home" in said
    assert kind in said
    # The reason, so the operator is not left to test it by hand.
    assert "direct message with the person who opened it" in said
    # And both ways out.
    assert "{chat_type: im}" in said
    assert "{channel: slack}" in said
    # Kept, not dropped: see the test below for why that matters.
    assert compose_section(scopes, channel="slack", chat="C1", chat_type=kind)[
        "events"
    ] == {"app_home": "context"}


def test_the_families_written_beside_it_are_not_taken_down_with_it(
    warnings, any_model
):
    scopes = _warned(
        [
            {
                "match": {"channel": "slack", "chat_type": "channel"},
                "delivery": {
                    "events": {"app_home": "context", "reaction": "turn"}
                },
            }
        ],
        warnings,
    )

    assert warnings
    assert compose_section(
        scopes, channel="slack", chat="C1", chat_type="channel"
    )["events"] == {"app_home": "context", "reaction": "turn"}


@pytest.mark.parametrize("kind", ["im", "mpim"])
def test_app_home_on_a_rule_that_can_be_a_dm_is_not_reported(kind, warnings, any_model):
    _warned(
        [
            {
                "match": {"channel": "slack", "chat_type": kind},
                "delivery": {"events": {"app_home": "context"}},
            }
        ],
        warnings,
    )

    assert warnings == []


def test_a_rule_naming_one_conversation_is_left_to_the_template(warnings, any_model):
    _warned(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"app_home": "context"}},
            }
        ],
        warnings,
    )

    assert warnings == []


def test_the_broadest_slack_rule_is_not_reported(warnings, any_model):
    # The place the family belongs: a rule naming no conversation matches
    # whichever one an event arrives in, App Home's direct message included.
    scopes = _warned(
        [
            {
                "match": {"channel": "slack"},
                "delivery": {"events": {"app_home": "context"}},
            }
        ],
        warnings,
    )

    assert warnings == []
    assert compose_section(scopes, channel="slack", chat="D1", chat_type="im")[
        "events"
    ] == {"app_home": "context"}


def test_the_other_three_families_are_never_reported_on_a_channel_rule(
    warnings, any_model
):
    from belle_agent.platforms.slack.events import (
        EVENT_FAMILIES,
        EVENT_FAMILY_APP_HOME,
    )

    others = [f for f in EVENT_FAMILIES if f != EVENT_FAMILY_APP_HOME]
    assert others

    _warned(
        [
            {
                "match": {"channel": "slack", "chat_type": "channel"},
                "delivery": {"events": {family: "context" for family in others}},
            }
        ],
        warnings,
    )

    assert warnings == []


def test_a_refused_mapping_is_not_also_cautioned(warnings, any_model, refusal):
    # One rule with one thing wrong with it says so once. The caution runs after
    # the validator and speaks about values that are kept, so a refused mapping
    # does not collect a second sentence about where it would have reached.
    error = refusal(
        [
            {
                "match": {"channel": "slack", "chat_type": "channel"},
                "delivery": {"events": {"app_home": "turn"}},
            }
        ]
    )

    assert "cannot start a turn" in error
    assert "direct message with the person who opened it" not in error
    assert not any("direct message with the person who opened it" in line for line in warnings)


# --------------------------------------------------------------------------
# chat_type: the kinds Slack has, and where the fact comes from
# --------------------------------------------------------------------------


def test_slack_declares_the_axis_and_its_four_kinds():
    capabilities = channel_capabilities("slack")

    assert capabilities is not None
    assert capabilities.populates(AXIS_CHAT_TYPE)
    assert capabilities.values_for(AXIS_CHAT_TYPE) == frozenset(
        {"channel", "group", "im", "mpim"}
    )


def test_a_kind_slack_does_not_have_is_refused_with_the_ones_it_does():
    with pytest.raises(BelleConfigError) as caught:
        _scopes([{"match": {"channel": "slack", "chat_type": "dm"}, "delivery": {"mode": ["all"]}}])

    assert "scopes[0].match.chat_type names dm, which slack does not have" in str(caught.value)
    assert "channel, group, im, mpim" in str(caught.value)


def test_a_rule_naming_a_kind_settles_only_for_that_kind(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "agent": {"model_name": "base"}},
            {
                "match": {"channel": "slack", "chat_type": "im"},
                "agent": {"model_name": "dm"},
            },
        ]
    )

    assert settled_override(config, "D1", chat_type="im").model_name == "dm"
    assert settled_override(config, "C1", chat_type="channel").model_name == "base"
    # No kind is not every kind. A caller with none gets the layer below, which
    # is the answer the settled maps were composed with.
    assert settled_override(config, "D1").model_name == "base"


def test_a_kind_rule_is_folded_under_a_rule_naming_the_conversation(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat_type": "im"},
                "delivery": {"prompt": "every dm"},
            },
            {
                "match": {"channel": "slack", "chat": "D1"},
                "delivery": {"prompt": "this dm"},
            },
        ]
    )

    assert settled_override(config, "D1", chat_type="im").prompt == "this dm"
    assert settled_override(config, "D9", chat_type="im").prompt == "every dm"


def test_a_kind_rule_and_a_sender_rule_meet_in_one_fold(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat_type": "im"},
                "delivery": {"prompt": "in a dm"},
            },
            {
                "match": {"channel": "slack", "user": ["U1"]},
                "agent": {"model_name": "theirs"},
            },
        ]
    )

    settled = settled_override(config, "D1", user_id="U1", chat_type="im")
    assert settled.prompt == "in a dm"
    assert settled.model_name == "theirs"


def test_a_config_with_no_kind_rule_still_takes_the_fast_path(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"prompt": "be terse"},
            }
        ]
    )

    for kind in ("", "channel", "im", "mpim"):
        assert settled_override(config, "C1", chat_type=kind).prompt == "be terse"


def test_the_triggers_of_a_conversation_can_be_set_by_its_kind(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat_type": "mpim"},
                "delivery": {"mode": ["all"]},
            }
        ],
        group_chat_mode="mention",
    )

    assert channel_triggers(
        config, "G1", group_chat_mode="mention", chat_type="mpim"
    ) == frozenset({"all"})
    # Every other kind stays on group_chat_mode, and so does a call with no kind
    # to offer -- the startup summary is one.
    assert channel_triggers(
        config, "C1", group_chat_mode="mention", chat_type="channel"
    ) == frozenset({"mention"})
    assert channel_triggers(config, "C1", group_chat_mode="mention") == frozenset(
        {"mention"}
    )


def test_one_kind_rule_can_set_mode_reply_and_a_prompt_together(any_model):
    said: list[str] = []
    scopes = _scopes(
        [
            {
                "match": {"channel": "slack", "chat_type": "channel"},
                "delivery": {
                    "mode": ["all"],
                    "reply": "optional",
                    "prompt_append": "Answer only when you have something to add.",
                },
            }
        ],
        warn=lambda message, *args: said.append(message % args if args else message),
    )

    assert said == [], said
    settled = compose_section(
        scopes, channel="slack", chat="C1", chat_type="channel"
    )
    assert settled["mode"] == frozenset({"all"})
    assert settled["reply"] == "optional"
    # prompt_append folds into prompt: the append form is how a scope adds to
    # the layer above it, and with no layer above it is the whole prompt.
    assert settled["prompt"] == "Answer only when you have something to add."


def test_a_kind_rule_settles_session_and_reply_through_the_connector(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat_type": "mpim"},
                "delivery": {"session": "channel", "reply": "optional"},
            }
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    assert channel._session_scope("G1", "", "mpim") == "channel"
    assert channel._session_scope("G1", "") != "channel"
    assert channel._channel_reply_is_optional("G1", "mpim")
    assert not channel._channel_reply_is_optional("G1")


def test_the_outbound_half_reads_the_kind_off_the_reply(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat_type": "channel"},
                "delivery": {"reply": "optional"},
            }
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    def _reply(metadata: dict) -> Any:
        """A reply shaped the way the gateway builds one.

        The gateway merges the request's metadata into every response, which is
        what puts the inbound half's record of the kind on the outbound message.
        """
        return Message(
            id="m1",
            type="event",
            channel_id="slack",
            session_id="s1",
            params={},
            timestamp=0.0,
            ok=True,
            metadata=metadata,
        )

    assert channel._reply_was_optional(
        _reply(
            {
                "slack_channel_type": "channel",
                REPLY_OPTIONAL_KEY: True,
            }
        ),
        "C1",
    )

    # No kind on the reply is no licence: the text is posted, which is the
    # direction every failure of this predicate is kept pointing in.
    assert not channel._reply_was_optional(
        _reply({REPLY_OPTIONAL_KEY: True}), "C1"
    )


def test_a_resumed_turn_reads_the_same_kind_its_first_dispatch_did(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "agent": {"model_name": "base"}},
            {
                "match": {"channel": "slack", "chat_type": "im"},
                "agent": {"model_name": "dm"},
            },
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    first_dispatch = channel._channel_model_name("D1", "U1", "im")
    channel._admission.turn_initiators = {}
    channel._remember_turn_initiator(
        "sess-1", "U1", "req-1", is_dm=True, chat_type="im"
    )
    entry = channel.turn_initiator("sess-1")
    assert entry is not None
    resumed = channel._channel_model_name("D1", entry.user_id, entry.chat_type)

    assert first_dispatch == resumed == "dm"


def test_a_resume_with_no_kind_on_record_is_matched_by_no_kind_rule(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "agent": {"model_name": "base"}},
            {
                "match": {"channel": "slack", "chat_type": "channel"},
                "agent": {"model_name": "rooms"},
            },
        ]
    )
    channel = SlackChannel(config, slack_connect.RobotMessageRouter())

    assert channel._channel_model_name("C1", "U1", "") == "base"


# --------------------------------------------------------------------------
# The workspace axis
# --------------------------------------------------------------------------


def test_slack_declares_the_workspace_axis():
    # Declared once the gateway builds one channel per credential block: two
    # instances answer for two teams, and the team id is what tells them apart.
    assert channel_capabilities("slack").populates("workspace")


def test_a_rule_naming_a_workspace_settles_only_for_that_workspace(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "agent": {"model_name": "base"}},
            {
                "match": {"channel": "slack", "workspace": "T000000AAAA"},
                "agent": {"model_name": "ours"},
            },
        ]
    )

    assert settled_override(config, "C1", workspace="T000000AAAA").model_name == "ours"
    # A second installation is a second organisation, and a rule written for one
    # of them does not settle the other one's requests.
    assert settled_override(config, "C1", workspace="T000000ZZZZ").model_name == "base"
    # No workspace is not every workspace. A caller with none -- the startup
    # summary, a channel whose auth.test has not answered -- gets the layer
    # below, which is the answer the settled maps were composed with.
    assert settled_override(config, "C1").model_name == "base"


def test_a_workspace_rule_is_folded_over_a_rule_naming_the_conversation(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "workspace": "T000000AAAA"},
                "delivery": {"prompt": "house style"},
            },
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"prompt": "this room"},
            },
        ]
    )

    assert (
        settled_override(config, "C1", workspace="T000000AAAA").prompt == "house style"
    )
    # In the other installation the workspace rule does not fire at all, so the
    # rule about the room is the only layer left.
    assert settled_override(config, "C1", workspace="T000000ZZZZ").prompt == "this room"

    # Naming both is how a room is written back on top of its own workspace.
    config = _configured(
        [
            {
                "match": {"channel": "slack", "workspace": "T000000AAAA"},
                "delivery": {"prompt": "house style"},
            },
            {
                "match": {
                    "channel": "slack",
                    "workspace": "T000000AAAA",
                    "chat": "C1",
                },
                "delivery": {"prompt": "this room"},
            },
        ]
    )

    assert settled_override(config, "C1", workspace="T000000AAAA").prompt == "this room"
    assert (
        settled_override(config, "C9", workspace="T000000AAAA").prompt == "house style"
    )


def test_a_workspace_rule_and_a_kind_rule_meet_in_one_fold(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "workspace": "T000000AAAA"},
                "delivery": {"prompt": "house style"},
            },
            {
                "match": {"channel": "slack", "chat_type": "im"},
                "agent": {"model_name": "dm"},
            },
        ]
    )

    settled = settled_override(config, "D1", chat_type="im", workspace="T000000AAAA")
    assert settled.prompt == "house style"
    assert settled.model_name == "dm"


def test_a_config_with_no_workspace_rule_still_takes_the_fast_path(any_model):
    """What keeps the axis free for every deployment that has not written one."""
    config = _configured(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"prompt": "be terse"},
            }
        ]
    )

    for team in ("", "T000000AAAA", "T000000ZZZZ"):
        assert settled_override(config, "C1", workspace=team).prompt == "be terse"


def test_the_triggers_of_a_conversation_can_be_set_by_its_workspace(any_model):
    config = _configured(
        [
            {
                "match": {"channel": "slack", "workspace": "T000000AAAA"},
                "delivery": {"mode": ["all"]},
            }
        ],
        group_chat_mode="mention",
    )

    assert channel_triggers(
        config, "C1", group_chat_mode="mention", workspace="T000000AAAA"
    ) == frozenset({"all"})
    assert channel_triggers(
        config, "C1", group_chat_mode="mention", workspace="T000000ZZZZ"
    ) == frozenset({"mention"})


def test_the_workspace_is_the_connections_team_and_not_the_events(any_model):
    config = _configured(
        [
            {"match": {"channel": "slack"}, "agent": {"model_name": "base"}},
            {
                "match": {"channel": "slack", "workspace": "T000000AAAA"},
                "agent": {"model_name": "ours"},
            },
        ]
    )
    channel = SlackChannel.__new__(SlackChannel)
    channel.config = config
    channel._identity = SlackWorkspaceIdentity()
    channel._identity.team_id = "T000000AAAA"

    # No event is consulted and none is passed: the model is settled from what
    # the connection knows about itself.
    assert channel._channel_model_name("C1", "U1", "channel") == "ours"

    # The same channel before auth.test has answered, and after stop: no team is
    # known, so the rule naming one does not fire.
    channel._identity.team_id = ""
    assert channel._channel_model_name("C1", "U1", "channel") == "base"
