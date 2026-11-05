"""Slack channel implementation based on Slack Bolt Socket Mode."""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from jiuwenswarm.extensions.sdk.channel_runtime import (
    BaseChannel,
    ChannelMetadata,
    RobotMessageRouter,
    RoutingTarget,
    SlackDeliveryTarget,
)
from jiuwenswarm.extensions.sdk.host_types import EventType, Message, ReqMethod
from jiuwenswarm.extensions.sdk.workspace_paths import get_agent_sessions_dir

from belle_agent.admission.buffer import InboundEventBuffer
from belle_agent.click_notices import (
    ANSWER_REFUSED_NOTICE,
    STOP_ACCEPTED_NOTICE,
    STOP_REFUSED_NOTICE,
    STOP_STALE_NOTICE,
    WITHDRAWN_QUESTION_NOTE,
)
from belle_agent.integrations.jiuwenswarm.config import (
    slack_agent_request_params,
    slack_default_channel_id_from_config,  # noqa: F401 - JWS gateway import
)
from belle_agent.integrations.jiuwenswarm.scope_capabilities import (
    SLACK_CAPABILITIES,
)
from belle_agent.integrations.jiuwenswarm.slack_activity import SlackActivityMixin
from belle_agent.integrations.jiuwenswarm.slack_command_handlers import SlackCommandsMixin
from belle_agent.integrations.jiuwenswarm.slack_decisions import SlackDecisionMixin
from belle_agent.integrations.jiuwenswarm.slack_dedup import get_slack_dedup_path
from belle_agent.integrations.jiuwenswarm.slack_inbound import (
    _INTERRUPT_RESUME_SOURCES,
    _MAX_QUEUED_PER_SESSION,
    POST_AS_ROOT_KEY,
    SLACK_ADDRESSED_KEY,
    SlackInboundMixin,
    post_tool_instruction,
    slack_audience_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_outbound import SlackOutboundMixin
from belle_agent.integrations.jiuwenswarm.slack_question_delivery import (
    _QUESTION_ACTION_ID_RE,
    _QUESTION_INPUT_ACTION_ID_PREFIX,
    _QUESTION_SUBMIT_ACTION_ID,
    SlackQuestionMixin,
    _SlackPendingQuestion,
)
from belle_agent.integrations.jiuwenswarm.slack_routing import (
    SLACK_CHANNEL_ID,
    belle_slack_policy,
    parse_slack_cron_session,
    slack_message_team_id,
)
from belle_agent.integrations.jiuwenswarm.slack_stream_turn import SlackStreamMixin
from belle_agent.integrations.jiuwenswarm.step_text import StepText
from belle_agent.platforms.base import MARK_STOPPED
from belle_agent.platforms.slack import blocks as slack_blocks  # noqa: F401 - read from this module
from belle_agent.platforms.slack import inputs as slack_inputs
from belle_agent.platforms.slack import questions as slack_questions
from belle_agent.platforms.slack import task_forms as slack_task_forms
from belle_agent.platforms.slack.activity import (
    _ACTIVITY_TITLE_DONE as _ACTIVITY_TITLE_DONE,
)
from belle_agent.platforms.slack.activity import (
    _ACTIVITY_TITLE_STOPPED as _ACTIVITY_TITLE_STOPPED,
)
from belle_agent.platforms.slack.activity import (
    _ACTIVITY_TITLE_WORKING as _ACTIVITY_TITLE_WORKING,
)
from belle_agent.platforms.slack.activity import (
    _ERROR_CARD_TITLE as _ERROR_CARD_TITLE,
)
from belle_agent.platforms.slack.activity import (
    _RUN_FAILED,
    STOP_ACTION_ID,
    ActivityCardState,
    _SlackActivityRecord,
    decode_stop_value,
    posting_refusal_code,
)
from belle_agent.platforms.slack.actors import SlackActors
from belle_agent.platforms.slack.admission import (
    AdmissionState,
    SlackMessageOrigin,
    SlackTurnInitiator,
    click_identity,
)
from belle_agent.platforms.slack.admission import (
    SlackQueuedMessage as _SlackQueuedMessage,  # noqa: F401 - read from this module
)
from belle_agent.platforms.slack.channel_config import SlackChannelConfig
from belle_agent.platforms.slack.config import (
    ACK_MODE_OFF,
    BLOCKKIT_TABLES_AUTO,  # noqa: F401 - read from this module
    BLOCKKIT_TABLES_DEFAULT,  # noqa: F401 - read from this module
    BLOCKKIT_TABLES_MODES,  # noqa: F401 - read from this module
    KEY_OPTIONAL_REPLY_MODE,
    KEY_SESSION_SURFACE,
    OPTIONAL_REPLY_POST_TOOL,
    OPTIONAL_REPLY_SENTINEL_UNSTORED,
    normalize_optional_reply_mode,
    normalize_reply_in_thread,
    resolve_render_tables,  # noqa: F401 - read from this module
    resolve_streaming_enabled,
    resolve_streaming_mode,
    settled_word,
)
from belle_agent.platforms.slack.cron import (
    CRON_RECORD_TIMEOUT_SECONDS,
    MAX_CRON_RECORDS,
    CronPush,
    CronRecord,
)
from belle_agent.platforms.slack.delivery import (
    CHANNEL_LOG_TAG,
    SlackDeliveryError,
    slack_failure_reason,
)
from belle_agent.platforms.slack.events import (
    EVENT_BUFFER_LIMITS,
    EVENT_BUFFER_TTL_SECONDS,
    EVENT_TYPE_FAMILIES,
    event_disposition,
)
from belle_agent.platforms.slack.file_transfer import (
    SLACK_FILE_HOST,
    download_attachment,
    download_attachments,
    safe_path_component,
    unreadable_file_notice,
)
from belle_agent.platforms.slack.history_policy import (
    KEY_ENFORCE_AUDIENCE,
    METADATA_WRITE_TARGET_REQUIRED_KEY,
)
from belle_agent.platforms.slack.identity import (
    SlackWorkspaceIdentity,
    click_channel_id,
    click_target,
    conversation_chat_type,
    event_team_id,
    learn_chat_type,
)
from belle_agent.platforms.slack.post import POST_MESSAGE
from belle_agent.platforms.slack.reactions import ReactionMarks
from belle_agent.platforms.slack.scope_policy import (
    DEFAULT_COMMAND_NAME,
    GRANTED_SCOPES_HEADER,
    TIER_TOOLS,
    command_token,
    normalise_command_name,
    resolve_bot_name,
    scope_reasons,
    scopes_for_tier,
    slack_granted_scopes,
    slack_required_scopes,
)
from belle_agent.platforms.slack.scoped_config import (
    GROUP_CHAT_MODES,
    GROUP_MODE_MENTION,
    SlackChannelOverride,
    channel_triggers,
    settled_override,
    slack_scope_enables_channel,
)
from belle_agent.platforms.slack.slack_dedup import SlackEventDedupStore
from belle_agent.platforms.slack.socket_mode import SlackSocket
from belle_agent.platforms.slack.stream_delivery import STREAM_STOPPED_TAIL, STREAM_TRUNCATION_SUFFIX
from belle_agent.platforms.slack.streaming import (
    SlackStreamState,
)
from belle_agent.platforms.slack.streaming import (
    _SlackStreamingSurface as _SlackStreamingSurface,
)
from belle_agent.platforms.slack.text import (
    MAX_SLACK_TEXT_LENGTH,
    MAX_SLACK_UPDATE_TEXT_LENGTH,
    normalize_slack_mrkdwn,
)
from belle_agent.platforms.slack.thread_status import (
    SlackThreadStatus,
)
from belle_agent.platforms.slack.turn_text import (
    inbound_events_block,
)
from belle_agent.scopes import (
    AXIS_CHAT_TYPE,
    CLICK_APPROVE,
    CLICK_STOP,
    MID_TURN_DEFAULT,
    MID_TURN_STEER,
    REPLY_OPTIONAL,
    SESSION_CHANNEL,
    SESSION_DEFAULT,
    SESSION_THREAD,
    SESSION_VALUES,
    click_rule,
)
from belle_agent.silence import (
    NO_REPLY_INSTRUCTION,
    NO_REPLY_SENTINEL,
    REPLY_OPTIONAL_KEY,
    SILENCE_UNSTORED_KEY,
    is_no_reply_sentinel,
    looks_like_a_declined_reply,
)
from belle_agent.turn_policy import mid_turn_mode

logger = logging.getLogger(__name__)

try:
    from slack_bolt.async_app import AsyncApp

except ImportError:
    AsyncApp = None  # type: ignore[assignment,misc]


# Slack renders a user mention as "<@U01ABCDEF>", optionally with a "|label"
# suffix on older payloads.
_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")

# The assistant API accepts custom text and clears it when the app posts.
# One deadline covers the entire attachment batch.
_ATTACHMENT_PHASE_TIMEOUT_SECONDS = 120.0

#: Ingest reactions: pending, usable, filtered, or failed.


# Both ceilings are Slack's rather than this connector's, and the posting tools in
# the agent runtime are held to the same two, so they are stated once in
# ``belle_agent.platforms.slack.text``.
_MAX_SLACK_TEXT_LENGTH = MAX_SLACK_TEXT_LENGTH
_MAX_SLACK_UPDATE_TEXT_LENGTH = MAX_SLACK_UPDATE_TEXT_LENGTH
_STREAM_TRUNCATION_SUFFIX = STREAM_TRUNCATION_SUFFIX
# A catch-all listener acknowledges unhandled Events API envelopes.
_ANY_EVENT_TYPE = re.compile(r".*")

# The stop button's action id and the three notices its clicker is shown live with the card it is drawn on.
_STOP_ACTION_ID = STOP_ACTION_ID
_STOP_ACCEPTED_NOTICE = STOP_ACCEPTED_NOTICE
_STOP_STALE_NOTICE = STOP_STALE_NOTICE
_STOP_REFUSED_NOTICE = STOP_REFUSED_NOTICE

# The same courtesy on the answer path, where a clicks rule is the only thing
# that can produce it. The allow-list refusal beside it stays silent: this notice
# belongs to the clicks gate alone and does not change what that refusal does.
_ANSWER_REFUSED_NOTICE = ANSWER_REFUSED_NOTICE
_INCOMPLETE_SUBMIT_NOTICE = slack_questions.INCOMPLETE_SUBMIT_NOTICE

# Questions posted and still waiting for a click. Bounded and aged for the same
# reason the stream map is: a turn that is cancelled, or answered from another
# client, never comes back to clear its entry.
_MAX_PENDING_QUESTIONS = 32
_PENDING_QUESTION_TIMEOUT_SECONDS = 3600.0

_STREAM_STOPPED_TAIL = STREAM_STOPPED_TAIL

_WITHDRAWN_QUESTION_NOTE = WITHDRAWN_QUESTION_NOTE

# The Slack SDK installs AsyncConnectionErrorRetryHandler by default and nothing
# else, so a 429 arrives here as a raised error rather than a retried call
# (slack_sdk.http_retry.builtin_async_handlers.async_default_handlers). Retrying
# rate limits is therefore ours to do.

_MAX_ACTIVE_STREAMS = 32
_STREAM_IDLE_TIMEOUT_SECONDS = 900.0
# Bound initiators whose terminal events never arrive.
_MAX_TURN_INITIATORS = 256
_TURN_INITIATOR_TIMEOUT_SECONDS = 3600.0
# Bound posting-tool records while a turn awaits its result.
_MAX_POSTING_TOOL_TURNS = 128
# Bound the answer text kept for requests whose final never arrives.
_MAX_STEP_TEXTS = 256

# Tell the sender when the queue refused their message.
_QUEUE_FULL_NOTICE = (
    f"This conversation is already holding {_MAX_QUEUED_PER_SESSION} messages"
    " waiting on the turn in progress, which is as many as it holds. Your"
    " message was not accepted and will not be answered. Send it again once"
    " the current work finishes."
)
# Ignore terminal acknowledgments that do not end the active turn.
_MAX_ACK_ONLY_REQUESTS = 256
_ACK_ONLY_REQUEST_TIMEOUT_SECONDS = 600.0


def _prune_bounded_map(
    entries: dict[Any, Any],
    *,
    cap: int,
    timeout: float,
    timestamp: Callable[[Any], float],
    what: str,
) -> None:
    """Expire old entries, then evict the oldest entries beyond cap."""
    cutoff = time.monotonic() - timeout
    for key in [key for key, value in entries.items() if timestamp(value) < cutoff]:
        del entries[key]
    while len(entries) >= cap:
        oldest = min(entries, key=lambda key: timestamp(entries[key]))
        logger.warning("Slack dropped the oldest of too many %s: %s", what, oldest)
        del entries[oldest]


_clamp = slack_inputs.clamp


def _mentioned_user_ids(text: str) -> list[str]:
    """Slack user ids mentioned in ``text``, in order and without duplicates."""
    return list(dict.fromkeys(_USER_MENTION_RE.findall(text or "")))


def _slack_timestamp_ms(message_ts: str) -> int:
    """Convert a Slack ``ts`` ("1710000000.000100") to epoch milliseconds."""
    try:
        return int(float(message_ts) * 1000)
    except (TypeError, ValueError):
        return int(time.time() * 1000)


# Slack SDK logs need the shared handlers to remain visible.
SDK_LOGGER_NAMES: tuple[str, ...] = ("slack_bolt", "slack_sdk")
# Keep this child handler-free so Bolt records propagate once.
SDK_BASE_LOGGER_NAME = "slack_bolt.jiuwenswarm"


def configure_sdk_logging(level_name: str) -> logging.Logger:
    """Attach Slack SDK loggers to the shared handlers."""
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.WARNING
    shared = list(logging.getLogger("jiuwenswarm").handlers)
    for name in SDK_LOGGER_NAMES:
        sdk_logger = logging.getLogger(name)
        sdk_logger.setLevel(level)
        sdk_logger.propagate = False
        for handler in shared:
            if handler not in sdk_logger.handlers:
                sdk_logger.addHandler(handler)
    floor = min((handler.level for handler in shared), default=level)
    logger.info(
        "Slack SDK logging wired: loggers=%s level=%s handlers=%d"
        " effective_floor=%s",
        "/".join(SDK_LOGGER_NAMES),
        logging.getLevelName(level),
        len(shared),
        logging.getLevelName(max(level, floor)),
    )
    base = logging.getLogger(SDK_BASE_LOGGER_NAME)
    base.setLevel(level)
    return base


@dataclass
class _SlackPostingToolCalls:
    """Track whether a turn posted, Slack refused its call, or no result arrived."""

    started_at: float = field(default_factory=time.monotonic)
    # Set by a result the rail did not flag as a failure.
    delivered: bool = False
    # The code of the last refused call, or ``""``.
    refused: str = ""


#: Scope-supported chat types. Member events may instead carry ``C`` or ``G``.
SLACK_CHAT_TYPES: frozenset[str] = frozenset(
    SLACK_CAPABILITIES.axis_values.get(AXIS_CHAT_TYPE) or ()
)


HEARTBEAT_SLACK_UNREACHABLE_MESSAGE = (
    "heartbeat.target=slack but channels.slack.default_channel_id is unset:"
    " a heartbeat relay carries no conversation of its own, so no heartbeat"
    " will ever be delivered to Slack. Set channels.slack.default_channel_id to"
    " the channel heartbeats should land in, or point heartbeat.target at"
    " another channel."
)


def warn_if_heartbeat_relay_unreachable(
    *,
    heartbeat_target: Any,
    default_channel_id: Any,
) -> str:
    """Warn if a Slack heartbeat has no destination. Return the warning."""
    if str(heartbeat_target or "").strip().lower() != SLACK_CHANNEL_ID:
        return ""
    if SlackChannel.delivery_is_reachable(
        session_id=None, default_channel_id=default_channel_id
    ):
        return ""
    logger.warning(HEARTBEAT_SLACK_UNREACHABLE_MESSAGE)
    return HEARTBEAT_SLACK_UNREACHABLE_MESSAGE


async def describe_slack_delivery_reachability(
    config: SlackChannelConfig,
    *,
    cron_store: Any,
    heartbeat_target: Any = "",
) -> str:
    """Report whether cron and heartbeat jobs can reach Slack at startup.

    Report an unreadable cron store in the result without blocking startup.
    """
    fallback = str(config.default_channel_id or "").strip()
    parts: list[str] = []
    unreachable: list[str] = []
    store_readable = True

    try:
        jobs = list(await cron_store.list_jobs())
    except Exception as exc:  # noqa: BLE001 - reported, never raised.
        store_readable = False
        jobs = []
        parts.append(
            f"the cron store could not be read ({exc}), so no cron job was checked"
        )

    if store_readable:
        enabled_slack = [
            job
            for job in jobs
            if str(getattr(job, "targets", "") or "").strip().lower()
            == SLACK_CHANNEL_ID
            and bool(getattr(job, "enabled", False))
        ]
        for job in enabled_slack:
            if not SlackChannel.delivery_is_reachable(
                session_id=getattr(job, "session_id", None),
                default_channel_id=fallback,
            ):
                unreachable.append(str(getattr(job, "id", "") or "").strip() or "<no id>")
        if not enabled_slack:
            parts.append("no enabled Slack cron job")
        elif not unreachable:
            parts.append(
                f"all {len(enabled_slack)} enabled Slack cron job(s) resolve a channel"
            )
        else:
            parts.append(
                f"{len(unreachable)} of {len(enabled_slack)} enabled Slack cron"
                f" job(s) cannot resolve a channel and will fail at delivery"
                f" ({', '.join(sorted(unreachable))})"
            )

    target = str(heartbeat_target or "").strip().lower()
    if target != SLACK_CHANNEL_ID:
        parts.append(f"heartbeat.target={target or '<unset>'} is not Slack")
    elif SlackChannel.delivery_is_reachable(
        session_id=None, default_channel_id=fallback
    ):
        parts.append("heartbeat.target=slack falls back to default_channel_id")
    else:
        parts.append(
            "heartbeat.target=slack has no fallback and can never be delivered"
        )

    parts.append(
        f"channels.slack.default_channel_id={fallback}"
        if fallback
        else "channels.slack.default_channel_id is unset"
    )

    message = f"channels.slack delivery: {'; '.join(parts)}"
    if unreachable or not store_readable or (target == SLACK_CHANNEL_ID and not fallback):
        logger.warning(message)
    else:
        logger.info(message)
    return message


class SlackChannel(SlackThreadStatus, SlackCommandsMixin, SlackInboundMixin, SlackDecisionMixin, SlackActivityMixin, SlackStreamMixin, SlackOutboundMixin, SlackQuestionMixin, BaseChannel):
    """Belle Slack channel hosted by JiuwenSwarm."""

    stop_action_id = _STOP_ACTION_ID
    name = "slack"
    # The reply delivery and the thread status log under their own names. A streamed reply's ending and
    # two lines of the command surface log through this module's logger.
    _log = logger
    _log_tag = CHANNEL_LOG_TAG

    # Keep question IDs and inputs for the Slack prompt renderer.
    renders_interactive_prompts = True

    def __init__(
        self,
        config: SlackChannelConfig,
        router: RobotMessageRouter,
        *,
        dedup_store: SlackEventDedupStore | None = None,
        im_platform_adapter: Any = None,
    ):
        super().__init__(config, router)
        self.config: SlackChannelConfig = config
        self._socket: SlackSocket | None = None
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._identity = SlackWorkspaceIdentity()
        self._actors = SlackActors(logger)
        # None means auth.test did not report scopes. Empty means none granted.
        self._granted_scopes: frozenset[str] | None = None
        # None disables ingestion when the AgentServer cannot share this filesystem.
        self._brain_ingest_hook: Callable[..., Any] | None = None
        # Track detached ingest tasks for shutdown.
        self._brain_tasks: set[asyncio.Task] = set()
        self.im_platform_adapter = self._im_platform_adapter = im_platform_adapter
        # Persist deduplication across Slack retries and restarts.
        self._dedup = dedup_store or SlackEventDedupStore(get_slack_dedup_path())
        self._stream_state = SlackStreamState()
        self._streams = self._stream_state.streams
        self._stream_next_update_at = self._stream_state.next_update_at
        self._stream_recipients = self._stream_state.recipients
        # Arrival-ordered queues, reservations, and active turns are owned by Belle.
        self._admission = AdmissionState()
        # Keep the established private views for callers that inspect them.
        self._queued_messages = self._admission.queued_messages
        self._pending_events = self._admission.pending_events
        self._admitting_events = self._admission.admitting_events
        self._event_admissions = self._admission.event_admissions
        self._lost_events: dict[str, int] = {}
        self._reported_required_event_reply: set[tuple[str, str]] = set()
        # Accepted input whose end-of-stream has no answer.
        self._ack_only_requests: dict[str, float] = {}
        self._stream_lock = self._stream_state.lock
        # Questions posted as buttons and not yet answered, keyed by request id.
        self._pending_questions: dict[str, _SlackPendingQuestion] = {}
        # Slash-command replies by request ID.
        self._agent_replies: dict[str, asyncio.Future[Message]] = {}
        self._slash_task_forms: dict[str, slack_task_forms.TaskForm] = {}
        # Last channel session and thread, used when ask forks a side session.
        self._channel_sessions: dict[str, tuple[str, str]] = {}
        # (team, DM channel, root ts) -> side session, oldest first.
        self._side_sessions: dict[tuple[str, str, str], str] = {}
        # Include channel in the key so a run cannot edit another channel's card.
        self._cron_records: dict[tuple[str, str], CronRecord] = {}
        self._activity_state = ActivityCardState()
        self._activity_records = self._activity_state.records
        # Resumed request id -> the id the request started under, so that the
        # turns an interrupt splits a request into share one card.
        self._activity_request_aliases = self._activity_state.request_aliases
        # Request id -> when a posting-tool call was first seen on that turn.
        # Read by the line reporting a turn whose text ``post_tool`` withheld.
        self._posting_tool_turns: dict[str, _SlackPostingToolCalls] = {}
        # Request id -> its answer text so far, to set its model calls apart.
        self._step_texts: dict[str, StepText] = {}
        # The delayed post and dispatch loop share activity records.
        self._activity_lock = self._activity_state.lock
        # Warn once per missing model until the connector is reconfigured.
        self._reported_missing_models: set[tuple[str, str]] = set()
        # Shared by every sender and thread; cleared when this connector restarts.
        self._model_overrides: dict[tuple[str, str], str] = {}
        # Warn once per channel when session and mid-turn policies conflict.
        self._reported_steer_under_channel_session: set[str] = set()
        # Warn once per unclaimed event type until reconfiguration.
        self._unclaimed_event_types: set[str] = set()
        self._reaction_marks = ReactionMarks(logger)
        # Probe status API availability per installation. Refusals can change it.
        self._agents_status_allowed = True
        # Buffered reactions, pins, and membership changes are in-memory only.
        self._inbound_events = InboundEventBuffer(
            limits=EVENT_BUFFER_LIMITS, ttl_seconds=EVENT_BUFFER_TTL_SECONDS
        )
        # Message payloads supply chat type. Other events use this cache.
        # Unknown stays unknown after restart rather than guessing.
        self._chat_types: dict[str, str] = {}
        self._init_decisions()


    @property
    def app_id(self) -> str:
        """Routing key for this Slack workspace, defaulting to the legacy key."""
        return self.config.app_id or "default"

    @property
    def _workspace_host(self) -> str:
        """Host read by the attachment allowlist."""
        return self._identity.host

    def claims_message(self, msg: Any) -> bool:
        """Claim a message only when both sides name this workspace's team."""
        return self._identity.claims_team(slack_message_team_id(msg))

    @property
    def channel_id(self) -> str:
        return self.name

    @property
    def clients(self) -> set[Any]:
        return set()

    def on_message(self, callback: Callable[[Message], Any]) -> None:
        self._on_message_cb = callback

    def _register_listeners(self, app: Any) -> None:
        app.event("app_mention")(self._handle_app_mention)
        app.event("message")(self._handle_message_event)
        self._register_extra_listeners(app)

    async def start(self) -> None:
        if AsyncApp is None:
            logger.error("Slack Socket Mode requires belle-agent[slack]")
            return
        if not self.config.enabled or self._running:
            return
        if not self.config.bot_token.strip() or not self.config.app_token.strip():
            logger.error("Slack Socket Mode needs bot_token and app_token")
            return
        if not self._prepare_start():
            return
        socket = SlackSocket(
            self.config.bot_token.strip(),
            self.config.app_token.strip(),
            self._register_listeners,
            logger=configure_sdk_logging(self.config.sdk_log_level),
            app_factory=AsyncApp,
        )
        self._socket = socket
        self._app, self._client, self._handler = socket.app, socket.client, socket.handler
        if not await self._prepare_handler():
            self._socket = self._app = self._client = self._handler = None
            await socket.close()
            return
        self._running = True
        self._on_started()
        try:
            await socket.run()
        except Exception:
            logger.exception("Slack Socket Mode failed")
            raise
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
        await self._before_stop()
        socket = self._socket
        self._socket = self._app = self._handler = self._client = None
        if socket is not None:
            try:
                await socket.close()
            except Exception as exc:  # noqa: BLE001
                self._log_stop_failure(exc)

    @staticmethod
    def _extract_outgoing_text(msg: Message) -> str:
        payload = msg.payload or {}
        if (
            msg.event_type == EventType.HEALTH_CHECK_RELAY
            and isinstance(payload, dict)
            and (health_check := payload.get("health_check"))
        ):
            return str(health_check).strip()
        if isinstance(payload, dict):
            if "content" in payload:
                content = payload["content"]
                return str(content.get("output", content) if isinstance(content, dict) else content or "").strip()
            if payload.get("error"):
                return str(payload["error"]).strip()
        if msg.params and "content" in msg.params:
            return str(msg.params["content"] or "").strip()
        return payload.strip() if isinstance(payload, str) else ""


    def _prepare_start(self) -> bool:
        if not normalise_command_name(self.config.command_name):
            logger.error(
                "SlackChannel%s has no usable Belle platforms.slack.command_name;"
                " not starting this workspace",
                self._identity.label(self.config.app_id),
            )
            return False
        self._shadow_decision_gate()
        return True

    def _register_extra_listeners(self, app: Any) -> None:
        for event_type in sorted(EVENT_TYPE_FAMILIES):
            app.event(event_type)(self._handle_inbound_event)
        app.event("agent_session_stopped")(self._handle_agent_session_stopped)
        app.event(_ANY_EVENT_TYPE)(self._handle_unclaimed_event)
        app.action(_QUESTION_ACTION_ID_RE)(self._handle_question_action)
        app.action(_STOP_ACTION_ID)(self._handle_stop_action)
        self._register_commands(app)

    async def _prepare_handler(self) -> bool:
        await self._load_bot_user_id()
        return self._scopes_permit_start()

    def _on_started(self) -> None:
        self._log_approval_button_availability()
        self._log_stop_button_availability()
        self._log_session_surface()

    async def _before_stop(self) -> None:
        self._slash_task_forms.clear()
        # Replayed ingests deduplicate by SHA-256.
        for task in list(self._brain_tasks):
            task.cancel()
        self._brain_tasks.clear()
        # Do not resume writes or answers from a previous connection.
        self._stream_state.clear()
        self._pending_questions.clear()
        self._cron_records.clear()
        self._activity_state.clear()
        self._posting_tool_turns.clear()
        self._step_texts.clear()
        # Queued messages cannot run after shutdown; log their loss.
        held = sum(len(queue) for queue in self._admission.queued_messages.values())
        if held:
            logger.warning(
                "Slack dropped %s queued message(s) across %s session(s):"
                " the channel stopped before the turns they were waiting on"
                " ended",
                held,
                len(self._admission.queued_messages),
            )
        for queue in self._admission.pending_events.values():
            for entry in queue:
                self._log_pending_event(entry, "channel-stopped")
        for arrivals in self._admission.admitting_events.values():
            for entry in arrivals.values():
                self._log_pending_event(entry, "channel-stopped-during-admission")
        self._admission.clear()
        self._lost_events.clear()
        self._reported_required_event_reply.clear()
        await self._close_decisions()
        # Restarted turns must not inherit old event context.
        self._inbound_events.forget()
        self._ack_only_requests.clear()
        self._identity.clear()
        # Re-read granted scopes after reconnect or reinstall.
        self._granted_scopes = None

    def _log_stop_failure(self, exc: Exception) -> None:
        logger.warning("SlackChannel stop failed: %s", slack_failure_reason(exc))





    def _log_approval_button_availability(self) -> None:
        """Confirm that conversation turns can deliver approval events."""
        logger.info("[SlackChannel] approval buttons active")

    def _log_stop_button_availability(self) -> None:
        """Report whether activity cards expose the stop button."""
        if self.config.activity_card:
            logger.info("[SlackChannel] stop button active")
            return
        logger.warning(
            "[SlackChannel] stop button inactive: it is rendered on the turn "
            "activity card and Belle platforms.slack.activity_card is false, so the "
            "only way to stop a running turn is to post a message, which "
            "cancels it without checking who sent it"
        )

    def _remember_question(self, pending: _SlackPendingQuestion) -> None:
        """Record a question, evicting stale entries first."""
        _prune_bounded_map(
            self._pending_questions,
            cap=_MAX_PENDING_QUESTIONS,
            timeout=_PENDING_QUESTION_TIMEOUT_SECONDS,
            timestamp=lambda entry: entry.touched_at,
            what="unanswered questions",
        )
        self._pending_questions[pending.request_id] = pending







    def _remember_cron_record(self, cron: CronPush, message_ts: str) -> None:
        """Record a posted placeholder so its result can rewrite it."""
        _prune_bounded_map(
            self._cron_records,
            cap=MAX_CRON_RECORDS,
            timeout=CRON_RECORD_TIMEOUT_SECONDS,
            timestamp=lambda entry: entry.touched_at,
            what="tracked cron runs",
        )
        self._cron_records[(cron.run_id, cron.channel_id)] = CronRecord(
            run_id=cron.run_id,
            channel_id=cron.channel_id,
            message_ts=message_ts,
        )


    def _prune_streams(self) -> None:
        """Evict idle streams, including turns that ended without text."""
        _prune_bounded_map(
            self._streams,
            cap=_MAX_ACTIVE_STREAMS,
            timeout=_STREAM_IDLE_TIMEOUT_SECONDS,
            timestamp=lambda stream: stream.touched_at,
            what="open streams",
        )
        if len(self._stream_next_update_at) > _MAX_ACTIVE_STREAMS:
            self._stream_next_update_at.clear()

    def _remember_turn_initiator(
        self,
        session_id: str,
        user_id: str,
        request_id: str,
        *,
        is_dm: bool,
        chat_type: str,
        model_name: str = "",
        origin: SlackMessageOrigin | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        preview_text: str = "",
    ) -> None:
        """Record the turn's sender and reply target, if any."""
        if not session_id or not user_id:
            return
        metadata = request_metadata or {}
        where = origin or SlackMessageOrigin()
        entry = SlackTurnInitiator(
            user_id=user_id,
            request_id=request_id,
            is_dm=is_dm,
            chat_type=chat_type,
            model_name=model_name,
            channel_id=where.channel_id,
            message_ts=where.message_ts,
            reply_thread_ts=where.reply_thread_ts,
            root_thread_ts=where.root_thread_ts,
            silence_is_honoured=metadata.get(REPLY_OPTIONAL_KEY) is True,
            addressed=bool(metadata.get(SLACK_ADDRESSED_KEY)),
            post_as_root=bool(metadata.get(POST_AS_ROOT_KEY)),
            trigger=str(metadata.get("slack_trigger") or ""),
            preview_text=_clamp(preview_text, 240),
        )
        self._admission.remember_turn(
            session_id, entry,
            cap=_MAX_TURN_INITIATORS,
            timeout=_TURN_INITIATOR_TIMEOUT_SECONDS,
            warn=logger.warning,
        )

    def _forget_turn_initiator(self, session_id: str, request_id: str) -> None:
        """Remove an initiator only if the ending request still owns it."""
        self._admission.forget_turn(session_id, request_id)

    def turn_initiator(self, session_id: str) -> SlackTurnInitiator | None:
        """Return the current turn's starter, or ``None`` if unknown."""
        return self._admission.turn_initiator(
            session_id, timeout=_TURN_INITIATOR_TIMEOUT_SECONDS,
        )

    def _settled(
        self,
        channel_id: str,
        user_id: str = "",
        chat_type: str = "",
        *,
        workspace: str | None = None,
    ) -> SlackChannelOverride:
        """Settle scopes for a conversation and sender."""
        return settled_override(
            self.config,
            channel_id,
            user_id=user_id,
            chat_type=chat_type,
            workspace=self._identity.team_id if workspace is None else workspace,
        )

    def _session_scope(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> str:
        """Resolve thread or channel session scope for this conversation."""
        settled = self._settled(channel_id, user_id, chat_type).session
        default = str(self.config.session or "").strip().lower()
        return settled if settled in SESSION_VALUES else (
            default if default in SESSION_VALUES else SESSION_DEFAULT
        )

    def _channel_is_allowed(self, channel_id: str) -> bool:
        return (
            not self.config.allowed_channel_ids
            or channel_id in self.config.allowed_channel_ids
            or slack_scope_enables_channel(self.config, channel_id)
        )

    def _mid_turn_mode(
        self,
        channel_id: str,
        user_id: str = "",
        *,
        chat_type: str = "",
        session_is_channel_wide: bool = False,
        required_subagent: bool = False,
    ) -> str:
        """Resolve how a new message affects the current turn."""
        settled = self._settled(channel_id, user_id, chat_type).mid_turn
        if settled == MID_TURN_STEER and session_is_channel_wide:
            # A steer could send a reply to another thread in a channel session.
            self._report_steer_under_a_channel_session(channel_id)
        return mid_turn_mode(
            settled, shared_session=session_is_channel_wide,
            requires_separate_turn=required_subagent,
        )

    def _report_steer_under_a_channel_session(self, channel_id: str) -> None:
        """Warn once per channel when steering conflicts with channel sessions."""
        if channel_id in self._reported_steer_under_channel_session:
            return
        self._reported_steer_under_channel_session.add(channel_id)
        logger.warning(
            "Slack %s sets delivery.session=%s and delivery.mid_turn=%s;"
            " these cannot both hold. Messages queue instead, so replies stay"
            " in their threads. Set session=%s to steer, or mid_turn=%s to queue",
            channel_id,
            SESSION_CHANNEL,
            MID_TURN_STEER,
            SESSION_THREAD,
            MID_TURN_DEFAULT,
        )

    @staticmethod
    def _is_runtime_accepted(msg: Message) -> bool:
        """Recognize runtime acceptance in typed and payload event shapes."""
        if msg.event_type == EventType.RUNTIME_ACCEPTED:
            return True
        payload = getattr(msg, "payload", None)
        return (
            isinstance(payload, Mapping)
            and str(payload.get("event_type") or "")
            == EventType.RUNTIME_ACCEPTED.value
        )

    def _note_ack_only_request(self, request_id: str) -> None:
        """Mark an accepted request whose ending must not close the turn."""
        if not request_id:
            return
        _prune_bounded_map(
            self._ack_only_requests,
            cap=_MAX_ACK_ONLY_REQUESTS,
            timeout=_ACK_ONLY_REQUEST_TIMEOUT_SECONDS,
            timestamp=lambda noted_at: noted_at,
            what="acknowledged requests",
        )
        self._ack_only_requests[request_id] = time.monotonic()

    def _is_ack_only_request(self, request_id: str) -> bool:
        """Consume an acknowledgement-only request marker once."""
        if not request_id:
            return False
        noted_at = self._ack_only_requests.pop(request_id, None)
        if noted_at is None:
            return False
        return noted_at >= time.monotonic() - _ACK_ONLY_REQUEST_TIMEOUT_SECONDS


    def _request(
        self,
        request_id: str,
        *,
        session_id: str | None,
        chat_id: str,
        user_id: str,
        req_method: ReqMethod,
        params: dict[str, Any],
        metadata: dict[str, Any],
        is_stream: bool = False,
        **fields: Any,
    ) -> Message:
        """Set both routing IDs; the gateway falls back from app_id to bot_id."""
        return Message(
            id=request_id,
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            provider="slack",
            chat_id=chat_id,
            user_id=user_id,
            req_method=req_method,
            is_stream=is_stream,
            app_id=self.app_id,
            bot_id=self.app_id,
            metadata=metadata,
            **fields,
        )

    async def _route_request(self, req: Message) -> None:
        """Route a request through the callback or message bus."""
        if self._on_message_cb is not None:
            result = self._on_message_cb(req)
            if asyncio.iscoroutine(result):
                await result
            return
        await self.bus.route_user_message(req)

    def _stream_recipient(self, msg: Message) -> tuple[str, str]:
        return self._stream_state.recipient(str(msg.session_id or ""), msg.metadata or {})

    @staticmethod
    def _stream_key(
        msg: Message, channel_id: str, thread_ts: str
    ) -> tuple[str, str, str]:
        return str(msg.id or ""), channel_id, thread_ts

    @staticmethod
    def _extract_delta_text(msg: Message) -> str:
        """Keep fragment whitespace and skip reasoning fragments."""
        payload = getattr(msg, "payload", None)
        if not isinstance(payload, dict):
            return ""
        chunk_type = str(payload.get("source_chunk_type") or "").strip().lower()
        if chunk_type == "llm_reasoning":
            return ""
        content = payload.get("content")
        if isinstance(content, dict):
            content = content.get("output", "")
        return str(content or "")

    def _reply_in_thread(self) -> str:
        """Normalize the configured thread mode."""
        return normalize_reply_in_thread(self.config.reply_in_thread)

    def _optional_reply_mode(self) -> str:
        """Normalize the configured optional-reply mode."""
        return normalize_optional_reply_mode(self.config.optional_reply_mode)

    def _mark_reply_is_optional(self, metadata: dict[str, Any]) -> None:
        """Set optional-reply metadata and mode-specific delivery rules."""
        metadata[REPLY_OPTIONAL_KEY] = True
        word = self._optional_reply_mode()
        if word == OPTIONAL_REPLY_POST_TOOL:
            metadata[METADATA_WRITE_TARGET_REQUIRED_KEY] = True
        if word == OPTIONAL_REPLY_SENTINEL_UNSTORED:
            metadata[SILENCE_UNSTORED_KEY] = True

    def _optional_reply_instruction(self, *, honoured: bool, offered: bool) -> str:
        """Tell this turn how to reply or choose silence."""
        if self._optional_reply_mode() != OPTIONAL_REPLY_POST_TOOL:
            return NO_REPLY_INSTRUCTION if offered else ""
        return post_tool_instruction() if honoured else ""

    def _step_text(self, msg: Message) -> StepText:
        """Find or start the answer text of this event's request."""
        request_id = str(msg.id or "")
        held = self._step_texts.get(request_id)
        if held is not None:
            return held
        _prune_bounded_map(
            self._step_texts,
            cap=_MAX_STEP_TEXTS,
            timeout=_TURN_INITIATOR_TIMEOUT_SECONDS,
            timestamp=lambda text: text.touched_at,
            what="requests whose answer text is kept",
        )
        opened = self._step_texts[request_id] = StepText()
        return opened

    def _with_step_breaks(self, msg: Message) -> Message:
        """Set the model calls of a final apart, as they were streamed."""
        written = self._step_texts.pop(str(msg.id or ""), None)
        payload = msg.payload
        if written is None or not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            return msg
        content = written.final(payload["content"])
        if content == payload["content"]:
            return msg
        return replace(msg, payload={**payload, "content": content})

    def _posting_tool_record(
        self, msg: Message, *, create: bool = False
    ) -> _SlackPostingToolCalls | None:
        """Find or create posting-tool state for this turn."""
        request_id = str(msg.id or "").strip() or str(msg.session_id or "").strip()
        if not request_id:
            return None
        resolved = self._activity_request_aliases.get(request_id, request_id)
        held = self._posting_tool_turns.get(resolved)
        if held is not None or not create:
            return held
        _prune_bounded_map(
            self._posting_tool_turns,
            cap=_MAX_POSTING_TOOL_TURNS,
            timeout=_TURN_INITIATOR_TIMEOUT_SECONDS,
            timestamp=lambda calls: calls.started_at,
            what="turns that called the Slack posting tool",
        )
        opened = _SlackPostingToolCalls()
        self._posting_tool_turns[resolved] = opened
        return opened

    def _note_posting_tool_call(self, msg: Message, name: str) -> None:
        """Open this turn's posting-tool record on its first call."""
        if name == POST_MESSAGE:
            self._posting_tool_record(msg, create=True)

    def _note_posting_tool_result(self, msg: Message, name: str, state: str) -> None:
        """Record whether one posting-tool call was refused.

        ``state`` is the rail's own classification, which is what decides. The
        payload is read only for the code behind a classification already made,
        never to make one.
        """
        if name != POST_MESSAGE:
            return
        calls = self._posting_tool_record(msg, create=True)
        if calls is None:
            return
        if state != _RUN_FAILED:
            calls.delivered = True
            return
        calls.refused = posting_refusal_code(msg.payload) or "refused"

    def _note_unposted_turn(self, msg: Message, channel_id: str, content: str) -> None:
        """Log why a post-tool turn produced no visible reply, without its text."""
        session_id = str(msg.session_id or "") or "-"
        calls = self._posting_tool_record(msg)
        if calls is None:
            logger.warning(
                "[SlackChannel] nothing posted under %s=%s and no %s call was"
                " seen: channel=%s session=%s chars=%d",
                KEY_OPTIONAL_REPLY_MODE,
                OPTIONAL_REPLY_POST_TOOL,
                POST_MESSAGE,
                channel_id or "-",
                session_id,
                len(content),
            )
            return
        if calls.refused and not calls.delivered:
            logger.warning(
                "[SlackChannel] nothing posted under %s=%s, the turn's %s call"
                " was refused (%s): channel=%s session=%s chars=%d",
                KEY_OPTIONAL_REPLY_MODE,
                OPTIONAL_REPLY_POST_TOOL,
                POST_MESSAGE,
                calls.refused,
                channel_id or "-",
                session_id,
                len(content),
            )
            return
        logger.info(
            "[SlackChannel] final text not posted under %s=%s, the turn called"
            " %s: channel=%s session=%s chars=%d",
            KEY_OPTIONAL_REPLY_MODE,
            OPTIONAL_REPLY_POST_TOOL,
            POST_MESSAGE,
            channel_id or "-",
            session_id,
            len(content),
        )

    def _group_chat_mode(self) -> str:
        return settled_word(
            "group_chat_mode",
            self.config.group_chat_mode or "",
            GROUP_CHAT_MODES,
            GROUP_MODE_MENTION,
        )

    def _channel_triggers(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> frozenset[str]:
        return channel_triggers(
            self.config,
            channel_id,
            group_chat_mode=self._group_chat_mode(),
            user_id=user_id,
            chat_type=chat_type,
            workspace=self._identity.team_id,
        )

    def _channel_prompt(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> str:
        """Resolve the standing prompt for this conversation."""
        prompt = self._settled(channel_id, user_id, chat_type).prompt
        return prompt if prompt is not None else ""

    def _fold_inbound_events(self, channel_id: str) -> str:
        """Return and clear buffered events. Existing events still fold after policy changes."""
        return inbound_events_block(self._inbound_events.fold(channel_id))

    def _channel_event_disposition(
        self, channel_id: str, family: str, chat_type: str = ""
    ) -> str:
        """Resolve event delivery for the conversation. An event actor is not a message sender."""
        return event_disposition(
            self._settled(channel_id, chat_type=chat_type).events, family
        )

    def _channel_reply_is_optional(self, channel_id: str, chat_type: str = "") -> bool:
        """Check whether this conversation permits a silent reply."""
        # Before the settled word, so the refusal cannot be read as one more
        # thing the key decides.
        if channel_id.startswith("D"):
            return False
        return self._settled(channel_id, chat_type=chat_type).reply == REPLY_OPTIONAL

    def _reply_was_optional(self, msg: Message, channel_id: str) -> bool:
        """Honor silence only when current policy and request metadata both permit it.

        Addressed turns can still use the silence token. Missing metadata denies it."""
        metadata = msg.metadata or {}
        # The kind of conversation this answer came from, off the reply's own
        # metadata, because ``delivery.reply`` may now be addressed on it and
        # the outbound path has no event to read. The inbound half stamped it
        # under the key the history toolkit already reads, so this is the same
        # fact rather than a second record of it, and a reply that arrives
        # without the key reads as no kind -- which withholds the licence and
        # posts the text, the direction the paragraph above keeps every failure
        # pointing in.
        if not self._channel_reply_is_optional(
            channel_id, str(metadata.get("slack_channel_type") or "")
        ):
            return False
        return metadata.get(REPLY_OPTIONAL_KEY) is True

    async def _discard_streamed_preview(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> None:
        """Delete a withheld reply preview after closing its stream.

        A failed delete leaves a visible preview and logs the failure."""
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id or self._client is None:
            return
        preview_ts, native = await self._close_stream(msg, channel_id, thread_ts)
        if not preview_ts:
            # No preview, or one whose surface had already failed -- in which
            # case ``_close_stream`` has warned about an abandoned message and
            # this has no handle to act on.
            return
        # Closing the session leaves a native stream open, and Slack refuses
        # chat.delete on an open stream with cant_delete_message. A stop that
        # fails still tries the delete: Slack may have ended the stream itself.
        stopped = native is None or await native.stop_stream(preview_ts)
        try:
            await self._client.chat_delete(channel=channel_id, ts=preview_ts)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[SlackChannel] the withheld reply's preview could not be"
                " deleted and is still on screen: channel=%s ts=%s reason=%s"
                " stream_stop=%s",
                channel_id,
                preview_ts,
                slack_failure_reason(exc),
                "-" if native is None else "ok" if stopped else native.error,
            )
            return
        logger.info(
            "[SlackChannel] withheld reply's preview deleted: channel=%s ts=%s",
            channel_id,
            preview_ts,
        )

    def _withhold_on_no_reply(
        self,
        msg: Message,
        content: str,
        routing_target: RoutingTarget | None,
    ) -> bool:
        """Withhold the reply this conversation's optional-reply mode withholds.

        Under ``post_tool`` that is the whole of the text, because the model
        reaches the conversation by calling the posting tool and the connector
        posts nothing of its own. Under the sentinel words it is an exact
        silence token and nothing else.
        """
        channel_id, _ = self._extract_delivery(msg, routing_target)
        if not self._reply_was_optional(msg, channel_id):
            return False
        if self._optional_reply_mode() == OPTIONAL_REPLY_POST_TOOL:
            self._note_unposted_turn(msg, channel_id, content)
            return True
        session_id = str(msg.session_id or "") or "-"
        if is_no_reply_sentinel(content):
            logger.info(
                "[SlackChannel] nothing posted, the turn wrote %s: channel=%s"
                " session=%s addressed=%s",
                NO_REPLY_SENTINEL,
                channel_id or "-",
                session_id,
                "yes" if (msg.metadata or {}).get(SLACK_ADDRESSED_KEY) else "no",
            )
            return True
        if looks_like_a_declined_reply(content):
            logger.warning(
                "[SlackChannel] %s near miss, reply delivered in full:"
                " channel=%s session=%s reply=%r",
                NO_REPLY_SENTINEL,
                channel_id or "-",
                session_id,
                content,
            )
        return False




    def set_brain_ingest_hook(self, hook: Callable[..., Any] | None) -> None:
        """Install the AgentServer ingest callback."""
        self._brain_ingest_hook = hook

    def bind_agent_client(self, agent_client: Any) -> None:
        from belle_agent.integrations.jiuwenswarm.second_brain_gateway import wire_brain_ingest_hook

        wire_brain_ingest_hook(self, agent_client)

    async def _handle_question_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        """Acknowledge a question-button click and submit the selected answer."""
        try:
            await self._route_question_action(ack, body, action)
        except Exception:
            logger.exception(
                "[SlackChannel] a question click raised: %s",
                click_identity(body, action),
            )

    async def _route_question_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        await ack()
        if not self._running:
            return
        if not isinstance(body, dict) or not isinstance(action, dict):
            return

        action_id = str(action.get("action_id") or "")
        if action_id.startswith(_QUESTION_INPUT_ACTION_ID_PREFIX):
            # Input changes do not submit an answer.
            logger.debug(
                "Slack question input changed, waiting for submit: action_id=%s",
                action_id,
            )
            return

        decoded = slack_questions.decode_button_value(action, logger)
        if decoded is None:
            return
        request_id = str(decoded.get("request_id") or "").strip()

        user_id = str((body.get("user") or {}).get("id") or "").strip()
        if user_id and not self.is_allowed(user_id):
            # Seeing a question does not grant permission to answer it.
            logger.warning(
                "Slack answer refused from a user outside allow_from: "
                "user=%s request_id=%s",
                user_id,
                request_id,
            )
            return

        chat_id, _clicked_ts = click_target(body)
        verdict = self._clicks_verdict(CLICK_APPROVE, chat_id, user_id)
        if verdict is not None and not verdict[0]:
            # Leave the question open for an authorized clicker.
            logger.warning(
                "Slack answer refused by a clicks rule: user=%s chat=%s "
                "request_id=%s reason=%s",
                user_id or "-",
                chat_id or "-",
                request_id,
                verdict[1],
            )
            await self._notify_clicker(
                body, user_id, _ANSWER_REFUSED_NOTICE, notice="answer-refused"
            )
            return

        pending = self._pending_questions.get(request_id)
        if pending is not None and pending.withdrawn_at is not None:
            logger.warning(
                "Slack answer ignored for a question withdrawn %.0fs earlier: "
                "request_id=%s source=%s user=%s",
                max(time.monotonic() - pending.withdrawn_at, 0.0),
                request_id,
                pending.source or "-",
                user_id,
            )
            await self._close_question_message(body, note=_WITHDRAWN_QUESTION_NOTE)
            return

        if pending is None:
            # A stale click must not approve a request twice.
            logger.info(
                "Slack answer ignored for a question that is no longer waiting: "
                "request_id=%s user=%s",
                request_id,
                user_id,
            )
            await self._close_question_message(body, note=_WITHDRAWN_QUESTION_NOTE)
            return

        if pending.inputs:
            values, label, problem = slack_questions.read_submitted_inputs(
                pending.inputs, body, request_id=pending.request_id,
                action_id_prefix=_QUESTION_INPUT_ACTION_ID_PREFIX, logger=logger,
            )
            if problem:
                # Keep the question open until all required inputs are present.
                logger.info(
                    "Slack submit refused as incomplete: request_id=%s user=%s "
                    "reason=%s",
                    request_id,
                    user_id,
                    problem,
                )
                await self._notify_clicker(
                    body, user_id, f"{_INCOMPLETE_SUBMIT_NOTICE} {problem}",
                    notice="incomplete submit",
                )
                return
        elif action_id == _QUESTION_SUBMIT_ACTION_ID:
            # A submit without inputs is a replay or malformed payload.
            logger.warning(
                "Slack submit ignored for a question with nothing to read: "
                "request_id=%s user=%s",
                request_id,
                user_id,
            )
            return
        else:
            label, value = slack_questions.resolve_answer(pending.labels, pending.values, decoded, action)
            values = [value]

        # Claim before awaiting so concurrent clicks cannot answer twice.
        self._pending_questions.pop(request_id, None)
        await self._close_question_message(
            body, note=slack_questions.answered_note(label, user_id)
        )
        await self._dispatch_answer(pending, values=values, body=body, user_id=user_id)

    async def _handle_stop_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        """Acknowledge a stop-button click and check permission before stopping."""
        try:
            await self._route_stop_action(ack, body, action)
        except Exception:
            logger.exception(
                "[SlackChannel] a stop click raised: %s",
                click_identity(body, action),
            )

    async def _route_stop_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        await ack()
        if not self._running:
            return
        if not isinstance(body, dict) or not isinstance(action, dict):
            return

        decoded = self._decode_stop_value(action)
        if decoded is None:
            return
        session_id, request_id, channel_id = decoded

        user = body.get("user")
        user_id = str(
            (user.get("id") if isinstance(user, Mapping) else "") or ""
        ).strip()

        permitted, ground = self._may_stop(session_id, user_id, channel_id)
        if not permitted:
            logger.warning(
                "Slack stop refused: user=%s session_id=%s request_id=%s "
                "ground=%s",
                user_id or "-",
                session_id,
                request_id,
                ground,
            )
            await self._notify_clicker(
                body, user_id, _STOP_REFUSED_NOTICE, notice="stop-refused"
            )
            return

        record = await self._claim_stop(request_id, channel_id)
        if record is None:
            logger.info(
                "Slack stop ignored for a turn that is no longer running: "
                "user=%s session_id=%s request_id=%s",
                user_id or "-",
                session_id,
                request_id,
            )
            await self._notify_clicker(
                body, user_id, _STOP_STALE_NOTICE, notice="stop-stale"
            )
            return

        logger.info(
            "[SlackChannel] stopping a turn: session_id=%s request_id=%s "
            "user=%s ground=%s",
            session_id,
            request_id,
            user_id or "-",
            ground,
        )
        await self._dispatch_stop(
            body, record, session_id=session_id, user_id=user_id
        )
        await self._notify_clicker(
            body, user_id, _STOP_ACCEPTED_NOTICE, notice="stop-accepted"
        )
        await self._close_stopped_turn(record, session_id, request_id)

    def _clicks_verdict(
        self, kind: str, channel_id: str, user_id: str
    ) -> tuple[bool, str] | None:
        """Return the matching click rule verdict, or None when no rule matches.

        A matching rule rejects a click without a user ID."""
        rule = click_rule(
            self.config.scopes,
            channel="slack",
            chat=channel_id or None,
            kind=kind,
        )
        if rule is None:
            return None
        ground = f"clicks.{kind}={rule.describe()}"
        if rule.permits(user_id):
            return True, ground
        if not user_id:
            return False, f"{ground} and the payload named no clicker"
        return False, ground

    def _may_stop(
        self, session_id: str, user_id: str, channel_id: str = ""
    ) -> tuple[bool, str]:
        """Check whether a user may stop a turn and return the reason."""
        initiator = self.turn_initiator(session_id)
        if initiator is not None and user_id and initiator.user_id == user_id:
            return True, "starter"
        if not self.is_allowed(user_id):
            return False, "allow_from"
        verdict = self._clicks_verdict(CLICK_STOP, channel_id, user_id)
        if verdict is None:
            return True, "allow_from"
        return verdict

    @staticmethod
    def _decode_stop_value(action: Mapping[str, Any]) -> tuple[str, str, str] | None:
        """Decode the session, request and channel IDs from a stop button."""
        return decode_stop_value(action, logger)

    async def _handle_agent_session_stopped(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        """Take a click on Slack's own stop button and report what it did."""
        await self._route_and_log(
            self._route_agent_session_stopped,
            event,
            body,
            family=str((event or {}).get("type") or "-"),
        )

    async def _route_agent_session_stopped(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> str:
        """Handle Slack native stop events for the current turn."""
        if not self._running:
            return "dropped:channel-not-running"
        if not isinstance(event, dict) or not isinstance(body, dict):
            return "dropped:malformed-payload"
        channel_id = str(event.get("channel") or "").strip()
        thread_ts = str(event.get("thread_ts") or "").strip()
        if not channel_id or not thread_ts:
            return "dropped:no-thread-on-event"
        user_id = str(event.get("user") or "").strip()
        streamed = self._streaming_message_ts(event)

        candidates = self._native_stop_candidates(channel_id, thread_ts)
        if not candidates:
            # Slack has drawn this thread: Slack named it, and it draws the
            # native stop button off a status it already holds there. That
            # status is what this takes down.
            await self._clear_thinking_status(
                SlackMessageOrigin(
                    channel_id=channel_id,
                    reply_thread_ts=thread_ts,
                    root_thread_ts=thread_ts,
                )
            )
            logger.info(
                "[SlackChannel] a native stop named a thread with no running "
                "turn: channel=%s thread_ts=%s streamed=%s",
                channel_id,
                thread_ts,
                ",".join(streamed) or "-",
            )
            return "ignored:no-running-turn"

        stopped = 0
        for session_id, entry in candidates:
            permitted, ground = self._may_stop(session_id, user_id, channel_id)
            record = await self._claim_stop_for_turn(session_id, entry, channel_id)
            if record is None:
                continue
            logger.info(
                "[SlackChannel] stopping a turn through Slack's own button: "
                "session_id=%s request_id=%s user=%s ground=%s permitted=%s "
                "streamed=%s",
                session_id,
                record.request_id,
                user_id or "-",
                ground,
                "yes" if permitted else "no",
                ",".join(streamed) or "-",
            )
            await self._dispatch_stop(
                body,
                record,
                session_id=session_id,
                user_id=user_id,
            )
            await self._close_stopped_turn(record, session_id, record.request_id)
            stopped += 1
        if not stopped:
            return "ignored:stop-already-claimed"
        return f"stopped:native-button-{stopped}"

    @staticmethod
    def _streaming_message_ts(event: Mapping[str, Any]) -> tuple[str, ...]:
        """Collect message timestamps from a native stop event."""
        raw = event.get("streaming_message_ts")
        if isinstance(raw, str):
            return (raw.strip(),) if raw.strip() else ()
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(str(item).strip() for item in raw if str(item or "").strip())

    def _native_stop_candidates(
        self, channel_id: str, thread_ts: str
    ) -> list[tuple[str, SlackTurnInitiator]]:
        """Find running turns that could match a native stop event."""
        return [
            (session_id, entry)
            for session_id, entry in tuple(self._admission.turn_initiators.items())
            if entry.channel_id == channel_id
            and entry.root_thread_ts == thread_ts
            and self.turn_initiator(session_id) is entry
        ]

    async def _claim_stop(
        self, request_id: str, channel_id: str
    ) -> _SlackActivityRecord | None:
        """Claim a running turn for a stop request."""
        key = (request_id, channel_id)
        async with self._activity_lock:
            record = self._activity_records.get(key)
            if record is None or record.closed or record.stop_requested:
                return None
            record.stop_requested = True
            record.touched_at = time.monotonic()
            return record

    async def _claim_stop_for_turn(
        self, session_id: str, entry: SlackTurnInitiator, channel_id: str
    ) -> _SlackActivityRecord | None:
        """Claim a stop only when the request still owns the turn."""
        request_id = self._activity_request_aliases.get(
            entry.request_id, entry.request_id
        )
        async with self._activity_lock:
            if self.turn_initiator(session_id) is not entry:
                return None
            key = (request_id, channel_id)
            record = self._activity_records.get(key)
            if record is None:
                record = self._activity_state.record(
                    key,
                    # The card is a message, so it takes the reply target the
                    # turn's own answer takes.
                    thread_ts=entry.reply_thread_ts,
                    session_id=session_id,
                )
            elif record.stop_requested or (
                record.session_id and record.session_id != session_id
            ):
                return None
            elif record.closed:
                record.reopen()
            record.stop_requested = True
            record.touched_at = time.monotonic()
            return record

    async def _settle_stopped_card(
        self, record: _SlackActivityRecord
    ) -> None:
        """Update the activity card after a turn stops."""
        async with self._activity_lock:
            if record.closed:
                return
            record.settle(stopped=True)
            if not record.message_ts:
                # Nothing on screen to settle. Only reachable if the card was
                # never posted, which the button being pressed argues against --
                # it is rendered on the card and nowhere else -- so this is
                # belt-and-braces rather than a case with a story.
                return
            key = (record.request_id, record.channel_id)
            reason = self._discard_reason(record, withheld=False)
            if reason and await self._discard_activity_card(
                key, record, reason=reason
            ):
                self._activity_records[key] = record
                return
            await self._refresh_activity_card(record, force=True)

    def _stream_keys_for(
        self, record: _SlackActivityRecord
    ) -> list[tuple[str, str, str]]:
        """Find stream keys associated with the stopped turn."""
        return [
            key
            for key in self._streams
            if key[1] == record.channel_id
            and self._activity_request_aliases.get(key[0], key[0])
            == record.request_id
        ]

    async def _close_stopped_turn(
        self, record: _SlackActivityRecord, session_id: str, request_id: str
    ) -> None:
        """Close a stopped turn and release its tracked state."""
        await self._settle_stopped_card(record)
        await self._stop_open_stream(record)
        await self._release_stopped_turn(session_id, request_id)

    async def _stop_open_stream(self, record: _SlackActivityRecord) -> None:
        """Close the stream associated with a stopped activity card."""
        if not record.stopped:
            return
        async with self._stream_lock:
            keys = self._stream_keys_for(record)
            streams = [(key, self._streams.pop(key)) for key in keys]
        for key, stream in streams:
            await self._finish_stopped_stream(stream, key[1])

    async def _release_stopped_turn(
        self, session_id: str, request_id: str
    ) -> None:
        """Retire a stopped turn and release any queued request."""
        entry = self._admission.turn_initiators.get(session_id)
        if entry is not None:
            folded = self._activity_request_aliases.get(
                entry.request_id, entry.request_id
            )
            if folded == request_id:
                # Written from the entry before it is retired, which is the
                # only reason this can be marked at all: a stopped turn sends
                # no terminal event, so the coordinates the terminal branch
                # would have used are about to be dropped here instead. The
                # same is true of the thinking status: a stopped turn reaches
                # ``send()``'s terminal branch no more than it reaches the
                # mark, so this is the only point left to clear the one it
                # opened.
                await self._mark_turn_ended(entry, MARK_STOPPED)
                await self._clear_thinking_status(entry.origin)
                self._forget_turn_initiator(session_id, entry.request_id)
                logger.info(
                    "[SlackChannel] turn initiator retired by a stop: "
                    "session_id=%s request_id=%s",
                    session_id,
                    request_id,
                )
        # Attempted whatever the entry said, and a no-op while the session is
        # busy: with a newer turn now running, the drain reads it and stands
        # aside, which is the same answer it gives on every other path.
        await self._drain_queued_messages(session_id)

    async def _end_superseded_turn(
        self,
        superseded: SlackTurnInitiator | None,
        channel_id: str,
        request_id: str,
    ) -> None:
        """Settle the turn that a new request cancelled."""
        if superseded is None or superseded.request_id == request_id:
            return
        # Marked before the record lookup below and never behind it. The card
        # is a display feature an operator can switch off and one a short turn
        # never reaches, and the halted turn's message needs its mark either
        # way -- the same reason the terminal branch settles the initiator
        # apart from the card. The entry itself is not retired here: the
        # dispatch overwrote it a few lines above this method's caller, so it
        # already names the turn that replaced this one.
        await self._mark_turn_ended(superseded, MARK_STOPPED)
        # A superseded turn reaches ``send()``'s terminal branch no more than
        # a stopped one does: the message that cancelled it is what is being
        # dispatched here, not a terminal event for the turn it replaced. This
        # is the only remaining point that can close the status the cancelled
        # turn's acknowledgement opened.
        await self._clear_thinking_status(superseded.origin)
        folded = self._activity_request_aliases.get(
            superseded.request_id, superseded.request_id
        )
        record = self._activity_records.get((folded, channel_id))
        if record is None or record.closed:
            return
        logger.info(
            "[SlackChannel] ending a turn the next message cancels: "
            "request_id=%s channel=%s",
            folded,
            channel_id,
        )
        await self._settle_stopped_card(record)
        await self._stop_open_stream(record)

    async def _dispatch_stop(
        self,
        body: Mapping[str, Any],
        record: _SlackActivityRecord,
        *,
        session_id: str,
        user_id: str,
    ) -> None:
        """Send a stop request for the turn that ``record`` claims.

        If any step of the stop raises, release the claim so that a later
        stop can try again, then raise the error."""
        try:
            channel_id = record.channel_id
            req = self._request(
                f"slack-stop-{int(time.time() * 1000)}",
                session_id=session_id,
                chat_id=channel_id,
                user_id=user_id,
                req_method=ReqMethod.CHAT_CANCEL,
                params={"intent": "cancel", "session_id": session_id},
                metadata={
                    "user_id": user_id,
                    "slack_team_id": event_team_id(body),
                    "slack_channel_id": channel_id,
                    # A click has no channel_type. Preserve unknown room kinds.
                    "slack_channel_type": self._conversation_chat_type(channel_id),
                    "slack_user_id": user_id,
                },
            )
            await self._route_request(req)
        except BaseException:
            record.stop_requested = False
            raise

    async def _dispatch_answer(
        self,
        pending: _SlackPendingQuestion,
        *,
        values: list[str],
        body: Mapping[str, Any],
        user_id: str,
    ) -> None:
        """Send a Slack answer to the waiting session."""
        params: dict[str, Any] = {
            "request_id": pending.request_id,
            "answers": [{
                "question": pending.question,
                "selected_options": list(values),
                "custom_input": "",
            }],
            "source": pending.source,
        }
        resumes_a_paused_turn = pending.source in _INTERRUPT_RESUME_SOURCES
        if resumes_a_paused_turn:
            params["query"] = ""
            params["supports_user_interaction"] = True

        container = body.get("container")
        container = container if isinstance(container, Mapping) else {}
        message = body.get("message")
        message = message if isinstance(message, Mapping) else {}
        channel_id = click_channel_id(body)
        thread_ts = str(
            message.get("thread_ts") or container.get("thread_ts") or ""
        ).strip()
        team_id = event_team_id(body)
        message_ts = str(container.get("message_ts") or "")
        action_ts = message_ts.strip() if message_ts else str(time.time())
        initiator = pending.initiator
        original_user = initiator.user_id if initiator else ""
        original_kind = initiator.chat_type if initiator else ""

        if resumes_a_paused_turn:
            # Preserve the original turn's model on resume.
            model_name = (
                initiator.model_name
                if original_user
                else self._channel_model_name(
                    channel_id, original_user, original_kind
                )
            )
            if model_name:
                params["model_name"] = model_name
            params.update(slack_agent_request_params(
                self.config, channel_id,
                user_id=original_user,
                chat_type=original_kind or self._conversation_chat_type(channel_id),
                workspace=self._identity.team_id,
            ))

        self._remember_stream_recipient(pending.session_id, user_id, team_id)

        conversation_chat_type = original_kind or (
            self._conversation_chat_type(channel_id)
        )

        metadata: dict[str, Any] = {
            "user_id": user_id,
            "slack_team_id": team_id,
            "slack_channel_id": channel_id,
            "slack_channel_type": conversation_chat_type or "channel",
            "slack_user_id": user_id,
            "slack_message_ts": message_ts,
            "slack_thread_ts": thread_ts,
            "message_ts": message_ts,
            **slack_audience_request_metadata(),
        }
        if resumes_a_paused_turn and initiator is not None:
            # Preserve silence and root-routing facts across the pause.
            if initiator.silence_is_honoured:
                self._mark_reply_is_optional(metadata)
            if initiator.addressed:
                metadata[SLACK_ADDRESSED_KEY] = True
            if initiator.post_as_root:
                metadata[POST_AS_ROOT_KEY] = True
            if initiator.trigger:
                metadata["slack_trigger"] = initiator.trigger

        req = self._request(
            f"slack-answer-{pending.request_id}-{action_ts}",
            session_id=pending.session_id,
            chat_id=channel_id,
            user_id=user_id,
            req_method=(
                ReqMethod.CHAT_SEND if resumes_a_paused_turn else ReqMethod.CHAT_ANSWER
            ),
            params=params,
            metadata=metadata,
            is_stream=resumes_a_paused_turn,
        )

        if resumes_a_paused_turn:
            self._remember_activity_alias(req.id, pending.turn_request_id)
            initiator_is_dm = initiator.is_dm if initiator else channel_id.startswith("D")
            self._remember_turn_initiator(
                pending.session_id,
                original_user or user_id,
                req.id,
                model_name=str(req.params.get("model_name") or ""),
                is_dm=initiator_is_dm,
                chat_type=conversation_chat_type,
                origin=initiator.origin if initiator else None,
                request_metadata=req.metadata,
            )

        logger.info(
            "[SlackChannel] answered question: request_id=%s source=%s method=%s "
            "session_id=%s user=%s",
            pending.request_id,
            pending.source or "-",
            req.req_method.value,
            pending.session_id,
            user_id,
        )

        await self._route_request(req)


    async def _apply_avatar_context(
        self,
        metadata: dict[str, Any],
        *,
        is_dm: bool,
        channel_id: str,
        user_id: str,
        text: str,
        message_ts: str,
    ) -> bool:
        """Add avatar metadata to channel messages when the operator enables it."""
        adapter = self._im_platform_adapter
        if not self.config.group_digital_avatar or adapter is None or is_dm:
            return False

        await adapter.observe_message(
            channel_id=channel_id,
            channel_type=str(metadata.get("slack_channel_type") or "channel"),
            user_id=user_id,
            text=text,
            message_ts=message_ts,
        )

        metadata["im_platform"] = "slack"
        # Inbound and outbound pipelines read different chat-type keys.
        metadata["chat_type"] = "group"
        metadata["im_chat_type"] = "group"
        metadata["im_sender_user_id"] = user_id
        # Avatar relevance uses channel history across threads.
        metadata["im_thread_id"] = channel_id
        metadata["timestamp_ms"] = _slack_timestamp_ms(message_ts)
        mentioned_user_ids = _mentioned_user_ids(text)
        if mentioned_user_ids:
            metadata["im_mentioned_user_ids"] = mentioned_user_ids
        metadata["avatar_mode"] = True
        metadata["principal_user_id"] = self.config.my_user_id.strip()
        metadata["triggering_user_id"] = user_id
        return True

    async def _load_bot_user_id(self) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.auth_test()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack auth.test failed; bot mentions may be duplicated: %s", exc
            )
            return

        # auth.test supplies the scopes before the first event arrives.
        self._granted_scopes = slack_granted_scopes(getattr(response, "headers", None))

        self._identity.learn_auth(response)
        if not self._identity.bot_user_id:
            logger.warning("Slack auth.test returned no bot user_id")
        if not self._identity.team_id:
            logger.warning(
                "Slack auth.test returned no team_id; with more than one"
                " workspace configured this connection cannot be matched to"
                " the replies meant for it"
            )
        if not self._identity.host:
            logger.info(
                "Slack auth.test returned no workspace url; inbound file"
                " downloads will accept %s only",
                SLACK_FILE_HOST,
            )
        self._publish_bot_user_id()

    def _scopes_permit_start(self) -> bool:
        """Check startup scopes against the features this connector requires."""
        granted = self._granted_scopes
        label = self._identity.label(self.config.app_id)
        if granted is None:
            logger.info(
                "Slack scopes could not be checked%s: auth.test returned no %s;"
                " missing grants will surface when used",
                label,
                GRANTED_SCOPES_HEADER,
            )
            return True

        required = slack_required_scopes()
        absent_core = sorted(required - granted)
        if absent_core:
            logger.error(
                "SlackChannel%s lacks %s; it cannot receive or answer messages."
                " Reinstall with these scopes; other channels are unaffected",
                label,
                ", ".join(absent_core),
            )
            return False

        # The gated tier also needs Slack's agent feature, so check only tools.
        reasons = scope_reasons(TIER_TOOLS)
        absent = sorted(
            scope for scope in scopes_for_tier(TIER_TOOLS) if scope not in granted
        )
        if absent:
            logger.warning(
                "SlackChannel%s lacks %s; affected features may fail: %s",
                label,
                ", ".join(absent),
                "; ".join(
                    f"{scope} -> {', '.join(reasons.get(scope) or ['no call here needs it'])}"
                    for scope in absent
                ),
            )
        else:
            logger.info(
                "SlackChannel%s holds every scope this connector asks for", label
            )
        return True

    def _remember_bot_user_id(self, body: dict[str, Any]) -> None:
        if self._identity.remember_authorization(body):
            self._publish_bot_user_id()

    def _learn_chat_type(self, event: Mapping[str, Any] | None) -> None:
        """Record the latest valid conversation type, including ignored events."""
        learn_chat_type(self._chat_types, event, known=SLACK_CHAT_TYPES)

    def _conversation_chat_type(
        self, chat_id: str, event: Mapping[str, Any] | None = None
    ) -> str:
        """Return Slack's known conversation type, or an empty string."""
        return conversation_chat_type(self._chat_types, chat_id, event, known=SLACK_CHAT_TYPES)

    def _publish_bot_user_id(self) -> None:
        """Give the avatar adapter the bot ID when known."""
        adapter = self._im_platform_adapter
        if adapter is None or not self._identity.bot_user_id:
            return
        setter = getattr(adapter, "set_bot_user_id", None)
        if callable(setter):
            setter(self._identity.bot_user_id)

    def _slash_command(self) -> str:
        """Return the currently configured slash-command token."""
        return command_token(
            normalise_command_name(self.config.command_name)
            or DEFAULT_COMMAND_NAME
        )

    def _bot_display_name(self) -> str:
        """Return the configured name or the connector default."""
        return resolve_bot_name(self.config.bot_name)

    async def _explain_queue_refusal(self, channel_id: str, user_id: str) -> None:
        """Tell the sender that the queue is full unless acknowledgements are off."""
        if self._acknowledge_mode() == ACK_MODE_OFF:
            return
        await self._post_ephemeral(
            channel_id, user_id, _QUEUE_FULL_NOTICE, notice="queue-full"
        )

    def _slack_file_hosts(self) -> frozenset[str]:
        """Allow the Slack file host and the host returned by auth.test."""
        if self._workspace_host:
            return frozenset({SLACK_FILE_HOST, self._workspace_host})
        return frozenset({SLACK_FILE_HOST})

    async def _download_attachment(
        self, file_info: Mapping[str, Any], session_id: str
    ) -> dict[str, Any]:
        upload_dir = get_agent_sessions_dir() / safe_path_component(session_id, "default") / "uploads"
        return await download_attachment(
            self._client,
            file_info,
            upload_dir,
            bot_token=self.config.bot_token.strip(),
            allowed_hosts=self._slack_file_hosts(),
            max_bytes=self.config.file_download_max_bytes,
        )

    async def _download_attachments(
        self, files: list[dict[str, Any]], session_id: str
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Fetch files within one deadline. Return records and per-file failures."""
        return await download_attachments(
            files,
            lambda file_info: self._download_attachment(file_info, session_id),
            budget_seconds=_ATTACHMENT_PHASE_TIMEOUT_SECONDS,
            logger=logger,
        )

    async def _report_download_failures(
        self, channel_id: str, thread_ts: str, failures: list[tuple[str, str]]
    ) -> None:
        """Post one notice per distinct reason, not one per file."""
        grouped: dict[str, list[str]] = {}
        for name, reason in failures:
            grouped.setdefault(reason, []).append(name)
        for reason, names in grouped.items():
            await self._report_unreadable_files(channel_id, thread_ts, names, reason)

    async def _report_unreadable_files(
        self,
        channel_id: str,
        thread_ts: str,
        names: list[str],
        reason: str,
    ) -> None:
        """Notify the channel of unreadable files even when acknowledgements are off."""
        if self._client is None or not channel_id or not names:
            return

        text = unreadable_file_notice(names, reason)

        kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            await self._client.chat_postMessage(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack attachment notice failed: %s", exc)

    async def _remember_event(self, event_id: str) -> bool:
        """Deduplicate Slack events across restarts."""
        return await self._dedup.remember(event_id)

    @staticmethod
    def resolve_delivery(
        *,
        delivery: Any = None,
        metadata: Mapping[str, Any] | None = None,
        session_id: Any = None,
        default_channel_id: Any = "",
    ) -> tuple[str, str]:
        """Resolve channel and thread by target, metadata, session, then default."""
        meta = metadata or {}
        if isinstance(delivery, SlackDeliveryTarget):
            channel_id = str(delivery.target_channel_id or "").strip()
            if channel_id:
                return channel_id, str(delivery.thread_ts or "").strip()

        channel_id = str(meta.get("slack_channel_id") or "").strip()
        if channel_id:
            return channel_id, str(meta.get("slack_thread_ts") or "").strip()

        channel_id, parsed_thread = parse_slack_cron_session(session_id)
        if channel_id:
            return channel_id, parsed_thread

        return str(default_channel_id or "").strip(), ""

    @staticmethod
    def delivery_is_reachable(
        *,
        session_id: Any = None,
        default_channel_id: Any = "",
        metadata: Mapping[str, Any] | None = None,
        delivery: Any = None,
    ) -> bool:
        """Check whether the delivery rules resolve a channel."""
        channel_id, _thread_ts = SlackChannel.resolve_delivery(
            delivery=delivery,
            metadata=metadata,
            session_id=session_id,
            default_channel_id=default_channel_id,
        )
        return bool(channel_id)

    def _extract_delivery(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
    ) -> tuple[str, str]:
        metadata = msg.metadata or {}
        channel_id, thread_ts = SlackChannel.resolve_delivery(
            delivery=routing_target.delivery if routing_target is not None else None,
            metadata=metadata,
            session_id=msg.session_id,
            default_channel_id=self.config.default_channel_id,
        )
        if bool(metadata.get(POST_AS_ROOT_KEY)):
            return channel_id, ""
        return channel_id, thread_ts

    def _require_delivery(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
    ) -> tuple[str, str]:
        """Require a connected client and an outbound destination."""
        if self._client is None:
            raise SlackDeliveryError("Slack client is not connected")
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id:
            raise SlackDeliveryError(
                "no target channel resolved from the routing target, message "
                "metadata, session id, or channels.slack.default_channel_id"
            )
        return channel_id, thread_ts


    def _streaming_mode(self) -> str:
        """Resolve the configured streaming mode."""
        return resolve_streaming_mode(self.config.enable_streaming)

    def _streaming_enabled(self) -> bool:
        return resolve_streaming_enabled(self.config.enable_streaming)

    @staticmethod
    def _normalize_slack_mrkdwn(content: str) -> str:
        """Convert supported Markdown to Slack mrkdwn."""
        return normalize_slack_mrkdwn(content)

    def get_metadata(self) -> ChannelMetadata:
        metadata = ChannelMetadata(
            channel_id=self.channel_id,
            source="slack",
            extra={
                "default_channel_id": self.config.default_channel_id,
                "allowed_channel_ids": list(self.config.allowed_channel_ids),
                "reply_in_thread": self.config.reply_in_thread,
                "session": self.config.session,
            },
        )
        extra = metadata.extra
        assert extra is not None
        policy = belle_slack_policy()
        extra.update({
                KEY_ENFORCE_AUDIENCE: policy["enforce_audience"],
                "reply_in_thread": self._reply_in_thread(),
                "group_chat_mode": self._group_chat_mode(),
                "conversation_overrides": {
                    channel_id: {
                        key: value
                        for key, value in (
                            ("mode", sorted(override.mode) if override.mode is not None else None),
                            ("prompt", override.prompt),
                            ("model_name", override.model_name),
                            ("mid_turn", override.mid_turn),
                            ("session", override.session),
                        )
                        if value is not None
                    }
                    for channel_id, override in (self.config.conversation_overrides or {}).items()
                },
                "acknowledge_mode": self._acknowledge_mode(),
                "acknowledgement_emoji": self.config.acknowledgement_emoji,
                "rejected_emoji": self.config.rejected_emoji,
                "queued_emoji": self.config.queued_emoji,
                "completed_emoji": self.config.completed_emoji,
                "failed_emoji": self.config.failed_emoji,
                "stopped_emoji": self.config.stopped_emoji,
                "thinking_status": self.config.thinking_status,
                "queued_status": self.config.queued_status,
                "steered_status": self.config.steered_status,
                KEY_SESSION_SURFACE: self._session_surface(),
                "session_surface_in_use": self._status_surface(),
                # Keep user text out of diagnostics.
                "queued_messages": {
                    session_id: len(queue)
                    for session_id, queue in self._admission.queued_messages.items()
                    if queue
                },
                "enable_streaming": bool(self.config.enable_streaming),
                "blockkit_tables": self._blockkit_tables_mode(),
                "render_tables": self._render_tables_mode(),
        })
        return metadata
