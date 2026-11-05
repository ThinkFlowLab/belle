"""Pin or unpin messages in the Slack conversation from trusted metadata."""

import json
import logging
from typing import Any

from .history_policy import (
    SlackWorkspaceUnresolved,
)
from .json_toolkit import (
    SlackMethodToolkitBase,
    slack_failure_detail,
    slack_refusal_json,
)
from .tool_common import (
    _SlackCallFailure,
)
from .tool_context import (
    checks_slack_ids,
    slack_message_id_error,
)

logger = logging.getLogger(__name__)

_PIN_ADD = "pins_add"
_PIN_REMOVE = "pins_remove"

_PIN_WRITE_SCOPE = "pins:write"

# These Slack errors mean the requested state already holds.
_ALREADY_IN_STATE = {_PIN_ADD: "already_pinned", _PIN_REMOVE: "no_pin"}

_PIN_FAILURE_DETAIL = {
    "too_many_pins": (
        "this conversation holds as many pinned messages as Slack allows, so"
        " nothing further can be pinned until somebody unpins one; calling"
        " again will not help"
    ),
    "not_pinnable": ("Slack will not pin this message; a second call will not change that"),
    "message_not_found": (
        "this conversation holds no message with that id; a message id is not"
        " transferable between conversations, so one taken from elsewhere names"
        " nothing here"
    ),
}


def _pin_refusal_json(
    code: str,
    detail: str = "",
    *,
    chat_id: str = "",
    message_id: str = "",
) -> str:
    """Report a refusal without asserting the message's pin state."""
    return slack_refusal_json(logger, "pin", code, detail, chat_id=chat_id, message_id=message_id)


class SlackPinToolkit(SlackMethodToolkitBase):
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [self._pin_tool_definition()]

    @staticmethod
    def _pin_tool_definition() -> dict[str, Any]:
        """Describe the request-scoped pin tool."""
        return {
            "name": "pin_message",
            "description": (
                "Pin or unpin a message in this conversation. All members see "
                "its pins. Pin messages worth finding again. "
                "Copy message_id from a Slack result. Never construct one. "
                "Set remove to true to unpin. Repeating either action succeeds "
                "when the requested state already holds. A full pin list "
                "requires a person to unpin another message. The result gives "
                "the conversation, message id, and pin state. It omits message content."
            ),
            "input_params": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": (
                            "Slack message id from this conversation, such as "
                            "1758123456.123456. Copy it from a Slack result."
                        ),
                    },
                    "remove": {
                        "type": "boolean",
                        "default": False,
                        "description": "Unpin the message when true.",
                    },
                },
                "required": ["message_id"],
            },
        }

    """Toolkit scoped to the Slack conversation in the active request metadata."""

    #: Pin deadline, including retries.
    DEFAULT_TIMEOUT_SECONDS = 30.0

    def _timeout_code(self, method: str) -> str:
        return "pin_timed_out"

    @checks_slack_ids
    async def pin_message(self, message_id: str, remove: bool = False) -> str:
        """Set a message's pin state in the request's conversation."""
        metadata = self._runtime_metadata()
        chat_id = str(metadata.get("slack_channel_id") or "").strip()
        raw_id = str(message_id or "").strip()

        if not chat_id:
            return _pin_refusal_json(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is"
                " nowhere to pin a message and nothing to fall back to",
            )
        if problem := slack_message_id_error(raw_id):
            return _pin_refusal_json(*problem, chat_id=chat_id, message_id=raw_id[:32])

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _pin_refusal_json(unresolved.code, unresolved.detail, chat_id=chat_id)
        method = _PIN_REMOVE if remove else _PIN_ADD
        try:
            await self._call(method, channel=chat_id, timestamp=raw_id)
        except _SlackCallFailure as exc:
            if exc.code == _ALREADY_IN_STATE[method]:
                logger.info(
                    "slack pin: %s in %s was already in the requested state (%s)",
                    raw_id,
                    chat_id,
                    exc.code,
                )
                return self._pin_result(chat_id, raw_id, remove=remove)
            return _pin_refusal_json(
                exc.code,
                slack_failure_detail(exc, _PIN_FAILURE_DETAIL, scope=_PIN_WRITE_SCOPE),
                chat_id=chat_id,
                message_id=raw_id,
            )

        logger.info(
            "slack pin: %s %s in %s",
            "unpinned" if remove else "pinned",
            raw_id,
            chat_id,
        )
        return self._pin_result(chat_id, raw_id, remove=remove)

    @staticmethod
    def _pin_result(chat_id: str, message_id: str, *, remove: bool) -> str:
        """Include the target and requested state omitted by Slack's response."""
        return json.dumps(
            {
                "ok": True,
                "chat_id": chat_id,
                "message_id": message_id,
                "pinned": not remove,
            },
            ensure_ascii=False,
        )
