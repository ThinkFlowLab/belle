"""Identity learned from one Slack workspace connection."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent.platforms.slack.events import event_chat_id
from belle_agent.platforms.slack.file_transfer import https_host

SLACK_ACTION_TOKEN_KEY = "slack_action_token"

def slack_action_token(
    event: Mapping[str, Any], body: Mapping[str, Any]
) -> str:
    """Return the event-scoped token, using the envelope as a fallback."""
    for source in (event, body):
        if not isinstance(source, Mapping):
            continue
        value = str(source.get("action_token") or "").strip()
        if value:
            return value
    return ""


def add_slack_action_token(
    metadata: dict[str, Any], event: Mapping[str, Any], body: Mapping[str, Any]
) -> None:
    if token := slack_action_token(event, body):
        metadata[SLACK_ACTION_TOKEN_KEY] = token


def message_envelope_ids(
    event: Mapping[str, Any], body: Mapping[str, Any]
) -> tuple[str, str, str, str, str]:
    user, channel, ts, thread_ts = (
        str(event.get(key) or "").strip()
        for key in ("user", "channel", "ts", "thread_ts")
    )
    event_id = str(body.get("event_id") or event.get("client_msg_id") or event.get("ts") or "").strip()
    return user, channel, ts, thread_ts, event_id


CHAT_TYPE_IM = "im"


def event_chat_type(event: Mapping[str, Any] | None) -> str:
    """Read the chat type stated by a Slack event."""
    return str(event.get("channel_type") or "").strip() if isinstance(event, Mapping) else ""


def chat_type_from_chat_id(chat_id: str) -> str:
    """Infer a DM from its ID; channel and group IDs are ambiguous."""
    return CHAT_TYPE_IM if str(chat_id or "").startswith("D") else ""


def learn_chat_type(chat_types: dict[str, str], event: Mapping[str, Any] | None, *, known: Collection[str]) -> None:
    """Record the latest valid conversation type an event states, including an ignored event's."""
    if not isinstance(event, Mapping):
        return
    chat_type = event_chat_type(event)
    if chat_type not in known:
        return
    chat_id = event_chat_id(event)
    if chat_id:
        chat_types[chat_id] = chat_type


def conversation_chat_type(
    chat_types: Mapping[str, str], chat_id: str, event: Mapping[str, Any] | None = None, *, known: Collection[str]
) -> str:
    """Return Slack's known conversation type, or an empty string.

    Message payloads state it. Other events read what an earlier message taught.
    """
    stated = event_chat_type(event)
    if stated in known:
        return stated
    return chat_types.get(chat_id, "") or chat_type_from_chat_id(chat_id)


def slack_team_id_from_session(session_id: Any) -> str:
    """Read the team from a Slack session ID."""
    raw = str(session_id or "").strip()
    if not raw.startswith("slack_"):
        return ""
    parts = raw.split("_", 2)
    return parts[1].strip() if len(parts) == 3 else ""


def event_team_id(body: Mapping[str, Any], event: Mapping[str, Any] | None = None) -> str:
    """Read the workspace ID from an event or interaction."""
    team = body.get("team")
    return str(
        (team.get("id") if isinstance(team, Mapping) else "")
        or body.get("team_id")
        or (event.get("team") if isinstance(event, Mapping) else "")
        or ""
    ).strip()


def click_channel_id(body: Mapping[str, Any]) -> str:
    """Read the conversation ID from a click payload."""
    channel = body.get("channel")
    container = body.get("container")
    container = container if isinstance(container, Mapping) else {}
    return str(
        (channel.get("id") if isinstance(channel, Mapping) else "")
        or container.get("channel_id")
        or ""
    ).strip()


def click_target(body: Mapping[str, Any]) -> tuple[str, str]:
    """Read the conversation and message IDs from a click payload."""
    message = body.get("message")
    message = message if isinstance(message, Mapping) else {}
    container = body.get("container")
    container = container if isinstance(container, Mapping) else {}
    return click_channel_id(body), str(message.get("ts") or container.get("message_ts") or "").strip()


@dataclass
class SlackWorkspaceIdentity:
    bot_user_id: str = ""
    bot_id: str = ""
    team_id: str = ""
    host: str = ""

    def learn_auth(self, response: Mapping[str, Any]) -> None:
        """Read the identity returned by Slack's auth.test endpoint."""
        self.bot_user_id = str(response.get("user_id") or "").strip()
        self.bot_id = str(response.get("bot_id") or "").strip()
        self.team_id = str(response.get("team_id") or "").strip()
        self.host = https_host(response.get("url"))

    def clear(self) -> None:
        self.bot_user_id = self.bot_id = self.team_id = self.host = ""

    def remember_authorization(self, body: Mapping[str, Any]) -> bool:
        """Use an event authorization if auth.test supplied no bot user ID."""
        if self.bot_user_id:
            return False
        authorizations = body.get("authorizations")
        if not isinstance(authorizations, list):
            return False
        for authorization in authorizations:
            if not isinstance(authorization, dict) or not authorization.get("is_bot"):
                continue
            user_id = str(authorization.get("user_id") or "").strip()
            if user_id:
                self.bot_user_id = user_id
                return True
        return False

    def claims_team(self, team_id: str) -> bool:
        return bool(self.team_id) and team_id == self.team_id

    def label(self, app_id: str) -> str:
        """Name this connection in logs when an ID is available."""
        block = app_id.strip()
        if block and self.team_id:
            return f" [{block}, {self.team_id}]"
        return f" [{block or self.team_id}]" if (block or self.team_id) else ""

    def mentions_bot_id(self, text: str) -> bool:
        """Detect a bot-ID token, which Slack does not treat as a mention."""
        return bool(self.bot_id) and f"<@{self.bot_id}>".casefold() in text.casefold()

    def leading_mention_pattern(self) -> re.Pattern[str] | None:
        if not self.bot_user_id:
            return None
        return re.compile(rf"^\s*<@{re.escape(self.bot_user_id)}>\s*", re.IGNORECASE)

    def has_leading_mention(self, text: str) -> bool:
        pattern = self.leading_mention_pattern()
        return pattern is not None and pattern.match(text) is not None

    def strip_leading_mention(self, text: str) -> str:
        pattern = self.leading_mention_pattern()
        return pattern.sub("", text, count=1) if pattern is not None else text
