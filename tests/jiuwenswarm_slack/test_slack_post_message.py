"""Slack writing tools, the audience rule, Block Kit, and confirmations."""

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import jiuwenswarm.common.config as config_module
import pytest
from slack_fakes import FakeSlackError as _FakeSlackError

from belle_agent.integrations.jiuwenswarm.slack_post import (
    DELETE_MESSAGE,
    EDIT_MESSAGE,
    POST_MESSAGE,
    SlackPostToolkit,
    slack_post_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_tool_context import METADATA_ORIGIN_KEY, ORIGIN_CRON_JOB
from belle_agent.integrations.jiuwenswarm.slack_write_confirmation_rail import (
    SlackWriteConfirmationRail,
)
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack import scope_policy as slack_scope_policy
from belle_agent.platforms.slack.history_policy import (
    METADATA_ASKER_KEY,
    METADATA_ENFORCE_AUDIENCE_KEY,
    METADATA_WRITE_TARGET_REQUIRED_KEY,
)
from belle_agent.platforms.slack.text import MAX_SLACK_POST_FALLBACK_LENGTH, MAX_SLACK_TEXT_LENGTH

_HERE = "C0HERE"
_NARROW = "C0NARROW"
_WIDE = "C0WIDE"
_PUBLIC = "C0PUBLIC"
_PERSON = "U0ASKER"
_COLLEAGUE = "U0B"
_ALSO_HERE = "U0C"
_STRANGER = "U0STRANGER"
_DM = "D0STRANGER"
_TS = "1758123456.123456"
_TS2 = "1758123499.123456"
_URL = "https://acme.slack.com/"

#: What this app is called where Belle's ``slack.bot_name`` says nothing.
_BOT = slack_scope_policy.DEFAULT_BOT_NAME

#: Who is in which conversation. ``C0NARROW`` is a subset of ``C0HERE`` and is
#: written to silently; ``C0WIDE`` holds one person who is not here and is what
#: a confirmation is asked about.
_MEMBERS = {
    _HERE: [_PERSON, _COLLEAGUE, _ALSO_HERE],
    _NARROW: [_PERSON, _COLLEAGUE],
    _WIDE: [_PERSON, _COLLEAGUE, _ALSO_HERE, _STRANGER],
    _PUBLIC: [_PERSON, _COLLEAGUE],
    _DM: [_PERSON, _STRANGER],
}




#: What ``users.info`` says about the asker. ``display_name`` set and an
#: ``image_512`` beside it is the shape a complete profile has; the tests that
#: care about the gaps pass their own.
_PROFILE: dict[str, Any] = {
    "display_name": "Ada",
    "real_name": "Ada Lovelace",
    "image_512": "https://avatars.slack-edge.com/ada_512.png",
}

#: The legacy login handle. Read by nothing and asserted against, because it is
#: the field a profile reader reaches for by accident.
_HANDLE = "ada.lovelace.legacy"


class _Workspace:
    """A fake Slack that records what it was asked and can be told to refuse."""

    def __init__(
        self,
        fail: "dict[str, str] | None" = None,
        profile: "dict[str, Any] | None" = None,
    ) -> None:
        self.fail = fail or {}
        self.profile = dict(_PROFILE if profile is None else profile)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_ts = iter([_TS, _TS2, "1758123500.123456", "1758123501.123456"])

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        error = self.fail.get(method)
        if error:
            raise _FakeSlackError(error)

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self._record("auth_test", kwargs)
        return {"ok": True, "url": _URL, "user_id": "U0BOT", "team_id": "T0ONE"}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postMessage", kwargs)
        return {"ok": True, "ts": next(self.next_ts), "channel": kwargs["channel"]}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postEphemeral", kwargs)
        return {"ok": True, "message_ts": next(self.next_ts)}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_update", kwargs)
        return {"ok": True, "ts": kwargs["ts"], "channel": kwargs["channel"]}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_delete", kwargs)
        return {"ok": True, "ts": kwargs["ts"], "channel": kwargs["channel"]}

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("users_info", kwargs)
        return {
            "ok": True,
            "user": {
                "id": kwargs["user"],
                # Slack sends the handle beside the profile. Nothing may show it.
                "name": _HANDLE,
                "profile": dict(self.profile),
            },
        }

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_open", kwargs)
        return {"ok": True, "channel": {"id": _DM}}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_members", kwargs)
        return {"ok": True, "members": list(_MEMBERS.get(kwargs["channel"], []))}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", kwargs)
        channel = str(kwargs["channel"])
        if channel.startswith("D"):
            # A direct message, which is never public whatever the flag says.
            return {
                "ok": True,
                "channel": {"id": channel, "is_im": True, "is_private": True},
            }
        return {
            "ok": True,
            "channel": {
                "id": channel,
                "is_channel": True,
                "is_private": channel != _PUBLIC,
            },
        }


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the token and Block Kit settings read by the toolkit."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(
    workspace: _Workspace,
    *,
    enforced: bool = True,
    asker: "str | None" = _PERSON,
    cron: bool = False,
    chat: str = _HERE,
) -> SlackPostToolkit:
    metadata: dict[str, Any] = {
        "slack_channel_id": chat,
        METADATA_ENFORCE_AUDIENCE_KEY: enforced,
    }
    if asker:
        metadata[METADATA_ASKER_KEY] = asker
    if cron:
        metadata[METADATA_ORIGIN_KEY] = ORIGIN_CRON_JOB
    return SlackPostToolkit(metadata=metadata, client=workspace)


def _run(coro: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(coro))


def _cards(toolkit: SlackPostToolkit) -> dict[str, Any]:
    cards = {}
    for tool in toolkit.get_tools():
        card = getattr(tool, "card", None) or tool._card
        cards[card.name] = card
    return cards


# ── 1. the three calls ───────────────────────────────────────────────────────


def test_a_message_is_posted_into_the_conversation_the_turn_came_from() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message(text="the report is ready"))

    assert result["ok"] is True
    assert result["chat_id"] == _HERE
    assert result["message_id"] == _TS
    # Built rather than asked for: auth.test already answers the workspace url
    # and needs no scope, where chat.getPermalink would be a method to classify
    # and a grant to ask an operator for.
    assert result["permalink"] == f"{_URL}archives/{_HERE}/p1758123456123456"
    assert result["parts"] == 1
    assert "part_message_ids" not in result
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["channel"] == _HERE
    assert posted["text"] == "the report is ready"
    # None of the four identity arguments, whatever else is sent.
    for withheld in ("username", "icon_emoji", "icon_url", "as_user"):
        assert withheld not in posted


def test_a_message_is_edited_in_place() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).edit_message(message_id=_TS, text="the report is late")
    )

    assert result == {
        "ok": True,
        "chat_id": _HERE,
        "message_id": _TS,
        "permalink": f"{_URL}archives/{_HERE}/p1758123456123456",
    }
    assert workspace.sent("chat_update")[0] == {
        "channel": _HERE,
        "ts": _TS,
        "text": "the report is late",
    }


def test_a_message_is_deleted() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).delete_message(message_id=_TS))

    assert result == {
        "ok": True,
        "chat_id": _HERE,
        "message_id": _TS,
        "deleted": True,
    }
    assert workspace.sent("chat_delete")[0] == {"channel": _HERE, "ts": _TS}


def test_a_thread_reply_names_the_message_it_answers() -> None:
    workspace = _Workspace()
    _run(
        _toolkit(workspace).post_message(
            text="answering", reply_to_message_id=_TS, also_post_to_chat=True
        )
    )
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["thread_ts"] == _TS
    assert posted["reply_broadcast"] is True


def test_a_long_message_is_split_and_the_parts_are_threaded_under_the_first() -> None:
    workspace = _Workspace()
    long_text = "\n\n".join("paragraph " + "x" * 2000 for _ in range(30))
    result = _run(_toolkit(workspace).post_message(text=long_text))

    assert result["parts"] == 2
    assert result["message_id"] == _TS
    assert result["part_message_ids"] == [_TS, _TS2]
    second = workspace.sent("chat_postMessage")[1]
    assert second["thread_ts"] == _TS


def test_an_edit_too_long_for_slack_is_refused_rather_than_cut() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).edit_message(message_id=_TS, text="y" * 5000)
    )
    assert result["ok"] is False
    assert result["error"] == "edit_text_too_long"
    assert workspace.methods() == []


# ── 2. the audience rule on the writing side ─────────────────────────────────


def test_a_request_with_no_audience_decision_mounts_nothing_and_refuses() -> None:
    assert slack_post_request_metadata("slack", {"slack_channel_id": _HERE}) == {}
    workspace = _Workspace()
    toolkit = SlackPostToolkit(
        metadata={"slack_channel_id": _HERE}, client=workspace
    )
    result = _run(toolkit.post_message(text="hello"))
    assert result["ok"] is False
    assert result["error"] == "audience_unsettled"
    assert workspace.methods() == []


def test_the_bound_lifted_still_mounts_the_tools() -> None:
    assert slack_post_request_metadata(
        "slack",
        {"slack_channel_id": _HERE, METADATA_ENFORCE_AUDIENCE_KEY: False},
    )


def test_all_three_cards_declare_chat_id() -> None:
    cards = _cards(_toolkit(_Workspace()))
    assert set(cards) == {POST_MESSAGE, EDIT_MESSAGE, DELETE_MESSAGE}
    for name, card in cards.items():
        assert "chat_id" in card.input_params["properties"], name


def test_the_card_states_both_halves_of_the_rule() -> None:
    """A model told only the member-list half gets the public case backwards."""
    cards = _cards(_toolkit(_Workspace()))
    for name in (POST_MESSAGE, EDIT_MESSAGE):
        text = cards[name].description
        assert "whose readers can already read this one" in text, name
        assert "public channel reaches the whole workspace" in text, name


def test_a_write_goes_silently_where_everybody_there_is_already_here() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message(chat_id=_NARROW, text="hello"))
    assert result["ok"] is True
    assert result["chat_id"] == _NARROW
    assert asyncio.run(
        _toolkit(_Workspace()).widening_question(POST_MESSAGE, {"chat_id": _NARROW})
    ) is None


def test_a_write_asks_before_it_reaches_somebody_who_is_not_here() -> None:
    question = asyncio.run(
        _toolkit(_Workspace()).widening_question(POST_MESSAGE, {"chat_id": _WIDE})
    )
    assert question is not None
    assert _WIDE in question
    assert _STRANGER in question
    assert "not in this conversation" in question


def test_an_edit_into_a_wider_conversation_asks_for_the_same_reason() -> None:
    """An edit shows the message to T's readers exactly as a post does."""
    question = asyncio.run(
        _toolkit(_Workspace()).widening_question(EDIT_MESSAGE, {"chat_id": _WIDE})
    )
    assert question is not None
    assert _STRANGER in question


def test_a_public_target_asks_and_describes_the_workspace() -> None:
    question = asyncio.run(
        _toolkit(_Workspace()).widening_question(POST_MESSAGE, {"chat_id": _PUBLIC})
    )
    assert question is not None
    assert "public channel" in question
    assert "workspace" in question


def test_write_audience_reads_members_beyond_page_25() -> None:
    class ManyPages(_Workspace):
        async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
            self._record("conversations_members", kwargs)
            index = int(kwargs.get("cursor") or 0)
            return {
                "members": [f"U{index}"],
                "response_metadata": {"next_cursor": str(index + 1) if index < 25 else ""},
            }

    workspace = ManyPages()
    members = asyncio.run(_toolkit(workspace)._conversation_members(_HERE))

    assert len(members) == 26
    assert "U25" in members
    assert len(workspace.sent("conversations_members")) == 26


def test_a_public_source_writes_anywhere_without_a_question() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, chat=_PUBLIC).post_message(chat_id=_WIDE, text="notice")
    )
    assert result["ok"] is True
    assert result["chat_id"] == _WIDE
    assert asyncio.run(
        _toolkit(_Workspace(), chat=_PUBLIC).widening_question(
            POST_MESSAGE, {"chat_id": _WIDE}
        )
    ) is None


def test_delete_never_asks_and_reads_no_membership_to_decide() -> None:
    """It removes a message and widens nothing, so there is nothing to put."""
    workspace = _Workspace()
    result = _run(_toolkit(workspace).delete_message(chat_id=_WIDE, message_id=_TS))
    assert result["ok"] is True
    assert result["chat_id"] == _WIDE
    assert "conversations_members" not in workspace.methods()
    assert asyncio.run(
        _toolkit(_Workspace()).widening_question(DELETE_MESSAGE, {"chat_id": _WIDE})
    ) is None


def test_the_bound_lifted_writes_anywhere_and_asks_nothing() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, enforced=False).post_message(
            chat_id=_WIDE, text="announcement"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _WIDE
    assert "conversations_members" not in workspace.methods()
    assert asyncio.run(
        _toolkit(_Workspace(), enforced=False).widening_question(
            POST_MESSAGE, {"chat_id": _WIDE}
        )
    ) is None



# ── 3. the two restrictions that are in code ─────────────────────────────────


def test_a_widening_write_with_nobody_to_ask_is_refused() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            chat_id=_WIDE, text="the nightly summary"
        )
    )
    assert result["ok"] is False
    assert result["error"] == "write_target_widens_audience"
    assert "scheduled run has no requester" in result["detail"]
    assert "chat_postMessage" not in workspace.methods()


def test_a_scheduled_run_still_writes_where_nobody_new_would_see_it() -> None:
    """The refusal above is about widening, not about being a scheduled run."""
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            chat_id=_NARROW, text="the nightly summary"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _NARROW


def test_a_scheduled_run_is_refused_a_public_target_out_of_a_private_room() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            chat_id=_PUBLIC, text="the nightly summary"
        )
    )
    assert result["ok"] is False
    assert result["error"] == "write_target_widens_audience"
    assert "cannot be listed" in result["detail"]
    assert "chat_postMessage" not in workspace.methods()


def test_a_scheduled_run_out_of_a_public_source_writes_anywhere() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, chat=_PUBLIC, asker=None, cron=True).post_message(
            chat_id=_WIDE, text="the nightly summary"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _WIDE


def test_an_ephemeral_goes_to_the_requester() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            text="only you need this", visible_only_to_user_id=_PERSON
        )
    )
    assert result["ok"] is True
    assert result["ephemeral"] is True
    assert result["visible_only_to_user_id"] == _PERSON
    # No permalink, and the absence is the fact: an ephemeral is in nobody's
    # archive, so there is no url that would reach it.
    assert "permalink" not in result
    assert workspace.sent("chat_postEphemeral")[0]["user"] == _PERSON


def test_an_ephemeral_to_anybody_else_is_refused_in_code() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            text="psst", visible_only_to_user_id=_STRANGER
        )
    )
    assert result["ok"] is False
    assert result["error"] == "ephemeral_requires_the_requester"
    assert workspace.methods() == []


def test_an_ephemeral_is_unavailable_to_a_turn_nobody_started() -> None:
    """No requester, so nothing to compare, and no special rule needed for it."""
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            text="psst", visible_only_to_user_id=_PERSON
        )
    )
    assert result["ok"] is False
    assert result["error"] == "ephemeral_requires_a_requester"
    assert workspace.methods() == []


def test_a_chat_id_that_names_a_person_opens_the_direct_message() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, enforced=False).post_message(
            chat_id=_STRANGER, text="a quiet word"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _DM
    assert workspace.sent("conversations_open")[0] == {"users": _STRANGER}
    assert workspace.sent("chat_postMessage")[0]["channel"] == _DM


def test_a_direct_message_is_held_to_the_rule_every_other_target_is() -> None:
    assert asyncio.run(
        _toolkit(_Workspace()).widening_question(
            POST_MESSAGE, {"chat_id": _COLLEAGUE}
        )
    ) is None
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(chat_id=_COLLEAGUE, text="a quiet word")
    )
    assert result["ok"] is True
    assert result["chat_id"] == _DM

    question = asyncio.run(
        _toolkit(_Workspace()).widening_question(POST_MESSAGE, {"chat_id": _STRANGER})
    )
    assert question is not None
    assert _STRANGER in question
    assert "privately" in question
    assert "not in this conversation" in question


def test_a_direct_message_audience_is_read_off_the_id_rather_than_fetched() -> None:
    workspace = _Workspace()
    asyncio.run(
        _toolkit(workspace).widening_question(POST_MESSAGE, {"chat_id": _STRANGER})
    )
    assert "conversations_open" not in workspace.methods()
    assert [
        kwargs["channel"] for kwargs in workspace.sent("conversations_members")
    ] == [_HERE]


def test_a_direct_message_to_the_requester_goes_through_the_general_rule() -> None:
    workspace = _Workspace()
    assert asyncio.run(
        _toolkit(workspace).widening_question(POST_MESSAGE, {"chat_id": _PERSON})
    ) is None
    assert "conversations_members" in workspace.methods()


def test_a_direct_message_with_nobody_to_ask_puts_no_question() -> None:
    assert asyncio.run(
        _toolkit(_Workspace(), asker=None, cron=True).widening_question(
            POST_MESSAGE, {"chat_id": _STRANGER}
        )
    ) is None
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            chat_id=_STRANGER, text="the nightly summary"
        )
    )
    assert result["ok"] is False
    assert result["error"] == "write_target_widens_audience"
    assert "chat_postMessage" not in workspace.methods()


def test_a_direct_message_source_is_compared_as_the_two_people_in_it() -> None:
    assert asyncio.run(
        _toolkit(_Workspace(), chat=_DM).widening_question(
            POST_MESSAGE, {"chat_id": _STRANGER}
        )
    ) is None
    question = asyncio.run(
        _toolkit(_Workspace(), chat=_DM).widening_question(
            POST_MESSAGE, {"chat_id": _COLLEAGUE}
        )
    )
    assert question is not None
    assert _COLLEAGUE in question


# ── 4. text, blocks, and the one set of checks ───────────────────────────────


def test_text_goes_through_the_renderer_an_ordinary_reply_goes_through() -> None:
    workspace = _Workspace()
    _run(
        _toolkit(workspace).post_message(
            text="## Results\n\n**done**\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        )
    )
    posted = workspace.sent("chat_postMessage")[0]
    # mrkdwn, not Markdown: a heading is bold and ``**`` is a single asterisk.
    assert "*Results*" in posted["text"]
    assert "**done**" not in posted["text"]
    # And the table reached Block Kit rather than the conversation as pipes.
    assert any(
        block.get("type") in {"table", "data_table"} for block in posted["blocks"]
    )


def test_declared_blocks_are_sent_and_the_text_becomes_the_fallback() -> None:
    """Declared blocks retain text as their notification fallback."""
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    result = _run(
        _toolkit(workspace).post_message(text="Deploy finished", blocks=blocks)
    )
    assert result["ok"] is True
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["blocks"] == blocks
    assert posted["text"] == "Deploy finished"


def test_declared_blocks_go_through_the_gate_a_fence_goes_through() -> None:
    """Declared blocks obey the same interactive policy as fences."""
    interactive = [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "approve",
                }
            ],
        }
    ]
    # The same payload, refused by the same function the fence path calls.
    assert slack_blocks.apply_block_policy(interactive) is None

    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(text="Deploy finished", blocks=interactive)
    )
    # Refused, and the text is posted in their place rather than silently
    # dropping what the model asked for.
    assert result["ok"] is True
    posted = workspace.sent("chat_postMessage")[0]
    assert "blocks" not in posted
    assert posted["text"] == "Deploy finished"


@pytest.mark.parametrize(
    ("tool_name", "api_method"),
    [("post_message", "chat_postMessage"), ("edit_message", "chat_update")],
)
def test_declared_blocks_refuse_controls_on_post_and_edit(
    tool_name: str, api_method: str
) -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace)
    blocks = [{
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "Continue"},
            "action_id": "continue",
        }],
    }]
    kwargs = {"text": "Continue", "blocks": blocks}
    if tool_name == "edit_message":
        kwargs["message_id"] = _TS

    assert _run(getattr(toolkit, tool_name)(**kwargs))["ok"] is True
    assert workspace.sent(api_method)[-1].get("blocks") in (None, [])


def test_refused_blocks_with_no_text_are_a_refusal_rather_than_a_blank_message() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            blocks=[{"type": "input", "element": {"type": "plain_text_input"}}]
        )
    )
    assert result["ok"] is False
    assert result["error"] == "blocks_refused"
    assert workspace.methods() == []


def test_neither_text_nor_blocks_is_refused_before_slack_is_asked() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message())
    assert result["ok"] is False
    assert result["error"] == "nothing_to_post"
    assert workspace.methods() == []


def test_a_non_string_fallback_is_refused_before_slack_is_asked() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message(text=7))
    assert result["error"] == "text_invalid" and workspace.methods() == []


@pytest.mark.parametrize("length", [MAX_SLACK_POST_FALLBACK_LENGTH, MAX_SLACK_POST_FALLBACK_LENGTH + 1])
def test_the_declared_fallback_is_preserved_or_refused(length: int) -> None:
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    result = _run(_toolkit(workspace).post_message(text="z" * length, blocks=blocks))
    if length == MAX_SLACK_POST_FALLBACK_LENGTH:
        assert result["ok"] is True and result["parts"] == 1
        assert workspace.sent("chat_postMessage")[0]["text"] == "z" * length
    else:
        assert result["error"] == "post_text_too_long"
        assert f"text is {length} characters" in result["detail"]
        assert workspace.methods() == []


@pytest.mark.parametrize("length", [4000, 4001])
def test_declared_edit_fallback_is_preserved_or_refused(length: int) -> None:
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    result = _run(_toolkit(workspace).edit_message(message_id=_TS, text="z" * length, blocks=blocks))
    if length == 4000:
        assert result["ok"] is True
        assert workspace.sent("chat_update")[0]["text"] == "z" * length
    else:
        assert result["error"] == "edit_text_too_long"
        assert f"text is {length} characters" in result["detail"]
        assert workspace.methods() == []


def test_attributed_declared_fallback_refuses_when_disclosure_exceeds_post_limit() -> None:
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    result = _run(_toolkit(workspace).post_message(
        chat_id=_NARROW, text="z" * MAX_SLACK_POST_FALLBACK_LENGTH, blocks=blocks, as_requester=True,
    ))
    assert result["error"] == "post_text_too_long"
    assert workspace.sent("chat_postMessage") == []


def test_oversized_fallback_refuses_before_opening_a_destination_dm() -> None:
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]

    posted = _run(_toolkit(workspace).post_message(
        chat_id=_STRANGER, text="z" * (MAX_SLACK_POST_FALLBACK_LENGTH + 1), blocks=blocks,
    ))
    edited = _run(_toolkit(workspace).edit_message(
        chat_id=_STRANGER, message_id=_TS, text="z" * 4001, blocks=blocks,
    ))

    assert posted["error"] == "post_text_too_long"
    assert edited["error"] == "edit_text_too_long"
    assert "conversations_open" not in workspace.methods()


def test_blocks_only_post_and_edit_keep_the_blocks() -> None:
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    assert _run(_toolkit(workspace).post_message(blocks=blocks))["ok"] is True
    assert _run(_toolkit(workspace).edit_message(message_id=_TS, blocks=blocks))["ok"] is True
    assert workspace.sent("chat_postMessage")[0]["blocks"] == blocks
    assert workspace.sent("chat_update")[0]["blocks"] == blocks


def test_declared_blocks_keep_whitespace_only_fallback_text() -> None:
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    assert _run(_toolkit(workspace).post_message(text="  ", blocks=blocks))["ok"] is True
    assert _run(_toolkit(workspace).edit_message(message_id=_TS, text="  ", blocks=blocks))["ok"] is True
    assert workspace.sent("chat_postMessage")[0]["text"] == "  "
    assert workspace.sent("chat_update")[0]["text"] == "  "


@pytest.mark.parametrize(
    ("written", "tables"),
    [
        ({}, slack_blocks.RENDER_TABLES_DEFAULT),
        ({"render_tables": False}, slack_blocks.RENDER_TABLES_OFF),
        ({"render_tables": True}, slack_blocks.RENDER_TABLES_DEFAULT),
        ({"render_tables": "basic"}, slack_blocks.RENDER_TABLES_BASIC),
        ({"render_tables": "nonsense"}, slack_blocks.RENDER_TABLES_DEFAULT),
    ],
)
def test_table_setting_normalizes_config(
    written: dict[str, Any],
    tables: str,
) -> None:
    assert slack_blocks.render_tables_setting(written) == tables


# ── 5. the request the tools will and will not serve ─────────────────────────


def test_the_metadata_provider_fails_closed() -> None:
    """Four conditions, each of which alone mounts nothing."""
    settled = {
        "slack_channel_id": _HERE,
        METADATA_ENFORCE_AUDIENCE_KEY: True,
    }
    assert slack_post_request_metadata("slack", settled) == settled

    # Not a Slack request at all.
    assert slack_post_request_metadata("web", settled) == {}
    # A cron request that the scheduler did not mark as its own: a stray
    # slack_channel_id left on some other request cannot reach these tools.
    assert slack_post_request_metadata("__cron__", settled) == {}
    assert slack_post_request_metadata(
        "__cron__", {**settled, METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}
    ) == {**settled, METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}
    # No conversation, so nowhere to post and nothing to fall back to.
    assert slack_post_request_metadata(
        "slack", {METADATA_ENFORCE_AUDIENCE_KEY: True}
    ) == {}
    # No decision: nobody with the configuration settled this request, which is a
    # different thing from settling it to false and is the one that mounts
    # nothing.
    assert slack_post_request_metadata("slack", {"slack_channel_id": _HERE}) == {}
    # Nothing at all.
    assert slack_post_request_metadata(None, None) == {}
    assert slack_post_request_metadata("slack", None) == {}


def test_a_provider_that_raises_refuses_rather_than_posting() -> None:
    def _explode() -> dict[str, Any]:
        raise RuntimeError("the context is gone")

    toolkit = SlackPostToolkit(metadata_provider=_explode, client=_Workspace())
    result = _run(toolkit.post_message(text="hello"))
    assert result["ok"] is False
    assert result["error"] == "audience_unsettled"


def test_a_malformed_message_id_never_reaches_slack() -> None:
    workspace = _Workspace()
    for call in (
        _toolkit(workspace).edit_message(message_id="yesterday", text="x"),
        _toolkit(workspace).delete_message(message_id="yesterday"),
    ):
        result = _run(call)
        assert result["error"] == "message_id_malformed"
    assert workspace.methods() == []


def test_a_slack_refusal_names_the_scope_it_wants() -> None:
    workspace = _Workspace(fail={"conversations_open": "missing_scope"})
    result = _run(
        _toolkit(workspace, enforced=False).post_message(
            chat_id=_STRANGER, text="a quiet word"
        )
    )
    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert "im:write" in result["detail"]
    assert "reinstalling the Slack app" in result["detail"]


# ── 6. the rail that puts the question ───────────────────────────────────────


class _Inputs:
    def __init__(self, tool_name: str, tool_args: Any) -> None:
        self.tool_name = tool_name
        self.tool_args = tool_args


class _Ctx:
    def __init__(self, tool_name: str, tool_args: Any) -> None:
        self.inputs = _Inputs(tool_name, tool_args)
        self.extra: dict[str, Any] = {}
        self.session = None


def _decide(
    toolkit: SlackPostToolkit,
    tool_name: str,
    arguments: Any,
    user_input: Any = None,
) -> Any:
    rail = SlackWriteConfirmationRail(toolkit)
    return asyncio.run(
        rail.resolve_interrupt(_Ctx(tool_name, arguments), None, user_input)
    )


def test_the_rail_approves_a_call_that_widens_nothing() -> None:
    decision = _decide(_toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _NARROW})
    assert type(decision).__name__ == "ApproveResult"


def test_the_rail_interrupts_with_the_question_the_toolkit_wrote() -> None:
    toolkit = _toolkit(_Workspace())
    decision = _decide(toolkit, POST_MESSAGE, json.dumps({"chat_id": _WIDE}))
    assert type(decision).__name__ == "InterruptResult"
    expected = asyncio.run(toolkit.widening_question(POST_MESSAGE, {"chat_id": _WIDE}))
    assert decision.request.message == expected
    assert [option["label"] for option in decision.request.ui_options] == [
        "Post it",
        "Do not post it",
    ]
    # Nothing is remembered: what is approved is an audience, and an audience is
    # exactly what differs between one call and the next.
    assert decision.request.auto_confirm_key == ""


def test_the_rail_skips_an_operator_tool_with_the_same_name() -> None:
    toolkit = _toolkit(_Workspace())
    tool = next(tool for tool in toolkit.get_tools() if tool.card.name == POST_MESSAGE)
    rail = SlackWriteConfirmationRail(toolkit, tools=[tool])
    ctx = _Ctx(POST_MESSAGE, {"chat_id": _WIDE})
    ctx.agent = SimpleNamespace(ability_manager={POST_MESSAGE: tool.card})
    assert type(asyncio.run(rail.resolve_interrupt(ctx, None, None))).__name__ == "InterruptResult"

    ctx.agent.ability_manager[POST_MESSAGE] = SimpleNamespace(name=POST_MESSAGE)
    assert type(asyncio.run(rail.resolve_interrupt(ctx, None, None))).__name__ == "ApproveResult"


def test_the_rail_approves_once_the_person_agrees() -> None:
    decision = _decide(
        _toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _WIDE}, "allow_once"
    )
    assert type(decision).__name__ == "ApproveResult"


def test_a_declined_confirmation_is_reported_as_a_refusal_the_model_knows() -> None:
    decision = _decide(
        _toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _WIDE}, "reject"
    )
    assert type(decision).__name__ == "RejectResult"
    result = json.loads(decision.tool_result)
    assert result["ok"] is False
    assert result["error"] == "write_confirmation_declined"


def test_an_unreadable_answer_is_asked_again_rather_than_read_as_either() -> None:
    """A mis-parsed yes posts what nobody agreed to; a mis-parsed no invents a refusal."""
    decision = _decide(
        _toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _WIDE}, object()
    )
    assert type(decision).__name__ == "InterruptResult"


def test_the_rail_leaves_delete_alone() -> None:
    assert DELETE_MESSAGE not in SlackWriteConfirmationRail(
        _toolkit(_Workspace())
    ).get_tools()


# ── 7. attribution ───────────────────────────────────────────────────────────
#
# One flag, and what it is for: somebody asks this bot to say something in
# another conversation, and the reader there can see whose errand it was. It is
# attribution rather than impersonation, and the shape is what makes the
# difference. No argument names a person, so the only name available is the one
# trusted request metadata already holds; and every such message says this app
# posted it, which the model cannot pass, withhold or reword.


def _marker() -> str:
    return f"Posted by <@U0BOT> on behalf of <@{_PERSON}>"


def _context_notes(posted: dict[str, Any]) -> list[str]:
    return [
        element["text"]
        for block in posted.get("blocks") or []
        if block.get("type") == "context"
        for element in block["elements"]
    ]


def test_the_flag_left_off_changes_nothing_about_the_post() -> None:
    """The default is the behaviour the tool had before the flag existed."""
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message(chat_id=_NARROW, text="hello"))

    assert result["ok"] is True
    assert "as_requester" not in result
    # The directory is not read, so a deployment that never sets the flag spends
    # no users:read allowance on it.
    assert "users_info" not in workspace.methods()
    posted = workspace.sent("chat_postMessage")[0]
    for withheld in ("username", "icon_emoji", "icon_url", "as_user"):
        assert withheld not in posted
    assert posted["text"] == "hello"
    assert _context_notes(posted) == []


def test_an_attributed_post_shows_the_askers_name_face_and_the_disclosure() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="the report is ready", as_requester=True
        )
    )

    assert result["ok"] is True
    assert result["as_requester"] == {
        "user_id": _PERSON,
        "name": "Ada",
        "icon": "the asker's",
    }
    assert workspace.sent("users_info") == [{"user": _PERSON}]
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["username"] == "Ada"
    assert posted["icon_url"] == _PROFILE["image_512"]
    # The legacy handle is a login name Slack shows nobody. Naming the person by
    # it would be naming them wrongly.
    assert _HANDLE not in json.dumps(posted)
    assert _context_notes(posted) == [_marker()]
    assert posted["blocks"][0]["type"] == "context"
    assert posted["blocks"][0]["elements"] == [
        {"type": "mrkdwn", "text": _marker()}
    ]
    assert posted["text"].startswith(_marker())


def test_missing_bot_identity_uses_the_configured_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from belle_agent.integrations.jiuwenswarm import slack_history

    monkeypatch.setattr(slack_history, "load_belle_config", lambda: {"platforms": {"slack": {"bot_name": "Scribe & Co"}}})
    workspace = _Workspace(fail={"auth_test": "invalid_auth"})
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["ok"] is True
    posted = workspace.sent("chat_postMessage")[0]
    assert _context_notes(posted) == [
        f"Posted by Scribe & Co on behalf of <@{_PERSON}>"
    ]
    assert posted["text"].startswith(
        f"Posted by Scribe & Co on behalf of <@{_PERSON}>"
    )


def test_a_text_only_post_puts_the_disclosure_before_the_sections() -> None:
    workspace = _Workspace()
    _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="the report is ready", as_requester=True
        )
    )
    blocks = workspace.sent("chat_postMessage")[0]["blocks"]
    assert blocks[1] == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "the report is ready"},
    }
    assert blocks[0]["type"] == "context"


def test_declared_blocks_keep_their_shape_and_gain_the_note() -> None:
    workspace = _Workspace()
    declared = [{"type": "section", "text": {"type": "mrkdwn", "text": "*done*"}}]
    _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW,
            text="the report is ready",
            blocks=list(declared),
            as_requester=True,
        )
    )
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["blocks"][1:] == declared
    assert _context_notes(posted) == [_marker()]


def test_the_notification_states_the_disclosure_as_well() -> None:
    workspace = _Workspace()
    _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW,
            text="the report is ready",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*x*"}}],
            as_requester=True,
        )
    )
    assert workspace.sent("chat_postMessage")[0]["text"] == (
        f"{_marker()}\nthe report is ready"
    )


def test_every_part_of_a_split_message_says_who_posted_it() -> None:
    """Each part is its own message under that name, so each one discloses."""
    workspace = _Workspace()
    long_text = "\n\n".join("paragraph " + "x" * 2000 for _ in range(30))
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text=long_text, as_requester=True
        )
    )
    assert result["parts"] == 2
    posts = workspace.sent("chat_postMessage")
    assert len(posts) == 2
    for posted in posts:
        assert posted["username"] == "Ada"
        assert _context_notes(posted) == [_marker()]
        assert posted["text"].startswith(_marker())


@pytest.mark.parametrize("length", [MAX_SLACK_TEXT_LENGTH, MAX_SLACK_TEXT_LENGTH + 1])
def test_attribution_survives_the_split_boundary(length: int) -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="x" * length, as_requester=True
        )
    )
    posts = workspace.sent("chat_postMessage")
    assert result["ok"] is True
    assert len(posts) == (1 if length == MAX_SLACK_TEXT_LENGTH else 2)
    assert "".join(
        block["text"]["text"]
        for post in posts
        for block in post["blocks"][1:]
    ) == "x" * length
    for post in posts:
        assert post["username"] == "Ada"
        assert post["icon_url"] == _PROFILE["image_512"]
        assert _context_notes(post) == [_marker()]
        assert post["text"].startswith(_marker())
        assert len(post["text"]) <= MAX_SLACK_POST_FALLBACK_LENGTH
        visible_text = "".join(block["text"]["text"] for block in post["blocks"][1:])
        assert post["text"] == f"{_marker()}\n{visible_text}"


def test_missing_bot_identity_keeps_a_plain_disclosure() -> None:
    workspace = _Workspace(fail={"auth_test": "invalid_auth"})
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["ok"] is True
    assert _context_notes(workspace.sent("chat_postMessage")[0]) == [
        f"Posted by {_BOT} on behalf of <@{_PERSON}>"
    ]


def test_invalid_bot_user_id_keeps_a_plain_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _Workspace()

    async def auth_test(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "url": _URL, "user_id": "not-a-user-id"}

    monkeypatch.setattr(workspace, "auth_test", auth_test)
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["permalink"] == f"{_URL}archives/{_NARROW}/p1758123456123456"
    assert _context_notes(workspace.sent("chat_postMessage")[0]) == [
        f"Posted by {_BOT} on behalf of <@{_PERSON}>"
    ]


def test_an_empty_display_name_falls_back_to_the_real_name() -> None:
    """``profile.display_name`` is unset on most Slack accounts."""
    workspace = _Workspace(profile={"display_name": "", "real_name": "Ada Lovelace"})
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["as_requester"]["name"] == "Ada Lovelace"
    assert workspace.sent("chat_postMessage")[0]["username"] == "Ada Lovelace"


def test_no_picture_keeps_the_apps_own_icon_and_posts_anyway() -> None:
    """The name is what attributes the message, so the post goes without a face."""
    workspace = _Workspace(profile={"display_name": "Ada"})
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["ok"] is True
    assert result["as_requester"] == {
        "user_id": _PERSON,
        "name": "Ada",
        "icon": "this app's",
    }
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["username"] == "Ada"
    # Omitted, which is what leaves Slack drawing the app's own icon.
    assert "icon_url" not in posted


def test_a_directory_that_cannot_be_read_posts_as_the_app_and_still_discloses() -> None:
    workspace = _Workspace(fail={"users_info": "missing_scope"})
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["ok"] is True
    assert result["as_requester"] == {
        "user_id": _PERSON,
        "name": "",
        "icon": "this app's",
    }
    posted = workspace.sent("chat_postMessage")[0]
    assert "username" not in posted
    assert "icon_url" not in posted
    assert _context_notes(posted) == [_marker()]


def test_a_scheduled_run_has_nobody_to_attribute_a_message_to() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, cron=True).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["ok"] is False
    assert result["error"] == "as_requester_requires_a_requester"
    assert "scheduled run has no requester" in result["detail"]
    assert "chat_postMessage" not in workspace.methods()


def test_attribution_into_this_conversation_is_refused_rather_than_ignored() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message(text="hello", as_requester=True))
    assert result["ok"] is False
    assert result["error"] == "as_requester_needs_another_conversation"
    assert "chat_postMessage" not in workspace.methods()

    # The same answer where a chat_id names this conversation outright.
    assert _run(
        _toolkit(_Workspace()).post_message(
            chat_id=_HERE, text="hello", as_requester=True
        )
    )["error"] == "as_requester_needs_another_conversation"


def test_an_ephemeral_cannot_be_attributed() -> None:
    """Showing somebody their own name tells them what they already know."""
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            text="hello", visible_only_to_user_id=_PERSON, as_requester=True
        )
    )
    assert result["ok"] is False
    assert result["error"] == "as_requester_cannot_be_ephemeral"
    assert "chat_postEphemeral" not in workspace.methods()


def test_an_install_without_the_customize_grant_is_told_which_scope_it_wants() -> None:
    """``chat:write`` posts; a name and a face cost a grant of their own."""
    workspace = _Workspace(fail={"chat_postMessage": "missing_scope"})
    result = _run(
        _toolkit(workspace).post_message(
            chat_id=_NARROW, text="hello", as_requester=True
        )
    )
    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert "chat:write.customize" in result["detail"]
    assert "nothing was posted" in result["detail"]


def test_the_scope_is_classified_at_the_tools_tier_and_nowhere_lower() -> None:
    """An install that mounts no posting tool must not hold the grant."""
    assert slack_scope_policy.METHOD_OPTION_REQUIREMENTS[
        ("chat_postMessage", "as_requester")
    ].scopes == ("chat:write.customize",)
    for tier in (slack_scope_policy.TIER_CORE, slack_scope_policy.TIER_CONNECTOR):
        assert "chat:write.customize" not in slack_scope_policy.scopes_for_tier(tier)
    assert "chat:write.customize" in slack_scope_policy.scopes_for_tier(
        slack_scope_policy.TIER_TOOLS
    )


def test_the_widening_question_is_asked_for_an_attributed_post_too() -> None:
    toolkit = _toolkit(_Workspace())
    question = asyncio.run(
        toolkit.widening_question(
            POST_MESSAGE, {"chat_id": _WIDE, "as_requester": True}
        )
    )
    assert question == asyncio.run(
        toolkit.widening_question(POST_MESSAGE, {"chat_id": _WIDE})
    )
    assert question is not None and _STRANGER in question

    decision = _decide(
        _toolkit(_Workspace()),
        POST_MESSAGE,
        {"chat_id": _WIDE, "as_requester": True},
    )
    assert type(decision).__name__ == "InterruptResult"
    assert decision.request.message == question


def test_nothing_lets_a_caller_name_a_person_or_a_picture() -> None:
    properties = _cards(_toolkit(_Workspace()))[POST_MESSAGE].input_params[
        "properties"
    ]
    assert properties["as_requester"]["type"] == "boolean"
    assert properties["as_requester"]["default"] is False
    assert "this app posted it" in properties["as_requester"]["description"]
    for forbidden in ("username", "icon_url", "icon_emoji", "as_user", "name"):
        assert forbidden not in properties

    taken = set(
        inspect.signature(SlackPostToolkit.post_message).parameters
    )
    assert taken & {"username", "icon_url", "icon_emoji", "as_user"} == set()
    # The flag is on post_message alone: an edit cannot change who a message
    # came from, and Slack would refuse one that tried.
    for other in (SlackPostToolkit.edit_message, SlackPostToolkit.delete_message):
        assert "as_requester" not in inspect.signature(other).parameters
    for name in (EDIT_MESSAGE, DELETE_MESSAGE):
        assert "as_requester" not in _cards(_toolkit(_Workspace()))[
            name
        ].input_params["properties"]


# ── 8. a request that requires a destination ─────────────────────────────────
#
# A deployment where this app delivers no reply of its own leaves post_message
# as a conversation's whole voice. An omitted destination would then put every
# answer at that conversation's top level, including an answer to a message
# that arrived inside a thread. The connector says so on the request, and the
# tool refuses rather than guesses.


def _toolkit_requiring_a_target(workspace: _Workspace) -> SlackPostToolkit:
    """A toolkit for a request whose connector delivers no reply of its own."""
    return SlackPostToolkit(
        metadata={
            "slack_channel_id": _HERE,
            METADATA_ENFORCE_AUDIENCE_KEY: True,
            METADATA_ASKER_KEY: _PERSON,
            METADATA_WRITE_TARGET_REQUIRED_KEY: True,
        },
        client=workspace,
    )


def test_a_post_that_names_no_destination_is_refused() -> None:
    workspace = _Workspace()
    result = _run(_toolkit_requiring_a_target(workspace).post_message(text="done"))

    assert result["ok"] is False
    assert result["error"] == "write_target_required"
    assert workspace.methods() == []


def test_the_refusal_names_both_arguments_and_the_conversation_to_name() -> None:
    """A model reading it has everything the next call needs."""
    result = _run(_toolkit_requiring_a_target(_Workspace()).post_message(text="done"))
    detail = result["detail"]

    assert "chat_id" in detail
    assert "reply_to_message_id" in detail
    assert "valid destination" in detail
    # The originating conversation must be named, and the refusal reports its
    # id so that naming it costs no further call.
    assert result["chat_id"] == _HERE


def test_a_named_conversation_is_posted_to() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit_requiring_a_target(workspace).post_message(
            chat_id=_HERE, text="done"
        )
    )

    assert result["ok"] is True
    assert result["chat_id"] == _HERE
    assert workspace.sent("chat_postMessage")[0]["channel"] == _HERE


def test_a_thread_reply_needs_no_conversation_beside_the_message_it_answers() -> None:
    """``reply_to_message_id`` alone names a place, and a narrower one."""
    workspace = _Workspace()
    result = _run(
        _toolkit_requiring_a_target(workspace).post_message(
            text="answering", reply_to_message_id=_TS
        )
    )

    assert result["ok"] is True
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["channel"] == _HERE
    assert posted["thread_ts"] == _TS


def test_an_edit_and_a_delete_keep_defaulting_to_this_conversation() -> None:
    """Decided rather than overlooked.

    Both take a message id, and a message id resolves in one conversation and
    nowhere else. Neither call can land somewhere nobody chose: the message is
    in this conversation, or Slack reports that it is not. The requirement is
    about where a new message goes.
    """
    workspace = _Workspace()
    edited = _run(
        _toolkit_requiring_a_target(workspace).edit_message(
            message_id=_TS, text="later than expected"
        )
    )
    deleted = _run(
        _toolkit_requiring_a_target(workspace).delete_message(message_id=_TS)
    )

    assert edited["ok"] is True
    assert edited["chat_id"] == _HERE
    assert deleted["ok"] is True
    assert deleted["chat_id"] == _HERE
    assert workspace.sent("chat_update")[0]["channel"] == _HERE
    assert workspace.sent("chat_delete")[0]["channel"] == _HERE


def test_a_request_that_requires_nothing_still_posts_where_it_came_from() -> None:
    """The absent key is every deployment that delivers a reply of its own."""
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message(text="done"))

    assert result["ok"] is True
    assert result["chat_id"] == _HERE


def test_the_post_card_promises_no_default_the_tool_may_refuse() -> None:
    """One card serves every deployment, so it must hold under both."""
    cards = _cards(_toolkit(_Workspace()))
    posting = cards[POST_MESSAGE].input_params["properties"]["chat_id"]["description"]

    assert "Omit it to use this conversation." not in posting
    assert "reply_to_message_id" in posting
    # An edit and a delete are never refused for omitting it, so their own
    # wording keeps the plain promise.
    for name in (EDIT_MESSAGE, DELETE_MESSAGE):
        other = cards[name].input_params["properties"]["chat_id"]["description"]
        assert "Omit it to use this conversation." in other, name
