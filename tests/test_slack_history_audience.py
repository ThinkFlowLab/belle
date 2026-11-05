"""Slack audience decisions and their metadata contract."""

from typing import Any

import pytest

import belle_agent.platforms.slack.history_policy as policy_module
from belle_agent.platforms.slack.history_policy import (
    ENFORCE_AUDIENCE_DEFAULT,
    KEY_ENFORCE_AUDIENCE,
    METADATA_ASKER_KEY,
    METADATA_ENFORCE_AUDIENCE_KEY,
    Audience,
    audience_metadata,
    audience_newcomers,
    audience_within,
    conversation_reaches_outside_workspace,
    effective_audience,
    read_permitted,
    resolve_enforce_audience,
    write_permitted,
)

_BOT = "U0BOT"
_ASKER = "U0ASKER"
_OTHER = "U0OTHER"
_OUTSIDE = "U0OUTSIDE"

_PUBLIC_RECORD = {"is_channel": True, "is_private": False}
_PRIVATE_RECORD = {"is_channel": True, "is_private": True}
_GROUP_DM_RECORD = {"is_mpim": True, "is_private": True}
_DM_RECORD = {"is_im": True, "is_private": True}

_KINDS = {
    "public": _PUBLIC_RECORD,
    "private": _PRIVATE_RECORD,
    "group_dm": _GROUP_DM_RECORD,
    "dm": _DM_RECORD,
}


def test_a_public_channel_reaches_the_whole_workspace() -> None:
    answer = effective_audience(_PUBLIC_RECORD, [_BOT])
    assert answer == Audience(workspace_wide=True, members=frozenset(), external=False)


@pytest.mark.parametrize("kind", ["private", "group_dm", "dm"])
def test_every_other_kind_reaches_the_people_in_it(kind: str) -> None:
    answer = effective_audience(_KINDS[kind], [_BOT, _ASKER])
    assert answer.workspace_wide is False
    assert answer.members == frozenset({_BOT, _ASKER})
    assert answer.external is False


def test_a_record_that_establishes_nothing_is_not_workspace_wide() -> None:
    answer = effective_audience({"is_channel": True}, [_BOT])
    assert answer.workspace_wide is False
    assert answer.members == frozenset({_BOT})


def test_a_direct_message_is_never_public_whatever_the_private_flag_says() -> None:
    answer = effective_audience({"is_im": True, "is_private": False}, [_BOT, _ASKER])
    assert answer.workspace_wide is False
    assert answer.members == frozenset({_BOT, _ASKER})


def test_nothing_at_all_is_an_empty_member_list_rather_than_the_workspace() -> None:
    assert effective_audience(None) == Audience(False, frozenset(), False)
    assert effective_audience("not a mapping") == Audience(False, frozenset(), False)


@pytest.mark.parametrize(
    "flag",
    ["is_ext_shared", "is_pending_ext_shared", "is_org_shared", "is_shared"],
)
def test_each_external_marker_alone_breaks_the_workspace_bound(flag: str) -> None:
    record = {**_PUBLIC_RECORD, flag: True}
    assert conversation_reaches_outside_workspace(record) is True
    answer = effective_audience(record, [_BOT, _OUTSIDE])
    assert answer.workspace_wide is False
    assert answer.members == frozenset({_BOT, _OUTSIDE})
    assert answer.external is True


def test_an_absent_marker_reads_as_not_shared() -> None:
    assert conversation_reaches_outside_workspace(_PUBLIC_RECORD) is False
    assert conversation_reaches_outside_workspace(None) is False
    assert effective_audience(_PUBLIC_RECORD).workspace_wide is True


def test_a_private_conversation_can_also_reach_outside_the_workspace() -> None:
    answer = effective_audience({**_PRIVATE_RECORD, "is_ext_shared": True}, [_BOT, _OUTSIDE])
    assert answer.workspace_wide is False
    assert answer.external is True


def _audience(kind: str, members: set[str]) -> Audience:
    return effective_audience(_KINDS[kind], members)


@pytest.mark.parametrize("target_kind", list(_KINDS))
def test_a_public_source_may_read_only_a_public_target(target_kind: str) -> None:
    source = _audience("public", {_BOT, _ASKER})
    target = _audience(target_kind, {_BOT, _ASKER, _OTHER})
    assert read_permitted(source, target) is (target_kind == "public")


@pytest.mark.parametrize("source_kind", ["private", "group_dm", "dm"])
def test_a_public_target_is_readable_from_any_bounded_source(source_kind: str) -> None:
    source = _audience(source_kind, {_BOT, _ASKER})
    target = _audience("public", set())
    assert read_permitted(source, target) is True


@pytest.mark.parametrize("source_kind", ["private", "group_dm", "dm"])
@pytest.mark.parametrize("target_kind", ["private", "group_dm", "dm"])
def test_a_bounded_pair_takes_the_subset_test(source_kind: str, target_kind: str) -> None:
    source = _audience(source_kind, {_BOT, _ASKER})
    inside = _audience(target_kind, {_BOT, _ASKER, _OTHER})
    outside = _audience(target_kind, {_BOT, _OTHER})
    assert read_permitted(source, inside) is True
    assert read_permitted(source, outside) is False


def test_a_conversation_reads_itself() -> None:
    for kind in _KINDS:
        same = _audience(kind, {_BOT, _ASKER})
        assert read_permitted(same, same) is True


@pytest.mark.parametrize("target_kind", list(_KINDS))
def test_a_public_source_may_write_anywhere(target_kind: str) -> None:
    source = _audience("public", {_BOT, _ASKER})
    target = _audience(target_kind, {_BOT, _OTHER})
    assert write_permitted(source, target) is True


@pytest.mark.parametrize("source_kind", ["private", "group_dm", "dm"])
def test_a_public_target_always_asks_from_a_bounded_source(source_kind: str) -> None:
    source = _audience(source_kind, {_BOT, _ASKER})
    target = _audience("public", set())
    assert write_permitted(source, target) is False


@pytest.mark.parametrize("source_kind", ["private", "group_dm", "dm"])
@pytest.mark.parametrize("target_kind", ["private", "group_dm", "dm"])
def test_a_bounded_write_pair_takes_the_inverted_subset_test(source_kind: str, target_kind: str) -> None:
    source = _audience(source_kind, {_BOT, _ASKER, _OTHER})
    inside = _audience(target_kind, {_BOT, _ASKER})
    outside = _audience(target_kind, {_BOT, _OUTSIDE})
    assert write_permitted(source, inside) is True
    assert write_permitted(source, outside) is False


def _external(kind: str, members: set[str]) -> Audience:
    return effective_audience({**_KINDS[kind], "is_ext_shared": True}, members)


def test_an_external_source_may_not_read_a_public_target() -> None:
    source = _external("public", {_BOT, _OUTSIDE})
    assert read_permitted(source, _audience("public", set())) is False


def test_an_external_target_may_not_be_read_into_a_public_source() -> None:
    source = _audience("public", {_BOT})
    assert read_permitted(source, _external("public", {_BOT, _OUTSIDE})) is False


def test_a_public_source_may_not_write_into_an_external_target() -> None:
    source = _audience("public", {_BOT})
    assert write_permitted(source, _external("public", {_BOT, _OUTSIDE})) is False


def test_an_external_source_may_not_be_written_out_of_into_a_public_target() -> None:
    source = _external("public", {_BOT, _OUTSIDE})
    assert write_permitted(source, _audience("public", set())) is False


def test_two_external_member_lists_still_compare_on_ids() -> None:
    source = _external("private", {_BOT, _OUTSIDE})
    inside = _external("private", {_BOT, _OUTSIDE, _OTHER})
    assert read_permitted(source, inside) is True
    assert write_permitted(inside, source) is True


def test_the_workspace_audience_is_inside_nothing_smaller() -> None:
    workspace = Audience(workspace_wide=True, members=frozenset(), external=False)
    listed = Audience(workspace_wide=False, members=frozenset({_BOT}), external=False)
    assert audience_within(workspace, listed) is False
    assert audience_within(workspace, workspace) is True


def test_newcomers_are_named_where_both_sides_are_member_lists() -> None:
    inner = _audience("private", {_BOT, _ASKER, _OTHER})
    outer = _audience("private", {_BOT, _ASKER})
    assert audience_newcomers(inner, outer) == (_OTHER,)


def test_newcomers_are_empty_where_the_test_passes() -> None:
    inner = _audience("private", {_BOT})
    outer = _audience("private", {_BOT, _ASKER})
    assert audience_newcomers(inner, outer) == ()


def test_a_workspace_audience_on_either_side_names_nobody() -> None:
    workspace = Audience(workspace_wide=True, members=frozenset(), external=False)
    listed = _audience("private", {_BOT})
    assert audience_newcomers(workspace, listed) is None
    assert audience_newcomers(listed, workspace) is None


@pytest.fixture(autouse=True)
def _forget_the_startup_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy_module, "_WARNED_AUDIENCE_UNENFORCED", False)


def test_a_deployment_that_wrote_nothing_holds_the_bound() -> None:
    assert ENFORCE_AUDIENCE_DEFAULT is True
    assert resolve_enforce_audience({}) is True
    assert resolve_enforce_audience(None) is True
    assert resolve_enforce_audience({KEY_ENFORCE_AUDIENCE: None}) is True
    assert resolve_enforce_audience({KEY_ENFORCE_AUDIENCE: ""}) is True


@pytest.mark.parametrize("written", [True, "true", "yes", "on", "1", "TRUE"])
def test_every_on_spelling_holds_the_bound(written: Any) -> None:
    said: list[tuple] = []
    assert resolve_enforce_audience({KEY_ENFORCE_AUDIENCE: written}, warn=lambda *a: said.append(a)) is True
    assert said == []


@pytest.mark.parametrize("written", [False, "false", "no", "off", "0", "FALSE"])
def test_every_off_spelling_lifts_the_bound_and_warns(written: Any) -> None:
    said: list[tuple] = []
    assert resolve_enforce_audience({KEY_ENFORCE_AUDIENCE: written}, warn=lambda *a: said.append(a)) is False
    assert len(said) == 1
    assert KEY_ENFORCE_AUDIENCE in said[0]


def test_the_startup_warning_is_said_once(monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[tuple] = []
    conf = {KEY_ENFORCE_AUDIENCE: False}
    for _ in range(5):
        assert resolve_enforce_audience(conf, warn=lambda *a: said.append(a)) is False
    assert len(said) == 1


def test_an_unreadable_value_keeps_the_bound_and_says_so() -> None:
    said: list[tuple] = []
    assert resolve_enforce_audience({KEY_ENFORCE_AUDIENCE: "maybe"}, warn=lambda *a: said.append(a)) is True
    assert len(said) == 1


def test_the_metadata_names_are_the_ones_the_design_settled() -> None:
    assert METADATA_ENFORCE_AUDIENCE_KEY == "slack_enforce_audience"
    assert METADATA_ASKER_KEY == "slack_user_id"
    assert KEY_ENFORCE_AUDIENCE == "enforce_audience"


def test_the_stamp_is_a_boolean() -> None:
    assert audience_metadata(True) == {METADATA_ENFORCE_AUDIENCE_KEY: True}
    assert audience_metadata(False) == {METADATA_ENFORCE_AUDIENCE_KEY: False}
