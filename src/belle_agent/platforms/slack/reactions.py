"""Add or remove a reaction in the request's Slack conversation."""

import json
import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.emoji import glyph_shortcode

from .delivery import CHANNEL_LOG_TAG, slack_error_code, slack_failure_reason
from .history_policy import (
    SlackWorkspaceUnresolved,
)
from .json_toolkit import (
    slack_echo,
)
from .tool_common import (
    _as_mapping,
    _safe_error_code,
    _SlackCallFailure,
)
from .tool_context import (
    SlackToolBinding,
    SlackToolRefused,
    checks_slack_ids,
)
from .tool_context import (
    slack_tool_error as _error,
)

# Preserve the raw message ts because millisecond timestamps lose precision.
SLACK_CHANNEL_ID_KEY = "slack_channel_id"
SLACK_MESSAGE_TS_KEY = "message_ts"

_ALREADY_ADDED = "already_reacted"
_ALREADY_REMOVED = "no_reaction"

_REFUSAL_DETAILS = {
    "missing_scope": "This Slack app needs reactions:write. An operator adds it and reinstalls the app.",
    "message_not_found": "No message with that ID is in this conversation. A ts from another conversation cannot be used here.",
    "not_in_channel": "The bot is not in this conversation. Someone already there must invite it.",
    "channel_not_found": "The bot is not in this conversation. Someone already there must invite it.",
    "is_archived": "This conversation is archived. Reactions cannot be changed until it is unarchived.",
    "too_many_reactions": "This message has the maximum number of reactions. One must come off before another is added.",
}

#: A reaction refused here rather than by Slack.
_ReactionRefused = SlackToolRefused


def slack_reaction_request_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Require trusted metadata with a conversation ID."""
    if not isinstance(metadata, Mapping):
        return {}
    if not str(metadata.get(SLACK_CHANNEL_ID_KEY) or "").strip():
        return {}
    return dict(metadata)


def _emoji_name(raw: Any) -> str:
    """Accept a shortcode or convert a supported Unicode glyph."""
    text = str(raw or "").strip()
    if text:
        text = text.strip(":").strip()
    if not text:
        raise _ReactionRefused(
            "emoji_required",
            "emoji is required and was empty. Pass a Slack shortcode without "
            "colons, such as eyes, white_check_mark or tada.",
        )

    if text.isascii():
        return text.lower()

    name, tones = glyph_shortcode(text) or ("", ())
    if not name:
        raise _ReactionRefused(
            "emoji_character_not_recognised",
            f"Slack names an emoji and never draws it, so {slack_echo(raw)} cannot "
            "be sent as it stands, and this tool does not know which name it "
            "goes by. Pass the shortcode instead, without colons -- tada "
            "rather than the party popper, white_check_mark rather than the "
            "tick. Any shortcode is accepted, including one this workspace "
            "added itself, so a name that looks unfamiliar is still worth "
            "passing.",
        )
    return name + "".join(f"::skin-tone-{tone}" for tone in tones)


class SlackReactionToolkit(SlackToolBinding):
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        card = {
            "name": "react_to_message",
            "description": (
                "Add or remove one emoji reaction on a message in this request's conversation. "
                "This marks the message without posting a new one. "
                "Omit message_id to use the message that started the turn. "
                "Scheduled runs have no default and must provide message_id. "
                "Copy the exact ID from a Slack result in this conversation. Never infer it from a time. "
                "Slack shortcodes work with or without colons. Common emoji characters convert to names. "
                "Custom emoji require shortcodes. The tool refuses unknown characters. "
                "Repeated add or remove calls succeed when the requested state already holds. "
                "Remove affects only this app's reaction. One call cannot clear every reaction. "
                "The result reports the action. Read the message to inspect current reactions."
            ),
            "input_params": {
                "type": "object",
                "required": ["emoji"],
                "properties": {
                    "emoji": {
                        "type": "string",
                        "description": (
                            "Name one emoji by Slack shortcode, such as eyes or +1. "
                            "Colons are optional. Common emoji characters also work. "
                            "Custom emoji require shortcodes."
                        ),
                    },
                    "message_id": {
                        "type": "string",
                        "description": (
                            "Copy an exact ID from a Slack result in this conversation. "
                            "Omit it for this turn's originating message. Scheduled runs require it."
                        ),
                    },
                    "remove": {
                        "type": "boolean",
                        "default": False,
                        "description": "Remove this app's reaction when true.",
                    },
                },
            },
        }
        return [card]

    """Toolkit scoped to one request's Slack conversation."""

    _resolver_failure_type = _SlackCallFailure
    _refusal_type = _ReactionRefused

    async def _call(self, remove: bool, channel: str, timestamp: str, name: str) -> None:
        """Treat either SDK refusal shape and already-settled state consistently."""
        client = self._get_client()
        method = client.reactions_remove if remove else client.reactions_add
        settled = _ALREADY_REMOVED if remove else _ALREADY_ADDED
        try:
            response = await method(channel=channel, timestamp=timestamp, name=name)
        except Exception as exc:  # noqa: BLE001 - SDK error types vary.
            error = _as_mapping(getattr(exc, "response", None)).get("error") or str(exc)
        else:
            data = _as_mapping(response)
            if data.get("ok", True) is not False:
                return
            error = data.get("error")
        code = _safe_error_code(error, self._bot_token)
        if code != settled:
            raise _ReactionRefused(code)

    @checks_slack_ids
    async def react_to_message(
        self,
        emoji: str,
        message_id: str | None = None,
        remove: bool | None = None,
    ) -> str:
        """Add or remove one emoji reaction on one message in this conversation."""
        request_metadata = self._runtime_metadata()
        try:
            await self._load_settings(request_metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _error(unresolved.code, unresolved.detail)
        chat_id = str(request_metadata.get(SLACK_CHANNEL_ID_KEY) or "").strip()
        if not chat_id:
            # The provider failed closed, or this turn never had a Slack
            # conversation. Either way there is no conversation to fall back
            # to, and inventing one would be the whole failure this tool is
            # built to make impossible.
            return _error(
                "trusted_slack_channel_context_required",
                "This turn carries no Slack conversation, so there is no "
                "message here to react to. Nothing about the request can "
                "supply one.",
            )

        target = str(message_id or "").strip()
        if not target:
            target = str(request_metadata.get(SLACK_MESSAGE_TS_KEY) or "").strip()
            if not target:
                return _error(
                    "message_id_required",
                    "message_id was not given and this turn was not started by "
                    "a Slack message, so there is no message it could have "
                    "meant. A scheduled run is the usual case. Pass the ts of "
                    "the message to react to.",
                )

        try:
            name = _emoji_name(emoji)
        except _ReactionRefused as refusal:
            return _error(refusal.code, refusal.detail)

        removing = bool(remove)
        try:
            await self._call(removing, chat_id, target, name)
        except _ReactionRefused as refusal:
            return _error(refusal.code, self._detail_for(refusal, name))

        return json.dumps(
            {
                "ok": True,
                "chat_id": chat_id,
                "message_id": target,
                "emoji": name,
                "removed": removing,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _detail_for(self, refusal: _ReactionRefused, name: str) -> str:
        """Explain actionable refusals; leave unknown codes unexplained."""
        if refusal.detail:
            return refusal.detail
        if refusal.code == "invalid_name":
            return (
                f"Slack does not know an emoji called {name} in this "
                "workspace. Check the spelling of the shortcode; a custom "
                "emoji has to already exist here to be reacted with."
            )
        return _REFUSAL_DETAILS.get(refusal.code, "")


class ReactionMarks:
    """Best-effort connector marks with one warning per failed emoji and error."""

    def __init__(self, logger: logging.Logger, *, tag: str = CHANNEL_LOG_TAG) -> None:
        self.logger = logger
        self.tag = tag
        self.reported_failures: set[tuple[str, str]] = set()

    @staticmethod
    def name(emoji: str) -> str:
        name = str(emoji or "").strip()
        return name.strip(":") if name != "disabled" else ""

    async def change(
        self, client: Any, channel_id: str, timestamp: str, emoji: str,
        *, remove: bool = False,
    ) -> Exception | None:
        name = self.name(emoji)
        if not name or not channel_id or not timestamp or client is None:
            return None
        try:
            method = client.reactions_remove if remove else client.reactions_add
            await method(channel=channel_id, timestamp=timestamp, name=name)
        except Exception as exc:  # noqa: BLE001 - marks never fail a turn
            if remove:
                self.logger.debug("Slack reactions.remove %r failed: %s", name, exc)
                return None
            self._report_failure(exc, name)
            return exc
        if not remove:
            self.logger.debug("%s reaction added:"
                              " channel=%s ts=%s emoji=%s", self.tag, channel_id, timestamp, name)
        return None

    def _report_failure(self, exc: Exception, name: str) -> None:
        reason = slack_failure_reason(exc)
        kind = (slack_error_code(exc) or exc.__class__.__name__, name)
        if kind in self.reported_failures:
            self.logger.debug("Slack reactions.add %r failed again: %s", name, reason)
            return
        self.reported_failures.add(kind)
        self.logger.warning(
            "Slack reactions.add %r failed: %s. An acknowledgement is attempted"
            " on every message this channel takes, so this is said once and"
            " logged at DEBUG from here on",
            name, reason,
        )
