"""What a platform's command surface asks of the conversation host.

A platform may give people commands of its own: Slack's slash command is one.
A command reads what runs and what waits in a conversation, removes a waiting
message, changes the model of a conversation, and reads what the backend
offers there. The host answers each of these here, from the state and the
scopes it already holds, so a command means the same on every platform and on
every backend. A platform receives the host through ``Platform.bind``, as a
``PlatformCommands``: what it reads and changes there is of its own
conversations only.

Two things a command does are no operation of this module, because they are
records like any other. Stopping a turn is an ``InboundStop``. A private turn
is an ``InboundMessage`` in the person's direct message, which names what the
turn requires and the side conversation it belongs to. Both pass the host's
receiver, so admission, the scopes, the queue and the bounds of a turn apply.

A held turn, as ``running`` and ``waiting`` return it, is the host's record of
one admitted message: ``conversation``, ``request_id``, ``inbound`` (the
message), and ``started_at`` or ``queued_at`` on the monotonic clock.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from belle_agent.backend import OP_STEER, BackendCapabilities, Catalog, TurnScope
from belle_agent.capability_catalog import scope_allows
from belle_agent.platforms.base import MARK_DROPPED, TRIGGER_DM, InboundDrop, InboundMessage
from belle_agent.scopes import (
    CLICK_QUEUE_DROP,
    CLICK_STOP,
    KEY_MID_TURN,
    KEY_SESSION,
    SECTION_AGENT,
    SECTION_DELIVERY,
    SESSION_CHANNEL,
)
from belle_agent.skills import packaged_skill_text
from belle_agent.turn_policy import mid_turn_mode

if TYPE_CHECKING:
    from belle_agent.host import ConversationHost, HostedPlatform

logger = logging.getLogger(__name__)

#: How long a command waits for the backend's catalog. The answer to a command is owed in seconds.
CATALOG_TIMEOUT_SECONDS = 8.0

SCOPE_FIELD_SKILLS_REQUIRED = "skills_required"
SCOPE_FIELD_SUBAGENT_REQUIRED = "subagent_required"


# Catalog commands use their first word as the catalog kind; values name any required scope field.
_COMMAND_FIELDS = {
    ("skills", "list"): "",
    ("skills", "use"): SCOPE_FIELD_SKILLS_REQUIRED,
    ("subagents", "list"): "",
    ("subagents", "use"): SCOPE_FIELD_SUBAGENT_REQUIRED,
    ("experts", "list"): "expert",
    ("models", "list"): "",
    ("models", "current"): "",
    ("models", "use"): "model",
    ("models", "reset"): "model",
}
_COMMAND_CANNOT: dict[str, str] = {
    SCOPE_FIELD_SKILLS_REQUIRED: "cannot require a skill of one turn",
    SCOPE_FIELD_SUBAGENT_REQUIRED: "cannot require a subagent of one turn",
    "expert": "applies no expert to a turn",
    "model": "cannot change the model of a conversation",
}


class HostCommands:
    """The command operations of ``ConversationHost``, which is their only base.

    An operation about a conversation takes ``platform``, which may be left out on a host of one platform.
    """

    #: What the host's own log lines name, and the platform of a name, or the one platform without a name.
    _label: str
    _on: Callable[..., "HostedPlatform"]
    #: Whether the host supplies the text of a required skill, and whether the backend supports an operation.
    _supplies_skills: bool
    _supports: Callable[[str], bool]

    @property
    def backend_capabilities(self) -> BackendCapabilities:
        """What the backend declares. Known once the host has started."""
        assert self._capabilities is not None
        return self._capabilities

    def command_unavailable(self, words: tuple[str, ...]) -> str:
        """Why a backend cannot provide a command, or an empty string."""
        if words[0] == "schedule":
            return "Belle scheduling is not active" if getattr(self, "schedule_toolkit", None) is None else ""
        if words[0] in {"bg", "sessions"}:
            return "Belle sessions are not active" if getattr(self, "session_toolkit", None) is None else ""
        field = _COMMAND_FIELDS.get(words)
        if field is None:
            return ""
        capabilities = self.backend_capabilities
        kind = words[0]
        if kind not in capabilities.catalog:
            return f"the {capabilities.backend} backend lists no {kind}"
        if field and field not in capabilities.scope_fields:
            return f"the {capabilities.backend} backend {_COMMAND_CANNOT[field]}"
        return ""

    def admits(self, sender: str, *, platform: str | None = None) -> bool:
        """Whether a person may ask anything of this deployment."""
        return self._sender_allowed(sender, platform)

    def serves(self, conversation: str, *, platform: str | None = None) -> bool:
        """Whether a shared conversation is served."""
        return self._conversation_allowed(conversation, platform)

    def _here(self, platform: str | None, workspace: str, conversation: str) -> tuple[str, str, str]:
        return self._on(platform).name, workspace, conversation

    def running(self, workspace: str, conversation: str, *, platform: str | None = None) -> list[Any]:
        """The turns that run in a conversation, across its threads and its side conversations."""
        here = self._here(platform, workspace, conversation)
        return [
            turn
            for key, turn in tuple(self._admission.turn_initiators.items())
            if (turn.inbound.platform, turn.inbound.workspace, turn.inbound.conversation) == here
            and self._live_turn(key) is turn
        ]

    def waiting(self, workspace: str, conversation: str, *, platform: str | None = None) -> list[Any]:
        """The messages that wait for a turn in a conversation, oldest first."""
        here = self._here(platform, workspace, conversation)
        return sorted(
            (
                turn
                for queue in self._admission.queued_messages.values()
                for turn in queue
                if (turn.inbound.platform, turn.inbound.workspace, turn.inbound.conversation) == here
            ),
            key=lambda turn: turn.queued_at,
        )

    def held(self, workspace: str, conversation: str, *, platform: str | None = None) -> int:
        """How many tool calls of a conversation wait for a person to agree."""
        here = self._here(platform, workspace, conversation)
        return sum(
            (call.turn.inbound.platform, call.turn.inbound.workspace, call.turn.inbound.conversation) == here
            for call in self._held.values()
        )

    def may_stop(self, turn: Any, sender: str) -> tuple[bool, str]:
        """Whether a person may stop a running turn, and on what ground. ``InboundStop`` is held to the same."""
        return self._may_click(CLICK_STOP, turn, sender)

    def may_drop(self, turn: Any, sender: str) -> tuple[bool, str]:
        """Whether a person may remove a waiting message: its sender, or whoever a ``clicks.queue_drop`` rule names."""
        return self._may_click(CLICK_QUEUE_DROP, turn, sender, unruled=False)

    async def _receive_drop(self, drop: InboundDrop) -> str:
        if not self._running:
            return "dropped:host-not-running"
        turn = next(
            (
                waiting
                for waiting in self.waiting(drop.workspace, drop.conversation, platform=drop.platform)
                if waiting.inbound.message_id == drop.message_id
            ),
            None,
        )
        if turn is None:
            return "ignored:no-message-waiting"
        permitted, ground = self.may_drop(turn, drop.sender)
        if not permitted:
            return f"refused:{ground}"
        queue = self._admission.queued_messages[turn.conversation]
        queue.remove(turn)
        if not queue:
            del self._admission.queued_messages[turn.conversation]
        await self._mark_turn(turn, MARK_DROPPED)
        return f"removed:waiting-message conversation={turn.conversation}"

    def mid_turn(self, at: InboundMessage) -> str:
        """What becomes of a message that arrives during a turn here: it waits, stops the turn, or joins it."""
        delivery = self._settle(at, SECTION_DELIVERY)
        shared = at.trigger != TRIGGER_DM and delivery.get(KEY_SESSION) == SESSION_CHANNEL
        # As at admission: where the backend cannot steer, steer queues.
        return mid_turn_mode(
            delivery.get(KEY_MID_TURN), shared_session=shared, requires_separate_turn=not self._supports(OP_STEER)
        )

    def settled(self, at: InboundMessage) -> TurnScope:
        """What the scopes settle for the person and the place ``at`` names, as the next turn there would have it.

        The model is the scope's, or else the one chosen for the conversation.
        """
        return self._turn_scope(at, {}, reply_optional=False)

    def scoped_model(self, at: InboundMessage) -> str:
        """The model a scope selects there, which no choice of a person's replaces."""
        return str(self._settle(at, SECTION_AGENT).get("model_name") or "").strip()

    async def catalog(self, *, timeout: float = CATALOG_TIMEOUT_SECONDS) -> Catalog | None:
        """Read what the backend lists. ``None``: it lists nothing, or did not answer in time.

        It is also the only question a command can put to a backend, so a status reads it as the health check.
        """
        # ponytail: a backend that lists nothing is never asked, and reads as not answering. Give the port a
        # health operation when such a backend exists.
        capabilities = self.backend_capabilities
        if not capabilities.catalog:
            return None
        try:
            return await asyncio.wait_for(self._backend.catalog(), timeout)
        except Exception as exc:  # noqa: BLE001 - a catalog that cannot be read ends one command, not the host
            # The type only: a backend's error can quote what it was reading.
            logger.warning(
                "%s could not read the catalog of backend %s: %s",
                self._label, capabilities.backend, type(exc).__name__,
            )
            return None

    def _unmet_requirement(self, inbound: InboundMessage, scope: TurnScope) -> str:
        """Name what a message requires of its turn that the turn cannot have, or nothing."""
        applied = self.backend_capabilities.scope_fields
        if inbound.skills_required:
            # Where the host supplies a skill's text, it has the text of a packaged skill only.
            unpackaged = self._supplies_skills and any(packaged_skill_text(name) is None for name in inbound.skills_required)
            if SCOPE_FIELD_SKILLS_REQUIRED not in applied or unpackaged:
                return f"refused:backend-does-not-apply-{SCOPE_FIELD_SKILLS_REQUIRED}"
            allowed = (scope_allows(name, scope.skills_available, scope.skills_unavailable) for name in inbound.skills_required)
            if not all(allowed):
                return "refused:skill-not-available-here"
        if denied := [name for name in scope.skills_required if name in scope.skills_unavailable]:
            logger.warning(
                "scopes: agent.skills.required and agent.skills.unavailable both name %s for %s conversation %s."
                " A required skill must be usable, so the turn is refused",
                ", ".join(denied), inbound.platform, inbound.conversation,
            )
            return "refused:skill-required-and-unavailable"
        if inbound.subagent_required is not None:
            if SCOPE_FIELD_SUBAGENT_REQUIRED not in applied:
                return f"refused:backend-does-not-apply-{SCOPE_FIELD_SUBAGENT_REQUIRED}"
            if scope.subagents_available is not None and inbound.subagent_required[0] not in scope.subagents_available:
                return "refused:subagent-not-available-here"
        return ""


class PlatformCommands:
    """The command operations as one platform's command surface reaches them: about its own conversations only.

    Every other member is the host's.
    """

    def __init__(self, host: "ConversationHost", platform: str) -> None:
        self._host = host
        self._platform = platform

    def __getattr__(self, name: str) -> Any:
        """Read every other member from the host."""
        return getattr(self._host, name)

    @property
    def model_choices(self) -> dict[tuple[str, str], str]:
        """The model a person chose for each conversation of this platform: see ``host.HostedPlatform``."""
        return self._host._on(self._platform).model_choices

    def admits(self, sender: str) -> bool:
        """See ``HostCommands.admits``."""
        return self._host.admits(sender, platform=self._platform)

    def serves(self, conversation: str) -> bool:
        """See ``HostCommands.serves``."""
        return self._host.serves(conversation, platform=self._platform)

    def running(self, workspace: str, conversation: str) -> list[Any]:
        """See ``HostCommands.running``."""
        return self._host.running(workspace, conversation, platform=self._platform)

    def waiting(self, workspace: str, conversation: str) -> list[Any]:
        """See ``HostCommands.waiting``."""
        return self._host.waiting(workspace, conversation, platform=self._platform)

    def held(self, workspace: str, conversation: str) -> int:
        """See ``HostCommands.held``."""
        return self._host.held(workspace, conversation, platform=self._platform)

    def events_of(self, workspace: str, conversation: str) -> tuple[bool, int, int]:
        """See ``EventAdmission.events_of``."""
        return self._host.events_of(workspace, conversation, platform=self._platform)
