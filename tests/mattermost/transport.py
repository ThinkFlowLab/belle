"""A Mattermost server that exists only in the test process: a recorded REST API and a WebSocket feeder.

Nothing here opens a connection. The REST API records every request and
answers from a table, as the server would. The feeder hands the connector the
events a test puts in it. The token and the ids are synthetic.
"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default
from typing import Any

import httpx

URL = "http://mattermost.invalid"
TOKEN = "synthetic-bot-token-0123456789"
BOT_USER = "b0tb0tb0tb0tb0tb0tb0tb0tb0"
CONNECTION = "c0nnc0nnc0nnc0nnc0nnc0nnc0"


@dataclass
class Request:
    """One request the connector sent."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: Any
    query: Mapping[str, str] = field(default_factory=dict)


@dataclass
class FakeRest:
    """The REST API: each ``(method, path below /api/v4)`` answered with ``(status, body)``."""

    #: Answers that take the place of what the server holds.
    routes: dict[tuple[str, str], tuple[int, Any]] = field(default_factory=dict)
    #: The user id of the bot the token belongs to, and the first letter of each post id this server gives.
    bot: str = BOT_USER
    post_letter: str = "p"
    requests: list[Request] = field(default_factory=list)
    #: Every post the server holds, by id: those the connector posted, and those a test puts here.
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The posts a request deleted, in order.
    deleted: list[dict[str, Any]] = field(default_factory=list)
    #: Set, every request fails with this transport error.
    unreachable: bool = False
    #: Channel records by id, each with its ``type`` and ``team_id``, and the user ids of each channel's members.
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)
    #: User records by id, each with its ``username``.
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The user ids of each team's members.
    teams: dict[str, list[str]] = field(default_factory=dict)
    #: Each file the server holds, by id: its record, as ``/files/{id}/info`` gives it, and its bytes.
    files: dict[str, tuple[dict[str, Any], bytes]] = field(default_factory=dict)
    #: The custom emoji of the server, by name.
    emoji: list[str] = field(default_factory=list)
    search_paging: bool = False
    #: What ``/config/client`` gives every client.
    client_config: dict[str, Any] = field(
        default_factory=lambda: {
            "SiteURL": "http://chat.invalid", "MaxPostSize": "16383", "MaxFileSize": "104857600",
            "EnableUserTypingMessages": "true", "TimeBetweenUserTypingUpdatesMilliseconds": "5000",
        }
    )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        path = request.url.path.removeprefix("/api/v4")
        if "multipart/form-data" in request.headers.get("content-type", ""):
            raw = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
            body = {
                part.get_param("name", header="content-disposition"): part.get_content()
                for part in BytesParser(policy=default).parsebytes(raw).iter_parts()
            }
        else:
            body = json.loads(request.content) if request.content else None
        self.requests.append(
            Request(request.method, path, dict(request.headers), body, dict(request.url.params))
        )
        body = self.requests[-1].body
        status, answer = self.routes.get((request.method, path)) or self._serve(
            request.method, path, body, dict(request.url.params)
        )
        if isinstance(answer, bytes):
            return httpx.Response(status, content=answer)
        return httpx.Response(status, json=answer)

    async def send_body(self, url: str, params: Mapping[str, str], token: str, data: bytes) -> tuple[int, bytes]:
        """Take an upload as the server does: only into a channel the bot is a member of, and up to ``MaxFileSize``."""
        path = url.split("/api/v4", 1)[1]
        self.requests.append(Request("POST", path, {"authorization": f"Bearer {token}"}, {**params, "bytes": len(data)}))
        channel = params["channel_id"]
        if len(data) > int(self.client_config["MaxFileSize"]):
            return 413, json.dumps({"id": "api.file.upload_file.too_large_detailed.app_error", "status_code": 413}).encode()
        if self.bot not in self.members.get(channel, []):
            return 403, json.dumps({"id": "api.context.permissions.app_error", "status_code": 403}).encode()
        info = self.file(f"f{len(self.files):025d}", channel, params["filename"], data)
        return 201, json.dumps({"file_infos": [info]}).encode()

    def file(self, file_id: str, channel: str, name: str, data: bytes, mime: str = "text/plain", post: str = "") -> dict[str, Any]:
        """Put a file on the server, shared in ``channel``, and return its record."""
        info = {"id": file_id, "channel_id": channel, "post_id": post, "name": name, "mime_type": mime,
                "size": len(data), "create_at": 1_800_000_000_000, "user_id": "a" * 26}
        self.files[file_id] = (info, data)
        return info

    def post(self, post_id: str, channel: str, user: str, message: str = "", root: str = "", **fields: Any) -> dict[str, Any]:
        """Put a post on the server, after every post it holds."""
        created = 1_800_000_000_000 + 1000 * len(self.posts)
        self.posts[post_id] = {"id": post_id, "channel_id": channel, "user_id": user, "message": message,
                               "root_id": root, "type": "", "props": {}, "create_at": created, **fields}
        return self.posts[post_id]

    def _channel_posts(self, channel: str, params: dict[str, str]) -> dict[str, Any]:
        newest_first = sorted((post for post in self.posts.values() if post["channel_id"] == channel),
                              key=lambda post: -post["create_at"])
        if before := params.get("before"):
            cut = next(index for index, post in enumerate(newest_first) if post["id"] == before)
            newest_first = newest_first[cut + 1:]
        size = int(params.get("per_page", 60))
        start = int(params.get("page", 0)) * size
        page = newest_first[start:start + size]
        rest = newest_first[start + len(page):]
        return {"order": [post["id"] for post in page], "posts": {post["id"]: post for post in page},
                "next_post_id": "", "prev_post_id": rest[0]["id"] if rest else ""}

    def _serve(self, method: str, path: str, body: Any, params: dict[str, str]) -> tuple[int, Any]:
        """Answer the routes the connector uses as the server would, from what it holds."""
        parts = path.strip("/").split("/")
        not_found = (404, {"id": "api.context.404.app_error", "status_code": 404})
        if (reading := self._reading(method, path, body, params)) is not None:
            return reading
        if (method, path) in (("POST", "/posts"), ("POST", "/posts/ephemeral")):
            post = body["post"] if path.endswith("ephemeral") else body
            post_id = f"{self.post_letter}{len(self.posts) + len(self.deleted):025d}"
            return 201, self.post(post_id, post["channel_id"], self.bot, post["message"], post.get("root_id", ""),
                                  props=post.get("props") or {}, file_ids=post.get("file_ids", []))
        if parts[0] == "posts" and len(parts) >= 2 and parts[1] in self.posts and (method, parts[2:]) in (
            ("PUT", ["patch"]), ("DELETE", []), ("POST", ["pin"]), ("POST", ["unpin"])
        ):
            post = self.posts[parts[1]]
            if parts[2:] in (["pin"], ["unpin"]):
                post["is_pinned"] = parts[2] == "pin"
                return 200, {"status": "OK"}
            if post["user_id"] != self.bot:
                return 403, {"id": "api.context.permissions.app_error", "status_code": 403}
            if method == "DELETE":
                self.deleted.append(self.posts.pop(parts[1]))
                return 200, {"status": "OK"}
            post.update({key: body[key] for key in ("message", "props") if key in body})
            return 200, post
        if method == "GET" and parts[0] == "files" and len(parts) in (2, 3):
            if parts[1] not in self.files:
                return not_found
            info, data = self.files[parts[1]]
            return (200, info) if parts[2:] == ["info"] else (200, data)
        if (method, path) == ("POST", "/reactions"):
            return 201, body
        if method == "DELETE" and parts[0] == "users" and parts[2:3] == ["posts"] and parts[4:5] == ["reactions"]:
            return 200, {"status": "OK"}
        if method == "GET" and parts[0] == "posts" and len(parts) in (2, 3) and parts[1] in self.posts:
            if len(parts) == 2:
                return 200, self.posts[parts[1]]
            root = self.posts[parts[1]]["root_id"] or parts[1]
            thread = {key: post for key, post in self.posts.items() if key == root or post["root_id"] == root}
            return 200, {"order": list(thread), "posts": thread}
        if method == "GET" and parts[0] == "teams" and len(parts) == 2:
            return 200, {"id": parts[1], "name": f"team-{parts[1][:4]}"}
        if method == "GET" and parts[0] == "teams" and parts[2:3] == ["members"] and len(parts) == 4:
            return (200, {"user_id": parts[3]}) if parts[3] in self.teams.get(parts[1], []) else not_found
        if (method, path) == ("GET", "/users/me/channels"):
            # Every channel of every team that the bot is a member of, direct and group ones too, unpaged.
            return 200, [
                {**channel, "last_post_at": max((post["create_at"] for post in self.posts.values()
                                                 if post["channel_id"] == key), default=0)}
                for key, channel in self.channels.items() if self.bot in self.members.get(key, [])
            ]
        if (method, path) == ("GET", "/users/me/teams"):
            return 200, [{"id": team, "name": f"team-{team[:4]}"} for team in sorted(self.teams)]
        if (method, path) == ("GET", "/users/me"):
            return 200, {"id": self.bot, "username": "belle", "is_bot": True}
        if (method, path) == ("POST", "/users/me/typing"):
            return 200, {"status": "OK"}
        if (method, path) == ("GET", "/config/client"):
            return 200, self.client_config
        if (method, path) == ("POST", "/users/ids"):
            return 200, [self.users[user] for user in body if user in self.users]
        if (method, path) == ("POST", "/channels/direct"):
            channel = "d" + "".join(sorted(body))[:25]
            self.channels[channel] = {"id": channel, "type": "D", "team_id": ""}
            self.members[channel] = list(body)
            return 201, self.channels[channel]
        if method == "GET" and parts[0] == "users" and len(parts) == 2:
            return (200, self.users[parts[1]]) if parts[1] in self.users else not_found
        if parts[0] == "teams" and parts[2:] == ["members", "ids"]:
            # As the server answers a user who is no admin: only current members, and -1 as the removal time
            # of every member but the one who asks.
            return 200, [{"user_id": user, "team_id": parts[1], "delete_at": 0 if user == self.bot else -1}
                         for user in body if user in self.teams.get(parts[1], [])]
        if method == "GET" and parts[0] == "channels" and len(parts) >= 2:
            if parts[1] not in self.channels:
                return not_found
            if len(parts) == 2:
                return 200, self.channels[parts[1]]
            if parts[2] == "members":
                if len(parts) == 4:
                    return (200, {"user_id": parts[3]}) if parts[3] in self.members.get(parts[1], []) else not_found
                page, size = int(params.get("page", 0)), int(params.get("per_page", 60))
                return 200, [{"user_id": user} for user in self.members.get(parts[1], [])[page * size:(page + 1) * size]]
            if parts[2] == "posts":
                return 200, self._channel_posts(parts[1], params)
        return not_found

    def _page(self, records: list[Any], params: dict[str, str]) -> tuple[int, Any]:
        page, size = int(params.get("page", 0)), int(params.get("per_page", 60))
        return 200, records[page * size:(page + 1) * size]

    def _reading(self, method: str, path: str, body: Any, params: dict[str, str]) -> tuple[int, Any] | None:
        """Answer the routes of the search, name and pin tools, or ``None``."""
        parts = path.strip("/").split("/")
        if method == "POST" and (path == "/files/search" or parts[0] == "teams" and parts[2:] == ["files", "search"]):
            team = parts[1] if parts[0] == "teams" else None
            words = [word.lower() for word in body["terms"].split() if ":" not in word]
            found = sorted(
                (info for info, data in self.files.values()
                 if self.bot in self.members.get(info["channel_id"], [])
                 and (team is None or self.channels.get(info["channel_id"], {}).get("team_id") == team)
                 and (body.get("include_deleted_channels") or not self.channels.get(info["channel_id"], {}).get("delete_at"))
                 and all(word in (info["name"] + " " + data.decode("utf-8", "replace")).lower() for word in words)),
                key=lambda info: -info["create_at"],
            )
            if self.search_paging and "page" in body:
                page, size = int(body["page"]), int(body["per_page"])
                found = found[page * size:(page + 1) * size]
            return 200, {"order": [info["id"] for info in found], "file_infos": {info["id"]: info for info in found}}
        if method == "POST" and (path == "/posts/search" or parts[0] == "teams" and parts[2:] == ["posts", "search"]):
            team = parts[1] if parts[0] == "teams" else None
            # The server searches the channels the bot is a member of, newest first, and ignores the filters here.
            words = [word.lower() for word in body["terms"].split() if ":" not in word]
            found = sorted(
                (post for post in self.posts.values()
                 if self.bot in self.members.get(post["channel_id"], [])
                 and (team is None or self.channels.get(post["channel_id"], {}).get("team_id") in (team, ""))
                 and (body.get("include_deleted_channels") or not self.channels.get(post["channel_id"], {}).get("delete_at"))
                 and (any(word in post["message"].lower() for word in words) if body.get("is_or_search")
                      else all(word in post["message"].lower() for word in words))),
                key=lambda post: -post["create_at"],
            )
            if self.search_paging and "page" in body:
                page, size = body["page"], body["per_page"]
                found = found[page * size:(page + 1) * size]
            return 200, {"order": [post["id"] for post in found], "posts": {post["id"]: post for post in found},
                         "first_inaccessible_post_time": 0, "matches": None}
        if method == "GET" and parts[0] == "channels" and parts[2:] == ["pinned"]:
            pinned = {key: post for key, post in self.posts.items() if post["channel_id"] == parts[1] and post.get("is_pinned")}
            return 200, {"order": list(pinned), "posts": pinned}
        if method == "GET" and parts[0] == "teams" and parts[2:] == ["channels"]:
            return self._page([channel for channel in self.channels.values()
                               if channel.get("team_id") == parts[1] and channel["type"] == "O"], params)
        if method == "GET" and parts[:2] == ["users", "me"] and parts[2:3] == ["teams"] and parts[4:] == ["channels"]:
            return self._page([channel for key, channel in self.channels.items()
                               if channel.get("team_id") == parts[3] and self.bot in self.members.get(key, [])], params)
        if (method, path) == ("GET", "/users"):
            return self._page(list(self.users.values()), params)
        if (method, path) == ("GET", "/emoji"):
            return self._page([{"id": f"e{index:025d}", "name": name} for index, name in enumerate(self.emoji)], params)
        return None

    def calls(self, method: str, prefix: str) -> list[Request]:
        """The requests of one method whose path starts with ``prefix``."""
        return [request for request in self.requests if request.method == method and request.path.startswith(prefix)]


class EventFeeder:
    """The WebSocket. A test puts events in; ``None`` drops the connection, which the connector may open again."""

    _WAKE = object()

    def __init__(self, *events: Mapping[str, Any] | None) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for event in events:
            self._queue.put_nowait(event)
        #: The URL and the token of each connection the connector opened.
        self.opened: list[tuple[str, str]] = []
        self.closed = False

    def feed(self, event: Mapping[str, Any] | None) -> None:
        self._queue.put_nowait(event)

    async def open(self, url: str, token: str) -> "EventFeeder":
        self.opened.append((url, token))
        self.closed = False
        return self

    async def receive(self) -> Mapping[str, Any] | None:
        while not self.closed:
            event = await self._queue.get()
            if event is self._WAKE:
                continue
            if event is None:
                self.closed = True
            return event
        return None

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(self._WAKE)


def hello(seq: int = 0, connection: str = CONNECTION) -> dict[str, Any]:
    """The first event of a connection."""
    return {"event": "hello", "data": {"connection_id": connection, "server_version": "synthetic"}, "seq": seq}


def posted(
    post_id: str,
    *,
    channel: str = "c" * 26,
    user: str = "a" * 26,
    message: str = "hello",
    channel_type: str = "O",
    team: str = "t" * 26,
    root: str = "",
    mentions: list[str] | None = None,
    seq: int = 1,
    edited: bool = False,
    **post: Any,
) -> dict[str, Any]:
    """A ``posted`` or ``post_edited`` event, with the post and the mentions as JSON text."""
    body = {"id": post_id, "channel_id": channel, "user_id": user, "message": message, "root_id": root,
            "type": "", "props": {}, "update_at": 1800000000000, **post}
    data: dict[str, Any] = {"post": json.dumps(body), "channel_type": channel_type, "team_id": team}
    if mentions:
        data["mentions"] = json.dumps(mentions)
    return {"event": "post_edited" if edited else "posted", "data": data, "seq": seq}

