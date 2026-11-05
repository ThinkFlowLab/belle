"""What the Slack connector shows of a running turn: the reply as it is written, and the activity card.

Slack is ``tests/slack_transport.py``: no connection is opened and the tokens
are synthetic. What Slack was sent is read from the recorded calls, and what a
reader would see from the messages the fake keeps on screen. A refusal is an
absent call beside a control that makes it. Time is a clock the check moves:
no check waits for a delay to pass.
"""

import asyncio
import json
import logging

import pytest
import pytest_asyncio
from conformance_kit import ModelTurn, worlds
from scripted_backend import ScriptedBackend, ScriptedTurn
from slack_kit import SlackKit
from slack_transport import TEAM, click_body

from belle_agent.backend import (
    EVENT_STEP,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    SUBAGENT,
    TurnEvent,
)
from belle_agent.click_notices import STOP_ACCEPTED_NOTICE, STOP_REFUSED_NOTICE, STOP_STALE_NOTICE
from belle_agent.config import BelleConfigError, load_config
from belle_agent.host import (
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_POSTED_BY_TOOL,
    OUTCOME_POSTED_FINAL,
    OUTCOME_WITHHELD,
)
from belle_agent.platforms.base import (
    ENDED_SILENT,
    MARK_DONE,
    MARK_FAILED,
    MARK_STOPPED,
    TRIGGER_MENTION,
    InboundMessage,
    InboundStop,
    create_platform,
)
from belle_agent.platforms.slack import activity, streaming
from belle_agent.platforms.slack.activity import STOP_ACTION_ID
from belle_agent.platforms.slack.stream_delivery import STREAM_STOPPED_TAIL
from belle_agent.platforms.slack.text import MAX_SLACK_UPDATE_TEXT_LENGTH
from belle_agent.scopes import capabilities as scope_capabilities

ROOM = "C0ROOM0001"
DIRECT = "D0DIRECT01"
ASKER = "U0ASKER001"
BYSTANDER = "U0OTHER002"
LEAD = "U0LEAD0003"
TS = "1710000001.000100"

STREAMING = "platforms: {slack: {enforce_audience: true, acknowledge_mode: 'off', activity_card: false, enable_streaming: true}}\n"
CARD = "platforms: {slack: {enforce_audience: true, acknowledge_mode: 'off', activity_card: true}}\n"
ANSWER_EVERYTHING = """
scopes:
  - match: {channel: slack}
    delivery: {mode: [all]}
"""
SILENCE_ALLOWED = """
scopes:
  - match: {channel: slack}
    delivery: {mode: [all], reply: optional}
"""


class Clock:
    """The time the card and the stream read, moved by a check."""

    def __init__(self) -> None:
        self.now = 5000.0
        self._sleepers: list[tuple[float, asyncio.Future]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            return await _real_sleep(0)
        waiting = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, waiting))
        await waiting

    async def advance(self, seconds: float) -> None:
        # Whatever was started goes to sleep at the time it was started, before that time moves.
        await settle()
        self.now += seconds
        for due, waiting in list(self._sleepers):
            if due <= self.now:
                self._sleepers.remove((due, waiting))
                if not waiting.done():
                    waiting.set_result(None)
        await settle()

    def __getattr__(self, name: str):
        # The module whose sleep this stands in for.
        return getattr(asyncio, name)


_real_sleep = asyncio.sleep


async def settle() -> None:
    """Let every task that can run without the clock moving run."""
    for _ in range(50):
        await _real_sleep(0)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """A clock in place of the one the card and the stream read. Nothing else in the process is touched."""
    clock = Clock()
    for module in (activity, streaming):
        monkeypatch.setattr(module, "time", clock)
        monkeypatch.setattr(module, "asyncio", clock)
    return clock


@pytest.fixture
def slack(monkeypatch) -> SlackKit:
    return SlackKit(monkeypatch)


@pytest_asyncio.fixture
async def connected(slack, tmp_path):
    """A connector with no host behind it. What it hands over is kept, and answered with ``outcomes``."""
    registry = scope_capabilities.snapshot_registry()
    started = []

    async def connect(text: str, outcome: str = "dispatched:test"):
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        platform = create_platform(load_config(path), "slack")
        received: list = []

        async def on_inbound(record) -> str:
            received.append(record)
            return outcome

        await platform.start(on_inbound)
        started.append(platform)
        return platform, slack.sockets[-1].web, received

    yield connect
    for platform in started:
        await platform.stop()
    scope_capabilities.restore_registry(registry)


@pytest_asyncio.fixture
async def build_world(slack, tmp_path):
    """A host on the connector, with the scripted backend behind it."""
    async with worlds(ScriptedBackend(), tmp_path, slack) as build:
        yield build


def _asked(conversation: str = ROOM, message_id: str = TS, **fields) -> InboundMessage:
    """A message that asked for a turn in a channel. Its answer goes in the thread it opens."""
    fields.setdefault("reply_thread", message_id)
    return InboundMessage(
        platform="slack", workspace=TEAM, conversation=conversation, message_id=message_id, sender=ASKER,
        text="a question", event_id="Ev0001", chat_type="channel", trigger=TRIGGER_MENTION, **fields,
    )


def _delta(text: str) -> TurnEvent:
    return TurnEvent(EVENT_TEXT_DELTA, text=text)


def _called(name: str, call_id: str, **data) -> TurnEvent:
    return TurnEvent(EVENT_TOOL_CALLED, data={"name": name, "call_id": call_id, **data})


def _result(name: str, call_id: str, text: str = "", **data) -> TurnEvent:
    return TurnEvent(EVENT_TOOL_RESULT, text=text, data={"name": name, "call_id": call_id, **data})


def _methods(web, *names: str) -> list[str]:
    wanted = names or ("chat_postMessage", "chat_update", "chat_delete", "chat_startStream", "chat_appendStream", "chat_stopStream")
    return [method for method, _ in web.calls if method in wanted]


def _message(text: str = "hello", **fields) -> dict:
    return {"type": "message", "channel": ROOM, "channel_type": "channel", "user": ASKER, "text": text, "ts": TS, **fields}


def _cards(web) -> list[dict]:
    """Each activity card on screen: the message of this app whose blocks hold a plan."""
    return [
        {"ts": ts, **message}
        for ts, message in web.screen.items()
        if any(block.get("type") == "plan" for block in message.get("blocks") or ())
    ]


def _tasks(card: dict) -> dict[str, dict]:
    return {task["title"]: task for task in card["blocks"][0]["tasks"]}


def _stop_button(card: dict) -> dict | None:
    for block in card["blocks"]:
        for element in block.get("elements", ()) if block.get("type") == "actions" else ():
            if element.get("action_id") == STOP_ACTION_ID:
                return element
    return None


def _flat(rich_text: dict | None) -> str:
    """The words and emoji names of one rich text field of a card, in order."""
    if not rich_text:
        return ""
    return "".join(
        part.get("text", "") or f":{part.get('name')}:"
        for section in rich_text["elements"]
        for part in section["elements"]
    )


# ── the settings ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("section", "shown"),
    [
        # The defaults of the Slack section: a card, and no reply shown as it is written.
        ("platforms: {slack: {enforce_audience: true}}\n", {EVENT_TOOL_CALLED, EVENT_TOOL_RESULT}),
        (STREAMING, {EVENT_TEXT_DELTA, EVENT_STEP}),
        ("platforms: {slack: {enforce_audience: true, activity_card: false}}\n", set()),
        ("platforms: {slack: {enforce_audience: true, enable_streaming: true}}\n", {EVENT_TEXT_DELTA, EVENT_STEP, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT}),
    ],
)
async def test_the_connector_shows_what_its_two_settings_turn_on(connected, section, shown):
    platform, _, _ = await connected(section)

    assert set(platform.shows) == shown
    assert {platform.shows.get(EVENT_TEXT_DELTA), platform.shows.get(EVENT_TOOL_CALLED)} <= {
        None, "platforms.slack.enable_streaming", "platforms.slack.activity_card",
    }


async def test_the_card_settings_keep_the_defaults_and_the_meaning_they_have_on_the_jiuwenswarm_channel(connected, caplog):
    platform, _, _ = await connected("platforms: {slack: {enforce_audience: true}}\n")
    assert (platform.config.activity_card, platform.config.enable_streaming) == (True, False)
    assert (platform.config.activity_card_delay_seconds, platform.config.activity_card_min_edit_seconds) == (5.0, 10.0)

    with caplog.at_level(logging.WARNING, logger="belle_agent.platforms.slack"):
        tuned, _, _ = await connected(
            "platforms: {slack: {enforce_audience: true, activity_card_delay_seconds: 2, activity_card_min_edit_seconds: 0}}\n"
        )
    assert (tuned.config.activity_card_delay_seconds, tuned.config.activity_card_min_edit_seconds) == (2.0, 0.0)
    # Neither surface is among the settings the connector says it does not act on.
    assert caplog.records == []


@pytest.mark.parametrize("value", ["soon", "'2'", "-1", "true"])
async def test_a_card_setting_that_is_not_seconds_is_refused(connected, value):
    with pytest.raises(BelleConfigError) as caught:
        await connected(f"platforms: {{slack: {{enforce_audience: true, activity_card_min_edit_seconds: {value}}}}}\n")

    assert str(caught.value) == "Belle config platforms.slack.activity_card_min_edit_seconds must be a number of seconds, 0 or more"


# ── a reply as it is written ─────────────────────────────────────────────


async def test_a_reply_in_a_thread_is_streamed_and_closed_with_the_rest_of_the_final(connected):
    platform, web, _ = await connected(STREAMING)
    asked = _asked()
    final = "The ferry leaves at noon.\nIt returns at four.\nBring a coat."

    await platform.progress(asked, _delta("The ferry leaves at noon.\nIt ret"))
    await platform.progress(asked, _delta("urns at four.\nBring"))
    await settle()
    delivered = await platform.conclude(asked, MARK_DONE, final)

    assert delivered is True
    # Slack's own stream, in the thread the answer goes in, addressed to whoever asked.
    assert _methods(web) == ["chat_startStream", "chat_appendStream", "chat_stopStream"]
    opened = web.payload("chat_startStream")
    assert (opened["channel"], opened["thread_ts"]) == (ROOM, TS)
    assert (opened["recipient_user_id"], opened["recipient_team_id"]) == (ASKER, TEAM)
    # Only whole lines are sent while the reply is written, and the close carries the rest.
    assert opened["chunks"] == [{"type": "markdown_text", "text": "The ferry leaves at noon.\n"}]
    assert web.payload("chat_appendStream")["chunks"] == [{"type": "markdown_text", "text": "It returns at four.\n"}]
    assert web.payload("chat_stopStream")["chunks"] == [{"type": "markdown_text", "text": "Bring a coat."}]
    # One message, and it reads as the final.
    assert web.shown(ROOM) == [final] and web.streaming == set()


async def test_a_new_step_starts_a_paragraph_and_a_step_that_continues_the_text_does_not(connected):
    platform, web, _ = await connected(STREAMING)
    asked = _asked()

    for event in (
        _delta("The ferry leaves at no"),
        TurnEvent(EVENT_STEP, data={"continues": True}),
        _delta("on.\n"),
        TurnEvent(EVENT_STEP),
        _delta("Bring a coat.\n"),
    ):
        await platform.progress(asked, event)
    await settle()

    sent = [chunk["text"] for method in ("chat_startStream", "chat_appendStream") for chunk in web.payload(method)["chunks"]]
    assert sent == ["The ferry leaves at noon.\n", "\nBring a coat.\n"]


async def test_a_reply_outside_a_thread_is_shown_in_a_message_that_is_edited(connected):
    platform, web, _ = await connected(STREAMING)
    asked = _asked(DIRECT, reply_thread="")

    await platform.progress(asked, _delta("The ferry "))
    await platform.progress(asked, _delta("leaves at noon."))
    await settle()
    delivered = await platform.conclude(asked, MARK_DONE, "The ferry leaves at noon. Bring a coat.")

    assert delivered is True
    # A stream has to be in a thread. Outside one the reply is a message that is rewritten as it grows.
    assert _methods(web) == ["chat_postMessage", "chat_update", "chat_update"]
    assert [payload["text"] for payload in web.payloads("chat_update")] == [
        "The ferry leaves at noon.", "The ferry leaves at noon. Bring a coat.",
    ]
    assert web.shown(DIRECT) == ["The ferry leaves at noon. Bring a coat."]


async def test_a_reply_whose_final_will_be_withheld_is_never_streamed(build_world):
    world = await build_world(STREAMING + SILENCE_ALLOWED)
    web = world.platform.web
    world.kit.play(ModelTurn(final="a thought nobody asked for\n"), ModelTurn(final="an answer that is owed\n"))

    await world.platform.app.deliver(_message("just talking"))
    await world.host.idle()

    # The host knew at admission that this final would be withheld, so no delta was handed over.
    assert _methods(web) == [] and web.screen == {}
    assert world.outcomes() == [OUTCOME_WITHHELD]

    # Control: the same connector and backend, a direct message, which is always owed its answer.
    await world.platform.app.deliver({**_message("a question", ts="1710000002.000100"), "channel": DIRECT, "channel_type": "im"})
    await world.host.idle()
    assert web.shown(DIRECT) == ["an answer that is owed"]
    assert world.outcomes() == [OUTCOME_WITHHELD, OUTCOME_POSTED_FINAL]


async def test_a_turn_that_starts_a_reply_and_then_posts_through_the_tool_leaves_only_the_tools_post(build_world):
    world = await build_world(STREAMING + ANSWER_EVERYTHING)
    web = world.platform.web
    said = (("post_message", {"text": "said through the tool", "reply_to_message_id": TS}),)
    world.kit.play(ScriptedTurn(narration="Let me post that.\n", final="Posted it.\n", calls=said))

    await world.platform.app.deliver(_message("say it"))
    await world.host.idle()

    # The narration was shown while it was written. The tool's post is the reply, so what was shown is taken down
    # and the final is not posted beside it.
    assert _methods(web) == ["chat_startStream", "chat_postMessage", "chat_stopStream", "chat_delete"]
    assert web.shown(ROOM) == ["said through the tool"]
    assert world.outcomes() == [OUTCOME_POSTED_BY_TOOL]


async def test_text_written_before_a_tool_call_is_shown_and_the_message_ends_as_the_final_alone(build_world):
    world = await build_world(STREAMING + ANSWER_EVERYTHING)
    web = world.platform.web
    world.kit.play(
        ScriptedTurn(narration="Let me look.\n", final="It is noon.\nThe ferry has left.\n", calls=(("react_to_message", {"emoji": "eyes"}),))
    )

    await world.platform.app.deliver(_message("what time is it"))
    await world.host.idle()

    # What an earlier step wrote is no part of the final. The stream is closed as it is and the message rewritten.
    assert _methods(web) == ["chat_startStream", "chat_appendStream", "chat_stopStream", "chat_update"]
    assert "chunks" not in web.payload("chat_stopStream")
    assert web.shown(ROOM) == ["It is noon.\nThe ferry has left."]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_a_stream_that_expires_mid_turn_ends_as_the_final_and_not_as_a_fragment_beside_it(connected, caplog):
    """An expired stream must not leave a fragment beside the final answer."""
    platform, web, _ = await connected(STREAMING)
    asked = _asked()
    early = "Reading the three reports.\n" + "A line of working notes.\n" * 30
    final = "\n".join(f"Finding {number}: the tide table for week {number} is wrong." for number in range(1, 200))
    assert len(early) > 700 and len(final) > 2 * MAX_SLACK_UPDATE_TEXT_LENGTH

    await platform.progress(asked, _delta(early))
    await settle()
    bubble = next(iter(web.screen))
    # Slack ends a stream that has been open too long. The message stays, and takes no more.
    web.expire(bubble)
    with caplog.at_level(logging.WARNING, logger="belle_agent.platforms.slack"):
        await platform.progress(asked, TurnEvent(EVENT_STEP))
        await platform.progress(asked, _delta(final[:900] + "\n"))
        await settle()
        delivered = await platform.conclude(asked, MARK_DONE, final)

    # Slack refuses both operations after a stream expires.
    refused = [method for method, payload in web.calls if method in ("chat_appendStream", "chat_stopStream")]
    assert refused == ["chat_appendStream", "chat_stopStream"]
    assert "message_not_in_streaming_state" in caplog.text
    # The bubble was rewritten as the first part of the final, and the rest follows in the same thread.
    assert delivered is True
    assert _methods(web)[-3:] == ["chat_stopStream", "chat_update", "chat_postMessage"]
    assert web.payload("chat_update")["ts"] == bubble
    shown = web.shown(ROOM)
    # The whole final, once, and nothing else: no message still holds the working notes.
    assert "".join(shown).replace("\n", "") == final.replace("\n", "")
    assert not any("working notes" in text for text in shown)
    assert "".join(shown).count("Finding 1: ") == 1


async def test_an_expired_stream_whose_final_fits_one_message_is_rewritten_whole(connected, caplog):
    platform, web, _ = await connected(STREAMING)
    asked = _asked()
    final = "The ferry leaves at noon.\nIt returns at four.\nBring a coat."

    await platform.progress(asked, _delta("The ferry leaves at noon.\n"))
    await settle()
    web.expire(next(iter(web.screen)))
    with caplog.at_level(logging.INFO, logger="belle_agent.platforms.slack"):
        await platform.progress(asked, _delta("It returns at four.\n"))
        await settle()
        delivered = await platform.conclude(asked, MARK_DONE, final)

    # Here the final continues what was streamed, and the stream will take no more: one message, rewritten whole.
    assert delivered is True
    assert _methods(web) == ["chat_startStream", "chat_appendStream", "chat_stopStream", "chat_update"]
    assert web.shown(ROOM) == [final]
    assert "[slack] the expired stream was rewritten whole" in caplog.text
    # Every line of the surface is tagged as this connector's and logged under the platform module that
    # writes it: none is tagged or named as the JiuwenSwarm channel.
    assert "[SlackChannel]" not in caplog.text
    assert {record.name for record in caplog.records} <= {
        "belle_agent.platforms.slack.turn_stream",
        "belle_agent.platforms.slack.stream_delivery",
    }


async def test_a_reply_that_cannot_be_rewritten_as_its_final_is_taken_down_and_the_final_posted_whole(build_world):
    world = await build_world(STREAMING + ANSWER_EVERYTHING)
    web = world.platform.web
    web.fail = lambda method, payload: "message_not_found" if method == "chat_update" else None
    world.kit.play(
        ScriptedTurn(narration="Let me look.\n", final="It is noon.\n", calls=(("react_to_message", {"emoji": "eyes"}),))
    )

    await world.platform.app.deliver(_message("what time is it"))
    await world.host.idle()

    # The host is told the reply did not become the final, and posts it as it would with nothing shown.
    assert _methods(web)[-3:] == ["chat_update", "chat_delete", "chat_postMessage"]
    assert web.shown(ROOM) == ["It is noon."]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_a_stream_that_will_not_open_falls_back_to_edits_and_then_to_the_whole_final(build_world, caplog):
    world = await build_world(STREAMING + ANSWER_EVERYTHING)
    web = world.platform.web
    web.fail = lambda method, payload: "not_allowed" if method == "chat_startStream" else None
    world.kit.play(ModelTurn(final="A first line.\nA second line.\n"), ModelTurn(final="A third line.\nA fourth line.\n"))

    with caplog.at_level(logging.INFO, logger="belle_agent.platforms.slack"):
        await world.platform.app.deliver(_message("first"))
        await world.host.idle()
    assert "showing this reply with edits instead" in caplog.text
    assert web.shown(ROOM) == ["A first line.\nA second line."]
    assert _methods(web, "chat_postMessage", "chat_update")[:1] == ["chat_postMessage"]

    # Neither surface opens: nothing is shown while the reply is written, and the final is posted once.
    web.fail = lambda method, payload: "not_allowed" if method in ("chat_startStream", "chat_postMessage") and payload.get("text") != "A third line.\nA fourth line." else None
    web.calls.clear()
    await world.platform.app.deliver(_message("second", ts="1710000002.000100"))
    await world.host.idle()
    assert web.shown(ROOM) == ["A first line.\nA second line.", "A third line.\nA fourth line."]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL, OUTCOME_POSTED_FINAL]


async def test_a_reply_being_written_when_its_turn_fails_is_taken_down_before_the_failure_is_told(build_world):
    world = await build_world(STREAMING + ANSWER_EVERYTHING)
    web = world.platform.web
    world.kit.play(ScriptedTurn(final="half of it\nand a half nobody gets\n", crash_mid_reply=True))

    await world.platform.app.deliver(_message("go"))
    await world.host.idle()

    assert _methods(web) == ["chat_startStream", "chat_stopStream", "chat_delete", "chat_postMessage"]
    assert web.shown(ROOM) == ["The turn failed: RuntimeError"]
    assert world.outcomes() == [OUTCOME_FAILED]


async def test_a_reply_being_written_when_its_turn_is_stopped_keeps_what_it_wrote_and_says_so(connected):
    platform, web, _ = await connected(STREAMING)
    asked = _asked()

    await platform.progress(asked, _delta("The ferry leaves at noon.\nIt ret"))
    await settle()
    delivered = await platform.conclude(asked, MARK_STOPPED)

    assert delivered is False
    assert _methods(web) == ["chat_startStream", "chat_stopStream"]
    assert web.shown(ROOM) == ["The ferry leaves at noon.\n" + STREAM_STOPPED_TAIL]
    assert web.streaming == set()


async def test_text_that_is_written_fast_costs_one_call_for_each_interval_and_not_one_for_each_delta(connected, monkeypatch, clock):
    monkeypatch.setattr(streaming, "_STREAM_APPEND_MIN_INTERVAL_SECONDS", 0.6)
    platform, web, _ = await connected(STREAMING)
    asked = _asked()

    await platform.progress(asked, _delta("Line 0.\n"))
    for number in range(1, 40):
        await platform.progress(asked, _delta(f"Line {number}.\n"))
        await settle()
    # Forty lines in no time at all: the stream was opened, and one append is all the interval allowed.
    assert _methods(web) == ["chat_startStream", "chat_appendStream"]

    # Each interval allows one more call, and what was written meanwhile travels together.
    await clock.advance(0.6)
    await clock.advance(0.6)
    assert _methods(web) == ["chat_startStream", "chat_appendStream", "chat_appendStream", "chat_appendStream"]
    assert web.shown(ROOM) == ["".join(f"Line {number}.\n" for number in range(40))]


# ── the activity card ────────────────────────────────────────────────────


async def test_a_short_turn_shows_no_card(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()

    await platform.progress(asked, _called("read_slack_conversation", "c1"))
    await platform.progress(asked, _result("read_slack_conversation", "c1", '{"ok": true}'))
    await clock.advance(4.9)
    assert await platform.conclude(asked, MARK_DONE, "the answer") is False
    await clock.advance(60)

    # The turn ended before the card was due, and a card is never posted for a turn that is over.
    assert web.calls == [("auth_test", {})]

    # Control: a turn that is still running once the delay has passed gets its card.
    later = _asked(message_id="1710000002.000100")
    await platform.progress(later, _called("read_slack_conversation", "c1"))
    await clock.advance(4.9)
    assert _cards(web) == []
    await clock.advance(0.1)
    (card,) = _cards(web)
    assert (card["channel"], card["thread_ts"]) == (ROOM, "1710000002.000100")
    assert card["blocks"][0]["title"] == "Working..."


def _stop_only(web) -> list[dict]:
    """Each card on screen that shows only its title and the stop button: a turn that has reported nothing."""
    return [
        {"ts": ts, **message}
        for ts, message in web.screen.items()
        if [block["type"] for block in message.get("blocks") or ()] == ["section", "actions"]
    ]


async def test_a_begun_turn_that_reports_nothing_shows_its_stop_after_the_delay_and_the_card_goes_at_the_end(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()

    await platform.begin(asked)
    await clock.advance(4.9)
    assert _stop_only(web) == []
    await clock.advance(0.1)
    (card,) = _stop_only(web)
    assert (card["channel"], card["thread_ts"]) == (ROOM, TS)
    assert card["blocks"][0]["text"]["text"] == "*Working...*"
    assert json.loads(_stop_button(card)["value"]) == {"session_id": TS, "request_id": TS, "channel_id": ROOM}

    assert await platform.conclude(asked, MARK_DONE, "the answer") is False

    assert web.screen == {} and _methods(web)[-1] == "chat_delete"
    assert platform._activity_records == {}


async def test_a_failed_turn_takes_down_a_card_that_held_only_its_stop_and_leaves_the_failure_to_its_notice(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()
    await platform.begin(asked)
    await clock.advance(5)
    assert len(_stop_only(web)) == 1

    await platform.conclude(asked, MARK_FAILED)

    assert web.screen == {} and _methods(web)[-1] == "chat_delete"


async def test_a_begun_card_becomes_the_card_of_the_tools_the_turn_then_calls(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()
    await platform.begin(asked)
    await clock.advance(5)

    await platform.progress(asked, _called("react_to_message", "c1"))
    await clock.advance(10)
    await platform.progress(asked, _result("react_to_message", "c1", '{"ok": true}'))

    (card,) = _cards(web)
    assert "react_to_message" in _flat(_tasks(card)["Tools"]["details"]) and _stop_button(card) is not None
    await platform.conclude(asked, MARK_DONE)
    (settled,) = _cards(web)
    assert settled["blocks"][0]["title"] == "Turn finished"


async def test_with_the_card_switched_off_a_begun_turn_shows_nothing(connected, clock):
    platform, web, _ = await connected(STREAMING)

    await platform.begin(_asked())
    await clock.advance(60)

    assert web.calls == [("auth_test", {})]


async def test_the_host_starts_the_card_of_a_turn_owed_a_reply_and_its_stop_stops_the_turn(build_world, clock):
    world = await build_world(CARD + ANSWER_EVERYTHING)
    world.kit.play(ScriptedTurn(final="said after the stop", hold_before_final=True))
    await world.platform.app.deliver(_message("think about it"))
    await world.kit.holding()
    await clock.advance(5)
    (card,) = _stop_only(world.platform.web)

    outcome = await _press_stop(world, card, user=ASKER)
    await world.host.idle()

    assert outcome.startswith("stopped") and world.outcomes() == [OUTCOME_INTERRUPTED]
    assert world.platform.web.screen == {}


async def test_the_host_starts_no_card_for_a_turn_whose_reply_is_optional(build_world, clock):
    world = await build_world(CARD + SILENCE_ALLOWED)
    world.kit.play(ScriptedTurn(final="", hold_before_final=True))
    await world.platform.app.deliver(_message("a note"))
    await world.kit.holding()
    await clock.advance(60)

    assert world.platform.web.screen == {}

    world.kit.release()
    await world.host.idle()


async def test_a_busy_turn_edits_its_card_no_more_often_than_the_interval_allows(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()
    await platform.progress(asked, _called("react_to_message", "c0"))
    await clock.advance(5)
    assert _methods(web) == ["chat_postMessage"]

    # Twenty tool calls and their results in nine seconds: not one edit.
    for number in range(1, 21):
        await platform.progress(asked, _called("react_to_message", f"c{number}"))
        await platform.progress(asked, _result("react_to_message", f"c{number}", '{"ok": true}'))
        await clock.advance(0.45)
    assert _methods(web) == ["chat_postMessage"]

    # Ten seconds after the post, one edit shows everything that happened meanwhile.
    await clock.advance(1.1)
    assert _methods(web) == ["chat_postMessage", "chat_update"]
    assert _flat(_tasks(_cards(web)[0])["Tools"]["details"]) == "react_to_message  :white_check_mark: 20  :zap: 1"

    # The next events wait for the next interval.
    await platform.progress(asked, _called("react_to_message", "c21"))
    await platform.progress(asked, _result("react_to_message", "c21", '{"ok": true}'))
    assert _methods(web) == ["chat_postMessage", "chat_update"]

    # The end of the turn is not held back by the interval.
    await platform.conclude(asked, MARK_DONE, "")
    assert _methods(web) == ["chat_postMessage", "chat_update", "chat_update"]


async def test_the_first_subagent_shows_at_once_and_one_that_joins_it_when_the_interval_ends(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()
    await platform.progress(asked, _called("bash", "c0"))
    await clock.advance(5)
    await platform.progress(asked, _called("subagent", "c1", **{SUBAGENT: "general"}))
    assert _methods(web) == ["chat_postMessage", "chat_update"]
    assert _cards(web)[0]["text"].startswith("Working... - 1 of 1 subagent running")

    await platform.progress(asked, _called("subagent", "c2", **{SUBAGENT: "general"}))
    assert _methods(web) == ["chat_postMessage", "chat_update"]
    await clock.advance(10)
    assert _methods(web) == ["chat_postMessage", "chat_update", "chat_update"]
    assert _cards(web)[0]["text"].startswith("Working... - 2 of 2 subagents running")


async def test_after_a_failed_edit_the_next_subagents_wait_for_the_interval(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()
    await platform.progress(asked, _called("bash", "c0"))
    await clock.advance(5)
    web.fail = lambda method, payload: "service_unavailable" if method == "chat_update" else None
    await platform.progress(asked, _called("subagent", "c1", **{SUBAGENT: "general"}))
    assert _methods(web) == ["chat_postMessage", "chat_update"]

    web.fail = None
    for number in range(2, 5):
        await platform.progress(asked, _called("subagent", f"c{number}", **{SUBAGENT: "general"}))
    assert _methods(web) == ["chat_postMessage", "chat_update"]
    await clock.advance(10)
    assert _methods(web) == ["chat_postMessage", "chat_update", "chat_update"]
    assert _cards(web)[0]["text"].startswith("Working... - 4 of 4 subagents running")


async def test_after_a_failed_edit_the_end_of_the_turn_waits_for_the_interval(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()
    await platform.progress(asked, _called("bash", "c0"))
    await clock.advance(5)
    web.fail = lambda method, payload: "service_unavailable" if method == "chat_update" else None
    await platform.progress(asked, _called("subagent", "c1", **{SUBAGENT: "general"}))
    assert _methods(web) == ["chat_postMessage", "chat_update"]

    web.fail = None
    await platform.conclude(asked, MARK_DONE, "")
    assert _methods(web) == ["chat_postMessage", "chat_update"]
    await clock.advance(10)
    assert _methods(web) == ["chat_postMessage", "chat_update", "chat_update"]
    assert not _cards(web)[0]["text"].startswith("Working...")


async def test_the_card_names_tools_as_given_counts_how_each_call_ended_and_lists_subagents_by_type(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()

    await platform.progress(asked, _called("post_message", "c1"))
    # A tool of the connector reports a refusal in its result. A backend reports a call that could not run.
    await platform.progress(asked, _result("post_message", "c1", '{"ok": false, "error": "tool_denied"}'))
    await platform.progress(asked, _called("shell", "c2"))
    await platform.progress(asked, _result("shell", "c2", "no such file", failed=True))
    await platform.progress(asked, _called("read_slack_conversation", "c3"))
    await platform.progress(asked, _result("read_slack_conversation", "c3", '{"ok": true, "messages": []}'))
    await platform.progress(asked, _called("subagent", "c4", **{SUBAGENT: "explore"}))
    await platform.progress(asked, _result("subagent", "c4", "<subagent>done</subagent>", **{SUBAGENT: "explore"}))
    await platform.progress(asked, _called("subagent", "c5", **{SUBAGENT: "general"}))
    await platform.progress(asked, _called("read_slack_conversation", "c6"))
    await clock.advance(5)

    (card,) = _cards(web)
    tasks = _tasks(card)
    assert set(tasks) == {"Subagents", "Tools"}
    assert _flat(tasks["Tools"]["details"]).split("\n") == [
        "post_message  :x: 1",
        "read_slack_conversation  :white_check_mark: 1  :zap: 1",
        "shell  :x: 1",
    ]
    assert _flat(tasks["Tools"]["output"]).startswith(":zap: read_slack_conversation")
    # A subagent is a line of its type and how its call ended. What it did inside is not on the card.
    assert _flat(tasks["Subagents"]["output"]).split("\n") == ["explore  :white_check_mark: 1", "general  :zap: 1"]
    assert "subagent" not in _flat(tasks["Tools"]["details"])
    assert card["text"].startswith("Working... - 1 of 2 subagents running - 1 of 4 tools running")


async def test_a_subagent_ends_on_its_own_result_and_not_on_a_tool_routed_from_its_child_session(connected, clock):
    platform, web, _ = await connected(CARD)
    asked = _asked()

    await platform.progress(asked, _called("transfer_task", "c1", **{SUBAGENT: "helper"}))
    await platform.progress(asked, _called("bash", "c1", child=True))
    await platform.progress(asked, _result("bash", "c1", "{}", child=True))
    await clock.advance(5)
    assert _flat(_tasks(_cards(web)[0])["Subagents"]["output"]) == "helper  :zap: 1"

    # The subagent tool's own result names no subagent here, as Docker Agent sends it.
    await platform.progress(asked, _result("transfer_task", "c1", "boom", failed=True))
    assert _flat(_tasks(_cards(web)[0])["Subagents"]["output"]) == "helper  :x: 1"


@pytest.mark.parametrize(
    ("state", "kept", "title"),
    [
        (MARK_DONE, True, "Turn finished"),
        (MARK_FAILED, True, "Turn failed"),
        # A turn that said nothing leaves nothing behind, as on the JiuwenSwarm channel.
        (ENDED_SILENT, False, ""),
        # A stopped turn is marked on its message. Its card goes.
        (MARK_STOPPED, False, ""),
    ],
)
async def test_a_card_is_settled_as_its_turn_ended_or_removed_where_nothing_is_left_to_say(connected, clock, state, kept, title):
    platform, web, _ = await connected(CARD)
    asked = _asked()
    await platform.progress(asked, _called("react_to_message", "c1"))
    await platform.progress(asked, _result("react_to_message", "c1", '{"ok": true}'))
    await clock.advance(5)
    (card,) = _cards(web)
    assert _stop_button(card) is not None

    assert await platform.conclude(asked, state) is False

    assert [bool(_cards(web)), _methods(web)[-1]] == [kept, "chat_update" if kept else "chat_delete"]
    if kept:
        (settled,) = _cards(web)
        assert settled["blocks"][0]["title"] == title
        # A turn that is over has nothing left to stop.
        assert _stop_button(settled) is None
        assert ("Harness error" in _tasks(settled)) == (state == MARK_FAILED)
    # Nothing of the turn is held once it has ended.
    assert platform._activity_records == {}


async def test_with_the_card_switched_off_no_tool_event_is_shown(build_world, clock):
    world = await build_world("platforms: {slack: {activity_card: false}}\n" + ANSWER_EVERYTHING)
    world.kit.play(ScriptedTurn(final="never said", calls=(("react_to_message", {"emoji": "eyes"}),), hold_before_final=True))
    await world.platform.app.deliver(_message("look"))
    await world.kit.holding()
    await clock.advance(60)

    assert _cards(world.platform.web) == [] and world.platform.progressed == []

    world.kit.release()
    await world.host.idle()


# ── the stop button on the card ──────────────────────────────────────────


async def _running_turn_with_a_card(build_world, clock, scopes: str = ANSWER_EVERYTHING):
    """A turn that has called a tool, has its card on screen and now waits before it writes its final."""
    world = await build_world(CARD + scopes)
    held = ScriptedTurn(final="said after the stop", calls=(("react_to_message", {"emoji": "eyes"}),), hold_before_final=True)
    world.kit.play(held)
    await world.platform.app.deliver(_message("do it"))
    await world.kit.holding()
    await clock.advance(5)
    (card,) = _cards(world.platform.web)
    return world, card


async def _press_stop(world, card: dict, *, user: str) -> str:
    body = click_body(user=user, channel=ROOM, message_ts=card["ts"], payload=card)
    return await world.platform.app.click(body, _stop_button(card))


def _told(web) -> list[tuple[str, str]]:
    return [(payload["user"], payload["text"]) for payload in web.payloads("chat_postEphemeral")]


async def test_the_stop_button_on_the_card_stops_the_turn_and_nothing_is_posted_after(build_world, clock):
    world, card = await _running_turn_with_a_card(build_world, clock)
    web = world.platform.web
    # The button names the message whose turn it stops, and the thread and conversation that turn is in.
    assert json.loads(_stop_button(card)["value"]) == {"session_id": TS, "request_id": TS, "channel_id": ROOM}

    outcome = await _press_stop(world, card, user=ASKER)
    await world.host.idle()

    assert outcome.startswith("stopped")
    assert _told(web) == [(ASKER, STOP_ACCEPTED_NOTICE)]
    # A stop is an absent side effect: the final the turn was about to write is on no screen.
    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert web.screen == {} and _methods(web)[-1] == "chat_delete"

    # Control: the conversation takes its next turn, and that one's final is posted.
    world.kit.play(ModelTurn(final="the control"))
    await world.platform.app.deliver(_message("do it now", ts="1710000002.000100"))
    await world.host.idle()
    assert web.shown(ROOM) == ["the control"]

    # The turn is over, so a second press of the same button stops nothing and says so.
    assert await _press_stop(world, card, user=ASKER) == "ignored:no-turn-running"
    assert _told(web)[-1] == (ASKER, STOP_STALE_NOTICE)


async def test_a_clicks_rule_decides_who_else_may_press_stop_and_the_starter_always_may(build_world, clock):
    rule = f"""
scopes:
  - match: {{channel: slack}}
    delivery: {{mode: [all]}}
    clicks: {{stop: {{user: [{LEAD}]}}}}
"""
    world, card = await _running_turn_with_a_card(build_world, clock, rule)
    web = world.platform.web

    # Seeing a card does not grant the right to stop its turn.
    assert (await _press_stop(world, card, user=BYSTANDER)).startswith("refused:clicks.stop")
    assert _told(web) == [(BYSTANDER, STOP_REFUSED_NOTICE)]
    assert world.outcomes() == [] and _stop_button(_cards(web)[0]) is not None

    # Control: whoever started the turn may stop it, whatever the rule says.
    assert (await _press_stop(world, card, user=ASKER)).startswith("stopped")
    await world.host.idle()
    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    assert _told(web)[-1] == (ASKER, STOP_ACCEPTED_NOTICE)


async def test_a_press_the_host_accepts_takes_the_button_off_the_card(connected, clock):
    platform, web, received = await connected(CARD, outcome="stopped session=slack:test")
    asked = _asked()
    await platform.progress(asked, _called("react_to_message", "c1"))
    await clock.advance(5)
    (card,) = _cards(web)

    body = click_body(user=BYSTANDER, channel=ROOM, message_ts=card["ts"], payload=card)
    await slack_app(platform).click(body, _stop_button(card))

    # The connector hands the host a request that names the message, and decides nothing itself.
    assert received == [InboundStop(platform="slack", workspace=TEAM, conversation=ROOM, message_id=TS, sender=BYSTANDER)]
    await clock.advance(10)
    await platform.progress(asked, _result("react_to_message", "c1", '{"ok": true}'))
    assert _stop_button(_cards(web)[0]) is None


def slack_app(platform):
    return platform._socket.app


# ── one turn, start to end ───────────────────────────────────────────────


async def test_one_turn_is_one_sequence_of_marks_card_reply_and_settling(build_world, clock):
    world = await build_world("platforms: {slack: {enable_streaming: true, activity_card: true, acknowledge_mode: reaction}}\n" + ANSWER_EVERYTHING)
    web = world.platform.web
    world.kit.play(
        ScriptedTurn(final="It is noon.\nThe ferry has left.\n", calls=(("react_to_message", {"emoji": "anchor"}),), hold_before_final=True)
    )

    await world.platform.app.deliver(_message("what time is it"))
    await world.kit.holding()
    await clock.advance(5)
    world.kit.release()
    await world.host.idle()

    seen = [
        (method, payload.get("name") or payload.get("status") or "")
        for method, payload in web.calls
        # Slack is asked whether it accepts the card before the card is sent. That call shows nothing.
        if method not in ("auth_test", "blocks.validate")
    ]
    assert seen == [
        # The message is acknowledged, and its thread says the app is at work.
        ("reactions_add", "eyes"),
        ("agents.sessions.setStatus", "processing"),
        # What the turn does. This reaction is the tool's own.
        ("reactions_add", "anchor"),
        # The turn is still running once the delay has passed: its card, with a stop button.
        ("chat_postMessage", ""),
        # The reply, as it is written, and closed.
        ("chat_startStream", ""),
        ("chat_appendStream", ""),
        ("chat_stopStream", ""),
        # The card is settled, the acknowledgement is removed, and the status is taken down.
        ("chat_update", ""),
        ("reactions_remove", "eyes"),
        ("agents.sessions.setStatus", "active"),
    ]
    card, reply = web.screen.values()
    assert card["blocks"][0]["title"] == "Turn finished"
    assert reply["text"].strip() == "It is noon.\nThe ferry has left."
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


@pytest.mark.parametrize("text", ["The ferry leaves", "x" * (MAX_SLACK_UPDATE_TEXT_LENGTH + 100)])
async def test_stopping_an_edited_fallback_marks_its_retained_preview(connected, text):
    platform, web, _ = await connected(STREAMING)
    asked = _asked(DIRECT, reply_thread="")
    await platform.progress(asked, _delta(text))
    await settle()

    assert await platform.conclude(asked, MARK_STOPPED) is False

    (shown,) = web.shown(DIRECT)
    assert shown.endswith(STREAM_STOPPED_TAIL)
    assert len(shown) <= MAX_SLACK_UPDATE_TEXT_LENGTH
    assert _methods(web) == ["chat_postMessage", "chat_update"]
    assert not platform._stream_state.streams


@pytest.mark.parametrize("state", [MARK_DONE, MARK_FAILED, ENDED_SILENT, MARK_STOPPED])
async def test_slack_cards_and_shared_delete_cards_have_the_same_end_and_cleanup_policy(connected, clock, state):
    from test_edit_surfaces import Settings, Transport, _posted

    from belle_agent.platforms.edit_surfaces import ActivityCards

    platform, web, _ = await connected(CARD)
    asked = _asked()
    transport = Transport("delete")
    cards = ActivityCards(transport, Settings())
    await _posted(cards, asked)
    await platform.progress(asked, _called("shell", "c1"))
    await clock.advance(5)

    await cards.end(asked, state)
    await platform.conclude(asked, state)

    removed = any(call[0] == "remove" for call in transport.calls)
    assert bool(_cards(web)) is not removed
    if not removed:
        assert transport.calls[-1][0] == "clear_stop"
        assert _stop_button(_cards(web)[0]) is None
    assert not cards.cards and not platform._activity_records


async def test_slack_and_shared_cleanup_cancel_cards_waiting_for_their_delay(connected, clock):
    from test_edit_surfaces import Settings, Transport

    from belle_agent.platforms.edit_surfaces import ActivityCards

    platform, web, _ = await connected(CARD)
    asked = _asked()
    transport = Transport("delete")
    cards = ActivityCards(transport, Settings(activity_card_delay_seconds=30))
    await cards.show(asked, _called("shell", "c1"))
    await platform.progress(asked, _called("shell", "c1"))
    timer = next(iter(cards.cards.values())).timer

    cards.close()
    platform._clear_turn_surfaces()
    await clock.advance(60)

    assert timer.cancelled()
    assert not cards.cards and not platform._activity_records
    assert not transport.calls and not _cards(web)
