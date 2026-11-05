"""What a running turn shows on Mattermost: its reply as it is written, and its activity card with a stop button.

Mattermost has no stream, so both are posts that Belle edits in place. Belle
spaces its edits by its own interval. The server may refuse an edit of an
older post, where an operator set ``PostEditTimeLimit``: the reply then keeps
what it shows and goes on in a new post in the same thread, and the card
keeps what it shows. A reply longer than the server's ``MaxPostSize`` goes
on in a new post too.
"""

import logging
from collections.abc import Callable, Mapping
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
from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.edit_surfaces import ActivityCards, EditedCard, EditedReplies, EditRefused
from belle_agent.platforms.mattermost.affordances import MATTERMOST_AFFORDANCES
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError, created_post_id
from belle_agent.platforms.mattermost.directory import Directory
from belle_agent.platforms.mattermost.render import RENDERER
from belle_agent.platforms.mattermost.settings import MattermostSettings

logger = logging.getLogger(__name__)

#: The settings that turn each surface on, as the host names them at start.
STREAMING_SETTING = "platforms.mattermost.enable_streaming"
ACTIVITY_SETTING = "platforms.mattermost.activity_card"
#: How often a reply shown as it is written is edited at most, in seconds. Belle's own budget.
STREAM_EDIT_SECONDS = 1.0
#: What a reply that a stop ended says at its end.
STOPPED_TAIL = f"\n\n_{progress.TITLE_STOPPED}_"
STOP_LABEL = "Stop"
#: The colour of the card's bar as its turn runs, and as it ended.
COLOR_WORKING = "#1c58d9"
COLOR_DONE = "#3db887"
COLOR_FAILED = "#d24b4e"
COLOR_STOPPED = "#8b8d93"
_RUN_EMOJI = {
    progress.RUN_RUNNING: ":zap:",
    progress.RUN_DONE: ":white_check_mark:",
    progress.RUN_FAILED: ":x:",
    progress.RUN_UNREPORTED: ":grey_question:",
}
_EDIT_TIME_LIMIT = "permissions_time_limit"

#: A stop button for the turn of a message, from its workspace and its message id. ``None``: no button.
StopButton = Callable[[str, str], Mapping[str, Any]] | None


def _too_late(exc: MattermostError) -> bool:
    """Whether the server refused an edit because the post is older than its edit time limit."""
    return _EDIT_TIME_LIMIT in exc.server_id


@dataclass
class _Card(EditedCard):
    """The activity card of one turn."""

    conversation: str = ""
    post_id: str = ""


class TurnSurfaces:
    """The reply and the card of each running turn of one connector."""

    logger: logging.Logger = logging.getLogger(__name__)
    log_name = "mattermost"
    id_label = "post"
    errors: tuple[type[Exception], ...] = (MattermostError,)
    reply_parts_in_thread = MATTERMOST_AFFORDANCES.reply_parts_in_thread
    silent_card = MATTERMOST_AFFORDANCES.silent_card

    def __init__(
        self, client: MattermostClient, directory: Directory, settings: MattermostSettings, stop_button: StopButton
    ) -> None:
        self._client = client
        self._directory = directory
        self._settings = settings
        self._stop_button = stop_button
        self.replies = EditedReplies(self, edit_seconds=STREAM_EDIT_SECONDS, stopped_tail=STOPPED_TAIL)
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
        """Return ``MaxPostSize`` in characters; zero means unlimited."""
        return await self._directory.post_limit()

    def cut_index(self, text: str, budget: int) -> int | None:
        """Return a cut index, or ``None`` when the text fits."""
        return RENDERER.cut_index(text, budget) if budget and RENDERER.measure(text) > budget else None

    async def send_reply(self, conversation: str, thread: str, text: str, *, final: bool) -> str:
        """Post a reply part and return its ID."""
        posted = await self._client.request(
            "POST", "/posts", json={"channel_id": conversation, "root_id": thread, "message": text}
        )
        return created_post_id(posted)

    async def edit_reply(self, conversation: str, message_id: str, thread: str, text: str, *, final: bool) -> None:
        """Edit a reply part; raise ``EditRefused`` if it is too old."""
        try:
            await self._client.request("PUT", f"/posts/{message_id}/patch", json={"message": text})
        except MattermostError as exc:
            if _too_late(exc):
                raise EditRefused(str(exc)) from exc
            raise

    def retry_after(self, exc: Exception) -> float | None:
        """Mattermost provides no retry delay."""
        return None

    # ── the card ─────────────────────────────────────────────────────────

    def new_card(self, inbound: InboundMessage) -> _Card:
        """Create an unposted card in the inbound thread."""
        return _Card(
            conversation=inbound.conversation, thread=inbound.reply_thread or "",
            workspace=inbound.workspace, message_id=inbound.message_id,
        )

    def card_id(self, card: _Card) -> str:
        """Return the posted card ID, if any."""
        return card.post_id

    def _body(self, card: _Card) -> dict[str, Any]:
        """The card: one attachment that holds its title, its runs and, while the turn runs, the stop button."""
        if card.stopped:
            title, color = progress.TITLE_STOPPED, COLOR_STOPPED
        else:
            title = progress.turn_title(card, closed=card.closed, failed=card.failed)
            color = COLOR_FAILED if card.failed else COLOR_DONE if card.closed else COLOR_WORKING
        summary = progress.card_summary(card)
        attachment: dict[str, Any] = {
            # What a notification, a client without attachments and a screen reader read.
            "fallback": f"{title} - {summary}" if summary else title,
            "color": color,
            "title": title,
            "text": "\n".join([summary, *progress.run_lines(card, _RUN_EMOJI)]).strip(),
        }
        if not card.closed and self._stop_button is not None:
            attachment["actions"] = [self._stop_button(card.workspace, card.message_id)]
        return {"message": "", "props": {"attachments": [attachment]}}

    async def write_card(self, card: _Card) -> float | None:
        """Post or edit a card, leaving refused edits unchanged."""
        body = self._body(card)
        if card.post_id:
            try:
                await self._client.request("PUT", f"/posts/{card.post_id}/patch", json=body)
            except MattermostError as exc:
                logger.warning("mattermost activity card not edited, left as it is: post=%s %s", card.post_id, exc)
        else:
            posted = await self._client.request(
                "POST", "/posts", json={"channel_id": card.conversation, "root_id": card.thread, **body}
            )
            card.post_id = created_post_id(posted)
            logger.info("mattermost activity card: posted channel=%s post=%s", card.conversation, card.post_id)
        return None

    async def show_stop(self, card: _Card) -> None:
        """The payload already includes the stop button."""

    async def clear_stop(self, card: _Card) -> None:
        """The settled payload omits the stop button."""

    async def remove(self, conversation: str, message_id: str) -> None:
        """Delete a bot post."""
        await self._client.request("DELETE", f"/posts/{message_id}")
