"""Keep licensed silence replies out of stored conversation history."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from belle_agent.silence import is_no_reply_sentinel, silence_is_unstored

logger = logging.getLogger(__name__)


class SilenceRail(DeepAgentRail):
    """Drop a final response that is only the silence token."""

    def __init__(
        self,
        metadata_provider: Callable[[], Mapping[str, Any] | None],
    ) -> None:
        super().__init__()
        self._metadata_provider = metadata_provider

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """End the turn before its final message reaches the context."""
        if not silence_is_unstored(self._read_metadata()):
            return
        response = getattr(getattr(ctx, "inputs", None), "response", None)
        content = getattr(response, "content", None)
        if not isinstance(content, str) or not is_no_reply_sentinel(content):
            return
        if getattr(response, "tool_calls", None):
            # Not a final response: the loop runs these tools next.
            return
        if ctx.has_pending_steering():
            # Somebody wrote while the model was answering. The turn has more to
            # do, so it stays on the ordinary path and answers again.
            return
        ctx.request_force_finish({"output": content, "result_type": "answer"})
        logger.info(
            "[SilenceRail] silence-only reply left out of the stored"
            " conversation: session=%s",
            self._session_id(ctx),
        )

    @staticmethod
    def _session_id(ctx: AgentCallbackContext) -> str:
        """The session this turn ran in, for the line above. ``-`` if unknown."""
        session = getattr(ctx, "session", None)
        read = getattr(session, "get_session_id", None)
        try:
            return str(read() or "-") if callable(read) else "-"
        except Exception:  # noqa: BLE001 - a log line may not fail a turn.
            return "-"

    def _read_metadata(self) -> Mapping[str, Any] | None:
        """Read this request's metadata. A failed read licenses nothing."""
        try:
            return self._metadata_provider()
        except Exception as exc:  # noqa: BLE001 - a provider may read anything.
            logger.warning(
                "[SilenceRail] request metadata unavailable: %s", exc
            )
            return None
