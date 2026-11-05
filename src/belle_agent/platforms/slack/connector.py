"""Slack as a platform of the conversation host, over Socket Mode.

The connector says what arrived and performs what the host decides. It is
assembled from the parts of this package every Slack connection uses: the
socket, the workspace identity, the admission helpers, the reply delivery,
the acknowledgement marks, the question presenter and the toolkits. It
decides nothing about who is answered or how.

The two tokens are read from the environment and from nowhere else. Neither
is accepted in the configuration file, and neither is logged. They must
belong to a Slack app of their own: an app-level token is shared across
connections, so a second process on another deployment's token takes part
of that deployment's events away from it.
"""

import base64
import functools
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from belle_agent.attachments import fetch_message_files
from belle_agent.backend import APPROVAL, QUESTION, Answer, Question
from belle_agent.click_notices import ANSWER_REFUSED_NOTICE, APPROVAL_LABELS, WITHDRAWN_QUESTION_NOTE
from belle_agent.config import BelleConfigError, platform_section
from belle_agent.platforms.base import (
    MARK_DONE,
    MARK_DROPPED,
    MARK_FAILED,
    MARK_QUEUED,
    MARK_RECEIVED,
    MARK_STARTED,
    MARK_STEERED,
    MARK_STOPPED,
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_NONE,
    TRIGGER_REPLY,
    EventKind,
    InboundAnswer,
    InboundEvent,
    InboundMessage,
    OnInbound,
    PlatformAffordances,
    PlatformUnreachable,
)
from belle_agent.platforms.slack import TOKEN_VARIABLES
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack import questions as slack_questions
from belle_agent.platforms.slack.actors import SLACK_USERS, ActorUnknown, SlackActors
from belle_agent.platforms.slack.admission import (
    SlackMessageOrigin,
    SlackTurnInitiator,
    click_identity,
    event_gate,
    event_identity,
    is_reply_to_bot,
    non_user_content_outcome,
)
from belle_agent.platforms.slack.bookmarks import SlackBookmarkToolkit
from belle_agent.platforms.slack.canvases import SlackCanvasToolkit
from belle_agent.platforms.slack.channel_config import DEFAULT_THINKING_STATUS, SlackChannelConfig
from belle_agent.platforms.slack.config import (
    ACK_MODE_OFF,
    normalize_app_messages,
    normalize_reply_in_thread,
    opens_a_thread,
    resolve_acknowledge_mode,
    resolve_app_messages,
    resolve_app_messages_from,
    resolve_blockkit_tables_mode,
    resolve_command_name,
    resolve_reaction_emoji,
    resolve_render_tables,
    resolve_reply_in_thread,
    resolve_seconds,
    resolve_session_surface,
    resolve_streaming_enabled,
)
from belle_agent.platforms.slack.connector_commands import SlackConnectorCommands
from belle_agent.platforms.slack.connector_home import SlackConnectorHome
from belle_agent.platforms.slack.decision_questions import SlackDecisionObserver
from belle_agent.platforms.slack.delivery import SlackDeliveryError, slack_error_code, slack_transient
from belle_agent.platforms.slack.directory import SlackDirectoryToolkit
from belle_agent.platforms.slack.events import (
    EVENT_BUFFER_LIMITS,
    EVENT_FAMILY_APP_HOME,
    EVENT_HELD_FAMILIES,
    EVENT_TURN_FAMILIES,
    EVENT_TYPE_FAMILIES,
    event_anchor,
    event_family,
    family_anchors_on_a_message,
    render_event,
)
from belle_agent.platforms.slack.file_transfer import (
    ATTACHMENT_LABEL,
    FILE_REASON_DOWNLOAD_FAILED,
    SLACK_FILE_HOST,
    describe_event_attachments,
    event_entries,
    fetch_attachment_bytes,
    resolve_attachment,
    route_guarded,
    slack_file_name,
)
from belle_agent.platforms.slack.files import SlackFileToolkit, SlackInlineHistoryToolkit
from belle_agent.platforms.slack.history import SlackWorkspaceClients
from belle_agent.platforms.slack.history_policy import (
    KEY_BOT_TOKEN,
    KEY_WORKSPACES,
    audience_metadata,
    resolve_enforce_audience,
)
from belle_agent.platforms.slack.home_tab import SlackHomeTabToolkit
from belle_agent.platforms.slack.identity import (
    CHAT_TYPE_IM,
    SLACK_ACTION_TOKEN_KEY,
    SlackWorkspaceIdentity,
    click_target,
    conversation_chat_type,
    event_team_id,
    learn_chat_type,
    message_envelope_ids,
    slack_action_token,
)
from belle_agent.platforms.slack.lists import SlackListToolkit
from belle_agent.platforms.slack.pins import SlackPinToolkit
from belle_agent.platforms.slack.places import SlackPlaces
from belle_agent.platforms.slack.post import POST_MESSAGE, SlackPostToolkit
from belle_agent.platforms.slack.reactions import ReactionMarks, SlackReactionToolkit
from belle_agent.platforms.slack.rendering import SlackRenderer
from belle_agent.platforms.slack.reply_delivery import SlackReplyDelivery
from belle_agent.platforms.slack.scheduled import SlackScheduledToolkit
from belle_agent.platforms.slack.scope_capabilities import SLACK_CHAT_TYPES, slack_capabilities
from belle_agent.platforms.slack.scope_policy import resolve_bot_name, slack_granted_scopes, slack_required_scopes
from belle_agent.platforms.slack.scoped_config import CHAT_TYPE_PUBLIC_CHANNEL
from belle_agent.platforms.slack.search import SlackSearchToolkit
from belle_agent.platforms.slack.socket_mode import SlackAppTokenRefused, SlackSocket
from belle_agent.platforms.slack.text import MAX_SLACK_TEXT_LENGTH
from belle_agent.platforms.slack.thread_status import SlackThreadStatus
from belle_agent.platforms.slack.tool_context import slack_request_metadata
from belle_agent.platforms.slack.turn_surfaces import SlackTurnSurfaces
from belle_agent.platforms.slack.turn_text import event_turn_text, inbound_events_block, location_label
from belle_agent.tools.host import APPROVE, REJECT

logger = logging.getLogger(__name__)

NAME = "slack"

#: The bot token of the Slack app this deployment owns, as Slack issues it (``xoxb-``).
BOT_TOKEN_ENV = TOKEN_VARIABLES["bot_token"]
#: The app-level token of the same app, which opens its Socket Mode connection (``xapp-``).
APP_TOKEN_ENV = TOKEN_VARIABLES["app_token"]

_SECRETS = {"bot_token": (BOT_TOKEN_ENV, "xoxb-"), "app_token": (APP_TOKEN_ENV, "xapp-")}

# One deadline covers every file of one message, as on the JiuwenSwarm channel.
FILE_BUDGET_SECONDS = 120.0
# Questions posted and still waiting for a click. A turn that is stopped never comes back to clear its entry.
MAX_SHOWN_QUESTIONS = 32
# Search needs the token Slack attaches to the event that started the turn. Kept for the newest messages only.
MAX_ACTION_TOKENS = 256

#: Settings of the JiuwenSwarm Slack channel that this connector does not act on. Each is named at start
#: where it is written, so a configuration copied from that channel does not look as if it took effect.
UNREAD_SETTINGS: frozenset[str] = frozenset(
    {
        "optional_reply_mode",
        "group_chat_mode",
        "session",
        "default_channel_id",
        "group_digital_avatar",
        "my_user_id",
        "principal_name",
        "enable_memory",
        "sdk_log_level",
    }
)

#: The ``uri`` of a file the message carries by reference, until ``fetch_files`` loads it.
_FILE_SCHEME = "slack-file:"
_FILE_INFO = "slack_file"
_ANY_EVENT_TYPE = re.compile(r".*")
_APPROVAL_STYLES = {APPROVE: "primary", REJECT: "danger"}

_PRESENTER = slack_questions.QuestionPresenter(
    action_id_prefix=slack_questions.QUESTION_ACTION_ID_PREFIX,
    option_style=lambda intent, value: _APPROVAL_STYLES.get(intent, ""),
    logger=logger,
)


@dataclass
class _Shown:
    """One question on screen, with what its buttons and inputs mean."""

    question: Question
    conversation: str
    labels: list[str]
    values: list[str]
    inputs: list[Any] = field(default_factory=list)


def _channel_config(slack: Mapping[str, Any]) -> SlackChannelConfig:
    """Read the settings this connector acts on, through the parsers the JiuwenSwarm channel reads them with.

    The JiuwenSwarm gateway builds the whole of ``SlackChannelConfig`` in one call that its tests pin
    keyword by keyword, so that call is not shared. A key left out here keeps the default of the type.
    """
    defaults = SlackChannelConfig()

    def words(key: str) -> str:
        return str(slack.get(key, getattr(defaults, key)) or "").strip()

    def emoji(key: str) -> str:
        return resolve_reaction_emoji(slack.get(key), getattr(defaults, key))

    def seconds(key: str) -> float:
        return resolve_seconds(slack, key, getattr(defaults, key))

    return SlackChannelConfig(
        enabled=True,
        file_download_max_bytes=slack.get("file_download_max_bytes", defaults.file_download_max_bytes),
        app_messages=resolve_app_messages(slack),
        app_messages_from=resolve_app_messages_from(slack),
        reply_in_thread=resolve_reply_in_thread(slack),
        acknowledge_mode=resolve_acknowledge_mode(slack),
        acknowledgement_text=words("acknowledgement_text") or defaults.acknowledgement_text,
        thinking_status=str(slack.get("thinking_status", DEFAULT_THINKING_STATUS) or "").strip(),
        queued_status=words("queued_status") or defaults.queued_status,
        steered_status=words("steered_status") or defaults.steered_status,
        session_surface=resolve_session_surface(slack),
        acknowledgement_emoji=emoji("acknowledgement_emoji"),
        rejected_emoji=emoji("rejected_emoji"),
        queued_emoji=emoji("queued_emoji"),
        completed_emoji=emoji("completed_emoji"),
        failed_emoji=emoji("failed_emoji"),
        stopped_emoji=emoji("stopped_emoji"),
        brain_react_pending=emoji("brain_react_pending"),
        brain_react_success=emoji("brain_react_success"),
        brain_react_declined=emoji("brain_react_declined"),
        brain_react_failure=emoji("brain_react_failure"),
        blockkit_tables=resolve_blockkit_tables_mode(slack),
        render_tables=resolve_render_tables(slack),
        bot_name=words("bot_name"),
        enable_streaming=resolve_streaming_enabled(slack.get("enable_streaming")),
        activity_card=bool(slack.get("activity_card", defaults.activity_card)),
        activity_card_delay_seconds=seconds("activity_card_delay_seconds"),
        activity_card_min_edit_seconds=seconds("activity_card_min_edit_seconds"),
        command_name=resolve_command_name(slack),
        home_tab_welcome=bool(slack.get("home_tab_welcome", False)),
    )


#: Where the answer of an event turn about a message goes on this host, which posts only where ``post_message`` names.
EVENT_REPLY_BY_TOOL = (
    f" If you do answer, pass the thread_ts named above as reply_to_message_id to {POST_MESSAGE}. A post that names"
    " no thread lands at the top level of this channel, where everyone in it sees it."
)

#: Slack's event families as the port describes a kind, from the tables the JiuwenSwarm channel reads.
_EVENT_KINDS: Mapping[str, EventKind] = {
    family: EventKind(
        held=held,
        turns=family in EVENT_TURN_FAMILIES,
        anchored=family_anchors_on_a_message(family),
        waits=family in EVENT_HELD_FAMILIES,
    )
    for family, held in EVENT_BUFFER_LIMITS.items()
}


class SlackPlatform(
    SlackConnectorCommands, SlackConnectorHome, SlackTurnSurfaces, SlackThreadStatus, SlackReplyDelivery, SlackDecisionObserver,
    SlackPlaces,
):
    """One Slack workspace, connected over Socket Mode."""

    name = NAME
    posting_tool = POST_MESSAGE
    # The shared mixins log under their own names. Their lines are tagged as this connector's.
    _log_tag = "[slack]"
    capabilities = slack_capabilities()
    #: Slack calls a public channel ``channel``. A private one is a ``group``.
    public_chat_types: frozenset[str] = frozenset({CHAT_TYPE_PUBLIC_CHANNEL})
    direct_chat_types = frozenset({CHAT_TYPE_IM})
    event_kinds = _EVENT_KINDS
    ephemeral_posts = True
    affordances = PlatformAffordances(
        questions="present", silent_card=None, reply_parts_in_thread=None,
    )

    def command_prefix(self, workspace: str) -> str:
        return f"/{self.config.command_name}"

    def __init__(
        self,
        settings: Mapping[str, Any],
        *,
        bot_token: str,
        app_token: str,
        label: str = NAME,
        bot_token_source: str = BOT_TOKEN_ENV,
        app_token_source: str = APP_TOKEN_ENV,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        #: What names this connection where several are held: the entry of ``slack.workspaces`` it was built from.
        self.label = label
        self._bot_token_source = bot_token_source
        self._app_token_source = app_token_source
        # What the toolkits read as the installation's settings. The token is held in memory only.
        self._settings = {**settings, KEY_BOT_TOKEN: bot_token}
        self._settings.pop("app_token", None)
        self.config = _channel_config(settings)
        self.renderer = SlackRenderer(self._blocks_and_kind_for)
        self._enforce_audience = resolve_enforce_audience(settings)
        self._search_enabled = bool(settings.get("search_enabled", True))
        # Off unless the operator offers it, as ``send_file_allowed`` is for Slack on the JiuwenSwarm channel.
        self._send_file_allowed = bool(settings.get("send_file_allowed", False))
        self._identity = SlackWorkspaceIdentity()
        #: The bot users of the other apps of this Belle. ``SlackWorkspaces`` gives them; they are bots here.
        self.other_apps: Callable[[], frozenset[str]] = frozenset
        self._actors = SlackActors(logger)
        self._reaction_marks = ReactionMarks(logger, tag=self._log_tag)
        # Probe status API availability per installation. Refusals can change it.
        self._agents_status_allowed = True
        self._socket: SlackSocket | None = None
        self._client: Any = None
        self._on_inbound: OnInbound | None = None
        # Message payloads state a conversation's kind. A mention and a click read it from here.
        self._chat_types: dict[str, str] = {}
        self._questions: dict[str, _Shown] = {}
        #: The message whose turn runs in each thread, for Slack's own stop button, which names a thread.
        self._running: dict[tuple[str, str], InboundMessage] = {}
        self._action_tokens: dict[tuple[str, str], str] = {}
        #: What the decision gate is told of the newest messages. Kept only where the host has a gate.
        self._decision_inputs: dict[tuple[str, str], dict[str, Any]] = {}
        self._workspaces = SlackWorkspaceClients(
            client_factory=lambda _token: self._client, config_loader=lambda: self._settings
        )
        self._init_turn_surfaces()

    # ── connection ───────────────────────────────────────────────────────

    def _register(self, app: Any) -> None:
        app.event("app_mention")(self._on_app_mention)
        app.event("message")(self._on_message)
        app.event("agent_session_stopped")(self._on_session_stopped)
        for event_type in sorted(EVENT_TYPE_FAMILIES):
            app.event(event_type)(self._on_event)
        # Bolt reports an event no listener takes. Slack sends whatever the app manifest subscribes to.
        app.event(_ANY_EVENT_TYPE)(self._on_unclaimed_event)
        app.action(slack_questions.QUESTION_ACTION_ID_RE)(self._on_question_action)
        self._register_turn_surfaces(app)
        self._register_commands(app)

    async def start(self, on_inbound: OnInbound) -> None:
        """Check the tokens and the scopes, then open the socket, or raise ``BelleConfigError`` with the cause.

        Raise ``PlatformUnreachable`` where Slack did not answer. Nothing is open then, so ``start`` can be called
        again.
        """
        try:
            await self._connect(on_inbound)
        except PlatformUnreachable:
            await self.stop()
            raise

    async def _connect(self, on_inbound: OnInbound) -> None:
        socket = SlackSocket(self._bot_token, self._app_token, self._register, logger=logging.getLogger("slack_bolt"))
        self._socket, self._client = socket, socket.client
        try:
            response = await self._client.auth_test()
        except Exception as exc:  # noqa: BLE001 - SDK error types vary, and none may carry the token out
            reason = slack_error_code(exc) or type(exc).__name__
            if slack_transient(exc):
                raise PlatformUnreachable(f"Slack did not answer auth.test: {reason}") from None
            raise BelleConfigError(
                f"Slack did not accept the bot token of {self._bot_token_source}: auth.test answered {reason}"
            ) from None
        self._identity.learn_auth(response)
        if not (self._identity.bot_user_id and self._identity.team_id):
            raise BelleConfigError("Slack auth.test named no bot user or no workspace, so no message can be told from this app's own")
        granted = slack_granted_scopes(getattr(response, "headers", None))
        missing = sorted(slack_required_scopes() - granted) if granted is not None else []
        if missing:
            raise BelleConfigError(
                f"the Slack app lacks {', '.join(missing)}, so it cannot receive or answer messages."
                " Reinstall it with these scopes"
            )
        self._granted_scopes = granted
        self._on_inbound = on_inbound
        try:
            await socket.connect()
        except SlackAppTokenRefused as refused:
            raise BelleConfigError(
                f"Slack did not accept the app token of {self._app_token_source}:"
                f" apps.connections.open answered {refused.code}"
            ) from None
        logger.info("slack connected%s", self._identity.label(self.config.app_id))

    @property
    def team_id(self) -> str:
        """The workspace this connection serves, as ``auth.test`` named it. Empty until started."""
        return self._identity.team_id

    @property
    def bot_user_id(self) -> str:
        """This app's bot user, as ``auth.test`` named it. Empty until started."""
        return self._identity.bot_user_id

    async def stop(self) -> None:
        self._on_inbound = None
        socket, self._socket, self._client = self._socket, None, None
        self._questions.clear()
        self._running.clear()
        self._action_tokens.clear()
        self._clear_turn_surfaces()
        self._decision_inputs.clear()
        self._identity.clear()
        if socket is not None:
            try:
                await socket.close()
            except Exception as exc:  # noqa: BLE001 - a socket that will not close does not stop the host stopping
                logger.warning("slack socket did not close cleanly: %s", slack_error_code(exc) or type(exc).__name__)

    # ── inbound ──────────────────────────────────────────────────────────

    async def _routed(self, route: Callable[..., Any], event: Any, body: Any, family: str) -> str:
        """Run one route and log what became of the event, without its text."""
        outcome = "handler-raised"
        try:
            if self._on_inbound is None:
                outcome = "dropped:platform-not-started"
            elif not isinstance(event, dict) or not isinstance(body, dict):
                outcome = "dropped:malformed-payload"
            else:
                self._identity.remember_authorization(body)
                outcome = await route(event, body)
        except Exception:
            logger.exception("slack inbound %s raised: %s", family, event_identity(event or {}, body or {}))
        else:
            logger.info("slack inbound %s: %s outcome=%s", family, event_identity(event or {}, body or {}), outcome)
        return outcome

    async def _on_message(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        return await self._routed(self._route_message, event, body, "message")

    async def _on_app_mention(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        return await self._routed(self._route_app_mention, event, body, "app_mention")

    async def _on_session_stopped(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        return await self._routed(self._route_session_stopped, event, body, "agent_session_stopped")

    async def _on_event(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        return await self._routed(self._route_event, event, body, str((event or {}).get("type") or "-"))

    async def _on_unclaimed_event(self, event: dict[str, Any], body: dict[str, Any]) -> None:
        """Take an event this connector does nothing with, so Bolt has a listener for it."""

    def _not_user_content(self, event: Mapping[str, Any]) -> str | None:
        return non_user_content_outcome(
            event,
            mode=normalize_app_messages(self.config.app_messages),
            app_ids=self.config.app_messages_from,
            bot_id=self._identity.bot_id,
            bot_user_id=self._identity.bot_user_id,
        )

    def _chat_type(self, chat_id: str, event: Mapping[str, Any] | None = None) -> str:
        return conversation_chat_type(self._chat_types, chat_id, event, known=SLACK_CHAT_TYPES)

    async def _route_message(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        assert self._on_inbound is not None
        learn_chat_type(self._chat_types, event, known=SLACK_CHAT_TYPES)
        if str(event.get("subtype") or "") == "message_changed":
            return await self._on_inbound(self._edit(event, body))
        outcome = self._not_user_content(event)
        if outcome is not None:
            return outcome
        text = str(event.get("text") or "").strip()
        if str(event.get("channel_type") or "") == "im":
            return await self._deliver(event, body, TRIGGER_DM, text)
        if self._identity.has_leading_mention(text):
            # Slack sends the same message as an app_mention, which is where it is taken.
            return "ignored:leading-bot-mention-handled-as-app_mention"
        trigger = TRIGGER_REPLY if is_reply_to_bot(event, self._identity.bot_user_id) else TRIGGER_NONE
        return await self._deliver(event, body, trigger, text)

    async def _route_app_mention(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        outcome = self._not_user_content(event)
        if outcome is not None:
            return outcome
        text = self._identity.strip_leading_mention(str(event.get("text") or "").strip()).strip()
        return await self._deliver(event, body, TRIGGER_MENTION, text)

    def _edit(self, event: Mapping[str, Any], body: Mapping[str, Any]) -> InboundMessage:
        """An edit of an earlier message. Slack nests the message as it now reads."""
        inner = event.get("message")
        inner = inner if isinstance(inner, Mapping) else {}
        channel = str(event.get("channel") or "").strip()
        return InboundMessage(
            platform=NAME,
            workspace=event_team_id(body, event),
            conversation=channel,
            message_id=str(inner.get("ts") or "").strip(),
            sender=str(inner.get("user") or "").strip(),
            text=str(inner.get("text") or ""),
            event_id=str(body.get("event_id") or "").strip(),
            thread=str(inner.get("thread_ts") or "").strip(),
            sender_is_bot=bool(inner.get("bot_id") or inner.get("bot_profile")),
            chat_type=self._chat_type(channel, event),
            edited=True,
        )

    async def _deliver(
        self, event: dict[str, Any], body: dict[str, Any], trigger: str, text: str, **requirements: Any
    ) -> str:
        """Hand one message to the host as the record the port defines. A command names what its turn requires."""
        assert self._on_inbound is not None
        user, channel, ts, thread_ts, event_id = message_envelope_ids(event, body)
        direct = trigger == TRIGGER_DM
        attachments = event_entries(event, "attachments")
        if attachments:
            text = describe_event_attachments(text, attachments)
        # Where the answer goes is this connector's setting. A reply in a thread stays in it.
        in_thread = bool(thread_ts) or opens_a_thread(
            normalize_reply_in_thread(self.config.reply_in_thread), is_direct_message=direct
        )
        # A private question asked from elsewhere, and the replies in its thread, are a conversation of their own.
        side = self._side_threads.get((channel, thread_ts or ts), "") if direct else ""
        token = slack_action_token(event, body)
        if token and self._search_enabled:
            while len(self._action_tokens) >= MAX_ACTION_TOKENS:
                del self._action_tokens[next(iter(self._action_tokens))]
            self._action_tokens[(channel, ts)] = token
        if getattr(self._host, "decides", False):
            while len(self._decision_inputs) >= MAX_ACTION_TOKENS:
                del self._decision_inputs[next(iter(self._decision_inputs))]
            # From the payload, as the JiuwenSwarm channel reads it: the text as written, a leading mention included.
            self._decision_inputs[(channel, ts)] = self._decision_input(
                event, body, channel_id=channel, message_ts=ts, thread_ts=thread_ts, user_id=user,
                chat_type=self._chat_type(channel, event), is_dm=direct, trigger=trigger,
                text=str(event.get("text") or text).strip(),
            )
        return await self._on_inbound(
            InboundMessage(
                platform=NAME,
                workspace=event_team_id(body, event),
                conversation=channel,
                message_id=ts,
                sender=user,
                text=text,
                event_id=event_id,
                thread=thread_ts,
                chat_type=self._chat_type(channel, event),
                trigger=trigger,
                # By reference: nothing is downloaded until the host says the message has a turn or is ingested.
                files=tuple(
                    {"name": slack_file_name(info), "uri": f"{_FILE_SCHEME}{info.get('id') or ''}", _FILE_INFO: info}
                    for info in event_entries(event, "files")
                ),
                reply_thread=(thread_ts or ts) if in_thread or side else "",
                # The ids the Slack tools take, so a turn can thread a post or name this message.
                envelope=location_label(ts, thread_ts),
                side=side,
                **requirements,
            )
        )

    async def _route_event(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        """Hand a reaction, a pin, a change of members or an App Home visit to the host, as the record the port defines."""
        assert self._on_inbound is not None
        family = event_family(event)
        if family is None:
            return "ignored:event-type-has-no-family"
        # The welcome is no event rule: it is made or refused first, whatever becomes of the visit as an event.
        welcome = await self._route_app_home_opened(event, body) if family == EVENT_FAMILY_APP_HOME else ""
        # What this app did itself is no news to it. Its own acknowledgement reaction would otherwise come back here.
        actor, chat, outcome = event_gate(event, self._identity.bot_user_id)
        if outcome:
            return welcome or outcome
        try:
            actor_is_bot = await self._actor_is_bot(actor, chat)
        except ActorUnknown as unknown:
            return welcome or unknown.outcome
        anchor, anchor_thread = event_anchor(event, family=family)
        outcome = await self._on_inbound(
            InboundEvent(
                platform=NAME,
                workspace=event_team_id(body, event),
                conversation=chat,
                kind=family,
                actor=actor,
                record=render_event(event, family=family),
                event_id=str(body.get("event_id") or event.get("event_ts") or "").strip(),
                # These payloads state no kind of conversation. It is what an earlier message taught, or unknown.
                chat_type=self._chat_type(chat, event),
                direct=chat.startswith("D"),
                actor_is_bot=actor_is_bot,
                anchor=anchor,
                anchor_thread=anchor_thread,
            )
        )
        # A visit kept as context says so. Any other visit is answered for by the welcome.
        return outcome if not welcome or outcome.startswith("buffered:") else welcome

    async def _actor_is_bot(self, actor: str, chat: str) -> bool:
        """Whether an app's bot did an event, which its payload does not say. Raise ``ActorUnknown`` where Slack does not."""
        if actor in self.other_apps() or actor in SLACK_USERS:
            return True
        if chat.startswith("D"):
            # A direct message holds this app and one person, or Slack, and what this app did stopped at the gate.
            return False
        return await self._actors.is_bot(self._client, actor, self._granted_scopes)

    def render_events(self, held: Mapping[str, Any] | None, started_by: InboundEvent | None = None) -> str:
        """Write events as the JiuwenSwarm channel does. The standing prompt and the reply rule reach a turn elsewhere."""
        kept = inbound_events_block(held)
        if started_by is None:
            return kept
        return event_turn_text(
            started_by.record,
            anchor_ts=started_by.anchor,
            anchor_thread_ts=started_by.anchor_thread,
            anchored=family_anchors_on_a_message(started_by.kind),
            buffered_events=kept,
            prompt="",
            silence_instruction="",
            destination=EVENT_REPLY_BY_TOOL if started_by.anchor else "",
        )

    async def _route_session_stopped(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        """Slack's own stop button, drawn off the thread status. It names a thread and the person who pressed it."""
        assert self._on_inbound is not None
        channel = str(event.get("channel") or "").strip()
        thread_ts = str(event.get("thread_ts") or "").strip()
        if not channel or not thread_ts:
            return "dropped:no-thread-on-event"
        running = self._running.get((channel, thread_ts))
        if running is None:
            # Slack drew the button off a status it still holds there. That status is what this takes down.
            origin = SlackMessageOrigin(channel_id=channel, reply_thread_ts=thread_ts, root_thread_ts=thread_ts)
            await self._clear_thinking_status(origin)
            return "ignored:no-turn-running"
        return await self._request_stop(
            running.workspace, channel, running.message_id, str(event.get("user") or "").strip()
        )

    # ── questions ────────────────────────────────────────────────────────

    async def ask(self, conversation: str, thread: str, question: Question, *, workspace: str = "") -> str:
        if self._client is None:
            raise SlackDeliveryError("Slack client is not connected", channel_id=conversation)
        approval = question.kind == APPROVAL
        presented = {
            "question": question.text,
            "options": [
                {
                    "label": APPROVAL_LABELS.get(option, option) if approval else option,
                    "value": option,
                    "intent": option if approval else "",
                }
                for option in question.options
            ],
        }
        inputs, options = _PRESENTER.answer_choices(
            presented, allow_text_input=question.kind == QUESTION and not question.options, request_id=question.id
        )
        blocks = (
            _PRESENTER.answer_blocks(
                request_id=question.id, source=question.kind, session_id="",
                question=presented, declared=inputs, options=options, dropped=[],
            )
            if inputs or options
            else None
        )
        if blocks is None:
            raise SlackDeliveryError(f"question {question.id} could not be rendered as Block Kit", channel_id=conversation)
        sent, message_ts, error = await self._post_text(
            channel_id=conversation,
            text=_PRESENTER.fallback_text(presented),
            thread_ts=thread,
            blocks=blocks,
            block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
            chunk_index=1,
            chunk_total=1,
        )
        if not sent:
            raise SlackDeliveryError(error, channel_id=conversation)
        while len(self._questions) >= MAX_SHOWN_QUESTIONS:
            del self._questions[next(iter(self._questions))]
        self._questions[question.id] = _Shown(
            question,
            conversation,
            labels=[option.label for option in options],
            values=[option.value for option in options],
            inputs=inputs,
        )
        return message_ts

    async def _on_question_action(self, ack: Any, body: dict[str, Any], action: dict[str, Any]) -> str:
        """Acknowledge a click on a question and hand the answer to the host, which decides who may give it."""
        outcome = "handler-raised"
        try:
            await ack()
            outcome = await self._route_question_action(body, action)
        except Exception:
            logger.exception("slack question click raised: %s", click_identity(body, action))
        else:
            logger.info("slack question click: %s outcome=%s", click_identity(body, action), outcome)
        return outcome

    async def _route_question_action(self, body: dict[str, Any], action: dict[str, Any]) -> str:
        if self._on_inbound is None:
            return "dropped:platform-not-started"
        if not isinstance(body, dict) or not isinstance(action, dict):
            return "dropped:malformed-payload"
        action_id = str(action.get("action_id") or "")
        if action_id.startswith(slack_questions.QUESTION_INPUT_ACTION_ID_PREFIX):
            # Input changes do not submit an answer.
            return "ignored:input-changed"
        decoded = slack_questions.decode_button_value(action, logger)
        if decoded is None:
            return "dropped:button-names-no-question"
        question_id = str(decoded.get("request_id") or "").strip()
        user = body.get("user")
        user_id = str((user.get("id") if isinstance(user, Mapping) else "") or "").strip()
        chat_id, _clicked_ts = click_target(body)

        # A question this process did not show is still put to the host, which knows what waits.
        shown = self._questions.get(question_id) or _Shown(Question(id=question_id, text=""), chat_id, [], [])
        if shown.inputs:
            values, label, problem = slack_questions.read_submitted_inputs(
                shown.inputs, body, request_id=question_id,
                action_id_prefix=slack_questions.QUESTION_INPUT_ACTION_ID_PREFIX, logger=logger,
            )
            if problem:
                # Keep the question open until all required inputs are present.
                await self._notify_clicker(
                    body, user_id, f"{slack_questions.INCOMPLETE_SUBMIT_NOTICE} {problem}", notice="incomplete submit"
                )
                return "refused:incomplete-submit"
        elif action_id == slack_questions.QUESTION_SUBMIT_ACTION_ID:
            return "ignored:submit-with-nothing-to-read"
        else:
            label, value = slack_questions.resolve_answer(shown.labels, shown.values, decoded, action)
            values = [value]

        approved = values[0] == APPROVE if shown.question.kind == APPROVAL else None
        outcome = await self._on_inbound(
            InboundAnswer(
                platform=NAME,
                workspace=event_team_id(body),
                conversation=chat_id,
                question_id=question_id,
                sender=user_id,
                answer=Answer(values=tuple(values), approved=approved),
            )
        )
        if outcome.startswith("answered"):
            self._questions.pop(question_id, None)
            await self._close_question_message(body, note=slack_questions.answered_note(label, user_id))
        elif outcome.startswith("refused:clicks."):
            # The question stays open for somebody who may answer it.
            await self._notify_clicker(body, user_id, ANSWER_REFUSED_NOTICE, notice="answer-refused")
        elif outcome.startswith("ignored:question-not-waiting"):
            # A stale click must not answer a question twice. Its buttons come off the message it was made on.
            if shown.conversation == chat_id:
                self._questions.pop(question_id, None)
            await self._close_question_message(body, note=WITHDRAWN_QUESTION_NOTE)
        return outcome

    # ── outbound ─────────────────────────────────────────────────────────

    async def post(
        self, conversation: str, thread: str, text: str, *, ephemeral_to: str | None = None, workspace: str = ""
    ) -> str:
        if self._client is None:
            raise SlackDeliveryError("Slack client is not connected", channel_id=conversation)
        if ephemeral_to:
            # Slack shows it to one person and returns nothing to edit it by.
            await self._post_ephemeral(conversation, ephemeral_to, text, notice="notice from the host")
            return ""
        chunks = self.renderer.parts(text, MAX_SLACK_TEXT_LENGTH)
        first = ""
        for index, chunk in enumerate(chunks):
            sent, message_ts, error = await self._post_text(
                channel_id=conversation, text=chunk.markdown, thread_ts=thread,
                chunk_index=index + 1, chunk_total=len(chunks), blocks=chunk.payload.blocks,
                block_kind=chunk.payload.block_kind,
            )
            if not sent:
                raise SlackDeliveryError(error, channel_id=conversation, chunks_sent=index, chunks_total=len(chunks))
            first = first or message_ts
        return first

    async def react(
        self, conversation: str, message_id: str, name: str, remove: bool = False, *, workspace: str = ""
    ) -> None:
        if self._client is None:
            raise SlackDeliveryError("Slack client is not connected", channel_id=conversation)
        method = self._client.reactions_remove if remove else self._client.reactions_add
        await method(channel=conversation, timestamp=message_id, name=ReactionMarks.name(name))

    def _origin(self, inbound: InboundMessage) -> SlackTurnInitiator:
        """The coordinates the acknowledgement marks and the thread status read."""
        return SlackTurnInitiator(
            user_id=inbound.sender,
            request_id=inbound.event_id,
            is_dm=inbound.trigger == TRIGGER_DM,
            chat_type=inbound.chat_type,
            channel_id=inbound.conversation,
            message_ts=inbound.message_id,
            reply_thread_ts=inbound.reply_thread or "",
            root_thread_ts=inbound.thread or inbound.message_id,
        )

    async def mark(self, inbound: InboundMessage, state: str) -> None:
        """Show what became of a message, the way the JiuwenSwarm channel does: a reaction and a thread status."""
        turn = self._origin(inbound)
        thread = (inbound.conversation, turn.root_thread_ts)
        await self._react_for_state(inbound.conversation, inbound.message_id, state)
        if state in (MARK_RECEIVED, MARK_QUEUED):
            await self._acknowledge_request(turn.origin, react=False)
            if state == MARK_QUEUED:
                await self._mark_queued(turn, react=False)
            else:
                self._running[thread] = inbound
        elif state == MARK_STEERED:
            await self._acknowledge_request(turn.origin, react=False)
            if self._acknowledge_mode() != ACK_MODE_OFF:
                await self._set_steered_status(turn.origin)
        elif state == MARK_DROPPED:
            queued = any(
                (waiting.inbound.thread or waiting.inbound.message_id) == turn.root_thread_ts
                for waiting in self._host.waiting(inbound.workspace, inbound.conversation)
            )
            await self._unmark_dropped(turn, queued_behind=queued, running=thread in self._running, react=False)
        elif state == MARK_STARTED:
            self._running[thread] = inbound
            if self._acknowledge_mode() != ACK_MODE_OFF:
                await self._set_thinking_status(turn.origin)
        elif state in (MARK_DONE, MARK_FAILED, MARK_STOPPED):
            if self._running.get(thread) is inbound:
                del self._running[thread]
            await self._clear_thinking_status(turn.origin)

    # ── decisions ────────────────────────────────────────────────────────

    def decision_facts(self, inbound: InboundMessage) -> dict[str, Any]:
        """What the decision gate is told of a message, as the JiuwenSwarm channel tells it."""
        return self._decision_inputs.pop((inbound.conversation, inbound.message_id))

    async def recent_messages(self, conversation: str, limit: int, before: str, *, workspace: str = "") -> list[dict[str, Any]]:
        """The messages of a conversation before one of them, the bot's own included, oldest first."""
        return await self._decision_window(conversation, limit, before)

    # ── files ────────────────────────────────────────────────────────────

    async def _load_file(self, conversation: str, info: Mapping[str, Any]) -> dict[str, Any]:
        """Fetch one file of a message from Slack, as the ``data:`` URI a turn and the second brain take."""
        url, resolved = await resolve_attachment(self._client, info)
        mimetype = str(resolved.get("mimetype") or "").strip().lower()
        hosts = frozenset({SLACK_FILE_HOST, self._identity.host} - {""})
        data = await fetch_attachment_bytes(url, mimetype, bot_token=self._bot_token, allowed_hosts=hosts)
        encoded = base64.b64encode(bytes(data)).decode("ascii")
        logger.info(
            "slack file: loaded %s (%d bytes) from %s file_id=%s",
            slack_file_name(resolved), len(data), conversation, resolved.get("id") or "-",
        )
        return {"name": slack_file_name(resolved), "uri": f"data:{mimetype or 'application/octet-stream'};base64,{encoded}"}

    async def fetch_files(self, inbound: InboundMessage) -> tuple[Mapping[str, Any], ...]:
        # One notice per distinct reason, where the message was sent, so its sender knows what was not read.
        async def notify(notice: str) -> None:
            if self._client is None:
                return
            sent, _, error = await self._post_text(
                channel_id=inbound.conversation, text=notice,
                thread_ts=inbound.reply_thread or "", chunk_index=1, chunk_total=1,
            )
            if not sent:
                logger.warning("slack could not say which files were not read: %s", error)

        return await fetch_message_files(
            [entry[_FILE_INFO] for entry in inbound.files if isinstance(entry.get(_FILE_INFO), dict)],
            route_guarded(functools.partial(self._load_file, inbound.conversation)),
            name=slack_file_name,
            budget_seconds=FILE_BUDGET_SECONDS,
            logger=logger,
            label=ATTACHMENT_LABEL,
            failed_reason=FILE_REASON_DOWNLOAD_FAILED,
            conversation=inbound.conversation,
            notify=notify,
            log_prefix="slack file",
        )

    # ── tools ────────────────────────────────────────────────────────────

    def toolkits(self, metadata_provider: Callable[[], Mapping[str, Any]]) -> list[Any]:
        """Return the Slack toolkits as they are, each reading the request in flight and this workspace's client."""
        bound: dict[str, Any] = {"metadata_provider": metadata_provider, "workspaces": self._workspaces}
        # A turn of this host shares no directory with this process, so a file is read and sent as content.
        history = SlackInlineHistoryToolkit(**bound)
        kits = [
            SlackPostToolkit(bot_name_resolver=resolve_bot_name, **bound),
            SlackReactionToolkit(**bound),
            SlackScheduledToolkit(bot_name_resolver=resolve_bot_name, **bound),
            history,
            SlackDirectoryToolkit(**bound),
            SlackPinToolkit(**bound),
            SlackBookmarkToolkit(**bound),
            SlackCanvasToolkit(**bound),
            SlackListToolkit(**bound),
            SlackHomeTabToolkit(**bound),
        ]
        if self._search_enabled:
            kits.append(SlackSearchToolkit(**bound))
        if self._send_file_allowed:
            kits.append(SlackFileToolkit(files=history, bot_name_resolver=resolve_bot_name, **bound))
        return kits

    async def resolve_content_attachment(self, ref: str, metadata: Mapping[str, Any]) -> dict[str, str]:
        """Read a Slack file using the current turn's audience gate."""
        if not isinstance(ref, str) or not ref:
            raise ValueError("attachment_ref must be a Slack file id")
        history = SlackInlineHistoryToolkit(metadata_provider=lambda: metadata, workspaces=self._workspaces)
        try:
            record, data = await history.fetch_slack_file(ref)
        finally:
            history._scratch.cleanup()
        if data is None:
            raise ValueError(f"attachment_ref: {record.get('error', 'file_refused')}: {record.get('detail', '')}")
        return {
            "document": base64.b64encode(data).decode("ascii"),
            "document_mime_type": str(record.get("mimetype") or "application/octet-stream"),
        }

    def request_metadata(self, inbound: InboundMessage) -> dict[str, Any]:
        metadata = slack_request_metadata(
            inbound.conversation,
            inbound.sender,
            inbound.workspace,
            inbound.event_id,
            inbound.chat_type,
            inbound.trigger == TRIGGER_DM,
            audience=audience_metadata(self._enforce_audience),
        )
        metadata.update(
            slack_message_ts=inbound.message_id,
            slack_thread_ts=inbound.reply_thread or "",
            message_ts=inbound.message_id,
        )
        token = self._action_tokens.get((inbound.conversation, inbound.message_id))
        if token:
            metadata[SLACK_ACTION_TOKEN_KEY] = token
        return metadata


def create_platform(config: Mapping[str, Any]) -> Any:
    """Build the connector a configuration describes: one workspace, or one connector for each of several.

    It is not connected.
    """
    section = platform_section(config, NAME)
    if not isinstance(section, Mapping):
        raise BelleConfigError(f"Belle config platforms.{NAME} must be a mapping")
    if not resolve_command_name(section):
        raise BelleConfigError(f"Belle config platforms.{NAME}.command_name is not a name Slack accepts for a slash command")
    for key in sorted(UNREAD_SETTINGS & set(section)):
        logger.warning("Belle config platforms.%s.%s is set, and this connector does not act on it. That setting does nothing", NAME, key)
    if section.get(KEY_WORKSPACES):
        # A non-empty list takes precedence over the single pair, which is then not read.
        from belle_agent.platforms.slack.workspaces import create_workspaces

        return create_workspaces(section)
    tokens, sources = {}, {}
    for key, (variable, prefix) in _SECRETS.items():
        configured = str(section.get(key) or "").strip()
        value = configured or os.environ.get(variable, "").strip()
        source = f"Belle config platforms.{NAME}.{key}" if configured else variable
        if not value:
            raise BelleConfigError(f"the Slack {key} is read from the environment variable {variable}, which is not set")
        if not value.startswith(prefix):
            raise BelleConfigError(f"{source} does not hold a token that starts with {prefix}")
        tokens[key] = value
        sources[key] = source
    return SlackPlatform(section, bot_token_source=sources["bot_token"], app_token_source=sources["app_token"], **tokens)
