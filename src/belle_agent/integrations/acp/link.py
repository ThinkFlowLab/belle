"""One ACP v1 connection to a harness, through the Unix socket of its relay.

Belle opens the socket and hands its two streams to the SDK. It starts no process: the relay starts one agent
process for each connection, and ends it when the connection closes.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from importlib.metadata import version
from typing import Any

from acp import connect_to_agent
from acp.connection import StreamDirection, StreamEvent
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.schema import AgentCapabilities, ClientCapabilities, Implementation

from belle_agent.backend import BackendError

logger = logging.getLogger(__name__)

#: The only ACP version Belle speaks. Version 2 is a draft.
ACP_VERSION = 1
#: A notification the SDK 0.12.1 router has no handler for. An agent sends it to withdraw a request it made.
CANCEL_REQUEST = "$/cancel_request"
PERMISSION_REQUEST = "session/request_permission"
PROMPT = "session/prompt"


class AcpError(BackendError):
    """The harness cannot serve Belle over ACP as it stands."""


class Link:
    """One open connection: the SDK client, its streams, and what the agent answered to ``initialize``."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, binding: str) -> None:
        self._reader, self._writer = reader, writer
        #: The line sent before the ACP stream, for the relay. Empty on a shared connection.
        self.binding = binding
        self.conn: Any = None
        self.capabilities = AgentCapabilities()
        self.agent = ""
        #: The ACP sessions open on this connection.
        self.sessions: set[str] = set()
        #: ``(session id, tool call id)`` of a held permission request, to its JSON-RPC id.
        self.permission_ids: dict[tuple[str, str], Any] = {}
        #: Called with the JSON-RPC id that a ``$/cancel_request`` withdraws.
        self.on_cancel_request: Callable[[Any], None] = lambda request_id: None
        #: Called with the session id of a ``session/prompt`` once the SDK has written it.
        self.on_prompt_sent: Callable[[str], None] = lambda session_id: None

    @property
    def alive(self) -> bool:
        """Whether the connection is still open both ways."""
        return not (self._reader.at_eof() or self._writer.is_closing())

    def observe(self, event: StreamEvent) -> None:
        """Read what the SDK hands no handler: a permission request's id, a withdrawn one's, and a sent prompt."""
        message, params = event.message, event.message.get("params")
        if not isinstance(params, dict):
            return
        if event.direction == StreamDirection.OUTGOING:
            if message.get("method") == PROMPT:
                self.on_prompt_sent(str(params.get("sessionId")))
        elif message.get("method") == PERMISSION_REQUEST and "id" in message:
            call = params.get("toolCall")
            call_id = call.get("toolCallId") if isinstance(call, dict) else None
            self.permission_ids[(str(params.get("sessionId")), str(call_id))] = message["id"]
        elif message.get("method") == CANCEL_REQUEST and "requestId" in params:
            self.on_cancel_request(params["requestId"])

    async def close(self) -> None:
        """Close the connection. The relay then ends the agent process behind it."""
        try:
            if self.conn is not None:
                # The SDK raises the write error of a connection that is gone already.
                with suppress(OSError):
                    await self.conn.close()
        finally:
            self._writer.close()
            with suppress(OSError):
                await self._writer.wait_closed()


async def open_link(socket: str, client: Callable[[Link], Any], *, binding: str, timeout: float) -> Link:
    """Connect to the relay at ``socket`` and initialize ACP v1 within ``timeout`` seconds.

    ``client(link)`` builds the SDK client of the connection. ``binding``, when set, is the first line sent: the
    relay reads it before the agent starts. Each failure raises an :class:`AcpError` that names its cause.
    """
    try:
        reader, writer = await asyncio.wait_for(
            # The same buffer limit as the SDK's own stdio streams.
            asyncio.open_unix_connection(socket, limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES),
            timeout,
        )
    except TimeoutError:
        raise AcpError(f"the ACP relay at {socket} did not accept a connection within {timeout:g}s") from None
    except OSError as exc:
        raise AcpError(f"cannot connect to the ACP relay at {socket}: {exc.strerror or exc}") from exc
    link = Link(reader, writer, binding)
    try:
        if binding:
            writer.write(f"{binding}\n".encode())
        link.conn = connect_to_agent(client(link), writer, reader, observers=[link.observe])
        answer = await asyncio.wait_for(
            link.conn.initialize(
                protocol_version=ACP_VERSION,
                # Belle offers no file system and no terminal to the agent.
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="belle", version=version("belle-agent")),
            ),
            timeout,
        )
    except TimeoutError:
        await link.close()
        raise AcpError(
            f"the agent behind {socket} did not answer initialize within {timeout:g}s (start_timeout_seconds)"
        ) from None
    except Exception as exc:
        await link.close()
        raise AcpError(f"the agent behind {socket} refused initialize: {type(exc).__name__}: {exc}") from exc
    except BaseException:
        # A cancel, too, closes the socket, so the relay ends the agent process.
        await link.close()
        raise
    if answer.protocol_version != ACP_VERSION:
        await link.close()
        raise AcpError(
            f"the agent behind {socket} answered ACP version {answer.protocol_version} to Belle's version"
            f" {ACP_VERSION}. Belle speaks version {ACP_VERSION} only"
        )
    link.capabilities = answer.agent_capabilities or AgentCapabilities()
    info = answer.agent_info
    link.agent = f"{info.name} {info.version}" if info is not None else ""
    return link
