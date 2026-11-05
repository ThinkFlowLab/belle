"""Shared lifecycle for editable replies and activity cards."""

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar

from belle_agent import progress
from belle_agent.backend import EVENT_STEP, EVENT_TOOL_CALLED, TurnEvent
from belle_agent.platforms.base import (
    ENDED_SILENT,
    MARK_DONE,
    MARK_FAILED,
    MARK_STOPPED,
    DeliveryUncertain,
    InboundMessage,
)
from belle_agent.streaming import SnapshotWriter, streamed_remainder

MAX_EDIT_SECONDS = 60.0


class EditRefused(Exception):
    """A message can no longer be edited."""


@dataclass
class EditedCard(progress.TurnProgress):
    """Shared state for one turn's activity card."""

    thread: str = ""
    workspace: str = ""
    message_id: str = ""
    timer: asyncio.Task[None] | None = None
    next_edit: float = 0.0
    closed: bool = False
    failed: bool = False
    stopped: bool = False


Card = TypeVar("Card", bound=EditedCard)


class CardSettings(Protocol):
    """Activity card timing settings."""

    @property
    def activity_card_delay_seconds(self) -> float:
        """Delay before posting a card."""
        ...

    @property
    def activity_card_min_edit_seconds(self) -> float:
        """Minimum interval between edits."""
        ...


class CardTransport(Protocol[Card]):
    """Platform operations for activity cards."""

    logger: logging.Logger
    log_name: str
    id_label: str
    errors: tuple[type[Exception], ...]
    #: Delete the cards of silent or stopped turns, and cards that held only the stop, or keep them settled.
    silent_card: Literal["delete", "keep"] | None

    def new_card(self, inbound: InboundMessage) -> Card:
        """Create an unposted card for ``inbound``."""
        ...

    def card_id(self, card: Card) -> str:
        """Return the posted message ID, or an empty string."""
        ...

    async def write_card(self, card: Card) -> float | None:
        """Post or edit the card; return a rate-limit delay if given."""
        ...

    async def show_stop(self, card: Card) -> None:
        """Offer a stop action when the card does not contain one."""
        ...

    async def clear_stop(self, card: Card) -> None:
        """Remove an added stop action from a settled card."""
        ...

    async def remove(self, conversation: str, message_id: str) -> None:
        """Remove one bot message."""
        ...


class ActivityCards(Generic[Card]):
    """Activity cards keyed by conversation and message."""

    def __init__(self, transport: CardTransport[Card], settings: CardSettings) -> None:
        self._transport = transport
        self._settings = settings
        self._cards: dict[tuple[str, str], Card] = {}
        self._lock = asyncio.Lock()

    @property
    def cards(self) -> Mapping[tuple[str, str], Card]:
        """Return active cards."""
        return self._cards

    async def begin(self, inbound: InboundMessage) -> None:
        """Start the card of a turn that has started, so that it shows its stop even if the turn reports nothing."""
        async with self._lock:
            key = (inbound.conversation, inbound.message_id)
            if key in self._cards:
                return
            card = self._cards[key] = self._transport.new_card(inbound)
            card.timer = asyncio.create_task(self._post_later(key), name=f"{self._transport.log_name}-activity-card")

    async def show(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Update a card with a tool event."""
        async with self._lock:
            await self._show(inbound, event)

    async def end(self, inbound: InboundMessage, state: str) -> None:
        """Settle or remove the card when its turn ends."""
        async with self._lock:
            await self._end(inbound, state)

    def close(self) -> None:
        """Cancel pending posts and forget all cards."""
        for card in self._cards.values():
            if card.timer is not None:
                card.timer.cancel()
        self._cards.clear()

    async def _show(self, inbound: InboundMessage, event: TurnEvent) -> None:
        if not event.data.get("call_id"):
            return
        key = (inbound.conversation, inbound.message_id)
        card = self._cards.get(key)
        if event.kind == EVENT_TOOL_CALLED:
            if card is None:
                card = self._transport.new_card(inbound)
                self._cards[key] = card
            card.closed = False
        if card is None or not progress.note_tool_event(card, event):
            return
        if not self._transport.card_id(card):
            if card.timer is None:
                card.timer = asyncio.create_task(
                    self._post_later(key), name=f"{self._transport.log_name}-activity-card"
                )
        elif time.monotonic() >= card.next_edit:
            await self._write(card)
        elif card.timer is None or card.timer.done():
            card.timer = asyncio.create_task(self._edit_later(card), name=f"{self._transport.log_name}-activity-card-edit")

    async def _post_later(self, key: tuple[str, str]) -> None:
        """Post a card if its turn is still running."""
        await asyncio.sleep(self._settings.activity_card_delay_seconds)
        async with self._lock:
            card = self._cards.get(key)
            if card is None or card.closed or self._transport.card_id(card):
                return
            card.timer = None
            try:
                await self._write(card)
            except self._transport.errors as exc:
                self._transport.logger.warning("%s could not post a turn's activity card: %s", self._transport.log_name, exc)

    async def _edit_later(self, card: Card) -> None:
        """Write the held edit when the edit interval ends."""
        await asyncio.sleep(max(0.0, card.next_edit - time.monotonic()))
        async with self._lock:
            if card.closed:
                return
            card.timer = None
            try:
                await self._write(card)
            except self._transport.errors as exc:
                self._transport.logger.warning("%s could not edit a turn's activity card: %s", self._transport.log_name, exc)

    async def _write(self, card: Card) -> None:
        """Write a card and schedule its next edit."""
        if card.timer is not None:
            # This write shows the latest state that a held edit would show.
            card.timer.cancel()
            card.timer = None
        retry_after = await self._transport.write_card(card)
        card.next_edit = time.monotonic() + max(self._settings.activity_card_min_edit_seconds, retry_after or 0)
        await self._transport.show_stop(card)

    async def _end(self, inbound: InboundMessage, state: str) -> None:
        card = self._cards.pop((inbound.conversation, inbound.message_id), None)
        if card is None:
            return
        if card.timer is not None:
            card.timer.cancel()
        card.settle_progress()
        card.closed, card.failed, card.stopped = True, state == MARK_FAILED, state == MARK_STOPPED
        if not (card_id := self._transport.card_id(card)):
            return
        if self._transport.silent_card == "delete" and progress.card_discard_reason(
            silent=state == ENDED_SILENT, stopped=card.stopped, empty=card.shows_nothing(),
        ):
            try:
                await self._transport.remove(inbound.conversation, card_id)
                return
            except self._transport.errors as exc:
                self._transport.logger.warning(
                    "%s activity card could not be taken down: %s=%s %s",
                    self._transport.log_name, self._transport.id_label, card_id, exc,
                )
        await self._write(card)
        await self._transport.clear_stop(card)


class ReplyTransport(Protocol):
    """Platform operations for editable replies."""

    logger: logging.Logger
    log_name: str
    id_label: str
    errors: tuple[type[Exception], ...]
    #: Thread later parts under the first when the reply has no thread.
    reply_parts_in_thread: bool | None

    async def reply_budget(self, conversation: str) -> int:
        """Return one message's size limit in the renderer's unit."""
        ...

    def cut_index(self, text: str, budget: int) -> int | None:
        """Return a cut index, or ``None`` if the text fits."""
        ...

    async def send_reply(self, conversation: str, thread: str, text: str, *, final: bool) -> str:
        """Post a reply part and return its message ID."""
        ...

    async def edit_reply(self, conversation: str, message_id: str, thread: str, text: str, *, final: bool) -> None:
        """Edit a reply part; raise ``EditRefused`` if it is too old."""
        ...

    def retry_after(self, exc: Exception) -> float | None:
        """Return a rate-limit delay, if present."""
        ...

    async def remove(self, conversation: str, message_id: str) -> None:
        """Remove one bot message."""
        ...


class EditedReply:
    """An editable reply; ``cut`` counts text in earlier messages."""

    def __init__(
        self, transport: ReplyTransport, budget: int, conversation: str, thread: str, *, interval: float, max_interval: float
    ) -> None:
        self._transport = transport
        self._budget = budget
        self.conversation = conversation
        self.thread = thread
        self.text = ""
        self.ids: list[str] = []
        self.cut = 0
        self._open = False
        self._covers = 0
        self.interval = interval
        self._max_interval = max_interval
        self._next = 0.0
        self.writer: SnapshotWriter | None = None
        self.uncertain: DeliveryUncertain | None = None

    def _finish(self) -> None:
        self.cut += self._covers
        self._open, self._covers = False, 0

    async def _show(self, text: str, covers: int, final: bool) -> None:
        """Show text in the last message or start another."""
        transport = self._transport
        if self._open:
            try:
                await transport.edit_reply(self.conversation, self.ids[-1], self.thread, text, final=final)
            except EditRefused:
                transport.logger.info(
                    "%s reply edit refused as too late, going on in a new post: %s=%s",
                    transport.log_name, transport.id_label, self.ids[-1],
                )
                self._finish()
                raise
        else:
            self.ids.append(await transport.send_reply(self.conversation, self.thread, text, final=final))
            if transport.reply_parts_in_thread:
                self.thread = self.thread or self.ids[0]
            self._open = True
        self._covers = covers

    async def show(self, text: str, *, final: bool) -> None:
        """Show the reply so far, split to fit the message budget."""
        while body := (live := text[self.cut :]).strip():
            index = self._transport.cut_index(body, self._budget)
            try:
                if index is None:
                    await self._show(body, len(live), final)
                    return
                index += len(live) - len(live.lstrip())
                await self._show(live[:index].strip(), index, final)
            except EditRefused:
                continue
            self._finish()

    async def write(self, _handle: str, text: str, _attempt: int) -> None:
        """Write after the edit interval; back off on rate limits."""
        if self.uncertain is not None:
            return
        transport = self._transport
        await asyncio.sleep(max(0.0, self._next - time.monotonic()))
        try:
            await self.show(text, final=False)
        except DeliveryUncertain as exc:
            self.uncertain = exc
            transport.logger.warning("%s reply delivery is uncertain: %s", transport.log_name, exc)
        except transport.errors as exc:
            if (wait := transport.retry_after(exc)) is None:
                transport.logger.warning("%s a reply shown as written was not updated: %s", transport.log_name, exc)
            else:
                self.interval = min(self._max_interval, max(self.interval * 2, wait))
                transport.logger.info(
                    "%s rate limit: a reply shown as written is edited every %.1f s now", transport.log_name, self.interval
                )
        self._next = time.monotonic() + self.interval


class EditedReplies:
    """Editable replies keyed by conversation and message."""

    def __init__(
        self,
        transport: ReplyTransport,
        *,
        edit_seconds: float,
        stopped_tail: str,
        max_edit_seconds: float = MAX_EDIT_SECONDS,
    ) -> None:
        self._transport = transport
        self._edit_seconds = edit_seconds
        self._max_edit_seconds = max_edit_seconds
        self._stopped_tail = stopped_tail
        self.replies: dict[tuple[str, str], EditedReply] = {}

    async def show(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Append a turn's text or start its next step."""
        key = (inbound.conversation, inbound.message_id)
        reply = self.replies.get(key)
        if event.kind == EVENT_STEP:
            if reply is not None and reply.text.strip() and not event.data.get("continues"):
                reply.text = reply.text.rstrip() + "\n\n"
            return
        if reply is None:
            if not event.text.strip():
                return
            reply = EditedReply(
                self._transport, await self._transport.reply_budget(inbound.conversation), inbound.conversation,
                inbound.reply_thread or "", interval=self._edit_seconds, max_interval=self._max_edit_seconds,
            )
            self.replies[key] = reply
            reply.writer = SnapshotWriter(reply, "")
        reply.text += event.text
        assert reply.writer is not None
        reply.writer.replace(reply.text)

    async def end(self, inbound: InboundMessage, state: str, final: str) -> bool:
        """Finalize a reply; return whether the streamed text became ``final``."""
        transport = self._transport
        reply = self.replies.pop((inbound.conversation, inbound.message_id), None)
        if reply is None or reply.writer is None:
            return False
        await reply.writer.stop()
        if reply.uncertain is not None:
            raise reply.uncertain
        if not reply.ids:
            return False
        if state == MARK_STOPPED:
            try:
                await reply.show(reply.text.rstrip() + self._stopped_tail, final=True)
            except transport.errors as exc:
                transport.logger.warning("%s a stopped reply was not marked as stopped: %s", transport.log_name, exc)
            return False
        rest = streamed_remainder(reply.text[: reply.cut], final.strip()) if state == MARK_DONE and final.strip() else None
        if rest is not None:
            try:
                await reply.show(reply.text[: reply.cut] + rest, final=True)
                return True
            except transport.errors as exc:
                transport.logger.warning(
                    "%s a reply shown as written could not end as its final: %s", transport.log_name, exc
                )
        # Remove previews that cannot become the final reply.
        for message_id in reply.ids:
            try:
                await transport.remove(reply.conversation, message_id)
            except transport.errors as exc:
                transport.logger.warning(
                    "%s a reply shown as written could not be taken down: %s=%s %s",
                    transport.log_name, transport.id_label, message_id, exc,
                )
        return False

    def close(self) -> None:
        """Cancel pending writes and forget all replies."""
        for reply in self.replies.values():
            if reply.writer is not None:
                reply.writer.cancel()
        self.replies.clear()
