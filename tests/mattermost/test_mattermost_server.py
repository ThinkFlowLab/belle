"""The connector and a host against a Mattermost test server, with the scripted backend.

``tests/conftest.py`` skips these checks unless ``MATTERMOST_TEST_URL`` and
``MATTERMOST_TEST_ADMIN_TOKEN`` are set. With the admin's token, each check
makes a team, a bot account, two users and their channels, all with fresh
names, and archives them after.
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import DOCUMENTS, ModelTurn, RecordedBackend, scripted_second_brain
from decision_standin import KEY, KEY_ENV, DecisionStandIn
from scripted_backend import ScriptedBackend

from belle_agent.attachments import FILE_REASON_TOO_LARGE
from belle_agent.backend import APPROVAL, EVENT_TEXT_DELTA, Question, TurnEvent
from belle_agent.config import BelleConfigError, load_config
from belle_agent.host import OUTCOME_POSTED_BY_TOOL, OUTCOME_WITHHELD, host_from_config
from belle_agent.message_decisions import ADVISORY_PREAMBLE
from belle_agent.platforms.base import MARK_DONE, MARK_RECEIVED, TRIGGER_DM, InboundMessage
from belle_agent.platforms.mattermost import connector as mattermost_connector
from belle_agent.platforms.mattermost import socket as socket_module
from belle_agent.platforms.mattermost import surfaces
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import TOKEN_ENV, MattermostSettings
from belle_agent.tools.host import APPROVE, REJECT

pytestmark = pytest.mark.mattermost_server

URL = os.environ.get("MATTERMOST_TEST_URL", "")
ADMIN_TOKEN = os.environ.get("MATTERMOST_TEST_ADMIN_TOKEN", "")
#: How long a check waits for the bot's answer to reach the server.
ANSWER_SECONDS = 20.0


class Rest:
    """The REST API as one account."""

    def __init__(self, token: str) -> None:
        self.http = httpx.AsyncClient(base_url=f"{URL}/api/v4", headers={"Authorization": f"Bearer {token}"}, timeout=30)

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        answer = await self.http.request(method, path, **kwargs)
        answer.raise_for_status()
        return answer.json() if answer.content else None

    async def posts(self, channel: str) -> list[dict[str, Any]]:
        """Every post of a channel, oldest first."""
        found = await self.call("GET", f"/channels/{channel}/posts", params={"per_page": 200})
        return sorted(found["posts"].values(), key=lambda post: post["create_at"])


@dataclass
class Place:
    """A team with a bot, alice and bob, the channels the checks use, and a host on the bot."""

    admin: Rest
    tag: str
    team: str
    bot: dict[str, Any]
    bot_token: str
    users: dict[str, tuple[dict[str, Any], Rest]] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)

    def user(self, name: str) -> Rest:
        return self.users[name][1]

    def id(self, name: str) -> str:
        return self.users[name][0]["id"]

    async def bot_posts(self, channel: str, count: int) -> list[dict[str, Any]]:
        """Wait for ``count`` posts of the bot in a channel, then a while longer for any more. Return them all."""
        async with asyncio.timeout(ANSWER_SECONDS):
            while len(await self._bot_posts(channel)) < count:
                await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)
        return await self._bot_posts(channel)

    async def _bot_posts(self, channel: str) -> list[dict[str, Any]]:
        return [post for post in await self.admin.posts(channel) if post["user_id"] == self.bot["user_id"]]


async def _channel(admin: Rest, team: str, name: str, members: list[str], kind: str = "O") -> str:
    channel = await admin.call("POST", "/channels", json={"team_id": team, "name": name, "display_name": name, "type": kind})
    for member in members:
        await admin.call("POST", f"/channels/{channel['id']}/members", json={"user_id": member})
    return channel["id"]


@pytest.fixture
def config(place, monkeypatch):
    """The configuration of a host on the bot.

    ``lounge`` takes mentions and replies to the bot. ``chatter`` takes every message, and owes no reply.
    """
    monkeypatch.setenv(TOKEN_ENV, place.bot_token)
    return {
        "platforms": {"mattermost": {"url": URL}},
        "scopes": [
            {"match": {"channel": "mattermost", "chat": place.channels["lounge"]}, "delivery": {"mode": ["mention", "reply"]}},
            {"match": {"channel": "mattermost", "chat": place.channels["chatter"]}, "delivery": {"mode": ["all"], "reply": "optional"}},
        ],
    }


@pytest.fixture
async def belle(config, tmp_path):
    """A host on the bot, with the scripted backend."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    backend = ScriptedBackend()
    host = host_from_config(load_config(path), backend.backend, state_dir=tmp_path / "state")
    await host.start()
    yield host, backend
    await host.stop()


async def _ended(host: Any, count: int) -> list[str]:
    async with asyncio.timeout(ANSWER_SECONDS):
        while len(host.ended) < count:
            await asyncio.sleep(0.2)
    return [record.outcome for record in host.ended]


async def test_start_authenticates_and_reads_hello(place):
    connector = MattermostPlatform(MattermostSettings(URL, place.bot_token))

    async def ignore(_record):
        return "ignored"

    await connector.start(ignore)
    try:
        assert connector.bot_user_id == place.bot["user_id"] and connector.socket.connection_id
    finally:
        await connector.stop()


async def test_a_wrong_token_stops_the_start_and_names_its_variable():
    connector = MattermostPlatform(MattermostSettings(URL, "synthetic-wrong-token"))

    async def ignore(_record):
        return "ignored"

    with pytest.raises(BelleConfigError, match=f"did not accept the bot token of {TOKEN_ENV}") as refused:
        await connector.start(ignore)
    assert "synthetic-wrong-token" not in str(refused.value)


async def test_a_direct_message_gets_one_answer_in_the_direct_channel(place, belle):
    _, backend = belle
    backend.play(ModelTurn(final="hello alice"))
    alice = place.user("alice")
    direct = await alice.call("POST", "/channels/direct", json=[place.id("alice"), place.bot["user_id"]])

    sent = await alice.call("POST", "/posts", json={"channel_id": direct["id"], "message": "hello"})

    (answer,) = await place.bot_posts(direct["id"], 1)
    assert (answer["message"], answer["root_id"]) == ("hello alice", "")
    # The answer ended the turn, and its end took the acknowledgement off.
    assert not await alice.call("GET", f"/posts/{sent['id']}/reactions")


async def test_a_mention_gets_one_answer_in_its_thread(place, belle):
    _, backend = belle
    backend.play(ModelTurn(final="the tide turns at four"))
    lounge = place.channels["lounge"]

    sent = await place.user("bob").call(
        "POST", "/posts", json={"channel_id": lounge, "message": f"@belle-{place.tag} when does the tide turn?"}
    )

    (answer,) = await place.bot_posts(lounge, 1)
    assert (answer["message"], answer["root_id"]) == ("the tide turns at four", sent["id"])


async def test_a_reply_under_the_bot_s_post_gets_one_answer_in_that_thread(place, belle):
    host, backend = belle
    backend.play(ModelTurn(final="yes, at four"))
    lounge = place.channels["lounge"]
    root = await host.platform.post(lounge, "", "The ferry leaves at noon.")

    await place.user("bob").call("POST", "/posts", json={"channel_id": lounge, "message": "and the tide?", "root_id": root})

    posts = await place.bot_posts(lounge, 2)
    assert [(post["message"], post["root_id"]) for post in posts] == [("The ferry leaves at noon.", ""), ("yes, at four", root)]


async def test_a_message_where_no_reply_is_owed_and_none_was_posted_gets_no_answer(place, belle):
    host, backend = belle
    backend.play(ModelTurn(final="a thought nobody asked for"))
    chatter = place.channels["chatter"]

    await place.user("alice").call("POST", "/posts", json={"channel_id": chatter, "message": "nice weather"})

    async with asyncio.timeout(ANSWER_SECONDS):
        while not host.ended:
            await asyncio.sleep(0.2)
    assert [record.outcome for record in host.ended] == [OUTCOME_WITHHELD]
    assert await place.bot_posts(chatter, 0) == []


async def test_an_event_the_server_sends_again_after_a_resume_starts_no_second_turn(place, belle):
    host, _ = belle
    socket = host.platform.socket
    logged: list[logging.LogRecord] = []
    handler = logging.Handler(logging.INFO)
    handler.emit = logged.append
    socket_module.logger.addHandler(handler)
    socket_module.logger.setLevel(logging.INFO)
    alice = place.user("alice")
    direct = await alice.call("POST", "/channels/direct", json=[place.id("alice"), place.bot["user_id"]])
    before = socket.sequence

    await alice.call("POST", "/posts", json={"channel_id": direct["id"], "message": "hello"})
    await place.bot_posts(direct["id"], 1)
    # Resume from before the message: the server sends its event again.
    socket.sequence = before
    await socket._stream.close()
    async with asyncio.timeout(ANSWER_SECONDS):
        while socket.sequence <= before:
            await asyncio.sleep(0.2)
    await asyncio.sleep(2.0)

    socket_module.logger.removeHandler(handler)

    assert any("resumed connection" in record.getMessage() for record in logged)
    assert len(await place.bot_posts(direct["id"], 1)) == 1
    assert len(host.ended) == 1


async def test_one_host_answers_on_mattermost_and_on_loopback_each_on_its_own(place, tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, place.bot_token)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"platforms": {"loopback": {"enabled": True}, "mattermost": {"url": URL}}}), encoding="utf-8")
    backend = ScriptedBackend()
    host = host_from_config(load_config(path), backend.backend, state_dir=tmp_path / "state")
    await host.start()
    try:
        backend.play(ModelTurn(final="hello on mattermost"), ModelTurn(final="hello on loopback"))
        alice = place.user("alice")
        direct = await alice.call("POST", "/channels/direct", json=[place.id("alice"), place.bot["user_id"]])
        await alice.call("POST", "/posts", json={"channel_id": direct["id"], "message": "hello"})
        (answer,) = await place.bot_posts(direct["id"], 1)
        loopback = host.platforms["loopback"]
        message = InboundMessage(
            platform="loopback", workspace="W1", conversation="D-U1", message_id="m1", event_id="e1",
            sender="U1", text="hello", chat_type="dm", trigger=TRIGGER_DM,
        )
        assert (await loopback.inject(message)).startswith("dispatched:dm conversation=loopback")
        await asyncio.wait_for(host.idle(), ANSWER_SECONDS)
    finally:
        await host.stop()

    assert answer["message"] == "hello on mattermost"
    assert [(post.kind, post.text) for post in loopback.log] == [("post", "hello on loopback")]


# ── the tools of a turn ──────────────────────────────────────────────────


async def test_in_an_ambient_channel_a_turn_speaks_only_through_the_posting_tool(place, belle):
    host, backend = belle
    chatter = place.channels["chatter"]
    alice = place.user("alice")
    backend.play(
        ModelTurn(final="said in a tool", calls=(("post_message", {"text": "On the harbour wall."}),)),
        ModelTurn(final="a thought nobody asked for"),
    )

    await alice.call("POST", "/posts", json={"channel_id": chatter, "message": "where is the ferry timetable?"})
    await _ended(host, 1)
    await alice.call("POST", "/posts", json={"channel_id": chatter, "message": "nice weather"})

    assert await _ended(host, 2) == [OUTCOME_POSTED_BY_TOOL, OUTCOME_WITHHELD]
    (posted,) = await place.bot_posts(chatter, 1)
    assert (posted["message"], posted["root_id"]) == ("On the harbour wall.", "")


async def test_a_reaction_added_twice_is_one_reaction(place, belle):
    host, backend = belle
    # Not eyes: the mark of a running turn is eyes, and the turn's end takes it off.
    tada = ("react_to_message", {"emoji": "tada"})
    backend.play(ModelTurn(calls=(tada, ("react_to_message", {"emoji": "\N{THUMBS UP SIGN}"}), tada)))
    chatter = place.channels["chatter"]

    sent = await place.user("alice").call("POST", "/posts", json={"channel_id": chatter, "message": "the ferry is back"})
    await _ended(host, 1)

    assert [json.loads(result)["ok"] for result in backend.results] == [True, True, True]
    reactions = await place.admin.call("GET", f"/posts/{sent['id']}/reactions")
    assert sorted(reaction["emoji_name"] for reaction in reactions) == ["+1", "tada"]


async def test_a_post_where_the_bot_is_not_a_member_is_refused_with_the_server_s_code(place, belle):
    host, backend = belle
    backend.play(ModelTurn(calls=(("post_message", {"text": "hello outside", "chat_id": place.channels["outside"]}),)))

    await place.user("alice").call("POST", "/posts", json={"channel_id": place.channels["chatter"], "message": "post it outside"})
    await _ended(host, 1)

    refused = json.loads(backend.results[0])
    assert refused["error"] == "forbidden" and "not a member" in refused["detail"]
    assert await place.bot_posts(place.channels["outside"], 0) == []


async def test_a_channel_and_a_thread_are_read_back_in_order_and_paged_to_the_end(place, tools):
    toolkit, turn = tools
    lounge = place.channels["lounge"]
    alice = place.user("alice")
    sent = [await alice.call("POST", "/posts", json={"channel_id": lounge, "message": f"line {index}"}) for index in range(5)]
    reply = await place.user("bob").call("POST", "/posts", json={"channel_id": lounge, "message": "a reply", "root_id": sent[1]["id"]})
    turn(lounge, place.id("alice"))

    pages, before = [], None
    while True:
        page = json.loads(await toolkit.read_conversation(max_messages=4, before_message_id=before))
        pages.append([message["text"] for message in page["messages"]])
        before = page["coverage"]["next_before_message_id"]
        if page["coverage"]["status"] == "complete":
            break
    thread = json.loads(await toolkit.read_conversation(message_id=reply["id"]))

    assert [text for found in reversed(pages) for text in found if text.startswith(("line", "a reply"))] == [
        "line 0", "line 1", "line 2", "line 3", "line 4", "a reply"
    ]
    assert [message["text"] for message in thread["messages"]] == ["line 1", "a reply"]
    assert thread["messages"][1]["author_name"] == f"bob-{place.tag}"


async def test_a_read_widening_its_audience_is_refused_and_an_open_channel_the_bot_is_not_in_is_read(place, tools):
    toolkit, turn = tools
    await place.user("bob").call("POST", "/posts", json={"channel_id": place.channels["outside"], "message": "out here"})
    turn(place.channels["chatter"], place.id("bob"))

    private_from_open = json.loads(await toolkit.read_conversation(chat_id=place.channels["quiet"]))
    open_not_joined = json.loads(await toolkit.read_conversation(chat_id=place.channels["outside"]))

    assert private_from_open["error"] == "source_is_public_and_target_is_not"
    assert [message["text"] for message in open_not_joined["messages"]][-1] == "out here"


async def test_the_decision_gate_reads_the_channel_and_its_advice_reaches_the_turn(place, config, tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    endpoint = DecisionStandIn()
    entry = {"id": "standin", "api": "openrouter", "api_base": endpoint.url, "api_key": f"${{{KEY_ENV}}}", "model_name": "typesafe/stand-in"}
    config["models"] = {"decision": {"entries": [entry]}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    backend = RecordedBackend(ScriptedBackend())
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    try:
        chatter = place.channels["chatter"]
        alice = place.user("alice")
        await alice.call("POST", "/posts", json={"channel_id": chatter, "message": "the ferry is late again"})
        await _ended(host, 1)
        await alice.call("POST", "/posts", json={"channel_id": chatter, "message": "does anyone know why?"})
        await _ended(host, 2)
    finally:
        await host.stop()
        endpoint.close()

    window = endpoint.states()[-1]["recent_messages"]
    assert window[-1] == {**window[-1], "author": f"@alice-{place.tag}", "text": "the ferry is late again"}
    assert endpoint.states()[-1]["conversation_kind"] == "public_channel"
    assert all(ADVISORY_PREAMBLE in request.text for request in backend.requests)


async def _upload(rest: Rest, channel: str, *files: tuple[str, bytes]) -> list[str]:
    """Upload files into a channel, and return their ids."""
    found = await rest.call(
        "POST", "/files", data={"channel_id": channel}, files=[("files", (name, data)) for name, data in files]
    )
    return [info["id"] for info in found["file_infos"]]


async def _reactions(rest: Rest, post: str) -> set[str]:
    return {reaction["emoji_name"] for reaction in await rest.call("GET", f"/posts/{post}/reactions") or ()}


#: One red pixel, and the smallest PDF a reader takes.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c4944415408d763f8cfc000000301"
    "0100c9fe92ef0000000049454e44ae426082"
)
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


async def test_the_files_of_a_message_reach_its_turn_and_one_over_the_ceiling_is_named(place, config, tmp_path, monkeypatch):
    monkeypatch.setattr(mattermost_connector, "MAX_FILE_BYTES", 4096)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    backend = RecordedBackend(ScriptedBackend())
    host = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await host.start()
    try:
        lounge, alice = place.channels["lounge"], place.user("alice")
        ids = await _upload(alice, lounge, ("pixel.png", PNG), ("brief.pdf", PDF), ("long.txt", b"x" * 5000))
        sent = await alice.call(
            "POST", "/posts", json={"channel_id": lounge, "message": f"@belle-{place.tag} see these", "file_ids": ids}
        )
        await _ended(host, 1)
        (notice,) = [post for post in await place.bot_posts(lounge, 2) if "could not read" in post["message"]]
    finally:
        await host.stop()

    (request,) = backend.requests
    # The server may give an upload's files in any order.
    assert sorted((entry["name"], entry["uri"].partition(",")[0]) for entry in request.files) == [
        ("brief.pdf", "data:application/pdf;base64"), ("pixel.png", "data:image/png;base64")
    ]
    assert notice["root_id"] == sent["id"]
    assert notice["message"] == f"Received 1 attachment (long.txt) but could not read it: {FILE_REASON_TOO_LARGE}."


async def test_a_file_of_another_channel_is_read_only_where_its_readers_may(place, tools):
    toolkit, turn = tools
    alice = place.user("alice")
    (open_file,) = await _upload(alice, place.channels["lounge"], ("tides.md", b"# Tides\nfour metres\n"))
    shared = await alice.call("POST", "/posts", json={"channel_id": place.channels["lounge"], "message": "tides", "file_ids": [open_file]})
    (private_file,) = await _upload(alice, place.channels["quiet"], ("secret.md", b"# Secret\n"))
    await alice.call("POST", "/posts", json={"channel_id": place.channels["quiet"], "message": "secret", "file_ids": [private_file]})
    turn(place.channels["chatter"], place.id("bob"))

    read = json.loads(await toolkit.download_file(file_id=open_file))
    refused = json.loads(await toolkit.download_file(file_id=private_file))

    assert (read["content"], read["chat_id"], read["message_id"]) == ("# Tides\nfour metres\n", place.channels["lounge"], shared["id"])
    assert refused["error"] == "source_is_public_and_target_is_not"


async def test_a_document_in_an_open_channel_is_ingested_marked_and_found_and_one_in_a_private_channel_is_not(
    place, config, tmp_path
):
    config["second_brain"] = {"enabled": True, "corpus_dir": str(tmp_path / "corpus"), "ingest": {"channels": ["public"]}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    backend = ScriptedBackend()
    host = host_from_config(load_config(path), backend.backend, state_dir=tmp_path / "state", second_brain=scripted_second_brain())
    await host.start()
    try:
        alice = place.user("alice")
        names = ("kite-bridle.md", "harbour-tides.md", "hive-inspection.md")
        ids = await _upload(alice, place.channels["lounge"], *((name, DOCUMENTS[name].encode()) for name in names))
        shared = await alice.call("POST", "/posts", json={"channel_id": place.channels["lounge"], "message": "notes", "file_ids": ids})
        (hidden,) = await _upload(alice, place.channels["quiet"], ("starter-hydration.md", DOCUMENTS["starter-hydration.md"].encode()))
        private = await alice.call("POST", "/posts", json={"channel_id": place.channels["quiet"], "message": "mine", "file_ids": [hidden]})
        async with asyncio.timeout(ANSWER_SECONDS):
            while await _reactions(alice, shared["id"]) != {"brain"}:
                await asyncio.sleep(0.2)
        backend.play(ModelTurn(final="found", calls=(("brain_search", {"query": "varroa mites on the sticky board"}), ("brain_list", {}))))
        direct = await alice.call("POST", "/channels/direct", json=[place.id("alice"), place.bot["user_id"]])
        await alice.call("POST", "/posts", json={"channel_id": direct["id"], "message": "what do the hive notes say?"})
        await _ended(host, 1)
    finally:
        await host.stop()

    assert await _reactions(alice, private["id"]) == set()
    found, listing = backend.results
    assert "BRAIN_DOC doc_id=hive-inspection-" in found and "kite-bridle-" not in found
    assert "3 document(s)" in listing and "starter-hydration" not in listing


async def _host(config: dict[str, Any], tmp_path: Path, **section: Any) -> tuple[Any, ScriptedBackend, RecordedBackend]:
    """A host on the bot with more settings in its section, and the requests its backend received."""
    config = {**config, "platforms": {"mattermost": {**config["platforms"]["mattermost"], **section}}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    backend = ScriptedBackend()
    recorded = RecordedBackend(backend.backend)
    host = host_from_config(load_config(path), recorded, state_dir=tmp_path / "state")
    await host.start()
    return host, backend, recorded


async def test_a_question_is_a_post_whose_buttons_the_server_shows_without_their_context(place, config, tmp_path):
    host, _, _ = await _host(config, tmp_path, actions={"bind": "127.0.0.1", "port": 0, "url": "http://belle.invalid:18400"})
    try:
        question = Question(id="q1", text="Post this in the other channel?", options=(APPROVE, REJECT), kind=APPROVAL)
        post_id = await host.platform.ask(place.channels["lounge"], "", question, workspace=place.team)
    finally:
        await host.stop()

    seen = await place.user("alice").call("GET", f"/posts/{post_id}")
    (attachment,) = seen["props"]["attachments"]
    assert seen["message"] == question.text
    assert [(action["id"], action["name"]) for action in attachment["actions"]] == [("option0", "Approve"), ("option1", "Reject")]
    # The server keeps the button's context, the listener's secret among it, from every client.
    assert all("integration" not in action and "context" not in action for action in attachment["actions"])


async def test_a_streamed_reply_and_the_card_of_its_turn_are_posts_the_server_holds_as_they_ended(place, config, tmp_path):
    settings = {"enable_streaming": True, "activity_card_delay_seconds": 0, "activity_card_min_edit_seconds": 0}
    host, backend, _ = await _host(config, tmp_path, **settings)
    try:
        backend.play(ModelTurn(final="The ferry leaves at noon, from the north quay.", calls=(("read_conversation", {}),)))
        lounge = place.channels["lounge"]
        sent = await place.user("bob").call("POST", "/posts", json={"channel_id": lounge, "message": f"@belle-{place.tag} ferry?"})
        await _ended(host, 1)
    finally:
        await host.stop()

    bot_posts = await place._bot_posts(lounge)
    posts = [(post["message"], post["root_id"]) for post in bot_posts]
    assert ("The ferry leaves at noon, from the north quay.", sent["id"]) in posts and len(posts) == 2
    cards = [post["props"]["attachments"] for post in bot_posts if post["props"].get("attachments")]
    assert [(card["title"], card["text"]) for (card,) in cards] == [
        ("Turn finished", "1 tool finished\n:white_check_mark: **read_conversation**")
    ]


async def test_a_reply_longer_than_the_server_takes_goes_in_parts_in_its_thread(place, belle):
    _, backend = belle
    long = " ".join(f"word{index:05d}" for index in range(2000))  # 20,000 characters
    backend.play(ModelTurn(final=long))
    lounge = place.channels["lounge"]

    sent = await place.user("bob").call("POST", "/posts", json={"channel_id": lounge, "message": f"@belle-{place.tag} all"})
    posts = await place.bot_posts(lounge, 2)

    assert [post["root_id"] for post in posts] == [sent["id"], sent["id"]]
    assert " ".join(post["message"] for post in posts) == long and all(len(post["message"]) <= 16383 for post in posts)


async def test_a_reply_whose_post_the_server_no_longer_lets_be_edited_goes_on_in_a_new_post(place, monkeypatch):
    monkeypatch.setattr(surfaces, "STREAM_EDIT_SECONDS", 0.0)
    connector = MattermostPlatform(MattermostSettings(URL, place.bot_token, enable_streaming=True))
    lounge = place.channels["lounge"]
    asked = await place.user("bob").call("POST", "/posts", json={"channel_id": lounge, "message": "ferry?"})
    inbound = InboundMessage(platform="mattermost", workspace=place.team, conversation=lounge, message_id=asked["id"],
                             sender=place.id("bob"), text="ferry?", reply_thread=asked["id"])

    async def ignore(_record):
        return "ignored"

    await connector.start(ignore)
    await place.admin.call("PUT", "/config/patch", json={"ServiceSettings": {"PostEditTimeLimit": 1}})
    try:
        await connector.progress(inbound, TurnEvent(EVENT_TEXT_DELTA, text="The ferry leaves at noon,"))
        await asyncio.sleep(2.0)  # Older than the limit: the server refuses the next edit.
        await connector.progress(inbound, TurnEvent(EVENT_TEXT_DELTA, text=" from the north quay."))
        await asyncio.sleep(0.5)
        assert await connector.conclude(inbound, MARK_DONE, "The ferry leaves at noon, from the north quay.") is True
    finally:
        await place.admin.call("PUT", "/config/patch", json={"ServiceSettings": {"PostEditTimeLimit": -1}})
        await connector.stop()

    # The first post kept what it showed, and the rest went on below it, in the same thread.
    posts = [(post["message"], post["root_id"]) for post in await place._bot_posts(lounge)]
    assert posts == [("The ferry leaves at noon,", asked["id"]), ("from the north quay.", asked["id"])]


async def test_a_reaction_is_kept_as_context_for_the_next_turn_of_its_channel(place, config, tmp_path):
    config["scopes"].append(
        {"match": {"channel": "mattermost", "chat": place.channels["chatter"]}, "delivery": {"events": {"reaction": "context"}}}
    )
    host, _, recorded = await _host(config, tmp_path)
    try:
        chatter = place.channels["chatter"]
        alice = place.user("alice")
        first = await alice.call("POST", "/posts", json={"channel_id": chatter, "message": "the ferry is back"})
        await _ended(host, 1)
        await alice.call("POST", "/reactions", json={"user_id": place.id("alice"), "post_id": first["id"], "emoji_name": "tada"})
        await asyncio.sleep(1.0)
        await alice.call("POST", "/posts", json={"channel_id": chatter, "message": "and now?"})
        await _ended(host, 2)
    finally:
        await host.stop()

    told = recorded.requests[-1].text
    assert "[mattermost_events]" in told and "tada" in told


async def test_a_post_for_one_person_without_the_permission_is_refused_with_its_name(place, belle):
    host, _ = belle

    with pytest.raises(MattermostError, match="create_post_ephemeral") as refused:
        await host.platform.post(place.channels["lounge"], "", "only you", ephemeral_to=place.id("alice"))
    assert refused.value.code == "forbidden"


async def _typing(stream: Any, seconds: float) -> list[dict[str, Any]]:
    """The typing notices a WebSocket receives within ``seconds``."""
    seen: list[dict[str, Any]] = []
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            while (event := await stream.receive()) is not None:
                if event.get("event") == "typing":
                    seen.append({**event["data"], "channel_id": event["broadcast"]["channel_id"]})
    return seen


async def test_a_running_turn_shows_the_bot_typing_in_its_thread_until_it_ends(place):
    connector = MattermostPlatform(MattermostSettings(URL, place.bot_token))
    alice = place.user("alice")
    lounge = place.channels["lounge"]
    root = await alice.call("POST", "/posts", json={"channel_id": lounge, "message": "a question"})
    message = InboundMessage(
        platform="mattermost", workspace=place.team, conversation=lounge, message_id=root["id"],
        sender=place.id("alice"), text="a question", reply_thread=root["id"],
    )
    reader = MattermostClient(URL, alice.http.headers["Authorization"].removeprefix("Bearer "))
    stream = await reader.events()

    async def ignore(_record):
        return "ignored"

    await connector.start(ignore)
    try:
        await connector.mark(message, MARK_RECEIVED)
        # The server's default interval is 5000 ms: a notice at once, then one every 2.5 s.
        running = await _typing(stream, 6.0)
        await connector.mark(message, MARK_DONE)
        ended = await _typing(stream, 4.0)
    finally:
        await connector.stop()
        await stream.close()
        await reader.close()

    assert len(running) >= 3
    assert {(notice["channel_id"], notice["parent_id"], notice["user_id"]) for notice in running} == {
        (lounge, root["id"], place.bot["user_id"])
    }
    assert ended == []
