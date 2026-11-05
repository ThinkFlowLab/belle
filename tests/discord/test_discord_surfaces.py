"""A running turn's reply shown as it is written, and its activity card: Discord messages that Belle edits in place."""

import asyncio
import time
from dataclasses import replace

import pytest
from discord_transport import TOKEN, FakeGateway, FakeRest, hello, ready

from belle_agent import progress
from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent
from belle_agent.platforms.base import (
    ENDED_SILENT,
    MARK_DONE,
    MARK_FAILED,
    MARK_STOPPED,
    DeliveryUncertain,
    InboundMessage,
)
from belle_agent.platforms.discord import surfaces
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.render import RENDERER, discord_markdown
from belle_agent.platforms.discord.settings import DiscordSettings

GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
ROOT = "1000000000000000800"
MESSAGE = InboundMessage(
    platform="discord", workspace=GUILD, conversation=CHANNEL, message_id=ROOT, sender="1000000000000000700",
    text="hi", reply_thread=ROOT,
)


async def _ignore(_record):
    return "ignored"


@pytest.fixture
async def connect(monkeypatch):
    monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.0)
    started = []

    async def start(**settings):
        rest = FakeRest(serve=True)
        rest.gateway()
        rest.answer("GET", f"/channels/{CHANNEL}/messages/{ROOT}", (200, {"id": ROOT, "channel_id": CHANNEL}))
        rest.channels[CHANNEL] = {"id": CHANNEL, "type": 0, "guild_id": GUILD}

        def opens(socket):
            socket.feed(hello())
            socket.feed(ready())

        client = DiscordClient(TOKEN, transport=rest.transport(), open_socket=FakeGateway(opens).open, sleep=_no_wait)
        connector = DiscordPlatform(DiscordSettings(token=TOKEN, typing_indicator=False, **settings), client=client)
        await connector.start(_ignore)
        started.append(connector)
        return connector, rest

    yield start
    for connector in started:
        await connector.stop()


async def _no_wait(_seconds):
    await asyncio.sleep(0)


async def _say(connector, *deltas: str) -> None:
    for delta in deltas:
        await connector.progress(MESSAGE, TurnEvent(EVENT_TEXT_DELTA, text=delta))
        await asyncio.sleep(0.01)


def _shown(rest: FakeRest) -> dict[str, dict]:
    """Each message the bot posted and has not deleted, by id, as its edits left it, with its channel."""
    shown: dict[str, dict] = {}
    posted = iter(rest.posted)
    for request in rest.requests:
        parts = request.path.strip("/").split("/")
        if parts[0] != "channels" or len(parts) < 3 or parts[2] != "messages":
            continue
        if request.method == "POST" and len(parts) == 3:
            channel, body, message_id = next(posted)
            shown[message_id] = {**body, "channel": channel}
        elif request.method == "PATCH" and len(parts) == 4 and parts[3] in shown:
            shown[parts[3]].update(request.body)
        elif request.method == "DELETE" and len(parts) == 4 and not _refused(rest, request):
            shown.pop(parts[3], None)
    return shown


def _refused(rest: FakeRest, request) -> bool:
    """Whether the fake answered this request with a refusal a test queued."""
    return any(status >= 400 for status, _, _ in rest.routes.get((request.method, request.path), ()))


def _messages(rest: FakeRest) -> list[str]:
    return [message["content"] for message in _shown(rest).values()]


async def test_each_surface_is_shown_only_where_its_setting_turns_it_on(connect):
    connector, _ = await connect(enable_streaming=True, activity_card=False)
    assert set(connector.shows) == {EVENT_TEXT_DELTA, EVENT_STEP}
    connector, _ = await connect()
    assert set(connector.shows) == {EVENT_TOOL_CALLED, EVENT_TOOL_RESULT}


async def test_a_streamed_reply_is_one_message_edited_in_place_that_ends_as_the_final(connect):
    connector, rest = await connect(enable_streaming=True)

    await _say(connector, "The tide ", "turns at four.")
    assert len(rest.posted) == 1 and [r for r in rest.requests if r.method == "PATCH"]
    assert await connector.conclude(MESSAGE, MARK_DONE, "The tide turns at four.") is True

    (message,) = _shown(rest).values()
    assert (message["content"], message["channel"]) == ("The tide turns at four.", ROOT)
    assert rest.channels[ROOT]["parent_id"] == CHANNEL


@pytest.mark.parametrize("body", [b"{broken", [], {}, {"id": "invalid", "channel_id": ROOT},
                                       {"id": "1000000000000000901", "channel_id": CHANNEL}])
async def test_an_uncertain_stream_create_stops_further_posts_and_refuses_final_fallback(connect, body):
    connector, rest = await connect(enable_streaming=True)
    rest.answer("POST", f"/channels/{ROOT}/messages", (200, body))

    await _say(connector, "first", " second")

    with pytest.raises(DeliveryUncertain):
        await connector.conclude(MESSAGE, MARK_DONE, "first second")
    assert len(rest.calls("POST", f"/channels/{ROOT}/messages")) == 1


async def test_an_uncertain_final_continuation_refuses_final_fallback(connect):
    connector, rest = await connect(enable_streaming=True)
    await _say(connector, "first")
    rest.answer("POST", f"/channels/{ROOT}/messages", (200, b"{broken"))

    with pytest.raises(DeliveryUncertain):
        await connector.conclude(MESSAGE, MARK_DONE, "first " * 400)
    assert len(rest.calls("POST", f"/channels/{ROOT}/messages")) == 2


async def test_every_delta_reaches_the_message_also_those_written_while_an_edit_waits(connect, monkeypatch):
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

    (deleted,) = [r for r in rest.requests if r.method == "DELETE"]
    assert _shown(rest) == {} and deleted.path.startswith(f"/channels/{ROOT}/messages/")


async def test_a_stopped_reply_keeps_what_it_wrote_marked_as_stopped(connect):
    connector, rest = await connect(enable_streaming=True)

    await _say(connector, "halfway")
    assert await connector.conclude(MESSAGE, MARK_STOPPED) is False

    assert _messages(rest) == ["halfway\n\n_Turn stopped_"]


async def test_a_reply_longer_than_a_message_goes_on_in_a_new_message_in_the_thread(connect):
    connector, rest = await connect(enable_streaming=True)
    first, second = "alpha " * 300, "omega " * 300

    await _say(connector, first, second)
    final = (first + second).strip()
    assert await connector.conclude(MESSAGE, MARK_DONE, final) is True

    shown = list(_shown(rest).values())
    assert len(shown) == 2 and all(len(message["content"]) <= 2000 for message in shown)
    assert " ".join(message["content"] for message in shown).split() == final.split()
    assert {message["channel"] for message in shown} == {ROOT}


async def test_a_reply_is_cut_where_it_fits_as_discord_shows_it(connect):
    connector, _ = await connect(enable_streaming=True)
    # Short as written, and over 2000 characters once each row is padded to the widest cell.
    table = "| a | b |\n|---|---|\n" + "".join(f"| {n} | y |\n" for n in range(50)) + f"| {'x' * 40} | w |\n"

    text = table + "\n" + "tail " * 400

    index = connector.surfaces.cut_index(text, 2000)

    assert index is not None and len(discord_markdown(text[:index].strip())) <= 2000 < len(discord_markdown(text))
    # Cut as written, the start of the reply would not fit once its table is drawn.
    assert len(discord_markdown(text[: RENDERER.cut_index(text, 2000)].strip())) > 2000


async def test_a_rate_limit_that_outlasts_the_retries_spaces_the_next_edits(connect, monkeypatch):
    connector, rest = await connect(enable_streaming=True)
    monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.01)
    await _say(connector, "first")
    (message_id,) = _shown(rest)
    rest.answer("PATCH", f"/channels/{ROOT}/messages/{message_id}", (429, {"retry_after": 0.25, "global": False}))

    await _say(connector, " second")
    await asyncio.sleep(0.05)

    (reply,) = connector.surfaces._replies.values()
    assert reply.interval == 0.25


def _card(rest: FakeRest) -> dict:
    """The one card posted, as its edits left it."""
    (message,) = _shown(rest).values()
    assert message["content"] == ""
    return message


async def test_the_card_comes_after_its_delay_with_a_stop_button_and_settles_without_it(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    called = TurnEvent(EVENT_TOOL_CALLED, data={"name": "read_conversation", "call_id": "k1"})

    await connector.progress(MESSAGE, called)
    await asyncio.sleep(0.01)
    card = _card(rest)
    (embed,) = card["embeds"]
    assert (embed["title"], embed["color"], card["channel"]) == ("Working...", surfaces.COLOR_WORKING, ROOT)
    assert embed["description"] == "1 of 1 tool running\n\N{HIGH VOLTAGE SIGN} **read_conversation**"
    (button,) = card["components"][0]["components"]
    assert button["label"] == "Stop" and button["custom_id"].endswith(f":x:{ROOT}")

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, text='{"ok": true}', data={"call_id": "k1"}))
    await connector.conclude(MESSAGE, MARK_DONE, "")
    card = _card(rest)
    (embed,) = card["embeds"]
    assert (embed["title"], embed["color"], card["components"]) == ("Turn finished", surfaces.COLOR_DONE, [])
    assert embed["description"] == "1 tool finished\n\N{WHITE HEAVY CHECK MARK} **read_conversation**"


async def test_a_click_on_the_card_s_stop_button_reaches_the_host_as_a_stop(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)
    received = []

    async def on_inbound(record):
        received.append(record)
        return "stopped:test"

    connector._on_inbound = on_inbound
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "post_message", "call_id": "k1"}))
    await asyncio.sleep(0.01)
    (button,) = _card(rest)["components"][0]["components"]

    outcome = await connector.questions.on_interaction({
        "id": "1", "type": 3, "token": "t", "guild_id": GUILD, "channel_id": ROOT,
        "member": {"user": {"id": "1000000000000000700"}}, "data": {"custom_id": button["custom_id"]},
    })

    assert outcome == "stopped:test" and (received[0].message_id, received[0].conversation) == (ROOT, CHANNEL)


async def test_a_stale_stop_click_removes_components_after_a_failed_settle_edit(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "read_conversation", "call_id": "k1"}))
    await asyncio.sleep(0.01)
    (card_id,) = _shown(rest)
    card_before = _card(rest)
    (button,) = card_before["components"][0]["components"]
    rest.answer("PATCH", f"/channels/{ROOT}/messages/{card_id}", (500, {"code": 0}))

    await connector.conclude(MESSAGE, MARK_DONE)

    outcome = await connector.questions.on_interaction({
        "id": "1", "type": 3, "token": "t", "guild_id": GUILD, "channel_id": ROOT,
        "member": {"user": {"id": "1000000000000000700"}}, "data": {"custom_id": button["custom_id"]},
    })

    assert outcome == "ignored"
    assert [request.body for request in rest.requests
            if request.method == "PATCH" and request.path.endswith("/messages/@original")] == [{"components": []}]


async def test_a_subagent_shows_its_type_and_what_its_call_says_it_does(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    arguments = {"description": "Run `sleep 10` && echo @everyone", "prompt": "p"}

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={
        "name": "subagent", "call_id": "s1", "subagent": "general", "arguments": arguments}))
    await asyncio.sleep(0.01)
    assert _card(rest)["embeds"][0]["description"] == (
        "1 of 1 subagent running\n\N{HIGH VOLTAGE SIGN} **general** · Run sleep 10 echo everyone")
    assert _card(rest)["allowed_mentions"] == {"parse": []}

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, data={"call_id": "s1", "subagent": "general", "failed": True}))
    await connector.conclude(MESSAGE, MARK_FAILED)
    (embed,) = _card(rest)["embeds"]
    assert (embed["title"], embed["color"]) == ("Turn failed, 1 failed", surfaces.COLOR_FAILED)


async def test_the_card_names_the_running_runs_first_and_counts_the_rest(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    for index in range(progress.MAX_RUN_LINES + 2):
        await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": f"tool{index}", "call_id": f"k{index}"}))
        if index:
            await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_RESULT, text="{}", data={"call_id": f"k{index}"}))
    await asyncio.sleep(0.01)

    lines = _card(rest)["embeds"][0]["description"].splitlines()
    assert lines[1] == "\N{HIGH VOLTAGE SIGN} **tool0**" and lines[-1] == "and 2 more"
    assert len(lines) == progress.MAX_RUN_LINES + 2


async def test_a_card_that_discord_will_not_edit_yet_waits_as_long_as_it_asks(connect):
    connector, rest = await connect(activity_card_delay_seconds=0, activity_card_min_edit_seconds=0)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "a", "call_id": "k1"}))
    await asyncio.sleep(0.01)
    (card_id,) = _shown(rest)
    rest.answer("PATCH", f"/channels/{ROOT}/messages/{card_id}", (429, {"retry_after": 30.0, "global": False}))

    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "b", "call_id": "k2"}))

    (card,) = connector.surfaces._cards.values()
    assert card.next_edit - time.monotonic() > 25


async def test_a_stopped_card_that_cannot_come_down_says_it_stopped(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "post_message", "call_id": "k1"}))
    await asyncio.sleep(0.01)
    (card_id,) = _shown(rest)
    rest.answer("DELETE", f"/channels/{ROOT}/messages/{card_id}", (500, {"code": 0}))

    await connector.conclude(MESSAGE, MARK_STOPPED)

    card = _card(rest)
    (embed,) = card["embeds"]
    assert (embed["title"], embed["color"], card["components"]) == ("Turn stopped", surfaces.COLOR_STOPPED, [])


async def test_a_card_of_a_turn_that_ends_silent_comes_down(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "post_message", "call_id": "k1"}))
    await asyncio.sleep(0.01)

    await connector.conclude(MESSAGE, ENDED_SILENT)

    assert _shown(rest) == {} and len([r for r in rest.requests if r.method == "DELETE"]) == 1


async def test_a_turn_that_calls_no_tool_gets_a_card_with_only_its_stop_button_which_goes_at_the_end(connect):
    connector, rest = await connect(activity_card_delay_seconds=0)

    await connector.begin(MESSAGE)
    await asyncio.sleep(0.01)
    card = _card(rest)
    (embed,) = card["embeds"]
    assert embed == {"title": "Working...", "color": surfaces.COLOR_WORKING}
    (button,) = card["components"][0]["components"]
    assert button["label"] == "Stop"

    await _say(connector, "the answer")
    await connector.conclude(MESSAGE, MARK_DONE, "the answer")

    assert [message["content"] for message in _shown(rest).values()] == ["the answer"]


async def test_a_begun_card_is_not_posted_with_the_card_turned_off(connect):
    connector, rest = await connect(activity_card=False, activity_card_delay_seconds=0)

    await connector.begin(MESSAGE)
    await asyncio.sleep(0.01)

    assert rest.posted == []


async def test_a_turn_that_ends_before_the_delay_shows_no_card(connect):
    connector, rest = await connect(activity_card_delay_seconds=30)
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "post_message", "call_id": "k1"}))

    await connector.conclude(MESSAGE, MARK_DONE, "done")

    assert rest.posted == []


async def test_active_thread_surfaces_keep_their_channels_until_turn_cleanup(connect):
    connector, rest = await connect(enable_streaming=True, activity_card_delay_seconds=0)
    await _say(connector, "draft")
    reply_id = rest.posted[0][2]
    await connector.progress(MESSAGE, TurnEvent(EVENT_TOOL_CALLED, data={"name": "tool", "call_id": "active"}))
    await asyncio.sleep(0.01)
    card_id = rest.posted[1][2]

    for index in range(257):
        other = replace(MESSAGE, message_id=str(int(ROOT) + index + 1))
        await connector.progress(other, TurnEvent(EVENT_TEXT_DELTA, text="other reply"))
        await asyncio.sleep(0)
    await _say(connector, " continued")
    assert any(request.path == f"/channels/{ROOT}/messages/{reply_id}" and request.body["content"] == "draft continued"
               for request in rest.requests if request.method == "PATCH")

    await connector.conclude(MESSAGE, MARK_STOPPED)
    assert rest.calls("DELETE", f"/channels/{ROOT}/messages/{card_id}")
    assert reply_id not in connector.surfaces._channels
    assert card_id not in connector.surfaces._channels


async def test_silent_thread_reply_cleanup_survives_other_surface_posts(connect):
    connector, rest = await connect(enable_streaming=True)
    await _say(connector, "draft")
    reply_id = rest.posted[0][2]
    for index in range(257):
        other = replace(MESSAGE, message_id=str(int(ROOT) + index + 1))
        await connector.progress(other, TurnEvent(EVENT_TEXT_DELTA, text="other reply"))
        await asyncio.sleep(0)

    await connector.conclude(MESSAGE, ENDED_SILENT)
    assert rest.calls("DELETE", f"/channels/{ROOT}/messages/{reply_id}")
    assert reply_id not in connector.surfaces._channels
