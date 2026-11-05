"""The Discord platform's kit for the conformance suite.

The platform under test is the Discord connector, built from its section like any other. Discord itself is
the fake of ``tests/discord/discord_transport.py``: the gateway opens, sends Hello and READY and then stays
quiet, and a check's inbound record becomes the gateway event Discord would send, handed to the
connector's event handler. The log is read from the REST calls the connector made. No connection is
opened and the token is synthetic.

The kit turns the connector's reactions off, as the Mattermost kit does: with the defaults a message that
starts a turn gets a reaction, so no check could find a conversation untouched.
"""

import itertools
import re
import sys
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1] / "discord"))

from conformance_kit import with_section
from discord_transport import BOT_ID, TOKEN, FakeGateway, FakeRest, hello, ready

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
from belle_agent.platforms.discord import connector, surfaces
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.questions import INTERACTION_COMPONENT, INTERACTION_MODAL, KIND_MODAL, TEXT_INPUT_ID
from belle_agent.platforms.discord.settings import MARK_EMOJI, TOKEN_ENV
from belle_agent.platforms.loopback.platform import Outbound
from belle_agent.tools.host import APPROVE, REJECT

NAME = "discord"
#: What the connector adds to a turn's text: the ids its tools take.
_ENVELOPE = re.compile(r"\n\n\[chat_id: [^\]]*\]( \[trigger: [a-z]+\])?\Z")
#: The checks' own chat types, in Discord's words.
_CHAT_TYPES = {"dm": "dm", "channel": "text"}
_TEXT, _DM, _THREAD = 0, 1, 11


class DiscordDriver:
    """Deliver records as Discord's gateway would, and read back what the connector asked Discord to do.

    Where the connector serves several bots, ``bots`` names the bot, its fake Discord and its user id for each
    workspace a check writes, which is the guild that bot is in. A record goes to the bot of its workspace, and
    the log names the workspace of the bot each action was asked of.
    """

    def __init__(
        self, host: ConversationHost, rest: FakeRest, bots: dict[str, tuple[str, FakeRest, str]] | None = None
    ) -> None:
        self._platform = host.platforms[NAME]
        self._rest = rest
        self._bots = dict(bots or {})
        #: A check may name one thread in two conversations; a Discord thread is one channel's. Each pair gets a
        #: thread channel of its own, and the log names the check's thread again.
        self._threads: dict[tuple[str, str], str] = {}
        self._named: dict[str, str] = {}
        self._members: dict[str, set[str]] = {}
        self._files = itertools.count(1)
        self._clicks = itertools.count(1)
        self._event_sequences: dict[str, int] = {}
        #: Each question the connector posted, by the id of the message that holds its components.
        self._questions: dict[str, Question] = {}
        ask = self._platform.ask

        async def remember(conversation: str, thread: str, question: Question, **where: Any) -> str:
            message_id = await ask(conversation, thread, question, **where)
            self._questions[message_id] = question
            return message_id

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

    def _at(self, workspace: str) -> tuple[FakeRest, str]:
        """The fake Discord and the bot user id that a record of this workspace reaches: the first bot's for any other."""
        if not self._bots:
            return self._rest, BOT_ID
        _, rest, bot = self._bots.get(workspace) or next(iter(self._bots.values()))
        return rest, bot

    def _connector(self, workspace: str) -> Any:
        """Select a named bot when the host uses a multi-bot Discord port."""
        if self._bots:
            name = (self._bots.get(workspace) or next(iter(self._bots.values())))[0]
            return self._platform.members[name]
        members = getattr(self._platform, "members", {})
        name, separator, _ = workspace.partition(":")
        return members.get(name, self._platform) if separator else self._platform

    @staticmethod
    def _guild(workspace: str) -> str:
        """Drop a named bot prefix from the guild ID sent over Discord's gateway."""
        _, separator, guild = workspace.partition(":")
        return guild if separator else workspace

    @property
    def log(self) -> list[Outbound]:
        """Every post and reaction the connector asked Discord for, in order, in the loopback words."""
        if not self._bots:
            return self._log(self._rest, "")
        return [entry for workspace, (_, rest, _bot) in self._bots.items() for entry in self._log(rest, workspace)]

    def _log(self, rest: FakeRest, workspace: str) -> list[Outbound]:
        seen: list[Outbound] = []
        # A message as its last edit left it, and the messages taken down.
        edited: dict[str, dict[str, Any]] = {}
        deleted: set[str] = set()
        for request in rest.requests:
            parts = request.path.strip("/").split("/")
            if parts[0] == "channels" and len(parts) == 4 and parts[2] == "messages":
                if request.method == "PATCH":
                    edited.setdefault(parts[3], {}).update(request.body)
                elif request.method == "DELETE":
                    deleted.add(parts[3])
        posted = iter(rest.posted)
        for request in rest.requests:
            parts = request.path.strip("/").split("/")
            if request.method == "POST" and len(parts) == 3 and parts[2] == "messages":
                channel, body, message_id = next(posted)
                if message_id in deleted:
                    continue  # Taken down: no message.
                body = {**body, **edited.get(message_id, {})}
                record = rest.channels.get(channel, {})
                in_thread = record.get("type") == _THREAD
                conversation = str(record.get("parent_id") or channel) if in_thread else channel
                fields = {"thread": self._named.get(channel, channel) if in_thread else "", "message_id": message_id,
                          "workspace": workspace}
                if (question := self._questions.get(message_id)) is not None:
                    kind = "approval" if question.kind == APPROVAL else "question"
                    seen.append(Outbound(kind, conversation, question=question, text=question.text, **fields))
                elif body.get("embeds"):
                    seen.append(Outbound("activity", conversation, text=str(body["embeds"][0].get("title") or ""), **fields))
                else:
                    seen.append(Outbound("post", conversation, text=str(body.get("content") or ""), **fields))
            elif request.method in ("PUT", "DELETE") and "reactions" in parts:
                seen.append(Outbound("reaction", "", message_id=parts[3], name=parts[5], removed=request.method == "DELETE",
                                     workspace=workspace))
        return seen

    def _channel(self, record: InboundMessage) -> str:
        """The channel the record's message is in, made known to the fake Discord."""
        direct = record.trigger == TRIGGER_DM or record.chat_type == "dm"
        guild = self._guild(record.workspace)
        rest, bot = self._at(record.workspace)
        rest.channels.setdefault(
            record.conversation,
            {"id": record.conversation, "type": _DM if direct else _TEXT,
             "guild_id": "" if direct else guild,
             "recipients": [{"id": record.sender}] if direct else []},
        )
        if not direct:
            members = self._members.setdefault(guild, {bot})
            members.add(record.sender)
            rest.answer("GET", f"/guilds/{guild}", (200, {
                "id": guild, "owner_id": bot,
                "roles": [{"id": guild, "permissions": str((1 << 10) | (1 << 11) | (1 << 38))}],
            }))
            rest.answer("GET", f"/guilds/{guild}/members", (200, [
                {"user": {"id": user}, "roles": []} for user in sorted(members)
            ]))
        if not record.thread:
            return record.conversation
        key = (record.conversation, record.thread)
        if key not in self._threads:
            taken = any(thread == record.thread for _, thread in self._threads)
            self._threads[key] = f"{record.thread}@{record.conversation}" if taken else record.thread
            self._named[self._threads[key]] = record.thread
        channel = self._threads[key]
        owner = bot if record.trigger == TRIGGER_REPLY else record.sender
        rest.channels.setdefault(
            channel,
            {"id": channel, "type": _THREAD, "parent_id": record.conversation, "owner_id": owner, "guild_id": guild},
        )
        return channel

    def _attachments(self, record: InboundMessage) -> list[dict[str, Any]]:
        """The record's files as attachments on Discord's CDN, which the fake serves."""
        found = []
        for entry in record.files:
            mime, data = read_data_uri(str(entry["uri"]))
            number = next(self._files)
            path = f"/attachments/{record.conversation}/{number}/{entry['name']}"
            self._at(record.workspace)[0].answer("GET", path, (200, data))
            found.append({"id": str(number), "filename": str(entry["name"]), "size": len(data),
                          "url": f"https://cdn.discordapp.com{path}", "content_type": mime})
        return found

    async def _event(self, record: InboundEvent) -> str:
        guild = self._guild(record.workspace)
        platform = self._connector(record.workspace)
        rest, bot = self._at(record.workspace)
        if record.kind == "member":
            rest.channels.setdefault(record.conversation, {
                "id": record.conversation, "type": _TEXT, "guild_id": guild,
            })
            rest.answer("GET", f"/guilds/{guild}", (200, {
                "id": guild, "owner_id": record.actor, "system_channel_id": record.conversation,
                "roles": [{"id": guild, "permissions": str((1 << 10) | (1 << 11))}],
            }))
            rest.answer("GET", f"/guilds/{guild}/members/{bot}", (200, {
                "user": {"id": bot}, "roles": [],
            }))
            sequence = self._event_sequences.setdefault(record.event_id, len(self._event_sequences) + 50)
            return await platform.handle({"op": 0, "s": sequence, "t": "GUILD_MEMBER_ADD", "d": {
                "guild_id": guild, "user": {"id": record.actor},
            }})
        if record.kind != "reaction":
            raise NotImplementedError(f"the Discord kit reports no {record.kind} event: member events need GUILD_MEMBERS")
        channel = record.anchor_thread if record.anchor_thread not in ("", record.anchor) else record.conversation
        rest.channels.setdefault(record.conversation, {"id": record.conversation, "type": _TEXT,
                                                        "guild_id": guild})
        actor = "1000000000000000999" if record.actor_is_bot else record.actor
        data = {"user_id": actor, "channel_id": channel, "message_id": record.anchor, "guild_id": guild,
                "emoji": {"name": "\N{PARTY POPPER}"},
                "member": {"user": {"id": actor, "bot": record.actor_is_bot}}}
        return await platform.handle({"op": 0, "s": 50, "t": "MESSAGE_REACTION_ADD", "d": data})

    async def _click(self, record: InboundAnswer | InboundStop) -> str:
        """Press a component as a person does: Discord sends the interaction over the gateway, with its custom id."""
        questions = self._connector(record.workspace).questions
        guild = self._guild(record.workspace)
        rest, _bot = self._at(record.workspace)
        rest.channels.setdefault(record.conversation, {"id": record.conversation, "type": _TEXT,
                                                        "guild_id": guild})
        kind, data = INTERACTION_COMPONENT, {}
        if isinstance(record, InboundStop):
            data = {"custom_id": questions.stop_button("Stop", message_id=record.message_id)["custom_id"]}
        else:
            assert self._questions, "the connector posted no question, so there is nothing to click"
            message_id = next((key for key, shown in self._questions.items() if shown.id == record.question_id), "")
            body = next(body for _, body, posted_id in rest.posted if posted_id == (message_id or list(self._questions)[-1]))
            component = body["components"][0]["components"][0]
            # A question the connector never showed: a component that names no question it waits on.
            known = bool(message_id)
            custom_id = component["custom_id"] if known else re.sub(r":(a|s|t):[0-9]+", r":\1:0", component["custom_id"])
            question = self._questions.get(message_id) or list(self._questions.values())[-1]
            if record.answer.approved is not None:
                value = APPROVE if record.answer.approved else REJECT
            else:
                value = record.answer.values[0]
            if not question.options:
                kind = INTERACTION_MODAL
                custom_id = re.sub(r":t:", f":{KIND_MODAL}:", custom_id)
                data = {"custom_id": custom_id, "components": [{"type": 1, "components": [
                    {"type": 4, "custom_id": TEXT_INPUT_ID, "value": value}]}]}
            elif component.get("options"):
                data = {"custom_id": custom_id, "values": [str(question.options.index(value))]}
            else:
                buttons = body["components"][0]["components"]
                custom_id = buttons[question.options.index(value)]["custom_id"]
                data = {"custom_id": custom_id if known else re.sub(r":a:[0-9]+:", ":a:0:", custom_id)}
        interaction: dict[str, Any] = {"id": str(next(self._clicks)), "type": kind, "token": f"click-{next(self._clicks)}",
                                       "channel_id": record.conversation, "data": data}
        user = {"id": record.sender, "username": record.sender}
        if rest.channels[record.conversation]["type"] == _DM:
            interaction["user"] = user
        else:
            interaction.update(guild_id=guild, member={"user": user})
        return await questions.on_interaction(interaction)

    async def inject(self, record: Any) -> str:
        """Deliver one record as the gateway event Discord would send, and return the host's outcome."""
        if isinstance(record, (InboundAnswer, InboundStop)):
            return await self._click(record)
        if isinstance(record, InboundEvent):
            return await self._event(record)
        if not isinstance(record, InboundMessage):
            raise NotImplementedError(f"the Discord connector receives no {type(record).__name__}")
        channel = self._channel(record)
        rest, bot = self._at(record.workspace)
        direct = rest.channels[record.conversation]["type"] == _DM
        text = record.text
        mentions = []
        if record.trigger == TRIGGER_MENTION:
            text, mentions = f"<@{bot}> {text}", [{"id": bot}]
        data: dict[str, Any] = {
            "id": record.message_id, "channel_id": channel, "content": text, "mentions": mentions, "type": 0,
            "author": {"id": record.sender, "username": record.sender, "bot": record.sender_is_bot},
            "attachments": self._attachments(record),
        }
        if not direct:
            data["guild_id"] = self._guild(record.workspace)
        kind = "MESSAGE_UPDATE" if record.edited else "MESSAGE_CREATE"
        if record.edited:
            data["edited_timestamp"] = record.event_id
        return await self._connector(record.workspace).handle({"op": 0, "s": 20, "t": kind, "d": data})


class DiscordKit:
    """The conformance suite's Discord: the connector on a fake REST API and gateway."""

    name = NAME
    #: The settings that turn on all the connector shows of a running turn. The card is due at once, and the
    #: spacing of its edits is taken out: a check reads what is shown, not when.
    shows = "platforms: {discord: {enable_streaming: true, activity_card: true, activity_card_delay_seconds: 0, activity_card_min_edit_seconds: 0}}\n"

    def __init__(self, monkeypatch: Any) -> None:
        self.rest = FakeRest(serve=True)
        self.rest.gateway()
        self.gateway = FakeGateway(*[self._opens] * 8)
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
        monkeypatch.setattr(connector, "DiscordClient", self._client)
        monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.0)

    @staticmethod
    def _opens(socket: Any) -> None:
        socket.feed(hello(41250))
        socket.feed(ready())

    def _client(self, token: str) -> DiscordClient:
        return DiscordClient(token, transport=self.rest.transport(), open_socket=self.gateway.open)

    def configure(self, settings: str) -> str:
        """A check's settings with Discord in place of loopback, and the connector's reactions and typing off."""
        text = re.sub(r"\bloopback\b", NAME, settings)
        text = re.sub(r"\bchat_type: (dm|channel)\b", lambda found: f"chat_type: {_CHAT_TYPES[found[1]]}", text)
        # The section turns Discord on: the kit sets its token.
        return with_section(text, NAME, defaults={"typing_indicator": False, **{key: "disabled" for key in MARK_EMOJI}})

    def attach(self, host: ConversationHost) -> DiscordDriver:
        """What a check drives the connector through."""
        return DiscordDriver(host, self.rest)

    def said(self, text: str) -> str:
        """A turn's text without the ids the connector adds."""
        return _ENVELOPE.sub("", text)

    def widens(self, platform: Any, origin: str, target: str) -> None:
        """The conformance suite's channel names are not Discord IDs."""
        raise NotImplementedError(
            "the check names C1 and C2, which are not Discord channel IDs; the posting tool refuses chat_id C2"
        )

    def holds(self, platform: Any, name: str, mime: str, data: bytes) -> tuple[str, dict[str, Any]]:
        """No tool reads a file yet."""
        raise NotImplementedError("the Discord connector has no tool that reads a file yet (note 14, D6)")


class DiscordBotsKit(DiscordKit):
    """Two Discord bots behind one connector, as ``platforms.discord.bots`` lists them.

    A check's workspace ``W1`` is a guild of the bot ``east``, and any other workspace a guild of the bot ``west``.
    Each bot has its own fake Discord, its own user id and message ids of its own, so that nothing a check reads
    can come from the wrong bot by chance.
    """

    #: Each bot's name, the workspace a check writes for it, its token and its user id.
    BOTS = (("east", "W1", "synthetic-token-east", BOT_ID), ("west", "W2", "synthetic-token-west", "1000000000000000117"))

    def __init__(self, monkeypatch: Any) -> None:
        super().__init__(monkeypatch)
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        #: The fake Discord and the gateway of each bot, by token.
        self.fakes: dict[str, tuple[FakeRest, FakeGateway]] = {}
        for index, (name, _workspace, token, user) in enumerate(self.BOTS):
            rest = self.rest if index == 0 else FakeRest(serve=True, _next_id=1720000000000000000)
            rest.gateway()
            payload = ready()
            payload["d"]["user"]["id"] = payload["d"]["application"]["id"] = user

            def opens(socket: Any, payload: dict[str, Any] = payload) -> None:
                socket.feed(hello(41250))
                socket.feed(payload)

            self.fakes[token] = (rest, FakeGateway(*[opens] * 8))
            monkeypatch.setenv(self.token_env(name), token)

    @staticmethod
    def token_env(name: str) -> str:
        return f"DISCORD_BOT_TOKEN_{name.upper()}"

    def _client(self, token: str) -> DiscordClient:
        rest, gateway = self.fakes[token]
        return DiscordClient(token, transport=rest.transport(), open_socket=gateway.open)

    def configure(self, settings: str) -> str:
        bots = [{"name": name, "token_env": self.token_env(name)} for name, _workspace, _token, _user in self.BOTS]
        return with_section(super().configure(settings), NAME, defaults={}, forced={"bots": bots})

    def attach(self, host: ConversationHost) -> DiscordDriver:
        bots = {workspace: (name, self.fakes[token][0], user) for name, workspace, token, user in self.BOTS}
        return DiscordDriver(host, self.rest, bots)
