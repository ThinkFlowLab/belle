"""The Slack platform's kit for the conformance suite.

The platform under test is the Slack connector, built from its section like
any other. Slack itself is the fake transport of
``tests/slack_transport.py``: a check's inbound record becomes the event or
the click Slack would send, delivered to the listeners the connector
registered, and the log is read from the web API calls the connector made.
No connection is opened and the tokens are synthetic.

The kit turns the connector's acknowledgements off. With Slack's default, a
message that starts a turn carries a reaction, so no check could find a
conversation untouched. ``tests/test_slack_connector.py`` checks the marks.
"""

import base64
import itertools
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from conformance_kit import with_section
from slack_transport import (
    APP_TOKEN,
    BOT_TOKEN,
    BOT_USER,
    FILE_HOST,
    SECOND_APP_TOKEN,
    SECOND_BOT_TOKEN,
    SECOND_TEAM,
    TEAM,
    FakeSocket,
    button,
    click_body,
)

from belle_agent.backend import APPROVAL, Question
from belle_agent.host import ConversationHost
from belle_agent.platforms.base import (
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_REPLY,
    InboundAnswer,
    InboundEvent,
    InboundMessage,
    InboundStop,
)
from belle_agent.platforms.slack import connector, streaming
from belle_agent.platforms.slack.activity import STOP_ACTION_ID
from belle_agent.platforms.slack.history import SlackHistoryToolkit
from belle_agent.platforms.slack.questions import QUESTION_ACTION_ID_PREFIX
from belle_agent.tools.host import APPROVE, REJECT

NAME = "slack"
#: What the connector adds to a turn's text: the ids its tools take.
_ENVELOPE = re.compile(r"\n\n\[ts: [^\]]*\]\Z")


@dataclass(frozen=True)
class Seen:
    """One action Slack was asked to perform, in the words the suite reads the loopback log in."""

    kind: str
    conversation: str
    thread: str = ""
    text: str = ""
    message_id: str = ""
    to: str = ""
    name: str = ""
    removed: bool = False
    question: Question | None = None
    #: The workspace of the app that was asked, where the connector serves several, else empty.
    workspace: str = ""


class SlackDriver:
    """Deliver records as Slack would, and read back what the connector asked Slack to do.

    Where the connector serves several workspaces, ``sockets`` names the app of each workspace a check writes,
    and a record goes through the app of its workspace. The log then names the workspace of the app each action
    was asked of.
    """

    def __init__(
        self, host: ConversationHost, socket: FakeSocket, files: dict[str, bytes], sockets: dict[str, FakeSocket] | None = None
    ) -> None:
        self.app = socket.app
        self.web = socket.web
        self._sockets = dict(sockets or {})
        self._files = files
        self._file_ids = itertools.count(1)
        #: The thread each delivered message roots or sits in, which is what Slack's stop button names.
        self._threads: dict[str, str] = {}
        self._questions: dict[str, Question] = {}
        platform = host.platforms[NAME]
        ask = platform.ask

        async def remember(conversation: str, thread: str, question: Question, **where: Any) -> str:
            self._questions[question.id] = question
            return await ask(conversation, thread, question, **where)

        platform.ask = remember
        #: What the host handed the connector of each running turn, and how it ended what the connector showed.
        self.progressed: list[tuple[str, Any]] = []
        self.concluded: list[tuple[str, str, str]] = []
        progress, conclude = platform.progress, platform.conclude

        async def handed(inbound: InboundMessage, event: Any) -> None:
            self.progressed.append((inbound.message_id, event))
            await progress(inbound, event)

        async def ended(inbound: InboundMessage, state: str, final: str = "") -> bool:
            self.concluded.append((inbound.message_id, state, final))
            return await conclude(inbound, state, final)

        platform.progress, platform.conclude = handed, ended

    # ── what the connector did ───────────────────────────────────────────

    @staticmethod
    def _question_id(payload: dict[str, Any]) -> str:
        for block in payload.get("blocks") or ():
            for element in block.get("elements", ()) if block.get("type") == "actions" else ():
                if str(element.get("action_id") or "").startswith(QUESTION_ACTION_ID_PREFIX):
                    return str(json.loads(element["value"])["request_id"])
        return ""

    def _at(self, workspace: str) -> FakeSocket:
        """The app that a record of this workspace reaches: the first for a workspace no app is in."""
        return self._sockets.get(workspace) or next(iter(self._sockets.values()))

    @property
    def log(self) -> list[Seen]:
        if not self._sockets:
            return self._log(self.web, "")
        return [entry for workspace, socket in self._sockets.items() for entry in self._log(socket.web, workspace)]

    def _log(self, web: Any, workspace: str) -> list[Seen]:
        seen = []
        sent = iter(web.sent)
        for method, payload in web.calls:
            conversation = str(payload.get("channel") or "")
            if method in ("reactions_add", "reactions_remove"):
                seen.append(
                    Seen("reaction", conversation, message_id=payload["timestamp"], name=payload["name"],
                         removed=method == "reactions_remove", workspace=workspace)
                )
            elif method in ("chat_postMessage", "chat_postEphemeral", "chat_startStream"):
                _, _, ts = next(sent)
                thread = str(payload.get("thread_ts") or "")
                question = self._questions.get(self._question_id(payload))
                if method == "chat_postEphemeral":
                    seen.append(Seen("ephemeral", conversation, thread, payload["text"], ts, to=payload["user"], workspace=workspace))
                elif question is not None:
                    kind = "approval" if question.kind == APPROVAL else "question"
                    seen.append(Seen(kind, conversation, thread, question.text, ts, question=question, workspace=workspace))
                elif ts in web.screen:
                    # A message reads as it does on screen now. One that was taken down is no post.
                    # A card is a plan, or the stop button alone of a turn that has reported nothing yet.
                    card = any(
                        block.get("type") == "plan"
                        or any(element.get("action_id") == STOP_ACTION_ID for element in block.get("elements") or ())
                        for block in payload.get("blocks") or ()
                    )
                    seen.append(Seen("activity" if card else "post", conversation, thread, web.screen[ts]["text"], ts, workspace=workspace))
        return seen

    # ── what Slack sends ─────────────────────────────────────────────────

    async def inject(self, record: InboundMessage | InboundAnswer | InboundStop | InboundEvent) -> str:
        if isinstance(record, InboundEvent):
            return await self._event(record)
        if isinstance(record, InboundAnswer):
            return await self._click(record)
        if isinstance(record, InboundStop):
            return await self._app(record.workspace).deliver(
                {
                    "type": "agent_session_stopped",
                    "channel": record.conversation,
                    "thread_ts": self._threads.get(record.message_id, record.message_id),
                    "user": record.sender,
                },
                team=record.workspace,
            )
        return await self._message(record)

    async def _event(self, record: InboundEvent) -> str:
        """One event of each family, in the shape Slack sends it. An app's own carries its bot user."""
        actor = BOT_USER if record.actor_is_bot else record.actor
        about = {"ts": record.anchor} if record.anchor else {}
        if record.anchor_thread:
            about["thread_ts"] = record.anchor_thread
        place = {"type": "message", "channel": record.conversation}
        shapes = {
            "reaction": {"type": "reaction_added", "reaction": "tada", "item": {**place, **about}},
            "pin": {"type": "pin_added", "channel_id": record.conversation, "item": {**place, "message": about}},
            "member": {"type": "member_joined_channel", "channel": record.conversation},
            "app_home": {"type": "app_home_opened", "channel": record.conversation, "tab": "messages"},
        }
        event = {**shapes[record.kind], "user": actor, "event_ts": "1800000000.000001"}
        return await self._app(record.workspace).deliver(event, event_id=record.event_id, team=record.workspace)

    def _app(self, workspace: str) -> Any:
        return self._at(workspace).app if self._sockets else self.app

    def _file(self, entry: dict[str, Any]) -> dict[str, Any]:
        header, _, payload = str(entry["uri"]).partition(",")
        file_id = f"F{next(self._file_ids):04d}"
        url = f"{FILE_HOST}/files-pri/{file_id}/{entry['name']}"
        self._files[url] = base64.b64decode(payload)
        return {"id": file_id, "name": entry["name"], "mimetype": header[5:].split(";")[0], "url_private": url}

    async def _message(self, record: InboundMessage) -> str:
        self._threads[record.message_id] = record.thread or record.message_id
        message: dict[str, Any] = {"user": record.sender, "text": record.text, "ts": record.message_id}
        if record.thread:
            message["thread_ts"] = record.thread
        if record.sender_is_bot:
            message["bot_id"] = "B0ANOTHERAPP"
        if record.files:
            message.update(subtype="file_share", files=[self._file(entry) for entry in record.files])
        place = {
            "type": "message",
            "channel": record.conversation,
            "channel_type": "im" if record.trigger == TRIGGER_DM else "channel",
        }
        if record.edited:
            event = {**place, "subtype": "message_changed", "message": message, "ts": f"{record.message_id}-edit"}
            return await self._app(record.workspace).deliver(event, event_id=record.event_id, team=record.workspace)
        if record.trigger == TRIGGER_REPLY:
            message["parent_user_id"] = BOT_USER
        if record.trigger == TRIGGER_MENTION:
            # Slack sends a mention twice: as the message it is, and as an app_mention with no channel type.
            message["text"] = f"<@{BOT_USER}> {record.text}"
            ignored = await self._app(record.workspace).deliver({**place, **message}, event_id=f"{record.event_id}-message", team=record.workspace)
            assert ignored.startswith("ignored:leading-bot-mention"), ignored
            mention = {"type": "app_mention", "channel": record.conversation, **message}
            return await self._app(record.workspace).deliver(mention, event_id=record.event_id, team=record.workspace)
        return await self._app(record.workspace).deliver({**place, **message}, event_id=record.event_id, team=record.workspace)

    async def _click(self, answer: InboundAnswer) -> str:
        """Press the button of the answer, on the message the connector posted the question with."""
        socket = self._at(answer.workspace) if self._sockets else None
        web = socket.web if socket is not None else self.web
        questions = [(payload, ts) for method, payload, ts in web.sent if self._question_id(payload)]
        assert questions, "Slack was shown no question, so there is nothing to click"
        payload, ts = next((shown for shown in questions if self._question_id(shown[0]) == answer.question_id), questions[-1])
        if answer.answer.approved is not None:
            value = APPROVE if answer.answer.approved else REJECT
        else:
            value = answer.answer.values[0]
        pressed = button(payload["blocks"], value=value)
        if self._question_id(payload) != answer.question_id:
            # A question Slack was never shown: the click of a button that names it, as a stale message would carry.
            pressed = {**pressed, "value": json.dumps({**json.loads(pressed["value"]), "request_id": answer.question_id})}
        body = click_body(
            user=answer.sender, channel=answer.conversation, message_ts=ts, payload=payload, team=answer.workspace
        )
        return await (socket.app if socket is not None else self.app).click(body, pressed)


class SlackKit:
    name = NAME

    def __init__(self, monkeypatch: Any) -> None:
        self.sockets: list[FakeSocket] = []
        self._files: dict[str, bytes] = {}
        monkeypatch.setenv(connector.BOT_TOKEN_ENV, BOT_TOKEN)
        monkeypatch.setenv(connector.APP_TOKEN_ENV, APP_TOKEN)
        monkeypatch.setattr(connector, "SlackSocket", self._socket)
        monkeypatch.setattr(connector, "fetch_attachment_bytes", self._fetch)
        monkeypatch.setattr(SlackHistoryToolkit, "_stream_url_to_disk", lambda toolkit, url, **where: self._stream(url, **where))
        monkeypatch.setattr(streaming, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(streaming, "_STREAM_APPEND_MIN_INTERVAL_SECONDS", 0.0)

    def _socket(self, *args: Any, **kwargs: Any) -> FakeSocket:
        self.sockets.append(FakeSocket(*args, **kwargs))
        return self.sockets[-1]

    async def _stream(self, url: str, *, mimetype: str, destination: Path) -> int:
        """Stand in for the byte transfer of ``download_slack_file``: write what the fake file host holds."""
        destination.write_bytes(self._files[url])
        return len(self._files[url])

    def holds(self, platform: SlackDriver, name: str, mime: str, data: bytes) -> tuple[str, dict[str, Any]]:
        file_id = f"F0HELD{len(platform.web.files):04d}"
        url = f"{FILE_HOST}/files-pri/T1-{file_id}/download/{name}"
        self._files[url] = data
        platform.web.files[file_id] = {
            "id": file_id, "name": name, "title": name, "mimetype": mime, "filetype": name.rsplit(".", 1)[-1],
            "user": "U1", "size": len(data), "created": 1710000000, "url_private_download": url,
            "permalink": f"https://synthetic-workspace.slack.com/files/U1/{file_id}/{name}",
            "shares": {"private": {"C1": [{"ts": "1710000000.000200"}]}},
        }
        return "download_slack_file", {"file_id": file_id}

    async def _fetch(self, url: str, mimetype: str, *, bot_token: str, allowed_hosts: Any) -> bytearray:
        assert bot_token in (BOT_TOKEN, SECOND_BOT_TOKEN) and url.startswith(FILE_HOST)
        return bytearray(self._files[url])

    #: The settings that turn on all the connector shows of a running turn. The card is due at once, and the
    #: spacing of writes is taken out: a check reads what is shown, not when.
    shows = "platforms: {slack: {enable_streaming: true, activity_card: true, activity_card_delay_seconds: 0}}\n"

    def configure(self, settings: str) -> str:
        # No acknowledgement and no activity card, unless a check asks: either would put a call in every log.
        defaults = {"enforce_audience": True, "acknowledge_mode": "off", "activity_card": False}
        # The section turns Slack on: the kit sets its tokens.
        return with_section(re.sub(r"\bloopback\b", NAME, settings), NAME, defaults=defaults)

    def attach(self, host: ConversationHost) -> SlackDriver:
        return SlackDriver(host, self.sockets[-1], self._files)

    def said(self, text: str) -> str:
        return _ENVELOPE.sub("", text)

    def widens(self, platform: SlackDriver, origin: str, target: str) -> None:
        # The target has one member the origin lacks, which is what the audience rule asks about.
        platform.web.members.update({origin: ["U1", BOT_USER], target: ["U1", "U0NEWREADER", BOT_USER]})


class SlackWorkspacesKit(SlackKit):
    """Two Slack apps behind one connector, as ``platforms.slack.workspaces`` lists them.

    A check's workspace ``W1`` is the first app's workspace and ``W2`` the second's. The fake transport gives
    both the same channel, user and message ids, so that only the workspace keeps them apart.
    """

    def __init__(self, monkeypatch: Any) -> None:
        super().__init__(monkeypatch)
        monkeypatch.setenv("SLACK_BOT_TOKEN_TWO", SECOND_BOT_TOKEN)
        monkeypatch.setenv("SLACK_APP_TOKEN_TWO", SECOND_APP_TOKEN)

    def configure(self, settings: str) -> str:
        workspaces = [
            {"bot_token": f"${{{connector.BOT_TOKEN_ENV}}}", "app_token": f"${{{connector.APP_TOKEN_ENV}}}"},
            {"bot_token": "${SLACK_BOT_TOKEN_TWO}", "app_token": "${SLACK_APP_TOKEN_TWO}"},
        ]
        return with_section(super().configure(settings), NAME, defaults={}, forced={"workspaces": workspaces})

    def attach(self, host: ConversationHost) -> SlackDriver:
        first, second = self.sockets[-2:]
        assert (first.web.team, second.web.team) == (TEAM, SECOND_TEAM)
        return SlackDriver(host, first, self._files, sockets={TEAM: first, SECOND_TEAM: second})
