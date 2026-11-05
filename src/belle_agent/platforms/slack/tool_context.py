"""Shared request gate, install binding, refusal shape and card prose."""

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.tools.inputs import IdForm, checks_ids, id_error

from .history_policy import METADATA_ASKER_KEY, METADATA_TEAM_KEY

#: What a result's permalinks are for. In every card that reports one, because
#: a link built from parts or carried across results is the same mistake wherever
#: it is made.
SLACK_PERMALINK_RULE = (
    " Copy a permalink verbatim from this result rather than building a Slack"
    " link from parts, and never reuse one result's link on another."
)

#: What a Slack ``ts`` is, for a card that reports one. The subject it is said
#: about leads the sentence, so each card supplies its own.
SLACK_TS_RULE = (
    "an opaque Slack identifier, not a date: cite ts_iso_utc whenever stating"
    " when something happened, and never infer a date from ts itself."
)

#: The shape of each kind of Slack id, and how a refusal states it. The lengths
#: are left open because Slack may extend them.
CONVERSATION_ID_RE = re.compile(r"\A[CDG][A-Z0-9]{2,}\Z")
USER_ID_RE = re.compile(r"\A[UW][A-Z0-9]{2,}\Z")
FILE_ID_RE = re.compile(r"\AF[A-Z0-9]{2,}\Z")
_MESSAGE_ID_RE = re.compile(r"\A\d{1,12}\.\d{1,6}\Z")
_CHAT_ID_RE = re.compile(r"\A[CDGUW][A-Z0-9]+\Z")
_MESSAGE_ID_FORM = "seconds and microseconds separated by a dot, such as 1758123456.123456"
_FILE_ID_FORM = "an F followed by upper-case letters and digits, such as F0123456789"
_ID_KINDS: dict[str, IdForm] = {
    "chat": (
        _CHAT_ID_RE,
        (
            "a C, D or G for a conversation, or a U or W for a person, followed by"
            " upper-case letters and digits, such as C0123456789"
        ),
    ),
    "user": (USER_ID_RE, "a U or W followed by upper-case letters and digits, such as U0123456789"),
    "message": (_MESSAGE_ID_RE, _MESSAGE_ID_FORM),
    "file": (FILE_ID_RE, _FILE_ID_FORM),
    "canvas": (FILE_ID_RE, _FILE_ID_FORM),
    "list": (FILE_ID_RE, _FILE_ID_FORM),
}
#: The kind of Slack id each tool argument holds. An argument whose name ends in ``s`` holds a list of them.
_ID_ARGUMENTS = {
    "chat_id": "chat",
    "chat_ids": "chat",
    "visible_only_to_user_id": "user",
    "user_ids": "user",
    "message_id": "message",
    "reply_to_message_id": "message",
    "file_id": "file",
    "canvas_id": "canvas",
    "list_id": "list",
    "copy_from_list_id": "list",
}


def slack_message_id_error(message_id: str) -> "tuple[str, str] | None":
    """Why *message_id* is not a Slack message id, or ``None`` when it is."""
    if not message_id:
        return (
            "message_id_required",
            (
                "no message id was given; a message id identifies one message and"
                " is taken unchanged from a result that reported it"
            ),
        )
    if not _MESSAGE_ID_RE.match(message_id):
        return "message_id_malformed", f"a Slack message id is {_MESSAGE_ID_FORM}; this is not one"
    return None


def slack_id_error(arguments: Mapping[str, Any]) -> "tuple[str, str] | None":
    """Why an argument that holds a Slack id does not hold one, or ``None`` when each does."""
    return id_error(arguments, _ID_ARGUMENTS, _ID_KINDS, "Slack")


def slack_tool_error(code: str, detail: str = "", **fields: Any) -> str:
    """The JSON of a refused tool call: ``ok`` false, the ``error`` code, the ``detail`` if any, and ``fields``."""
    payload = {"ok": False, "error": code, **fields}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload, ensure_ascii=False)


#: Refuse a call whose Slack id argument is malformed, before the tool checks anything else.
checks_slack_ids = checks_ids(_ID_ARGUMENTS, _ID_KINDS, "Slack", slack_tool_error)


class SlackToolRefused(RuntimeError):
    """A tool refusal with a stable code and caller-facing detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def cross_installation_refusal(
    request_team: str, bound_team: str, installations: int, *, code: str
) -> "SlackToolRefused | None":
    """Refuse member comparisons across Slack installations.

    User IDs belong to one installation. The bound team comes from ``auth.test``;
    an unknown team fails closed when multiple installations are configured.
    """
    if installations <= 1:
        return None
    if request_team and bound_team and request_team == bound_team:
        return None
    return SlackToolRefused(
        code,
        "this deployment serves more than one Slack workspace, and whether the"
        " members about to be compared are all in one of them was not"
        f" established (request: {request_team or 'unstamped'}, client:"
        f" {bound_team or 'unbound'}); a user id names one person in one"
        " workspace and somebody else or nobody in another, so the membership"
        " rule is refused rather than computed across the two",
    )


def slack_request_metadata(
    channel_id: str,
    user_id: str,
    team_id: str,
    event_id: str,
    chat_type: str,
    is_dm: bool,
    *,
    audience: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the metadata the Slack toolkits read for a turn one Slack event started.

    ``audience`` is the settled audience decision, as ``history_policy.audience_metadata`` stamps it.
    """
    return {
        "user_id": user_id,
        "slack_event_id": event_id,
        "slack_team_id": team_id,
        "slack_channel_id": channel_id,
        "slack_channel_type": "im" if is_dm else (chat_type or "channel"),
        "slack_user_id": user_id,
        **audience,
    }


def runtime_slack_metadata(metadata: Mapping[str, Any], provider: Callable[[], Any] | None) -> dict[str, Any]:
    """Read live metadata once. Return an empty mapping on provider errors."""
    if provider is None:
        return dict(metadata)
    try:
        provided = provider()
    except Exception:  # noqa: BLE001 - request metadata must fail closed.
        return {}
    return dict(provided) if isinstance(provided, Mapping) else {}


class SlackToolBinding:
    """Bind a tool call to the Slack installation named by request metadata."""

    #: Raised by the workspace resolver when no install answers for a token.
    _resolver_failure_type: type[Exception] = Exception

    #: Raised in its place, so a caller sees this toolkit's own refusal shape.
    _refusal_type: type[Exception] = RuntimeError

    def __init__(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        metadata_provider: Callable[[], Any] | None = None,
        client: Any = None,
        workspaces: Any = None,
    ) -> None:
        self._request_metadata = dict(metadata) if metadata else {}
        self._metadata_provider = metadata_provider
        self._client = client
        self._workspaces = workspaces
        self._bot_token = ""

    def update_runtime_context(self, *, metadata: dict[str, Any] | None = None) -> None:
        self._request_metadata = dict(metadata) if metadata else {}

    @staticmethod
    def _requester(metadata: Mapping[str, Any]) -> str:
        """Return the requester stamped by the connector, if any."""
        return str(metadata.get(METADATA_ASKER_KEY) or "").strip()

    def _runtime_metadata(self) -> dict[str, Any]:
        return runtime_slack_metadata(self._request_metadata, self._metadata_provider)

    async def _load_settings(self, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        """Bind this call to the install it arrived from, and return its settings."""
        slack = await self._workspaces.settings_for(str(metadata.get(METADATA_TEAM_KEY) or "").strip())
        self._bot_token = str(slack.get("bot_token") or "").strip()
        return slack

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            return self._workspaces.client_for(self._bot_token)
        except self._resolver_failure_type as exc:
            raise self._refusal_type(exc.code) from None
