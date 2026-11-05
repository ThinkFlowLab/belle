"""Slack audience policy integration with config, history, and posting tools."""

import asyncio
import json
from typing import Any

import pytest
import yaml
from belle_agent.integrations.jiuwenswarm.slack_tool_context import METADATA_ORIGIN_KEY, ORIGIN_CRON_JOB
from belle_agent.platforms.slack.history_policy import (
    KEY_ENFORCE_AUDIENCE,
    METADATA_ASKER_KEY,
    METADATA_ENFORCE_AUDIENCE_KEY,
)

import jiuwenswarm.common.config as config_module
from belle_agent.integrations.jiuwenswarm.slack_history import SlackHistoryToolkit
from belle_agent.integrations.jiuwenswarm.slack_post import (
    POST_MESSAGE,
    SlackPostToolkit,
    slack_post_request_metadata,
)
from slack_fakes import FakeSlackError as _FakeSlackError

_BOT = "U0BOT"
_ASKER = "U0ASKER"
_OTHER = "U0OTHER"
_OUTSIDE = "U0OUTSIDE"
_PUBLIC_RECORD = {"is_channel": True, "is_private": False}
_PRIVATE_RECORD = {"is_channel": True, "is_private": True}
_DM_RECORD = {"is_im": True, "is_private": True}
_TEMPLATES = (
    "config.yaml",
    "config.team.distributed.leader.yaml",
    "config.team.distributed.teammate.yaml",
)


def test_the_agent_history_scope_key_is_retired() -> None:
    """The ladder's per-conversation form goes with the ladder.

    ``agent.history`` accepted one of the five words per conversation, per sender
    or per workspace. A safety bound has no sensible per-conversation form: the
    startup warning for a lifted bound is said once for the whole installation,
    and a rule lifting it for one room would be a bound an operator cannot read
    off a single line. ``enforce_audience`` is deliberately not addressable.
    """
    from belle_agent.scopes import SECTION_AGENT, channel_capabilities

    slack = channel_capabilities("slack")
    assert slack is not None
    assert "history" not in slack.sections[SECTION_AGENT]
    assert "history" not in slack.layer0_keys.get(SECTION_AGENT, {})
    assert "agent.history" not in slack.validators
    assert "agent.history" not in slack.axis_restrictions
    assert KEY_ENFORCE_AUDIENCE not in {
        key for twins in slack.layer0_keys.values() for key in twins.values()
    }


@pytest.mark.parametrize("name", _TEMPLATES)
def test_every_template_omits_belle_config_and_duplicate_policy(name: str) -> None:
    from pathlib import Path

    import jiuwenswarm

    path = Path(jiuwenswarm.__file__).parent / "resources" / name
    text = path.read_text(encoding="utf-8")
    slack = yaml.safe_load(text)["channels"]["slack"]
    assert "belle_agent" not in yaml.safe_load(text)
    for key in (KEY_ENFORCE_AUDIENCE, "history_never_read", "history_exempt_members"):
        assert key not in slack
    # Checked on the parsed block rather than on the raw text, because
    # ``permissions`` has a ``write`` key of its own at the same indent.
    for retired in ("history", "write", "history_digest_channel_ids"):
        assert retired not in slack, f"{name} still ships channels.slack.{retired}"


# ── the toolkits ─────────────────────────────────────────────────────────────

_HERE_PRIVATE = "C0HEREPRIV"
_HERE_PUBLIC = "C0HEREPUB"
_TARGET_PRIVATE_WIDE = "C0WIDE"
_TARGET_PRIVATE_NARROW = "C0NARROW"
_TARGET_PUBLIC = "C0PUB"
_TARGET_EXTERNAL = "C0EXT"
_DM_WITH_ASKER = "D0ASKER"
_DM_WITH_OTHER = "D0OTHER"

#: One table, read by the fake for both toolkits. ``C0NARROW`` is a subset of the
#: private source and ``C0WIDE`` holds one person who is not in it.
_ROOMS: "dict[str, tuple[dict[str, Any], list[str]]]" = {
    _HERE_PRIVATE: (_PRIVATE_RECORD, [_BOT, _ASKER]),
    _HERE_PUBLIC: (_PUBLIC_RECORD, [_BOT, _ASKER]),
    _TARGET_PRIVATE_WIDE: (_PRIVATE_RECORD, [_BOT, _ASKER, _OTHER]),
    _TARGET_PRIVATE_NARROW: (_PRIVATE_RECORD, [_BOT, _ASKER]),
    _TARGET_PUBLIC: (_PUBLIC_RECORD, [_BOT]),
    _TARGET_EXTERNAL: ({**_PUBLIC_RECORD, "is_ext_shared": True}, [_BOT, _OUTSIDE]),
    _DM_WITH_ASKER: (_DM_RECORD, [_BOT, _ASKER]),
    _DM_WITH_OTHER: (_DM_RECORD, [_BOT, _OTHER]),
}


class _Workspace:
    """A fake Slack that answers from ``_ROOMS`` and records what it was asked."""

    def __init__(self, fail: "dict[str, str] | None" = None) -> None:
        self.fail = fail or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        error = self.fail.get(method)
        if error:
            raise _FakeSlackError(error)

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self._record("auth_test", kwargs)
        return {
            "ok": True,
            "url": "https://acme.slack.com/",
            "user_id": _BOT,
            "team_id": "T0ONE",
        }

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", kwargs)
        channel = str(kwargs["channel"])
        record, _members = _ROOMS.get(channel, (_PRIVATE_RECORD, []))
        return {"ok": True, "channel": {"id": channel, **record}}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_members", kwargs)
        channel = str(kwargs["channel"])
        _record, members = _ROOMS.get(channel, (_PRIVATE_RECORD, []))
        return {"ok": True, "members": list(members)}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_history", kwargs)
        return {"ok": True, "messages": [], "has_more": False}

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_open", kwargs)
        users = str(kwargs["users"])
        return {
            "ok": True,
            "channel": {"id": _DM_WITH_ASKER if users == _ASKER else _DM_WITH_OTHER},
        }

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postMessage", kwargs)
        return {"ok": True, "ts": "1758123456.123456", "channel": kwargs["channel"]}

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("users_info", kwargs)
        return {"ok": True, "user": {"id": kwargs["user"], "name": kwargs["user"]}}


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token and the settings both toolkits read out of config."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _metadata(
    *,
    chat: str = _HERE_PRIVATE,
    chat_type: str = "group",
    enforced: bool = True,
    asker: "str | None" = _ASKER,
    cron: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_channel_id": chat,
        "slack_channel_type": chat_type,
        METADATA_ENFORCE_AUDIENCE_KEY: enforced,
    }
    if asker:
        metadata[METADATA_ASKER_KEY] = asker
    if cron:
        metadata[METADATA_ORIGIN_KEY] = ORIGIN_CRON_JOB
    return metadata


def _run(coro: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(coro))


# ── 7. the read gate ─────────────────────────────────────────────────────────


def _read(metadata: dict[str, Any], target: "str | None") -> dict[str, Any]:
    workspace = _Workspace()
    toolkit = SlackHistoryToolkit(metadata=metadata, client=workspace)
    return _run(toolkit.read_slack_conversation(hours=1, chat_id=target))


def test_a_private_source_reads_a_target_everybody_here_is_in() -> None:
    answer = _read(_metadata(), _TARGET_PRIVATE_WIDE)
    assert answer.get("error") is None, answer
    assert answer["chat_id"] == _TARGET_PRIVATE_WIDE


def test_a_private_source_is_refused_a_target_somebody_here_is_missing_from() -> None:
    answer = _read(_metadata(), _TARGET_PUBLIC)
    assert answer.get("error") is None, answer


def test_a_private_source_is_refused_a_narrower_private_target() -> None:
    workspace = _Workspace()
    toolkit = SlackHistoryToolkit(metadata=_metadata(), client=workspace)
    answer = _run(toolkit.read_slack_conversation(hours=1, chat_id=_DM_WITH_OTHER))
    assert answer["error"] == "source_members_not_in_target"


def test_a_public_source_is_refused_a_private_target_by_name() -> None:
    """The refusal names the reason the old ladder got the sign of."""
    answer = _read(
        _metadata(chat=_HERE_PUBLIC, chat_type="channel"), _TARGET_PRIVATE_WIDE
    )
    assert answer["error"] == "source_is_public_and_target_is_not"
    assert "public channel" in answer["detail"]


def test_a_public_source_may_read_another_public_channel() -> None:
    answer = _read(_metadata(chat=_HERE_PUBLIC, chat_type="channel"), _TARGET_PUBLIC)
    assert answer.get("error") is None, answer


def test_an_externally_shared_target_is_refused_by_its_own_marker() -> None:
    """A public target would be readable from here. Shared outside, it is not.

    The source is private, so the refusal is the one the guard buys rather than
    the public-source one: the target's member list holds somebody this
    conversation does not reach, and the workspace does not cover them.
    """
    answer = _read(_metadata(), _TARGET_EXTERNAL)
    assert answer["error"] == "target_reaches_outside_workspace"


def test_a_read_refusal_has_no_confirmation_path() -> None:
    """Deliberately absent. A question put here about T says that T exists."""
    toolkit = SlackHistoryToolkit(metadata=_metadata(), client=_Workspace())
    assert not hasattr(toolkit, "widening_question")


def test_the_bound_lifted_reads_a_target_the_rule_refuses() -> None:
    answer = _read(_metadata(enforced=False), _DM_WITH_OTHER)
    assert answer.get("error") is None, answer


def test_a_request_with_no_audience_decision_is_refused() -> None:
    """An absent key is *no connector settled this*, which is not ``false``."""
    metadata = _metadata()
    del metadata[METADATA_ENFORCE_AUDIENCE_KEY]
    toolkit = SlackHistoryToolkit(metadata=metadata, client=_Workspace())
    answer = _run(toolkit.read_slack_conversation(hours=1))
    assert answer["error"] == "audience_unsettled"


def test_the_card_always_declares_chat_id() -> None:
    """One card shape, where the ladder gave two."""
    toolkit = SlackHistoryToolkit(metadata=_metadata(), client=_Workspace())
    cards = {tool.card.name: tool.card for tool in toolkit.get_tools()}
    assert "chat_id" in cards["read_slack_conversation"].input_params["properties"]
    assert "chat_id" in cards["read_pinned_messages"].input_params["properties"]


# ── 8. the write gate ────────────────────────────────────────────────────────


def _post_toolkit(
    workspace: _Workspace, metadata: dict[str, Any]
) -> SlackPostToolkit:
    return SlackPostToolkit(metadata=metadata, client=workspace)


def _post(metadata: dict[str, Any], target: str) -> dict[str, Any]:
    workspace = _Workspace()
    toolkit = _post_toolkit(workspace, metadata)
    return _run(toolkit.post_message(text="hello", chat_id=target))


def test_a_write_into_a_narrower_target_goes_without_a_question() -> None:
    workspace = _Workspace()
    toolkit = _post_toolkit(workspace, _metadata())
    assert asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _TARGET_PRIVATE_NARROW})
    ) is None


def test_a_write_into_a_wider_target_asks_and_names_who_would_see_it() -> None:
    workspace = _Workspace()
    toolkit = _post_toolkit(workspace, _metadata())
    question = asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _TARGET_PRIVATE_WIDE})
    )
    assert question is not None
    assert _OTHER in question


def test_a_write_into_a_public_target_asks_and_names_the_workspace() -> None:
    """The unnameable case. No list of the workspace exists to read out."""
    workspace = _Workspace()
    toolkit = _post_toolkit(workspace, _metadata())
    question = asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _TARGET_PUBLIC})
    )
    assert question is not None
    assert "public channel" in question
    assert "workspace" in question


def test_a_write_out_of_a_public_source_asks_nothing() -> None:
    workspace = _Workspace()
    toolkit = _post_toolkit(
        workspace, _metadata(chat=_HERE_PUBLIC, chat_type="channel")
    )
    assert asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _TARGET_PRIVATE_WIDE})
    ) is None


def test_a_write_out_of_a_public_source_into_an_external_target_asks() -> None:
    workspace = _Workspace()
    toolkit = _post_toolkit(
        workspace, _metadata(chat=_HERE_PUBLIC, chat_type="channel")
    )
    question = asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _TARGET_EXTERNAL})
    )
    assert question is not None
    assert "outside this workspace" in question


def test_a_direct_message_target_goes_through_the_general_rule() -> None:
    """No special case, and there was one.

    The direct message with the requester reaches the requester and the bot, both
    of whom are in the conversation the request came from, so the general rule
    answers "no question" without any short-circuit. The direct message with
    somebody else widens by exactly that person and is asked about.
    """
    workspace = _Workspace()
    toolkit = _post_toolkit(workspace, _metadata())
    assert asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _ASKER})
    ) is None
    # The rule was applied rather than skipped: the member lists were read.
    assert "conversations_members" in workspace.methods()

    other = _Workspace()
    other_toolkit = _post_toolkit(other, _metadata())
    question = asyncio.run(
        other_toolkit.widening_question(POST_MESSAGE, {"chat_id": _OTHER})
    )
    assert question is not None
    assert _OTHER in question


def test_a_cron_turn_refuses_a_widening_write() -> None:
    """Nobody to ask, so the rule refuses rather than asking."""
    answer = _post(_metadata(asker=None, cron=True), _TARGET_PRIVATE_WIDE)
    assert answer["error"] == "write_target_widens_audience"


def test_a_cron_turn_allows_a_write_that_widens_nothing() -> None:
    answer = _post(_metadata(asker=None, cron=True), _TARGET_PRIVATE_NARROW)
    assert answer.get("error") is None, answer
    assert answer["chat_id"] == _TARGET_PRIVATE_NARROW


def test_a_cron_turn_out_of_a_public_source_writes_anywhere() -> None:
    """A consequence this design accepts, asserted so it is not a surprise."""
    answer = _post(
        _metadata(chat=_HERE_PUBLIC, chat_type="channel", asker=None, cron=True),
        _TARGET_PRIVATE_WIDE,
    )
    assert answer.get("error") is None, answer


def test_the_bound_lifted_writes_into_a_target_the_rule_refuses() -> None:
    answer = _post(_metadata(enforced=False), _TARGET_PUBLIC)
    assert answer.get("error") is None, answer


def test_the_bound_lifted_asks_nothing() -> None:
    workspace = _Workspace()
    toolkit = _post_toolkit(workspace, _metadata(enforced=False))
    assert asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _TARGET_PUBLIC})
    ) is None


def test_a_posting_request_with_no_audience_decision_mounts_nothing() -> None:
    metadata = _metadata()
    del metadata[METADATA_ENFORCE_AUDIENCE_KEY]
    assert slack_post_request_metadata("slack", metadata) == {}


def test_a_posting_request_with_the_bound_lifted_still_mounts() -> None:
    """``false`` is a deployment with no bound, not one with no tools."""
    assert slack_post_request_metadata("slack", _metadata(enforced=False))


def test_every_posting_card_declares_chat_id() -> None:
    toolkit = _post_toolkit(_Workspace(), _metadata())
    for tool in toolkit.get_tools():
        assert "chat_id" in tool.card.input_params["properties"], tool.card.name
