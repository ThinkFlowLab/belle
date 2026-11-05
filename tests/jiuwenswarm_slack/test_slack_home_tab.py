"""Publishing and clearing a Slack App Home tab.

Three things are pinned here that nothing else can pin.

* **The tab is always the asker's.** ``views.publish`` takes any ``user_id``, so
  the only thing keeping a page out of a stranger's private space is that this
  module never builds an argument for one. A test that asserts the published
  ``user_id`` is the stamped requester is what stops somebody adding the
  argument later without noticing what it costs.
* **Both arguments empty is a refusal rather than a clear.** Slack would read an
  empty view as *empty the tab*, so the divergence is deliberate and has to stay
  deliberate: a regression to Slack's semantics would turn a failed computation
  into somebody's wiped page, with no history to restore it from.
* **The declared ``blocks`` argument is not a second route.** It goes through the
  allow-list and the interactive gate a fenced ``blockkit`` payload goes
  through, and the test that fences and declares the same payload under the same
  configuration is what says so.
"""

import asyncio
import json
from typing import Any

import jiuwenswarm.common.config as config_module
import pytest
from slack_fakes import FakeSlackError as _FakeSlackError

from belle_agent.integrations.jiuwenswarm.slack_home_tab import (
    SlackHomeTabToolkit,
    slack_home_tab_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_tool_context import METADATA_ORIGIN_KEY, ORIGIN_CRON_JOB
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack.history_policy import (
    METADATA_ASKER_KEY,
)

_ASKER = "U-ASKER"
_SOMEBODY_ELSE = "U-STRANGER"
_CHAT = "C-ROOM"




class _Workspace:
    """A fake Slack that records every publish and can refuse one."""

    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def views_publish(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise _FakeSlackError(self.fail)
        # The live method answers with the whole view it stored. Nothing here
        # reads it, which is the point of asserting on what was sent instead.
        return {"ok": True, "view": {"id": "V1", "hash": "1.2"}}

    @property
    def views(self) -> list[dict[str, Any]]:
        return [call["view"] for call in self.calls]


@pytest.fixture
def _config(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The token and the Block Kit rules this toolkit reads.

    A factory rather than an autouse fixture, because half of this file is about
    what the two Block Kit keys do and each of those tests needs its own values.
    """

    def _set(**slack: Any) -> None:
        settings = {"bot_token": "xoxb-config-secret"}
        settings.update(slack)
        monkeypatch.setattr(
            config_module,
            "get_config",
            lambda: {"channels": {"slack": settings}},
        )

    _set()
    return _set


def _toolkit(workspace: _Workspace, **metadata: Any) -> SlackHomeTabToolkit:
    base: dict[str, Any] = {METADATA_ASKER_KEY: _ASKER, "slack_channel_id": _CHAT}
    base.update(metadata)
    return SlackHomeTabToolkit(metadata=base, client=workspace)


def _publish(toolkit: SlackHomeTabToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.publish_slack_home_tab(**kwargs)))


def _clear(toolkit: SlackHomeTabToolkit) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.clear_slack_home_tab()))


def _cards(toolkit: SlackHomeTabToolkit) -> dict[str, Any]:
    cards: dict[str, Any] = {}
    for tool in toolkit.get_tools():
        card = getattr(tool, "card", None) or tool._card
        cards[card.name] = card
    return cards


# ── 1. publishing ────────────────────────────────────────────────────────────


def test_text_is_published_as_the_blocks_of_a_home_view(_config: Any) -> None:
    workspace = _Workspace()

    result = _publish(_toolkit(workspace), text="Today's numbers are in.")

    assert result == {
        "ok": True,
        "user_id": _ASKER,
        "blocks": 1,
        "replaced_a_view": True,
        "view_hash": "1.2",
    }
    assert workspace.views == [
        {
            "type": "home",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Today's numbers are in."},
                }
            ],
        }
    ]


def test_text_goes_through_the_renderer_a_reply_goes_through(_config: Any) -> None:
    workspace = _Workspace()
    text = "Totals:\n\n| kind | n |\n| --- | --- |\n| open | 3 |\n"

    result = _publish(_toolkit(workspace), text=text)

    assert result["ok"] is True
    types = [block["type"] for block in workspace.views[0]["blocks"]]
    assert "data_table" in types, types


def test_declared_blocks_are_published_unchanged(_config: Any) -> None:
    workspace = _Workspace()
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Today"}}]

    result = _publish(_toolkit(workspace), blocks=blocks)

    assert result == {
        "ok": True,
        "user_id": _ASKER,
        "blocks": 1,
        "replaced_a_view": True,
        "view_hash": "1.2",
    }
    assert workspace.views == [{"type": "home", "blocks": blocks}]


def test_declared_blocks_win_over_text(_config: Any) -> None:
    workspace = _Workspace()
    blocks = [{"type": "divider"}]

    _publish(_toolkit(workspace), text="a summary", blocks=blocks)

    assert workspace.views == [{"type": "home", "blocks": blocks}]


def test_home_hash_guards_publish_and_clear(_config: Any) -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace)

    assert _publish(toolkit, text="new view", hash="1.1")["ok"]
    assert json.loads(asyncio.run(toolkit.clear_slack_home_tab(hash="1.2")))["ok"]
    assert [call["hash"] for call in workspace.calls] == ["1.1", "1.2"]
    assert all(call["user_id"] == _ASKER for call in workspace.calls)


def test_home_hash_conflict_is_distinct(_config: Any) -> None:
    workspace = _Workspace(fail="hash_conflict")

    result = _publish(_toolkit(workspace), text="stale view", hash="old")

    assert result["error"] == "hash_conflict"
    assert "refresh" in result["detail"]
    assert workspace.calls[0]["hash"] == "old"


# ── 2. the tab is the asker's, and there is no argument for anybody else's ────


def test_the_view_is_published_to_the_requester_the_gateway_stamped(
    _config: Any,
) -> None:
    workspace = _Workspace()

    _publish(_toolkit(workspace, **{METADATA_ASKER_KEY: _SOMEBODY_ELSE}), text="hi")

    assert workspace.calls[0]["user_id"] == _SOMEBODY_ELSE


def test_neither_tool_accepts_an_argument_naming_a_tab(_config: Any) -> None:
    cards = _cards(_toolkit(_Workspace()))
    publish = cards["publish_slack_home_tab"].input_params
    clear = cards["clear_slack_home_tab"].input_params

    # The page and the hash of the view it replaces are the only arguments: whose tab it is is never one.
    assert set(publish["properties"]) == {"text", "blocks", "hash"}
    assert set(clear["properties"]) == {"hash"}
    assert publish["required"] == clear["required"] == []


def test_a_request_that_names_nobody_is_refused_by_both_tools(_config: Any) -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace, **{METADATA_ASKER_KEY: ""})

    published = _publish(toolkit, text="hello")
    cleared = _clear(toolkit)

    for result in (published, cleared):
        assert result["ok"] is False
        assert result["error"] == "trusted_slack_requester_required"
    assert workspace.calls == []


# ── 3. both empty is a refusal, not a clear ──────────────────────────────────


def test_publishing_with_neither_argument_refuses_and_names_the_alternative(
    _config: Any,
) -> None:
    workspace = _Workspace()

    result = _publish(_toolkit(workspace))

    assert result["ok"] is False
    assert result["error"] == "nothing_to_publish"
    assert "clear_slack_home_tab" in result["detail"]
    assert workspace.calls == [], "an omission must not reach Slack at all"


def test_text_that_renders_to_nothing_is_the_same_refusal(_config: Any) -> None:
    workspace = _Workspace()

    result = _publish(_toolkit(workspace), text="   \n\n  ")

    assert result["ok"] is False
    assert result["error"] == "nothing_to_publish"
    assert "clear_slack_home_tab" in result["detail"]
    assert workspace.calls == []


# ── 4. clearing, which is the only way to empty a tab ────────────────────────


def test_clear_publishes_an_empty_block_list(_config: Any) -> None:
    """No method deletes a view, so emptying one is publishing nothing into it."""
    workspace = _Workspace()

    result = _clear(_toolkit(workspace))

    assert result == {
        "ok": True,
        "user_id": _ASKER,
        "blocks": 0,
        "replaced_a_view": True,
        "view_hash": "1.2",
    }
    assert workspace.views == [{"type": "home", "blocks": []}]


def test_clearing_twice_is_safe(_config: Any) -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace)

    assert _clear(toolkit)["ok"] is True
    assert _clear(toolkit)["ok"] is True
    assert len(workspace.calls) == 2


# ── 5. the Block Kit gates, which are one set of rules for two spellings ─────


def test_a_declared_interactive_block_is_refused(_config: Any) -> None:
    workspace = _Workspace()
    blocks = [
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Go"}}
            ],
        }
    ]

    result = _publish(_toolkit(workspace), blocks=blocks)

    assert result["ok"] is False
    assert result["error"] == "blocks_refused"
    assert "interactive" in result["detail"]
    assert workspace.calls == []


def test_the_declared_argument_and_a_fence_are_held_to_the_same_rules(
    _config: Any,
) -> None:
    """Declared blocks and fenced blocks follow the same rule."""
    payload = [
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Go"}}
            ],
        }
    ]
    fenced = "```blockkit\n" + json.dumps({"blocks": payload}) + "\n```"

    workspace = _Workspace()
    declared = _publish(_toolkit(workspace), blocks=payload)
    through_a_fence = slack_blocks.render_blocks(fenced)

    assert declared["error"] == "blocks_refused"
    assert through_a_fence is None


def test_refused_blocks_fall_back_to_the_text_when_there_is_some(
    _config: Any,
) -> None:
    workspace = _Workspace()

    result = _publish(
        _toolkit(workspace), text="Today's numbers are in.", blocks=[{"type": "actions"}]
    )

    assert result["ok"] is True
    assert workspace.views[0]["blocks"] == [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Today's numbers are in."},
        }
    ]


def test_a_view_over_slacks_block_ceiling_is_refused_before_the_call(
    _config: Any,
) -> None:
    workspace = _Workspace()
    blocks = [{"type": "divider"}] * (slack_blocks.MAX_BLOCKS_PER_VIEW + 1)

    result = _publish(_toolkit(workspace), blocks=blocks)

    assert result["ok"] is False
    assert result["error"] == "home_tab_too_many_blocks"
    assert str(slack_blocks.MAX_BLOCKS_PER_VIEW) in result["detail"]
    assert workspace.calls == []


# ── 6. what Slack refuses, and what the caller is told about it ──────────────


def test_not_enabled_is_surfaced_and_names_the_manifest_flag(_config: Any) -> None:
    workspace = _Workspace(fail="not_enabled")

    result = _publish(_toolkit(workspace), text="hello")

    assert result["ok"] is False
    assert result["error"] == "not_enabled"
    assert "home_tab_enabled" in result["detail"]
    assert "reinstall" in result["detail"]


def test_view_too_large_is_surfaced_with_the_size_that_binds(_config: Any) -> None:
    workspace = _Workspace(fail="view_too_large")

    result = _publish(_toolkit(workspace), text="hello")

    assert result["ok"] is False
    assert result["error"] == "view_too_large"
    assert "250 KB" in result["detail"]


def test_an_unnamed_refusal_names_the_method_slacks_way(_config: Any) -> None:
    workspace = _Workspace(fail="fatal_error")

    result = _publish(_toolkit(workspace), text="hello")

    assert result["ok"] is False
    assert result["error"] == "fatal_error"
    assert "views.publish" in result["detail"]


def test_a_refusal_claims_nothing_about_what_the_tab_holds(_config: Any) -> None:
    result = _publish(_toolkit(_Workspace(fail="not_enabled")), text="hello")

    assert "blocks" not in result
    assert "replaced_a_view" not in result


# ── 7. which requests may touch a Home tab at all ────────────────────────────


def test_an_inbound_slack_turn_naming_its_asker_may_publish() -> None:
    metadata = {METADATA_ASKER_KEY: _ASKER, "slack_channel_id": _CHAT}

    assert slack_home_tab_request_metadata("slack", metadata) == metadata


def test_the_provider_fails_closed_on_every_wrong_request_shape() -> None:
    for channel_id, metadata in (
        ("web", {METADATA_ASKER_KEY: _ASKER}),
        (None, {METADATA_ASKER_KEY: _ASKER}),
        ("", {METADATA_ASKER_KEY: _ASKER}),
        ("slack", None),
        ("slack", "not a mapping"),
        ("slack", {}),
        ("slack", {METADATA_ASKER_KEY: ""}),
        ("slack", {METADATA_ASKER_KEY: "   "}),
        ("slack", {"slack_channel_id": _CHAT}),
    ):
        assert slack_home_tab_request_metadata(channel_id, metadata) == {}, (
            channel_id,
            metadata,
        )


def test_a_scheduled_run_may_not_publish_a_home_tab() -> None:
    assert (
        slack_home_tab_request_metadata(
            "__cron__",
            {METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB, "slack_channel_id": _CHAT},
        )
        == {}
    )


def test_a_failing_provider_publishes_nothing(_config: Any) -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("the worker boundary is gone")

    workspace = _Workspace()
    toolkit = SlackHomeTabToolkit(metadata_provider=_boom, client=workspace)

    result = _publish(toolkit, text="hello")

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_requester_required"
    assert workspace.calls == []


# ── 8. the cards ─────────────────────────────────────────────────────────────


def test_the_publish_card_sends_a_model_elsewhere_for_somebody_elses_tab(
    _config: Any,
) -> None:
    description = _cards(_toolkit(_Workspace()))["publish_slack_home_tab"].description

    assert "person who asked" in description
    assert "direct message" in description


def test_the_publish_card_says_the_page_is_replaced_and_nobody_is_told(
    _config: Any,
) -> None:
    description = _cards(_toolkit(_Workspace()))["publish_slack_home_tab"].description

    assert "replaces the whole page" in description
    assert "no history" in description
    assert "sends no notification" in description


def test_the_publish_card_names_the_tool_that_clears(_config: Any) -> None:
    description = _cards(_toolkit(_Workspace()))["publish_slack_home_tab"].description

    assert "clear_slack_home_tab" in description


def test_the_publish_card_says_what_an_image_can_show(_config: Any) -> None:
    description = _cards(_toolkit(_Workspace()))["publish_slack_home_tab"].description

    assert "uploads a file" in description


def test_the_clear_card_says_it_cannot_be_undone(_config: Any) -> None:
    description = _cards(_toolkit(_Workspace()))["clear_slack_home_tab"].description

    assert "cannot be undone" in description


def test_the_two_cards_name_no_conversation_tool(_config: Any) -> None:
    for card in _cards(_toolkit(_Workspace())).values():
        for name in (
            "post_message",
            "pin_message",
            "read_slack_conversation",
            "find_by_name",
        "read_slack_canvas",
        "write_slack_canvas",
        "write_slack_channel_canvas",
        "edit_slack_canvas",
        "share_slack_canvas",
        "delete_slack_canvas",
        "read_slack_list",
        "write_slack_list",
        "edit_slack_list",
        "share_slack_list",
        "delete_slack_list",
        ):
            assert name not in card.description
