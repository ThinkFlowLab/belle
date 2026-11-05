"""Belle's listener for what the server posts to it: a click on a button, and the slash command.

The server posts both to addresses Belle gives it, on the core hook
listener. A click carries the ``context`` that Belle wrote on its button,
and Belle checks there a secret that is new at each start, since the server
sends no credential with a click. A slash command carries the command's
token in its ``Authorization`` header, which Belle checks against the one an
operator set.
"""

import hmac
import logging
import secrets
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from belle_agent.hook_server import HookServer

logger = logging.getLogger(__name__)

ACTION = "action"
COMMAND = "command"
#: The key of a button's ``context`` that holds the secret of this start.
SECRET = "secret"

#: Answers one request: what the server sent and its headers by lower-case name, to the JSON the server applies.
Handler = Callable[[Mapping[str, Any], Mapping[str, str]], Coroutine[Any, Any, dict[str, Any]]]


class ActionListener:
    """The listener of one connector, from ``start`` to ``close``."""

    def __init__(self, url: str, *, bind: str, port: int, on_action: Handler, on_command: Handler) -> None:
        self.url = url
        self._handlers = {ACTION: on_action, COMMAND: on_command}
        self._server = HookServer(self._answer, host=bind, port=port, prefix="/")
        #: Written on every button this start posts. A click with another came from a button of an earlier start.
        self.secret = secrets.token_hex(16)

    @property
    def port(self) -> int:
        """The port it listens on, once started."""
        return self._server.port

    def integration(self, **context: Any) -> dict[str, Any]:
        """What a button carries: where the server posts its click, and the context the click comes back with."""
        return {"url": f"{self.url}/{ACTION}", "context": {**context, SECRET: self.secret}}

    def context(self, request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The context of a click that a button of this start carried, or ``None``."""
        context = request.get("context")
        if not isinstance(context, Mapping) or not isinstance(context.get(SECRET), str):
            return None
        return context if hmac.compare_digest(context[SECRET].encode(), self.secret.encode()) else None

    async def start(self) -> None:
        """Listen, until ``close``."""
        await self._server.start()
        logger.info("mattermost action listener on port %d for %s", self.port, self.url)

    async def close(self) -> None:
        """Stop listening."""
        await self._server.close()

    def _answer(self, hook: str, request: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        """Answer one request in the listener's thread, from the handler of its path on the connector's loop."""
        handler = self._handlers.get(hook)
        if handler is None:
            raise LookupError(f"no handler for /{hook}")
        answer: dict[str, Any] = self._server.run(handler(request, headers))
        return answer
