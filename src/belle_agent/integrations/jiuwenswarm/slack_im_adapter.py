"""Expose cached Slack history to the synchronous IM pipeline."""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from jiuwenswarm.extensions.sdk.im_runtime import IMHistoryMessage

from belle_agent.platforms.slack.history_policy import (
    METADATA_ENFORCE_AUDIENCE_KEY,
)

PLATFORM_NAME = "Slack"
logger = logging.getLogger(__name__)

# metadata keys this adapter owns, mirroring the feishu/wecom pairs.
REPLY_CANDIDATE_USER_ID_KEY = "reply_candidate_slack_user_id"
REPLY_USER_ID_KEY = "reply_slack_user_id"

# Scan periodically for messages missed by the connector and for display names.
_DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0
_DEFAULT_HISTORY_HOURS = 24.0
# The inbound pipeline requests at most 500 messages by default.
_MAX_CACHED_MESSAGES = 500

# ``conversations.history`` is only meaningful for the channel types the
# toolkit itself accepts.
_HISTORY_CHANNEL_TYPES = ("channel", "group")


def _timestamp_ms(slack_ts: Any) -> int:
    """Convert a Slack ``ts`` ("1710000000.000100") to epoch milliseconds."""
    try:
        return int(float(slack_ts) * 1000)
    except (TypeError, ValueError):
        return 0


class SlackIMPlatformAdapter:
    """Adapter letting Slack take part in the group digital-avatar pipeline."""

    channel_id = "slack"

    def __init__(
        self,
        *,
        my_user_id: str = "",
        principal_name: str = "",
        bot_name: str = "",
        bot_user_id: str = "",
        history_hours: float = _DEFAULT_HISTORY_HOURS,
        refresh_interval_seconds: float = _DEFAULT_REFRESH_INTERVAL_SECONDS,
        toolkit_factory: Callable[[dict[str, Any]], Any] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._my_user_id = (my_user_id or "").strip()
        self._principal_name = (principal_name or "").strip()
        self._bot_name = (bot_name or "").strip()
        self._bot_user_id = (bot_user_id or "").strip()
        self._history_hours = history_hours
        self._refresh_interval_seconds = max(0.0, float(refresh_interval_seconds))
        self._toolkit_factory = toolkit_factory or self._build_toolkit
        self._now = now
        # channel id -> Slack timestamp -> history entry
        self._history: dict[str, dict[str, IMHistoryMessage]] = {}
        self._refreshed_at: dict[str, float] = {}
        self._user_names: dict[str, str] = {}

    # ------------------------------------------------------------------
    # connector-side (async) surface
    # ------------------------------------------------------------------

    def set_bot_user_id(self, bot_user_id: str) -> None:
        """Record the bot's own user id once ``auth.test`` has resolved it."""
        self._bot_user_id = (bot_user_id or "").strip()

    async def observe_message(
        self,
        *,
        channel_id: str,
        channel_type: str,
        user_id: str,
        text: str,
        message_ts: str,
    ) -> None:
        """Cache an inbound message even if the history refresh fails."""
        channel_id = (channel_id or "").strip()
        if not channel_id:
            return
        if channel_type in _HISTORY_CHANNEL_TYPES and self._refresh_due(channel_id):
            try:
                await self._refresh_history(channel_id, channel_type)
            except Exception as exc:  # noqa: BLE001 - best effort by contract
                logger.warning("[SlackIMAdapter] history refresh failed: %s", exc)
        self._remember_message(
            channel_id=channel_id,
            user_id=(user_id or "").strip(),
            text=text or "",
            message_ts=(message_ts or "").strip(),
        )

    def _refresh_due(self, channel_id: str) -> bool:
        last = self._refreshed_at.get(channel_id)
        return last is None or (
            self._now() - last >= self._refresh_interval_seconds
        )

    def _build_toolkit(self, metadata: dict[str, Any]) -> Any:
        from belle_agent.integrations.jiuwenswarm.slack_history import (
            SlackHistoryToolkit,
        )

        return SlackHistoryToolkit(metadata=metadata)

    async def _refresh_history(self, channel_id: str, channel_type: str) -> None:
        """Replace the cached snapshot for one channel from the Slack API."""
        # Stamp before the call so a failed scan does not retry immediately.
        self._refreshed_at[channel_id] = self._now()
        toolkit = self._toolkit_factory(
            {
                "slack_channel_id": channel_id,
                "slack_channel_type": channel_type,
                # Keep audience checks active if this read later gains a target.
                METADATA_ENFORCE_AUDIENCE_KEY: True,
            }
        )
        raw = await toolkit.read_slack_conversation(
            hours=self._history_hours,
            include_threads=True,
        )
        try:
            snapshot = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("[SlackIMAdapter] history snapshot is not JSON: %s", exc)
            return
        if not isinstance(snapshot, dict) or not snapshot.get("ok"):
            logger.info(
                "[SlackIMAdapter] history unavailable for %s: %s",
                channel_id,
                (snapshot or {}).get("error") if isinstance(snapshot, dict) else "",
            )
            return

        messages = snapshot.get("messages")
        if not isinstance(messages, list):
            return

        bucket = self._history.setdefault(channel_id, {})
        for item in messages:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("ts") or "").strip()
            if not ts:
                continue
            user_id = str(item.get("author_user_id") or "").strip()
            user_name = str(item.get("author_name") or "").strip()
            # Do not cache an unresolved ID as a display name.
            if user_id and user_name and user_name != user_id:
                self._user_names[user_id] = user_name
            bucket[ts] = IMHistoryMessage(
                user_id=user_id,
                user_name=user_name or self.resolve_user_display_name(user_id),
                content=str(item.get("text") or "").strip(),
                timestamp_ms=_timestamp_ms(ts),
            )
        self._trim(channel_id)

    def _remember_message(
        self, *, channel_id: str, user_id: str, text: str, message_ts: str
    ) -> None:
        if not message_ts or not text.strip():
            return
        bucket = self._history.setdefault(channel_id, {})
        # Resolve the name when read, after a later refresh may have learned it.
        bucket[message_ts] = IMHistoryMessage(
            user_id=user_id,
            user_name="",
            content=text.strip(),
            timestamp_ms=_timestamp_ms(message_ts),
        )
        self._trim(channel_id)

    def _trim(self, channel_id: str) -> None:
        bucket = self._history.get(channel_id)
        if not bucket or len(bucket) <= _MAX_CACHED_MESSAGES:
            return
        keep = sorted(bucket.items(), key=lambda pair: pair[1].timestamp_ms)[
            -_MAX_CACHED_MESSAGES:
        ]
        self._history[channel_id] = dict(keep)

    # ------------------------------------------------------------------
    # IMPlatformAdapter -- inbound
    # ------------------------------------------------------------------

    def get_principal_user_id(self) -> str:
        return self._my_user_id

    def get_principal_display_name(self) -> str:
        return self._principal_name or self.resolve_user_display_name(self._my_user_id)

    def resolve_user_display_name(self, user_id: str) -> str:
        """Return a cached display name or the Slack user ID."""
        user_id = (user_id or "").strip()
        if not user_id:
            return ""
        return self._user_names.get(user_id, user_id)

    def get_bot_mention_tokens(self) -> list[str]:
        """Return Slack ID and plain-name mention tokens for this bot."""
        tokens: list[str] = []
        if self._bot_user_id:
            tokens.append(f"<@{self._bot_user_id}>")
        if self._bot_name:
            tokens.append(f"@{self._bot_name}")
        return tokens

    def load_recent_messages(
        self, thread_id: str, limit: int = 500
    ) -> list[IMHistoryMessage]:
        bucket = self._history.get((thread_id or "").strip())
        if not bucket:
            return []
        ordered = sorted(bucket.values(), key=lambda item: item.timestamp_ms)
        selected = ordered[-limit:] if limit > 0 else []
        return [
            item
            if item.user_name
            else IMHistoryMessage(
                user_id=item.user_id,
                user_name=self.resolve_user_display_name(item.user_id),
                content=item.content,
                timestamp_ms=item.timestamp_ms,
            )
            for item in selected
        ]

    def build_relevance_metadata(
        self,
        metadata: dict[str, Any],
        *,
        sender_user_id: str,
        relevant: bool,
    ) -> dict[str, Any]:
        if not relevant:
            return {}
        # An explicit delivery decision already exists; do not overwrite it.
        if str(metadata.get("reply_scope") or "").strip():
            return {}
        if str(metadata.get("chat_type") or "").strip() != "group":
            return {}

        principal_user_id = self.get_principal_user_id()
        if not principal_user_id or sender_user_id == principal_user_id:
            return {}

        patch: dict[str, Any] = {
            REPLY_CANDIDATE_USER_ID_KEY: principal_user_id,
            "reply_candidate_reason": "processor_target_user",
            "reply_candidate_user_id": principal_user_id,
        }
        principal_name = self.get_principal_display_name()
        if principal_name:
            patch["reply_target_name"] = principal_name
        return patch

    # ------------------------------------------------------------------
    # IMPlatformAdapter -- outbound
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return PLATFORM_NAME

    @property
    def reply_user_id_key(self) -> str:
        return REPLY_USER_ID_KEY

    @property
    def use_keyword_override(self) -> bool:
        return True

    def get_candidate_user_id(self, metadata: dict[str, Any]) -> str:
        candidate = str(metadata.get(REPLY_CANDIDATE_USER_ID_KEY) or "").strip()
        if candidate:
            return candidate
        # Recover a candidate for replies that skipped the inbound pipeline.
        patch = self.build_relevance_metadata(
            metadata,
            sender_user_id=str(metadata.get("im_sender_user_id") or ""),
            relevant=True,
        )
        if patch:
            candidate = str(patch.get(REPLY_CANDIDATE_USER_ID_KEY) or "").strip()
            if candidate:
                metadata.update(patch)
        return candidate
