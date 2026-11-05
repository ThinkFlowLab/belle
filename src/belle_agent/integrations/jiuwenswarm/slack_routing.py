"""Establish Slack provenance for inbound requests and cron job sessions.

A stored session id alone does not prove which Slack conversation created a
job. Creation records trust only when the request came from that session.
Each run then resolves the current audience setting and stamps the delivery
conversation as the source audience. Jobs without a trusted Slack source or
delivery conversation receive no Slack audience metadata.

The gateway and runtime share these helpers without importing one another.
"""

import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.integrations.jiuwenswarm.config import load_belle_config
from belle_agent.integrations.jiuwenswarm.slack_tool_config import slack_section
from belle_agent.integrations.jiuwenswarm.slack_tool_context import (
    METADATA_ORIGIN_KEY,
    ORIGIN_CRON_JOB,
    slack_post_request_metadata,
)
from belle_agent.platforms.slack.history_policy import audience_metadata
from belle_agent.platforms.slack.identity import slack_team_id_from_session

logger = logging.getLogger(__name__)

# The channel id an inbound Slack request holds. A cron run has no Slack
# channel of its own; it arrives on ``__cron__`` (see ``CronSchedulerService``).
SLACK_CHANNEL_ID = "slack"
CRON_CHANNEL_ID = "__cron__"

# Stamped on a cron run's request metadata alongside the Slack conversation, so
# that a Slack context on a cron turn is only ever one the scheduler put there
# and never one that some other cron code path happened to leave behind.
SLACK_HISTORY_ORIGIN_KEY = METADATA_ORIGIN_KEY
SLACK_HISTORY_ORIGIN_CRON_JOB = ORIGIN_CRON_JOB


def request_carries_slack_history_context(
    channel_id: str,
    metadata: dict[str, Any] | None,
) -> bool:
    """Accept an inbound Slack turn or a cron run stamped by the scheduler."""
    channel = str(channel_id or "").strip().lower()
    if channel == SLACK_CHANNEL_ID:
        return True
    if channel != CRON_CHANNEL_ID:
        return False
    return (
        isinstance(metadata, dict)
        and metadata.get(SLACK_HISTORY_ORIGIN_KEY) == SLACK_HISTORY_ORIGIN_CRON_JOB
    )


def filter_slack_history_request_metadata(
    channel_id: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Require a Slack conversation and an explicit audience decision."""
    if not isinstance(metadata, dict):
        return {}
    return slack_post_request_metadata(channel_id, metadata)


def belle_slack_policy() -> dict[str, Any]:
    """Read Belle's current Slack policy."""
    return slack_section(load_belle_config())


def slack_audience_metadata() -> dict[str, Any]:
    """Stamp Belle's current Slack audience policy on a new turn."""
    policy = belle_slack_policy()
    return audience_metadata(policy["enforce_audience"])


def slack_message_team_id(msg: Any) -> str:
    """Use the inbound team ID, falling back to the session ID."""
    metadata = getattr(msg, "metadata", None)
    if isinstance(metadata, Mapping):
        team = str(metadata.get("slack_team_id") or "").strip()
        if team:
            return team
    return slack_team_id_from_session(getattr(msg, "session_id", ""))


def slack_session_id(
    *, team_id: str, channel_id: str, user_id: str, root_thread_ts: str,
    is_dm: bool, channel_wide: bool = False,
) -> str:
    """Build JiuwenSwarm's Slack session ID for a DM, thread, or channel."""
    team = str(team_id or "").strip()
    if not team:
        return ""
    if is_dm:
        return f"slack_{team}_{channel_id}_{user_id}"
    return f"slack_{team}_{channel_id}" if channel_wide else f"slack_{team}_{channel_id}_{root_thread_ts}"


def validate_belle_slack_startup(enabled_workspaces: list[Any]) -> None:
    """Check Belle policy only when a Slack workspace will connect."""
    if not enabled_workspaces:
        return
    if not belle_slack_policy()["enforce_audience"]:
        logger.warning("[App] Belle Slack audience rule is disabled")


def parse_slack_cron_session(session_id: Any) -> tuple[str, str]:
    """Return the ``(channel_id, thread_ts)`` a Slack cron session id names.

    Return empty strings for invalid IDs. A target with a dot is a thread
    timestamp. Other targets, including DM user IDs, resolve to the channel
    root. Only the first underscore-separated target field is read. Cron
    callers use this without importing the Slack connector.
    """
    raw = str(session_id or "").strip()
    if not raw.startswith("slack_"):
        return "", ""
    parts = raw.split("_", 4)
    if len(parts) < 3:
        return "", ""
    channel_id = parts[2].strip()
    if not channel_id:
        return "", ""
    if len(parts) == 3:
        # Named by channel alone. Both remaining fields have to be filled for
        # the string to be one of these at all: a real id never has an empty
        # team, an unknown workspace being spelled ``default``, so an empty one
        # means the string was assembled out of something missing rather than
        # out of a conversation.
        return (channel_id, "") if parts[1].strip() else ("", "")
    session_target = parts[3].strip()
    return channel_id, (session_target if "." in session_target else "")


def slack_cron_mutation(job: Any, *, action: str, request_channel_id: str) -> None:
    """Warn when a job has no proven Slack origin or delivery channel."""
    session_id = getattr(job, "session_id", None)
    if str(session_id or "").startswith("slack_") and getattr(job, "origin_channel_id", "") != SLACK_CHANNEL_ID:
        logger.warning(
            "[Cron] job carries a Slack-shaped session_id that this request cannot "
            "vouch for (request channel=%r); it will deliver as usual but will not "
            "be given Slack history access",
            request_channel_id or "<unset>",
        )
    warn_if_slack_cron_delivery_unreachable(
        targets=getattr(job, "targets", ""), session_id=session_id, job_id=getattr(job, "id", "")
    )


CRON_SLACK_UNREACHABLE_TEMPLATE = (
    "[Cron] job %s targets Slack but no delivery channel can be resolved: its"
    " session_id %r does not name a Slack conversation and"
    " channels.slack.default_channel_id is unset, so every run of this job will"
    " fail at delivery. Create the job from the Slack conversation it should"
    " post into, or set channels.slack.default_channel_id."
)


def warn_if_slack_cron_delivery_unreachable(
    *,
    targets: Any,
    session_id: Any,
    job_id: Any = "",
) -> bool:
    """Warn when neither the job session nor the default names a Slack channel.

    The caller still writes the job. Return whether a warning was logged.
    """
    if str(targets or "").strip().lower() != SLACK_CHANNEL_ID:
        return False
    try:
        from belle_agent.integrations.jiuwenswarm.config import (
            slack_default_channel_id_from_config,
        )
        from belle_agent.integrations.jiuwenswarm.slack_channel import SlackChannel

        reachable = SlackChannel.delivery_is_reachable(
            session_id=session_id,
            default_channel_id=slack_default_channel_id_from_config(),
        )
    except Exception as exc:  # noqa: BLE001 - a warning may not break a write.
        logger.warning(
            "[Cron] could not check Slack delivery reachability for job %s: %s",
            str(job_id or "").strip() or "<new>",
            exc,
        )
        return False
    if reachable:
        return False
    logger.warning(
        CRON_SLACK_UNREACHABLE_TEMPLATE,
        str(job_id or "").strip() or "<new>",
        str(session_id or ""),
    )
    return True


def _slack_channel_type_for(channel_id: str) -> str:
    """A word the history tool will accept for a conversation cron cannot ask about.

    A cron job records a conversation id but not its type, and the history tool
    refuses a request whose ``slack_channel_type`` it does not recognise.

    Only the ``D`` half is a reading: ``D`` names a one-to-one direct message and
    nothing else, while ``C`` and ``G`` separate none of the three room kinds. So
    "channel" here is a word chosen to be accepted rather than a claim about the
    conversation, and nothing but the history tool's own origin check may be keyed
    on it.
    """
    return "im" if str(channel_id or "").startswith("D") else "channel"


def slack_history_metadata_for_cron_job(job: Any) -> dict[str, Any]:
    """Stamp a trusted cron job with its Slack conversation and current policy.

    Return an empty mapping when session provenance or policy cannot be checked.
    The runtime then mounts no Slack tools. Scheduled runs have no requester.
    """
    if getattr(job, "origin_channel_id", "") != SLACK_CHANNEL_ID:
        return {}
    channel_id, thread_ts = parse_slack_cron_session(getattr(job, "session_id", None))
    if not channel_id:
        # A job flagged trusted whose session id no longer parses: the flag and
        # the string it describes have drifted apart, so neither is believed.
        logger.warning(
            "[Cron] job %s is flagged as a trusted Slack session but its "
            "session_id does not name a channel; no Slack history context",
            getattr(job, "id", "<unknown>"),
        )
        return {}
    try:
        audience = slack_audience_metadata()
    except Exception:
        logger.exception(
            "[Cron] Belle Slack policy could not be loaded for job %s",
            getattr(job, "id", "<unknown>"),
        )
        return {}
    metadata: dict[str, Any] = {
        "slack_channel_id": channel_id,
        "slack_channel_type": _slack_channel_type_for(channel_id),
        SLACK_HISTORY_ORIGIN_KEY: SLACK_HISTORY_ORIGIN_CRON_JOB,
        # One stamp for both directions. There is no second key for posting: the
        # audience rule is one rule, and a cron run that read a different answer
        # for writes than for reads would be two gates on one deployment. A
        # scheduled run has no requester, so the posting toolkit is what refuses
        # the one case that would need somebody to ask.
        **audience,
    }
    if thread_ts:
        metadata["slack_thread_ts"] = thread_ts
    return metadata


__all__ = [
    "CRON_CHANNEL_ID",
    "CRON_SLACK_UNREACHABLE_TEMPLATE",
    "SLACK_CHANNEL_ID",
    "SLACK_HISTORY_ORIGIN_CRON_JOB",
    "SLACK_HISTORY_ORIGIN_KEY",
    "belle_slack_policy",
    "filter_slack_history_request_metadata",
    "parse_slack_cron_session",
    "request_carries_slack_history_context",
    "slack_audience_metadata",
    "slack_cron_mutation",
    "slack_history_metadata_for_cron_job",
    "validate_belle_slack_startup",
    "warn_if_slack_cron_delivery_unreachable",
]
