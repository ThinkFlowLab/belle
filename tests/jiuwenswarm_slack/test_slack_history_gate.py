"""The gate on reading a Slack conversation other than the one asked in.

The rule under test, for a request made in conversation ``S`` about conversation
``T``, is

    effective_audience(S) subset-of effective_audience(T)

where ``effective_audience`` answers the whole workspace for a public channel and
the member list for everything else. Read it as *nobody S reaches learns anything
they could not already learn*, because the answer lands in S in front of all of
them.

Two things follow and both are tested here. A public target is readable from any
bounded source, because anybody in the workspace could join it alone. A public
source may read only another public channel, because the answer it posts is
readable by the whole workspace.

The member-list half includes the asker. Anything the gate cannot establish is
a refusal.

The rule itself, as a function with no Slack in it, is in
``test_slack_audience_rule.py``.
"""

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
import jiuwenswarm
from belle_agent.integrations.jiuwenswarm.slack_tool_context import METADATA_ORIGIN_KEY, ORIGIN_CRON_JOB
from belle_agent.platforms.slack.history_policy import (
    METADATA_ENFORCE_AUDIENCE_KEY,
)

import jiuwenswarm.common.config as config_module
from belle_agent.integrations.jiuwenswarm import slack_history
from belle_agent.integrations.jiuwenswarm.slack_history import SlackHistoryToolkit
from slack_fakes import FakeSlackServerError as _FakeSlackError
from slack_log_capture import captured_logs as _captured

# History reads log from Belle, not from the JiuwenSwarm adapter.
_LOGGER_NAME = SlackHistoryToolkit.read_slack_conversation.__module__

_ORIGIN = "C0ORIGIN01"
_TARGET = "C0TARGET01"
_PUBLIC = "C0PUBLIC01"
_DM = "D0ALICE001"
_ALICE = "U-ALICE"
_BOB = "U-BOB"
_BOT = "U-BOT"
_INTEGRATION = "B-PAGERDUTY"
# A roomful of people, for the refusal that reports a count rather than a list.
_CROWD = tuple(f"U-CROWD{index:02d}" for index in range(11))
_MPIM = "G0GROUP001"


def _warnings(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.levelno >= logging.WARNING]


class _Workspace:
    """A fake Slack with a membership list and one message per conversation.

    Deliberately not a scripted response queue. The gate makes a different
    number of calls depending on which branch refuses, and a queue would make
    every test assert the call count by accident.
    """

    def __init__(
        self,
        *,
        members: "dict[str, set[str]] | None" = None,
        public: "set[str] | None" = None,
        mpim: "set[str] | None" = None,
        guests: "set[str] | None" = None,
        single_channel_guests: "set[str] | None" = None,
        applications: "set[str] | None" = None,
        users: "dict[str, dict[str, Any]] | None" = None,
        fail: "dict[str, str] | None" = None,
        fail_members: "dict[str, str] | None" = None,
        members_pages: "dict[str, list[dict[str, Any]]] | None" = None,
    ) -> None:
        self.members = members or {}
        self.public = public or set()
        self.mpim = mpim or set()
        # What ``users.info`` says about a person. The three named sets are the
        # three answers that matter to the gate; ``users`` is the escape hatch
        # for a record that answers something else, or nothing.
        self.guests = guests or set()
        self.single_channel_guests = single_channel_guests or set()
        self.applications = applications or set()
        self.users = users or {}
        self.fail = fail or {}
        # Keyed by conversation rather than by method, so a test can decline the
        # membership of one room and not the other. The gate reads the source's
        # first, and a whole-method failure would never reach the target's.
        self.fail_members = fail_members or {}
        self.members_pages = members_pages or {}
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls[method].append(kwargs)
        if method in self.fail:
            raise _FakeSlackError(self.fail[method])
        if method == "conversations_members":
            error = self.fail_members.get(str(kwargs.get("channel") or ""))
            if error:
                raise _FakeSlackError(error)

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self._record("auth_test", kwargs)
        return {"user_id": _BOT, "bot_id": "B-SELF", "url": "https://x.slack.com/"}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", kwargs)
        channel = str(kwargs["channel"])
        if channel.startswith("D"):
            return {"channel": {"id": channel, "is_im": True, "is_private": True}}
        if channel in self.mpim:
            return {"channel": {"id": channel, "is_mpim": True, "is_private": True}}
        return {
            "channel": {
                "id": channel,
                "is_channel": True,
                "is_private": channel not in self.public,
            }
        }

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("users_info", kwargs)
        user_id = str(kwargs["user"])
        if user_id in self.users:
            return {"ok": True, "user": self.users[user_id]}
        if user_id in self.applications:
            # What Slack sends for an app: it says the account is a bot and
            # says nothing at all about guest status, which is the shape the
            # gate has to be right about.
            return {"ok": True, "user": {"id": user_id, "is_bot": True}}
        return {
            "ok": True,
            "user": {
                "id": user_id,
                "is_bot": False,
                "is_restricted": (
                    user_id in self.guests or user_id in self.single_channel_guests
                ),
                "is_ultra_restricted": user_id in self.single_channel_guests,
            },
        }

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_members", kwargs)
        channel = str(kwargs["channel"])
        scripted = self.members_pages.get(channel)
        if scripted is not None:
            cursor = str(kwargs.get("cursor") or "")
            return scripted[0 if not cursor else int(cursor)]
        return {"members": sorted(self.members.get(channel, set()))}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_history", kwargs)
        channel = str(kwargs["channel"])
        return {
            "messages": [
                {"ts": "199999.000100", "user": _ALICE, "text": f"hello from {channel}"}
            ]
        }

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_replies", kwargs)
        return {"messages": []}


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bounds this toolkit reads. Never the policy: that arrives stamped."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _metadata(enforced: bool = True, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slack_channel_id": _ORIGIN,
        "slack_channel_type": "channel",
        "slack_user_id": _ALICE,
        METADATA_ENFORCE_AUDIENCE_KEY: enforced,
    }
    base.update(overrides)
    return base


def _toolkit(
    workspace: _Workspace,
    enforced: bool = True,
    *,
    lookups: int = 0,
    monotonic: "Any | None" = None,
    **metadata: Any,
) -> SlackHistoryToolkit:
    """One toolkit, with the directory-lookup budget an argument.

    Zero by default, so that no display name is resolved and a ``users.info``
    call in the record would be somebody's mistake. The gate itself spends none
    of this budget: the audience rule reads member lists and conversation
    records, and asks the directory nothing.
    """
    clock = {"monotonic": monotonic} if monotonic is not None else {}
    return SlackHistoryToolkit(
        metadata=_metadata(enforced, **metadata),
        client=workspace,
        now=lambda: 200_000.0,
        max_user_lookups=lookups,
        **clock,
    )


def _read(toolkit: SlackHistoryToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.read_slack_conversation(**kwargs)))


def _card(toolkit: SlackHistoryToolkit) -> Any:
    tool = toolkit.get_tools()[0]
    return getattr(tool, "card", None) or tool._card


# ── 1. The audience decision, and one card shape ─────────────────────────────


def test_a_request_with_no_audience_decision_is_refused() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    metadata = _metadata()
    del metadata[METADATA_ENFORCE_AUDIENCE_KEY]
    toolkit = SlackHistoryToolkit(
        metadata=metadata, client=workspace, now=lambda: 200_000.0
    )
    result = _read(toolkit)
    assert result["error"] == "audience_unsettled"
    assert not workspace.calls


def test_the_card_always_declares_a_target() -> None:
    card = _card(_toolkit(_Workspace()))
    assert "chat_id" in card.input_params["properties"]


def test_reading_this_conversation_costs_no_gate_call_at_all() -> None:
    """The default path. No target named, so there is nothing to compare."""
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    result = _read(_toolkit(workspace))

    assert result["ok"] is True
    assert result["chat_id"] == _ORIGIN
    # No member list anywhere. The conversation's own record is fetched after
    # the scan, for the name the answer refers to the room by, which is
    # presentation and no part of deciding what may be read.
    assert "conversations_members" not in workspace.calls


def test_naming_the_originating_conversation_is_the_same_as_naming_nothing() -> None:
    """Every audience is a subset of itself, so no comparison is owed."""
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    result = _read(_toolkit(workspace), chat_id=_ORIGIN)

    assert result["ok"] is True
    assert result["chat_id"] == _ORIGIN
    assert "conversations_members" not in workspace.calls


def test_the_bound_lifted_reads_a_target_the_rule_refuses() -> None:
    """``enforce_audience: false`` holds no bound, and nothing is compared."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_BOB}},
    )
    result = _read(_toolkit(workspace, enforced=False), chat_id=_TARGET)

    assert result["ok"] is True
    assert result["chat_id"] == _TARGET
    assert "conversations_members" not in workspace.calls


def test_one_call_spends_the_lookup_budget_on_names_alone() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
    )
    result = _read(
        _toolkit(workspace, lookups=5), chat_id=_TARGET
    )
    assert result["ok"] is True
    asked = [call["user"] for call in workspace.calls["users_info"]]
    assert asked == [_ALICE]


# ── 2. The subset rule ───────────────────────────────────────────────────────


def test_the_card_states_the_rule_and_names_no_api_and_no_scope() -> None:
    card = _card(_toolkit(_Workspace()))
    assert "chat_id" in card.input_params["properties"]
    text = card.description + card.input_params["properties"]["chat_id"]["description"]
    for forbidden in ("scope", "conversations.members", "conversations.info", "API"):
        assert forbidden not in text
    assert "everybody this conversation reaches can already read" in text
    assert "public channel" in text


def test_a_read_is_allowed_when_everybody_here_is_also_there() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOB, _BOT}}
    )
    result = _read(_toolkit(workspace), chat_id=_TARGET)

    assert result["ok"] is True
    assert result["chat_id"] == _TARGET
    # The scan itself went to the target, not to the room that asked.
    assert [call["channel"] for call in workspace.calls["conversations_history"]] == [
        _TARGET
    ]


def test_a_read_is_refused_by_count_and_names_nobody() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _TARGET: {_ALICE, _BOT}},
    )
    with _captured(_LOGGER_NAME) as records:
        result = _read(_toolkit(workspace), chat_id=_TARGET)

    assert result["ok"] is False
    assert result["error"] == "source_members_not_in_target"
    assert "1 member(s)" in result["detail"]
    assert _names_nobody(result["detail"], {_ALICE, _BOB, _BOT})
    # And the refusal names the room the request came from, never the room it
    # was refused access to dressed up as the room it read.
    assert result["chat_id"] == _ORIGIN
    assert result["messages"] == []
    assert any("source_members_not_in_target" in said for said in _warnings(records))


def _names_nobody(detail: str, ids: "set[str]") -> bool:
    """Whether a refusal detail mentions none of a set of member ids.

    The property, rather than the sentence: a rewording of the refusal must not
    quietly stop this being tested, and an id appearing anywhere in the string
    is the leak whatever the surrounding words are.
    """
    return not any(member in detail for member in ids)


def test_a_private_target_is_refused_by_count_and_names_nobody() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(_toolkit(workspace), chat_id=_TARGET)

    assert result["error"] == "source_members_not_in_target"
    assert _names_nobody(result["detail"], {_ALICE, _BOB, _BOT, _INTEGRATION})
    # Still says how many, and still says which rooms -- the asker named one of
    # those and was already in the other.
    assert "1" in result["detail"]
    assert _ORIGIN in result["detail"] and _TARGET in result["detail"]


def test_the_count_is_the_whole_of_it_however_many_are_blocking() -> None:
    crowd = {f"U-{index:02d}" for index in range(9)}
    workspace = _Workspace(
        members={_ORIGIN: crowd | {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(_toolkit(workspace), chat_id=_TARGET)

    assert result["error"] == "source_members_not_in_target"
    assert _names_nobody(result["detail"], crowd)
    assert "9" in result["detail"]
    assert "more" not in result["detail"]


@pytest.mark.parametrize(
    ("record", "why"),
    [
        (
            {"is_channel": True, "is_private": True},
            "a private channel",
        ),
        (
            {"is_im": True, "is_private": False},
            "a direct message whose private flag reads false",
        ),
        (
            {"is_mpim": True, "is_private": False},
            "a group direct message whose private flag reads false",
        ),
        (
            {"is_channel": True},
            "a channel record carrying no private flag at all",
        ),
    ],
)
def test_only_a_public_channel_earns_a_name(
    record: "dict[str, Any]", why: str
) -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOB, _BOT}, "C0ROOM0001": {_ALICE}})

    async def _info(**kwargs: Any) -> dict[str, Any]:
        workspace.calls["conversations_info"].append(kwargs)
        return {"channel": dict(record, id="C0ROOM0001")}

    workspace.conversations_info = _info  # type: ignore[method-assign]
    result = _read(_toolkit(workspace), chat_id="C0ROOM0001")

    assert result["error"] == "source_members_not_in_target", why
    assert _names_nobody(result["detail"], {_ALICE, _BOB, _BOT}), why


def test_the_asker_being_entitled_is_not_enough_on_its_own() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(
        _toolkit(workspace, slack_user_id=_ALICE), chat_id=_TARGET
    )
    assert result["error"] == "source_members_not_in_target"


def test_a_direct_message_reduces_to_the_asker_and_the_bot() -> None:
    workspace = _Workspace(
        members={_DM: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOB, _BOT}}
    )
    result = _read(
        _toolkit(workspace,
            slack_channel_id=_DM,
            slack_channel_type="im",
        ),
        chat_id=_TARGET,
    )
    assert result["ok"] is True
    assert result["chat_id"] == _TARGET


def test_a_direct_message_is_still_refused_when_the_bot_is_not_in_the_target() -> None:
    workspace = _Workspace(
        members={_DM: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOB}}
    )
    result = _read(
        _toolkit(workspace,
            slack_channel_id=_DM,
            slack_channel_type="im",
        ),
        chat_id=_TARGET,
    )
    assert result["error"] == "source_members_not_in_target"
    assert "1 member(s)" in result["detail"]


# ── 3. Public is asymmetric, and it tightens on both ends ────────────────────


def test_a_public_target_is_read_without_any_member_list() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _PUBLIC: {_ALICE}},
        public={_PUBLIC},
    )
    result = _read(_toolkit(workspace), chat_id=_PUBLIC)

    assert result["ok"] is True
    assert result["chat_id"] == _PUBLIC
    # Settled on the two records alone. A member list fetched here would be a
    # call made to be discarded.
    assert "conversations_members" not in workspace.calls


def test_a_public_source_may_read_only_another_public_channel() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        public={_ORIGIN},
    )
    result = _read(_toolkit(workspace), chat_id=_TARGET)

    assert result["error"] == "source_is_public_and_target_is_not"
    assert "public channel" in result["detail"]
    # Refused on the two records. The memberships line up and do not matter.
    assert "conversations_members" not in workspace.calls


def test_a_public_pair_reads_either_way() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _PUBLIC: {_BOB}},
        public={_ORIGIN, _PUBLIC},
    )
    result = _read(_toolkit(workspace), chat_id=_PUBLIC)
    assert result["ok"] is True


def test_a_group_dm_is_never_public_however_its_private_flag_reads() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOB, _BOT}, "G0ROOM0001": {_ALICE}})
    real_info = workspace.conversations_info

    async def _mpim_info(**kwargs: Any) -> dict[str, Any]:
        if str(kwargs.get("channel")) != "G0ROOM0001":
            return await real_info(**kwargs)
        workspace.calls["conversations_info"].append(kwargs)
        return {"channel": {"id": "G0ROOM0001", "is_mpim": True, "is_private": False}}

    workspace.conversations_info = _mpim_info  # type: ignore[method-assign]
    result = _read(_toolkit(workspace), chat_id="G0ROOM0001")
    assert result["error"] == "source_members_not_in_target"


# ── 3b. the Slack Connect guard ──────────────────────────────────────────────
#
# A Slack Connect conversation holds people from outside the workspace, so it is
# not workspace-bounded and the workspace answer is wrong for it even where Slack
# calls it public. Four markers say so, and the gate already holds the record
# every one of them is on, so the guard costs no call.


def _shared(workspace: "_Workspace", channel_id: str, flag: str) -> None:
    """Make one conversation answer with an external-share marker set."""
    real_info = workspace.conversations_info

    async def _info(**kwargs: Any) -> dict[str, Any]:
        answer = await real_info(**kwargs)
        if str(kwargs.get("channel")) == channel_id:
            answer["channel"][flag] = True
        return answer

    workspace.conversations_info = _info  # type: ignore[method-assign]


@pytest.mark.parametrize(
    "flag",
    ["is_ext_shared", "is_pending_ext_shared", "is_org_shared", "is_shared"],
)
def test_an_externally_shared_public_target_is_refused(flag: str) -> None:
    """Four different facts, and any one of them breaks the workspace bound."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _PUBLIC: {_BOB, _BOT}},
        public={_PUBLIC},
    )
    _shared(workspace, _PUBLIC, flag)
    result = _read(_toolkit(workspace), chat_id=_PUBLIC)

    assert result["error"] == "target_reaches_outside_workspace"
    assert "outside this workspace" in result["detail"]


def test_an_externally_shared_target_is_still_read_when_everybody_here_is_in_it() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _PUBLIC: {_ALICE, _BOT, "W-OUTSIDE"}},
        public={_PUBLIC},
    )
    _shared(workspace, _PUBLIC, "is_ext_shared")
    result = _read(_toolkit(workspace), chat_id=_PUBLIC)
    assert result["ok"] is True


def test_an_externally_shared_public_source_may_not_read_a_public_target() -> None:
    """The guard applies on either end, because it is about the conversation."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, "W-OUTSIDE"}, _PUBLIC: {_BOB, _BOT}},
        public={_ORIGIN, _PUBLIC},
    )
    _shared(workspace, _ORIGIN, "is_ext_shared")
    result = _read(_toolkit(workspace), chat_id=_PUBLIC)

    assert result["error"] == "source_members_not_in_target"


def test_a_source_whose_record_slack_never_stated_is_refused() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}})
    real_info = workspace.conversations_info

    async def _blank(**kwargs: Any) -> dict[str, Any]:
        if str(kwargs.get("channel")) == _ORIGIN:
            workspace.calls["conversations_info"].append(kwargs)
            return {"channel": {}}
        return await real_info(**kwargs)

    workspace.conversations_info = _blank  # type: ignore[method-assign]
    result = _read(_toolkit(workspace), chat_id=_TARGET)
    assert result["error"] == "source_conversation_record_unavailable"


# ── 4. Cron ──────────────────────────────────────────────────────────────────


def _cron_metadata(enforced: bool = True, **overrides: Any) -> dict[str, Any]:
    """What the scheduler stamps: a conversation, a marker, and no sender."""
    base = _metadata(enforced)
    base.pop("slack_user_id")
    base[METADATA_ORIGIN_KEY] = ORIGIN_CRON_JOB
    base.update(overrides)
    return base


def test_a_cron_run_is_gated_on_membership_like_anything_else() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOB, _BOT}}
    )
    toolkit = SlackHistoryToolkit(
        metadata=_cron_metadata(),
        client=workspace,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    result = _read(toolkit, chat_id=_TARGET)
    assert result["ok"] is True
    assert result["chat_id"] == _TARGET


def test_a_cron_run_is_refused_on_the_same_rule() -> None:
    """And reports it the same way: a count, and no ids."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _TARGET: {_ALICE, _BOT}},
    )
    toolkit = SlackHistoryToolkit(
        metadata=_cron_metadata(),
        client=workspace,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    result = _read(toolkit, chat_id=_TARGET)
    assert result["error"] == "source_members_not_in_target"
    assert "1 member(s)" in result["detail"]


def test_a_cron_run_needs_no_asker_and_an_inbound_request_does() -> None:
    members = {_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}

    cron = SlackHistoryToolkit(
        metadata=_cron_metadata(),
        client=_Workspace(members=members),
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    assert _read(cron, chat_id=_TARGET)["ok"] is True

    inbound = _metadata()
    inbound.pop("slack_user_id")
    unsigned = SlackHistoryToolkit(
        metadata=inbound,
        client=_Workspace(members=members),
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    assert _read(unsigned, chat_id=_TARGET)["error"] == "history_asker_unresolved"


# ── 5. Fail closed, branch by branch ─────────────────────────────────────────


def test_an_unsettled_policy_is_refused_rather_than_defaulted() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": _ORIGIN,
            "slack_channel_type": "channel",
            "slack_user_id": _ALICE,
        },
        client=workspace,
        now=lambda: 200_000.0,
    )
    result = _read(toolkit)
    assert result["error"] == "audience_unsettled"
    assert workspace.calls == {}


@pytest.mark.parametrize("written", ["", 0, None, [], {}])
def test_anything_falsey_on_the_wire_lifts_the_bound_rather_than_refusing(
    written: Any,
) -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_BOB}},
    )
    result = _read(
        _toolkit(workspace, **{METADATA_ENFORCE_AUDIENCE_KEY: written}),
        chat_id=_TARGET,
    )
    assert result["ok"] is True


def test_no_trusted_conversation_returns_exactly_what_it_always_did() -> None:
    toolkit = SlackHistoryToolkit(metadata={}, client=_Workspace())
    assert _read(toolkit) == {
        "ok": False,
        "error": "trusted_slack_channel_context_required",
        "messages": [],
    }


def test_a_membership_lookup_that_fails_refuses_rather_than_answering() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_members": "channel_not_found"},
    )
    result = _read(_toolkit(workspace), chat_id=_TARGET)

    assert result["ok"] is False
    assert result["error"] == "history_gate_slack_refused"
    assert result["chat_id"] == _ORIGIN
    assert result["messages"] == []
    assert "conversations_history" not in workspace.calls


def test_a_conversation_record_that_cannot_be_read_refuses() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_info": "channel_not_found"},
    )
    result = _read(_toolkit(workspace), chat_id=_TARGET)
    assert result["error"] == "history_gate_slack_refused"
    assert "conversations_history" not in workspace.calls


def test_a_refused_call_says_which_call_slack_refused() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_info": "missing_scope"},
    )
    detail = _read(_toolkit(workspace), chat_id=_TARGET)["detail"]

    assert "conversations.info" in detail
    assert "conversations.members" not in detail
    assert "missing_scope" in detail


@pytest.mark.parametrize(
    ("target", "kind", "scope"),
    [
        (_TARGET, "private channel", "groups:read"),
        (_DM, "direct message", "im:read"),
    ],
    ids=["private-channel", "direct-message"],
)
def test_a_refused_membership_names_the_conversation_kind_and_its_scope(
    target: str, kind: str, scope: str
) -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, target: {_ALICE, _BOT}},
        fail_members={target: "missing_scope"},
    )
    detail = _read(_toolkit(workspace), chat_id=target)["detail"]

    assert "conversations.members" in detail
    assert kind in detail
    assert scope in detail


def test_a_refused_source_membership_names_no_scope_it_has_not_established() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail_members={_ORIGIN: "missing_scope"},
    )
    detail = _read(_toolkit(workspace), chat_id=_TARGET)["detail"]

    assert "conversations.members" in detail
    assert _ORIGIN in detail
    assert not [word for word in ("channels:read", "groups:read", "im:read") if word in detail]


def test_the_four_ways_the_gate_can_fail_are_four_different_codes() -> None:
    refused = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_info": "missing_scope"},
    )
    assert (
        _read(_toolkit(refused), chat_id=_TARGET)["error"]
        == "history_gate_slack_refused"
    )

    # A clock that jumps past the deadline the moment it is armed. The first
    # reading arms it; every reading after it is the timeout.
    readings = iter([0.0])
    timed_out = SlackHistoryToolkit(
        metadata=_metadata(),
        client=_Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE}}),
        now=lambda: 200_000.0,
        monotonic=lambda: next(readings, 10_000.0),
        max_user_lookups=0,
    )
    result = _read(timed_out, chat_id=_TARGET)
    assert result["error"] == "history_gate_timed_out"
    assert "history_scan_timeout_seconds" in result["detail"]

    # One call is enough for the conversations.info and nothing after it.
    exhausted = SlackHistoryToolkit(
        metadata=_metadata(),
        client=_Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE}}),
        now=lambda: 200_000.0,
        max_api_calls=1,
        max_user_lookups=0,
    )
    result = _read(exhausted, chat_id=_TARGET)
    assert result["error"] == "history_gate_call_budget_exhausted"
    assert "history_max_api_calls" in result["detail"]

    # There was a fourth, ``history_gate_user_lookups_exhausted``. It is gone
    # with the ladder word whose directory probe could run out: the audience rule
    # asks the directory nothing, so the lookup budget can no longer refuse a
    # read. Three causes, three numbers to raise.


def test_a_truncated_member_list_refuses_rather_than_being_believed() -> None:
    workspace = _Workspace(
        members={_TARGET: {_ALICE, _BOT}},
        members_pages={_ORIGIN: [{"members": [_ALICE], "has_more": True}]},
    )
    result = _read(_toolkit(workspace), chat_id=_TARGET)
    # Named as itself rather than folded into the generic gate failure: Slack
    # answered, so nothing was unavailable -- what it returned was short, which
    # is a different thing for whoever reads the log.
    assert result["error"] == "conversation_members_incomplete"
    assert _ORIGIN in result["detail"]
    assert result["messages"] == []
    assert "conversations_history" not in workspace.calls


def _answering_info(
    workspace: _Workspace, answer: dict[str, Any]
) -> None:
    """Make every conversations.info on this workspace answer ``answer``."""

    async def _info(**kwargs: Any) -> dict[str, Any]:
        workspace.calls["conversations_info"].append(kwargs)
        return answer

    workspace.conversations_info = _info  # type: ignore[method-assign]


def test_a_target_this_tool_does_not_read_is_named_rather_than_attempted() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    # A record that says what it is, and says something this tool does not read.
    _answering_info(workspace, {"channel": {"id": "C0ODDROOM1", "is_workflow": True}})

    result = _read(_toolkit(workspace), chat_id="C0ODDROOM1")

    assert result["error"] == "target_conversation_record_unavailable"
    assert "conversations_history" not in workspace.calls


@pytest.mark.parametrize(
    "answer",
    [
        # Slack answered ok and sent no channel object at all.
        {},
        # A channel object that is not one.
        {"channel": _TARGET},
        # A record that names the conversation and says nothing about its kind.
        {"channel": {"id": _TARGET}},
    ],
)
def test_a_target_whose_kind_slack_never_stated_is_refused(
    answer: dict[str, Any],
) -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}})
    _answering_info(workspace, answer)

    result = _read(_toolkit(workspace), chat_id=_TARGET)

    assert result["ok"] is False
    assert result["error"] == "target_conversation_record_unavailable"
    assert _TARGET in result["detail"]
    # Refused before any membership is enumerated, and nothing is read.
    assert "conversations_members" not in workspace.calls
    assert "conversations_history" not in workspace.calls
    assert result["messages"] == []


def test_an_unreadable_record_denies_the_open_relaxation_too() -> None:
    """The widest word is still a word about a kind Slack has stated."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}}, public={_PUBLIC}
    )
    _answering_info(workspace, {"channel": {"id": _PUBLIC, "is_private": False}})

    result = _read(_toolkit(workspace), chat_id=_PUBLIC)

    assert result["error"] == "target_conversation_record_unavailable"
    assert "conversations_history" not in workspace.calls


def test_no_refusal_ever_returns_the_originating_conversations_messages() -> None:
    """The invariant behind every branch above, asserted once as itself."""
    cases: "list[tuple[dict[str, set[str]], set[str]]]" = [
        # The subset test, failing.
        ({_TARGET: {_BOB, _BOT}}, set()),
        # A public source and a private target.
        ({_TARGET: {_ALICE, _BOT}}, {_ORIGIN}),
    ]
    for target_members, public in cases:
        workspace = _Workspace(
            members={_ORIGIN: {_ALICE, _BOT}, **target_members}, public=public
        )
        result = _read(_toolkit(workspace), chat_id=_TARGET)
        assert result["ok"] is False, public
        assert result["messages"] == []
        assert "conversations_history" not in workspace.calls


# ── 6. The membership cache ──────────────────────────────────────────────────


def test_one_member_list_is_read_once_for_several_reads_in_a_turn() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    toolkit = _toolkit(workspace)
    assert _read(toolkit, chat_id=_TARGET)["ok"] is True
    assert _read(toolkit, chat_id=_TARGET)["ok"] is True

    read = [call["channel"] for call in workspace.calls["conversations_members"]]
    assert sorted(read) == [_ORIGIN, _TARGET]


def test_the_cache_expires_so_a_removal_takes_effect() -> None:
    clock = {"now": 1_000.0}
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}})
    toolkit = SlackHistoryToolkit(
        metadata=_metadata(),
        client=workspace,
        now=lambda: 200_000.0,
        monotonic=lambda: clock["now"],
        max_user_lookups=0,
    )
    assert _read(toolkit, chat_id=_TARGET)["ok"] is True

    # The second read refuses, which is what says the fresh list was read rather
    # than the cached one.
    workspace.members[_TARGET] = {_ALICE}
    clock["now"] += 3_600.0
    result = _read(toolkit, chat_id=_TARGET)
    assert result["error"] == "source_members_not_in_target"


def test_history_bounds_do_not_remain_in_the_host_template() -> None:
    import yaml

    root = Path(jiuwenswarm.__file__).resolve().parent
    data = yaml.safe_load(
        (root / "resources" / "config.yaml").read_text()
    )
    assert "history_members_cache_seconds" not in data["channels"]["slack"]


def test_the_toolkit_reads_the_names_the_connector_writes() -> None:
    from belle_agent.platforms.slack import history_policy as slack_history_policy

    assert (
        slack_history.METADATA_ENFORCE_AUDIENCE_KEY
        is slack_history_policy.METADATA_ENFORCE_AUDIENCE_KEY
    )
    assert slack_history.METADATA_ASKER_KEY is slack_history_policy.METADATA_ASKER_KEY
