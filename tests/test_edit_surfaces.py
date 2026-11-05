import asyncio
import logging
from dataclasses import dataclass

import pytest

from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent
from belle_agent.platforms import edit_surfaces
from belle_agent.platforms.base import ENDED_SILENT, MARK_DONE, MARK_STOPPED, InboundMessage
from belle_agent.platforms.edit_surfaces import ActivityCards, EditedCard


class Refused(Exception):
    pass


@dataclass
class Card(EditedCard):
    conversation: str = ""
    card_id: str = ""


@dataclass
class Settings:
    activity_card_delay_seconds: float = 0.0
    activity_card_min_edit_seconds: float = 0.0


class Transport:
    logger = logging.getLogger("test.edit_surfaces")
    log_name = "fake"
    id_label = "post"
    errors = (Refused,)

    def __init__(self, silent_card="delete"):
        self.silent_card = silent_card
        self.calls = []
        self.retry_after = None
        self.refuse = set()
        self.posted = 0

    def new_card(self, inbound):
        return Card(conversation=inbound.conversation, message_id=inbound.message_id, workspace=inbound.workspace)

    def card_id(self, card):
        return card.card_id

    async def write_card(self, card):
        if "write" in self.refuse:
            raise Refused("no")
        if card.card_id:
            self.calls.append(("edit", card.card_id, card.closed))
        else:
            self.posted += 1
            card.card_id = f"P{self.posted}"
            self.calls.append(("post", card.card_id, card.closed))
        return self.retry_after

    async def show_stop(self, card):
        self.calls.append(("show_stop", card.card_id, card.closed))

    async def clear_stop(self, card):
        self.calls.append(("clear_stop", card.card_id))

    async def remove(self, conversation, message_id):
        if "remove" in self.refuse:
            raise Refused("gone")
        self.calls.append(("remove", conversation, message_id))


def _inbound(message_id="M1"):
    return InboundMessage(platform="fake", workspace="W1", conversation="C1", message_id=message_id, sender="U1", text="hi")


def _called(call_id="c1"):
    return TurnEvent(EVENT_TOOL_CALLED, data={"call_id": call_id, "name": "search"})


def _result(call_id="c1"):
    return TurnEvent(EVENT_TOOL_RESULT, text="ok", data={"call_id": call_id})


async def _posted(cards, inbound):
    await cards.show(inbound, _called())
    card = cards.cards[(inbound.conversation, inbound.message_id)]
    assert card.timer is not None
    await card.timer
    return card


async def test_a_card_is_posted_once_the_delay_has_passed_and_its_stop_offered():
    transport = Transport()
    cards = ActivityCards(transport, Settings())

    card = await _posted(cards, _inbound())

    assert transport.calls == [("post", "P1", False), ("show_stop", "P1", False)]
    assert card.timer is None


async def test_an_event_with_no_call_id_shows_nothing():
    transport = Transport()
    cards = ActivityCards(transport, Settings())

    await cards.show(_inbound(), TurnEvent(EVENT_TOOL_CALLED, data={"name": "search"}))

    assert cards.cards == {}


async def test_a_card_whose_turn_ends_before_the_delay_is_never_posted():
    transport = Transport()
    cards = ActivityCards(transport, Settings(activity_card_delay_seconds=30))

    await cards.show(_inbound(), _called())
    timer = cards.cards[("C1", "M1")].timer
    await cards.end(_inbound(), MARK_DONE)
    await asyncio.sleep(0)

    assert timer is not None and timer.cancelled()
    assert transport.calls == []


async def test_an_edit_waits_for_the_settings_interval_and_is_written_when_it_ends():
    transport = Transport()
    cards = ActivityCards(transport, Settings(activity_card_min_edit_seconds=0.5))
    card = await _posted(cards, _inbound())

    await cards.show(_inbound(), _result())
    assert [call[0] for call in transport.calls] == ["post", "show_stop"]

    assert card.timer is not None
    await card.timer
    assert transport.calls[-2:] == [("edit", "P1", False), ("show_stop", "P1", False)]


async def test_a_rate_limit_longer_than_the_interval_spaces_the_next_edit(monkeypatch):
    transport = Transport()
    transport.retry_after = 90.0
    cards = ActivityCards(transport, Settings(activity_card_min_edit_seconds=1))
    card = await _posted(cards, _inbound())

    now = edit_surfaces.time.monotonic()
    assert 89 < card.next_edit - now <= 90
    monkeypatch.setattr(edit_surfaces.time, "monotonic", lambda: now + 30)
    await cards.show(_inbound(), _result())
    assert [call[0] for call in transport.calls] == ["post", "show_stop"]
    monkeypatch.setattr(edit_surfaces.time, "monotonic", lambda: now + 91)
    await cards.show(_inbound(), _called("c2"))
    assert transport.calls[-2:] == [("edit", "P1", False), ("show_stop", "P1", False)]


async def test_active_cards_remain_until_their_turns_end():
    transport = Transport()
    cards = ActivityCards(transport, Settings(activity_card_delay_seconds=30))

    for message_id in (f"M{index}" for index in range(33)):
        await cards.show(_inbound(message_id), _called())

    assert len(cards.cards) == 33
    await cards.end(_inbound("M1"), MARK_DONE)
    assert len(cards.cards) == 32
    cards.close()


@pytest.mark.parametrize("state", [ENDED_SILENT, MARK_STOPPED])
async def test_where_cards_are_deleted_a_silent_or_stopped_turn_takes_its_card_down(state):
    transport = Transport("delete")
    cards = ActivityCards(transport, Settings())
    await _posted(cards, _inbound())

    await cards.end(_inbound(), state)

    assert transport.calls[2:] == [("remove", "C1", "P1")]
    assert cards.cards == {}


async def test_a_card_that_cannot_be_taken_down_is_settled_instead(caplog):
    transport = Transport("delete")
    transport.refuse.add("remove")
    cards = ActivityCards(transport, Settings())
    await _posted(cards, _inbound())

    with caplog.at_level(logging.WARNING, logger="test.edit_surfaces"):
        await cards.end(_inbound(), ENDED_SILENT)

    assert transport.calls[2:] == [("edit", "P1", True), ("show_stop", "P1", True), ("clear_stop", "P1")]
    assert caplog.messages == ["fake activity card could not be taken down: post=P1 gone"]


@pytest.mark.parametrize("state", [ENDED_SILENT, MARK_STOPPED, MARK_DONE])
async def test_where_cards_are_kept_every_ending_settles_the_card_and_clears_its_stop(state):
    transport = Transport("keep")
    cards = ActivityCards(transport, Settings())
    card = await _posted(cards, _inbound())

    await cards.end(_inbound(), state)

    assert transport.calls[2:] == [("edit", "P1", True), ("show_stop", "P1", True), ("clear_stop", "P1")]
    assert (card.closed, card.stopped) == (True, state == MARK_STOPPED)


async def test_a_done_turn_settles_its_card_where_cards_are_deleted_too():
    transport = Transport("delete")
    cards = ActivityCards(transport, Settings())
    await _posted(cards, _inbound())

    await cards.end(_inbound(), MARK_DONE)

    assert transport.calls[2:] == [("edit", "P1", True), ("show_stop", "P1", True), ("clear_stop", "P1")]


async def test_a_begun_turn_that_calls_no_tool_still_gets_its_card_and_stop_after_the_delay():
    transport = Transport()
    cards = ActivityCards(transport, Settings())

    await cards.begin(_inbound())
    card = cards.cards[("C1", "M1")]
    assert card.timer is not None
    await card.timer

    assert transport.calls == [("post", "P1", False), ("show_stop", "P1", False)]
    assert card.shows_nothing()


async def test_a_card_that_held_only_its_stop_comes_down_when_its_turn_is_done():
    transport = Transport("delete")
    cards = ActivityCards(transport, Settings())
    await cards.begin(_inbound())
    await cards.cards[("C1", "M1")].timer

    await cards.end(_inbound(), MARK_DONE)

    assert transport.calls[2:] == [("remove", "C1", "P1")]


async def test_a_begun_card_that_then_shows_a_tool_is_posted_once_and_settled_at_the_end():
    transport = Transport("delete")
    cards = ActivityCards(transport, Settings(activity_card_delay_seconds=0.05))
    await cards.begin(_inbound())
    timer = cards.cards[("C1", "M1")].timer

    await cards.show(_inbound(), _called())
    assert cards.cards[("C1", "M1")].timer is timer
    await timer
    await cards.end(_inbound(), MARK_DONE)

    assert [call[0] for call in transport.calls] == ["post", "show_stop", "edit", "show_stop", "clear_stop"]


async def test_begin_leaves_a_card_already_started_as_it_is():
    transport = Transport()
    cards = ActivityCards(transport, Settings(activity_card_delay_seconds=30))
    await cards.show(_inbound(), _called())
    card = cards.cards[("C1", "M1")]

    await cards.begin(_inbound())

    assert cards.cards[("C1", "M1")] is card and len(card.tool_runs) == 1
    cards.close()


async def test_a_begun_turn_that_ends_before_the_delay_posts_nothing():
    transport = Transport()
    cards = ActivityCards(transport, Settings(activity_card_delay_seconds=30))
    await cards.begin(_inbound())
    timer = cards.cards[("C1", "M1")].timer

    await cards.end(_inbound(), MARK_DONE)
    await asyncio.sleep(0)

    assert timer is not None and timer.cancelled()
    assert transport.calls == []


async def test_a_post_the_transport_refuses_is_logged_and_tried_again_on_the_next_event(caplog):
    transport = Transport()
    transport.refuse.add("write")
    cards = ActivityCards(transport, Settings())

    with caplog.at_level(logging.WARNING, logger="test.edit_surfaces"):
        card = await _posted(cards, _inbound())

    assert caplog.messages == ["fake could not post a turn's activity card: no"]
    assert (card.card_id, card.timer, transport.calls) == ("", None, [])
    transport.refuse.clear()
    await cards.show(_inbound(), _result())
    assert card.timer is not None
    await card.timer
    assert transport.calls == [("post", "P1", False), ("show_stop", "P1", False)]


async def test_close_forgets_every_card_and_cancels_what_waits_to_post():
    transport = Transport()
    cards = ActivityCards(transport, Settings(activity_card_delay_seconds=30))
    await cards.show(_inbound(), _called())
    timer = cards.cards[("C1", "M1")].timer

    cards.close()
    await asyncio.sleep(0)

    assert cards.cards == {} and timer is not None and timer.cancelled()
    assert transport.calls == []


class TooLate(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, wait):
        super().__init__(f"wait {wait}")
        self.wait = wait


class ReplyTransport:
    logger = logging.getLogger("test.edit_surfaces")
    log_name = "fake"
    id_label = "post"
    errors = (Refused, RateLimited)

    def __init__(self, budget=0, parts_in_thread=True):
        self.budget = budget
        self.reply_parts_in_thread = parts_in_thread
        self.shown = {}
        self.calls = []
        self.too_late = set()
        self.edit_errors = []

    async def reply_budget(self, conversation):
        return self.budget

    def cut_index(self, text, budget):
        if not budget or len(text) <= budget:
            return None
        return text.rfind(" ", 0, budget + 1) + 1 or budget

    async def send_reply(self, conversation, thread, text, *, final):
        message_id = f"P{len(self.shown) + 1}"
        self.shown[message_id] = text
        self.calls.append(("send", message_id, thread, text, final))
        return message_id

    async def edit_reply(self, conversation, message_id, thread, text, *, final):
        if message_id in self.too_late:
            raise edit_surfaces.EditRefused("too late")
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.shown[message_id] = text
        self.calls.append(("edit", message_id, thread, text, final))

    def retry_after(self, exc):
        return exc.wait if isinstance(exc, RateLimited) else None

    async def remove(self, conversation, message_id):
        self.shown.pop(message_id)
        self.calls.append(("remove", message_id))


def _reply(transport, thread="", interval=1.0, max_interval=60.0):
    return edit_surfaces.EditedReply(transport, transport.budget, "C1", thread, interval=interval, max_interval=max_interval)


async def test_a_reply_longer_than_the_budget_goes_on_in_new_messages_in_the_thread_of_the_first():
    transport = ReplyTransport(budget=10)
    reply = _reply(transport)

    await reply.show("one two three four five", final=True)

    assert list(transport.shown.values()) == ["one two", "three four", "five"]
    assert [call[2] for call in transport.calls] == ["", "P1", "P1"]


async def test_where_parts_stay_where_the_reply_goes_every_part_has_its_thread():
    transport = ReplyTransport(budget=10, parts_in_thread=False)
    reply = _reply(transport)

    await reply.show("one two three four five", final=True)

    assert [call[2] for call in transport.calls] == ["", "", ""]


async def test_the_last_message_is_edited_as_the_reply_grows_and_earlier_ones_are_left_as_they_are():
    transport = ReplyTransport(budget=10)
    reply = _reply(transport)

    await reply.show("one two", final=False)
    await reply.show("one two three", final=False)
    await reply.show("one two three four", final=True)

    assert transport.shown == {"P1": "one two", "P2": "three four"}
    assert [(call[0], call[1], call[4]) for call in transport.calls] == [
        ("send", "P1", False), ("edit", "P1", False), ("send", "P2", False), ("edit", "P2", True)
    ]


async def test_an_edit_refused_as_too_late_keeps_what_its_message_shows_and_goes_on_below(caplog):
    transport = ReplyTransport()
    reply = _reply(transport)
    await reply.show("first", final=False)
    transport.too_late.add("P1")

    with caplog.at_level(logging.INFO, logger="test.edit_surfaces"):
        await reply.show("first and more", final=False)

    assert transport.shown == {"P1": "first", "P2": "and more"}
    assert reply.cut == len("first")
    assert "fake reply edit refused as too late, going on in a new post: post=P1" in caplog.messages


async def test_a_rate_limit_lengthens_the_interval_to_the_platform_s_wait_or_twice_and_no_further(caplog):
    transport = ReplyTransport()
    reply = _reply(transport, interval=1.0, max_interval=5.0)
    await reply.write("", "first", 0)

    transport.edit_errors = [RateLimited(0.5)]
    with caplog.at_level(logging.INFO, logger="test.edit_surfaces"):
        reply._next = 0.0
        await reply.write("", "first second", 0)
    assert reply.interval == 2.0
    assert "fake rate limit: a reply shown as written is edited every 2.0 s now" in caplog.messages

    for wait, interval in ((3.0, 4.0), (30.0, 5.0)):
        transport.edit_errors = [RateLimited(wait)]
        reply._next = 0.0
        await reply.write("", "first second", 0)
        assert reply.interval == interval
    assert transport.shown == {"P1": "first"}


async def test_any_other_failure_of_a_write_is_logged_and_leaves_the_interval(caplog):
    transport = ReplyTransport()
    reply = _reply(transport)
    await reply.write("", "first", 0)
    transport.edit_errors = [Refused("no")]

    with caplog.at_level(logging.WARNING, logger="test.edit_surfaces"):
        reply._next = 0.0
        await reply.write("", "first second", 0)

    assert reply.interval == 1.0
    assert caplog.messages == ["fake a reply shown as written was not updated: no"]


async def _streamed(replies, *deltas):
    for delta in deltas:
        await replies.show(_inbound(), TurnEvent(EVENT_TEXT_DELTA, text=delta))
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_stopped_reply_keeps_what_it_wrote_marked_as_stopped():
    transport = ReplyTransport()
    replies = edit_surfaces.EditedReplies(transport, edit_seconds=0.0, stopped_tail="\n\n_stopped_")
    await _streamed(replies, "half ", "done")

    assert await replies.end(_inbound(), MARK_STOPPED, "") is False
    assert transport.shown == {"P1": "half done\n\n_stopped_"}
    assert transport.calls[-1][4] is True
    assert replies.replies == {}


async def test_a_done_reply_becomes_its_final_and_one_that_cannot_comes_down():
    transport = ReplyTransport()
    replies = edit_surfaces.EditedReplies(transport, edit_seconds=0.0, stopped_tail="")
    await _streamed(replies, "Hello")
    assert await replies.end(_inbound(), MARK_DONE, "Hello world") is True
    assert transport.shown == {"P1": "Hello world"}

    transport = ReplyTransport(budget=10)
    replies = edit_surfaces.EditedReplies(transport, edit_seconds=0.0, stopped_tail="")
    await _streamed(replies, "Something else entirely")
    assert len(transport.shown) == 3
    assert await replies.end(_inbound(), MARK_DONE, "Hello world") is False
    assert transport.shown == {} and [call for call in transport.calls if call[0] == "remove"] == [
        ("remove", "P1"), ("remove", "P2"), ("remove", "P3")
    ]


async def test_a_new_step_starts_a_paragraph_and_a_step_that_continues_the_text_does_not():
    transport = ReplyTransport()
    replies = edit_surfaces.EditedReplies(transport, edit_seconds=0.0, stopped_tail="")
    for event in (
        TurnEvent(EVENT_TEXT_DELTA, text="The quick br"),
        TurnEvent(EVENT_STEP, data={"continues": True}),
        TurnEvent(EVENT_TEXT_DELTA, text="own fox."),
        TurnEvent(EVENT_STEP),
        TurnEvent(EVENT_TEXT_DELTA, text="Done."),
    ):
        await replies.show(_inbound(), event)
    for _ in range(3):
        await asyncio.sleep(0)

    assert transport.shown == {"P1": "The quick brown fox.\n\nDone."}
