"""Admit platform events as context or independent turns."""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from belle_agent.admission.buffer import InboundEventBuffer
from belle_agent.admission.policy import EVENT_OFF, EVENT_TURN, KEY_EVENTS, event_disposition
from belle_agent.admission.state import AdmissionState
from belle_agent.platforms.base import EventKind, InboundEvent
from belle_agent.scopes import (
    KEY_REPLY,
    KEY_SESSION,
    REPLY_OPTIONAL,
    SECTION_DELIVERY,
    SESSION_CHANNEL,
    Scope,
    compose_section,
)
from belle_agent.turn_policy import conversation_key

if TYPE_CHECKING:
    from belle_agent.host import HostedPlatform

logger = logging.getLogger(__name__)

# What the turn of an event is told, on every platform. An event does not address the bot. Conversation
# instructions may override the silent default. The destination depends on the event's anchor.
EVENT_TURN_RANKING = (
    "That event started this turn. Nobody addressed you and nothing above is a"
    " question. Staying silent is the right outcome for an event unless this"
    " conversation's own instructions say that an event of this kind is a"
    " request; nothing else overrides that."
)
# A reply to an event that names no message appears at channel level.
EVENT_TURN_DESTINATION_ROOT = (
    " If you do answer, your reply is posted at the top level of this channel,"
    " where everyone in it sees it rather than only the people following a"
    " thread, and that is the only place this turn can post anything."
)

# The bounds the JiuwenSwarm Slack channel applies to the same state.
MAX_PENDING_EVENTS_PER_KEY = 256
MAX_PENDING_EVENTS = 4096
PENDING_EVENT_TIMEOUT_SECONDS = 86400.0
EVENT_CONTEXT_TTL_SECONDS = 3600.0


@dataclass
class _PendingEvent:
    """One event that waits for a turn of its own."""

    event: InboundEvent
    kind: EventKind
    conversation: str
    arrival_seq: int
    queued_at: float = field(default_factory=time.monotonic)
    loss_logged: bool = False


def _place(workspace: str, conversation: str) -> str:
    """Name the one conversation whose turns are told an event."""
    return f"{workspace}:{conversation}"


class EventAdmission:
    """The part of the conversation host that takes events.

    The host supplies the turns: ``_occupied(key)``, ``_start_event_turn(event, key)``,
    which returns the request id or the refusal of the turn, and ``_drain(key)``.
    """

    #: Each platform the host serves, by name: see ``host.HostedPlatform``.
    _hosted: Mapping[str, "HostedPlatform"]
    _on: Callable[..., "HostedPlatform"]
    _scopes: tuple[Scope, ...]
    _admission: AdmissionState
    _running: bool

    def _init_events(self) -> None:
        # Held in memory only, one buffer per platform. A restart starts every conversation with none.
        self._event_context = {
            name: InboundEventBuffer(
                limits={kind_name: kind.held for kind_name, kind in hosted.platform.event_kinds.items()},
                ttl_seconds=EVENT_CONTEXT_TTL_SECONDS,
            )
            for name, hosted in self._hosted.items()
        }
        self._reported_required_reply: set[tuple[str, str, str]] = set()

    @property
    def lost_events(self) -> dict[tuple[str, str], int]:
        """How many events each conversation of the one platform this host serves lost. See ``HostedPlatform``."""
        return self._on().lost_events

    def _stop_events(self) -> None:
        """Say which waiting events the stop loses, and keep none for a later start."""
        for queue in self._admission.pending_events.values():
            for entry in queue:
                self._log_pending(entry, "host-stopped")
        for arrivals in self._admission.admitting_events.values():
            for entry in arrivals.values():
                self._log_pending(entry, "host-stopped-during-admission")
        self._reported_required_reply.clear()
        for hosted in self._hosted.values():
            hosted.lost_events.clear()
        for buffer in self._event_context.values():
            buffer.forget()

    def events_of(self, workspace: str, conversation: str, *, platform: str | None = None) -> tuple[bool, int, int]:
        """What a status says of a conversation's events: whether the turn of one runs there, how many wait
        for a turn, and how many were lost since the start. ``platform`` may be left out on a host of one."""
        hosted = self._on(platform)
        admission, place = self._admission, (hosted.name, workspace, conversation)
        running = any(
            admission.event_turn_sessions.get(key) == turn.request_id
            for key, turn in admission.turn_initiators.items()
            if (turn.inbound.platform, turn.inbound.workspace, turn.inbound.conversation) == place
        )
        waiting = {
            id(entry)
            for held in (*admission.pending_events.values(), *(arrivals.values() for arrivals in admission.admitting_events.values()))
            for entry in held
            if (entry.event.platform, entry.event.workspace, entry.event.conversation) == place
        }
        return running, len(waiting), hosted.lost_events.get((workspace, conversation), 0)

    # ── context ──────────────────────────────────────────────────────────

    def _keep(self, event: InboundEvent) -> None:
        self._event_context[event.platform].record(_place(event.workspace, event.conversation), event.kind, event.record)

    def _keep_instead(self, event: InboundEvent, reason: str, explanation: str) -> str:
        self._keep(event)
        logger.info(
            "%s event buffered instead of waking a turn: kind=%s conversation=%s reason=%s -- %s",
            event.platform, event.kind, event.conversation, reason, explanation,
        )
        return f"buffered:{event.kind} (turn-downgraded:{reason})"

    def _events_text(self, platform: str, workspace: str, conversation: str, started_by: InboundEvent | None) -> str:
        """Take what a conversation kept since its last turn, as its next turn is told it.

        Events are kept under the conversation they happened in and taken once,
        so a turn anywhere else is told none of them, and neither is a later
        turn here.
        """
        held = self._event_context[platform].fold(_place(workspace, conversation))
        if not held and started_by is None:
            return ""
        return self._hosted[platform].platform.render_events({**held, "chat_id": conversation} if held else None, started_by)

    # ── admission ────────────────────────────────────────────────────────

    async def _take_event(self, event: InboundEvent, kind: EventKind) -> str:
        # The one who did it is not a sender: the conversation's rule is read with nobody in that place.
        delivery = compose_section(
            self._scopes,
            channel=event.platform,
            chat=event.conversation,
            chat_type=event.chat_type or None,
            workspace=event.workspace or None,
            section=SECTION_DELIVERY,
        )
        disposition = event_disposition(delivery.get(KEY_EVENTS), event.kind, may_turn=kind.turns)
        if disposition == EVENT_OFF:
            return f"ignored:events-off-for-{event.kind}-here"
        if disposition == EVENT_TURN:
            return await self._wake(event, kind, delivery)
        self._keep(event)
        return f"buffered:{event.kind}"

    async def _fresh(self, event: InboundEvent) -> bool:
        if not event.event_id:
            return True
        return await self._hosted[event.platform].dedup.remember(f"{event.workspace}:{event.conversation}:{event.kind}:{event.event_id}")

    async def _wake(self, event: InboundEvent, kind: EventKind, delivery: Mapping[str, Any]) -> str:
        """Start a turn for an event, hold the event until one can start, or keep it as context."""

        def unavailable(reason: str, explanation: str) -> str:
            if kind.waits:
                self._log_loss(event, reason=reason)
                return f"dropped:{event.kind}-{reason}"
            return self._keep_instead(event, reason, explanation)

        if kind.anchored and not event.anchor:
            return unavailable("no-anchor", "the event names no message to answer")
        if not event.workspace:
            return unavailable("no-workspace", "the event named no workspace, so it has no session of its own")
        # A turn nobody asked for must be free to say nothing, and a direct message is always owed an answer.
        if event.direct or delivery.get(KEY_REPLY) != REPLY_OPTIONAL:
            told = (event.platform, event.conversation, event.kind)
            if kind.waits and told not in self._reported_required_reply:
                self._reported_required_reply.add(told)
                logger.warning(
                    "%s %s turn events in %s use context because delivery.reply is required;"
                    " set it to optional for independent event turns",
                    event.platform, event.kind, event.conversation,
                )
            return self._keep_instead(
                event,
                "reply-required",
                "a required reply would oblige this turn to speak about the event; set delivery.reply to"
                " optional for this conversation to let an event turn stay silent",
            )
        # An event about no message has no thread to give it a session of its own.
        key = conversation_key(
            platform=event.platform,
            workspace=event.workspace,
            conversation=event.conversation,
            sender=event.actor,
            thread=event.anchor_thread or event.anchor,
            direct=False,
            shared_session=not kind.anchored or delivery.get(KEY_SESSION) == SESSION_CHANNEL,
        )
        if not kind.waits:
            fresh = await self._fresh(event)
            if not self._running:
                return f"dropped:{event.kind}-host-stopped"
            if not fresh:
                return f"ignored:already-handled (dedupe) kind={event.kind}"
            return self._dispatch_event(event, kind, key)

        admission = self._admission
        # A question waits inside its running turn on this host, so a running turn covers an awaited answer.
        reserved = admission.can_reserve(key, turn_running=self._occupied(key), awaiting_answer=False)
        pending = _PendingEvent(event=event, kind=kind, conversation=key, arrival_seq=admission.next_arrival())
        self._prune_pending()
        if (
            admission.pending_count(key) >= MAX_PENDING_EVENTS_PER_KEY
            or admission.pending_count() >= MAX_PENDING_EVENTS
        ):
            self._log_pending(pending, "admission-full")
            return f"dropped:pending-{event.kind}-full"
        admission.track_admitting(pending.conversation, pending)
        logger.info(
            "%s event turn admission: event_id=%s kind=%s actor=%s conversation=%s anchor=%s key=%s",
            event.platform, event.event_id, event.kind, event.actor, event.conversation, event.anchor, key,
        )
        if reserved:
            admission.event_admissions.add(key)
        try:
            fresh = await self._fresh(event)
            if not self._running:
                self._log_pending(pending, "host-stopped-during-admission")
                return f"dropped:{event.kind}-host-stopped"
            if not fresh:
                return f"ignored:already-handled (dedupe) kind={event.kind}"
            return self._dispatch_pending(pending, reserved=reserved)
        except BaseException:
            self._log_pending(pending, "admission-error")
            raise
        finally:
            admission.finish_admission(key, pending.arrival_seq, reserved)
            if self._running and not self._occupied(key):
                self._drain(key)

    def _dispatch_pending(self, pending: _PendingEvent, *, reserved: bool) -> str:
        key = pending.conversation
        if self._admission.should_hold(
            key, pending.arrival_seq, reserved=reserved, turn_running=self._occupied(key), awaiting_answer=False
        ):
            return self._hold(pending)
        return self._dispatch_event(pending.event, pending.kind, key)

    def _dispatch_event(self, event: InboundEvent, kind: EventKind, key: str) -> str:
        if self._occupied(key):
            return self._keep_instead(event, "turn-in-flight", f"a turn is already running in conversation={key}")
        request_id, refused = self._start_event_turn(event, key)
        if refused:
            return refused
        if kind.waits:
            # A message that arrives while this turn runs waits for it, whatever ``mid_turn`` says.
            self._admission.event_turn_sessions[key] = request_id
        logger.info(
            "%s event woke a turn: kind=%s conversation=%s anchor=%s actor=%s key=%s request_id=%s",
            event.platform, event.kind, event.conversation, event.anchor or "top-level", event.actor, key, request_id,
        )
        return f"dispatched:event:{event.kind} conversation={key}"

    def _hold(self, pending: _PendingEvent) -> str:
        self._prune_pending()
        event, key = pending.event, pending.conversation
        if not self._admission.hold_event(
            pending.conversation, pending, max_per_session=MAX_PENDING_EVENTS_PER_KEY, max_total=MAX_PENDING_EVENTS
        ):
            self._log_pending(pending, "queue-full")
            return f"dropped:pending-{event.kind}-full"
        logger.info(
            "%s event turn pending: event_id=%s kind=%s actor=%s conversation=%s anchor=%s key=%s pending=%s",
            event.platform, event.event_id, event.kind, event.actor, event.conversation, event.anchor, key,
            len(self._admission.pending_events[key]),
        )
        self._drain(key)
        return f"held:{event.kind} conversation={key}"

    def _drain_events(self, key: str) -> None:
        """Start the oldest event that waits on ``key``, while nothing else holds the key."""
        self._prune_pending()
        admission = self._admission
        while (
            self._running
            and admission.pending_events.get(key)
            and not self._occupied(key)
            and key not in admission.event_admissions
        ):
            pending = admission.take_pending_event(key)
            if pending is None:
                return
            try:
                outcome = self._dispatch_pending(pending, reserved=True)
            except BaseException:
                self._log_pending(pending, "dispatch-error")
                raise
            finally:
                admission.event_admissions.discard(key)
            if not outcome.startswith(("dispatched:", "buffered:", "held:")):
                self._log_pending(pending, f"dispatch-{outcome}")

    # ── losses ───────────────────────────────────────────────────────────

    def _prune_pending(self) -> None:
        for entry in self._admission.prune_events(cutoff=time.monotonic() - PENDING_EVENT_TIMEOUT_SECONDS):
            self._log_pending(entry, "expired")

    def _log_pending(self, pending: _PendingEvent, reason: str) -> None:
        if pending.loss_logged:
            return
        pending.loss_logged = True
        self._log_loss(pending.event, key=pending.conversation, reason=reason)

    def _log_loss(self, event: InboundEvent, *, reason: str, key: str = "") -> None:
        """Count and name an event that will get no turn. It is not kept as context for anyone."""
        lost, place = self._hosted[event.platform].lost_events, (event.workspace, event.conversation)
        lost[place] = lost.get(place, 0) + 1
        logger.warning(
            "%s event turn lost: event_id=%s kind=%s actor=%s conversation=%s anchor=%s key=%s reason=%s;"
            " recover manually",
            event.platform, event.event_id, event.kind, event.actor, event.conversation, event.anchor,
            key, reason,
        )
