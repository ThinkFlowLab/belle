"""Inbound Slack routing for the JiuwenSwarm channel."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from jiuwenswarm.extensions.sdk.host_types import ReqMethod
from jiuwenswarm.extensions.sdk.queued_turn import mark_queued_new_turn

from belle_agent.integrations.jiuwenswarm.config import (
    configured_models,
    slack_agent_request_params,
)
from belle_agent.integrations.jiuwenswarm.slack_routing import (
    slack_audience_metadata,
    slack_session_id,
)
from belle_agent.integrations.openjiuwen.turn_requirements import apply_turn_requirements
from belle_agent.platforms.slack import inputs as slack_inputs
from belle_agent.platforms.slack.actors import SLACK_USERS, ActorUnknown
from belle_agent.platforms.slack.admission import (
    PendingEvent,
    SlackMessageOrigin,
    app_messages_note,
    event_gate,
    event_identity,
    message_route,
    non_user_content_outcome,
)
from belle_agent.platforms.slack.admission import (
    SlackQueuedMessage as _SlackQueuedMessage,
)
from belle_agent.platforms.slack.brain_ingest import (
    maybe_dispatch_brain_ingest,
)
from belle_agent.platforms.slack.config import (
    ACK_MODE_OFF,
    normalize_app_messages,
    opens_a_thread,
)
from belle_agent.platforms.slack.events import (
    EVENT_CONTEXT,
    EVENT_OFF,
    EVENT_TURN,
    event_anchor,
    event_family,
    family_anchors_on_a_message,
    family_waits_its_turn,
    render_event,
)
from belle_agent.platforms.slack.file_transfer import (
    attachment_params,
    describe_downloaded_attachments,
    describe_event_attachments,
    event_entries,
)
from belle_agent.platforms.slack.history_policy import (
    METADATA_ASKER_KEY,
    METADATA_TEAM_KEY,
)
from belle_agent.platforms.slack.identity import (
    add_slack_action_token,
    event_team_id,
    message_envelope_ids,
)
from belle_agent.platforms.slack.post import POST_MESSAGE
from belle_agent.platforms.slack.scoped_config import (
    MENTION_ROUTE_TRIGGERS,
    TRIGGER_EVENT,
)
from belle_agent.platforms.slack.tool_context import slack_request_metadata
from belle_agent.platforms.slack.turn_text import (
    append_turn_advice,
    event_turn_text,
    message_turn_text,
)
from belle_agent.scopes import (
    MID_TURN_STEER,
    SESSION_CHANNEL,
)
from belle_agent.silence import (
    NO_REPLY_INSTRUCTION,
)
from belle_agent.silence import (
    post_tool_instruction as belle_post_tool_instruction,
)

_MAX_QUEUED_SESSIONS = 64
_MAX_QUEUED_PER_SESSION = 8
_QUEUED_MESSAGE_TIMEOUT_SECONDS = 3600.0

logger = logging.getLogger("belle_agent.integrations.jiuwenswarm.slack_channel")

def post_tool_instruction() -> str:
    return belle_post_tool_instruction(POST_MESSAGE)


SLACK_ADDRESSED_KEY = "slack_addressed"


POST_AS_ROOT_KEY = "post_as_root"




_INTERRUPT_RESUME_SOURCES = frozenset(
    {
        "ask_user_interrupt",
        "confirm_interrupt",
        "permission_interrupt",
        "evolution_interrupt",
    }
)


_MAX_PENDING_EVENTS_PER_SESSION = 256


_MAX_PENDING_EVENTS = 4096


_PENDING_EVENT_TIMEOUT_SECONDS = 86400.0


def slack_audience_request_metadata() -> dict[str, Any]:
    """Stamp the audience rule on every Slack request."""
    return slack_audience_metadata()


class SlackInboundMixin:

    def _queue_mid_turn_message(self, entry: _SlackQueuedMessage) -> bool:
        self._prune_queued_messages()
        return self._admission.queue_message(
            entry.session_id, entry, max_per_session=_MAX_QUEUED_PER_SESSION,
            warn=logger.warning,
        )

    def _prune_queued_messages(self) -> None:
        self._admission.prune_messages(
            cutoff=time.monotonic() - _QUEUED_MESSAGE_TIMEOUT_SECONDS,
            max_sessions=_MAX_QUEUED_SESSIONS, warn=logger.warning,
        )

    def _log_event_loss(
        self, *, event_id: str, event_type: str, family: str,
        actor_id: str, chat_id: str, anchor_ts: str = "",
        session_id: str = "", reason: str,
    ) -> None:
        self._lost_events[chat_id] = self._lost_events.get(chat_id, 0) + 1
        logger.warning(
            "Slack event turn lost: event_id=%s type=%s family=%s actor=%s"
            " channel=%s anchor=%s session=%s reason=%s; recover manually",
            event_id, event_type, family, actor_id, chat_id, anchor_ts,
            session_id, reason,
        )

    def _log_pending_event(self, entry: PendingEvent, reason: str) -> None:
        if entry.loss_logged:
            return
        entry.loss_logged = True
        self._log_event_loss(
            event_id=entry.event_id,
            event_type=str(entry.event.get("type") or ""),
            family=entry.family,
            actor_id=entry.actor_id,
            chat_id=entry.chat_id,
            anchor_ts=entry.anchor_ts,
            session_id=entry.session_id,
            reason=reason,
        )

    def _prune_pending_events(self) -> None:
        expired = self._admission.prune_events(
            cutoff=time.monotonic() - _PENDING_EVENT_TIMEOUT_SECONDS,
        )
        for entry in expired:
            self._log_pending_event(entry, "expired")

    async def _finish_event_admission(
        self, session_id: str, arrival_seq: int, reserved: bool
    ) -> None:
        self._admission.finish_admission(session_id, arrival_seq, reserved)
        if (
            (arrival_seq or reserved)
            and self._running
            and self.turn_initiator(session_id) is None
        ):
            await self._drain_queued_messages(session_id)

    async def _hold_pending_event(self, entry: PendingEvent) -> str:
        self._prune_pending_events()
        if not self._admission.hold_event(
            entry.session_id, entry,
            max_per_session=_MAX_PENDING_EVENTS_PER_SESSION,
            max_total=_MAX_PENDING_EVENTS,
        ):
            self._log_pending_event(entry, "queue-full")
            return f"dropped:pending-{entry.family}-full"
        queue = self._admission.pending_events[entry.session_id]
        logger.info(
            "Slack event turn pending: event_id=%s type=%s family=%s actor=%s"
            " channel=%s anchor=%s session=%s pending=%s",
            entry.event_id, entry.event.get("type"), entry.family,
            entry.actor_id, entry.chat_id, entry.anchor_ts,
            entry.session_id, len(queue),
        )
        await self._drain_queued_messages(entry.session_id)
        return f"held:{entry.family} session={entry.session_id}"

    async def _drain_pending_events(self, session_id: str) -> None:
        self._prune_pending_events()
        while (
            self._running
            and self._admission.pending_events.get(session_id)
            and self.turn_initiator(session_id) is None
            and not self._session_awaits_an_answer(session_id)
            and session_id not in self._admission.event_admissions
        ):
            entry = self._admission.take_pending_event(session_id)
            if entry is None:
                return
            try:
                outcome = await self._wake_turn_for_event(
                    entry.event, entry.body, family=entry.family,
                    chat_id=entry.chat_id, chat_type=entry.chat_type,
                    actor_id=entry.actor_id, record=entry.record,
                    remembered=True, reservation_owned=True,
                    arrival_seq=entry.arrival_seq,
                )
            except BaseException:
                self._log_pending_event(entry, "dispatch-error")
                raise
            finally:
                self._admission.event_admissions.discard(session_id)
            if not outcome.startswith(("dispatched:", "buffered:", "held:")):
                self._log_pending_event(entry, f"dispatch-{outcome}")

    async def _drain_queued_messages(self, session_id: str) -> None:
        if not self._running or not session_id or session_id in self._admission.event_admissions:
            return
        self._prune_queued_messages()
        if not self._admission.queued_messages.get(session_id):
            await self._drain_pending_events(session_id)
            return
        if self.turn_initiator(session_id) is not None:
            return
        entry = self._admission.take_message(session_id)
        await self._dispatch_queued_message(entry)

    async def _dispatch_queued_message(self, entry: _SlackQueuedMessage) -> None:
        self._remember_turn_initiator(
            entry.session_id,
            entry.user_id,
            entry.request.id,
            is_dm=entry.is_dm,
            chat_type=entry.chat_type,
            model_name=str(entry.request.params.get("model_name") or ""),
            origin=entry.origin,
            # Use metadata settled when the message arrived.
            request_metadata=entry.request.metadata,
            preview_text=entry.preview_text,
        )
        self._remember_stream_recipient(
            entry.session_id, entry.user_id, entry.team_id
        )
        await self._unmark_queued(entry)
        if self._acknowledge_mode() != ACK_MODE_OFF:
            await self._set_thinking_status(entry.origin)
        await self._begin_activity_card(entry.session_id)
        # A queued message withdraws interrupts before a late click can answer them.
        await self._withdraw_questions_for_session(entry.session_id)
        logger.info(
            "[SlackChannel] queued message dispatched: session=%s request_id=%s"
            " user=%s still_queued=%s",
            entry.session_id,
            entry.request.id,
            entry.user_id,
            len(self._admission.queued_messages.get(entry.session_id, ())),
        )
        metadata = dict(entry.request.metadata or {})
        mark_queued_new_turn(metadata)
        entry.request.metadata = metadata
        await self._route_request(entry.request)

    def _non_user_content_outcome(self, event: Mapping[str, Any]) -> str | None:
        return non_user_content_outcome(
            event, mode=normalize_app_messages(self.config.app_messages),
            app_ids=self.config.app_messages_from,
            bot_id=self._identity.bot_id, bot_user_id=self._identity.bot_user_id,
        )

    def _log_event_outcome(
        self,
        event: Mapping[str, Any],
        body: Mapping[str, Any],
        source: str,
        outcome: str,
    ) -> None:
        logger.info(
            "[SlackChannel] inbound %s: %s%s outcome=%s",
            source,
            event_identity(event, body),
            app_messages_note(
                event, mode=normalize_app_messages(self.config.app_messages),
                app_ids=self.config.app_messages_from,
            ),
            outcome,
        )

    async def _route_and_log(
        self,
        route: Callable[[dict[str, Any], dict[str, Any]], Awaitable[str]],
        event: dict[str, Any],
        body: dict[str, Any],
        *,
        family: str,
    ) -> None:
        """Log route failures before Slack Bolt catches them."""
        outcome = "handler-raised"
        try:
            outcome = await route(event, body)
        except Exception:
            logger.exception(
                "[SlackChannel] inbound %s raised: %s",
                family,
                event_identity(event, body),
            )
        finally:
            self._log_event_outcome(event, body, family, outcome)

    async def _handle_app_mention(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        await self._route_and_log(
            self._route_app_mention, event, body, family="app_mention"
        )

    async def _route_app_mention(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> str:
        self._remember_bot_user_id(body)
        channel_id = str(event.get("channel") or "").strip()
        sender_id = str(event.get("user") or "").strip()
        # app_mention omits channel_type. Resolve it for scoped trigger rules.
        chat_type = self._conversation_chat_type(channel_id, event)
        if not (
            self._channel_triggers(channel_id, sender_id, chat_type)
            & MENTION_ROUTE_TRIGGERS
        ):
            return "ignored:mention-not-a-trigger-here"
        return await self._handle_slack_event(
            event, body, is_dm=False, trigger="mention"
        )

    async def _handle_message_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        await self._route_and_log(
            self._route_message_event, event, body, family="message"
        )

    async def _route_message_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> str:
        self._remember_bot_user_id(body)
        self._learn_chat_type(event)
        outcome = self._non_user_content_outcome(event)
        if outcome is not None:
            return outcome

        if str(event.get("channel_type") or "") == "im":
            return await self._handle_slack_event(
                event, body, is_dm=True, trigger="dm"
            )

        channel_id = str(event.get("channel") or "").strip()

        # Ingest before checking reply triggers, after excluding DMs.
        ingesting = maybe_dispatch_brain_ingest(self, event)

        triggers = self._channel_triggers(
            channel_id,
            str(event.get("user") or "").strip(),
            self._conversation_chat_type(channel_id, event),
        )
        text = str(event.get("text") or "").strip()
        trigger, outcome = message_route(
            triggers, event, text, bot_user_id=self._identity.bot_user_id,
            leading_mention=self._identity.has_leading_mention(text),
            mentions_bot_id=self._identity.mentions_bot_id(text),
            ingesting=ingesting,
        )
        if outcome:
            return outcome

        return await self._handle_slack_event(
            event, body, is_dm=False, trigger=trigger
        )

    async def _handle_inbound_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        await self._route_and_log(
            self._route_inbound_event,
            event,
            body,
            family=str((event or {}).get("type") or "-"),
        )

    async def _route_inbound_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> str:
        if not self._running:
            return "dropped:channel-not-running"
        if not isinstance(event, dict) or not isinstance(body, dict):
            return "dropped:malformed-payload"
        family = event_family(event)
        if family is None:
            return "ignored:event-type-has-no-family"
        self._remember_bot_user_id(body)
        actor_id, chat_id, outcome = event_gate(event, self._identity.bot_user_id)
        if outcome:
            return outcome

        if not self._channel_is_allowed(chat_id):
            return "ignored:channel-not-in-allowed_channel_ids"
        if not self.is_allowed(actor_id):
            return "refused:actor-not-in-allow_from"

        # Welcome is allowed after identity checks even when events are off.
        welcome = await self._welcome_empty_home_tab(event, user_id=actor_id)

        chat_type = self._conversation_chat_type(chat_id, event)
        disposition = self._channel_event_disposition(chat_id, family, chat_type)
        if disposition == EVENT_OFF:
            return welcome or f"ignored:events-off-for-{family}-here"
        if not chat_id.startswith("D") or actor_id in SLACK_USERS:
            # The payload says nothing of an app. A direct message holds this app and one person, or Slack.
            try:
                if await self._actors.is_bot(self._client, actor_id, self._granted_scopes):
                    return "ignored:done-by-an-app"
            except ActorUnknown as unknown:
                return unknown.outcome

        record = render_event(event, family=family)

        if disposition == EVENT_TURN:
            return await self._wake_turn_for_event(
                event,
                body,
                family=family,
                chat_id=chat_id,
                chat_type=chat_type,
                actor_id=actor_id,
                record=record,
            )

        if disposition != EVENT_CONTEXT:
            return f"dropped:unknown-events-disposition-for-{family}"

        self._inbound_events.record(chat_id, family, record)
        return f"buffered:{family}"

    async def _welcome_empty_home_tab(
        self, event: dict[str, Any], *, user_id: str
    ) -> str:
        if not self.config.home_tab_welcome:
            return ""
        # Keep agent tools out of gateway startup.
        from belle_agent.platforms.slack.home_tab import (
            home_tab_welcome_outcome,
            publish_home_tab_welcome,
        )

        outcome = home_tab_welcome_outcome(event)
        if outcome is not None:
            return outcome
        if not user_id or self._client is None:
            return ""

        from belle_agent.integrations.jiuwenswarm.slack_home_tab import (
            SlackHomeTabToolkit,
        )

        toolkit = SlackHomeTabToolkit(
            metadata={
                METADATA_ASKER_KEY: user_id,
                METADATA_TEAM_KEY: self._identity.team_id,
            },
            client=self._client,
        )
        if not await publish_home_tab_welcome(
            toolkit, bot_name=self.config.bot_name, command=self._slash_command()
        ):
            return "failed:home-tab-welcome-not-published"
        logger.info(
            "[SlackChannel] welcomed %s onto an empty Home tab", user_id
        )
        return "welcomed:empty-home-tab"

    def _request_metadata(
        self,
        channel_id: str,
        user_id: str,
        team_id: str,
        event_id: str,
        chat_type: str,
        is_dm: bool,
    ) -> dict[str, Any]:
        return slack_request_metadata(
            channel_id, user_id, team_id, event_id, chat_type, is_dm,
            audience=slack_audience_request_metadata(),
        )

    async def _wake_turn_for_event(
        self,
        event: dict[str, Any],
        body: dict[str, Any],
        *,
        family: str,
        chat_id: str,
        chat_type: str,
        actor_id: str,
        record: dict[str, Any],
        remembered: bool = False,
        reservation_owned: bool = False,
        arrival_seq: int = 0,
    ) -> str:
        """Dispatch an event turn or buffer it when dispatch is unsafe."""
        event_id = str(body.get("event_id") or event.get("event_ts") or "").strip()
        anchored = family_anchors_on_a_message(family)
        anchor_ts, anchor_thread_ts = event_anchor(event, family=family)
        waits = family_waits_its_turn(family)

        def unavailable(reason: str, explanation: str) -> str:
            if waits:
                self._log_event_loss(
                    event_id=event_id, event_type=str(event.get("type") or ""),
                    family=family, actor_id=actor_id, chat_id=chat_id,
                    anchor_ts=anchor_ts, reason=reason,
                )
                return f"dropped:{family}-{reason}"
            return self._buffer_instead_of_waking(
                chat_id, family, record, reason, explanation
            )

        if anchored and not anchor_ts:
            return unavailable("no-anchor", "the event names no message to answer")

        team_id = event_team_id(body, event)
        if not team_id:
            return unavailable(
                "no-team",
                "the event named no workspace, so it has no session of its own",
            )

        is_dm = chat_id.startswith("D")

        # Event turns require optional reply delivery.
        if not self._channel_reply_is_optional(chat_id, chat_type):
            if waits:
                key = (chat_id, family)
                if key not in self._reported_required_event_reply:
                    self._reported_required_event_reply.add(key)
                    logger.warning(
                        "Slack %s turn events in %s use context because"
                        " delivery.reply is required; set it to optional"
                        " for independent event turns",
                        family, chat_id,
                    )
            return self._buffer_instead_of_waking(
                chat_id,
                family,
                record,
                "reply-required",
                "a required reply would oblige this turn to speak about the"
                " event; set delivery.reply to optional for this conversation"
                " to let an event turn stay silent",
            )

        # Membership events have no message thread to anchor a session.
        session_is_channel_wide = not is_dm and (
            not anchored
            or self._session_scope(chat_id, actor_id, chat_type) == SESSION_CHANNEL
        )
        session_id = slack_session_id(
            team_id=team_id,
            channel_id=chat_id,
            user_id=actor_id,
            root_thread_ts=anchor_thread_ts,
            is_dm=is_dm,
            channel_wide=session_is_channel_wide,
        )
        if not session_id:
            return unavailable(
                "no-session", "no session id could be built for the anchor"
            )

        reservation = waits and (
            reservation_owned or (
                self._admission.can_reserve(
                    session_id,
                    turn_running=self.turn_initiator(session_id) is not None,
                    awaiting_answer=self._session_awaits_an_answer(session_id),
                )
            )
        )
        pending = None
        if waits:
            if not remembered:
                arrival_seq = self._admission.next_arrival()
            pending = PendingEvent(
                event=event, body=body, family=family, chat_id=chat_id,
                chat_type=chat_type, actor_id=actor_id, record=record,
                session_id=session_id, event_id=event_id, anchor_ts=anchor_ts,
                arrival_seq=arrival_seq,
            )
            if not remembered:
                self._prune_pending_events()
                if (
                    self._admission.pending_count(session_id) >= _MAX_PENDING_EVENTS_PER_SESSION
                    or self._admission.pending_count() >= _MAX_PENDING_EVENTS
                ):
                    self._log_pending_event(pending, "admission-full")
                    return f"dropped:pending-{family}-full"
                self._admission.track_admitting(pending.session_id, pending)
                logger.info(
                    "Slack event turn admission: event_id=%s type=%s family=%s"
                    " actor=%s channel=%s anchor=%s session=%s",
                    event_id, event.get("type"), family, actor_id, chat_id,
                    anchor_ts, session_id,
                )
        if reservation and not reservation_owned:
            self._admission.event_admissions.add(session_id)

        dedupe_key = f"{team_id}:{chat_id}:{family}:{event_id}" if event_id else ""
        try:
            fresh = remembered or not dedupe_key or await self._remember_event(dedupe_key)
        except BaseException as exc:
            if pending is not None:
                reason = (
                    "admission-cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "admission-error"
                )
                self._log_pending_event(pending, reason)
            await self._finish_event_admission(
                session_id, arrival_seq if not remembered else 0,
                reservation and not reservation_owned,
            )
            raise
        try:
            if not self._running:
                if pending is not None:
                    self._log_pending_event(pending, "channel-stopped-during-admission")
                return f"dropped:{family}-channel-stopped"
            if (
                pending is not None
                and not remembered
                and pending.queued_at
                < time.monotonic() - _PENDING_EVENT_TIMEOUT_SECONDS
            ):
                self._log_pending_event(pending, "expired-during-admission")
                return f"dropped:{family}-expired"
            if not fresh:
                return f"ignored:already-handled (dedupe) family={family}"

            if waits and self._admission.should_hold(
                session_id, arrival_seq, reserved=reservation,
                turn_running=self.turn_initiator(session_id) is not None,
                awaiting_answer=self._session_awaits_an_answer(session_id),
            ):
                return await self._hold_pending_event(pending)

            if self.turn_initiator(session_id) is not None:
                return self._buffer_instead_of_waking(
                    chat_id,
                    family,
                    record,
                    "turn-in-flight",
                    f"a turn is already running on session={session_id}",
                )
            if self._session_awaits_an_answer(session_id):
                return self._buffer_instead_of_waking(
                    chat_id,
                    family,
                    record,
                    "question-on-screen",
                    f"session={session_id} is waiting on an answer from somebody",
                )

            text = event_turn_text(
                record, anchor_ts=anchor_ts, anchor_thread_ts=anchor_thread_ts,
                anchored=anchored, buffered_events=self._fold_inbound_events(chat_id),
                prompt=self._channel_prompt(chat_id, actor_id, chat_type),
                silence_instruction=self._optional_reply_instruction(
                    honoured=self._channel_reply_is_optional(chat_id, chat_type),
                    offered=True,
                ),
            )

            metadata = self._request_metadata(
                chat_id, actor_id, team_id, event_id, chat_type, is_dm
            )
            metadata["slack_thread_ts"] = anchor_thread_ts
            if anchored:
                metadata["slack_message_ts"] = anchor_ts
                metadata["message_ts"] = anchor_ts
            else:
                metadata[POST_AS_ROOT_KEY] = True
            metadata["slack_trigger"] = TRIGGER_EVENT
            self._mark_reply_is_optional(metadata)
            add_slack_action_token(metadata, event, body)

            params: dict[str, Any] = {"content": text, "query": text}
            params.update(slack_agent_request_params(
                self.config, chat_id, user_id=actor_id, chat_type=chat_type,
                workspace=self._identity.team_id,
            ))
            model_name = self._channel_model_name(chat_id, actor_id, chat_type)
            if model_name:
                params["model_name"] = model_name

            req = self._request(
                event_id or f"slack-event-{int(time.time() * 1000)}",
                session_id=session_id,
                chat_id=chat_id,
                user_id=actor_id,
                req_method=ReqMethod.CHAT_SEND,
                params=params,
                metadata=metadata,
                is_stream=True,
            )

            self._remember_stream_recipient(session_id, actor_id, team_id)
            self._remember_turn_initiator(
                session_id,
                actor_id,
                req.id,
                is_dm=is_dm,
                chat_type=chat_type,
                model_name=str(req.params.get("model_name") or ""),
                request_metadata=req.metadata,
            )
            if waits:
                self._admission.event_turn_sessions[session_id] = req.id
            try:
                await self._route_request(req)
            except BaseException:
                self._forget_turn_initiator(session_id, req.id)
                if waits and not remembered:
                    self._log_pending_event(pending, "dispatch-error")
                raise
            logger.info(
                "[SlackChannel] event woke a turn: family=%s channel=%s anchor=%s"
                " actor=%s session=%s request_id=%s",
                family,
                chat_id,
                anchor_ts if anchored else "channel-root",
                actor_id,
                session_id,
                req.id,
            )
            return f"dispatched:{TRIGGER_EVENT}:{family} session={session_id}"

        finally:
            await self._finish_event_admission(
                session_id, arrival_seq if not remembered else 0,
                reservation and not reservation_owned,
            )

    def _buffer_instead_of_waking(
        self,
        chat_id: str,
        family: str,
        record: Mapping[str, Any],
        reason: str,
        explanation: str,
    ) -> str:
        self._inbound_events.record(chat_id, family, record)
        logger.info(
            "[SlackChannel] event buffered instead of waking a turn:"
            " family=%s channel=%s reason=%s -- %s",
            family,
            chat_id,
            reason,
            explanation,
        )
        return f"buffered:{family} (turn-downgraded:{reason})"

    def _session_awaits_an_answer(self, session_id: str) -> bool:
        if not session_id:
            return False
        return any(
            entry.session_id == session_id
            and entry.withdrawn_at is None
            and entry.source in _INTERRUPT_RESUME_SOURCES
            for entry in self._pending_questions.values()
        )

    async def _handle_unclaimed_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        event_type = str((event or {}).get("type") or "").strip() or "-"
        if event_type not in self._unclaimed_event_types:
            self._unclaimed_event_types.add(event_type)
            logger.info(
                "[SlackChannel] inbound %s: acknowledged and dropped, no"
                " listener reads it. Either the app manifest subscribes to it"
                " and nothing here was written for it, or the subscription is"
                " newer than this build",
                event_type,
            )

    async def _handle_slack_event(
        self,
        event: dict[str, Any],
        body: dict[str, Any],
        *,
        is_dm: bool,
        trigger: str,
        required_skills: tuple[str, ...] = (),
        required_subagent: tuple[str, str] | None = None,
        clear_session_extensions: bool = False,
    ) -> str:
        if not self._running:
            return "dropped:channel-not-running"
        if not isinstance(event, dict) or not isinstance(body, dict):
            return "dropped:malformed-payload"
        outcome = self._non_user_content_outcome(event)
        if outcome is not None:
            return outcome
        user_id, channel_id, message_ts, thread_ts, event_id = message_envelope_ids(event, body)
        chat_type = self._conversation_chat_type(channel_id, event)

        if not (user_id and channel_id):
            return "dropped:no-user-or-channel-on-event"

        team_id = event_team_id(body, event)
        message_identity = str(
            event.get("ts") or event.get("client_msg_id") or event_id
        ).strip()
        dedupe_key = ":".join(
            part for part in (team_id, channel_id, message_identity) if part
        )
        # Deduplicate before acknowledging Slack retries.
        if dedupe_key and not await self._remember_event(dedupe_key):
            return "ignored:already-handled (dedupe)"

        if not is_dm and not self._channel_is_allowed(channel_id):
            return "ignored:channel-not-in-allowed_channel_ids"
        if not self.is_allowed(user_id):
            await self._reject_request(channel_id, message_ts)
            return "refused:user-not-in-allow_from"

        text = str(event.get("text") or "").strip()
        addressed = self._identity.has_leading_mention(text)
        if not is_dm and trigger == "mention":
            text = self._identity.strip_leading_mention(text).strip()
        preview_text = text
        root_thread_ts = thread_ts or message_ts
        answer_in_thread = bool(thread_ts) or opens_a_thread(
            self._reply_in_thread(), is_direct_message=is_dm
        )
        reply_thread_ts = root_thread_ts if answer_in_thread else ""

        session_is_channel_wide = (
            not is_dm
            and self._session_scope(channel_id, user_id, chat_type) == SESSION_CHANNEL
        )
        session_id = slack_session_id(
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            root_thread_ts=root_thread_ts,
            is_dm=is_dm,
            channel_wide=session_is_channel_wide,
        )
        if not session_id:
            # Without a team id, a session could collide with another workspace.
            logger.warning(
                "Slack event carried no team_id; dropping it rather than"
                " sharing a session with another workspace: channel=%s ts=%s",
                channel_id, message_ts,
            )
            return "dropped:no-team-on-event"

        side_session = (
            self._side_session(team_id, channel_id, root_thread_ts) if is_dm else ""
        )
        if side_session:
            session_id = side_session
            reply_thread_ts = root_thread_ts
        elif not is_dm:
            self._note_channel_session(channel_id, session_id, root_thread_ts)

        origin = SlackMessageOrigin(
            channel_id=channel_id,
            message_ts=message_ts,
            reply_thread_ts=reply_thread_ts,
            root_thread_ts=root_thread_ts,
            is_dm=is_dm,
        )

        files = event_entries(event, "files")
        attachments = event_entries(event, "attachments")

        decision_input = {
            "channel_id": channel_id,
            "message_ts": message_ts,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "chat_type": chat_type,
            "is_dm": is_dm,
            "trigger": trigger,
            "text": str(event.get("text") or "").strip(),
        }
        self._observe_decision_in_shadow(event, body, **decision_input)

        # Acknowledge before downloading attachments.
        if text or files or attachments:
            await self._acknowledge_request(origin)

        downloaded: list[dict[str, Any]] = []
        if files:
            downloaded, failures = await self._download_attachments(
                files, session_id
            )
            if failures:
                await self._report_download_failures(
                    channel_id, reply_thread_ts, failures
                )
            if downloaded:
                text = describe_downloaded_attachments(text, downloaded)

        if attachments:
            text = describe_event_attachments(text, attachments)

        if not text:
            return "dropped:no-text-and-no-readable-attachment"

        prompt = self._channel_prompt(channel_id, user_id, chat_type)
        # The optional-reply matcher honors NO_REPLY even on addressed turns.
        silence_is_honoured = self._channel_reply_is_optional(channel_id, chat_type)
        silence_is_offered = silence_is_honoured and not addressed
        text = message_turn_text(
            text, message_ts=message_ts, thread_ts=thread_ts,
            trigger=trigger, prompt=prompt,
            buffered_events=self._fold_inbound_events(channel_id),
            silence_instruction=self._optional_reply_instruction(
                honoured=silence_is_honoured, offered=silence_is_offered,
            ),
        )

        metadata = self._request_metadata(
            channel_id, user_id, team_id, event_id, chat_type, is_dm
        )
        metadata.update(
            slack_message_ts=message_ts,
            slack_thread_ts=reply_thread_ts,
            message_ts=message_ts,
        )
        metadata["slack_trigger"] = trigger
        if silence_is_honoured:
            self._mark_reply_is_optional(metadata)
        if addressed:
            metadata[SLACK_ADDRESSED_KEY] = True

        add_slack_action_token(metadata, event, body)

        avatar_mode = await self._apply_avatar_context(
            metadata,
            is_dm=is_dm,
            channel_id=channel_id,
            user_id=user_id,
            text=text,
            message_ts=message_ts,
        )

        params: dict[str, Any] = {"content": text, "query": text}
        params.update(slack_agent_request_params(
            self.config, channel_id, user_id=user_id, chat_type=chat_type,
            workspace=self._identity.team_id,
        ))
        refusal = apply_turn_requirements(
            params, required_skills=required_skills,
            required_subagent=required_subagent,
            clear_extensions=clear_session_extensions,
        )
        if refusal:
            return refusal
        if downloaded:
            params["files"] = attachment_params(downloaded)
            params["media_items"] = downloaded
        # The adapter otherwise reuses the last model after a reset.
        model_name = self._channel_model_name(
            channel_id, user_id, chat_type
        ) or configured_models().default
        if model_name:
            params["model_name"] = model_name

        req = self._request(
            event_id or f"slack-{int(time.time() * 1000)}",
            session_id=session_id,
            chat_id=channel_id,
            user_id=user_id,
            req_method=ReqMethod.CHAT_SEND,
            params=params,
            metadata=metadata,
            # Keep question events even when visible reply streaming is off.
            is_stream=True,
            group_digital_avatar=avatar_mode,
            enable_memory=self.config.enable_memory if avatar_mode else None,
        )

        mid_turn = self._mid_turn_mode(
            channel_id,
            user_id,
            chat_type=chat_type,
            session_is_channel_wide=session_is_channel_wide,
            required_subagent=required_subagent is not None,
        )
        superseded = self.turn_initiator(session_id)
        running = superseded is not None

        async def queue_request(*, observe: bool) -> str:
            entry = _SlackQueuedMessage(
                request=req,
                session_id=session_id,
                user_id=user_id,
                team_id=team_id,
                is_dm=is_dm,
                chat_type=chat_type,
                channel_id=channel_id,
                message_ts=message_ts,
                reply_thread_ts=reply_thread_ts,
                root_thread_ts=root_thread_ts,
                preview_text=slack_inputs.clamp(preview_text, 240),
            )
            if not self._queue_mid_turn_message(entry):
                await self._reject_request(channel_id, message_ts)
                await self._explain_queue_refusal(channel_id, user_id)
                return f"refused:queue-full session={session_id}"
            if observe and self.config.decision_forward:
                self._observe_queued_decision(event, body, **decision_input)
            await self._mark_queued(entry)
            # Drain now in case the previous turn ended without a terminal event.
            await self._drain_queued_messages(session_id)
            return f"queued:{trigger} session={session_id}"

        def must_queue() -> bool:
            return self._admission.must_queue_message(
                session_id, mid_turn=mid_turn, turn_running=running,
                event_turn_active=session_id in self._admission.event_turn_sessions,
            )

        if must_queue():
            return await queue_request(observe=True)

        # Asked again below: fetching advice awaits, and a turn can start while
        # this one waits for it.
        decision_advice = await self._decision_advice_for_turn(
            event, body, **decision_input
        )
        superseded = self.turn_initiator(session_id)
        running = superseded is not None
        if must_queue():
            return await queue_request(observe=False)
        if decision_advice:
            content = append_turn_advice(
                str(req.params["content"]), decision_advice,
                suffixes=(NO_REPLY_INSTRUCTION, post_tool_instruction()),
            )
            req.params["content"] = content
            req.params["query"] = content

        if running and mid_turn == MID_TURN_STEER:
            params["input_mode"] = MID_TURN_STEER
            logger.info(
                "[SlackChannel] steering the running turn: session=%s"
                " request_id=%s user=%s",
                session_id,
                req.id,
                user_id,
            )
            if self._acknowledge_mode() != ACK_MODE_OFF:
                await self._set_steered_status(origin)
            await self._route_request(req)
            return f"steered:{trigger} session={session_id}"

        self._remember_stream_recipient(session_id, user_id, team_id)
        self._remember_turn_initiator(
            session_id,
            user_id,
            req.id,
            is_dm=is_dm,
            chat_type=chat_type,
            model_name=str(req.params.get("model_name") or ""),
            origin=origin,
            request_metadata=req.metadata,
            preview_text=preview_text,
        )

        # Withdraw old questions before the new request can receive a click.
        await self._withdraw_questions_for_session(session_id)

        if running:
            await self._end_superseded_turn(superseded, channel_id, req.id)

        await self._begin_activity_card(session_id)
        await self._route_request(req)
        return f"dispatched:{trigger} session={session_id}"
