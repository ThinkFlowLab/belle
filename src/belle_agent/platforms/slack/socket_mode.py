"""Slack Socket Mode connection, independent of any agent harness."""

import asyncio
from collections.abc import Callable
from typing import Any

from belle_agent.platforms.base import PlatformUnreachable
from belle_agent.platforms.slack.delivery import slack_error_code, slack_transient

try:
    from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp
except ImportError:
    AsyncSocketModeHandler = None
    AsyncApp = None


#: How long the socket may take to open once Slack has given its URL. Slack's client tries a socket that does not
#: open again, for ever, and reports nothing: past this, the start is told that Slack did not answer.
OPEN_SECONDS = 30.0


class SlackAppTokenRefused(Exception):
    """Slack refused the app token with ``code``. No retry with this token can connect."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SlackSocket:
    def __init__(
        self,
        bot_token: str,
        app_token: str,
        register: Callable[[Any], None],
        *,
        logger: Any = None,
        app_factory: Callable[..., Any] | None = None,
    ) -> None:
        if AsyncSocketModeHandler is None or (app_factory or AsyncApp) is None:
            raise RuntimeError("Slack Socket Mode requires slack-bolt")
        self.app = (app_factory or AsyncApp)(token=bot_token, logger=logger)
        register(self.app)
        self.client = self.app.client
        self.handler = AsyncSocketModeHandler(self.app, app_token)

    async def connect(self) -> None:
        """Open the connection and return. Events are then delivered until ``close``.

        Slack's client retries every failure to connect, a refused app token as well, and never returns until it
        connects. The connection URL is asked for here first. Slack's refusal of the app token raises
        ``SlackAppTokenRefused``. Where Slack did not answer, or the socket did not open within ``OPEN_SECONDS``,
        ``PlatformUnreachable`` is raised. ``close`` the socket after either.
        """
        socket_client = self.handler.client
        try:
            socket_client.wss_uri = await socket_client.issue_new_wss_url()
        except Exception as exc:
            code = slack_error_code(exc)
            if slack_transient(exc):
                raise PlatformUnreachable(
                    f"Slack did not answer apps.connections.open: {code or type(exc).__name__}"
                ) from None
            if code:
                raise SlackAppTokenRefused(code) from None
            raise
        try:
            await asyncio.wait_for(self.handler.connect_async(), OPEN_SECONDS)
        except TimeoutError:
            raise PlatformUnreachable(f"the Slack socket did not open within {OPEN_SECONDS:.0f} s") from None

    async def run(self) -> None:
        await self.handler.start_async()

    async def close(self) -> None:
        await self.handler.close_async()
