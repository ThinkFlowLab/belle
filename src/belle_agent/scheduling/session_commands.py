"""Text commands for retained and detached sessions."""

import json
from collections.abc import Mapping
from typing import Any, Protocol

from belle_agent.commands.grammar import Subcommand, usage_text
from belle_agent.platforms.base import InboundMessage
from belle_agent.scheduling.records import RUN_ID

SESSION_SUBCOMMANDS = (
    Subcommand(("bg",), "Start a task in a separate session.", "<task>"),
    Subcommand(("sessions", "list"), "List sessions visible here.", "[cursor]"),
    Subcommand(("sessions", "show"), "Show one session without its result.", "<session id> [cursor]"),
    Subcommand(("sessions", "result"), "Read your session result in a direct message.", "<session id> [run id] [cursor]"),
    Subcommand(("sessions", "continue"), "Continue a retained session.", "<session id> <task>"),
    Subcommand(("sessions", "cancel"), "Stop a session you started.", "<session id>"),
)


def _kind(row: Mapping[str, Any]) -> str:
    if row["kind"] == "scheduled":
        return f"scheduled {row['job_id']} ({row['job_mode']})"
    return str(row["kind"])


class SessionCommands(Protocol):
    async def invoke_at(self, name: str, arguments: Mapping[str, Any], *, at: InboundMessage) -> str: ...


async def session_command(
    toolkit: SessionCommands, at: InboundMessage, sub: Subcommand, arguments: str, command: str,
) -> tuple[str, str]:
    """Run a command through the session tool boundary."""
    action = sub.words[-1]
    parts = arguments.split()
    if ((action == "bg" and not arguments.strip())
            or (action == "list" and len(parts) > 1)
            or (action == "result" and len(parts) not in (1, 2, 3))
            or (action == "continue" and len(parts) < 2)
            or (action == "show" and len(parts) not in (1, 2))
            or (action == "cancel" and len(parts) != 1)):
        return usage_text(sub, command), "refused:session-usage"
    kwargs: dict[str, Any]
    if action == "bg":
        method, kwargs = "session_start", {"prompt": arguments.strip()}
    elif action == "list":
        method, kwargs = "session_list", {"cursor": parts[0]} if parts else {}
    else:
        method, kwargs = f"session_{'get' if action == 'show' else action}", {"session_id": parts[0]}
        if action == "result":
            rest = parts[1:]
            if rest and RUN_ID.fullmatch(rest[0]):
                kwargs["run_id"] = rest.pop(0)
            if len(rest) > 1:
                return usage_text(sub, command), "refused:session-usage"
            if rest:
                kwargs["cursor"] = rest[0]
        if action == "show" and len(parts) == 2:
            kwargs["cursor"] = parts[1]
        if action == "continue":
            kwargs["prompt"] = arguments.split(None, 1)[1]
    result = json.loads(await toolkit.invoke_at(method, kwargs, at=at))
    if not result.get("ok"):
        code = str(result.get("error") or "failed")
        return str(result.get("detail") or code), f"refused:{code}"
    if action == "bg":
        return f"Started session `{result['session_id']}`. It is {result['state']}.", "answered:session-start"
    if action == "continue":
        message = f"Continued session `{result['session_id']}` as run `{result['run_id']}`. It is {result['state']}."
        return message, "answered:session-continue"
    if action == "list":
        sessions = result["sessions"]
        lines = [f"**Sessions visible here** ({len(sessions)})"]
        lines.extend(f"- `{row['session_id']}`: {_kind(row)}, {row['state']}, {row['created_iso_utc']}" for row in sessions)
        if not sessions:
            lines.append("No sessions are visible.")
        if result["next_cursor"]:
            lines.append(f"More: `{command} sessions list {result['next_cursor']}`")
        return "\n".join(lines), "answered:session-list"
    if action == "show":
        session = result["session"]
        lines = [f"**Session `{session['session_id']}`**"]
        lines.extend(f"- {key.replace('_', ' ')}: {value}" for key, value in session.items() if key not in {"runs", "deletion"})
        deletion = session["deletion"]
        if deletion["state"] != "none":
            lines.append(f"- deletion: {deletion['state']}, {deletion['attempts']} attempts, last error: {deletion['last_error']}")
        lines.extend(f"- run `{row['run_id']}`: {row['state']}" for row in session["runs"])
        if result["next_cursor"]:
            lines.append(f"More runs: `{command} sessions show {session['session_id']} {result['next_cursor']}`")
        return "\n".join(lines), "answered:session-show"
    if action == "cancel":
        state = result["state"]
        verb = "Stop requested" if any(row["stop_request_sent"] for row in result["runs"]) else "Session state"
        return f"{verb} for `{result['session_id']}`: {state}.", "answered:session-cancel"
    if not result["available"]:
        return f"Session `{result['session_id']}` has no result: {result['reason']}.", "answered:session-result"
    body = result["result"] or "(empty result)"
    if result["next_cursor"]:
        body += f"\n\nMore: `{command} sessions result {result['session_id']} {result['run_id']} {result['next_cursor']}`"
    return body, "answered:session-result"
