"""Confirm Slack writes that widen an audience or change access."""

import json
import logging
from typing import Any

from jiuwenswarm.extensions.sdk.permission_options import (
    ALLOW_ONCE,
    REJECT,
    resolve_permission_action,
)
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    BaseInterruptRail,
    InterruptDecision,
)

from belle_agent.integrations.jiuwenswarm.slack_post import (
    WIDENING_TOOL_NAMES,
)

logger = logging.getLogger(__name__)

#: Above the permission rail's 90, so that a turn which has to answer both
#: questions is asked the specific one -- "these four people will see it" --
#: rather than the generic "approve post_message?", which says nothing the
#: specific one does not.
RAIL_PRIORITY = 95

#: The two buttons. Values come from the shared vocabulary so that the side
#: rendering them and the side parsing the answer back agree: an unrecognised
#: answer is a refusal, and a person clicking "yes" and having the call refused
#: is the failure this shared table prevents.
_OPTIONS: list[dict[str, str]] = [
    {
        "value": ALLOW_ONCE,
        "label": "Post it",
        "description": "Send the message to that conversation",
    },
    {
        "value": REJECT,
        "label": "Do not post it",
        "description": "Leave the message unsent",
    },
]

_DECLINED = (
    "the requester declined this change. Do not call the same tool again with"
    " these arguments unless the requester starts a new turn"
)


def call_arguments(
    ctx: AgentCallbackContext, tool_call: ToolCall | None
) -> "dict[str, Any] | None":
    """The call's arguments as a mapping, or ``None`` if they cannot be read.

    The rail's own ``tool_args``, which an earlier rail may have rewritten,
    wins over the tool call's: the arguments that will be sent are the ones
    worth judging.
    """
    raw = getattr(ctx.inputs, "tool_args", None)
    if raw is None and tool_call is not None:
        raw = getattr(tool_call, "arguments", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class SlackWriteConfirmationRail(BaseInterruptRail):
    """Ask the turn's requester before a Slack write widens access.

    ``toolkit`` answers ``widening_question`` for each of ``tool_names``.
    """

    priority: int = RAIL_PRIORITY

    def __init__(
        self,
        toolkit: Any,
        *,
        tools: list[Any] | None = None,
        tool_names: frozenset[str] = WIDENING_TOOL_NAMES,
        access_change: bool = False,
    ) -> None:
        super().__init__(tool_names=sorted(tool_names))
        self._toolkit = toolkit
        self._owned_cards = {tool.card.name: tool.card for tool in tools or ()}
        self._options = (
            [{**_OPTIONS[0], "label": "Approve", "description": "Allow this action"},
             {**_OPTIONS[1], "label": "Reject", "description": "Leave it undone"}]
            if access_change else _OPTIONS
        )

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: ToolCall | None,
        user_input: Any | None,
        auto_confirm_config: dict | None = None,
    ) -> InterruptDecision:
        """Approve, ask, or refuse one call of ``post_message`` or ``edit_message``.

        ``auto_confirm_config`` is accepted and not read: what is approved here
        is an audience, and an audience is what differs between two calls.

        Everything that is not a question approves. A call whose arguments are
        malformed, whose deployment holds no audience bound, or whose membership
        could not be read reaches the tool, which refuses it in its own shape
        with its own code. Asking somebody to approve a call that was never
        going to happen teaches them to click through.
        """
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if self._owned_cards:
            manager = getattr(getattr(ctx, "agent", None), "ability_manager", None)
            if manager is not None and manager.get(tool_name) is not self._owned_cards.get(tool_name):
                return self.approve()
        arguments = call_arguments(ctx, tool_call)

        try:
            question = await self._toolkit.widening_question(tool_name, arguments)
        except Exception:
            # The toolkit re-runs the same check and refuses the call itself, so
            # approving here is not a decision to post: it is a decision not to
            # ask about something that is about to fail.
            logger.exception(
                "[SlackWriteConfirmationRail] the audience check raised for"
                " tool=%s; the tool itself will refuse the call",
                tool_name,
            )
            return self.approve()

        if not question:
            return self.approve()
        if user_input is None:
            return self._ask(question)

        approved = self._approved(user_input)
        if approved is None:
            # The answer came back in a shape nothing here recognises. Asked
            # again rather than read as either answer: a mis-parsed "yes" posts
            # something nobody agreed to, and a mis-parsed "no" reports a
            # refusal the person never made.
            logger.warning(
                "[SlackWriteConfirmationRail] the answer to a widening"
                " confirmation could not be read (%s); asking again",
                type(user_input).__name__,
            )
            return self._ask(question)
        if approved:
            return self.approve()
        return self.reject(
            tool_result=json.dumps(
                {
                    "ok": False,
                    "error": "write_confirmation_declined",
                    "detail": _DECLINED,
                },
                ensure_ascii=False,
            )
        )

    def _ask(self, question: str) -> InterruptDecision:
        """Put the widening question to the person who started the turn."""
        return self.interrupt(
            InterruptRequest(
                message=question,
                payload_schema=ConfirmPayload.to_schema(),
                ui_options=list(self._options),
            )
        )

    @staticmethod
    def _approved(user_input: Any) -> "bool | None":
        """Whether the person agreed, or ``None`` when the answer is unreadable.

        Three shapes reach here, one per client family: the structured
        ``ConfirmPayload`` the web and ACP paths build, the mapping a JSON
        transport leaves it as, and the bare option string Slack's buttons and
        the TUI send back. The last goes through the shared vocabulary, so a
        client sending the label where another sends the value is understood
        either way.
        """
        if isinstance(user_input, ConfirmPayload):
            return bool(user_input.approved)
        if isinstance(user_input, dict):
            if "approved" in user_input:
                try:
                    return bool(ConfirmPayload.model_validate(user_input).approved)
                except Exception:  # noqa: BLE001 - pydantic error types vary.
                    return None
            for key in ("value", "label", "option", "answer"):
                if user_input.get(key):
                    return SlackWriteConfirmationRail._approved(user_input[key])
            return None
        if isinstance(user_input, str):
            action = resolve_permission_action(user_input)
            if action is None:
                return None
            return action != REJECT
        return None


__all__ = ["RAIL_PRIORITY", "SlackWriteConfirmationRail", "call_arguments"]
