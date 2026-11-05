"""What a running turn shows on Discord: its reply as it is written, and its activity card with a Stop button.

Discord has no stream, so both are messages that Belle edits in place. Belle spaces its edits by its own
interval; the client also waits out each rate limit Discord names, and one that outlasts its retries spaces
the next edits further. Discord sets no time limit on editing a message. A reply longer than one message
goes on in a new message in the same thread. The card is one message with an embed and, while its turn runs,
a Stop button; a card of a turn that said nothing or was stopped is deleted, which leaves no trace.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent import progress
from belle_agent.backend import (
    EVENT_STEP,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    TurnEvent,
)
from belle_agent.platforms.base import DeliveryUncertain, InboundMessage
from belle_agent.platforms.discord.affordances import DISCORD_AFFORDANCES, MESSAGE_LIMIT
from belle_agent.platforms.discord.client import DiscordClient, DiscordError
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE
from belle_agent.platforms.discord.render import ALLOWED_MENTIONS, RENDERER, discord_markdown
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.platforms.edit_surfaces import ActivityCards, EditedCard, EditedReplies

logger = logging.getLogger(__name__)

#: The settings that turn each surface on, as the host names them at start.
STREAMING_SETTING = "platforms.discord.enable_streaming"
ACTIVITY_SETTING = "platforms.discord.activity_card"
#: How often a reply shown as it is written is edited at most, in seconds: Belle's own pacing, below the five
#: edits in five seconds per channel seen on Discord. The client honours the limits Discord states.
STREAM_EDIT_SECONDS = 1.2
#: What a reply that a stop ended says at its end.
STOPPED_TAIL = f"\n\n_{progress.TITLE_STOPPED}_"
STOP_LABEL = "Stop"
#: The colour of the card's embed as its turn runs, and as it ended.
COLOR_WORKING = 0x1C58D9
COLOR_DONE = 0x3DB887
COLOR_FAILED = 0xD24B4E
COLOR_STOPPED = 0x8B8D93
_RUN_EMOJI = {
    progress.RUN_RUNNING: "\N{HIGH VOLTAGE SIGN}",
    progress.RUN_DONE: "\N{WHITE HEAVY CHECK MARK}",
    progress.RUN_FAILED: "\N{CROSS MARK}",
    progress.RUN_UNREPORTED: "\N{WHITE QUESTION MARK ORNAMENT}",
}

#: The channel a post in a thread of a conversation goes to, the thread started where it is new.
ChannelFor = Callable[[str, str], Awaitable[str]]
#: A Stop button for the turn of a message, from its message id. ``None``: no button.
StopButton = Callable[[str], Mapping[str, Any]] | None


@dataclass
class _Card(EditedCard):
    """The activity card of one turn."""

    conversation: str = ""
    channel: str = ""
    post_id: str = ""


class TurnSurfaces:
    """The reply and the card of each running turn of one connector."""

    logger: logging.Logger = logging.getLogger(__name__)
    log_name = "discord"
    id_label = "message"
    errors: tuple[type[Exception], ...] = (DiscordError,)
    #: As the connector declares them (``DISCORD_AFFORDANCES``).
    reply_parts_in_thread = DISCORD_AFFORDANCES.reply_parts_in_thread
    silent_card = DISCORD_AFFORDANCES.silent_card

    def __init__(self, client: DiscordClient, settings: DiscordSettings, channel_for: ChannelFor, stop_button: StopButton) -> None:
        self._client = client
        self._settings = settings
        self._channel_for = channel_for
        self._stop_button = stop_button
        #: The channel of each message Belle posted here: a message is edited and deleted in its channel.
        self._channels: dict[str, str] = {}
        self.replies = EditedReplies(self, edit_seconds=STREAM_EDIT_SECONDS, stopped_tail=STOPPED_TAIL)
        self._replies = self.replies.replies
        self.cards = ActivityCards(self, settings)
        self._cards = self.cards.cards

    @property
    def shows(self) -> Mapping[str, str]:
        """Each kind of event a turn shows as configured, with its setting."""
        shown: dict[str, str] = {}
        if self._settings.enable_streaming:
            shown.update({EVENT_TEXT_DELTA: STREAMING_SETTING, EVENT_STEP: STREAMING_SETTING})
        if self._settings.activity_card:
            shown.update({EVENT_TOOL_CALLED: ACTIVITY_SETTING, EVENT_TOOL_RESULT: ACTIVITY_SETTING})
        return shown

    async def begin(self, inbound: InboundMessage) -> None:
        """Start the card of a turn, so that a turn that calls no tool can be stopped by its button too."""
        # A card with no run and no button would say only that the turn works.
        if self._settings.activity_card and self._stop_button is not None:
            await self.cards.begin(inbound)

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Show one event of a running turn."""
        if event.kind in (EVENT_TEXT_DELTA, EVENT_STEP):
            await self.replies.show(inbound, event)
        elif event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT):
            await self.cards.show(inbound, event)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End the reply that was shown, then settle the card. Return whether the reply became ``final``."""
        key = (inbound.conversation, inbound.message_id)
        reply, card = self._replies.get(key), self._cards.get(key)
        try:
            return await self.replies.end(inbound, state, final)
        finally:
            try:
                await self.cards.end(inbound, state)
            finally:
                for message_id in reply.ids if reply is not None else ():
                    self._channels.pop(message_id, None)
                if card is not None:
                    self._channels.pop(card.post_id, None)

    async def close(self) -> None:
        """Forget every turn, and stop what still waits to write."""
        self.replies.close()
        self.cards.close()
        self._channels.clear()

    def _keep(self, message_id: str, channel: str) -> None:
        self._channels[message_id] = channel

    async def _post(self, channel: str, body: Mapping[str, Any]) -> str:
        path = f"/channels/{channel}/messages"
        posted = await self._client.mapping("POST", path, json=body)
        message_id = posted.get("id")
        if not isinstance(message_id, str) or not SNOWFLAKE_RE.fullmatch(message_id) or posted.get("channel_id") != channel:
            raise DiscordError("bad_response", "POST", path)
        self._keep(message_id, channel)
        return message_id

    # ── the reply ────────────────────────────────────────────────────────

    async def reply_budget(self, conversation: str) -> int:
        """The characters one message holds, as Discord documents it."""
        return MESSAGE_LIMIT

    def cut_index(self, text: str, budget: int) -> int | None:
        """Where a message that shows the start of ``text`` ends, or ``None`` where all of it fits, as Discord shows it."""
        if RENDERER.measure(discord_markdown(text)) <= budget:
            return None
        # A table takes more room as Discord shows it than as written: the cut is taken back until it fits.
        allowed = budget
        while True:
            index = RENDERER.cut_index(text, allowed)
            over = RENDERER.measure(discord_markdown(text[:index].strip())) - budget
            if over <= 0 or allowed <= 1:
                return index
            allowed = max(1, allowed - over)

    @staticmethod
    def _content(text: str) -> dict[str, Any]:
        return {"content": discord_markdown(text), "allowed_mentions": ALLOWED_MENTIONS}

    async def send_reply(self, conversation: str, thread: str, text: str, *, final: bool) -> str:
        """Post one part of a reply in ``thread``. Return the message's id."""
        channel = await self._channel_for(conversation, thread)
        try:
            return await self._post(channel, self._content(text))
        except DiscordError as exc:
            if exc.code in {"unreachable", "bad_response"}:
                raise DeliveryUncertain(f"Discord message creation in channel {channel} could not be confirmed") from None
            raise

    async def edit_reply(self, conversation: str, message_id: str, thread: str, text: str, *, final: bool) -> None:
        """Edit one message of a reply. Discord sets no time limit on an edit, so none is refused as too late."""
        channel = self._channels.get(message_id, conversation)
        await self._client.request("PATCH", f"/channels/{channel}/messages/{message_id}", json=self._content(text))

    def retry_after(self, exc: Exception) -> float | None:
        """The seconds a rate limit that outlasted the client's retries asked to wait, or ``None``."""
        return exc.retry_after if isinstance(exc, DiscordError) and exc.code == "rate_limited" else None

    # ── the card ─────────────────────────────────────────────────────────

    def new_card(self, inbound: InboundMessage) -> _Card:
        """A card, not yet posted, in the thread of ``inbound``."""
        return _Card(
            conversation=inbound.conversation, thread=inbound.reply_thread or "",
            workspace=inbound.workspace, message_id=inbound.message_id,
        )

    def card_id(self, card: _Card) -> str:
        """The id of the card's message, or empty while it is not posted."""
        return card.post_id

    def _body(self, card: _Card) -> dict[str, Any]:
        """The card: one embed with its title and its runs and, while the turn runs, a row with the Stop button."""
        if card.stopped:
            title, color = progress.TITLE_STOPPED, COLOR_STOPPED
        else:
            title = progress.turn_title(card, closed=card.closed, failed=card.failed)
            color = COLOR_FAILED if card.failed else COLOR_DONE if card.closed else COLOR_WORKING
        summary = progress.card_summary(card)
        embed: dict[str, Any] = {"title": title, "color": color}
        # A card that has no run yet shows its title and the Stop button only.
        if description := "\n".join([summary, *progress.run_lines(card, _RUN_EMOJI)]).strip():
            embed["description"] = description
        rows = []
        if not card.closed and self._stop_button is not None:
            rows = [{"type": 1, "components": [self._stop_button(card.message_id)]}]
        return {"content": "", "embeds": [embed], "components": rows, "allowed_mentions": {"parse": []}}

    async def write_card(self, card: _Card) -> float | None:
        """Post the card, or edit it. An edit Discord refuses leaves it as it is; a rate limit's wait is returned."""
        body = self._body(card)
        if card.post_id:
            try:
                await self._client.request("PATCH", f"/channels/{card.channel}/messages/{card.post_id}", json=body)
            except DiscordError as exc:
                logger.warning("discord activity card not edited, left as it is: message=%s %s", card.post_id, exc)
                return self.retry_after(exc)
        else:
            card.channel = await self._channel_for(card.conversation, card.thread)
            card.post_id = await self._post(card.channel, body)
            logger.info("discord activity card: posted channel=%s message=%s", card.channel, card.post_id)
        return None

    async def show_stop(self, card: _Card) -> None:
        """Nothing to add: the Stop button is part of the card's payload."""

    async def clear_stop(self, card: _Card) -> None:
        """Nothing to take off: a settled card's payload has no Stop button."""

    async def remove(self, conversation: str, message_id: str) -> None:
        """Delete one message of the bot's, in the channel it was posted in."""
        channel = self._channels.get(message_id, conversation)
        await self._client.request("DELETE", f"/channels/{channel}/messages/{message_id}")
