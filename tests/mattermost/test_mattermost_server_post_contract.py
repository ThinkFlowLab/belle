"""Post result IDs and placement against an isolated Mattermost server."""

import asyncio
import json

import pytest
from test_mattermost_server import URL, ModelTurn, _ended, belle, config

from belle_agent.host import OUTCOME_POSTED_BY_TOOL
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError

pytestmark = pytest.mark.mattermost_server
__all__ = ("belle", "config")  # Imported fixtures for this test module.


async def test_split_ids_and_resolved_child_thread_match_member_reads(place, tools, record_property):
    toolkit, turn = tools
    channel = place.channels["lounge"]
    alice = place.user("alice")
    turn(channel, place.id("alice"))
    limit = await toolkit._directory.post_limit()
    assert limit > 0
    result = json.loads(await toolkit.post_message(text="x" * (limit * 2 + 1)))
    record_property("split_result", json.dumps(result))
    assert result["ok"] and result["parts"] == 3 and result["thread_id"] == ""
    assert result["message_id"] == result["part_message_ids"][0]
    posts = [await alice.call("GET", f"/posts/{post_id}") for post_id in result["part_message_ids"]]
    assert [post["id"] for post in posts] == result["part_message_ids"]
    assert [post["root_id"] for post in posts] == ["", result["message_id"], result["message_id"]]
    reply = json.loads(await toolkit.post_message(text="reply to child", reply_to_message_id=posts[1]["id"]))
    actual = await alice.call("GET", f"/posts/{reply['message_id']}")
    assert reply["thread_id"] == actual["root_id"] == result["message_id"]
    elsewhere = await alice.call("POST", "/posts", json={"channel_id": place.channels["chatter"], "message": "elsewhere"})
    refused = json.loads(await toolkit.post_message(text="wrong destination", reply_to_message_id=elsewhere["id"]))
    assert refused["error"] == "message_not_found" and "message_id" not in refused


async def test_requester_only_post_is_sent_to_only_the_requester_websocket(place, tools, record_property):
    toolkit, turn = tools
    channel = place.channels["lounge"]
    turn(channel, place.id("alice"))
    clients = [MattermostClient(URL, rest.http.headers["Authorization"].removeprefix("Bearer "))
               for rest in (place.user("alice"), place.user("bob"))]
    streams = []
    try:
        for client in clients:
            stream = await client.events()
            streams.append(stream)
            assert (await stream.receive())["event"] == "hello"
        result = json.loads(await toolkit.post_message(text="requester-only contract", visible_only_to_user_id=place.id("alice")))
        if result.get("error") == "forbidden":
            pytest.skip("server bot role lacks create_post_ephemeral permission")
        record_property("ephemeral_result", json.dumps(result))
        assert result["ok"] and result["ephemeral"] and "permalink" not in result

        async def next_ephemeral(stream):
            while True:
                event = await stream.receive()
                if event and event.get("event") == "ephemeral_message":
                    return json.loads(event["data"]["post"])

        shown = await asyncio.wait_for(next_ephemeral(streams[0]), 5)
        assert shown["id"] == result["message_id"] and shown["channel_id"] == channel
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(next_ephemeral(streams[1]), 1)
        for rest in (place.user("alice"), place.user("bob")):
            assert (await rest.http.get(f"/posts/{result['message_id']}")).status_code == 404
    finally:
        for stream in streams:
            await stream.close()
        for client in clients:
            await client.close()


async def test_partial_tool_post_in_ambient_channel_suppresses_the_final(place, request, monkeypatch):
    host, backend = request.getfixturevalue("belle")
    channel = place.channels["chatter"]
    limit = await host.platform.directory.post_limit()
    client = host.platform._client
    request = client.request
    sent_ids = []

    async def fail_second_part(method, path, **kwargs):
        payload = kwargs.get("json", {})
        if method == "POST" and path == "/posts" and payload.get("message", "").startswith("x"):
            if sent_ids:
                raise MattermostError("forbidden", method, path, status=403)
            answer = await request(method, path, **kwargs)
            sent_ids.append(answer["id"])
            return answer
        return await request(method, path, **kwargs)

    monkeypatch.setattr(client, "request", fail_second_part)
    backend.play(ModelTurn(final="This final must be suppressed", calls=(("post_message", {"text": "x" * (limit + 1)}),)))
    await place.user("alice").call("POST", "/posts", json={"channel_id": channel, "message": "please post the long text"})
    assert await _ended(host, 1) == [OUTCOME_POSTED_BY_TOOL]
    result = json.loads(backend.results[0])
    assert result["ok"] is False and result["part_message_ids"] == sent_ids
    posts = await place.bot_posts(channel, 1)
    assert [post["id"] for post in posts] == sent_ids


async def test_cross_channel_widening_requires_a_requester_and_respects_the_approved_target(place, tools):
    toolkit, turn = tools
    quiet, lounge = place.channels["quiet"], place.channels["lounge"]
    turn(quiet, "", "P")
    refused = json.loads(await toolkit.post_message(text="unapproved", chat_id=lounge))
    assert refused["error"] == "write_target_widens_audience" and "message_id" not in refused
    assert await place.bot_posts(lounge, 0) == []
    turn(quiet, place.id("alice"), "P")
    assert await toolkit.widening_question("post_message", {"text": "approved", "chat_id": lounge})
    # The host invokes the tool only after approval; this call exercises that approved destination.
    approved = json.loads(await toolkit.post_message(text="approved", chat_id=lounge))
    shown = await place.user("bob").call("GET", f"/posts/{approved['message_id']}")
    assert approved["chat_id"] == shown["channel_id"] == lounge
