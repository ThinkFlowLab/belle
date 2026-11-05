"""The Mattermost platform's kit for the conformance suite.

The platform under test is the Mattermost connector, built from its section
like any other. Mattermost itself is the fake transport of
``tests/mattermost/transport.py``: a check's inbound record becomes the
WebSocket event the server would send, handed to the connector's event
handler, and the log is read from the REST calls the connector made. No
connection is opened and the token is synthetic.

The kit turns the connector's reactions off. With the defaults, a message
that starts a turn gets a reaction, so no check could find a conversation
untouched. ``tests/mattermost/test_mattermost_outbound.py`` checks the marks.
"""

import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1] / "mattermost"))

from conformance_kit import with_section
from transport import TOKEN, EventFeeder, FakeRest, hello, posted

from belle_agent.backend import APPROVAL, Question, read_data_uri
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
from belle_agent.platforms.loopback.platform import Outbound
from belle_agent.platforms.mattermost import connector, surfaces
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.settings import MARK_EMOJI, TOKEN_ENV
from belle_agent.tools.host import APPROVE, REJECT

NAME = "mattermost"
#: What the connector adds to a turn's text: the ids its tools take.
_ENVELOPE = re.compile(r"\n\n\[chat_id: [^\]]*\]\Z")
#: The checks' own chat types, in Mattermost's words.
_CHAT_TYPES = {"dm": "D", "channel": "O"}


#: How the title of an activity card starts.
_CARD_TITLES = ("Working", "Turn ")

_FILE_NUMBERS = itertools.count(1)


def _card_title(post: dict[str, Any]) -> str:
    """The title of the one attachment of a post, or nothing."""
    attachments = (post.get("props") or {}).get("attachments") or []
    return str(attachments[0].get("title") or "") if len(attachments) == 1 else ""


def next_file_id() -> str:
    """A file id of the server's form, new each time."""
    return f"f{next(_FILE_NUMBERS):025d}"


class MattermostDriver:
    """Deliver records as the server would, and read back what the connector asked the server to do.

    Where the connector serves several servers, ``servers`` names the server of each workspace a check writes, and
    a record goes to the server of its workspace. The log then names the workspace of the server each action
    was asked of.
    """

    def __init__(self, host: ConversationHost, rest: FakeRest, servers: dict[str, tuple[str, FakeRest]] | None = None) -> None:
        self._platform = host.platforms[NAME]
        self._rest = rest
        members = self._platform.members if hasattr(self._platform, "members") else {self._platform.server: self._platform}
        #: The connector and the server of each workspace a check writes, the first for any other.
        self._servers = {workspace: (members[server], served) for workspace, (server, served) in (servers or {}).items()}
        self._first = next(iter(self._servers.values()), (self._platform, rest))
        #: Each question the connector posted, by its post id, and the workspace of each message delivered.
        self._questions: dict[str, Question] = {}
        self._workspaces: dict[str, str] = {}
        self._seqs: dict[str, int] = {}
        ask = self._platform.ask

        async def remember(conversation: str, thread: str, question: Question, **where: Any) -> str:
            post_id = await ask(conversation, thread, question, **where)
            self._questions[post_id] = question
            return post_id

        self._platform.ask = remember
        #: What the host handed the connector of each running turn, and how it ended what the connector showed.
        self.progressed: list[tuple[str, Any]] = []
        self.concluded: list[tuple[str, str, str]] = []
        progress, conclude = self._platform.progress, self._platform.conclude

        async def handed(inbound: InboundMessage, event: Any) -> None:
            self.progressed.append((inbound.message_id, event))
            await progress(inbound, event)

        async def ended(inbound: InboundMessage, state: str, final: str = "") -> bool:
            self.concluded.append((inbound.message_id, state, final))
            return bool(await conclude(inbound, state, final))

        self._platform.progress, self._platform.conclude = handed, ended

    def _at(self, workspace: str) -> tuple[Any, FakeRest]:
        """The connector and the server that a record of this workspace reaches."""
        return self._servers.get(workspace, self._first)

    @property
    def log(self) -> list[Outbound]:
        if not self._servers:
            return self._log(self._rest, "")
        return [entry for workspace, (_member, rest) in self._servers.items() for entry in self._log(rest, workspace)]

    def _log(self, rest: FakeRest, workspace: str) -> list[Outbound]:
        seen: list[Outbound] = []
        held = {**{post["id"]: post for post in rest.deleted}, **rest.posts}
        posts = iter(sorted(held.values(), key=lambda post: post["id"]))
        for request in rest.requests:
            if request.method == "POST" and request.path in ("/posts", "/posts/ephemeral"):
                post = next(posts)
                if post["id"] not in rest.posts:
                    continue  # Taken down: no post.
                fields = {"thread": post["root_id"], "text": post["message"], "message_id": post["id"], "workspace": workspace}
                if request.path.endswith("ephemeral"):
                    seen.append(Outbound("ephemeral", post["channel_id"], to=request.body["user_id"], **fields))
                elif _card_title(post).startswith(_CARD_TITLES):
                    seen.append(Outbound("activity", post["channel_id"], **{**fields, "text": _card_title(post)}))
                elif (question := self._questions.get(post["id"])) is not None:
                    kind = "approval" if question.kind == APPROVAL else "question"
                    seen.append(Outbound(kind, post["channel_id"], question=question, **{**fields, "text": question.text}))
                else:
                    seen.append(Outbound("post", post["channel_id"], **fields))
            elif request.path == "/reactions":
                seen.append(Outbound("reaction", "", message_id=request.body["post_id"], name=request.body["emoji_name"],
                                     workspace=workspace))
            elif request.method == "DELETE" and "/reactions/" in request.path:
                _, _, _, message_id, _, name = request.path.strip("/").split("/")
                seen.append(Outbound("reaction", "", message_id=message_id, name=name, removed=True, workspace=workspace))
        return seen

    def _files(self, record: InboundMessage) -> dict[str, Any]:
        """The fields of a post that carries the record's files, each put on the server in its conversation."""
        if not record.files:
            return {}
        infos = []
        for entry in record.files:
            mime, data = read_data_uri(str(entry["uri"]))
            infos.append(self._at(record.workspace)[1].file(next_file_id(), record.conversation, str(entry["name"]), data, mime, post=record.message_id))
        return {"file_ids": [info["id"] for info in infos], "metadata": {"files": infos}}

    async def _click(self, record: InboundAnswer | InboundStop) -> str:
        """Press a button as a person does: the server posts the click, with the button's context, to the listener."""
        member, _rest = self._at(record.workspace)
        assert member.questions is not None, "the connector has no listener, so no button"
        if isinstance(record, InboundStop):
            button = member.questions.stop_button(
                "Stop", workspace=self._workspaces.get(record.message_id, record.workspace), message_id=record.message_id
            )
            post_id, context = "card", dict(button["integration"]["context"])
        else:
            assert self._questions, "the connector posted no question, so there is nothing to click"
            post_id = next((key for key, shown in self._questions.items() if shown.id == record.question_id), "")
            question = self._questions.get(post_id) or list(self._questions.values())[-1]
            post_id = post_id or list(self._questions)[-1]
            if record.answer.approved is not None:
                value = APPROVE if record.answer.approved else REJECT
            else:
                value = record.answer.values[0]
            asked_on = next(rest for _, rest in [*self._servers.values(), self._first] if post_id in rest.posts)
            (attachment,) = asked_on.posts[post_id]["props"]["attachments"]
            context = dict(attachment["actions"][question.options.index(value)]["integration"]["context"])
            # A question the connector never showed: the click of a button that names it, as a stale post carries.
            context["record"] = record.question_id
        request = {"user_id": record.sender, "user_name": record.sender, "post_id": post_id,
                   "channel_id": record.conversation, "context": context}
        _, outcome = await member.questions.click(request)
        return outcome

    async def _event(self, record: InboundEvent) -> str:
        """The WebSocket event the server sends for a reaction or for a member. A bot's own carries the bot's id."""
        member, rest = self._at(record.workspace)
        actor = member.bot_user_id if record.actor_is_bot else record.actor
        direct = record.direct or record.chat_type == "dm"
        rest.channels.setdefault(
            record.conversation,
            {"id": record.conversation, "type": "D" if direct else "O", "team_id": "" if direct else record.workspace},
        )
        if record.anchor and record.anchor_thread not in ("", record.anchor):
            rest.posts.setdefault(record.anchor, {"id": record.anchor, "root_id": record.anchor_thread})
        # The same delivery comes again under the same number of the same connection.
        seq = self._seqs.setdefault(record.event_id, len(self._seqs) + 100)
        broadcast = {"channel_id": record.conversation}
        if record.kind == "reaction":
            reaction = {"user_id": actor, "post_id": record.anchor, "emoji_name": "tada", "channel_id": record.conversation}
            event = {"event": "reaction_added", "data": {"reaction": json.dumps(reaction)}, "broadcast": broadcast}
        elif record.kind == "member":
            event = {"event": "user_added", "data": {"user_id": actor, "team_id": record.workspace}, "broadcast": broadcast}
        else:
            raise NotImplementedError(f"the Mattermost connector reports no {record.kind} event")
        return await member.handle({**event, "seq": seq})

    async def inject(self, record: Any) -> str:
        if isinstance(record, (InboundAnswer, InboundStop)):
            return await self._click(record)
        if isinstance(record, InboundEvent):
            return await self._event(record)
        if not isinstance(record, InboundMessage):
            raise NotImplementedError(f"the Mattermost connector receives no {type(record).__name__} yet")
        member, rest = self._at(record.workspace)
        direct = record.trigger == TRIGGER_DM or record.chat_type == "dm"
        self._workspaces[record.message_id] = member.workspace("D" if direct else "O", record.workspace)
        text = record.text
        if record.trigger == TRIGGER_MENTION:
            text = f"@belle {text}"
        if record.trigger == TRIGGER_REPLY:
            rest.posts.setdefault(record.thread, {"id": record.thread, "user_id": member.bot_user_id})
        event = posted(
            record.message_id,
            channel=record.conversation,
            user=record.sender,
            message=text,
            channel_type="D" if direct else "O",
            team="" if direct else record.workspace,
            root=record.thread,
            mentions=[member.bot_user_id] if record.trigger == TRIGGER_MENTION else None,
            edited=record.edited,
            props={"from_bot": "true"} if record.sender_is_bot else {},
            **self._files(record),
        )
        return await member.handle(event)


class MattermostKit:
    name = NAME
    #: The settings that turn on all the connector shows of a running turn. The card is due at once, and the
    #: spacing of its edits is taken out: a check reads what is shown, not when.
    shows = "platforms: {mattermost: {enable_streaming: true, activity_card: true, activity_card_delay_seconds: 0, activity_card_min_edit_seconds: 0}}\n"

    def __init__(self, monkeypatch: Any) -> None:
        self.rest = FakeRest()
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
        monkeypatch.setattr(connector, "MattermostClient", self._client)
        monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.0)

    def _client(self, url: str, token: str) -> MattermostClient:
        return MattermostClient(url, token, transport=self.rest.transport(), open_events=EventFeeder(hello()).open)

    def configure(self, settings: str) -> str:
        text = re.sub(r"\bloopback\b", NAME, settings)
        text = re.sub(r"\bchat_type: (dm|channel)\b", lambda found: f"chat_type: {_CHAT_TYPES[found[1]]}", text)
        defaults = {
            "url": "http://mattermost.invalid",
            # The listener of clicks binds loopback on a free port. Nothing posts to it: the driver clicks directly.
            "actions": {"bind": "127.0.0.1", "port": 0, "url": "http://belle.invalid:18400"},
            **{key: "disabled" for key in MARK_EMOJI},
        }
        # The section turns Mattermost on: the kit sets its token, and the url is literal.
        return with_section(text, NAME, defaults=defaults)

    def attach(self, host: ConversationHost) -> MattermostDriver:
        return MattermostDriver(host, self.rest)

    def said(self, text: str) -> str:
        return _ENVELOPE.sub("", text)

    def widens(self, platform: Any, origin: str, target: str) -> None:
        raise NotImplementedError(
            "a check names its conversations C1 and C2, which are no Mattermost channel ids: the posting tool"
            " refuses chat_id C2 as malformed, so the post a person agrees to would never be made"
        )

    def holds(self, platform: Any, name: str, mime: str, data: bytes) -> tuple[str, dict[str, Any]]:
        file_id = next_file_id()
        self.rest.file(file_id, "C1", name, data, mime)
        return "download_file", {"file_id": file_id}


class MattermostServersKit(MattermostKit):
    """Two servers behind one connector, as ``platforms.mattermost.servers`` lists them.

    A check's workspace ``W1`` is a team of the first server, and any other workspace a team of the second.
    Each server has its own bot, its own tokens and post ids of its own, so that nothing a check reads can come
    from the wrong server by chance.
    """

    #: Each server's name, the workspace a check writes for it, and its address.
    SERVERS = (("one", "W1", "http://one.invalid"), ("two", "W2", "http://two.invalid"))

    def __init__(self, monkeypatch: Any) -> None:
        super().__init__(monkeypatch)
        self.rests = {self.SERVERS[0][2]: self.rest, self.SERVERS[1][2]: FakeRest(bot="b0t2" * 6 + "b0", post_letter="q")}
        #: The WebSocket of each server, by address.
        self.feeders: dict[str, EventFeeder] = {}
        for name, _workspace, _url in self.SERVERS:
            monkeypatch.setenv(self.token_env(name), self.token(name))
            monkeypatch.setenv(self.command_env(name), f"command-token-{name}")

    @staticmethod
    def token_env(name: str) -> str:
        return f"MATTERMOST_BOT_TOKEN_{name.upper()}"

    @staticmethod
    def command_env(name: str) -> str:
        return f"MATTERMOST_COMMAND_TOKEN_{name.upper()}"

    @staticmethod
    def token(name: str) -> str:
        return f"{TOKEN}-{name}"

    def _client(self, url: str, token: str) -> MattermostClient:
        self.feeders[url] = EventFeeder(hello())
        return MattermostClient(url, token, transport=self.rests[url].transport(), open_events=self.feeders[url].open)

    def configure(self, settings: str) -> str:
        servers = [
            # Each listener binds loopback on a free port.
            {"name": name, "url": url, "bot_token_env": self.token_env(name), "command_token_env": self.command_env(name),
             "actions": {"bind": "127.0.0.1", "port": 0, "url": f"http://belle-{name}.invalid:18400"}}
            for name, _workspace, url in self.SERVERS
        ]
        text = super().configure(settings)
        return with_section(text, NAME, defaults={}, forced={"url": "", "actions": {}, "servers": servers})

    def attach(self, host: ConversationHost) -> MattermostDriver:
        servers = {workspace: (name, self.rests[url]) for name, workspace, url in self.SERVERS}
        return MattermostDriver(host, self.rest, servers)
