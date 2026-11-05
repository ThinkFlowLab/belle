"""Slack place checks for unattended delivery."""

from collections.abc import Mapping
from typing import Any

from belle_agent.audience import audience_newcomers, write_permitted
from belle_agent.platforms.base import PlaceCheck
from belle_agent.platforms.slack.delivery import slack_error_code
from belle_agent.platforms.slack.history_policy import conversation_reaches_outside_workspace, effective_audience
from belle_agent.platforms.slack.tool_common import _as_mapping, conversation_info, conversation_members


class SlackPlaces:
    """Place operations shared by a single workspace connector."""

    _client: Any
    _identity: Any
    _enforce_audience: bool

    @property
    def workspaces(self) -> frozenset[str]:
        return frozenset({self._identity.team_id}) if self._identity.team_id else frozenset()

    async def _place_call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        return _as_mapping(await getattr(self._client, method)(**kwargs))

    async def _place_info(self, conversation: str) -> dict[str, Any]:
        return await conversation_info(self._place_call, conversation)

    async def _place_audience(self, conversation: str):
        info = await self._place_info(conversation)
        if info.get("is_channel") and not info.get("is_private") and not conversation_reaches_outside_workspace(info):
            return effective_audience(info)
        return effective_audience(info, await conversation_members(self._place_call, conversation))

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck:
        if workspace and workspace != self._identity.team_id:
            return PlaceCheck(False, False, f"conversation {conversation} is not in workspace {workspace}")
        chat_type = ""
        try:
            info = await self._place_info(conversation)
            if not info.get("id"):
                return PlaceCheck(False, False, f"conversation {conversation} was not found")
            if info.get("is_im"):
                chat_type = "im"
            elif info.get("is_mpim"):
                chat_type = "mpim"
            elif info.get("is_private") or info.get("is_group"):
                chat_type = "group"
            elif info.get("is_channel"):
                chat_type = "channel"
            member = bool(info.get("is_member") or info.get("is_im") and info.get("is_open"))
            if not member:
                return PlaceCheck(True, False, chat_type=chat_type)
            if thread not in ("", "new"):
                answer = await self._place_call("conversations_replies", channel=conversation, ts=thread, limit=1)
                messages = answer.get("messages")
                if not isinstance(messages, list) or not messages or not isinstance(messages[0], Mapping) or messages[0].get("ts") != thread:
                    return PlaceCheck(False, False, f"thread {thread} is not in {conversation}")
        except Exception as exc:
            if slack_error_code(exc) == "not_in_channel":
                return PlaceCheck(True, False, "bot is not a member of the conversation", chat_type)
            if slack_error_code(exc) in {"channel_not_found", "thread_not_found", "message_not_found"}:
                return PlaceCheck(False, False, f"conversation or thread was not found: {slack_error_code(exc)}")
            raise
        return PlaceCheck(True, True, chat_type=chat_type)

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        if workspace and workspace != self._identity.team_id:
            return False
        info = await self._place_info(conversation)
        if info.get("is_channel") and not info.get("is_private") and not conversation_reaches_outside_workspace(info):
            response = await self._place_call("users_info", user=user)
            record = response.get("user")
            return isinstance(record, Mapping) and record.get("id") == user
        return user in await conversation_members(self._place_call, conversation)

    async def newcomers(self, origin: str, target: str, *, workspace: str = "") -> tuple[str, ...] | None:
        if workspace and workspace != self._identity.team_id:
            raise ValueError(f"workspace {workspace} is not served by this Slack connection")
        if not self._enforce_audience:
            return ()
        source = await self._place_audience(origin)
        reached = await self._place_audience(target)
        return () if write_permitted(source, reached) else None if reached.external else audience_newcomers(reached, source)

    async def open_direct_message(self, user: str, *, workspace: str = "") -> str:
        if workspace and workspace != self._identity.team_id:
            raise ValueError(f"workspace {workspace} is not served by this Slack connection")
        response = await self._place_call("conversations_open", users=user)
        channel = response.get("channel")
        room = str(channel.get("id") or "") if isinstance(channel, Mapping) else ""
        if not room:
            raise RuntimeError("conversations.open returned no channel id")
        return room

    def mention(self, user: str) -> str:
        return f"<@{user}>"
