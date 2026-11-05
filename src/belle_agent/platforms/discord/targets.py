"""Resolve Discord destinations using current REST permission evidence."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from belle_agent.platforms.discord.client import DiscordError

VIEW = 1 << 10
SEND = 1 << 11
READ_HISTORY = 1 << 16
CONNECT = 1 << 20
ADMIN = 1 << 3
MANAGE_THREADS = 1 << 34
SEND_THREADS = 1 << 38
MENTION_EVERYONE = 1 << 17
SEND_TTS = 1 << 12
THREAD_TYPES = (10, 11, 12)


def permissions(guild: Mapping[str, Any], channel: Mapping[str, Any], member: Mapping[str, Any]) -> int:
    """Apply Discord overwrites, then the member's active timeout restriction."""
    until = member.get('communication_disabled_until')
    timeout = None
    if until is not None:
        try:
            if not isinstance(until, str):
                raise TypeError
            timeout = datetime.fromisoformat(until)
            if timeout.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError('communication_disabled_until must be null or an ISO8601 timestamp with timezone') from None
    user = str(member['user']['id'])
    roles = {str(guild['id']), *map(str, member['roles'])}
    if not roles <= {str(role['id']) for role in guild['roles']}:
        raise ValueError('member roles are absent from guild roles')
    value = 0
    for role in guild['roles']:
        if str(role['id']) in roles:
            value |= int(role['permissions'])
    if user == str(guild['owner_id']) or value & ADMIN:
        return -1
    for kind, ids in ((0, {str(guild['id'])}), (0, roles - {str(guild['id'])}), (1, {user})):
        allow = deny = 0
        for overwrite in channel.get('permission_overwrites', ()):
            if overwrite['type'] == kind and str(overwrite['id']) in ids:
                deny |= int(overwrite['deny'])
                allow |= int(overwrite['allow'])
        value = value & ~deny | allow
    # Discord removes all other permissions during a timeout, even overwrite grants.
    if timeout is not None and timeout > datetime.now(UTC):
        value &= VIEW | READ_HISTORY
    return value


async def visibility_channels(client: Any, channel: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """A category is also visible through any of its visible child channels."""
    if channel.get('type') != 4:
        return (channel,)
    guild_id = str(channel['guild_id'])
    path = f'/guilds/{guild_id}/channels'
    channels = await client.request('GET', path)
    if not isinstance(channels, list) or any(not isinstance(item, Mapping) for item in channels):
        raise DiscordError('bad_response', 'GET', path)
    children = tuple(item for item in channels if str(item.get('parent_id')) == str(channel['id']))
    if any(str(child.get('guild_id')) != guild_id for child in children):
        raise DiscordError('target_mismatch', 'GET', path)
    return (channel, *children)


@dataclass(frozen=True)
class Target:
    channel: str
    guild: str
    parent: str
    readers: frozenset[str]
    bot_permissions: int = 0
    mentionable_roles: frozenset[str] = frozenset()
    kind: int = 0
    history_readers: frozenset[str] = frozenset()


class TargetResolver:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.client = platform._client

    async def resolve(self, channel_id: str, requester: str, *, posting: bool = True, visibility_only: bool = False) -> Target:
        try:
            return await self._resolve(channel_id, requester, posting=posting, visibility_only=visibility_only)
        except (KeyError, TypeError, ValueError):
            raise DiscordError('bad_response', 'GET', f'/channels/{channel_id}') from None

    async def _resolve(self, channel_id: str, requester: str, *, posting: bool, visibility_only: bool = False) -> Target:
        channel = await self.client.mapping('GET', f'/channels/{channel_id}')
        if str(channel.get('id')) != channel_id:
            raise DiscordError('target_mismatch', 'GET', f'/channels/{channel_id}')
        kind = channel.get('type')
        if kind == 1:
            dm_readers = frozenset({self.platform.bot_user_id, *(str(user['id']) for user in channel['recipients'])})
            if requester and requester not in dm_readers:
                raise DiscordError('requester_cannot_access_target', 'GET', f'/channels/{channel_id}')
            return Target(channel_id, '', '', dm_readers, kind=kind, history_readers=dm_readers)
        supported: tuple[int, ...] = (0, 5, 15, 16, *THREAD_TYPES)
        if not posting:
            supported += (2, 4, 13, 14) if visibility_only else (2,)
        if kind not in supported:
            raise DiscordError('unsupported_post_target', 'GET', f'/channels/{channel_id}')
        guild_id = str(channel['guild_id'])
        guild = await self.client.mapping('GET', f'/guilds/{guild_id}')
        if str(guild['id']) != guild_id:
            raise DiscordError('target_mismatch', 'GET', f'/guilds/{guild_id}')
        parent = str(channel.get('parent_id') or '') if kind in THREAD_TYPES else ''
        if kind in THREAD_TYPES and not parent:
            raise DiscordError('thread_parent_required', 'GET', f'/channels/{channel_id}')
        permission_channel = await self.client.mapping('GET', f'/channels/{parent}') if parent else channel
        if parent and (str(permission_channel.get('id')) != parent or str(permission_channel.get('guild_id')) != guild_id or permission_channel.get('type') not in (0, 5, 15, 16)):
            raise DiscordError('target_mismatch', 'GET', f'/channels/{parent}')
        visible_channels = await visibility_channels(self.client, permission_channel)
        private_members: set[str] = set()
        if kind == 12:
            after = '0'
            while True:
                page = await self.client.request('GET', f'/channels/{channel_id}/thread-members', params={'limit': 100, 'after': after, 'with_member': 'true'})
                if not isinstance(page, list):
                    raise DiscordError('bad_response', 'GET', f'/channels/{channel_id}/thread-members')
                private_members.update(str(item['user_id']) for item in page)
                if len(page) < 100:
                    break
                cursor = str(page[-1]['user_id'])
                if cursor == after:
                    raise DiscordError('bad_response', 'GET', f'/channels/{channel_id}/thread-members')
                after = cursor
        readers: set[str] = set()
        history_readers: set[str] = set()
        bot_permissions = 0
        after = '0'
        while True:
            members = await self.client.request('GET', f'/guilds/{guild_id}/members', params={'limit': 1000, 'after': after})
            if not isinstance(members, list):
                raise DiscordError('bad_response', 'GET', f'/guilds/{guild_id}/members')
            for member in members:
                user = str(member['user']['id'])
                granted = permissions(guild, permission_channel, member)
                if kind == 4 and any(permissions(guild, child, member) & VIEW for child in visible_channels[1:]):
                    granted |= VIEW
                accessible = (
                    bool(granted & VIEW)
                    and (kind != 2 or visibility_only or bool(granted & CONNECT))
                    and (kind != 12 or user in private_members or bool(granted & MANAGE_THREADS))
                )
                if accessible:
                    readers.add(user)
                    if granted & READ_HISTORY:
                        history_readers.add(user)
                    if user == self.platform.bot_user_id:
                        bot_permissions = granted
            if len(members) < 1000:
                break
            cursor = str(members[-1]['user']['id'])
            if cursor == after:
                raise DiscordError('bad_response', 'GET', f'/guilds/{guild_id}/members')
            after = cursor
        required = SEND_THREADS if parent else SEND
        if not bot_permissions or posting and not bot_permissions & required:
            raise DiscordError('bot_cannot_post_target', 'GET', f'/channels/{channel_id}')
        if requester and requester not in readers:
            raise DiscordError('requester_cannot_access_target', 'GET', f'/channels/{channel_id}')
        mentionable = frozenset(str(role['id']) for role in guild['roles'] if role.get('mentionable'))
        return Target(channel_id, guild_id, parent, frozenset(readers), bot_permissions, mentionable, kind,
                      frozenset(history_readers))
