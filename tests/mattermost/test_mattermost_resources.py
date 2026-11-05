"""Resource tools preserve requester and channel authority."""

import json

from belle_agent.platforms.mattermost.client import MattermostError
from belle_agent.platforms.mattermost.resources import MattermostResourcesToolkit

CHANNEL = "c" * 26
TEAM = "t" * 26
USER = "u" * 26
BOOKMARK = "b" * 26


class Client:
    def __init__(self) -> None:
        self.calls = []
        self.permissions = ["add_bookmark_public_channel"]

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == f"/users/{USER}":
            return {"roles": "system_user"}
        if path == f"/channels/{CHANNEL}/members/{USER}":
            return {"roles": "channel_user"}
        if path == f"/teams/{TEAM}/members/{USER}":
            return {"roles": "team_user"}
        if path == "/roles/names":
            return [{"name": name, "permissions": self.permissions} for name in kwargs["json"]]
        if path == f"/channels/{CHANNEL}/bookmarks":
            return {"id": BOOKMARK} if method == "POST" else [{"id": BOOKMARK, "type": "link", "link_url": "https://example.org"}]
        if path == f"/channels/{CHANNEL}/bookmarks/{BOOKMARK}":
            return {"id": BOOKMARK}
        if path == f"/teams/{TEAM}/groups":
            return [{"id": "g" * 26, "name": "group", "member_count": 3}]
        raise AssertionError((method, path))


class Directory:
    async def channel(self, channel):
        assert channel == CHANNEL
        return {"id": CHANNEL, "team_id": TEAM, "type": "O"}

    async def members(self, channel):
        assert channel == CHANNEL
        return frozenset({USER})

    async def in_team(self, team, users):
        assert team == TEAM
        return frozenset(users)


def tool(client=None):
    client = client or Client()
    return MattermostResourcesToolkit(client, Directory(), lambda: "bot", lambda: {
        "mattermost_channel_id": CHANNEL, "mattermost_user_id": USER,
    }), client


async def test_groups_page_is_team_scoped_and_does_not_expose_members() -> None:
    resource, client = tool()
    result = json.loads(await resource.list_mattermost_groups(scope="team"))
    assert result["groups"] == [{"id": "g" * 26, "name": "group", "member_count": 3}]
    assert result["coverage"]["status"] == "complete"
    assert client.calls[-1][1] == f"/teams/{TEAM}/groups"


async def test_bookmark_create_requires_requester_permission() -> None:
    resource, client = tool()
    client.permissions = []
    result = json.loads(await resource.create_mattermost_bookmark("Guide", "link", link_url="https://example.org"))
    assert result["error"] == "requester_bookmark_permission_required"
    assert not any(method == "POST" and path.endswith("/bookmarks") for method, path, _ in client.calls)


async def test_bookmark_create_uses_validated_native_fields() -> None:
    resource, client = tool()
    result = json.loads(await resource.create_mattermost_bookmark("Guide", "link", link_url="https://example.org"))
    assert result["bookmark"]["id"] == BOOKMARK
    assert client.calls[-1] == ("POST", f"/channels/{CHANNEL}/bookmarks", {
        "json": {"display_name": "Guide", "type": "link", "link_url": "https://example.org"},
    })


async def test_bookmark_delete_needs_its_own_permission() -> None:
    resource, client = tool()
    result = json.loads(await resource.delete_mattermost_bookmark(BOOKMARK))
    assert result["error"] == "requester_bookmark_permission_required"
    client.permissions = ["delete_bookmark_public_channel"]
    result = json.loads(await resource.delete_mattermost_bookmark(BOOKMARK))
    assert result["bookmark"]["id"] == BOOKMARK


async def test_bookmark_writes_require_current_turn_and_confirmation() -> None:
    resource, _ = tool()
    assert resource.active_turn_only == {
        "create_mattermost_bookmark", "edit_mattermost_bookmark", "delete_mattermost_bookmark",
    }
    assert await resource.widening_question("delete_mattermost_bookmark", {"bookmark_id": BOOKMARK}) == (
        f"Delete bookmark {BOOKMARK} in Mattermost channel {CHANNEL}?"
    )


async def test_bookmark_edit_can_keep_existing_link_source() -> None:
    resource, client = tool()
    client.permissions = ["edit_bookmark_public_channel"]
    result = json.loads(await resource.edit_mattermost_bookmark(BOOKMARK, display_name="Updated"))
    assert result["ok"]
    assert client.calls[-1] == ("PATCH", f"/channels/{CHANNEL}/bookmarks/{BOOKMARK}", {
        "json": {"display_name": "Updated"},
    })


async def test_unlicensed_group_endpoint_is_unavailable_not_empty() -> None:
    class Unlicensed(Client):
        async def request(self, method, path, **kwargs):
            if path.endswith("/groups"):
                raise MattermostError("server_error", method, path, 501)
            return await super().request(method, path, **kwargs)

    resource, _ = tool(Unlicensed())
    result = json.loads(await resource.list_mattermost_groups(scope="team"))
    assert result["error"] == "feature_unavailable"
