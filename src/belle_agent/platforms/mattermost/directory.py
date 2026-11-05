"""What the server says of channels, their members and users, as the bot sees them, read once and kept a short time."""

import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any

from belle_agent.audience import Audience, audience_newcomers, write_permitted
from belle_agent.platforms.mattermost.capabilities import CHAT_TYPE_OPEN
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.render import RENDERER

#: How long a channel's member list is kept, in seconds. It decides who may read what, so it is kept briefly.
MEMBERS_SECONDS = 60.0
MEMBERS_PAGE = 200
#: A member list longer than this many pages is not read, and the check it serves refuses.
MAX_MEMBER_PAGES = 25
#: How many user names and channel records are kept.
MAX_KEPT = 1024


class MembersIncomplete(RuntimeError):
    """A channel has more members than one check reads."""


def _keep(cache: "OrderedDict[str, Any]", key: str, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > MAX_KEPT:
        cache.popitem(last=False)


class Directory:
    """Channels, members and user names of one server. Each method raises ``MattermostError`` as the server refused."""

    def __init__(self, client: MattermostClient, *, audience_member_max_pages: int = MAX_MEMBER_PAGES) -> None:
        self._client = client
        self._audience_member_max_pages = audience_member_max_pages
        self._channels: OrderedDict[str, Mapping[str, Any]] = OrderedDict()
        self._members: dict[str, tuple[float, frozenset[str]]] = {}
        self._names: OrderedDict[str, str] = OrderedDict()
        self._client_config: Mapping[str, Any] | None = None
        self._team_names: dict[str, str] = {}

    async def channel(self, channel_id: str, *, fresh: bool = False) -> Mapping[str, Any]:
        """The channel's record: its ``type``, ``team_id``, ``name``, ``display_name`` and ``shared`` flag."""
        if fresh or channel_id not in self._channels:
            found = await self._client.request("GET", f"/channels/{channel_id}")
            _keep(self._channels, channel_id, found if isinstance(found, Mapping) else {})
        return self._channels[channel_id]

    async def members(self, channel_id: str, *, fresh: bool = False) -> frozenset[str]:
        """The user ids of a channel's members, or ``MembersIncomplete`` where they are too many to read."""
        now = time.monotonic()
        kept = self._members.get(channel_id)
        if not fresh and kept is not None and now - kept[0] < MEMBERS_SECONDS:
            return kept[1]
        found: set[str] = set()
        for page in range(self._audience_member_max_pages):
            batch = await self._client.request(
                "GET", f"/channels/{channel_id}/members", params={"page": page, "per_page": MEMBERS_PAGE}
            )
            batch = batch if isinstance(batch, list) else []
            found.update(str(member.get("user_id") or "") for member in batch if isinstance(member, Mapping))
            if len(batch) < MEMBERS_PAGE:
                break
        else:
            raise MembersIncomplete(
                f"{channel_id} has more members than platforms.mattermost.audience_member_max_pages"
                f" ({self._audience_member_max_pages} pages of {MEMBERS_PAGE}) can establish"
            )
        members = frozenset(found - {""})
        self._members[channel_id] = (now, members)
        return members

    async def audience(self, channel_id: str, *, fresh: bool = False) -> Audience:
        """Who reads a channel: its team, for an open channel, or its members. A shared channel reaches outside."""
        record = await self.channel(channel_id, fresh=fresh)
        external = bool(record.get("shared"))
        if record.get("type") == CHAT_TYPE_OPEN and not external:
            return Audience(workspace_wide=True, members=frozenset(), external=False)
        return Audience(workspace_wide=False, members=await self.members(channel_id, fresh=fresh), external=external)

    async def newcomers(self, origin: str, target: str, *, fresh: bool = False) -> tuple[tuple[str, ...] | None, Audience]:
        """Who a post in the target reaches beyond the origin's audience."""
        source_record = await self.channel(origin, fresh=fresh)
        target_record = await self.channel(target, fresh=fresh)
        source = await self.audience(origin, fresh=fresh)
        reached = await self.audience(target, fresh=fresh)
        if source.workspace_wide and source_record.get("team_id") != target_record.get("team_id"):
            source = Audience(False, frozenset(), source.external)
        return (() if write_permitted(source, reached) else audience_newcomers(reached, source)), reached

    async def in_team(self, team_id: str, user_ids: Iterable[str]) -> frozenset[str]:
        """Those of ``user_ids`` who are members of the team.

        The server returns current members only. To a user who is no admin it gives -1 as the removal time of
        each member but that user, so the time says nothing and is not read.
        """
        wanted = sorted(set(user_ids))
        if not wanted:
            return frozenset()
        found = await self._client.request("POST", f"/teams/{team_id}/members/ids", json=wanted)
        return frozenset(
            str(member.get("user_id") or "") for member in (found if isinstance(found, list) else []) if isinstance(member, Mapping)
        ) - {""}

    def learn_name(self, user_id: str, username: str) -> None:
        """Keep a user's name that an event gave."""
        if user_id and username:
            _keep(self._names, user_id, username)

    def known_name(self, user_id: str) -> str:
        """The user name kept for an id, or empty."""
        return self._names.get(user_id, "")

    async def usernames(self, user_ids: Iterable[str]) -> dict[str, str]:
        """The user name of each id the server knows. An id it does not know is left out."""
        user_ids = list(user_ids)
        wanted = sorted({user_id for user_id in user_ids if user_id} - set(self._names))
        if wanted:
            found = await self._client.request("POST", "/users/ids", json=wanted)
            for user in found if isinstance(found, list) else []:
                if isinstance(user, Mapping):
                    self.learn_name(str(user.get("id") or ""), str(user.get("username") or ""))
        return {user_id: self._names[user_id] for user_id in user_ids if user_id in self._names}

    async def team_name(self, team_id: str) -> str:
        """The name of a team, which addresses it in the web app. With no team id, the name of the bot's first team."""
        if team_id not in self._team_names:
            if team_id:
                found = await self._client.request("GET", f"/teams/{team_id}")
            else:
                teams = await self._client.request("GET", "/users/me/teams")
                found = teams[0] if isinstance(teams, list) and teams else {}
            self._team_names[team_id] = str((found if isinstance(found, Mapping) else {}).get("name") or "")
        return self._team_names[team_id]

    async def client_config(self) -> Mapping[str, Any]:
        """The settings the server gives every client, such as ``SiteURL`` and ``MaxPostSize``. Read once."""
        if self._client_config is None:
            found = await self._client.request("GET", "/config/client", params={"format": "old"})
            self._client_config = found if isinstance(found, Mapping) else {}
        return self._client_config

    async def post_limit(self) -> int:
        """The server's ``MaxPostSize``, the most characters a post holds, or 0 where the server states none."""
        try:
            return max(0, int(str((await self.client_config()).get("MaxPostSize") or "")))
        except (MattermostError, ValueError):
            return 0

    async def parts(self, text: str) -> list[str]:
        """The text in parts of the server's size. Where the server states no size, the whole text, for it to judge."""
        return [part.markdown for part in RENDERER.parts(text, await self.post_limit())]
