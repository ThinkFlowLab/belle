"""JiuwenSwarm path for persisted Slack event IDs."""

from pathlib import Path

from jiuwenswarm.extensions.sdk.workspace_paths import get_user_workspace_dir


def get_slack_dedup_path() -> Path:
    """Keep the gateway state path across package moves."""
    return get_user_workspace_dir() / "gateway" / "slack_seen_events.json"
