"""Posts in their thread, reactions, and the reactions that mark a message."""

import pytest
from transport import BOT_USER, TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.platforms import base
from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import MattermostSettings, read_settings

CHANNEL = "c" * 26
POST = "p" * 26
MESSAGE = InboundMessage(platform="mattermost", workspace="t" * 26, conversation=CHANNEL, message_id=POST, sender="a" * 26, text="hi")


async def _ignore(_record):
    return "ignored"


@pytest.fixture
async def connected():
    rest = FakeRest()
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
    connector = MattermostPlatform(MattermostSettings(URL, TOKEN), client=client)
    await connector.start(_ignore)
    yield connector, rest
    await connector.stop()


async def test_a_post_goes_in_the_thread_it_names_and_returns_its_id(connected):
    connector, rest = connected

    post_id = await connector.post(CHANNEL, POST, "four o'clock")

    (sent,) = rest.calls("POST", "/posts")
    assert sent.body == {"channel_id": CHANNEL, "message": "four o'clock", "root_id": POST}
    assert post_id == rest.posts[post_id]["id"]


async def test_a_post_for_one_person_goes_through_the_ephemeral_route(connected):
    connector, rest = connected

    await connector.post(CHANNEL, "", "only you", ephemeral_to="u" * 26)

    (sent,) = rest.calls("POST", "/posts/ephemeral")
    assert sent.body == {"user_id": "u" * 26, "post": {"channel_id": CHANNEL, "message": "only you", "root_id": ""}}


async def test_a_reaction_is_the_bot_s_and_is_removed_by_its_name(connected):
    connector, rest = connected

    await connector.react(CHANNEL, POST, "eyes")
    await connector.react(CHANNEL, POST, "eyes", remove=True)

    assert rest.calls("POST", "/reactions")[0].body == {"user_id": BOT_USER, "post_id": POST, "emoji_name": "eyes"}
    assert rest.calls("DELETE", "/users/")[0].path == f"/users/{BOT_USER}/posts/{POST}/reactions/eyes"


def _reactions(rest: FakeRest) -> list[tuple[str, str]]:
    seen = []
    for request in rest.requests:
        if request.path == "/reactions":
            seen.append(("+", request.body["emoji_name"]))
        elif request.method == "DELETE":
            seen.append(("-", request.path.rsplit("/", 1)[-1]))
    return seen


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (base.MARK_RECEIVED, [("+", "eyes")]),
        (base.MARK_STEERED, [("+", "eyes")]),
        (base.MARK_QUEUED, [("+", "eyes"), ("+", "hourglass_flowing_sand")]),
        (base.MARK_STARTED, [("-", "hourglass_flowing_sand")]),
        (base.MARK_DROPPED, [("-", "hourglass_flowing_sand"), ("-", "eyes")]),
        (base.MARK_REFUSED, [("+", "no_entry_sign")]),
        # completed_emoji is disabled by default.
        (base.MARK_DONE, [("-", "eyes")]),
        (base.MARK_FAILED, [("-", "eyes"), ("+", "x")]),
        (base.MARK_STOPPED, [("-", "eyes"), ("+", "black_square_for_stop")]),
        (base.MARK_INGESTING, [("+", "open_book")]),
        (base.MARK_INGESTED, [("-", "open_book"), ("+", "brain")]),
        (base.MARK_INGEST_FAILED, [("-", "open_book"), ("+", "warning")]),
        (base.MARK_INGEST_DECLINED, [("-", "open_book"), ("+", "x")]),
        (base.MARK_INGEST_EMPTY, [("-", "open_book")]),
    ],
)
async def test_each_mark_adds_and_removes_the_configured_reactions(connected, state, expected):
    connector, rest = connected

    await connector.mark(MESSAGE, state)

    assert _reactions(rest) == expected


def test_a_mark_key_takes_another_emoji_or_none(monkeypatch):
    monkeypatch.setenv("MATTERMOST_BOT_TOKEN", TOKEN)
    settings = read_settings({"platforms": {"mattermost": {"url": URL, "acknowledgement_emoji": "disabled", "failed_emoji": "warning"}}})

    assert (settings.emoji["acknowledgement_emoji"], settings.emoji["failed_emoji"]) == ("", "warning")


async def test_a_reply_longer_than_the_server_takes_goes_in_parts_in_the_thread_of_the_first(connected):
    connector, rest = connected
    rest.client_config["MaxPostSize"] = "20"

    first = await connector.post(CHANNEL, "", "one two three four five six seven eight nine")

    sent = [request.body for request in rest.calls("POST", "/posts")]
    assert len(sent) == 3
    assert "".join(body["message"] for body in sent) == "one two three four five six seven eight nine"
    assert all(len(body["message"]) <= 20 for body in sent)
    assert [body["root_id"] for body in sent] == ["", first, first]


async def test_a_post_for_one_person_that_the_server_refuses_names_the_permission(connected):
    connector, rest = connected
    rest.routes[("POST", "/posts/ephemeral")] = (403, {"id": "api.context.permissions.app_error"})

    with pytest.raises(MattermostError, match="create_post_ephemeral") as refused:
        await connector.post(CHANNEL, "", "only you", ephemeral_to="u" * 26)
    assert refused.value.code == "forbidden"
