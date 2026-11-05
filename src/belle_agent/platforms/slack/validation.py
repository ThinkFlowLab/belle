"""Validate Slack settings before shared configuration validation."""

import math
from pathlib import Path
from typing import Any

from belle_agent.config import BelleConfigError, expand_env
from belle_agent.platforms.slack.duration_limits import (
    MAX_MEMBERS_CACHE_SECONDS,
    MIN_HISTORY_DURATION_SECONDS,
)


def validate_config(data: dict[str, Any], config_path: Path) -> None:
    """Validate and expand the Slack section in place."""
    # The group itself: an absent section passes, and a null one is refused.
    platforms = data.get("platforms") or {}
    if "slack" not in platforms:
        return
    slack = platforms["slack"]
    if not isinstance(slack, dict):
        raise BelleConfigError("Belle config platforms.slack must be a mapping")

    key = "enforce_audience"
    if not isinstance(slack.get(key), bool):
        raise BelleConfigError(f"Belle config platforms.slack.{key} must be a boolean")
    workspaces = slack.get("workspaces")
    for key in ("bot_token", "app_token"):
        value = slack.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise BelleConfigError(f"Belle config platforms.slack.{key} must be a string")
            slack[key] = expand_env(value)
    if workspaces is not None:
        if not isinstance(workspaces, list) or any(not isinstance(item, dict) for item in workspaces):
            raise BelleConfigError("Belle config platforms.slack.workspaces must be a list of mappings")
        for workspace in workspaces:
            for key in ("bot_token", "app_token"):
                if isinstance(workspace.get(key), str):
                    workspace[key] = expand_env(workspace[key])
    questions = slack.get("decision_questions_file")
    if questions is not None and questions != "":
        if not isinstance(questions, str):
            raise BelleConfigError("Belle config platforms.slack.decision_questions_file must be a path")
        questions = expand_env(questions)
        if questions:
            question_path = Path(questions).expanduser()
            slack["decision_questions_file"] = str(
                question_path if question_path.is_absolute() else config_path.resolve().parent / question_path
            )
        else:
            slack["decision_questions_file"] = ""
    if "search_enabled" in slack and not isinstance(slack["search_enabled"], bool):
        raise BelleConfigError("Belle config platforms.slack.search_enabled must be a boolean")
    for key in (
        "canvas_read_max_chars", "canvas_sections_max_items", "directory_scan_max_pages", "lists_export_max_bytes",
        "lists_export_max_chars", "file_download_max_bytes",
    ):
        if key in slack and (isinstance(slack[key], bool) or not isinstance(slack[key], int) or slack[key] < 1):
            raise BelleConfigError(f"Belle config platforms.slack.{key} must be a positive integer")
    for key in (
        "history_max_messages",
        "history_max_total_chars",
        "history_max_message_chars",
        "history_max_roots_scanned",
        "history_max_api_calls",
        "history_max_user_lookups",
    ):
        minimum = 0 if key == "history_max_user_lookups" else 1
        if key in slack and (isinstance(slack[key], bool) or not isinstance(slack[key], int) or slack[key] < minimum):
            form = "nonnegative" if minimum == 0 else "positive"
            raise BelleConfigError(f"Belle config platforms.slack.{key} must be a {form} integer")
    for key in ("history_scan_timeout_seconds", "history_members_cache_seconds"):
        if key not in slack:
            continue
        value = slack[key]
        maximum = MAX_MEMBERS_CACHE_SECONDS if key == "history_members_cache_seconds" else None
        try:
            valid = (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and value >= MIN_HISTORY_DURATION_SECONDS
                and (maximum is None or value <= maximum)
            )
        except OverflowError:
            valid = False
        if not valid:
            limit = (
                f"from {MIN_HISTORY_DURATION_SECONDS:g} to {maximum:g} seconds"
                if maximum is not None
                else f"at least {MIN_HISTORY_DURATION_SECONDS:g} seconds"
            )
            raise BelleConfigError(f"Belle config platforms.slack.{key} must be a finite number {limit}")
    for key in ("history_never_read", "history_exempt_members"):
        if key in slack:
            raise BelleConfigError(f"Belle config platforms.slack.{key} is no longer supported")
