"""Hold the Discord gateway connection: identify, heartbeat, and resume after a drop.

The gateway sends Hello with the heartbeat interval. The bot heartbeats at that interval, the first time
after a random part of it, and a heartbeat that gets no ACK before the next one marks the connection as
dead ("zombied"): it is closed with a code that keeps the session, and resumed on ``resume_gateway_url``.
A close code that Discord marks as not reconnectable stops the gateway with an error that names what to
fix. ``close`` ends the session with code 1000.
"""

import asyncio
import logging
import random
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from belle_agent.platforms.discord.client import DiscordClient, DiscordError, GatewaySocket
from belle_agent.platforms.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

VERSION = 10
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTENT_GUILDS = 1 << 0
INTENT_GUILD_MEMBERS = 1 << 1
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_GUILD_MESSAGE_REACTIONS = 1 << 10
INTENT_DIRECT_MESSAGES = 1 << 12
INTENT_DIRECT_MESSAGE_REACTIONS = 1 << 13
INTENT_MESSAGE_CONTENT = 1 << 15
#: What every connection asks for. The two privileged intents follow the settings.
BASE_INTENTS = (
    INTENT_GUILDS | INTENT_GUILD_MESSAGES | INTENT_GUILD_MESSAGE_REACTIONS
    | INTENT_DIRECT_MESSAGES | INTENT_DIRECT_MESSAGE_REACTIONS
)

#: The close codes Discord marks as not reconnectable, each with what the operator must change.
FATAL_CLOSES = {
    4004: "Discord rejected the bot token: set DISCORD_BOT_TOKEN to the token of the application's bot",
    4010: "Discord refused the shard this connection sent",
    4011: "Discord requires sharding for this bot (2,500 guilds or more), and this platform runs one connection",
    4012: f"Discord refused gateway API version {VERSION}",
    4013: "Discord refused the intents this connection asked for",
    4014: (
        "a privileged intent this connection asks for is not enabled for the application: turn on Message Content"
        " (and Server Members, where platforms.discord.guild_members is true) in the developer portal, Bot tab,"
        " or set platforms.discord.message_content to false"
    ),
}
#: The code the bot closes a dead connection with: any but 1000 and 1001, which would end the session.
ZOMBIE_CLOSE = 4000
#: The code ``close`` ends the session with.
NORMAL_CLOSE = 1000
#: The wait before the first new attempt after a failed connection, doubled after each failure up to the maximum.
RETRY_FIRST_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0
#: After an Invalid Session, Discord asks for a random wait of one to five seconds before the next Identify.
INVALID_SESSION_WAIT = (1.0, 5.0)


class GatewayStopped(RuntimeError):
    """The gateway cannot go on: Discord closed with a code it marks as not reconnectable, or refused to start."""

    def __init__(self, reason: str, code: int = 0) -> None:
        self.code = code
        super().__init__(f"discord gateway stopped{f' (close code {code})' if code else ''}: {reason}")


def intents(settings: DiscordSettings) -> int:
    """The intents a connection asks for, by the settings of the privileged ones."""
    found = BASE_INTENTS
    if settings.message_content:
        found |= INTENT_MESSAGE_CONTENT
    if settings.guild_members:
        found |= INTENT_GUILD_MEMBERS
    return found


class DiscordGateway:
    """The gateway of one bot, read as one stream of dispatch events across drops."""

    def __init__(
        self,
        client: DiscordClient,
        settings: DiscordSettings,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._token = settings.token
        self._intents = intents(settings)
        self._sleep = sleep
        self._jitter = jitter
        self._socket: GatewaySocket | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._acked = True
        self._closed = False
        self._url = ""
        #: The code the bot closed the current connection with itself, where it did; the socket may keep none.
        self._sent_close: int | None = None
        #: What READY gave: the session, the URL to resume it on, the bot's user and its guilds.
        self.session_id = ""
        self.resume_url = ""
        self.sequence: int | None = None
        self.user: Mapping[str, Any] = {}
        #: The application the bot belongs to, which answers interactions and owns the slash command.
        self.application_id = ""
        self.guilds: tuple[Mapping[str, Any], ...] = ()

    async def connect(self) -> Mapping[str, Any]:
        """Open a new session and return READY's data. Raise ``GatewayStopped`` or ``DiscordError``.

        A connection this opened is dropped when it raises ``DiscordError``, so ``connect`` can be called again.
        """
        found = await self._client.gateway_bot()
        shards = found.get("shards", 1)
        if isinstance(shards, int) and shards > 1:
            raise GatewayStopped(f"Discord asks for {shards} shards, and this platform runs one connection")
        limit = found.get("session_start_limit")
        limit = limit if isinstance(limit, Mapping) else {}
        if limit.get("remaining") == 0:
            raise GatewayStopped(
                f"the identify budget is spent ({limit.get('total')} per day); it fills again in"
                f" {int(limit.get('reset_after') or 0) / 1000:.0f} s"
            )
        self._url = f"{str(found.get('url') or '').rstrip('/')}/?v={VERSION}&encoding=json"
        try:
            await self._open(self._url)
            await self._identify()
            while True:
                payload = await self._read()
                if payload is None:
                    code = self._close_code() or 0
                    raise GatewayStopped(FATAL_CLOSES.get(code, "the connection closed before READY"), code)
                if payload.get("op") == OP_INVALID_SESSION:
                    raise GatewayStopped("Discord answered Identify with Invalid Session")
                if payload.get("op") == OP_DISPATCH and payload.get("t") == "READY":
                    return self._ready(payload)
        except DiscordError:
            await self._drop()
            raise

    def _ready(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = payload.get("d")
        data = data if isinstance(data, Mapping) else {}
        self.session_id = str(data.get("session_id") or "")
        self.resume_url = f"{str(data.get('resume_gateway_url') or '').rstrip('/')}/?v={VERSION}&encoding=json"
        user = data.get("user")
        self.user = user if isinstance(user, Mapping) else {}
        application = data.get("application")
        self.application_id = str(application.get("id") or "") if isinstance(application, Mapping) else ""
        guilds = data.get("guilds")
        self.guilds = tuple(guild for guild in guilds if isinstance(guild, Mapping)) if isinstance(guilds, list) else ()
        logger.info("discord gateway ready: user=%s guilds=%d", self.user.get("username", ""), len(self.guilds))
        return data

    async def events(self) -> AsyncIterator[Mapping[str, Any]]:
        """Yield each dispatch event after READY, across drops, until ``close``. Raise ``GatewayStopped``."""
        while not self._closed:
            payload = await self._read()
            if payload is None:
                if self._closed:
                    return
                code = self._close_code()
                if code in FATAL_CLOSES:
                    raise GatewayStopped(FATAL_CLOSES[code], code)
                resume = code not in (4007, 4009)
                action = "resuming session" if resume else "identifying a new session"
                if self._sent_close is not None:
                    logger.warning("discord gateway closed (code %s, sent by Belle): %s", self._sent_close, action)
                else:
                    logger.warning("discord gateway closed (code %s): %s", code, action)
                await self._reconnect(resume=resume)
                continue
            op = payload.get("op")
            if op == OP_DISPATCH:
                if payload.get("t") == "READY":
                    self._ready(payload)
                yield payload
            elif op == OP_RECONNECT:
                logger.info("discord gateway asked to reconnect: resuming session")
                await self._drop()
                await self._reconnect(resume=True)
            elif op == OP_INVALID_SESSION:
                if not self.session_id:
                    raise GatewayStopped("Discord answered Identify with Invalid Session")
                resumable = payload.get("d") is True
                logger.warning("discord gateway invalidated the session (resumable=%s)", resumable)
                await self._drop()
                if not resumable:
                    await self._sleep(INVALID_SESSION_WAIT[0] + self._jitter() * (INVALID_SESSION_WAIT[1] - INVALID_SESSION_WAIT[0]))
                await self._reconnect(resume=resumable)

    async def _read(self) -> Mapping[str, Any] | None:
        """The next payload. Heartbeat requests, ACKs and sequence numbers are handled here."""
        while self._socket is not None:
            receiving = asyncio.create_task(self._socket.receive())
            try:
                heartbeat = self._heartbeat
                if heartbeat is not None:
                    done, _ = await asyncio.wait((receiving, heartbeat), return_when=asyncio.FIRST_COMPLETED)
                    if heartbeat in done:
                        if not heartbeat.cancelled() and (error := heartbeat.exception()) is not None:
                            logger.warning("discord gateway heartbeat failed: %s", error)
                        return None
                payload = await receiving
            finally:
                if not receiving.done():
                    receiving.cancel()
                await asyncio.gather(receiving, return_exceptions=True)
            if payload is None:
                return None
            if isinstance(payload.get("s"), int):
                self.sequence = payload["s"]
            op = payload.get("op")
            if op == OP_HEARTBEAT:
                try:
                    await self._beat()
                except DiscordError:
                    return None
            elif op == OP_HEARTBEAT_ACK:
                self._acked = True
            else:
                return payload
        return None

    async def _open(self, url: str) -> None:
        """Open a connection, read Hello, and start the heartbeat."""
        self._socket = await self._client.open_socket(url)
        hello = await self._socket.receive()
        data = hello.get("d") if hello and hello.get("op") == OP_HELLO else None
        interval = data.get("heartbeat_interval") if isinstance(data, Mapping) else None
        if not isinstance(interval, int | float) or interval <= 0:
            code = self._close_code()
            if code in FATAL_CLOSES:
                raise GatewayStopped(FATAL_CLOSES[code], code)
            raise DiscordError("bad_response", "GET", "gateway")
        self._acked = True
        self._sent_close = None
        self._heartbeat = asyncio.create_task(self._heartbeats(interval / 1000))

    async def _identify(self) -> None:
        await self._send({"op": OP_IDENTIFY, "d": {
            "token": self._token, "intents": self._intents,
            "properties": {"os": sys.platform, "browser": "belle-agent", "device": "belle-agent"},
        }})

    async def _beat(self) -> None:
        self._acked = False
        await self._send({"op": OP_HEARTBEAT, "d": self.sequence})

    async def _heartbeats(self, interval: float) -> None:
        """Heartbeat at ``interval``, the first time after a random part of it. An unanswered one ends the connection."""
        await self._sleep(interval * self._jitter())
        while self._socket is not None:
            if not self._acked:
                logger.warning("discord gateway sent no heartbeat ACK: the connection is dead, resuming")
                self._sent_close = ZOMBIE_CLOSE
                await self._socket.close(ZOMBIE_CLOSE)
                return
            await self._beat()
            await self._sleep(interval)

    async def _send(self, payload: Mapping[str, Any]) -> None:
        if self._socket is not None:
            await self._socket.send(payload)

    def _close_code(self) -> int | None:
        return self._socket.close_code if self._socket is not None else None

    async def _drop(self) -> None:
        """End this connection and its heartbeat, keeping the session."""
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None
        socket, self._socket = self._socket, None
        if socket is not None and socket.close_code is None:
            try:
                await socket.close(ZOMBIE_CLOSE)
            except (DiscordError, OSError) as exc:
                logger.warning("discord gateway connection could not close: %s", type(exc).__name__)

    async def _reconnect(self, *, resume: bool) -> None:
        """Open a new connection, waiting longer after each failure: resume the session, or identify anew."""
        await self._drop()
        delay = RETRY_FIRST_SECONDS
        while not self._closed:
            try:
                if resume and self.session_id:
                    await self._open(self.resume_url)
                    await self._send({"op": OP_RESUME, "d": {
                        "token": self._token, "session_id": self.session_id, "seq": self.sequence,
                    }})
                    logger.info("discord gateway resuming session at sequence %s", self.sequence)
                else:
                    self.session_id, self.sequence = "", None
                    await self._open(self._url)
                    await self._identify()
                    logger.info("discord gateway identifying a new session")
                return
            except DiscordError as exc:
                await self._drop()
                logger.warning("discord gateway did not reopen: %s; next attempt in %.0f s", exc, delay)
            await self._sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    async def close(self) -> None:
        """End the session with code 1000 and stop ``events``."""
        self._closed = True
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close(NORMAL_CLOSE)
