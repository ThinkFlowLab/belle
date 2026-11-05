"""Select Slack workspaces and enforce audience bounds."""

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from belle_agent.audience import Audience, audience_newcomers, audience_within, read_permitted, write_permitted
from belle_agent.identifiers import id_list

logger = logging.getLogger(__name__)

KEY_ENFORCE_AUDIENCE = "enforce_audience"

ENFORCE_AUDIENCE_DEFAULT = True

METADATA_ENFORCE_AUDIENCE_KEY = "slack_enforce_audience"
METADATA_ASKER_KEY = "slack_user_id"
METADATA_WRITE_TARGET_REQUIRED_KEY = "slack_write_target_required"
METADATA_TEAM_KEY = "slack_team_id"

KEY_WORKSPACES = "workspaces"
KEY_BOT_TOKEN = "bot_token"

SLACK_WORKSPACE_KEYS: tuple[str, ...] = (
    "bot_token",
    "app_token",
    "enabled",
    "default_channel_id",
)

class SlackWorkspaceUnresolved(LookupError):
    """No configured Slack installation can safely serve this request."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def slack_workspace_blocks(
    slack_conf: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Return installations, or top-level settings if no list is set."""
    if not isinstance(slack_conf, Mapping):
        return ()
    raw_blocks = slack_conf.get(KEY_WORKSPACES)
    if not raw_blocks:
        return (slack_conf,)
    if not isinstance(raw_blocks, (list, tuple)):
        logger.warning(
            "Slack %s is %s rather than a list; no workspace can be selected for a tool call",
            KEY_WORKSPACES,
            type(raw_blocks).__name__,
        )
        return ()
    return tuple(entry for entry in raw_blocks if isinstance(entry, Mapping))


def _overlay_workspace(slack_conf: Mapping[str, Any], block: Mapping[str, Any]) -> Mapping[str, Any]:
    """Overlay one installation's settings on connector-wide settings."""
    if block is slack_conf:
        return slack_conf
    merged = dict(slack_conf)
    merged.update({key: block[key] for key in SLACK_WORKSPACE_KEYS if key in block})
    return merged


def select_slack_workspace(
    slack_conf: Mapping[str, Any] | None,
    team_id: str | None = None,
    *,
    team_of_token: Callable[[str], str | None] | None = None,
) -> Mapping[str, Any]:
    """Select an installation by team ID. Reject ambiguous requests."""
    conf = slack_conf if isinstance(slack_conf, Mapping) else {}
    blocks = slack_workspace_blocks(conf)
    if len(blocks) == 1:
        return _overlay_workspace(conf, blocks[0])
    team = str(team_id or "").strip()
    if not blocks:
        raise SlackWorkspaceUnresolved(
            "slack_workspace_unconfigured",
            "Slack workspace list is set but holds no readable"
            " install, so there is no token any Slack tool may use",
        )
    if not team:
        raise SlackWorkspaceUnresolved(
            "slack_request_names_no_workspace",
            f"{len(blocks)} Slack workspaces are configured and this request"
            f" carries no {METADATA_TEAM_KEY}, so which of them it belongs to"
            f" cannot be established; serving it from any of them would be a"
            f" guess",
        )
    lookup = team_of_token if callable(team_of_token) else lambda _token: None
    for block in blocks:
        token = str(block.get(KEY_BOT_TOKEN) or "").strip()
        if not token:
            continue
        if str(lookup(token) or "").strip() == team:
            return _overlay_workspace(conf, block)
    raise SlackWorkspaceUnresolved(
        "slack_workspace_unknown",
        f"no configured Slack workspace is installed in {team}, so this"
        f" request cannot be served; it is a configuration fact rather than a"
        f" permission one",
    )


def conversation_is_public(info: Mapping[str, Any] | None) -> bool:
    """Return true only for a confirmed public channel."""
    if not isinstance(info, Mapping):
        return False
    if info.get("is_private") is not False:
        return False
    if info.get("is_im") or info.get("is_mpim"):
        return False
    return bool(info.get("is_channel"))


_EXTERNAL_SHARE_FLAGS: tuple[str, ...] = (
    "is_ext_shared",
    "is_pending_ext_shared",
    "is_org_shared",
    "is_shared",
)


def conversation_reaches_outside_workspace(info: Mapping[str, Any] | None) -> bool:
    """Return true when Slack marks a conversation as externally shared."""
    if not isinstance(info, Mapping):
        return False
    return any(bool(info.get(flag)) for flag in _EXTERNAL_SHARE_FLAGS)


def effective_audience(info: Mapping[str, Any] | None, members: Iterable[str] = ()) -> Audience:
    """Return the workspace or member audience of a conversation."""
    external = conversation_reaches_outside_workspace(info)
    if conversation_is_public(info) and not external:
        return Audience(workspace_wide=True, members=frozenset(), external=False)
    return Audience(workspace_wide=False, members=frozenset(members or ()), external=external)


_WARNED_AUDIENCE_UNENFORCED = False

_AUDIENCE_OFF_WORDS: frozenset[str] = frozenset({"false", "no", "off", "0"})
_AUDIENCE_ON_WORDS: frozenset[str] = frozenset({"true", "yes", "on", "1"})


def resolve_enforce_audience(
    slack_conf: Mapping[str, Any] | None,
    *,
    warn: Any = None,
) -> bool:
    """Enforce audience bounds unless an explicit false value disables them."""
    global _WARNED_AUDIENCE_UNENFORCED
    emit = warn if callable(warn) else logger.warning
    if not isinstance(slack_conf, Mapping):
        return ENFORCE_AUDIENCE_DEFAULT

    raw = slack_conf.get(KEY_ENFORCE_AUDIENCE)
    if raw is None:
        return ENFORCE_AUDIENCE_DEFAULT
    word = str(raw).strip().lower()
    if not word:
        return ENFORCE_AUDIENCE_DEFAULT
    if word in _AUDIENCE_OFF_WORDS:
        if not _WARNED_AUDIENCE_UNENFORCED:
            _WARNED_AUDIENCE_UNENFORCED = True
            emit(
                "Slack %s is false, so no read or post is held to"
                " an audience bound: this turn may read any conversation the bot"
                " can reach and post into any conversation it can reach,"
                " whoever is in either one. No other Slack setting"
                " switches a safety rule off wholesale",
                KEY_ENFORCE_AUDIENCE,
            )
        return False
    if word not in _AUDIENCE_ON_WORDS:
        emit(
            "Slack %s=%r is not a boolean; the audience rule"
            " stays enforced, which is what an unreadable value has to buy",
            KEY_ENFORCE_AUDIENCE,
            raw,
        )
    return True


def audience_metadata(enforce: Any) -> dict[str, bool]:
    """Stamp audience enforcement onto request metadata."""
    return {METADATA_ENFORCE_AUDIENCE_KEY: bool(enforce)}


__all__ = [
    "ENFORCE_AUDIENCE_DEFAULT",
    "KEY_BOT_TOKEN",
    "KEY_ENFORCE_AUDIENCE",
    "KEY_WORKSPACES",
    "METADATA_ASKER_KEY",
    "METADATA_ENFORCE_AUDIENCE_KEY",
    "METADATA_TEAM_KEY",
    "METADATA_WRITE_TARGET_REQUIRED_KEY",
    "SLACK_WORKSPACE_KEYS",
    "Audience",
    "SlackWorkspaceUnresolved",
    "audience_metadata",
    "audience_newcomers",
    "audience_within",
    "conversation_is_public",
    "conversation_reaches_outside_workspace",
    "effective_audience",
    "id_list",
    "read_permitted",
    "resolve_enforce_audience",
    "select_slack_workspace",
    "slack_workspace_blocks",
    "write_permitted",
]
