"""Refusal codes shared by schedule tools."""

CODES = frozenset({
    "invalid_arguments", "approval_required", "tool_denied", "job_id_malformed", "job_not_found",
    "name_invalid", "when_invalid", "zone_required", "zone_unknown", "time_malformed",
    "time_zone_mismatch", "local_time_nonexistent", "local_time_ambiguous", "time_in_past",
    "cron_invalid", "interval_too_short", "schedule_never_runs", "address_invalid",
    "delivery_not_allowed", "destination_unreachable", "target_not_readable", "audience_widened",
    "delivery_unavailable", "model_unknown", "skill_unknown", "expert_unknown", "subagent_unknown",
    "turn_out_of_scope", "job_limit_reached", "not_job_owner", "no_change", "job_ended",
    "job_running", "job_queue_full", "result_too_large", "store_unreadable",
    "trusted_conversation_context_required",
    "session_not_found", "session_limit_reached", "capability_unavailable",
    "session_busy", "session_has_no_run", "session_expired", "backend_conversation_unavailable",
})


class Refused(Exception):
    """A schedule call cannot proceed for the given precise reason."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)
