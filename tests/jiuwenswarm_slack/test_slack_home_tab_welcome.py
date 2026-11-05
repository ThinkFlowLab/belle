"""The welcome the connector publishes onto an empty App Home tab.

Two things are pinned here that nothing else pins.

* **What counts as empty.** Slack answers it on the payload and the answer has
  three states, not two: no ``view`` field at all is a tab nobody ever published
  to, a ``view`` holding no blocks is a tab that was published to and then
  cleared, and a ``view`` holding blocks is a page somebody is reading. The
  first two are welcomed and the third is left alone. A regression that read
  only the presence of the field would republish over whatever a person had
  asked for.
* **It is not a disposition.** ``delivery.events`` ships ``off`` for the
  app_home family and stays off, so a welcome that waited on that gate would
  never run anywhere. The tests below configure no events policy at all and
  still expect a publish, which is what says the two settings are independent.
"""

from typing import Any

import pytest
import jiuwenswarm

import jiuwenswarm.common.config as config_module
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)


pytestmark = pytest.mark.usefixtures("isolated_slack_dedup_store")

_PERSON = "U-PERSON"
_DM = "D-PERSON"
_BODY = {"event_id": "Ev1", "team_id": "T1"}


class _Workspace:
    """A fake Slack that records every published view."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def views_publish(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "view": {"id": "V1"}}

    @property
    def text(self) -> str:
        """Every published block's text, joined, for one assertion to read."""
        parts: list[str] = []
        for call in self.calls:
            for block in call["view"]["blocks"]:
                field = block.get("text") or {}
                parts.append(str(field.get("text") or ""))
        return "\n".join(parts)


@pytest.fixture(autouse=True)
def _slack_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The install the toolkit resolves the request against.

    The publish goes through the Home tab tool, which asks the workspace
    resolver for the settings of the install the request arrived in before it
    calls Slack. One configured block is the single-workspace shape and is what
    that resolver answers with here.
    """
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-test"}}},
    )


def _channel(workspace: _Workspace | None = None, **settings: Any) -> SlackChannel:
    """A running channel with the Home tab welcome turned on by default."""
    settings.setdefault("home_tab_welcome", True)
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **settings), RobotMessageRouter()
    )
    channel._running = True
    channel._identity.bot_user_id = "U-BOT"
    channel._identity.team_id = "T1"
    channel._client = workspace
    return channel


def _opened(
    *, tab: str = "home", view: dict[str, Any] | None = None, published: bool = False
) -> dict[str, Any]:
    """One ``app_home_opened`` as Slack sends it.

    ``channel`` is a ``D`` and can be nothing else: an App Home belongs to one
    person and what Slack names here is the direct message between that person
    and this app. ``view`` is present only once something has been published for
    that person, which is the distinction the whole feature turns on, so it is
    absent unless a test asks for it.
    """
    event: dict[str, Any] = {
        "type": "app_home_opened",
        "user": _PERSON,
        "channel": _DM,
        "event_ts": "1710000400.000500",
        "tab": tab,
    }
    if view is not None or published:
        event["view"] = view if view is not None else {"id": "V1", "blocks": []}
    return event


# ----------------------------------------------------------------------
# What counts as an empty tab
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tab_nobody_ever_published_to_is_welcomed() -> None:
    # No view field at all. Slack includes one only after a first publish, so
    # its absence is the whole evidence that this tab has never held anything.
    workspace = _Workspace()
    channel = _channel(workspace)

    outcome = await channel._route_inbound_event(_opened(), _BODY)

    assert outcome == "welcomed:empty-home-tab"
    assert len(workspace.calls) == 1
    assert workspace.calls[0]["user_id"] == _PERSON
    assert workspace.calls[0]["view"]["type"] == "home"


@pytest.mark.asyncio
async def test_a_cleared_tab_is_welcomed() -> None:
    # The second empty state, and it is a real one rather than a shape nothing
    # produces: Slack has no method that deletes a view, so clear_slack_home_tab
    # empties a tab by publishing an empty block list into it. What comes back
    # on the next opening is a view field holding no blocks.
    workspace = _Workspace()
    channel = _channel(workspace)

    outcome = await channel._route_inbound_event(
        _opened(view={"id": "V1", "type": "home", "blocks": []}), _BODY
    )

    assert outcome == "welcomed:empty-home-tab"
    assert len(workspace.calls) == 1


@pytest.mark.asyncio
async def test_a_tab_with_content_is_left_alone() -> None:
    # Publishing replaces the whole page and Slack keeps no history of one, so
    # a welcome over somebody's content would destroy it with nothing to
    # restore it from.
    workspace = _Workspace()
    channel = _channel(workspace)

    outcome = await channel._route_inbound_event(
        _opened(view={"id": "V1", "blocks": [{"type": "divider"}]}), _BODY
    )

    assert outcome == "ignored:home-tab-already-has-content"
    assert workspace.calls == []


@pytest.mark.asyncio
async def test_the_welcome_is_published_once_and_not_again() -> None:
    # No marker records who has been welcomed, and none is needed. The first
    # publish is what makes the tab non-empty, and the second opening reads that
    # page back on its own payload.
    workspace = _Workspace()
    channel = _channel(workspace)

    await channel._route_inbound_event(_opened(), _BODY)
    published = {"id": "V1", "blocks": workspace.calls[0]["view"]["blocks"]}
    outcome = await channel._route_inbound_event(_opened(view=published), _BODY)

    assert outcome == "ignored:home-tab-already-has-content"
    assert len(workspace.calls) == 1


# ----------------------------------------------------------------------
# Which tab was opened
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_messages_tab_is_never_welcomed() -> None:
    # The same event fires for the Messages tab, which is the direct message
    # conversation and has no view of any kind. Read as an empty page it would
    # publish a welcome every time somebody opened a conversation with the bot.
    workspace = _Workspace()
    channel = _channel(workspace)

    outcome = await channel._route_inbound_event(_opened(tab="messages"), _BODY)

    assert outcome == "ignored:events-off-for-app_home-here"
    assert workspace.calls == []


# ----------------------------------------------------------------------
# The setting
# ----------------------------------------------------------------------


def test_the_welcome_ships_off() -> None:
    # Publishing to a Home tab notifies nobody and the page stays until
    # something replaces it, so an upgrade into this key must change nothing.
    assert SlackChannelConfig().home_tab_welcome is False


@pytest.mark.asyncio
async def test_nothing_is_published_while_the_setting_is_off() -> None:
    workspace = _Workspace()
    channel = _channel(workspace, home_tab_welcome=False)

    outcome = await channel._route_inbound_event(_opened(), _BODY)

    assert outcome == "ignored:events-off-for-app_home-here"
    assert workspace.calls == []


@pytest.mark.asyncio
async def test_the_welcome_does_not_wait_on_the_events_disposition() -> None:
    # The two settings answer different questions. delivery.events decides what
    # the model is shown and ships off for this family everywhere; the welcome
    # decides whether the connector publishes a view. Nothing here writes an
    # events policy, so app_home is off, and the page is published anyway.
    workspace = _Workspace()
    channel = _channel(workspace)
    assert channel._channel_event_disposition(_DM, "app_home") == "off"

    await channel._route_inbound_event(_opened(), _BODY)

    assert len(workspace.calls) == 1


@pytest.mark.asyncio
async def test_a_licensed_room_still_buffers_the_event() -> None:
    # The welcome is held rather than returned, so a deployment that also asked
    # for app_home events keeps them. Publishing a page and recording that
    # somebody opened the tab are two things, and doing the first must not
    # silently drop the second.
    workspace = _Workspace()
    channel = _channel(
        workspace,
        conversation_overrides={
            _DM: slack_connect.SlackChannelOverride(events={"app_home": "context"})
        },
    )

    outcome = await channel._route_inbound_event(_opened(), _BODY)

    assert outcome == "buffered:app_home"
    assert len(workspace.calls) == 1
    folded = channel._inbound_events.fold(_DM)
    assert [record["family"] for record in folded["events"]] == ["app_home"]


# ----------------------------------------------------------------------
# What it says
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_text_names_the_configured_bot_and_command() -> None:
    # Both are read from config on every welcome. A workspace that renamed its
    # bot or its slash command reads its own names back, and the help pointer
    # stays a command that workspace actually has.
    workspace = _Workspace()
    channel = _channel(workspace, bot_name="Atlas", command_name="atlas")

    await channel._route_inbound_event(_opened(), _BODY)

    text = workspace.text
    assert "Atlas" in text
    assert "`/atlas help`" in text
    assert "Welcome" in text


@pytest.mark.asyncio
async def test_an_unnamed_bot_falls_back_rather_than_naming_a_product() -> None:
    # bot_name ships empty. The fallback is the one this connector already uses
    # where it has to refer to itself in prose.
    workspace = _Workspace()
    channel = _channel(workspace)

    await channel._route_inbound_event(_opened(), _BODY)

    text = workspace.text
    assert "the assistant" in text
    assert "`/belle help`" in text


@pytest.mark.asyncio
async def test_the_welcome_is_three_lines() -> None:
    # Somebody opening the app for the first time reads what the page is, what
    # they may do with it, and where the rest is written down. A page that grew
    # past that stops being read.
    workspace = _Workspace()
    channel = _channel(workspace)

    await channel._route_inbound_event(_opened(), _BODY)

    assert len(workspace.text.splitlines()) == 3


# ----------------------------------------------------------------------
# The template
# ----------------------------------------------------------------------


def test_the_key_is_in_the_packaged_template() -> None:
    # A key absent from the template has no default to hand down and is taken
    # back out of an operator's own file by the next upgrade's merge.
    import pathlib

    import yaml

    template = pathlib.Path(__file__).resolve().parents[2] / "examples/config/belle-config.yaml"
    shipped = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert shipped["platforms"]["slack"]["home_tab_welcome"] is False


@pytest.mark.asyncio
async def test_a_refused_publish_says_so_rather_than_claiming_a_page() -> None:
    # The Home tab surface is turned on in the Slack app's own configuration
    # rather than by a scope, so it can be off in a workspace where everything
    # else works. Nobody is waiting on this publish, so the only thing the
    # refusal has to do is not read as a success.
    from slack_fakes import FakeSlackError

    class _Refusing(_Workspace):
        async def views_publish(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            raise FakeSlackError("not_enabled")

    workspace = _Refusing()
    channel = _channel(workspace)

    outcome = await channel._route_inbound_event(_opened(), _BODY)

    assert outcome == "failed:home-tab-welcome-not-published"


@pytest.mark.asyncio
async def test_the_published_page_is_block_kit_the_tool_rendered() -> None:
    # The welcome goes through the Home tab tool's own publishing path rather
    # than a second publisher beside it, which is what keeps one rendering, one
    # block cap and one refusal shape behind every view this app publishes.
    workspace = _Workspace()
    channel = _channel(workspace)

    await channel._route_inbound_event(_opened(), _BODY)

    view = workspace.calls[0]["view"]
    assert view["type"] == "home"
    assert view["blocks"]
    assert all(
        isinstance(block, dict) and block.get("type") for block in view["blocks"]
    )
