"""Text commands for visible jobs, including an operator's deployment-wide jobs."""

import json
from collections.abc import Mapping
from html import escape
from typing import Any, Protocol

from belle_agent.commands.grammar import Subcommand, usage_text
from belle_agent.platforms.base import InboundMessage

SCHEDULE_SUBCOMMANDS = (
    Subcommand(("schedule", "list"), "List scheduled jobs visible to you."),
    Subcommand(("schedule", "show"), "Show one scheduled job.", "<job id>"),
    Subcommand(("schedule", "pause"), "Pause a scheduled job.", "<job id>"),
    Subcommand(("schedule", "resume"), "Resume a scheduled job.", "<job id>"),
    Subcommand(("schedule", "delete"), "Delete a job and keep its run history.", "<job id>"),
    Subcommand(("schedule", "run"), "Run a scheduled job now.", "<job id>"),
)


class CommandToolkit(Protocol):
    async def invoke_at(self, name: str, arguments: Mapping[str, Any], *, at: InboundMessage) -> str: ...


def _job_line(job: Mapping[str, Any]) -> str:
    name = escape(str(job.get("name") or ""))
    state = "enabled" if job.get("enabled") else "paused"
    next_runs = job.get("next_runs") or []
    next_due = f", next {next_runs[0]['due_iso_utc']}" if next_runs else ""
    return f"- `{job['job_id']}`: {name} ({state}{next_due})"


async def schedule_command(
    toolkit: CommandToolkit, at: InboundMessage, sub: Subcommand, arguments: str, command: str,
) -> tuple[str, str]:
    """Run one command through the same toolkit that serves model tool calls."""
    action = sub.words[1]
    parts = arguments.split()
    if (action == "list" and parts) or (action != "list" and len(parts) != 1):
        return usage_text(sub, command), "refused:schedule-usage"
    job_id = parts[0] if parts else ""
    actions: dict[str, tuple[str, dict[str, Any]]] = {
        "list": ("schedule_list", {}),
        "show": ("schedule_list", {"job_id": job_id}),
        "pause": ("schedule_update", {"job_id": job_id, "enabled": False}),
        "resume": ("schedule_update", {"job_id": job_id, "enabled": True}),
        "delete": ("schedule_delete", {"job_id": job_id, "keep_history": True}),
        "run": ("schedule_run_now", {"job_id": job_id}),
    }
    method, kwargs = actions[action]
    result = json.loads(await toolkit.invoke_at(method, kwargs, at=at))
    if not result.get("ok"):
        code = str(result.get("error") or "failed")
        return str(result.get("detail") or code), f"refused:{code}"
    if action == "list":
        jobs = result["jobs"]
        return ("**Scheduled jobs**\n" + "\n".join(_job_line(job) for job in jobs)
                if jobs else "No scheduled jobs are visible."), "answered:schedule-list"
    if action == "show":
        job = result["jobs"][0]
        detail = "\n".join(f"    {line}" for line in json.dumps(job, ensure_ascii=False, indent=2).splitlines())
        return f"**Scheduled job `{job_id}`**\n{detail}", "answered:schedule-show"
    if action == "run":
        run = result["run"]
        return f"Started run `{run['session_id']}` for `{job_id}`.", "answered:schedule-run"
    if action == "delete":
        return f"Deleted `{job_id}`. Its run history is kept.", "answered:schedule-delete"
    return f"{'Paused' if action == 'pause' else 'Resumed'} `{job_id}`.", f"answered:schedule-{action}"
