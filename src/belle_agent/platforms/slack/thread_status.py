"""Slack thread-status fallback and request acknowledgements."""

import logging
from dataclasses import dataclass
from typing import Any

from belle_agent.platforms.base import (
    MARK_DROPPED,
    MARK_INGEST_DECLINED,
    MARK_INGEST_EMPTY,
    MARK_INGEST_FAILED,
    MARK_INGESTED,
    MARK_INGESTING,
    MARK_QUEUED,
    MARK_RECEIVED,
    MARK_REFUSED,
    MARK_STARTED,
)
from belle_agent.platforms.marks import react_marks
from belle_agent.platforms.slack.admission import SlackMessageOrigin, SlackQueuedMessage, SlackTurnInitiator
from belle_agent.platforms.slack.config import (
    ACK_MODE_BOTH,
    ACK_MODE_OFF,
    ACK_MODE_REACTION,
    ACK_MODE_TEXT,
    ACKNOWLEDGE_MODES,
    KEY_SESSION_SURFACE,
    SESSION_SURFACE_AGENTS,
    SESSION_SURFACE_ASSISTANT,
    SESSION_SURFACE_AUTO,
    SESSION_SURFACE_OFF,
    resolve_session_surface,
)
from belle_agent.platforms.slack.delivery import slack_error_code, slack_failure_reason
from belle_agent.platforms.slack.retry import TRANSIENT_ERRORS

logger = logging.getLogger(__name__)

_DEFAULT_QUEUED_STATUS = "has queued a message"
_DEFAULT_STEERED_STATUS = "has steered a message into the running turn"

# The agents API accepts lifecycle words and requires an explicit "active" call.
_ASSISTANT_SET_STATUS_METHOD = "assistant.threads.setStatus"
_AGENTS_SET_STATUS_METHOD = "agents.sessions.setStatus"

# A turn sets processing, then active. Closed would end the conversation.
AGENTS_STATUS_ACTIVE = "active"
AGENTS_STATUS_PROCESSING = "processing"

# Only known availability refusals switch to the assistant API. Unknown codes
# remain visible so a new Slack refusal does not silently change the API.
_AGENTS_UNAVAILABLE = frozenset(
    {
        "feature_disabled",
        "not_agent_app",
        "missing_scope",
        "not_authorized",
        "no_permission",
        "access_denied",
        "team_access_not_granted",
        "enterprise_is_restricted",
        "not_allowed_token_type",
    }
)

# Caller errors indicate a connector defect and must remain visible.
_AGENTS_CALLER_ERRORS = frozenset(
    {
        "invalid_arguments",
        "invalid_arg_name",
        "invalid_array_arg",
        "invalid_status",
        "thread_ts_required",
        "thread_ts_not_allowed",
        "invalid_charset",
        "invalid_form_data",
        "invalid_json",
        "invalid_post_type",
        "json_not_object",
        "missing_post_type",
    }
)

# Transient errors leave the API choice unchanged.
_AGENTS_TRANSIENT = TRANSIENT_ERRORS

# Both APIs use the same token, so fallback cannot fix token refusals.
_AGENTS_TOKEN_REFUSALS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_expired",
        "token_revoked",
        "account_inactive",
    }
)


@dataclass(frozen=True)
class _ThreadStatus:
    """Status text and lifecycle word for Slack's two thread-status APIs."""

    text: str
    lifecycle: str
    label: str = ""


class SlackThreadStatus:
    #: How the lines of the class that uses this mixin are tagged. A connector sets its own.
    _log_tag: str = "[slack]"

    def _customised_status_wording(self) -> tuple[str, ...]:
        return tuple(
            key
            for key, value, default in (
                ("queued_status", self.config.queued_status, _DEFAULT_QUEUED_STATUS),
                ("steered_status", self.config.steered_status, _DEFAULT_STEERED_STATUS),
            )
            if str(value or "").strip() not in ("", default)
        )

    def _log_session_surface(self) -> None:
        """Report custom status wording hidden by the session surface."""
        surface = self._session_surface()
        if surface in (SESSION_SURFACE_ASSISTANT, SESSION_SURFACE_OFF):
            return
        customised = self._customised_status_wording()
        if not customised:
            return
        named = " and ".join(f"Belle platforms.slack.{key}" for key in customised)
        verb = "is" if len(customised) == 1 else "are"
        if surface == SESSION_SURFACE_AUTO:
            logger.info(
                "%s%s Belle platforms.slack.session_surface=auto resolved to "
                "%s: %s %s set, and %s takes four fixed lifecycle words rather "
                "than configured text. Write session_surface: agents to use the "
                "newer API anyway",
                self._log_tag,
                self._identity.label(self.config.app_id),
                SESSION_SURFACE_ASSISTANT,
                named,
                verb,
                _AGENTS_SET_STATUS_METHOD,
            )
            return
        logger.warning(
            "%s%s Belle platforms.slack.session_surface=%s, so %s "
            "%s not drawn: %s takes four fixed words and the queued and "
            "steered states read the same in a thread. Write "
            "session_surface: assistant to keep the wording",
            self._log_tag,
            self._identity.label(self.config.app_id),
            surface,
            named,
            verb,
            _AGENTS_SET_STATUS_METHOD,
        )


    def _acknowledge_mode(self) -> str:
        mode = str(self.config.acknowledge_mode or "").strip().lower()
        if mode not in ACKNOWLEDGE_MODES:
            logger.warning(
                "Unknown Slack acknowledge_mode %r; falling back to %r",
                self.config.acknowledge_mode,
                ACK_MODE_REACTION,
            )
            return ACK_MODE_REACTION
        return mode

    async def _add_reaction(
        self, channel_id: str, timestamp: str, emoji: str
    ) -> Exception | None:
        return await self._reaction_marks.change(
            self._client, channel_id, timestamp, emoji
        )

    async def _react_for_state(self, channel_id: str, timestamp: str, state: str, *, queued_only: bool = False) -> None:
        brain = state in {MARK_INGESTING, MARK_INGESTED, MARK_INGEST_FAILED, MARK_INGEST_DECLINED, MARK_INGEST_EMPTY}
        if not brain and self._acknowledge_mode() not in (ACK_MODE_REACTION, ACK_MODE_BOTH):
            return

        async def change(conversation: str, message_id: str, name: str, remove: bool = False) -> None:
            await self._reaction_marks.change(self._client, conversation, message_id, name, remove=remove)

        await react_marks(
            change, channel_id, timestamp, state,
            lambda key: getattr(self.config, key, "") if not queued_only or key == "queued_emoji" else "",
        )

    def _session_surface(self) -> str:
        """Resolve the configured status surface, including manually built configs."""
        return resolve_session_surface(
            {KEY_SESSION_SURFACE: self.config.session_surface}
        )

    def _status_surface(self) -> str:
        """Select the status API. Preserve custom status wording under auto."""
        word = self._session_surface()
        if word in (SESSION_SURFACE_ASSISTANT, SESSION_SURFACE_OFF):
            return word
        if word == SESSION_SURFACE_AUTO and self._customised_status_wording():
            return SESSION_SURFACE_ASSISTANT
        return SESSION_SURFACE_AGENTS if self._agents_status_allowed else SESSION_SURFACE_ASSISTANT

    def _remember_agents_status(self, allowed: bool, reason: str) -> None:
        """Record agent-status availability and log changes once."""
        if self._agents_status_allowed == allowed:
            return
        self._agents_status_allowed = allowed
        logger.info(
            "%s%s thread status moves to %s: %s returned %r",
            self._log_tag,
            self._identity.label(self.config.app_id),
            _AGENTS_SET_STATUS_METHOD if allowed else _ASSISTANT_SET_STATUS_METHOD,
            _AGENTS_SET_STATUS_METHOD,
            reason,
        )

    @staticmethod
    def _agents_refusal_level(code: str, *, ends_turn: bool) -> int:
        """Warn for terminal or unclassified failures. Debug transient refusals."""
        if ends_turn or code in _AGENTS_CALLER_ERRORS:
            return logging.WARNING
        if code in _AGENTS_TRANSIENT or code in _AGENTS_TOKEN_REFUSALS:
            return logging.DEBUG
        return logging.WARNING

    async def _set_thread_status(
        self, origin: SlackMessageOrigin, status: _ThreadStatus
    ) -> None:
        """Set a thread status through the available API without failing the turn."""
        if not origin.channel_id or not origin.root_thread_ts or self._client is None:
            return
        # Both APIs move readers into empty threads.
        if origin.status_moves_the_reader:
            return
        surface = self._status_surface()
        if surface == SESSION_SURFACE_OFF:
            return
        if surface == SESSION_SURFACE_AGENTS and await self._set_agents_status(
            origin.channel_id, origin.root_thread_ts, status
        ):
            return
        await self._set_assistant_status(
            origin.channel_id, origin.root_thread_ts, status
        )

    async def _set_agents_status(
        self, channel_id: str, thread_ts: str, status: _ThreadStatus
    ) -> bool:
        """Return False only when Slack rejects the agent-status API as unavailable."""
        try:
            await self._client.api_call(
                _AGENTS_SET_STATUS_METHOD,
                data={
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "status": status.lifecycle,
                },
            )
        except Exception as exc:  # noqa: BLE001
            code = slack_error_code(exc)
            if code in _AGENTS_UNAVAILABLE:
                self._remember_agents_status(False, code)
                return False
            logger.log(
                self._agents_refusal_level(code, ends_turn=not status.text),
                "Slack %s%s failed: %s",
                _AGENTS_SET_STATUS_METHOD,
                f" ({status.label})" if status.label else "",
                slack_failure_reason(exc),
            )
        return True

    async def _set_assistant_status(
        self, channel_id: str, thread_ts: str, status: _ThreadStatus
    ) -> None:
        """Set assistant thread status. Slack clears it on post or timeout."""
        try:
            await self._client.api_call(
                _ASSISTANT_SET_STATUS_METHOD,
                data={
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "status": status.text,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Slack %s%s failed: %s",
                _ASSISTANT_SET_STATUS_METHOD,
                f" ({status.label})" if status.label else "",
                exc,
            )

    async def _set_configured_status(
        self, origin: SlackMessageOrigin, text: str, lifecycle: str
    ) -> None:
        text = str(text or "").strip()
        if text:
            await self._set_thread_status(origin, _ThreadStatus(text, lifecycle))

    async def _set_thinking_status(self, origin: SlackMessageOrigin) -> None:
        await self._set_configured_status(origin, self.config.thinking_status, AGENTS_STATUS_PROCESSING)

    async def _clear_thinking_status(self, origin: SlackMessageOrigin) -> None:
        await self._set_thread_status(origin, _ThreadStatus("", AGENTS_STATUS_ACTIVE, "clear"))

    async def _set_queued_status(self, origin: SlackMessageOrigin) -> None:
        await self._set_configured_status(origin, self.config.queued_status, AGENTS_STATUS_ACTIVE)

    async def _set_steered_status(self, origin: SlackMessageOrigin) -> None:
        await self._set_configured_status(origin, self.config.steered_status, AGENTS_STATUS_PROCESSING)

    async def _acknowledge_request(self, origin: SlackMessageOrigin, *, react: bool = True) -> None:
        """Acknowledge a request with the configured reaction, status and text."""
        mode = self._acknowledge_mode()
        if mode == ACK_MODE_OFF or self._client is None:
            return

        if react:
            await self._react_for_state(origin.channel_id, origin.message_ts, MARK_RECEIVED)

        await self._set_thinking_status(origin)

        if mode not in (ACK_MODE_TEXT, ACK_MODE_BOTH):
            return

        text = self.config.acknowledgement_text.strip()
        if not text:
            return

        kwargs: dict[str, Any] = {"channel": origin.channel_id, "text": text}
        if origin.reply_thread_ts:
            kwargs["thread_ts"] = origin.reply_thread_ts
        try:
            await self._client.chat_postMessage(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack acknowledgement failed: %s", exc)

    async def _reject_request(self, channel_id: str, message_ts: str) -> None:
        await self._react_for_state(channel_id, message_ts, MARK_REFUSED)

    async def _mark_queued(self, entry: SlackQueuedMessage, *, react: bool = True) -> None:
        """Mark a queued message when acknowledgements are enabled."""
        if react:
            await self._react_for_state(entry.channel_id, entry.message_ts, MARK_QUEUED, queued_only=True)
        if self._acknowledge_mode() != ACK_MODE_OFF:
            await self._set_queued_status(entry.origin)

    async def _unmark_queued(self, entry: SlackQueuedMessage) -> None:
        await self._react_for_state(entry.channel_id, entry.message_ts, MARK_STARTED)

    async def _unmark_dropped(
        self, entry: SlackQueuedMessage, *, queued_behind: bool, running: bool, react: bool = True
    ) -> None:
        """Update the thread status after dropping a queued message."""
        if react:
            await self._react_for_state(entry.channel_id, entry.message_ts, MARK_DROPPED)
        if self._acknowledge_mode() != ACK_MODE_OFF:
            # Keep the status until the thread's final queued turn ends.
            if queued_behind:
                await self._set_queued_status(entry.origin)
            elif running:
                await self._set_thinking_status(entry.origin)
            else:
                await self._clear_thinking_status(entry.origin)

    async def _mark_turn_ended(self, entry: SlackTurnInitiator, state: str) -> None:
        await self._react_for_state(entry.channel_id, entry.message_ts, state)
