"""The edit, removal, pin, file, search and name tools against a Mattermost test server.

The fixtures of ``conftest.py`` give a team with a bot, alice,
bob and their channels, and the connector's toolkit on the bot. Two checks
need a server setting that a default server does not have, and skip without it.
"""

import asyncio
import json

import pytest
from test_mattermost_server import Rest

from belle_agent.attachments import MAX_FILE_BYTES

pytestmark = pytest.mark.mattermost_server


async def _client_config(rest: Rest) -> dict:
    return await rest.call("GET", "/config/client", params={"format": "old"})


async def test_the_bot_edits_and_deletes_its_own_post_and_not_another_author_s(place, tools):
    toolkit, turn = tools
    lounge = place.channels["lounge"]
    turn(lounge, place.id("alice"))
    posted = json.loads(await toolkit.post_message(text="draft"))
    theirs = await place.user("alice").call("POST", "/posts", json={"channel_id": lounge, "message": "mine"})

    edited = json.loads(await toolkit.edit_message(message_id=posted["message_id"], text="final"))
    refused = json.loads(await toolkit.edit_message(message_id=theirs["id"], text="changed"))
    shown = await place.admin.call("GET", f"/posts/{posted['message_id']}")
    deleted = json.loads(await toolkit.delete_message(message_id=posted["message_id"]))
    not_deleted = json.loads(await toolkit.delete_message(message_id=theirs["id"]))

    assert edited["ok"] and shown["message"] == "final"
    assert refused["error"] == "message_not_own"
    assert deleted == {"ok": True, "chat_id": lounge, "message_id": posted["message_id"], "deleted": True}
    assert not_deleted["error"] == "message_not_own"
    assert [post["message"] for post in await place.admin.posts(lounge)][-1] == "mine"


async def test_an_edit_and_a_pin_past_the_server_s_edit_limit_are_refused_with_the_limit(place, tools):
    limit = int(str((await _client_config(place.admin)).get("PostEditTimeLimit") or -1))
    if not 0 < limit <= 30:
        pytest.skip("the server sets no edit limit of 30 seconds or less (PostEditTimeLimit)")
    toolkit, turn = tools
    turn(place.channels["lounge"], place.id("alice"))
    posted = json.loads(await toolkit.post_message(text="soon old"))
    await asyncio.sleep(limit + 1)

    edit = json.loads(await toolkit.edit_message(message_id=posted["message_id"], text="too late"))
    pin = json.loads(await toolkit.pin_message(message_id=posted["message_id"]))

    for refused in (edit, pin):
        assert refused["error"] == "edit_time_limit_passed"
        assert f"for {limit} seconds" in refused["detail"] and "permissions_time_limit" in refused["detail"]


async def test_a_text_file_sent_into_a_thread_is_read_back_by_a_member(place, tools):
    toolkit, turn = tools
    lounge = place.channels["lounge"]
    alice = place.user("alice")
    root = await alice.call("POST", "/posts", json={"channel_id": lounge, "message": "send me the notes"})
    turn(lounge, place.id("alice"))

    sent = json.loads(await toolkit.send_file(filename="notes.md", content="# Notes\nslack water\n", reply_to_message_id=root["id"]))
    outside = json.loads(await toolkit.send_file(filename="notes.md", content="x", chat_id=place.channels["outside"]))

    post = await alice.call("GET", f"/posts/{sent['message_id']}")
    body = await alice.http.get(f"/files/{sent['file_id']}")
    assert (post["root_id"], post["file_ids"]) == (root["id"], [sent["file_id"]])
    assert body.content == b"# Notes\nslack water\n"
    assert outside["error"] == "forbidden" and "member" in outside["detail"]


async def test_a_file_over_the_server_s_limit_is_refused_with_the_server_s_answer(place, tools):
    limit = int(str((await _client_config(place.admin)).get("MaxFileSize") or 0))
    if not 0 < limit < MAX_FILE_BYTES:
        pytest.skip("the server's MaxFileSize is not below this deployment's ceiling, so Belle refuses first")
    toolkit, turn = tools
    turn(place.channels["lounge"], place.id("alice"))

    refused = json.loads(await toolkit.send_file(filename="big.bin", content="x" * (limit + 1)))

    assert refused["error"] == "too_large"
    assert f"at most {limit} bytes" in refused["detail"] and "too_large_detailed" in refused["detail"]


async def test_a_pin_is_read_back_and_an_unpin_takes_it_away(place, tools):
    toolkit, turn = tools
    lounge = place.channels["lounge"]
    keep = await place.user("alice").call("POST", "/posts", json={"channel_id": lounge, "message": "the tide table"})
    turn(lounge, place.id("alice"))

    pinned = json.loads(await toolkit.pin_message(message_id=keep["id"]))
    again = json.loads(await toolkit.pin_message(message_id=keep["id"]))
    listed = json.loads(await toolkit.read_mattermost_pinned_messages())
    json.loads(await toolkit.pin_message(message_id=keep["id"], remove=True))
    after = json.loads(await toolkit.read_mattermost_pinned_messages())

    assert pinned["pinned"] and again["pinned"]
    assert [message["text"] for message in listed["pinned_messages"]] == ["the tide table"]
    assert after["pinned_messages"] == [] and after["coverage"]["status"] == "complete"


async def test_a_search_with_from_and_in_and_the_open_channel_it_could_not_search(place, tools):
    toolkit, turn = tools
    word = f"zebracorn{place.tag}"
    alice, bob = place.user("alice"), place.user("bob")
    await alice.call("POST", "/posts", json={"channel_id": place.channels["lounge"], "message": f"{word} by alice"})
    await bob.call("POST", "/posts", json={"channel_id": place.channels["lounge"], "message": f"{word} by bob"})
    await alice.call("POST", "/posts", json={"channel_id": place.channels["chatter"], "message": f"{word} in chatter"})
    await bob.call("POST", "/posts", json={"channel_id": place.channels["outside"], "message": f"{word} outside"})
    turn(place.channels["lounge"], place.id("alice"))

    every = json.loads(await toolkit.search_messages(query=word))
    from_bob = json.loads(await toolkit.search_messages(query=f"{word} from:bob-{place.tag}"))
    in_chatter = json.loads(await toolkit.search_messages(query=f"{word} in:chatter-{place.tag}"))
    in_outside = json.loads(await toolkit.search_messages(query=f"{word} in:outside-{place.tag}"))

    assert sorted(message["text"] for message in every["messages"]) == [
        f"{word} by alice", f"{word} by bob", f"{word} in chatter"
    ]
    assert [message["author_name"] for message in from_bob["messages"]] == [f"bob-{place.tag}"]
    assert [message["chat_id"] for message in in_chatter["messages"]] == [place.channels["chatter"]]
    assert in_outside["messages"] == []
    assert {"chat_id": place.channels["outside"], "chat_name": f"outside-{place.tag}"} in in_outside["coverage"]["channels_not_searched"]
    assert in_outside["coverage"]["channels"] == "partial"


async def test_search_or_and_file_index_on_the_server(place, tools):
    toolkit, turn = tools
    lounge = place.channels["lounge"]
    first, second = f"first{place.tag}", f"second{place.tag}"
    await place.user("alice").call("POST", "/posts", json={"channel_id": lounge, "message": first})
    await place.user("bob").call("POST", "/posts", json={"channel_id": lounge, "message": second})
    turn(lounge, place.id("alice"))

    combined = json.loads(await toolkit.search_messages(query=f"{first} {second}", is_or_search=True))
    assert {first, second} <= {message["text"] for message in combined["messages"]}
    team_combined = json.loads(await toolkit.search_messages(query=f"{first} {second}", team_id=place.team,
                                                     is_or_search=True))
    assert {first, second} <= {message["text"] for message in team_combined["messages"]}

    filename = f"file{place.tag}.txt"
    sent = json.loads(await toolkit.send_file(filename=filename, content="indexed file"))
    assert sent["ok"]
    found = json.loads(await toolkit.search_mattermost_files(query=f"file{place.tag}"))
    assert found["ok"] and sent["file_id"] in {item["file_id"] for item in found["files"]}
    team_found = json.loads(await toolkit.search_mattermost_files(query=f"file{place.tag}", team_id=place.team))
    assert team_found["ok"] and sent["file_id"] in {item["file_id"] for item in team_found["files"]}
    extension_found = json.loads(await toolkit.search_mattermost_files(query=f"file{place.tag} ext:txt"))
    assert extension_found["ok"] and sent["file_id"] in {item["file_id"] for item in extension_found["files"]}


async def test_a_person_and_a_channel_are_found_by_name_and_a_private_channel_only_where_the_bot_is(place, tools):
    toolkit, turn = tools
    secret = await place.admin.call(
        "POST", "/channels",
        json={"team_id": place.team, "name": f"quiet-secret-{place.tag}", "display_name": "secret", "type": "P"},
    )
    turn(place.channels["lounge"], place.id("alice"))

    person = json.loads(await toolkit.find_by_name(query=f"bob-{place.tag}", kind="person"))
    channels = json.loads(await toolkit.find_by_name(query="quiet", kind="chat"))
    outside = json.loads(await toolkit.find_by_name(query=f"#outside-{place.tag}"))

    assert [(match["id"], match["match_type"]) for match in person["matches"]] == [(place.id("bob"), "exact")]
    assert "email" not in json.dumps(person)
    found = {match["id"] for match in channels["matches"]}
    assert place.channels["quiet"] in found and secret["id"] not in found
    assert [match["id"] for match in outside["matches"] if match["kind"] == "chat"] == [place.channels["outside"]]
