"""Slack canvas behavior, including read limits and partial access changes."""

import asyncio
import json
from typing import Any

import jiuwenswarm.common.config as config_module
import pytest
from slack_fakes import RecordingSlackClient

from belle_agent.integrations.jiuwenswarm.slack_canvases import (
    ACCESS_LEVELS,
    CANVAS_TOOL_NAMES,
    MAX_CONTENT_CHARS,
    MAX_MARKDOWN_CHARS,
    SECTION_TYPES,
    SlackCanvasToolkit,
    slack_canvas_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_tool_context import METADATA_ORIGIN_KEY, ORIGIN_CRON_JOB
from belle_agent.platforms.slack.history import SlackWorkspaceClients
from belle_agent.platforms.slack.history_policy import (
    METADATA_ENFORCE_AUDIENCE_KEY,
    METADATA_TEAM_KEY,
)

_CANVAS = "F1234ABCD"
_CHAT = "C000000AAAA"
_DM = "D000000AAAA"
_ALICE = "U000000AAAA"
_BOB = "U000000BBBB"
_CAROL = "U000000CCCC"
_HOST = "https://acme.slack.com"
_MARKDOWN = "# Runbook\n\nRestart the worker."
_HTML = (
    '<div class="quip-canvas-content">'
    '<h1 id="temp:C:Cbc9">Runbook</h1>'
    '<p id="temp:C:Cbd0">Restart the worker.</p>'
    "</div>"
)
class _Workspace(RecordingSlackClient):
    """A fake Slack that records every call and can refuse chosen ones."""

    def __init__(
        self,
        *,
        fail: Any = None,
        file_record: Any = None,
        sections: Any = None,
        content: str = _MARKDOWN,
    ) -> None:
        super().__init__(fail)
        self.file_record = (
            {"filetype": "canvas", "title": "Runbook", "channels": [_CHAT]}
            if file_record is None
            else file_record
        )
        self.sections = (
            [{"id": "temp:C:Cbc9"}, {"id": "temp:C:Cbd0"}]
            if sections is None
            else sections
        )
        self.content = content

    async def api_call(self, method: str, json: Any = None) -> dict[str, Any]:
        payload = dict(json or {})
        self.record_call(method, payload)
        if method == "canvases.sections.lookup":
            return {"ok": True, "sections": list(self.sections)}
        if method == "canvases.getContent":
            return {"ok": True, "content": _HTML if payload["content_type"] == "html" else self.content}
        return {"ok": True, "canvas_id": _CANVAS}

    async def files_info(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("files_info", dict(kwargs))
        return {"ok": True, "file": dict(self.file_record)}

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("auth_test", dict(kwargs)))
        return {"ok": True, "url": f"{_HOST}/", "user_id": "U0BOT"}

@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(
    workspace: _Workspace, *, workspaces: SlackWorkspaceClients | None = None, **metadata: Any
) -> SlackCanvasToolkit:
    base: dict[str, Any] = {
        METADATA_TEAM_KEY: "T000", "slack_channel_id": _CHAT,
        "slack_channel_type": "channel", METADATA_ENFORCE_AUDIENCE_KEY: True,
    }
    base.update(metadata)
    return SlackCanvasToolkit(
        metadata=base,
        client=workspace,
        workspaces=workspaces,
    )


def _run(coro: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(coro))


def _cards(toolkit: SlackCanvasToolkit) -> dict[str, Any]:
    cards: dict[str, Any] = {}
    for tool in toolkit.get_tools():
        card = getattr(tool, "card", None) or tool._card
        cards[card.name] = card
    return cards


def _all_cards() -> dict[str, Any]:
    return _cards(_toolkit(_Workspace()))


# ── reading ────────────────────────────────────────────────────────────────


def test_canvas_read_uses_native_markdown_and_optional_section_lookup() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).read_slack_canvas(
        canvas_id=_CANVAS, section_types=["any_header"],
    ))

    assert result["ok"] is True
    assert result["content"] == _MARKDOWN
    assert result["content_format"] == "markdown"
    assert result["content_coverage"] == "complete"
    assert result["sections"] == [
        {"section_id": "temp:C:Cbc9"},
        {"section_id": "temp:C:Cbd0"},
    ]
    assert workspace.payload("canvases.getContent") == {
        "canvas_id": _CANVAS, "content_type": "markdown",
    }


def test_canvas_section_limit_reports_full_count_and_can_be_raised() -> None:
    workspace = _Workspace(sections=[{"id": f"section-{index}"} for index in range(501)])
    limited = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS, contains_text="section"))
    settings = SlackWorkspaceClients(config_loader=lambda: {"canvas_sections_max_items": 501})
    complete = _run(_toolkit(workspace, workspaces=settings).read_slack_canvas(
        canvas_id=_CANVAS, contains_text="section",
    ))

    assert limited["sections_count"] == 500
    assert limited["sections_total"] == 501
    assert "sections_truncated" in "".join(limited["coverage"])
    assert complete["sections_count"] == complete["sections_total"] == 501
    assert "sections_truncated" not in "".join(complete.get("coverage", []))


def test_canvas_read_can_request_native_html() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).read_slack_canvas(
        canvas_id=_CANVAS, content_format="html",
    ))

    assert result["content"] == _HTML
    assert result["content_format"] == "html"
    assert workspace.payload("canvases.getContent")["content_type"] == "html"


def test_canvas_read_rejects_unknown_format_before_api_call() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).read_slack_canvas(
        canvas_id=_CANVAS, content_format="plain",
    ))

    assert result["error"] == "content_format_invalid"
    assert workspace.calls == []


def test_canvas_read_without_filter_skips_section_lookup() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))

    assert result["content"] == _MARKDOWN
    assert result["sections_filter"] == "none"
    assert "sections" not in result
    assert workspace.payloads("canvases.sections.lookup") == []


def test_failed_section_lookup_does_not_hide_authorized_content() -> None:
    workspace = _Workspace(fail={"canvases.sections.lookup": "missing_scope"})
    result = _run(_toolkit(workspace).read_slack_canvas(
        canvas_id=_CANVAS, section_types=["any_header"],
    ))

    assert result["content"] == _MARKDOWN
    assert "sections" not in result
    assert "sections_unavailable" in "".join(result["coverage"])


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"contains_text": "Runbook"}, {"contains_text": "Runbook"}),
        ({"section_types": ["h1"]}, {"section_types": ["h1"]}),
        (
            {"section_types": ["h1", "table"], "contains_text": "Runbook"},
            {"section_types": ["h1", "table"], "contains_text": "Runbook"},
        ),
    ],
)
def test_canvas_section_filters_reach_slack(filters: dict[str, Any], expected: dict[str, Any]) -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS, **filters))

    assert result["ok"] is True
    assert workspace.payload("canvases.sections.lookup")["criteria"] == expected


def test_unknown_section_type_never_reaches_slack() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).read_slack_canvas(
        canvas_id=_CANVAS, section_types=["heading"],
    ))

    assert result["error"] == "section_type_unknown"
    assert workspace.calls == []


def test_filterable_section_types_are_offered_as_an_enum() -> None:
    card = _all_cards()["read_slack_canvas"]
    enum = card.input_params["properties"]["section_types"]["items"]["enum"]

    assert enum == list(SECTION_TYPES)


def test_large_canvas_is_refused_without_returning_partial_content() -> None:
    workspace = _Workspace(content="x" * (MAX_CONTENT_CHARS + 1))
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "canvas_content_too_large"
    assert str(MAX_CONTENT_CHARS + 1) in result["detail"]
    assert "content" not in result


def test_canvas_limit_is_operator_configurable_at_exact_boundary() -> None:
    settings = SlackWorkspaceClients(config_loader=lambda: {"canvas_read_max_chars": 4})
    workspace = _Workspace(content="abcd")
    result = _run(_toolkit(workspace, workspaces=settings).read_slack_canvas(canvas_id=_CANVAS))
    assert result["content"] == "abcd"

    workspace.content = "abcde"
    result = _run(_toolkit(workspace, workspaces=settings).read_slack_canvas(canvas_id=_CANVAS))
    assert result["error"] == "canvas_content_too_large"
    assert "5" in result["detail"] and "4" in result["detail"]


def test_channel_canvas_link_is_a_file_home() -> None:
    workspace = _Workspace(file_record={
        "filetype": "canvas", "is_channel_space": True, "linked_channel_id": _CHAT,
    })
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))
    assert result["content"] == _MARKDOWN


def test_native_canvas_read_needs_no_private_download_url() -> None:
    workspace = _Workspace(file_record={
        "filetype": "canvas", "title": "Runbook", "channels": [_CHAT],
    })
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))

    assert result["content"] == _MARKDOWN


def test_canvas_without_proven_source_audience_is_refused_before_content_call() -> None:
    workspace = _Workspace(file_record={
        "filetype": "canvas", "title": "Private", "channels": [],
    })
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))

    assert result["ok"] is False
    assert result["error"] == "slack_file_home_unknown"
    assert workspace.payloads("canvases.getContent") == []


def test_missing_canvas_scope_refuses_content() -> None:
    workspace = _Workspace(fail={"canvases.getContent": "missing_scope"})
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "missing_scope"
    assert "canvases:read" in result["detail"]
    assert "content" not in result


def test_non_canvas_file_is_refused() -> None:
    workspace = _Workspace(file_record={"filetype": "pdf", "channels": [_CHAT]})
    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "not_a_canvas"
    assert workspace.payloads("canvases.getContent") == []


# ── 2. canvas_not_found names all three states ───────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        "read",
        "edit",
        "delete",
        "share",
    ],
)
def test_canvas_not_found_names_every_state_it_conflates(call: str) -> None:
    workspace = _Workspace(fail="canvas_not_found")
    toolkit = _toolkit(workspace)
    calls = {
        "read": lambda: toolkit.read_slack_canvas(canvas_id=_CANVAS),
        "edit": lambda: toolkit.edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "x"}],
        ),
        "delete": lambda: toolkit.delete_slack_canvas(canvas_id=_CANVAS),
        "share": lambda: toolkit.share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        ),
    }

    result = _run(calls[call]())

    assert result["error"] == "canvas_not_found"
    detail = result["detail"]
    assert "does not exist" in detail
    assert "deleted" in detail
    assert "never been shared with this app" in detail


# ── 3. creating, and why it is two tools ─────────────────────────────────────


def test_the_two_creations_are_two_tools_with_two_names() -> None:
    names = set(_all_cards())

    assert "write_slack_canvas" in names
    assert "write_slack_channel_canvas" in names
    assert set(CANVAS_TOOL_NAMES) == names


def test_a_standalone_canvas_is_created_with_slacks_content_wrapper() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_canvas(title="Runbook", markdown="# Hi")
    )

    assert result == {
        "ok": True,
        "canvas_id": _CANVAS,
        "is_channel_canvas": False,
    }
    assert workspace.payload("canvases.create") == {
        "title": "Runbook",
        "document_content": {"type": "markdown", "markdown": "# Hi"},
    }


def test_a_chat_id_tabs_a_standalone_canvas_into_a_conversation() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_canvas(markdown="hi", chat_id=_CHAT)
    )

    assert result["chat_id"] == _CHAT
    assert workspace.payload("canvases.create")["channel_id"] == _CHAT


def test_a_channel_canvas_goes_to_the_other_method_entirely() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_channel_canvas(
            chat_id=_CHAT, markdown="hi"
        )
    )

    assert result["is_channel_canvas"] is True
    assert workspace.payload("conversations.canvases.create")["channel_id"] == _CHAT
    assert workspace.payloads("canvases.create") == []


def test_a_channel_canvas_needs_a_conversation() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).write_slack_channel_canvas(chat_id=""))

    assert result["error"] == "chat_id_required"
    assert workspace.calls == []


@pytest.mark.parametrize("tool", ["standalone", "channel"])
def test_a_direct_message_holds_no_canvas_and_is_refused_before_the_call(
    tool: str,
) -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace)
    call = (
        toolkit.write_slack_canvas(markdown="hi", chat_id=_DM)
        if tool == "standalone"
        else toolkit.write_slack_channel_canvas(chat_id=_DM)
    )

    result = _run(call)

    assert result["error"] == "chat_id_is_a_direct_message"
    assert workspace.calls == []


def test_a_conversation_already_holding_its_canvas_is_told_there_is_no_second() -> None:
    workspace = _Workspace(fail="channel_canvas_already_exists")

    result = _run(
        _toolkit(workspace).write_slack_channel_canvas(chat_id=_CHAT)
    )

    assert result["error"] == "channel_canvas_already_exists"
    assert "one at a time" in result["detail"]
    assert "edit the existing canvas" in result["detail"]
    assert "no way to delete this one" not in result["detail"]


def test_the_free_plan_refusal_names_the_argument_that_fixes_it() -> None:
    workspace = _Workspace(fail="free_teams_cannot_create_standalone_canvases")

    result = _run(_toolkit(workspace).write_slack_canvas(markdown="hi"))

    assert result["error"] == "free_teams_cannot_create_standalone_canvases"
    assert "Pass chat_id" in result["detail"]


@pytest.mark.parametrize(
    "code",
    [
        "free_teams_cannot_edit_standalone_canvases",
        "free_team_canvas_tab_already_exists",
        "canvas_disabled_user_team",
        "canvas_globally_disabled",
        "restricted_action",
    ],
)
def test_every_plan_and_kill_switch_refusal_is_explained(code: str) -> None:
    workspace = _Workspace(fail=code)

    result = _run(_toolkit(workspace).write_slack_canvas(markdown="hi"))

    assert result["error"] == code
    assert result["detail"]
    assert f"Slack refused {code}" not in result["detail"]


def test_content_over_slacks_ceiling_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_canvas(
            markdown="x" * (MAX_MARKDOWN_CHARS + 1)
        )
    )

    assert result["error"] == "content_too_large"
    assert str(MAX_MARKDOWN_CHARS) in result["detail"]
    assert workspace.calls == []


# ── 4. editing ───────────────────────────────────────────────────────────────


def test_one_change_is_sent_as_slacks_change_object() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[
                {
                    "operation": "insert_after",
                    "section_id": "temp:C:Cbc9",
                    "markdown": "more",
                }
            ],
        )
    )

    assert result["ok"] is True
    assert workspace.payload("canvases.edit") == {
        "canvas_id": _CANVAS,
        "changes": [
            {
                "operation": "insert_after",
                "section_id": "temp:C:Cbc9",
                "document_content": {"type": "markdown", "markdown": "more"},
            }
        ],
    }


def test_rename_writes_the_title_wrapper_rather_than_the_document_one() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "rename", "title": "New title"}],
        )
    )

    change = workspace.payload("canvases.edit")["changes"][0]
    assert change["title_content"] == {"type": "markdown", "markdown": "New title"}
    assert "document_content" not in change


def test_a_title_is_named_the_same_way_on_every_canvas_tool() -> None:
    cards = _all_cards()

    for name in ("write_slack_canvas", "write_slack_channel_canvas"):
        assert "title" in cards[name].input_params["properties"]

    change = cards["edit_slack_canvas"].input_params["properties"]["changes"]
    assert "title" in change["items"]["properties"]
    assert "title_markdown" not in change["items"]["properties"]


def test_a_rename_without_a_title_names_the_argument_it_wanted() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS, changes=[{"operation": "rename"}]
        )
    )

    assert result["error"] == "change_content_required"
    assert "Pass it as title" in result["detail"]
    assert workspace.calls == []


def test_a_rename_to_whitespace_is_refused_like_a_rename_to_nothing() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS, changes=[{"operation": "rename", "title": "   "}]
        )
    )

    assert result["error"] == "change_content_required"
    assert workspace.calls == []


def test_a_rename_title_keeps_its_inner_spacing() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "rename", "title": "  Q3  plan  "}],
        )
    )

    change = workspace.payload("canvases.edit")["changes"][0]
    assert change["title_content"]["markdown"] == "Q3  plan"


def test_document_content_is_not_stripped_with_the_rename_title() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[
                {
                    "operation": "insert_at_end",
                    "markdown": "    indented code\n",
                }
            ],
        )
    )

    change = workspace.payload("canvases.edit")["changes"][0]
    assert change["document_content"]["markdown"] == "    indented code\n"


def test_a_blank_title_on_create_asks_for_an_untitled_canvas() -> None:
    workspace = _Workspace()

    _run(_toolkit(workspace).write_slack_canvas(title="   ", markdown="body"))

    assert "title" not in workspace.payload("canvases.create")


def test_more_than_one_change_is_refused_in_slacks_own_words() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[
                {"operation": "insert_at_end", "markdown": "a"},
                {"operation": "insert_at_end", "markdown": "b"},
            ],
        )
    )

    assert result["error"] == "too_many_changes"
    assert (
        "Only one operation per API call is currently supported"
        in result["detail"]
    )
    assert workspace.calls == []


def test_an_empty_change_list_leaves_the_canvas_alone() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(canvas_id=_CANVAS, changes=[])
    )

    assert result["error"] == "changes_required"
    assert workspace.calls == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"operation": "sprinkle", "markdown": "x"}, "operation_unknown"),
        ({"operation": "insert_after", "markdown": "x"}, "section_id_required"),
        ({"operation": "delete"}, "section_id_required"),
        (
            {"operation": "insert_at_end", "section_id": "s", "markdown": "x"},
            "section_id_not_accepted",
        ),
        ({"operation": "insert_at_end"}, "change_content_required"),
        ({"operation": "rename"}, "change_content_required"),
        ("not an object", "change_malformed"),
    ],
)
def test_a_change_slack_would_decline_is_declined_before_the_write(
    change: Any, code: str
) -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(canvas_id=_CANVAS, changes=[change])
    )

    assert result["error"] == code
    assert workspace.calls == []


def test_replace_without_a_section_id_is_allowed_and_documented() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "replace", "markdown": "everything"}],
        )
    )

    assert result["ok"] is True
    assert "section_id" not in workspace.payload("canvases.edit")["changes"][0]
    description = _all_cards()["edit_slack_canvas"].description
    assert "replaces the entire canvas" in description


def test_slacks_own_parse_error_is_passed_through_rather_than_flattened() -> None:
    detail = "'content' error: line 28: Unsupported block type (List) within block quote"
    workspace = _Workspace(fail=("invalid_arguments", detail))

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "> - a"}],
        )
    )

    assert result["error"] == "invalid_arguments"
    assert detail in result["detail"]


def test_a_locked_canvas_is_told_that_retrying_is_the_remedy() -> None:
    workspace = _Workspace(fail="canvas_editing_locked")

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "x"}],
        )
    )

    assert result["error"] == "canvas_editing_locked"
    assert "retry" in result["detail"]
    assert "nothing changed" in result["detail"]


def test_a_canvas_too_large_refusal_says_what_has_to_shrink() -> None:
    workspace = _Workspace(fail="canvas_too_large")

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "x"}],
        )
    )

    assert result["error"] == "canvas_too_large"
    assert "smaller" in result["detail"]


# ── 5. sharing ───────────────────────────────────────────────────────────────


def test_a_grant_names_its_targets_as_an_array() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE, _BOB]
        )
    )

    assert result["ok"] is True
    assert result["granted"] == [_ALICE, _BOB]
    assert workspace.payload("canvases.access.set") == {
        "canvas_id": _CANVAS,
        "user_ids": [_ALICE, _BOB],
        "access_level": "read",
    }


def test_remove_goes_to_the_other_method_and_carries_no_level() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, chat_ids=[_CHAT], remove=True
        )
    )

    assert result["access_level"] == "none"
    assert workspace.payload("canvases.access.delete") == {
        "canvas_id": _CANVAS,
        "channel_ids": [_CHAT],
    }
    assert workspace.payloads("canvases.access.set") == []


def test_a_conversation_target_reaches_slack_under_slacks_own_name() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", chat_ids=[_CHAT]
        )
    )

    assert result["ok"] is True
    payload = workspace.payload("canvases.access.set")
    assert payload["channel_ids"] == [_CHAT]
    assert "chat_ids" not in payload
    assert result["granted"] == [_CHAT]


def test_neither_target_is_refused_and_so_is_both() -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace)

    neither = _run(
        toolkit.share_slack_canvas(canvas_id=_CANVAS, access_level="read")
    )
    both = _run(
        toolkit.share_slack_canvas(
            canvas_id=_CANVAS,
            access_level="read",
            chat_ids=[_CHAT],
            user_ids=[_ALICE],
        )
    )

    assert neither["error"] == "access_targets_required"
    assert both["error"] == "access_targets_ambiguous"
    assert workspace.calls == []


def test_owner_cannot_be_given_to_a_conversation() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="owner", chat_ids=[_CHAT]
        )
    )

    assert result["error"] == "owner_must_be_a_person"
    assert workspace.calls == []


def test_owner_named_for_several_people_is_refused_on_its_arity() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="owner", user_ids=[_ALICE, _BOB]
        )
    )

    assert result["error"] == "owner_is_one_person"
    assert "2 were named" in result["detail"]
    assert workspace.calls == []


def test_a_direct_message_is_not_a_share_target() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", chat_ids=[_CHAT, _DM]
        )
    )

    assert result["error"] == "chat_id_is_a_direct_message"
    assert workspace.calls == []


def test_an_unknown_access_level_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="admin", user_ids=[_ALICE]
        )
    )

    assert result["error"] == "access_level_unknown"
    assert ", ".join(ACCESS_LEVELS) in result["detail"]


def test_remove_and_a_level_together_are_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS,
            access_level="read",
            user_ids=[_ALICE],
            remove=True,
        )
    )

    assert result["error"] == "access_level_not_accepted"
    assert workspace.calls == []


def test_a_multi_target_grant_that_half_succeeds_reports_each_target() -> None:
    def fail(method: str, payload: dict[str, Any]) -> Any:
        if method != "canvases.access.set":
            return None
        people = payload.get("user_ids") or []
        if len(people) > 1:
            return "failed_to_update_user_ids"
        return "user_not_found" if people == [_BOB] else None

    workspace = _Workspace(fail=fail)

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS,
            access_level="read",
            user_ids=[_ALICE, _BOB, _CAROL],
        )
    )

    assert result["ok"] is False
    assert result["error"] == "access_partially_applied"
    assert result["granted"] == [_ALICE, _CAROL]
    assert result["failed"] == [{"id": _BOB, "error": "user_not_found"}]
    assert "undetermined" not in result
    # One call for the whole set, then one per target to find out which.
    assert len(workspace.payloads("canvases.access.set")) == 4


def test_a_single_target_failure_is_not_retried_target_by_target() -> None:
    workspace = _Workspace(fail="user_not_found")

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        )
    )

    assert result["error"] == "user_not_found"
    assert len(workspace.payloads("canvases.access.set")) == 1


def test_a_refused_user_grant_names_the_step_no_canvas_method_performs() -> None:
    workspace = _Workspace(fail="failed_to_update_user_ids")

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        )
    )

    assert "sent to them directly" in result["next_step"]
    assert "does not count" in result["next_step"]


def test_a_channel_canvas_has_no_access_to_set_and_the_refusal_says_so() -> None:
    workspace = _Workspace(fail="canvas_not_found")

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", chat_ids=[_CHAT]
        )
    )

    assert "follows from who is in that conversation" in result["next_step"]


def test_the_share_tool_posts_nothing() -> None:
    workspace = _Workspace(fail="failed_to_update_user_ids")

    _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        )
    )

    assert [name for name, _ in workspace.calls if "chat" in name] == []


# ── 6. deleting ──────────────────────────────────────────────────────────────


def test_a_delete_reports_the_canvas_it_deleted() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).delete_slack_canvas(canvas_id=_CANVAS))

    assert result == {"ok": True, "canvas_id": _CANVAS, "deleted": True}
    assert workspace.payload("canvases.delete") == {"canvas_id": _CANVAS}


def test_deleting_switched_off_for_a_workspace_is_explained() -> None:
    workspace = _Workspace(fail="canvas_deleting_disabled")

    result = _run(_toolkit(workspace).delete_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "canvas_deleting_disabled"
    assert "untouched" in result["detail"]
    assert "deleted" not in result


# ── 7. arguments that never reach Slack ──────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "C1234ABCD", "f1234abcd", "../etc"])
def test_a_canvas_id_that_is_not_one_is_refused_before_any_call(bad: str) -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).delete_slack_canvas(canvas_id=bad))

    assert result["error"] in {"canvas_id_required", "canvas_id_malformed"}
    assert workspace.calls == []


def test_a_bare_string_is_not_silently_read_as_a_one_element_array() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=_ALICE
        )
    )

    assert result["error"] == "access_targets_malformed"
    assert workspace.calls == []


def test_changes_that_is_not_a_list_is_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS, changes={"operation": "insert_at_end"}
        )
    )

    assert result["error"] == "changes_malformed"
    assert workspace.calls == []


# ── 8. which requests may touch a canvas at all ──────────────────────────────


def test_an_inbound_slack_turn_may_touch_a_canvas() -> None:
    metadata = {METADATA_TEAM_KEY: "T000"}

    assert slack_canvas_request_metadata("slack", metadata) == metadata


def test_a_marked_scheduled_run_may_touch_a_canvas() -> None:
    metadata = {METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB, METADATA_TEAM_KEY: "T000"}

    assert slack_canvas_request_metadata("__cron__", metadata) == metadata


def test_the_provider_fails_closed_on_every_wrong_request_shape() -> None:
    for channel_id, metadata in (
        ("web", {METADATA_TEAM_KEY: "T000"}),
        (None, {METADATA_TEAM_KEY: "T000"}),
        ("", {METADATA_TEAM_KEY: "T000"}),
        ("slack", None),
        ("slack", "not a mapping"),
        ("__cron__", {METADATA_TEAM_KEY: "T000"}),
        ("__cron__", None),
    ):
        assert slack_canvas_request_metadata(channel_id, metadata) == {}, (
            channel_id,
            metadata,
        )


def test_a_failing_provider_touches_no_canvas() -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("the worker boundary is gone")

    workspace = _Workspace()
    toolkit = SlackCanvasToolkit(metadata_provider=_boom, client=workspace)

    result = _run(toolkit.delete_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "trusted_slack_request_required"
    assert workspace.calls == []


# ── 9. the cards ─────────────────────────────────────────────────────────────


def test_the_read_card_exposes_native_content_formats() -> None:
    card = _all_cards()["read_slack_canvas"]

    assert card.input_params["properties"]["content_format"]["enum"] == ["markdown", "html"]
    assert "canvas_read_max_chars" in card.description


def test_the_two_creation_cards_each_name_the_other() -> None:
    cards = _all_cards()

    assert "write_slack_channel_canvas" in cards["write_slack_canvas"].description
    assert "write_slack_canvas" in cards["write_slack_channel_canvas"].description


def test_the_channel_canvas_card_says_a_channel_holds_one_at_a_time() -> None:
    description = _all_cards()["write_slack_channel_canvas"].description

    assert "one of these at a time" in description
    assert "permanently" not in description
    assert "cannot be deleted" not in description


def test_no_card_says_a_channel_canvas_cannot_be_deleted() -> None:
    for card in _all_cards().values():
        assert "cannot be deleted" not in card.description


def test_the_delete_card_says_a_channel_canvas_goes_too_and_what_is_untried() -> None:
    description = _all_cards()["delete_slack_canvas"].description

    assert "deletes a conversation's own canvas as well" in description
    assert "nobody has tried whether it accepts a new one" in description


def test_the_delete_card_says_it_cannot_be_undone_before_anything_else() -> None:
    description = _all_cards()["delete_slack_canvas"].description

    assert "It cannot be undone" in description
    assert "there is no way to get it back" in description
    assert description.index("It cannot be undone") < description.index(
        "conversation's own canvas"
    )
    assert "cannot be undone" in description


def test_the_share_card_names_the_step_it_does_not_perform() -> None:
    description = _all_cards()["share_slack_canvas"].description

    assert "This does not hand anybody the canvas" in description
    assert "sent to them directly" in description
    assert "This tool will not send it" in description


def test_the_share_card_says_owner_is_one_person() -> None:
    description = _all_cards()["share_slack_canvas"].description

    assert "exactly one person" in description


def test_the_six_cards_name_no_tool_outside_this_module() -> None:
    for card in _all_cards().values():
        for name in (
            "post_message",
            "pin_message",
            "read_pinned_messages",
            "read_slack_conversation",
            "download_slack_file",
            "search_slack_workspace",
            "find_by_name",
            "publish_slack_home_tab",
        ):
            assert name not in card.description, card.name
