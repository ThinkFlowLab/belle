"""Refuse a JiuwenSwarm job whose runs cannot reach the Belle channel they are meant for."""

import json
from collections.abc import Callable, Mapping
from typing import Any

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    BaseInterruptRail,
    InterruptDecision,
)

from belle_agent.integrations.jiuwenswarm.slack_write_confirmation_rail import (
    RAIL_PRIORITY as CONFIRMATION_PRIORITY,
)
from belle_agent.integrations.jiuwenswarm.slack_write_confirmation_rail import call_arguments
from belle_agent.platforms.matrix.capabilities import NAME as MATRIX
from belle_agent.platforms.mattermost.capabilities import NAME as MATTERMOST

#: Belle's channels that no JiuwenSwarm job reaches, by channel id. JiuwenSwarm sets the limit for a
#: scheduled job: a target needs a delivery address, and its extension SDK has no address type for an
#: extension's channel, so it pushes such a job to its web channel instead. A heartbeat run is a turn
#: that the AgentServer starts, and these channels post only the turns that they started.
UNREACHED_CHANNELS = {MATTERMOST: "Mattermost", MATRIX: "Matrix"}

#: The combined tool: its ``add`` creates a job and its ``update`` changes one.
CRON = "cron"
CRON_CREATE = "cron_create_job"
CRON_UPDATE = "cron_update_job"
#: A heartbeat job is bound to the session of the turn that creates it.
HEARTBEAT_CREATE = "heartbeat_create_job"

#: The arguments of ``cron`` that are not fields of a job or patch given flat, as OpenJiuwen reads them.
_CRON_CALL_KEYS = frozenset({
    "action", "job", "jobId", "id", "patch", "includeDisabled", "text", "mode", "contextMessages",
    "gatewayUrl", "gatewayToken", "timeoutMs", "runMode",
})

#: Above the confirmations: nobody is asked to approve a job that is then refused.
RAIL_PRIORITY = CONFIRMATION_PRIORITY + 1

CRON_TARGET_UNAVAILABLE = "cron_target_unavailable"
HEARTBEAT_UNAVAILABLE = "heartbeat_unavailable"

_CRON_DETAIL = (
    "The job's delivery channel {given} {means}, and JiuwenSwarm cannot deliver a scheduled job to"
    " {platform}, so nothing was scheduled.{heartbeat} Name another channel that delivers scheduled jobs,"
    " or tell the person that a job cannot post to {platform}"
)
_HEARTBEAT_ALSO = " A heartbeat follow-up cannot post here either."
_HEARTBEAT_DETAIL = (
    "A heartbeat follow-up of this {platform} conversation would run as a turn that Belle's {platform}"
    " channel does not post, so nothing was scheduled. A scheduled job cannot post here either. Tell the"
    " person that a follow-up cannot post here"
)


def _job_fields(tool_name: str, arguments: Mapping[str, Any]) -> tuple[Any, bool]:
    """The cron job or patch that a call writes, or ``None``, and whether the call creates the job."""
    if tool_name == CRON_CREATE:
        return arguments, True
    if tool_name == CRON_UPDATE:
        return arguments.get("patch"), False
    action = str(arguments.get("action") or "").strip().lower()
    if action not in ("add", "update"):
        return None, False
    nested = arguments.get("job" if action == "add" else "patch")
    # OpenJiuwen takes the job or patch nested, or flat where the nested one is absent or empty.
    fields = nested or {key: value for key, value in arguments.items() if key not in _CRON_CALL_KEYS}
    return fields, action == "add"


def _target_of(fields: Mapping[str, Any], creates: bool) -> tuple[bool, Any]:
    """Whether the fields set the job's target, and the value they set.

    JiuwenSwarm reads ``delivery.channel`` before ``targets``. A new job without either takes the turn's channel.
    """
    delivery = fields.get("delivery")
    delivery = delivery if isinstance(delivery, Mapping) else {}
    sets = creates or "channel" in delivery or "targets" in fields
    return sets, delivery.get("channel") or fields.get("targets")


def _refusal(code: str, detail: str) -> str:
    return json.dumps({"ok": False, "error": code, "detail": detail}, ensure_ascii=False)


class JobDeliveryRail(BaseInterruptRail):
    """Refuse a job for a channel of ``UNREACHED_CHANNELS``.

    That is a cron job that names such a channel, from any turn. In a turn of such a channel, it is
    also a cron job that names no channel, and a heartbeat job. ``channel_id`` names the channel of the
    request in flight.
    """

    priority: int = RAIL_PRIORITY

    def __init__(self, channel_id: Callable[[], str]) -> None:
        super().__init__(tool_names=(CRON, CRON_CREATE, CRON_UPDATE, HEARTBEAT_CREATE))
        self._channel_id = channel_id

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: ToolCall | None,
        user_input: Any | None,
        auto_confirm_config: dict | None = None,
    ) -> InterruptDecision:
        """Approve every call but one that schedules a run for a channel that no run reaches.

        A cron call whose arguments cannot be read reaches the tool, which refuses it in its own words.
        """
        here = UNREACHED_CHANNELS.get(str(self._channel_id() or "").strip())
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if tool_name == HEARTBEAT_CREATE:
            if here is None:
                return self.approve()
            return self.reject(tool_result=_refusal(HEARTBEAT_UNAVAILABLE, _HEARTBEAT_DETAIL.format(platform=here)))
        arguments = call_arguments(ctx, tool_call)
        if arguments is None:
            return self.approve()
        fields, creates = _job_fields(tool_name, arguments)
        if not isinstance(fields, Mapping):
            return self.approve()
        sets, target = _target_of(fields, creates)
        if not sets:
            return self.approve()
        # JiuwenSwarm reads a channel as str(value).strip(), and a channel id without regard to case.
        named = str(target or "").strip().lower()
        if named in UNREACHED_CHANNELS:
            platform = UNREACHED_CHANNELS[named]
            means = f"names {platform}"
        elif not named and here is not None:
            platform, means = here, f"means this {here} conversation"
        else:
            return self.approve()
        given = "left out" if target is None else json.dumps(target, ensure_ascii=False)
        detail = _CRON_DETAIL.format(
            given=given, means=means, platform=platform, heartbeat=_HEARTBEAT_ALSO if here else ""
        )
        return self.reject(tool_result=_refusal(CRON_TARGET_UNAVAILABLE, detail))


__all__ = [
    "CRON_TARGET_UNAVAILABLE",
    "HEARTBEAT_UNAVAILABLE",
    "RAIL_PRIORITY",
    "UNREACHED_CHANNELS",
    "JobDeliveryRail",
]
