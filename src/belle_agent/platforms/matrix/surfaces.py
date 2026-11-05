"""Show streamed replies and activity cards as editable Matrix notices.

An edit includes text twice, so replies continue in a new notice after 30,000
bytes, or 22,000 in encrypted rooms. Belle sends one edit per interval. When
matrix-sdk retries a rate-limited edit, that edit waits. If the helper returns
the rate limit instead, Belle increases the interval and skips that edit.
Final replies and cards retry after rate limits. A person's ⏹️ reaction stops
the turn.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from belle_agent import progress
from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent
from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.edit_surfaces import ActivityCards, EditedCard, EditedReplies
from belle_agent.platforms.matrix import protocol, render
from belle_agent.platforms.matrix.affordances import MATRIX_AFFORDANCES
from belle_agent.platforms.matrix.helper import HelperError
from belle_agent.platforms.matrix.settings import MatrixSettings

logger = logging.getLogger(__name__)

STREAMING_SETTING = "platforms.matrix.enable_streaming"
ACTIVITY_SETTING = "platforms.matrix.activity_card"
#: Minimum seconds between streamed edits.
STREAM_EDIT_SECONDS = 1.0
#: Maximum seconds between edits after rate limits.
MAX_EDIT_SECONDS = 60.0
STOPPED_TAIL = f"\n\n_{progress.TITLE_STOPPED}_"
STOP_KEY = "\N{BLACK SQUARE FOR STOP}"
#: Match Element's picker form so other users' stop reactions join it.
STOP_REACTION = f"{STOP_KEY}\N{VARIATION SELECTOR-16}"
STOP_HINT = f"_React with {STOP_REACTION} to stop._"
_RUN_EMOJI = {
    progress.RUN_RUNNING: "\N{HIGH VOLTAGE SIGN}",
    progress.RUN_DONE: "\N{WHITE HEAVY CHECK MARK}",
    progress.RUN_FAILED: "\N{CROSS MARK}",
    progress.RUN_UNREPORTED: "\N{WHITE QUESTION MARK ORNAMENT}",
}


class Account(Protocol):
    """What the surfaces need of the connector."""

    async def call(self, op: str, **fields: Any) -> dict[str, Any]:
        """One request of the helper. Raise ``HelperError`` where it refuses."""
        ...

    async def send_event(self, conversation: str, event_type: str, content: Mapping[str, Any], *, tries: int = ...) -> str:
        """Send one event, tried ``tries`` times in all after a rate limit, by default as often as any send. Return its id."""
        ...

    async def redact(self, conversation: str, event_id: str) -> None:
        """Remove one event of the bot's."""
        ...

    async def react(self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = "") -> None:
        """Add or remove one reaction of the bot's."""
        ...


@dataclass
class _Card(EditedCard):
    """The activity card of one turn."""

    room: str = ""
    event_id: str = ""
    #: The bot's stop reaction is on the card.
    stop_shown: bool = False


class TurnSurfaces:
    """The reply and the card of each running turn of one connector."""

    logger: logging.Logger = logging.getLogger(__name__)
    log_name = "matrix"
    id_label = "event"
    errors: tuple[type[Exception], ...] = (HelperError,)
    reply_parts_in_thread = MATRIX_AFFORDANCES.reply_parts_in_thread
    silent_card = MATRIX_AFFORDANCES.silent_card

    def __init__(self, account: Account, settings: MatrixSettings) -> None:
        self._account = account
        self._settings = settings
        self.replies = EditedReplies(
            self, edit_seconds=STREAM_EDIT_SECONDS, stopped_tail=STOPPED_TAIL, max_edit_seconds=MAX_EDIT_SECONDS
        )
        self._replies = self.replies.replies
        self.cards = ActivityCards(self, settings)

    @property
    def shows(self) -> Mapping[str, str]:
        """Each kind of event a turn shows as configured, with its setting."""
        shown: dict[str, str] = {}
        if self._settings.enable_streaming:
            shown.update({EVENT_TEXT_DELTA: STREAMING_SETTING, EVENT_STEP: STREAMING_SETTING})
        if self._settings.activity_card:
            shown.update({EVENT_TOOL_CALLED: ACTIVITY_SETTING, EVENT_TOOL_RESULT: ACTIVITY_SETTING})
        return shown

    def card_of(self, event_id: str) -> tuple[str, str] | None:
        """The room and the message of the turn whose open card is the event ``event_id``, or ``None``."""
        card = next((card for card in self.cards.cards.values() if card.event_id == event_id and not card.closed), None)
        return None if card is None else (card.room, card.message_id)

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Show one event of a running turn."""
        if event.kind in (EVENT_TEXT_DELTA, EVENT_STEP):
            await self.replies.show(inbound, event)
        elif event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT):
            await self.cards.show(inbound, event)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End the reply that was shown, then settle the card. Return whether the reply became ``final``."""
        try:
            return await self.replies.end(inbound, state, final)
        finally:
            await self.cards.end(inbound, state)

    async def close(self) -> None:
        """Forget every turn, and stop what still waits to write."""
        self.replies.close()
        self.cards.close()

    # ── the reply ────────────────────────────────────────────────────────

    async def reply_budget(self, conversation: str) -> int:
        """Return the room's streamed byte budget."""
        info = await self._account.call("room_info", room=conversation)
        return render.STREAM_BUDGET[bool(info.get("encrypted", True))]

    def cut_index(self, text: str, budget: int) -> int | None:
        """Return a cut index, or ``None`` when the text fits."""
        return None if render.RENDERER.measure(text) <= budget else render.RENDERER.cut_index(text, budget)

    async def send_reply(self, conversation: str, thread: str, text: str, *, final: bool) -> str:
        """Send a notice; nonfinal writes are tried once."""
        tries = {} if final else {"tries": 1}
        return await self._account.send_event(conversation, "m.room.message", render.notice(text, thread), **tries)

    async def edit_reply(self, conversation: str, message_id: str, thread: str, text: str, *, final: bool) -> None:
        """Edit a notice; nonfinal edits are tried once."""
        new = {key: value for key, value in render.notice(text, thread).items() if key != "m.relates_to"}
        tries = {} if final else {"tries": 1}
        await self._account.send_event(conversation, "m.room.message", render.edit(message_id, new), **tries)

    def retry_after(self, exc: Exception) -> float | None:
        """Return the helper's rate-limit delay, if any."""
        if isinstance(exc, HelperError) and exc.code == protocol.RATE_LIMITED:
            return (exc.retry_after_ms or 0) / 1000
        return None

    # ── the card ─────────────────────────────────────────────────────────

    def new_card(self, inbound: InboundMessage) -> _Card:
        """Create an unposted card in the inbound thread."""
        return _Card(
            room=inbound.conversation, thread=inbound.reply_thread or "",
            workspace=inbound.workspace, message_id=inbound.message_id,
        )

    def card_id(self, card: _Card) -> str:
        """Return the posted event ID, if any."""
        return card.event_id

    @staticmethod
    def _text(card: _Card) -> str:
        """The card: its title, how its runs stand, a line for each run and, while the turn runs, how to stop it."""
        title = progress.TITLE_STOPPED if card.stopped else progress.turn_title(card, closed=card.closed, failed=card.failed)
        lines = [f"**{title}**", progress.card_summary(card), *progress.run_lines(card, _RUN_EMOJI)]
        if not card.closed:
            lines.append(STOP_HINT)
        return "\n\n".join(line for line in lines if line)

    async def write_card(self, card: _Card) -> float | None:
        """Post or edit a card; retry initial and final writes after rate limits."""
        notice = render.notice(self._text(card), card.thread)
        tries = {"tries": 1} if card.event_id and not card.closed else {}
        try:
            if card.event_id:
                new = {key: value for key, value in notice.items() if key != "m.relates_to"}
                await self._account.send_event(card.room, "m.room.message", render.edit(card.event_id, new), **tries)
            else:
                card.event_id = await self._account.send_event(card.room, "m.room.message", notice, **tries)
                logger.info("matrix activity card: posted room=%s event=%s", card.room, card.event_id)
        except HelperError as exc:
            logger.warning("matrix activity card not written, left as it is: event=%s %s", card.event_id or "-", exc)
            if exc.code == protocol.RATE_LIMITED:
                return (exc.retry_after_ms or 0) / 1000
        return None

    async def show_stop(self, card: _Card) -> None:
        """Add a stop reaction to an active card once."""
        if card.event_id and not card.closed and not card.stop_shown:
            try:
                await self._account.react(card.room, card.event_id, STOP_REACTION)
                card.stop_shown = True
            except HelperError as exc:
                logger.warning("matrix activity card has no stop reaction: event=%s %s", card.event_id, exc)

    async def clear_stop(self, card: _Card) -> None:
        """Remove the stop reaction from a settled card."""
        if card.stop_shown:
            try:
                await self._account.react(card.room, card.event_id, STOP_REACTION, remove=True)
            except HelperError as exc:
                logger.warning("matrix the stop reaction of a card could not be taken off: event=%s %s", card.event_id, exc)

    async def remove(self, conversation: str, message_id: str) -> None:
        """Redact a bot event."""
        await self._account.redact(conversation, message_id)
