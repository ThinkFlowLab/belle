"""Persist a bounded window of Slack event IDs across reconnects."""

from belle_agent.admission.dedup import DEFAULT_MAX_ENTRIES, EventDedupStore


class SlackEventDedupStore(EventDedupStore):
    """Remember handled Slack event IDs across restarts and processes."""

    label = "Slack"


__all__ = ["DEFAULT_MAX_ENTRIES", "SlackEventDedupStore"]
