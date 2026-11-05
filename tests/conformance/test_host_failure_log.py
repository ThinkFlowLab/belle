"""A failed turn logs structured diagnostics without its answer or error text: the harness's error type and session
lead to the harness's own record of the failure."""

import logging
from collections.abc import AsyncIterator

import pytest
from conformance_kit import worlds
from scripted_backend import ScriptedBackend

from belle_agent.backend import EVENT_ACCEPTED, EVENT_FAILED, TurnEvent, TurnRequest
from belle_agent.platforms.base import TRIGGER_MENTION


class FailingBackend(ScriptedBackend):
    def __init__(self, terminal: TurnEvent | None = None, *, crash: bool = False) -> None:
        super().__init__()
        self.terminal = terminal
        self.crash = crash

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(EVENT_ACCEPTED)
        if self.crash:
            raise RuntimeError("private answer and sk-unrecognized-secret")
        yield self.terminal or TurnEvent(EVENT_FAILED)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"failure_kind": "provider", "status": 404}, "failure_kind=provider status=404"),
        (
            {"failure_kind": "sk-unrecognized-secret", "status": "404 private answer"},
            "failure_kind=- status=-",
        ),
    ],
)
async def test_failure_log_keeps_only_known_fields(tmp_path, caplog, data, expected):
    caplog.set_level(logging.WARNING, logger="belle_agent.host")
    terminal = TurnEvent(
        EVENT_FAILED,
        text="private answer\n\nProvider returned sk-unrecognized-secret",
        data={**data, "type": "private answer", "reason": "sk-unrecognized-secret"},
    )
    async with worlds(FailingBackend(terminal), tmp_path) as build:
        world = await build()
        await world.send(world.message("go", trigger=TRIGGER_MENTION))

    (line,) = [record.getMessage() for record in caplog.records if "turn failed:" in record.getMessage()]
    assert expected in line
    assert "private answer" not in caplog.text
    assert "sk-unrecognized-secret" not in caplog.text


async def test_backend_exception_message_does_not_reach_logs(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="belle_agent.host")
    async with worlds(FailingBackend(crash=True), tmp_path) as build:
        world = await build()
        await world.send(world.message("go", trigger=TRIGGER_MENTION))

    assert "backend raised during a turn" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "sk-unrecognized-secret" not in caplog.text
    assert "private answer" not in caplog.text


async def test_failure_log_names_the_harness_error_type_and_session(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="belle_agent.host")
    terminal = TurnEvent(
        EVENT_FAILED,
        text="private answer\n\nThe model's response stopped: Compaction summary did not match the required template.",
        data={"failure_kind": "backend", "error_type": "output-limit", "backend_session": "ses_belle_0f3a9c"},
    )
    async with worlds(FailingBackend(terminal), tmp_path) as build:
        world = await build()
        await world.send(world.message("go", trigger=TRIGGER_MENTION))

    (line,) = [record.getMessage() for record in caplog.records if "turn failed:" in record.getMessage()]
    # The operator finds the harness's own record of the failure by its session, and its kind by the type.
    assert "error_type=output-limit backend_session=ses_belle_0f3a9c" in line
    assert "private answer" not in caplog.text
    assert "Compaction summary" not in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "Provider returned sk-unrecognized-secret",
        "private\nanswer",
        "output-limit\n",
        "",
        404,
        None,
        "x" * 129,
    ],
    ids=["a-sentence", "a-newline", "a-trailing-newline", "empty", "not-text", "none", "too-long"],
)
async def test_failure_log_drops_an_error_type_or_session_that_is_not_an_identifier(tmp_path, caplog, value):
    caplog.set_level(logging.WARNING, logger="belle_agent.host")
    terminal = TurnEvent(EVENT_FAILED, text="private answer", data={"error_type": value, "backend_session": value})
    async with worlds(FailingBackend(terminal), tmp_path) as build:
        world = await build()
        await world.send(world.message("go", trigger=TRIGGER_MENTION))

    (line,) = [record.getMessage() for record in caplog.records if "turn failed:" in record.getMessage()]
    assert "error_type=- backend_session=-" in line
    assert "sk-unrecognized-secret" not in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("error_type", ["provider.invalid-request", "hook_blocked", "-32603", "t" * 64])
async def test_failure_log_keeps_an_error_type_of_letters_digits_dots_underscores_and_hyphens(tmp_path, caplog, error_type):
    caplog.set_level(logging.WARNING, logger="belle_agent.host")
    terminal = TurnEvent(EVENT_FAILED, data={"error_type": error_type, "backend_session": "s1"})
    async with worlds(FailingBackend(terminal), tmp_path) as build:
        world = await build()
        await world.send(world.message("go", trigger=TRIGGER_MENTION))

    (line,) = [record.getMessage() for record in caplog.records if "turn failed:" in record.getMessage()]
    assert f"error_type={error_type} backend_session=s1" in line


async def test_failure_log_keeps_an_error_type_longer_than_a_name_out(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="belle_agent.host")
    # A session id may be as long as 128; a type over 64 is no name, and may be a credential.
    terminal = TurnEvent(EVENT_FAILED, data={"error_type": "t" * 65, "backend_session": "s" * 128})
    async with worlds(FailingBackend(terminal), tmp_path) as build:
        world = await build()
        await world.send(world.message("go", trigger=TRIGGER_MENTION))

    (line,) = [record.getMessage() for record in caplog.records if "turn failed:" in record.getMessage()]
    assert f"error_type=- backend_session={'s' * 128}" in line
