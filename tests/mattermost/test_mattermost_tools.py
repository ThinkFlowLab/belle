"""The posting, reaction and reading tools on the fake server: where each call lands, and what it refuses."""

import inspect
import json

import pytest
from transport import BOT_USER, TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import MattermostSettings
from belle_agent.platforms.mattermost.toolkit import MattermostToolkit

TEAM, OTHER_TEAM = "t" * 26, "u" * 26
LOUNGE, QUIET, OUTSIDE, ELSEWHERE, PAIR = "l" * 26, "q" * 26, "o" * 26, "e" * 26, "w" * 26
ALICE, BOB, CAROL = "a" * 26, "b" * 26, "c" * 26
MESSAGE = "m" * 26


class Turn:
    """A started connector, its toolkit, and the metadata of the turn whose calls it serves."""

    def __init__(self, rest: FakeRest, settings: MattermostSettings | None = None) -> None:
        self.rest = rest
        client = MattermostClient(
            URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open, send_body=rest.send_body
        )
        self.connector = MattermostPlatform(settings or MattermostSettings(URL, TOKEN), client=client)
        self.metadata: dict = {}
        (self.tools,) = self.connector.toolkits(lambda: self.metadata)

    def came_from(self, channel: str, sender: str = ALICE, message: str = MESSAGE, chat_type: str = "P") -> None:
        inbound = InboundMessage("mattermost", TEAM, channel, message, sender, "hi", chat_type=chat_type)
        self.metadata = self.connector.request_metadata(inbound)

    async def call(self, name: str, **arguments) -> dict:
        return json.loads(await getattr(self.tools, name)(**arguments))


def test_each_card_requires_only_the_arguments_its_tool_cannot_do_without():
    for card in MattermostToolkit.get_tool_definitions(None):
        parameters = inspect.signature(getattr(MattermostToolkit, card["name"])).parameters.values()
        without_default = {parameter.name for parameter in parameters if parameter.default is inspect.Parameter.empty} - {"self"}
        assert set(card["input_params"].get("required", [])) == without_default
        assert set(card["input_params"]["properties"]) == {parameter.name for parameter in parameters} - {"self"}


# ── post_message ─────────────────────────────────────────────────────────


async def test_a_post_goes_to_the_turn_s_channel_and_says_where_it_landed(turn):
    turn.came_from(QUIET)

    result = await turn.call("post_message", text="hello")

    (sent,) = turn.rest.calls("POST", "/posts")
    assert sent.body == {"channel_id": QUIET, "message": "hello", "root_id": ""}
    assert result == {"ok": True, "chat_id": QUIET, "message_id": result["message_id"],
                      "permalink": f"http://chat.invalid/team-tttt/pl/{result['message_id']}",
                      "parts": 1, "thread_id": ""}


async def test_native_priority_props_and_silent_delivery_reach_post_api(turn):
    turn.came_from(QUIET)

    result = await turn.call(
        "post_message", text="Urgent update", priority="urgent", requested_ack=True,
        persistent_notifications=True, silent=True, props={"card": "Details"},
    )

    assert result["ok"]
    (sent,) = turn.rest.calls("POST", "/posts")
    assert sent.body["metadata"] == {"priority": {
        "priority": "urgent", "requested_ack": True, "persistent_notifications": True,
    }}
    assert sent.body["props"] == {"card": "Details"}
    assert sent.query == {"silent": "true"}


async def test_native_post_rejects_identity_override_and_ambiguous_split(turn):
    turn.came_from(QUIET)
    turn.rest.client_config["MaxPostSize"] = "12"

    override = await turn.call("post_message", text="hello", props={"override_username": "Alice"})
    split = await turn.call("post_message", text="one two three four", priority="important")

    assert override["error"] == "props_invalid"
    assert split["error"] == "native_options_require_one_part"
    assert turn.rest.calls("POST", "/posts") == []


@pytest.mark.parametrize("answer", [{}, {"id": 42}, {"id": " "}])
async def test_a_post_with_no_valid_server_id_is_not_reported_as_success(turn, answer):
    turn.came_from(QUIET)
    turn.rest.routes[("POST", "/posts")] = (201, answer)

    result = await turn.call("post_message", text="hello")

    assert result["error"] == "bad_response"
    assert "message_id" not in result


async def test_a_reply_to_a_reply_goes_in_the_thread_of_its_root(turn):
    turn.came_from(QUIET)
    turn.rest.post("r" * 26, QUIET, ALICE, "root")
    turn.rest.post("s" * 26, QUIET, BOB, "reply", root="r" * 26)

    result = await turn.call("post_message", text="answer", reply_to_message_id="s" * 26)

    assert result["thread_id"] == "r" * 26
    assert turn.rest.calls("POST", "/posts")[0].body["root_id"] == "r" * 26


async def test_a_reply_to_a_message_of_another_channel_is_refused(turn):
    turn.came_from(QUIET)
    turn.rest.post("r" * 26, LOUNGE, ALICE, "root")

    result = await turn.call("post_message", text="answer", reply_to_message_id="r" * 26)

    assert result["error"] == "message_not_found"
    assert turn.rest.calls("POST", "/posts") == []


async def test_a_text_longer_than_the_server_takes_is_posted_in_parts_in_one_thread(turn):
    turn.came_from(QUIET)
    turn.rest.client_config["MaxPostSize"] = "12"

    result = await turn.call("post_message", text="one two three four five six")

    bodies = [request.body for request in turn.rest.calls("POST", "/posts")]
    assert [body["message"] for body in bodies] == ["one two", " three four", " five six"]
    assert "".join(body["message"] for body in bodies) == "one two three four five six"
    assert [body["root_id"] for body in bodies] == ["", result["message_id"], result["message_id"]]
    assert result["parts"] == 3 and result["part_message_ids"][0] == result["message_id"]
    assert result["thread_id"] == ""
    assert list(turn.rest.posts) == result["part_message_ids"]


async def test_a_blank_multipart_piece_is_refused_before_any_post(turn):
    turn.came_from(QUIET)
    turn.rest.client_config["MaxPostSize"] = "12"

    result = await turn.call("post_message", text="A" + " " * 40 + "B")

    assert result["error"] == "post_text_cannot_be_split"
    assert turn.rest.calls("POST", "/posts") == []


async def test_a_post_for_the_requester_alone_goes_through_the_ephemeral_route(turn):
    turn.came_from(QUIET)

    result = await turn.call("post_message", text="only you", visible_only_to_user_id=ALICE)

    assert turn.rest.calls("POST", "/posts/ephemeral")[0].body["user_id"] == ALICE
    assert result["ephemeral"] is True and "permalink" not in result


async def test_an_ephemeral_post_needs_the_requester_and_the_permission(turn):
    turn.came_from(QUIET)
    refused_for_bob = await turn.call("post_message", text="x", visible_only_to_user_id=BOB)
    turn.rest.routes[("POST", "/posts/ephemeral")] = (403, {"id": "api.context.permissions.app_error"})

    refused_by_server = await turn.call("post_message", text="x", visible_only_to_user_id=ALICE)

    assert refused_for_bob["error"] == "ephemeral_requires_the_requester"
    assert refused_by_server["error"] == "forbidden" and "create_post_ephemeral" in refused_by_server["detail"]


async def test_post_text_is_kept_and_non_string_text_is_refused(turn):
    turn.came_from(QUIET)
    before = len(turn.rest.calls("POST", "/posts"))
    assert (await turn.call("post_message", text=7))["error"] == "text_invalid"
    assert len(turn.rest.calls("POST", "/posts")) == before
    assert (await turn.call("post_message", text="  exact text  "))["ok"] is True
    assert turn.rest.calls("POST", "/posts")[-1].body["message"] == "  exact text  "


async def test_a_post_where_the_bot_is_no_member_is_refused_with_the_server_s_code(turn):
    turn.came_from(QUIET)
    turn.rest.members[QUIET] = [ALICE, BOB, CAROL, BOT_USER]
    turn.rest.routes[("POST", "/posts")] = (403, {"id": "api.context.permissions.app_error"})

    result = await turn.call("post_message", text="x", chat_id=OUTSIDE)

    assert result["error"] == "forbidden" and "not a member" in result["detail"]


async def test_a_malformed_channel_id_is_refused_before_any_call(turn):
    turn.came_from(QUIET)
    before = len(turn.rest.requests)

    result = await turn.call("post_message", text="x", chat_id="null")

    assert result["error"] == "chat_id_malformed"
    assert len(turn.rest.requests) == before


async def test_a_channel_whose_readers_read_this_one_takes_a_post_with_no_question(turn):
    turn.came_from(QUIET)

    assert await turn.tools.widening_question("post_message", {"text": "x", "chat_id": PAIR}) is None
    result = await turn.call("post_message", text="x", chat_id=PAIR)

    assert result["chat_id"] == PAIR


async def test_a_post_that_reaches_new_readers_is_put_to_the_requester_and_refused_without_one(turn):
    turn.came_from(PAIR)

    question = await turn.tools.widening_question("post_message", {"text": "x", "chat_id": QUIET})
    turn.metadata["mattermost_user_id"] = ""
    unattended = await turn.call("post_message", text="x", chat_id=QUIET)

    assert question.startswith("This message would go to ~quiet-room, where it will also be seen by 1 person")
    assert "@bob" in question
    assert unattended["error"] == "write_target_widens_audience"
    assert turn.rest.calls("POST", "/posts") == []


async def test_an_open_channel_reaches_its_team_and_a_person_s_id_opens_their_direct_channel(turn):
    turn.came_from(PAIR)

    to_team = await turn.tools.widening_question("post_message", {"text": "x", "chat_id": LOUNGE})
    to_carol = await turn.tools.widening_question("post_message", {"text": "x", "chat_id": CAROL})

    assert to_team.startswith("This message would go to ~lounge, which is a public channel")
    assert to_carol.startswith("This message would be sent privately to @carol")


async def test_an_open_channel_of_another_team_is_new_readers_even_from_an_open_channel(turn):
    turn.came_from(LOUNGE, chat_type="O")

    assert await turn.tools.widening_question("post_message", {"text": "x", "chat_id": OUTSIDE}) is None
    assert "public channel" in await turn.tools.widening_question("post_message", {"text": "x", "chat_id": ELSEWHERE})


# ── react_to_message ─────────────────────────────────────────────────────


async def test_a_reaction_lands_on_the_turn_s_message_and_a_character_becomes_its_name(turn):
    turn.came_from(QUIET)

    plain = await turn.call("react_to_message", emoji=":eyes:")
    toned = await turn.call("react_to_message", emoji="\N{THUMBS UP SIGN}\U0001f3fc")
    removed = await turn.call("react_to_message", emoji="eyes", remove=True)

    names = [request.body["emoji_name"] for request in turn.rest.calls("POST", "/reactions")]
    assert names == ["eyes", "+1_medium_light_skin_tone"]
    assert turn.rest.calls("DELETE", "/users/")[0].path == f"/users/{BOT_USER}/posts/{MESSAGE}/reactions/eyes"
    assert plain["message_id"] == toned["message_id"] == MESSAGE and removed["removed"] is True


@pytest.mark.parametrize(
    "emoji,code",
    [("", "emoji_required"), ("\N{SNOWMAN}", "emoji_character_not_recognised"),
     ("\N{HANDSHAKE}\U0001f3fb\U0001f3ff", "emoji_skin_tones_not_supported")],
)
async def test_an_emoji_the_tool_cannot_name_is_refused(turn, emoji, code):
    turn.came_from(QUIET)

    assert (await turn.call("react_to_message", emoji=emoji))["error"] == code
    assert turn.rest.calls("POST", "/reactions") == []


async def test_a_reaction_on_a_message_of_another_channel_is_refused(turn):
    turn.came_from(QUIET)
    turn.rest.post("x" * 26, LOUNGE, ALICE, "elsewhere")

    result = await turn.call("react_to_message", emoji="eyes", message_id="x" * 26)

    assert result["error"] == "message_not_found"


async def test_cross_channel_reaction_checks_location_and_wider_audience(turn):
    turn.came_from(PAIR)
    target = "x" * 26
    turn.rest.post(target, QUIET, ALICE, "elsewhere")

    question = await turn.tools.widening_question(
        "react_to_message", {"emoji": "eyes", "message_id": target, "chat_id": QUIET}
    )
    turn.metadata["mattermost_user_id"] = ""
    refused = await turn.call("react_to_message", emoji="eyes", message_id=target, chat_id=QUIET)
    removed = await turn.call("react_to_message", emoji="eyes", message_id=target, chat_id=QUIET, remove=True)

    assert "@bob" in question
    assert refused["error"] == "write_target_widens_audience"
    assert removed["chat_id"] == QUIET and removed["removed"] is True
    assert turn.rest.calls("POST", "/reactions") == []


async def test_an_unknown_emoji_name_is_refused_with_the_server_s_answer(turn):
    turn.came_from(QUIET)
    turn.rest.routes[("POST", "/reactions")] = (404, {"id": "app.emoji.get_by_name.no_result"})

    result = await turn.call("react_to_message", emoji="no_such_emoji")

    assert result["error"] == "not_found" and "no emoji called no_such_emoji" in result["detail"]


# ── read_conversation ────────────────────────────────────────────────────


def _seed(rest: FakeRest, channel: str, count: int) -> list[str]:
    ids = [f"{index:02d}" + "z" * 24 for index in range(count)]
    for index, post_id in enumerate(ids):
        rest.post(post_id, channel, (ALICE, BOB)[index % 2], f"message {index}")
    return ids


async def test_a_channel_is_read_oldest_first_and_paged_back_to_its_start(turn):
    turn.came_from(QUIET)
    ids = _seed(turn.rest, QUIET, 5)

    first = await turn.call("read_conversation", max_messages=3)
    second = await turn.call("read_conversation", max_messages=3,
                             before_message_id=first["coverage"]["next_before_message_id"])

    assert [message["message_id"] for message in first["messages"]] == ids[2:]
    assert first["coverage"]["status"] == "partial" and first["coverage"]["next_before_message_id"] == ids[2]
    assert [message["message_id"] for message in second["messages"]] == ids[:2]
    assert second["coverage"] == {**second["coverage"], "status": "complete", "next_before_message_id": None}
    assert first["messages"][0] == {
        "message_id": ids[2], "root_message_id": "", "author_user_id": ALICE, "author_name": "alice",
        "is_author_bot": False, "text": "message 2", "created_iso_utc": "2027-01-15T08:00:02Z",
        "permalink": f"http://chat.invalid/team-tttt/pl/{ids[2]}",
    }
    assert first["chat_name"] == "Quiet Room" and first["chat_type"] == "P"


async def test_a_read_after_an_instant_stops_there_and_is_complete(turn):
    turn.came_from(QUIET)
    ids = _seed(turn.rest, QUIET, 5)

    result = await turn.call("read_conversation", after_iso_utc="2027-01-15T08:00:02Z")

    assert [message["message_id"] for message in result["messages"]] == ids[3:]
    assert result["coverage"]["status"] == "complete"
    assert (await turn.call("read_conversation", after_iso_utc="last week"))["error"] == "after_iso_utc_must_be_an_iso_instant"


async def test_a_thread_is_read_whole_from_any_of_its_messages(turn):
    turn.came_from(QUIET)
    turn.rest.post("r" * 26, QUIET, ALICE, "root")
    turn.rest.post("s" * 26, QUIET, BOB, "reply", root="r" * 26)
    turn.rest.post("n" * 26, QUIET, BOB, "not in the thread")

    result = await turn.call("read_conversation", message_id="s" * 26)

    assert [message["text"] for message in result["messages"]] == ["root", "reply"]
    assert result["coverage"]["status"] == "complete"


async def test_a_thread_applies_the_time_bound_before_paging(turn):
    turn.came_from(QUIET)
    root, old, recent, latest = (character * 26 for character in "rost")
    turn.rest.post(root, QUIET, ALICE, "root")
    turn.rest.post(old, QUIET, BOB, "old reply", root=root)
    turn.rest.post(recent, QUIET, BOB, "recent reply", root=root)
    turn.rest.post(latest, QUIET, ALICE, "latest reply", root=root)
    bound = "2027-01-15T08:00:01Z"

    first = await turn.call("read_conversation", message_id=root, after_iso_utc=bound, max_messages=1)
    older = await turn.call("read_conversation", message_id=root, after_iso_utc=bound, max_messages=1,
                            before_message_id=first["coverage"]["next_before_message_id"])
    empty = await turn.call("read_conversation", message_id=root, after_iso_utc="2027-01-15T08:00:03Z")

    assert [message["message_id"] for message in first["messages"]] == [latest]
    assert first["coverage"]["status"] == "partial"
    assert [message["message_id"] for message in older["messages"]] == [recent]
    assert older["coverage"]["status"] == "complete"
    assert empty["messages"] == [] and empty["coverage"]["status"] == "complete"
    assert first["coverage"]["after_iso_utc"] == bound


async def test_thread_pages_remain_stable_when_posts_share_a_timestamp(turn):
    turn.came_from(QUIET)
    root, first_id, second_id = (character * 26 for character in "rst")
    root_post = turn.rest.post(root, QUIET, ALICE, "root")
    turn.rest.post(first_id, QUIET, BOB, "first reply", root=root)
    turn.rest.post(second_id, QUIET, BOB, "second reply", root=root)
    for post in turn.rest.posts.values():
        post["create_at"] = root_post["create_at"]

    newest = await turn.call("read_conversation", message_id=root, max_messages=1)
    turn.rest.posts = dict(reversed(list(turn.rest.posts.items())))
    older = await turn.call(
        "read_conversation", message_id=root, max_messages=1,
        before_message_id=newest["coverage"]["next_before_message_id"],
    )

    assert [item["message_id"] for item in newest["messages"]] == [second_id]
    assert [item["message_id"] for item in older["messages"]] == [first_id]
    assert older["coverage"]["status"] == "partial"


async def test_another_channel_is_read_only_where_this_one_s_readers_read_it(turn):
    turn.came_from(PAIR)
    _seed(turn.rest, QUIET, 1)
    _seed(turn.rest, PAIR, 1)

    into_pair = await turn.call("read_conversation", chat_id=QUIET)
    turn.came_from(QUIET)
    into_quiet = await turn.call("read_conversation", chat_id=PAIR)

    assert into_pair["ok"] is True
    assert into_quiet["error"] == "source_members_not_in_target"
    assert into_quiet["detail"].startswith(f"1 member(s) of {QUIET} are not in {PAIR}")


async def test_an_open_channel_is_read_from_its_team_and_from_another_only_by_its_members(turn):
    turn.came_from(QUIET)
    _seed(turn.rest, OUTSIDE, 1)
    _seed(turn.rest, ELSEWHERE, 1)

    same_team = await turn.call("read_conversation", chat_id=OUTSIDE)
    other_team = await turn.call("read_conversation", chat_id=ELSEWHERE)
    turn.came_from(PAIR)
    other_team_from_its_members = await turn.call("read_conversation", chat_id=ELSEWHERE)

    assert same_team["ok"] is True
    # bob is in the quiet room and not in the other team.
    assert other_team["error"] == "source_members_not_in_target"
    assert other_team_from_its_members["ok"] is True


# ── the decision gate's inputs ───────────────────────────────────────────


async def test_the_gate_reads_the_messages_before_one_oldest_first_by_name(turn):
    ids = _seed(turn.rest, QUIET, 4)
    turn.rest.post("j" * 26, QUIET, CAROL, "carol joined", type="system_join_channel")
    turn.rest.post("k" * 26, QUIET, BOT_USER, "the bot said")
    turn.rest.post("y" * 26, QUIET, ALICE, "the message itself")

    window = await turn.connector.recent_messages(QUIET, 10, "y" * 26, workspace=TEAM)

    assert [entry["author"] for entry in window] == ["@alice", "@bob", "@alice", "@bob", "belle"]
    assert window[0] == {"author": "@alice", "text": "message 0", "ts": ids[0], "thread_ts": "", "thread_position": "root", "file_count": 0}


async def test_the_gate_is_told_the_kind_of_channel_and_the_bot_s_mention(turn):
    inbound = InboundMessage("mattermost", TEAM, LOUNGE, MESSAGE, ALICE, "who knows?", chat_type="O")

    facts = turn.connector.decision_facts(inbound)

    assert facts["conversation_kind"] == "public_channel"
    assert facts["bot_name"] == "belle (mentioned as @belle)"
    assert facts["last_message"]["text"] == "who knows?"


async def test_a_post_in_a_direct_channel_is_linked_through_a_team_of_the_bot(turn):
    turn.came_from(PAIR)

    result = await turn.call("post_message", text="x", chat_id=CAROL)

    assert result["permalink"] == f"http://chat.invalid/team-tttt/pl/{result['message_id']}"


@pytest.mark.parametrize("ephemeral", [False, True])
@pytest.mark.parametrize("sent_count", [0, 1, 2])
@pytest.mark.parametrize("in_thread", [False, True])
async def test_a_split_refusal_reports_every_visible_part(turn, monkeypatch, ephemeral, sent_count, in_thread):
    from belle_agent.platforms.mattermost.client import MattermostError

    turn.came_from(QUIET)
    turn.rest.client_config["MaxPostSize"] = "12"
    if in_thread:
        turn.rest.post("r" * 26, QUIET, ALICE, "root")
        turn.rest.post("s" * 26, QUIET, BOB, "reply", root="r" * 26)
    request = turn.tools._client.request
    sent_ids = []
    route = "/posts/ephemeral" if ephemeral else "/posts"

    async def refuse_later(method, path, **kwargs):
        if method == "POST" and path == route:
            if len(sent_ids) == sent_count:
                raise MattermostError("forbidden", method, path, status=403)
            answer = await request(method, path, **kwargs)
            sent_ids.append(answer["id"])
            return answer
        return await request(method, path, **kwargs)

    monkeypatch.setattr(turn.tools._client, "request", refuse_later)
    result = await turn.call("post_message", text="one two three four five six",
                             reply_to_message_id="s" * 26 if in_thread else None,
                             **({"visible_only_to_user_id": ALICE} if ephemeral else {}))

    assert result["ok"] is False and result["error"] == "forbidden"
    assert result["chat_id"] == QUIET
    assert "permalink" not in result
    if sent_count:
        assert result["message_id"] == sent_ids[0]
        assert result["part_message_ids"] == sent_ids
        assert result["parts"] == sent_count
        assert result["thread_id"] == ("r" * 26 if in_thread else "")
        assert "duplicate" in result["detail"]
        if ephemeral:
            assert result["ephemeral"] is True
            assert result["visible_only_to_user_id"] == ALICE
    else:
        assert "message_id" not in result and "part_message_ids" not in result


async def test_requester_only_post_cannot_leave_the_origin_channel(turn):
    turn.came_from(QUIET)

    refused = await turn.call("post_message", text="private", chat_id=PAIR, visible_only_to_user_id=ALICE)

    assert refused["ok"] is False
    assert "message_id" not in refused
    assert turn.rest.calls("POST", "/posts") == []
    assert turn.rest.calls("POST", "/posts/ephemeral") == []
