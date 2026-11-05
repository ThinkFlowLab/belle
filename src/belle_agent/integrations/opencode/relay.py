"""Translate relay hooks from the admitted turn scope.

The plugin forwards hooks and applies each answer. This endpoint reads the
running turn's scope. The ``context`` hook appends its system additions.
The ``execute.before`` hook applies the tool host's deny or rewrite verdict.
Other hooks return an empty answer. The plugin replaces each file's contents in
``prompt`` and ``context`` events with ``{"omitted": <length>}``.

With the plugin loaded, system additions enter each request through
``context``. Without it, the adapter uses a session instruction entry.
In both modes, session permission rules control the tool roster and approvals.
Skills and model selection also use the server API.
"""

from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.backend import VERDICT_DENY, VERDICT_REWRITE, ToolCall, ToolHost, TurnScope
from belle_agent.hook_server import HookServer

#: Names the turn running in a session: its conversation and its scope, or ``None`` outside one.
TurnOf = Callable[[str], "tuple[str, TurnScope] | None"]


def _answer(hook: str, event: Mapping[str, Any], turn: "tuple[str, TurnScope] | None", tools: ToolHost,
            run: Callable[[Any], Any]) -> dict[str, Any]:
    """The answer to one hook, from the turn's scope. ``run`` runs a coroutine on the adapter's loop."""
    if turn is None:
        return {}
    conversation, scope = turn
    if hook == "context":
        return {"system_append": list(scope.system_additions)}
    if hook == "execute.before":
        verdict = run(tools.authorize(ToolCall(
            conversation=conversation, name=str(event.get("tool") or ""),
            arguments=event.get("input") or {}, call_id=str(event.get("id") or ""),
        )))
        if verdict.action == VERDICT_DENY:
            return {"deny": verdict.message}
        if verdict.action == VERDICT_REWRITE and verdict.arguments is not None:
            return {"input": dict(verdict.arguments)}
        return {}
    return {}


class RelayEndpoint(HookServer):
    """The far end of the relay plugin, for as long as it is started."""

    def __init__(self, turn_of: TurnOf, tools: ToolHost, *, token: str = "", host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__(self._relay, token=token, host=host, port=port)
        self._turn_of = turn_of
        self._tools = tools

    def _relay(self, hook: str, event: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        """Answer one hook from the turn running in the session it names."""
        return _answer(hook, event, self._turn_of(str(event.get("sessionID") or "")), self._tools, self.run)
