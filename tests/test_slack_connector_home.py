"""The Home tab welcome of the Slack connector, on a Slack that exists only in this process.

``app_home_opened`` arrives through the listener the connector registered.
What was published is read from the ``views.publish`` calls the connector made.
"""


import pytest
import pytest_asyncio
from conformance_kit import worlds
from scripted_backend import ScriptedBackend
from slack_kit import SlackKit
from slack_transport import BOT_USER, dm_of

from belle_agent.platforms.slack import commands as slack_commands

ASKER = "U0ASKER001"
OTHER = "U0OTHER002"
ROOM = "C0ROOM0001"
WELCOMING = "platforms: {slack: {home_tab_welcome: true, command_name: desk, bot_name: Belle}}\n"


@pytest.fixture
def slack(monkeypatch) -> SlackKit:
    return SlackKit(monkeypatch)


@pytest_asyncio.fixture
async def build_world(slack, tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, slack) as build:
        yield build


def _opened(user: str = ASKER, *, tab: str = "home", view: dict | None = None, **fields) -> dict:
    """One ``app_home_opened`` as Slack sends it: the channel is the person's direct message with the app."""
    event = {"type": "app_home_opened", "user": user, "channel": dm_of(user), "event_ts": "1710000400.000500", "tab": tab, **fields}
    if view is not None:
        event["view"] = view
    return event


def _published(world) -> list[dict]:
    return world.platform.web.payloads("views_publish")


def _text(view: dict) -> str:
    return "\n".join(str((block.get("text") or {}).get("text") or "") for block in view["blocks"])


async def test_a_person_who_opens_an_empty_home_tab_is_welcomed_on_it(build_world):
    world = await build_world(WELCOMING)

    never_published = await world.platform.app.deliver(_opened())
    cleared = await world.platform.app.deliver(_opened(OTHER, view={"id": "V1", "type": "home", "blocks": []}))

    assert never_published == cleared == "welcomed:empty-home-tab"
    first, second = _published(world)
    # The page is published for the person who opened the tab, and for nobody else.
    assert (first["user_id"], second["user_id"]) == (ASKER, OTHER) and first["view"]["type"] == "home"
    assert "Welcome" in _text(first["view"]) and "Ask Belle to put something here" in _text(first["view"])
    # Nothing else was done for the visit: no message, no turn.
    assert world.platform.web.sent == [] and world.requests == []


async def test_the_welcome_names_the_configured_command_and_nothing_a_backend_might_not_provide(build_world):
    world = await build_world(WELCOMING)

    await world.platform.app.deliver(_opened())
    text = _text(_published(world)[0]["view"])

    # It points at help, which every backend answers and which says what the backend at hand does not provide.
    assert "`/desk help`" in text
    for sub in slack_commands.SUBCOMMANDS:
        if sub is not slack_commands.HELP:
            assert f"/desk {sub.name}" not in text


async def test_a_page_somebody_is_reading_and_the_messages_tab_are_left_alone(build_world):
    world = await build_world(WELCOMING)
    page = {"id": "V1", "blocks": [{"type": "divider"}]}

    assert await world.platform.app.deliver(_opened(view=page)) == "ignored:home-tab-already-has-content"
    assert await world.platform.app.deliver(_opened(tab="messages")) == "ignored:not-the-home-tab"
    assert _published(world) == []

    # Control: the same person's empty tab is welcomed.
    assert await world.platform.app.deliver(_opened()) == "welcomed:empty-home-tab"
    assert len(_published(world)) == 1


async def test_the_setting_is_off_unless_it_is_written(build_world):
    off = await build_world()
    assert await off.platform.app.deliver(_opened()) == "ignored:home-tab-welcome-is-off"
    assert _published(off) == []


async def test_only_who_may_ask_is_welcomed_and_the_list_of_rooms_does_not_hold_the_home_tab(build_world):
    world = await build_world(
        f"platforms: {{slack: {{home_tab_welcome: true, allow_from: [{ASKER}], allowed_channel_ids: [{ROOM}]}}}}\n"
    )

    assert await world.platform.app.deliver(_opened(OTHER)) == "refused:user-not-in-allow_from"
    assert _published(world) == []
    # App Home is the person's direct message with the app, which the list of served rooms never held.
    assert await world.platform.app.deliver(_opened()) == "welcomed:empty-home-tab"
    assert [payload["user_id"] for payload in _published(world)] == [ASKER]


async def test_a_visit_by_the_app_itself_or_with_nobody_named_publishes_nothing(build_world):
    world = await build_world(WELCOMING)

    assert await world.platform.app.deliver(_opened(BOT_USER)) == "ignored:done-by-this-bot"
    assert await world.platform.app.deliver(_opened("")) == "dropped:no-actor-on-event"
    assert await world.platform.app.deliver(_opened(bot_id="B0ANOTHERAPP")) == "ignored:done-by-an-app"
    assert _published(world) == []


async def test_a_welcome_slack_refuses_is_reported_and_changes_nothing_else(build_world):
    world = await build_world(WELCOMING)
    world.platform.web.fail = {"views_publish": "not_enabled"}

    assert await world.platform.app.deliver(_opened()) == "failed:home-tab-welcome-not-published"
    assert world.platform.web.sent == [] and world.requests == []
