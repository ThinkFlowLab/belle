"""What the Slack connector shows of a running turn: the platform port's optional members.

``shows`` says which kinds of event the connector shows as it is configured.
The host says when a turn starts through ``begin``, hands those events over
through ``progress``, and says how the turn ended through ``conclude``. The reply as it is written is ``turn_stream``.
What the turn is doing is ``turn_activity``.
"""

from collections.abc import Mapping
from typing import Any

from belle_agent.backend import EVENT_STEP, EVENT_TEXT_DELTA, EVENT_TOOL_CALLED, EVENT_TOOL_RESULT, TurnEvent
from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.slack.activity import STOP_ACTION_ID
from belle_agent.platforms.slack.turn_activity import ACTIVITY_SETTING, SlackTurnActivity
from belle_agent.platforms.slack.turn_stream import STREAMING_SETTING, SlackTurnStream


class SlackTurnSurfaces(SlackTurnStream, SlackTurnActivity):
    def _init_turn_surfaces(self) -> None:
        self._init_streams()
        self._init_activity()

    def _register_turn_surfaces(self, app: Any) -> None:
        app.action(STOP_ACTION_ID)(self._on_stop_action)

    def _clear_turn_surfaces(self) -> None:
        self._clear_streams()
        self._activity_state.clear()

    @property
    def shows(self) -> Mapping[str, str]:
        shown: dict[str, str] = {}
        if self._streams_replies():
            shown.update({EVENT_TEXT_DELTA: STREAMING_SETTING, EVENT_STEP: STREAMING_SETTING})
        if self.config.activity_card:
            shown.update({EVENT_TOOL_CALLED: ACTIVITY_SETTING, EVENT_TOOL_RESULT: ACTIVITY_SETTING})
        return shown

    async def begin(self, inbound: InboundMessage) -> None:
        if self.config.activity_card:
            await self._begin_activity(inbound)

    async def progress(self, inbound: InboundMessage, event: TurnEvent) -> None:
        if event.kind in (EVENT_TEXT_DELTA, EVENT_STEP):
            await self._show_reply(inbound, event)
        elif event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT):
            await self._show_activity(inbound, event)

    async def conclude(self, inbound: InboundMessage, state: str, final: str = "") -> bool:
        """End the reply that was shown, then settle the card. Return whether the reply became ``final``."""
        try:
            return await self._end_reply(inbound, state, final)
        finally:
            await self._end_activity(inbound, state)
