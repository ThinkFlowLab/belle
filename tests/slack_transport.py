"""A Slack that exists only in the test process: a web client, a Bolt app and a Socket Mode connection.

Nothing here opens a connection. The web client records every call and
answers as Slack would. The app keeps the listeners a connector registers,
and a test delivers an event or a click by calling them. The tokens are
synthetic, and name no installation.
"""

import inspect
import itertools
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The recording client every Slack toolkit test builds on.
sys.path.append(str(Path(__file__).resolve().parent / "jiuwenswarm_slack"))

from slack_fakes import FakeSlackError, RecordingSlackClient

BOT_TOKEN = "xoxb-11-22-abcdef"
APP_TOKEN = "xapp-1-A11-22-abcdef"
TEAM = "W1"
BOT_USER = "U0BELLEBOT"
BOT_ID = "B0BELLEBOT"
WORKSPACE_URL = "https://synthetic-workspace.slack.com/"
# A second Slack app, installed in a second workspace. Its ids may be those of the first: an id names
# a conversation or a person in one workspace only.
SECOND_BOT_TOKEN = "xoxb-33-44-ghijkl"
SECOND_APP_TOKEN = "xapp-1-A33-44-ghijkl"
SECOND_TEAM = "W2"
#: The workspace each synthetic token pair is installed in.
INSTALLATIONS = {(BOT_TOKEN, APP_TOKEN): TEAM, (SECOND_BOT_TOKEN, SECOND_APP_TOKEN): SECOND_TEAM}
FILE_HOST = "https://files.slack.com"


class FakeSlackWeb(RecordingSlackClient):
    """The web API calls the connector and the toolkits make, recorded and answered."""

    def __init__(self, fail: Any = None, *, scopes: str | None = None, team: str = TEAM, bot_user: str = BOT_USER) -> None:
        super().__init__(fail)
        #: The workspace this client's token is installed in, and this app's bot user.
        self.team = team
        self.bot_user = bot_user
        #: The users ``users.info`` names bots: those of other apps.
        self.bots: set[str] = set()
        self._ts = itertools.count(1)
        #: Every message this app sent, as ``(method, arguments, ts)``, in order.
        self.sent: list[tuple[str, dict[str, Any], str]] = []
        #: Conversation id to what ``conversations.info`` and ``conversations.members`` answer for it.
        self.conversations: dict[str, dict[str, Any]] = {}
        self.members: dict[str, list[str]] = {}
        #: Conversation id to the messages ``conversations.history`` answers with, newest first as Slack gives them.
        self.history: dict[str, list[dict[str, Any]]] = {}
        self._scopes = scopes
        #: The messages of this app that are on screen, by ts: what a reader sees after every edit and deletion.
        self.screen: dict[str, dict[str, Any]] = {}
        #: The ts of each message Slack still holds in its streaming state.
        self.streaming: set[str] = set()
        #: File id to what ``files.info`` answers for it. Every upload this app made, as its arguments, in order.
        self.files: dict[str, dict[str, Any]] = {}
        self.uploads: list[dict[str, Any]] = []

    def _send(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.record_call(method, kwargs)
        ts = f"1800000000.{next(self._ts):06d}"
        self.sent.append((method, kwargs, ts))
        if method != "chat_postEphemeral":
            self.screen[ts] = {
                "channel": kwargs.get("channel", ""), "thread_ts": kwargs.get("thread_ts", ""),
                "text": kwargs.get("text", ""), "blocks": kwargs.get("blocks"),
            }
        channel = str(kwargs.get("channel", ""))
        # A message posted to a person opens the app's direct message with them, and Slack answers with its id.
        return {"ok": True, "ts": ts, "channel": dm_of(channel) if channel.startswith("U") else channel}

    # ── a reply as it is written ─────────────────────────────────────────

    def _stream(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Take the chunks of one stream call into the message, as Slack does while it holds the message streaming."""
        self.record_call(method, kwargs)
        ts = kwargs.get("ts", "")
        if ts not in self.streaming:
            raise FakeSlackError("message_not_in_streaming_state")
        for chunk in kwargs.get("chunks") or ():
            if chunk.get("type") == "markdown_text":
                self.screen[ts]["text"] += chunk.get("text", "")
            else:
                self.screen[ts]["blocks"] = chunk.get("blocks")
        return {"ok": True, "ts": ts}

    async def chat_startStream(self, **kwargs: Any) -> dict[str, Any]:
        answer = self._send("chat_startStream", kwargs)
        self.screen[answer["ts"]]["text"] = "".join(chunk.get("text", "") for chunk in kwargs.get("chunks") or ())
        self.streaming.add(answer["ts"])
        return answer

    async def chat_appendStream(self, **kwargs: Any) -> dict[str, Any]:
        return self._stream("chat_appendStream", kwargs)

    async def chat_stopStream(self, **kwargs: Any) -> dict[str, Any]:
        answer = self._stream("chat_stopStream", kwargs)
        self.streaming.discard(answer["ts"])
        return answer

    def expire(self, ts: str) -> None:
        """End a stream as Slack does one that has been open too long: the message stays, and takes no more."""
        self.streaming.discard(ts)

    def shown(self, channel: str) -> list[str]:
        """The text of each message of this app on screen in one conversation, oldest first."""
        return [message["text"] for message in self.screen.values() if message["channel"] == channel]

    async def auth_test(self, **kwargs: Any) -> Any:
        self.record_call("auth_test", kwargs)
        answer = _Response(
            {"ok": True, "user_id": self.bot_user, "bot_id": BOT_ID, "team_id": self.team, "url": WORKSPACE_URL},
            {} if self._scopes is None else {"x-oauth-scopes": self._scopes},
        )
        return answer

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        return self._send("chat_postMessage", kwargs)

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        answer = self._send("chat_postEphemeral", kwargs)
        return {"ok": True, "message_ts": answer["ts"]}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("chat_update", kwargs)
        ts = kwargs.get("ts", "")
        if ts in self.streaming:
            raise FakeSlackError("streaming_state_conflict")
        if ts in self.screen:
            self.screen[ts].update(text=kwargs.get("text", ""), blocks=kwargs.get("blocks"))
        return {"ok": True, "ts": ts}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("chat_delete", kwargs)
        self.screen.pop(kwargs.get("ts", ""), None)
        self.streaming.discard(kwargs.get("ts", ""))
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("reactions_add", kwargs)
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("reactions_remove", kwargs)
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        self.record_call(method, dict(kwargs.get("data") or kwargs.get("json") or {}))
        return {"ok": True}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("conversations_info", kwargs)
        channel = kwargs.get("channel", "")
        return {"ok": True, "channel": {"id": channel, "is_channel": True, "is_private": True, **self.conversations.get(channel, {})}}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("conversations_members", kwargs)
        return {"ok": True, "members": list(self.members.get(kwargs.get("channel", ""), ())), "response_metadata": {"next_cursor": ""}}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("conversations_history", kwargs)
        return {"ok": True, "messages": list(self.history.get(kwargs.get("channel", ""), ()))}

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("conversations_replies", kwargs)
        root = kwargs.get("ts", "")
        return {"ok": True, "messages": [message for message in self.history.get(kwargs.get("channel", ""), ())
                                         if message.get("ts") == root]}

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("conversations_open", kwargs)
        return {"ok": True, "channel": {"id": f"D{kwargs.get('users', '')}"}}

    async def files_info(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("files_info", kwargs)
        record = self.files.get(kwargs.get("file", ""))
        if record is None:
            raise FakeSlackError("file_not_found")
        return {"ok": True, "file": dict(record)}

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        """The SDK's upload: it asks Slack for an upload URL, sends the bytes and completes the upload."""
        self.record_call("files_upload_v2", kwargs)
        self.uploads.append(kwargs)
        count = len(kwargs.get("file_uploads") or [kwargs])
        return {"ok": True, "files": [{"id": f"F0UPLOAD{len(self.uploads) + index:02d}"} for index in range(count)]}

    async def views_publish(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("views_publish", kwargs)
        return {"ok": True, "view": {"id": "V0HOME0001"}}

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self.record_call("users_info", kwargs)
        user = kwargs.get("user", "")
        if user in self.bots:
            return {"ok": True, "user": {"id": user, "is_bot": True, "profile": {"display_name": "An App"}}}
        return {"ok": True, "user": {"id": user, "is_bot": False, "profile": {"display_name": "A Person"}}}


def dm_of(user: str) -> str:
    """The id of the app's direct message with a person."""
    return f"D{user[1:]}"


@dataclass
class Exchange:
    """What one command, click or search was answered with."""

    #: What the listener acknowledged with: the payload Slack shows at once, in the envelope's answer.
    acks: list[dict[str, Any]] = field(default_factory=list)
    #: What it posted to the ``response_url`` afterwards, in order.
    responses: list[dict[str, Any]] = field(default_factory=list)
    #: What the listener returned.
    returned: Any = None

    @property
    def said(self) -> list[dict[str, Any]]:
        """Every payload with something for a person to read, the acknowledgement first."""
        return [payload for payload in (*self.acks, *self.responses) if payload.get("text") or payload.get("blocks")]

    @property
    def texts(self) -> list[str]:
        return [str(payload.get("text") or "") for payload in self.said]


class _Response(dict):
    """An answer with headers, as the SDK gives one."""

    def __init__(self, data: Mapping[str, Any], headers: Mapping[str, str]) -> None:
        super().__init__(data)
        self.headers = dict(headers)


class FakeBoltApp:
    """Keeps the listeners a connector registers, by event type and by action id."""

    def __init__(self, client: FakeSlackWeb) -> None:
        self.client = client
        self.events: list[tuple[Any, Any]] = []
        self.actions: list[tuple[Any, Any]] = []
        self.commands: list[tuple[Any, Any]] = []
        self.option_sources: list[tuple[Any, Any]] = []
        #: Every post to a ``response_url``, in order, whichever command or click it answered.
        self.responses: list[dict[str, Any]] = []
        #: What ``respond`` returns: Slack's answer to the response URL, as Bolt hands it back. Nothing by default.
        self.respond_answer: Any = None

    def event(self, name: Any):
        return lambda handler: self.events.append((name, handler)) or handler

    def action(self, name: Any):
        return lambda handler: self.actions.append((name, handler)) or handler

    def command(self, name: Any):
        return lambda handler: self.commands.append((name, handler)) or handler

    def options(self, name: Any):
        return lambda handler: self.option_sources.append((name, handler)) or handler

    @staticmethod
    def _matches(pattern: Any, value: str) -> bool:
        return bool(pattern.search(value)) if isinstance(pattern, re.Pattern) else pattern == value

    def _listener(self, listeners: list[tuple[Any, Any]], value: str) -> Any:
        # Bolt hands a request to the first listener that matches it.
        for pattern, handler in listeners:
            if self._matches(pattern, value):
                return handler
        raise AssertionError(f"no listener takes {value}")

    async def deliver(self, event: dict[str, Any], *, event_id: str = "", team: str | None = None) -> Any:
        """Deliver one Events API envelope, as Socket Mode would. Return what the listener returned."""
        body = {"type": "event_callback", "team_id": self.client.team if team is None else team, "event_id": event_id, "event": event}
        return await self._listener(self.events, str(event.get("type") or ""))(event=event, body=body)

    async def _exchange(self, handler: Any, **given: Any) -> Exchange:
        """Call a listener with the arguments it names, as Bolt does, and keep what it answers with."""
        exchange = Exchange()

        async def ack(*args: Any, **kwargs: Any) -> None:
            exchange.acks.append({"text": args[0], **kwargs} if args else dict(kwargs))

        async def respond(*args: Any, **kwargs: Any) -> Any:
            exchange.responses.append({"text": args[0], **kwargs} if args else dict(kwargs))
            self.responses.append(exchange.responses[-1])
            return self.respond_answer

        wanted = inspect.signature(handler).parameters
        offered = {"ack": ack, "respond": respond, **given}
        exchange.returned = await handler(**{name: value for name, value in offered.items() if name in wanted})
        return exchange

    async def slash(self, command: str, text: str = "", *, user: str, channel: str, team: str | None = None) -> Exchange:
        """Deliver one slash command, as Socket Mode would. Return what it was answered with."""
        payload = {
            "command": command, "text": text, "user_id": user, "channel_id": channel,
            "team_id": self.client.team if team is None else team,
            "response_url": "https://hooks.slack.test/commands/synthetic", "trigger_id": "synthetic.trigger",
        }
        return await self._exchange(self._listener(self.commands, command), body=payload, command=payload)

    async def click(self, body: dict[str, Any], action: dict[str, Any]) -> Any:
        """Deliver one block action. Return what the listener returned."""
        return (await self.interact(body, action)).returned

    async def interact(self, body: dict[str, Any], action: dict[str, Any]) -> Exchange:
        """Deliver one block action. Return what it was answered with."""
        handler = self._listener(self.actions, str(action.get("action_id") or ""))
        return await self._exchange(handler, body=body, action=action)

    async def suggest(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Deliver one search of an external select. Return the options it was answered with."""
        exchange = await self._exchange(self._listener(self.option_sources, str(body.get("action_id") or "")), body=body)
        return list(exchange.acks[-1].get("options") or ()) if exchange.acks else []


class FakeSocket:
    """Stands where ``SlackSocket`` stands: it builds the app, registers the listeners, and connects nothing."""

    def __init__(self, bot_token: str, app_token: str, register: Any, *, web: FakeSlackWeb | None = None, **_: Any) -> None:
        assert (bot_token, app_token) in INSTALLATIONS, "the connector was started with tokens this test did not give it"
        self.tokens = (bot_token, app_token)
        self.web = web or FakeSlackWeb(team=INSTALLATIONS[self.tokens])
        self.app = FakeBoltApp(self.web)
        register(self.app)
        self.client = self.web
        self.connected = self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True


def button(blocks: list[dict[str, Any]], *, value: str | None = None, index: int | None = None) -> dict[str, Any]:
    """Find one button of a rendered question: by the option it stands for, or by its place."""
    buttons = [
        element
        for block in blocks
        if block.get("type") == "actions"
        for element in block.get("elements", ())
        if element.get("type") == "button"
    ]
    for place, element in enumerate(buttons):
        decoded = json.loads(element["value"])
        if (index is not None and place == index) or (value is not None and decoded.get("value") == value):
            return element
    raise AssertionError(f"no button for value={value!r} index={index!r} among {[element['value'] for element in buttons]}")


def ephemeral_body(*, user: str, channel: str, state: Mapping[str, Any] | None = None, team: str = TEAM) -> dict[str, Any]:
    """The body of a click on an ephemeral answer to a command: Slack names its container and gives no message."""
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "channel": {"id": channel},
        "container": {"type": "message", "channel_id": channel, "is_ephemeral": True},
        "state": {"values": dict(state or {})},
    }


def click_body(*, user: str, channel: str, message_ts: str, payload: Mapping[str, Any], team: str = TEAM) -> dict[str, Any]:
    """The body of a click on a message this app posted with ``payload``."""
    thread = str(payload.get("thread_ts") or "")
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "channel": {"id": channel},
        "container": {"type": "message", "message_ts": message_ts, "channel_id": channel, "thread_ts": thread},
        "message": {"ts": message_ts, "text": payload.get("text", ""), "blocks": payload.get("blocks", []), "thread_ts": thread},
    }
