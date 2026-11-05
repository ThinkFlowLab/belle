"""Slack event admission, addressing, and bounded queue decisions."""

import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from belle_agent.admission.policy import selected_message_trigger
from belle_agent.admission.state import AdmissionState as _AdmissionState
from belle_agent.platforms.slack.actors import SLACK_USERS
from belle_agent.platforms.slack.config import (
    APP_MESSAGES_ALL,
    APP_MESSAGES_LISTED,
    APP_MESSAGES_NONE,
    KEY_APP_MESSAGES_FROM,
)
from belle_agent.platforms.slack.events import event_actor_id, event_anchor, event_chat_id, event_family
from belle_agent.platforms.slack.file_transfer import event_entries

# These subtypes contain new user content. Edits and unfurls must not restart a turn.
_USER_CONTENT_SUBTYPES = frozenset({"file_share", "thread_broadcast"})


def message_route(
    triggers: Collection[str], event: Mapping[str, Any], text: str, *,
    bot_user_id: str, leading_mention: bool, mentions_bot_id: bool,
    ingesting: bool,
) -> tuple[str, str]:
    """Return a channel trigger or the reason an inbound message is ignored."""
    if not triggers:
        return "", "ingested:second-brain" if ingesting else "ignored:no-trigger-configured-for-channel"
    if leading_mention:
        return "", "ignored:leading-bot-mention-handled-as-app_mention"
    trigger = selected_message_trigger(
        triggers, text, reply_to_bot=is_reply_to_bot(event, bot_user_id),
        has_file=bool(event_entries(event, "files")),
    )
    if trigger:
        return trigger, ""
    if ingesting:
        return "", "ingested:second-brain"
    return "", "ignored:bot-id-mention-not-a-trigger" if mentions_bot_id else "ignored:no-trigger-matched"


def event_gate(event: Mapping[str, Any], bot_user_id: str) -> tuple[str, str, str]:
    """Return actor, conversation, and any reason to refuse routing."""
    actor_id = event_actor_id(event)
    if not actor_id:
        return "", "", "dropped:no-actor-on-event"
    if bot_user_id and actor_id == bot_user_id:
        return actor_id, "", "ignored:done-by-this-bot"
    if event.get("bot_id") or event.get("bot_profile"):
        return actor_id, "", "ignored:done-by-an-app"
    chat_id = event_chat_id(event)
    if not chat_id:
        return actor_id, "", "dropped:no-conversation-on-event"
    return actor_id, chat_id, ""


def is_reply_to_bot(event: Mapping[str, Any], bot_user_id: str) -> bool:
    """Only a reply inside a thread can address the bot through its parent."""
    thread_ts = str(event.get("thread_ts") or "").strip()
    if not thread_ts or thread_ts == str(event.get("ts") or "").strip():
        return False
    return bool(
        bot_user_id
        and str(event.get("parent_user_id") or "").strip() == bot_user_id
    )


def is_own_app_post(event: Mapping[str, Any], bot_id: str, bot_user_id: str) -> bool:
    event_bot_id = str(event.get("bot_id") or "").strip()
    if bot_id and event_bot_id == bot_id:
        return True
    user_id = str(event.get("user") or "").strip()
    return bool(bot_user_id and user_id == bot_user_id)


def non_user_content_outcome(
    event: Mapping[str, Any], *, mode: str, app_ids: Collection[str],
    bot_id: str, bot_user_id: str,
) -> str | None:
    """Explain why an event cannot start a user-message turn."""
    if str(event.get("user") or "").strip() in SLACK_USERS:
        # A notice from Slack in a direct message wants no answer, and Slack refuses one in its own
        # (restricted_action_read_only_channel), so a turn there would only fail.
        if str(event.get("channel_type") or "") == "im":
            return "ignored:direct-message-from-slack"
        # Elsewhere Slack's users count as an app with no app_id: only `all` admits them.
        if mode != APP_MESSAGES_ALL:
            return "ignored:posted-by-slack"
    if event.get("bot_id") or event.get("bot_profile"):
        if mode == APP_MESSAGES_NONE:
            return "ignored:posted-by-an-app"
        if is_own_app_post(event, bot_id, bot_user_id):
            return "ignored:posted-by-this-bot"
        if not (bot_id or bot_user_id):
            return "ignored:app-post-with-this-bot-unidentified"
        if mode == APP_MESSAGES_LISTED:
            app_id = str(event.get("app_id") or "").strip()
            if not app_id:
                return "ignored:posted-by-an-app-with-no-app_id"
            if app_id not in app_ids:
                return f"ignored:app-not-in-{KEY_APP_MESSAGES_FROM} ({app_id})"
        if not str(event.get("user") or "").strip():
            return "ignored:app-posted-with-no-user"
    user_id = str(event.get("user") or "").strip()
    if bot_user_id and user_id == bot_user_id:
        return "ignored:posted-by-this-bot"
    subtype = str(event.get("subtype") or "").strip()
    if subtype and subtype not in _USER_CONTENT_SUBTYPES:
        return f"ignored:subtype-not-user-content ({subtype})"
    return None


def app_messages_note(
    event: Mapping[str, Any], *, mode: str, app_ids: Collection[str]
) -> str:
    """Log the app-message rule without recording message content."""
    if mode == APP_MESSAGES_NONE or not (
        event.get("bot_id") or event.get("bot_profile")
    ):
        return ""
    app_id = str(event.get("app_id") or "").strip()
    if mode == APP_MESSAGES_LISTED and app_id not in app_ids:
        return ""
    return f" app_messages={mode} app_id={app_id or '-'}"


def event_identity(event: Mapping[str, Any], body: Mapping[str, Any]) -> str:
    """Identify an inbound event without logging its message text."""
    family = event_family(event)
    anchor_ts = event_anchor(event, family=family)[0] if family else ""
    return (
        f"channel={event.get('channel') or event_chat_id(event) or '-'} "
        f"channel_type={event.get('channel_type') or '-'} "
        f"ts={event.get('ts') or anchor_ts or '-'} "
        f"subtype={event.get('subtype') or '-'} "
        f"user={event.get('user') or '-'} "
        f"event_id={body.get('event_id') or event.get('client_msg_id') or '-'}"
    )


def click_identity(body: Any, action: Any) -> str:
    """Identify a click without logging answer content."""
    body = body if isinstance(body, Mapping) else {}
    action = action if isinstance(action, Mapping) else {}
    user = body.get("user")
    channel = body.get("channel")
    container = body.get("container")
    channel_id = channel.get("id") if isinstance(channel, Mapping) else ""
    user_id = user.get("id") if isinstance(user, Mapping) else ""
    message_ts = container.get("message_ts") if isinstance(container, Mapping) else ""
    return (
        f"action_id={action.get('action_id') or '-'} "
        f"channel={channel_id or '-'} user={user_id or '-'} "
        f"message_ts={message_ts or '-'} trigger_id={body.get('trigger_id') or '-'}"
    )


@dataclass
class PendingEvent:
    event: dict[str, Any]
    body: dict[str, Any]
    family: str
    chat_id: str
    chat_type: str
    actor_id: str
    record: dict[str, Any]
    session_id: str
    event_id: str
    anchor_ts: str
    arrival_seq: int
    queued_at: float = field(default_factory=time.monotonic)
    loss_logged: bool = False


@dataclass(frozen=True)
class SlackMessageOrigin:
    """Coordinates of an inbound message, its reply, and its status thread."""

    channel_id: str = ""
    message_ts: str = ""
    # A top-level DM reply has no thread target; status still uses root_thread_ts.
    reply_thread_ts: str = ""
    root_thread_ts: str = ""
    is_dm: bool = False

    @property
    def status_moves_the_reader(self) -> bool:
        # A top-level DM reply must not open an otherwise empty status thread.
        return self.is_dm and not self.reply_thread_ts


@dataclass
class SlackTurnInitiator:
    """Request and Slack message facts retained until a turn ends."""

    user_id: str
    request_id: str
    # A group DM is not an `im`; chat_type also survives a later button click.
    is_dm: bool
    chat_type: str
    model_name: str = ""
    channel_id: str = ""
    message_ts: str = ""
    reply_thread_ts: str = ""
    root_thread_ts: str = ""
    # Keep dispatch-time delivery facts: a resumed request has only click metadata.
    silence_is_honoured: bool = False
    addressed: bool = False
    post_as_root: bool = False
    trigger: str = ""
    started_at: float = field(default_factory=time.monotonic)
    preview_text: str = ""

    @property
    def origin(self) -> SlackMessageOrigin:
        return _message_origin(self)


@dataclass
class SlackQueuedMessage:

    request: Any
    session_id: str
    user_id: str
    is_dm: bool
    chat_type: str
    team_id: str = ""
    channel_id: str = ""
    message_ts: str = ""
    reply_thread_ts: str = ""
    root_thread_ts: str = ""
    queued_at: float = field(default_factory=time.monotonic)
    preview_text: str = ""

    @property
    def origin(self) -> SlackMessageOrigin:
        return _message_origin(self)


def _message_origin(message: SlackTurnInitiator | SlackQueuedMessage) -> SlackMessageOrigin:
    return SlackMessageOrigin(
        channel_id=message.channel_id,
        message_ts=message.message_ts,
        reply_thread_ts=message.reply_thread_ts,
        root_thread_ts=message.root_thread_ts,
        is_dm=message.is_dm,
    )


@dataclass
class AdmissionState(_AdmissionState):
    """Own waiting messages and event admission state for one Slack connector."""

    pending_events: dict[str, list[PendingEvent]] = field(default_factory=dict)
    admitting_events: dict[str, dict[int, PendingEvent]] = field(default_factory=dict)
    turn_initiators: dict[str, SlackTurnInitiator] = field(default_factory=dict)
    label: str = "Slack"
