"""A running turn's reply shown as it is written, and its activity card: posts that Belle edits in place."""

import asyncio

import pytest
from transport import TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent import progress
from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent
from belle_agent.platforms.base import ENDED_SILENT, MARK_DONE, MARK_FAILED, MARK_STOPPED, InboundMessage
from belle_agent.platforms.mattermost import surfaces
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import MattermostSettings

CHANNEL = "c" * 26
MESSAGE = InboundMessage(
    platform="mattermost", workspace="t" * 26, conversation=CHANNEL, message_id="m" * 26, sender="a" * 26,
    text="hi", reply_thread="m" * 26,
)
TOO_LATE = (400, {"id": "api.post.update_post.permissions_time_limit.app_error"})


async def _ignore(_record):
    return "ignored"


@pytest.fixture
async def connect(monkeypatch):
    monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.0)
    started = []

    async def start(**settings):
        rest = FakeRest()
        client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
        connector = MattermostPlatform(MattermostSettings(URL, TOKEN, **settings), client=client)
        await connector.start(_ignore)
        started.append(connector)
        return connector, rest

    yield start
    for connector in started:
        await connector.stop()


async def _say(connector, *deltas: str) -> None:
    for delta in deltas:
        await connector.progress(MESSAGE, TurnEvent(EVENT_TEXT_DELTA, text=delta))
        await asyncio.sleep(0.01)


def _messages(rest: FakeRest) -> list[str]:
    return [post["message"] for post in rest.posts.values()]


async def test_each_surface_is_shown_only_where_its_setting_turns_it_on(connect):
    connector, _ = await connect(enable_streaming=True, activity_card=False)
    assert set(connector.shows) == {EVENT_TEXT_DELTA, EVENT_STEP}
    connector, _ = await connect()
    assert set(connector.shows) == {EVENT_TOOL_CALLED, EVENT_TOOL_RESULT}


async def test_a_streamed_reply_is_one_post_edited_in_place_that_ends_as_the_final(connect):
    connector, rest = await connect(enable_streaming=True)

    await _say(connector, "The tide ", "turns at four.")
    assert len(rest.calls("POST", "/posts")) == 1 and rest.calls("PUT", "/posts/")
    assert await connector.conclude(MESSAGE, MARK_DONE, "The tide turns at four.") is True

    (post,) = rest.posts.values()
    assert (post["message"], post["root_id"]) == ("The tide turns at four.", MESSAGE.message_id)


async def test_a_streamed_reply_with_no_server_post_id_stays_unopened(connect):
    connector, rest = await connect(enable_streaming=True)
    rest.routes[("POST", "/posts")] = (201, {})

    await _say(connector, "draft")

    (reply,) = connector.surfaces._replies.values()
    assert reply.ids == [] and reply._open is False


async def test_every_delta_reaches_the_post_also_those_written_while_an_edit_waits(connect, monkeypatch):
    connector, rest = await connect(enable_streaming=True)
    monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.05)
    words = [f"word{index} " for index in range(12)]

    for word in words:
        await connector.progress(MESSAGE, TurnEvent(EVENT_TEXT_DELTA, text=word))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)

    assert _messages(rest) == ["".join(words).strip()]


async def test_a_reply_that_is_not_the_final_comes_down_and_the_host_posts_the_final(connect):
    connector, rest = await connect(enable_streaming=True)

    await _say(connector, "draft")
    assert await connector.conclude(MESSAGE, MARK_FAILED) is False

    assert rest.posts == {} and [post["message"] for post in rest.deleted] == ["draft"]


async def test_a_stopped_reply_keeps_what_it_wrote_marked_as_stopped(connect):
    connector, rest = await connect(enable_streaming=True)

    await _say(connector, "halfway")
    assert await connector.conclude(MESSAGE, MARK_STOPPED) is False

    assert _messages(rest) == ["halfway\n\n_Turn stopped_"]


async def test_a_refused_edit_keeps_what_the_post_shows_and_the_reply_goes_on_below(connect):
    connector, rest = await connect(enable_streaming=True)
    await _say(connector, "first part")
    (first,) = rest.posts
    rest.routes[("PUT", f"/posts/{first}/patch")] = TOO_LATE

    await _say(connector, " and the rest")
    assert await connector.conclude(MESSAGE, MARK_DONE, "first part and the rest") is True

    assert _messages(rest) == ["first part", "and the rest"]
    assert list(rest.posts.values())[1]["root_id"] == MESSAGE.message_id


async def test_a_reply_longer_than_the_server_takes_goes_on_in_a_new_post(connect):
    connector, rest = await connect(enable_streaming=True)
    rest.client_config["MaxPostSize"] = "20"

    await _say(connector, "one two three four ", "five six seven eight ", "nine")
    final = "one two three four five six seven eight nine"
    assert await connector.conclude(MESSAGE, MARK_DONE, final) is True

    assert _messages(rest) == ["one two three four", "five six seven eight", "nine"]


def _card(rest: FakeRest) -> dict:
    """The one attachment of the one card posted."""
    (post,) = rest.posts.values()
    assert post["message"] == ""
    (attachment,) = post["props"]["attachments"]
    return attachment


async def test_the_card_comes_after_its_delay_with_a_stop_button_and_settles_without_it(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0,
                                    actions_url="http://belle.invalid:18400")
    called = TurnEvent(EVENT_TOOL_CALLED, data={"name": "read_conversation", "call_id": "k1"})

    await connector.progress(MESSAGE, called)
    await asyncio.sleep(0.01)
    card = _card(rest)
    assert (card["title"], card["color"]) == ("Working...", surfaces.COLOR_WORKING)
    assert card["text"] == "1 of 1 tool running\n:zap: **read_conversation**"
    assert card["fallback"] == "Working... - 1 of 1 tool running"
    (button,) = card["actions"]
    assert (button["name"], button["integration"]["context"]["message"]) == ("Stop", MESSAGE.message_id)

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, text='{"ok": true}', data={"call_id": "k1"}))
    await connector.conclude(MESSAGE, MARK_DONE, "")
    card = _card(rest)
    assert (card["title"], card["color"], "actions" in card) == ("Turn finished", surfaces.COLOR_DONE, False)
    assert card["text"] == "1 tool finished\n:white_check_mark: **read_conversation**"


async def test_an_activity_card_with_no_server_post_id_is_not_marked_as_posted(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    rest.routes[("POST", "/posts")] = (201, {})
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "read_conversation", "call_id": "k1"}))
    await asyncio.sleep(0.01)

    (card,) = connector.surfaces.cards.cards.values()
    assert card.post_id == "" and card.next_edit == 0


async def test_a_subagent_shows_its_type_and_what_its_call_says_it_does(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    arguments = {"description": "Run `sleep 10` && echo @all", "prompt": "p"}

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={
        "name": "subagent", "call_id": "s1", "subagent": "general", "arguments": arguments}))
    await asyncio.sleep(0.01)
    assert _card(rest)["text"] == "1 of 1 subagent running\n:zap: **general** · Run sleep 10 echo all"

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, data={"call_id": "s1", "failed": True}))
    await connector.conclude(MESSAGE, MARK_FAILED)
    card = _card(rest)
    assert (card["title"], card["color"]) == ("Turn failed, 1 failed", surfaces.COLOR_FAILED)
    assert card["text"] == "1 of 1 subagent failed\n:x: **general** · Run sleep 10 echo all"


async def test_a_tool_routed_from_the_child_session_does_not_end_its_subagent(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={
        "name": "subagent", "call_id": "s1", "subagent": "general"}))
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "bash", "call_id": "s1", "child": True}))
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, data={"call_id": "s1", "failed": True, "child": True}))
    await asyncio.sleep(0.01)
    assert _card(rest)["text"] == "1 of 1 subagent running\n:zap: **general**"

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, text="{}", data={"call_id": "s1"}))
    await connector.conclude(MESSAGE, MARK_FAILED)
    assert _card(rest)["title"] == "Turn failed"


async def test_the_card_names_the_running_runs_first_and_counts_the_rest(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    for index in range(progress.MAX_RUN_LINES + 2):
        await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": f"tool{index}", "call_id": f"k{index}"}))
        if index:
            await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, text="{}", data={"call_id": f"k{index}"}))
    await asyncio.sleep(0.01)

    lines = _card(rest)["text"].splitlines()
    assert lines[1] == ":zap: **tool0**" and lines[-1] == "and 2 more"
    assert len(lines) == progress.MAX_RUN_LINES + 2


async def test_a_stopped_card_that_cannot_come_down_says_it_stopped(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "post_message", "call_id": "k1"}))
    await asyncio.sleep(0.01)
    (post_id,) = rest.posts
    rest.routes[("DELETE", f"/posts/{post_id}")] = (500, {"id": "app_error"})

    await connector.conclude(MESSAGE, MARK_STOPPED)

    card = _card(rest)
    assert (card["title"], card["color"], "actions" in card) == ("Turn stopped", surfaces.COLOR_STOPPED, False)


async def test_a_card_of_a_turn_that_ends_silent_comes_down(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "post_message", "call_id": "k1"}))
    await asyncio.sleep(0.01)

    await connector.conclude(MESSAGE, ENDED_SILENT)

    assert rest.posts == {} and len(rest.deleted) == 1


async def test_a_turn_that_calls_no_tool_gets_a_card_with_only_its_stop_button_which_goes_at_the_end(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, actions_url="http://belle.invalid:18400")

    await connector.begin(MESSAGE)
    await asyncio.sleep(0.01)
    card = _card(rest)
    assert (card["title"], card["text"], card["fallback"]) == ("Working...", "", "Working...")
    (button,) = card["actions"]
    assert button["name"] == "Stop"

    await connector.conclude(MESSAGE, MARK_DONE, "")

    assert rest.posts == {} and len(rest.deleted) == 1


async def test_without_the_action_listener_a_begun_turn_posts_no_card(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)

    await connector.begin(MESSAGE)
    await asyncio.sleep(0.01)

    assert rest.calls("POST", "/posts") == []


async def test_a_turn_that_ends_before_the_delay_shows_no_card(connect):
    connector, rest = await connect(activity_card_delay_seconds=30)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "post_message", "call_id": "k1"}))

    await connector.conclude(MESSAGE, MARK_DONE, "done")

    assert rest.calls("POST", "/posts") == []
