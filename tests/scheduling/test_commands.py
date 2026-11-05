"""Scheduled-job text commands reuse the trusted tool boundary."""

import json

import pytest

from belle_agent.platforms.base import InboundMessage
from belle_agent.scheduling.commands import SCHEDULE_SUBCOMMANDS, schedule_command


class Toolkit:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, dict, InboundMessage]] = []
        self.result = result or {"ok": True}

    async def invoke_at(self, name: str, arguments: dict, *, at: InboundMessage) -> str:
        self.calls.append((name, arguments, at))
        return json.dumps(self.result)


AT = InboundMessage("loopback", "W", "room", "", "alice", "", chat_type="channel")
JOB_ID = "job-1234567890"


@pytest.mark.parametrize(("action", "arguments", "method", "kwargs"), [
    ("list", "", "schedule_list", {}),
    ("show", JOB_ID, "schedule_list", {"job_id": JOB_ID}),
    ("pause", JOB_ID, "schedule_update", {"job_id": JOB_ID, "enabled": False}),
    ("resume", JOB_ID, "schedule_update", {"job_id": JOB_ID, "enabled": True}),
    ("delete", JOB_ID, "schedule_delete", {"job_id": JOB_ID, "keep_history": True}),
    ("run", JOB_ID, "schedule_run_now", {"job_id": JOB_ID}),
])
async def test_schedule_command_uses_toolkit_with_command_place(action, arguments, method, kwargs):
    result = {"ok": True, "jobs": [{"job_id": JOB_ID, "name": "Daily", "enabled": True, "next_runs": []}]}
    result["run"] = {"session_id": "ses-1234567890"}
    toolkit = Toolkit(result)
    sub = next(item for item in SCHEDULE_SUBCOMMANDS if item.words[1] == action)
    text, outcome = await schedule_command(toolkit, AT, sub, arguments, "/belle")
    assert toolkit.calls == [(method, kwargs, AT)]
    assert text and outcome == f"answered:schedule-{action}"


@pytest.mark.parametrize(("action", "arguments"), [("list", JOB_ID), ("show", ""), ("pause", f"{JOB_ID} extra")])
async def test_schedule_command_refuses_invalid_usage_before_toolkit(action, arguments):
    toolkit = Toolkit()
    sub = next(item for item in SCHEDULE_SUBCOMMANDS if item.words[1] == action)
    text, outcome = await schedule_command(toolkit, AT, sub, arguments, "/belle")
    assert text.startswith("Usage:")
    assert outcome == "refused:schedule-usage"
    assert not toolkit.calls


async def test_schedule_command_preserves_toolkit_refusal():
    toolkit = Toolkit({"ok": False, "error": "not_job_owner", "detail": "Only the creator may delete this job"})
    sub = next(item for item in SCHEDULE_SUBCOMMANDS if item.words == ("schedule", "delete"))
    assert await schedule_command(toolkit, AT, sub, JOB_ID, "/belle") == (
        "Only the creator may delete this job", "refused:not_job_owner"
    )


async def test_list_escapes_message_markup_in_job_names():
    toolkit = Toolkit({"ok": True, "jobs": [{
        "job_id": JOB_ID, "name": "alert <!channel> & friends", "enabled": True, "next_runs": []
    }]})
    sub = next(item for item in SCHEDULE_SUBCOMMANDS if item.words == ("schedule", "list"))
    text, _ = await schedule_command(toolkit, AT, sub, "", "/belle")
    assert "<!channel>" not in text
    assert "&lt;!channel&gt; &amp; friends" in text
