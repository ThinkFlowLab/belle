"""What the schedule tools ask of the conversation host."""

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from belle_agent.backend import BackendCapabilities
from belle_agent.platforms.base import InboundMessage
from belle_agent.scheduling.address import Address
from belle_agent.scheduling.turnspec import ScopeLimits
from belle_agent.scheduling.when import Schedule, next_runs
from belle_agent.scopes import SECTION_AGENT

if TYPE_CHECKING:
    from belle_agent.host import ConversationHost
    from belle_agent.scheduling.runner import Runner
    from belle_agent.scheduling.toolkit import ScheduleToolkit


class StoredRecurrence:
    """Use the validated recurrence stored with each job."""

    def next_after(self, when: dict[str, Any], after: datetime) -> datetime | None:
        return next(iter(next_runs(Schedule(when), after, 1)), None)


class ScheduledTurns:
    """The host operations that the schedule tools need."""

    @property
    def _schedule_host(self) -> "ConversationHost":
        # ConversationHost is the sole class that uses this mixin.
        return cast("ConversationHost", self)

    def _schedule_components(self) -> tuple["ScheduleToolkit", "Runner"]:
        host = self._schedule_host
        toolkit, scheduler = host.schedule_toolkit, host.scheduler
        if toolkit is None or scheduler is None:
            raise RuntimeError("scheduler host is not running")
        return toolkit, scheduler

    @property
    def capabilities(self) -> BackendCapabilities:
        host = self._schedule_host
        return host.backend_capabilities

    def places(self, platform: str) -> Any:
        host = self._schedule_host
        connector = host.platforms.get(platform)
        return connector if connector is not None and hasattr(connector, "check_place") else None

    def scope_limits(self, place: Address, user: str, chat_type: str) -> ScopeLimits:
        host = self._schedule_host
        inbound = InboundMessage(place.platform, place.workspace, place.conversation, "", user, "", chat_type=chat_type)
        scope = host._turn_scope(inbound, {}, reply_optional=False)
        model = host._settle(inbound, SECTION_AGENT).get("model_name")
        return ScopeLimits(place, replace(scope, model=str(model or "").strip(), system_additions=()))
