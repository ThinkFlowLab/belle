"""Drive a running loopback platform by hand, over a local socket.

The loopback platform has no screen and no keyboard. Where its
configuration names a socket path, ``loopback: {control: <path>}``, the
running platform listens there, and this module is also the command that
talks to it:

    python -m belle_agent.platforms.loopback dm U1 "hello"
    python -m belle_agent.platforms.loopback channel C1 U2 "anyone seen the build?" [--mention]
    python -m belle_agent.platforms.loopback log

``dm`` and ``channel`` inject one message and print the host's admission
outcome. ``log`` prints every outbound action so far, one JSON object per
line. A turn runs after its message is admitted, so what it posted is in a
later ``log``. Each request is one JSON line and so is its answer.

This is a demonstration surface. It carries messages in and the log out, and
nothing else: no answer to a question, no stop, no history.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import socket
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from belle_agent.platforms.base import TRIGGER_DM, TRIGGER_MENTION, TRIGGER_NONE, InboundMessage

#: Names the socket for the command, where ``--socket`` does not.
SOCKET_ENV = "BELLE_LOOPBACK_CONTROL"
WORKSPACE = "local"


def inbound(platform: str, chat_type: str, fields: Mapping[str, Any]) -> InboundMessage:
    """Build the message one ``inject`` request describes, with an id no earlier process gave.

    The host remembers the ids it has handled across a restart, so a counter that began again would
    have the first messages of every start ignored as second deliveries.
    """
    identity = f"in-{uuid.uuid4().hex[:12]}"
    return InboundMessage(
        platform=platform,
        workspace=WORKSPACE,
        conversation=str(fields["conversation"]),
        message_id=identity,
        event_id=identity,
        sender=str(fields["sender"]),
        text=str(fields["text"]),
        chat_type=chat_type,
        trigger=str(fields.get("trigger") or TRIGGER_NONE),
    )


async def serve(
    path: str,
    inject: Callable[[Mapping[str, Any]], Awaitable[str]],
    log: Callable[[], Sequence[Any]],
) -> asyncio.AbstractServer:
    """Listen on ``path`` and answer each request with what ``inject`` returns or what ``log`` holds."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            if "inject" in request:
                reply: dict[str, Any] = {"outcome": await inject(request["inject"])}
            else:
                # A question object is shown by its text, which the action already carries.
                reply = {"log": [{**dataclasses.asdict(action), "question": None} for action in log()]}
        except Exception as exc:  # noqa: BLE001 - a malformed request is answered, not raised into the platform
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(answer, path=path)


def request(path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Send one request to a running platform and return its answer."""
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(path)
        client.sendall(json.dumps(payload).encode() + b"\n")
        return json.loads(client.makefile().readline())


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m belle_agent.platforms.loopback", description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=os.environ.get(SOCKET_ENV), help=f"the control socket (default: ${SOCKET_ENV})")
    commands = parser.add_subparsers(dest="command", required=True)
    dm = commands.add_parser("dm", help="inject a direct message")
    channel = commands.add_parser("channel", help="inject a channel message")
    channel.add_argument("conversation")
    channel.add_argument("--mention", action="store_true", help="the message mentions the bot")
    for command in (dm, channel):
        command.add_argument("sender")
        command.add_argument("text")
    commands.add_parser("log", help="print every outbound action so far")
    options = parser.parse_args(arguments)
    if not options.socket:
        parser.error(f"name the control socket with --socket or ${SOCKET_ENV}")

    if options.command == "log":
        payload: dict[str, Any] = {"log": True}
    elif options.command == "dm":
        fields = {"conversation": f"D-{options.sender}", "trigger": TRIGGER_DM}
        payload = {"inject": {**fields, "sender": options.sender, "text": options.text, "chat_type": "dm"}}
    else:
        fields = {"conversation": options.conversation, "trigger": TRIGGER_MENTION if options.mention else TRIGGER_NONE}
        payload = {"inject": {**fields, "sender": options.sender, "text": options.text, "chat_type": "channel"}}
    reply = request(options.socket, payload)
    if "error" in reply:
        print(reply["error"], file=sys.stderr)
        return 1
    for line in reply.get("log", [reply]):
        print(json.dumps(line))
    return 0
