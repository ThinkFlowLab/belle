"""The activity card of a turn, for a connector on the conversation host.

The host says when a turn owed a reply starts, and hands the connector the
tool events of a running turn. They are folded into the card the platform
package already draws: posted once the turn has run for the configured delay,
edited no more often than the configured interval, and settled when the host
says how the turn ended. The card carries a stop button, and a click on it is
a request to stop the turn that the host decides. A turn that has reported
nothing yet gets a card with its title and the button only, which goes when
the turn ends.
"""

import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.backend import EVENT_TOOL_CALLED, SUBAGENT, TurnEvent
from belle_agent.click_notices import stop_notice
from belle_agent.platforms.base import ENDED_SILENT, MARK_FAILED, MARK_STOPPED, InboundMessage, InboundStop
from belle_agent.platforms.slack.activity import (
    _RUN_DONE,
    _RUN_FAILED,
    _RUN_RUNNING,
    STOP_ACTION_ID,
    ActivityCardRenderer,
    ActivityCardState,
    decode_stop_value,
    run_label,
    subagent_run_id,
)
from belle_agent.platforms.slack.admission import click_identity
from belle_agent.platforms.slack.identity import event_team_id
from belle_agent.progress import tool_result_failed

logger = logging.getLogger(__name__)

#: The setting that turns the surface on, as the host names it at start.
ACTIVITY_SETTING = "platforms.slack.activity_card"


class SlackTurnActivity(ActivityCardRenderer):
    """Show what a running turn is doing on its card, and take a click on the card's stop button."""

    stop_action_id = STOP_ACTION_ID

    def _init_activity(self) -> None:
        self._activity_state = ActivityCardState()
        self._activity_records = self._activity_state.records
        self._activity_lock = self._activity_state.lock

    def _activity_key(self, inbound: InboundMessage) -> tuple[str, str] | None:
        """A turn's card is found by the message that started the turn, which is what a stop names."""
        return self._activity_state.key(inbound.message_id, inbound.conversation)

    async def _begin_activity(self, inbound: InboundMessage) -> None:
        """Start the card of a turn, so that a turn that calls no tool can be stopped by its button too."""
        key = self._activity_key(inbound)
        if key is None or self._client is None:
            return
        async with self._activity_lock:
            if key in self._activity_records:
                return
            record = self._activity_state.record(
                key, thread_ts=inbound.reply_thread or "", session_id=inbound.thread or inbound.message_id,
            )
            await self._arm_or_refresh(key, record, force=False)

    async def _show_activity(self, inbound: InboundMessage, event: TurnEvent) -> None:
        """Fold one tool call, or how one ended, into the card of its turn."""
        key = self._activity_key(inbound)
        call_id = str(event.data.get("call_id") or "").strip()
        # An unpaired call cannot leave the running state.
        if key is None or not call_id or self._client is None:
            return
        subagent = event.data.get(SUBAGENT)
        async with self._activity_lock:
            if event.kind == EVENT_TOOL_CALLED:
                record = self._activity_state.record(
                    key,
                    # The card is a message, so it goes where the turn's own answer goes.
                    thread_ts=inbound.reply_thread or "",
                    # The thread the turn runs in, which is what Slack's own stop button names.
                    session_id=inbound.thread or inbound.message_id,
                )
                if subagent is None:
                    await self._track_tool_call(key, record, call_id, str(event.data.get("name") or ""))
                else:
                    await self._track_subagent_run(key, record, call_id, run_label(subagent, "subagent"), _RUN_RUNNING)
                return
            record = self._activity_records.get(key)
            if record is None:
                # No matching run remains on screen.
                return
            state = _RUN_FAILED if tool_result_failed(event) else _RUN_DONE
            # A tool routed from a child session reuses its subagent's call_id and sets ``child``.
            ends_subagent = subagent is not None or (
                not event.data.get("child") and subagent_run_id(call_id) in record.subagent_runs
            )
            if ends_subagent:
                await self._track_subagent_run(key, record, call_id, run_label(subagent, "subagent"), state)
            else:
                await self._track_tool_result(record, call_id, state)

    async def _post_activity_card(self, key: tuple[str, str], record: Any) -> bool:
        posted = await super()._post_activity_card(key, record)
        if posted:
            logger.info("slack activity card: posted conversation=%s ts=%s", record.channel_id, record.message_ts)
        return posted

    async def _end_activity(self, inbound: InboundMessage, state: str) -> None:
        """Settle the card of a turn that ended, or remove one that has nothing left to say."""
        key = self._activity_key(inbound)
        if key is None:
            return
        async with self._activity_lock:
            record = self._activity_records.pop(key, None)
            if record is None:
                return
            record.failed = state == MARK_FAILED
            record.settle(stopped=state == MARK_STOPPED)
            if not record.message_ts or self._client is None:
                # The turn ended before its card was due. Nothing is on screen.
                return
            # A turn that said nothing leaves nothing behind, and a stopped turn is marked on its message.
            reason = self._discard_reason(record, withheld=state == ENDED_SILENT)
            conversation, ts = record.channel_id, record.message_ts
            removed = bool(reason) and await self._discard_activity_card(key, record, reason=reason)
            if not removed:
                await self._refresh_activity_card(record, force=True)
            logger.info(
                "slack activity card: settled conversation=%s ts=%s state=%s card=%s",
                conversation, ts, state, "removed" if removed else "kept",
            )

    # ── the stop button ──────────────────────────────────────────────────

    async def _request_stop(self, workspace: str, conversation: str, message_id: str, sender: str) -> str:
        """Hand the host a request to stop the turn of one message, whichever way a person made it.

        The card's button, Slack's own stop button and the stop command all come here, so the host
        decides each by one rule and the turn's card and reply end the same way.
        """
        outcome = await self._on_inbound(
            InboundStop(
                platform=self.name, workspace=workspace, conversation=conversation, message_id=message_id, sender=sender
            )
        )
        record = self._activity_records.get((message_id, conversation))
        if outcome.startswith("stopped") and record is not None:
            # The button is drawn on no later edit of the card.
            record.stop_requested = True
        return outcome

    async def _on_stop_action(self, ack: Any, body: dict[str, Any], action: dict[str, Any]) -> str:
        """Acknowledge a click on a card's stop button and hand the request to the host, which decides who may stop."""
        outcome = "handler-raised"
        try:
            await ack()
            outcome = await self._route_stop_action(body, action)
        except Exception:
            logger.exception("slack stop click raised: %s", click_identity(body, action))
        else:
            logger.info("slack stop click: %s outcome=%s", click_identity(body, action), outcome)
        return outcome

    async def _route_stop_action(self, body: dict[str, Any], action: dict[str, Any]) -> str:
        if self._on_inbound is None:
            return "dropped:platform-not-started"
        if not isinstance(body, dict) or not isinstance(action, dict):
            return "dropped:malformed-payload"
        decoded = decode_stop_value(action, logger)
        if decoded is None:
            return "dropped:button-names-no-turn"
        _thread, message_id, channel_id = decoded
        user = body.get("user")
        user_id = str((user.get("id") if isinstance(user, Mapping) else "") or "").strip()
        outcome = await self._request_stop(event_team_id(body), channel_id, message_id, user_id)
        notice, label = stop_notice(outcome)
        await self._notify_clicker(body, user_id, notice, notice=label)
        return outcome
