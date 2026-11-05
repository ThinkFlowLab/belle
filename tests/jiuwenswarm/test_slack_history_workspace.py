"""Slack workspace selection and session identity."""

from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm.slack_routing import slack_session_id
from belle_agent.platforms.slack.history import SlackWorkspaceClients
from belle_agent.platforms.slack.history_policy import (
    METADATA_TEAM_KEY,
    SlackWorkspaceUnresolved,
    select_slack_workspace,
    slack_workspace_blocks,
)
from belle_agent.platforms.slack.identity import (
    chat_type_from_chat_id,
    click_channel_id,
    click_target,
    event_chat_type,
    event_team_id,
    slack_team_id_from_session,
)

FLAT = {
    "bot_token": "xoxb-one",
    "app_token": "xapp-one",
    "search_enabled": True,
    "history": "members",
    "history_max_api_calls": 40,
}

TWO = {
    "search_enabled": True,
    "history": "members",
    "workspaces": [
        {"bot_token": "xoxb-a", "app_token": "xapp-a"},
        {"bot_token": "xoxb-b", "app_token": "xapp-b", "default_channel_id": "C0B"},
    ],
}

IDENTITIES = {"xoxb-a": "T0AAA", "xoxb-b": "T0BBB"}


def _identify(token: str) -> str | None:
    return IDENTITIES.get(token)


def test_a_flat_mapping_is_returned_unchanged_whatever_is_asked_of_it() -> None:
    assert select_slack_workspace(FLAT) is FLAT
    assert select_slack_workspace(FLAT, "") is FLAT
    assert select_slack_workspace(FLAT, "T0AAA") is FLAT
    assert select_slack_workspace(FLAT, "T0ZZZ", team_of_token=_identify) is FLAT


@pytest.mark.asyncio
async def test_resolver_reads_only_slack_settings() -> None:
    assert await SlackWorkspaceClients(config_loader=lambda: FLAT).settings_for(None) is FLAT
    assert await SlackWorkspaceClients(config_loader=dict).settings_for(None) == {}
    assert await SlackWorkspaceClients(config_loader=lambda: "nonsense").settings_for(None) == {}


def test_an_empty_workspaces_list_is_read_as_no_list_at_all() -> None:
    upgraded = dict(FLAT, workspaces=[])
    assert slack_workspace_blocks(upgraded) == (upgraded,)
    assert select_slack_workspace(upgraded, "T0AAA") is upgraded


def test_one_written_block_is_overlaid_onto_the_connector_wide_keys() -> None:
    one = {"search_enabled": True, "workspaces": [{"bot_token": "xoxb-a"}]}
    resolved = select_slack_workspace(one, "T0ZZZ")
    assert resolved["bot_token"] == "xoxb-a"
    assert resolved["search_enabled"] is True


def test_a_request_is_served_by_the_block_installed_in_its_team() -> None:
    a = select_slack_workspace(TWO, "T0AAA", team_of_token=_identify)
    b = select_slack_workspace(TWO, "T0BBB", team_of_token=_identify)
    assert a["bot_token"] == "xoxb-a"
    assert b["bot_token"] == "xoxb-b"
    assert b["default_channel_id"] == "C0B"
    assert a.get("default_channel_id", "") == ""
    assert a["history"] == b["history"] == "members"


def test_a_team_no_block_answers_to_is_refused_rather_than_served() -> None:
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(TWO, "T0CCC", team_of_token=_identify)
    assert raised.value.code == "slack_workspace_unknown"
    assert "T0CCC" in raised.value.detail


def test_a_request_naming_no_workspace_is_refused_once_there_are_several() -> None:
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(TWO, "", team_of_token=_identify)
    assert raised.value.code == "slack_request_names_no_workspace"
    assert METADATA_TEAM_KEY in raised.value.detail


def test_a_token_nobody_has_identified_yet_matches_nothing() -> None:
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(TWO, "T0AAA", team_of_token=lambda _token: None)
    assert raised.value.code == "slack_workspace_unknown"
    with pytest.raises(SlackWorkspaceUnresolved):
        select_slack_workspace(TWO, "T0AAA")


def test_a_workspaces_key_that_is_not_a_list_yields_no_install() -> None:
    broken = dict(FLAT, workspaces="nonsense")
    assert slack_workspace_blocks(broken) == ()
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(broken, "T0AAA", team_of_token=_identify)
    assert raised.value.code == "slack_workspace_unconfigured"


def test_a_malformed_entry_is_dropped_and_the_others_still_serve() -> None:
    mixed = {
        "workspaces": [
            "not a mapping",
            {"bot_token": "xoxb-a"},
            {"bot_token": "xoxb-b"},
        ]
    }
    assert len(slack_workspace_blocks(mixed)) == 2
    resolved = select_slack_workspace(mixed, "T0BBB", team_of_token=_identify)
    assert resolved["bot_token"] == "xoxb-b"


def test_a_written_list_wins_over_a_top_level_pair_left_beside_it() -> None:
    mixed = dict(FLAT, workspaces=[{"bot_token": "xoxb-b"}, {"bot_token": "xoxb-a"}])
    resolved = select_slack_workspace(mixed, "T0AAA", team_of_token=_identify)
    assert resolved["bot_token"] == "xoxb-a"
    assert resolved["app_token"] == "xapp-one"


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        ("slack_T0AAA_C0AAA", "T0AAA"),
        ("slack_T0AAA_C0AAA_1700000000.000100", "T0AAA"),
        ("slack_T0AAA", ""),
        ("slack_", ""),
        ("wechat_T0AAA_C0AAA", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_the_team_is_read_off_a_slack_session_id(session_id: Any, expected: str) -> None:
    assert slack_team_id_from_session(session_id) == expected


def test_chat_type_is_only_inferred_for_dm_ids() -> None:
    assert [chat_type_from_chat_id(chat) for chat in ("D1", "C1", "G1", "")] == ["im", "", "", ""]


def test_event_chat_type_needs_an_explicit_value() -> None:
    assert event_chat_type({"channel_type": " mpim "}) == "mpim"
    assert event_chat_type({"channel": "C1"}) == event_chat_type(None) == ""


def test_slack_session_id_scopes_workspaces_and_conversations() -> None:
    args = {"channel_id": "C1", "user_id": "U1", "root_thread_ts": "1710.1"}
    assert slack_session_id(team_id="T1", is_dm=False, **args) == "slack_T1_C1_1710.1"
    assert slack_session_id(team_id="T1", is_dm=False, channel_wide=True, **args) == "slack_T1_C1"
    assert slack_session_id(team_id="T1", is_dm=True, **args) == "slack_T1_C1_U1"
    assert slack_session_id(team_id=" ", is_dm=False, **args) == ""


def test_click_identity_reads_both_slack_payload_shapes() -> None:
    body = {"team": {"id": "T1"}, "channel": {"id": "C1"}, "message": {"ts": "1710.1"}}
    assert event_team_id(body) == "T1"
    assert click_target(body) == ("C1", "1710.1")
    fallback = {"team_id": "T2", "container": {"channel_id": "C2", "message_ts": "1710.2"}}
    assert event_team_id(fallback) == "T2"
    assert click_channel_id(fallback) == "C2"
    assert click_target(fallback) == ("C2", "1710.2")
    assert event_team_id({}, {"team": "T3"}) == "T3"


@pytest.mark.asyncio
async def test_unreadable_live_config_fails_closed() -> None:
    def broken_loader() -> dict[str, Any]:
        raise RuntimeError("config unavailable")

    assert await SlackWorkspaceClients(config_loader=broken_loader).settings_for(None) == {}
    assert select_slack_workspace(TWO, "T0AAA", team_of_token=_identify)["bot_token"] == "xoxb-a"
