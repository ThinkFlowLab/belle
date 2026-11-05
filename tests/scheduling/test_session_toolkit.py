"""Session tool identity, audience, and paging checks."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from belle_agent.backend import OP_CONTINUE, BackendCapabilities, Catalog, ToolCall, TurnScope
from belle_agent.models import ConfiguredModels
from belle_agent.platforms.base import TRIGGER_DM, InboundMessage, PlaceCheck
from belle_agent.scheduling.address import Address
from belle_agent.scheduling.records import RUN_STATES, DetachedSession, SessionRun
from belle_agent.scheduling.session_cards import session_cards
from belle_agent.scheduling.session_commands import SESSION_SUBCOMMANDS, session_command
from belle_agent.scheduling.session_toolkit import _KINDS, _STARTED_BY, SessionToolkit
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.store import JobStore
from belle_agent.scheduling.toolkit import ScheduleToolkit
from belle_agent.scheduling.turnspec import ScopeLimits
from belle_agent.scopes.people import EMPTY_DIRECTORY, PeopleDirectory
from belle_agent.tools.host import BoundTurn, ToolHost

from .fake_clock import FakeClock

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


class Places:
    workspaces = frozenset({"one", "two"})

    def __init__(self) -> None:
        self.readable = True
        self.dm_type = "direct"

    async def can_read(self, user: str, conversation: str, *, workspace: str = "") -> bool:
        return self.readable

    async def check_place(self, conversation: str, thread: str, *, workspace: str = "") -> PlaceCheck:
        return PlaceCheck(True, True, chat_type=self.dm_type if conversation.startswith("dm-") else "channel")


class Host:
    def __init__(self, places: Places) -> None:
        self.platforms = {"loopback": SimpleNamespace(posting_tool="post_message")}
        self.capabilities = BackendCapabilities("opencode")
        self.directory = EMPTY_DIRECTORY
        self._places = places
        self.model = ""
        self.scope_calls = 0

    async def catalog(self) -> Catalog:
        return Catalog(models=ConfiguredModels(names=("old-selection", "new-selection")))

    def scope_limits(self, place: Address, user: str, chat_type: str) -> ScopeLimits:
        self.scope_calls += 1
        return ScopeLimits(place, TurnScope(model=self.model))

    def session_posting_tool(self, platform: str) -> str:
        return str(self.platforms[platform].posting_tool)

    def places(self, platform: str) -> Places | None:
        return self._places if platform == "loopback" else None


class Runner:
    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.starts = 0

    async def request_session(self, session: DetachedSession) -> DetachedSession:
        key = session.runs[0].request.idempotency_key  # type: ignore[union-attr]
        async with self.store.transaction() as snapshot:
            for existing in snapshot.sessions.values():
                if existing["runs"][0]["request"]["idempotency_key"] == key:
                    return DetachedSession.from_json(existing, "session")
            self.starts += 1
            snapshot.sessions[session.session_id] = session.to_json()
            return session

    async def cancel_session(self, session_id: str) -> dict[str, Any]:
        async with self.store.transaction() as snapshot:
            run = snapshot.sessions[session_id]["runs"][-1]
            run.update(state="cancelled", stop_requested=True, ended_iso_utc="2026-10-18T12:00:01Z")
        row = {"run_id": run["run_id"], "state": "cancelled", "stop_requested": False, "stop_request_sent": False}
        return {"session_id": session_id, "state": "cancelled", "runs": [row]}

    async def continue_session(
        self, session_id: str, prompt: str, turn: dict[str, Any], chat_type: str, key: str,
    ) -> tuple[DetachedSession, SessionRun]:
        async with self.store.transaction() as snapshot:
            record = snapshot.sessions[session_id]
            if not any((run["request"] or {}).get("idempotency_key") == key for run in record["runs"]):
                record["origin_chat_type"] = chat_type
                record["runs"].append({
                    **record["runs"][-1], "run_id": "run-continued1", "prompt": prompt, "turn": turn,
                    "state": "pending", "result": "", "result_available": False, "started_by": "request",
                    "delivery": "notice", "clock": None, "report": {"state": "pending", "message_id": "", "error": ""},
                    "request": {"idempotency_key": key, "fingerprint": "f", "inbound": {},
                                "requested_iso_utc": "2026-10-18T12:00:00Z", "approved_readers": None,
                                "continues": True},
                })
            session = DetachedSession.from_json(record, "session")
        return session, session.runs[-1]


@pytest.fixture
def rig(tmp_path: Path) -> tuple[SessionToolkit, JobStore, Runner, Places]:
    clock = FakeClock(NOW)
    store = JobStore(tmp_path, clock=clock)
    places = Places()
    host = Host(places)
    settings = SchedulingSettings()
    schedule = ScheduleToolkit(store, None, settings, host, clock)  # type: ignore[arg-type]
    runner = Runner(store)
    return SessionToolkit(store, runner, schedule, clock), store, runner, places


def at(*, workspace: str = "one", conversation: str = "room", user: str = "alice",
       event_id: str = "event-one") -> InboundMessage:
    return InboundMessage("loopback", workspace, conversation, "", user, "", event_id=event_id, chat_type="channel")


def dm_at(*, workspace: str = "one", user: str = "alice") -> InboundMessage:
    return InboundMessage("loopback", workspace, f"dm-{user}", "", user, "",
                          chat_type="direct", trigger=TRIGGER_DM)


def read(value: str) -> dict[str, Any]:
    return json.loads(value)


@pytest.mark.asyncio
async def test_start_retries_one_trusted_command_without_duplicate(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, _, runner, _ = rig
    first = read(await toolkit.invoke_at("session_start", {"prompt": "Write a note"}, at=at()))
    again = read(await toolkit.invoke_at("session_start", {"prompt": "Write a note"}, at=at()))
    assert first["ok"] and first["session_id"] == again["session_id"]
    assert runner.starts == 1


@pytest.mark.asyncio
async def test_start_retries_one_bound_tool_call_without_duplicate(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, runner, _ = rig
    host = ToolHost([toolkit])
    host.bind("backend-conversation", BoundTurn(
        "turn-one", metadata={"belle_inbound": {"message_id": "original-message",
                                                  "reply_thread": "original-reply",
                                                  "envelope": "original-envelope",
                                                  "reply_optional": True}, "belle_place": {
            "platform": "loopback", "workspace": "one", "conversation": "room", "thread": "",
            "sender": "alice", "chat_type": "channel",
        }},
    ))
    call = ToolCall("backend-conversation", "session_start", {"prompt": "task"}, "call-one")
    first = read(await host.call(call))
    second = read(await host.call(call))
    assert first["session_id"] == second["session_id"] and runner.starts == 1
    async with store.transaction() as snapshot:
        assert snapshot.sessions[first["session_id"]]["runs"][0]["request"]["inbound"] == {
            "message_id": "original-message", "reply_thread": "original-reply",
            "envelope": "original-envelope", "reply_optional": True,
        }


@pytest.mark.asyncio
async def test_distinct_tool_calls_in_one_turn_start_distinct_sessions(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, _, runner, _ = rig
    host = ToolHost([toolkit])
    host.bind("backend-conversation", BoundTurn(
        "turn-one", metadata={"belle_place": {
            "platform": "loopback", "workspace": "one", "conversation": "room", "thread": "",
            "sender": "alice", "chat_type": "channel",
        }},
    ))
    first = read(await host.call(ToolCall("backend-conversation", "session_start", {"prompt": "first"}, "call-one")))
    second = read(await host.call(ToolCall("backend-conversation", "session_start", {"prompt": "second"}, "call-two")))
    assert first["session_id"] != second["session_id"] and runner.starts == 2


@pytest.mark.asyncio
async def test_start_requires_valid_input_and_stable_call_id(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, _, runner, _ = rig
    for args, field in (({"prompt": ""}, "prompt"), ({"prompt": "x", "notify": "yes"}, "notify"),
                        ({"prompt": "x", "questions": []}, "questions"),
                        ({"prompt": "x", "turn": {"unknown": "x"}}, "turn")):
        answer = read(await toolkit.invoke_at("session_start", args, at=at(event_id="")))
        assert not answer["ok"] and field in answer["detail"]
    answer = read(await toolkit.invoke_at("session_start", {"prompt": "x"}, at=at(event_id="")))
    assert answer["error"] == "capability_unavailable"
    assert runner.starts == 0
    listed = read(await toolkit.invoke_at("session_list", {"started_by": []}, at=at()))
    assert listed["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_results_follow_current_membership_and_workspace(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, places = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "private task"}, at=at()))
    session_id = started["session_id"]
    async with store.transaction() as snapshot:
        run = snapshot.sessions[session_id]["runs"][0]
        run.update(state="succeeded", result="private result", result_available=True,
                   ended_iso_utc="2026-10-18T12:00:01Z")
    assert read(await toolkit.invoke_at("session_result", {"session_id": session_id},
                                        at=at()))["error"] == "session_not_found"
    visible = read(await toolkit.invoke_at("session_result", {"session_id": session_id}, at=dm_at()))
    assert visible["result"] == "private result"
    assert read(await toolkit.invoke_at("session_result", {"session_id": session_id},
                                        at=dm_at(user="bob")))["error"] == "session_not_found"
    places.dm_type = "private"
    assert read(await toolkit.invoke_at("session_result", {"session_id": session_id},
                                        at=dm_at()))["error"] == "session_not_found"
    places.dm_type = "direct"
    assert read(await toolkit.invoke_at("session_get", {"session_id": session_id},
                                        at=at(workspace="two")))["error"] == "session_not_found"
    places.readable = False
    assert read(await toolkit.invoke_at("session_result", {"session_id": session_id},
                                        at=dm_at()))["error"] == "session_not_found"


@pytest.mark.asyncio
async def test_result_pages_and_listing_do_not_leak_prompt(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "secret prompt"}, at=at()))
    session_id = started["session_id"]
    async with store.transaction() as snapshot:
        run = snapshot.sessions[session_id]["runs"][0]
        run.update(state="succeeded", result="abcdef", result_available=True,
                   ended_iso_utc="2026-10-18T12:00:01Z")
    listed = read(await toolkit.invoke_at("session_list", {}, at=at()))
    assert len(listed["sessions"]) == 1
    assert "secret prompt" not in json.dumps(listed)
    shown = read(await toolkit.invoke_at("session_get", {"session_id": session_id}, at=at()))
    assert "abcdef" not in json.dumps(shown)
    first = read(await toolkit.invoke_at("session_result",
                                         {"session_id": session_id, "result_max_chars": 3}, at=dm_at()))
    assert first["result"] == "abc" and first["result_truncated"]
    second = read(await toolkit.invoke_at("session_result",
                                          {"session_id": session_id, "result_max_chars": 3,
                                           "cursor": first["next_cursor"]}, at=dm_at()))
    assert second["result"] == "def" and second["next_cursor"] is None


@pytest.mark.asyncio
async def test_result_reports_unretained_final(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "task"}, at=at()))
    session_id = started["session_id"]
    async with store.transaction() as snapshot:
        snapshot.sessions[session_id]["runs"][0].update(
            state="succeeded", outcome="posted-by-tool", result="", result_available=False,
            ended_iso_utc="2026-10-18T12:00:01Z",
        )
    result = read(await toolkit.invoke_at("session_result", {"session_id": session_id}, at=dm_at()))
    assert result["available"] is False and result["reason"] == "not_retained"


@pytest.mark.asyncio
async def test_result_reports_failure_without_notification(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "task", "notify": False}, at=at()))
    session_id = started["session_id"]
    async with store.transaction() as snapshot:
        snapshot.sessions[session_id]["runs"][0].update(
            state="failed", outcome="failed", error="backend refused the request",
            ended_iso_utc="2026-10-18T12:00:01Z",
        )
    result = read(await toolkit.invoke_at("session_result", {"session_id": session_id}, at=dm_at()))
    assert result["available"] is False and result["error"] == "backend refused the request"


@pytest.mark.asyncio
async def test_cancel_requires_requester(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, _, _, _ = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "task"}, at=at()))
    session_id = started["session_id"]
    denied = read(await toolkit.invoke_at("session_cancel", {"session_id": session_id},
                                          at=at(user="bob")))
    assert denied["error"] == "session_not_found"
    cancelled = read(await toolkit.invoke_at("session_cancel", {"session_id": session_id}, at=at()))
    assert cancelled["state"] == "cancelled" and cancelled["runs"][0]["stop_request_sent"] is False


@pytest.mark.asyncio
async def test_continue_rechecks_authority_scope_and_result_visibility(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, places = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "first"}, at=at()))
    session_id = started["session_id"]
    async with store.transaction() as snapshot:
        record = snapshot.sessions[session_id]
        record["runs"][0]["turn"] = {"model": "old-selection"}
        record["runs"][0].update(
            state="succeeded", result="first result", result_available=True,
            ended_iso_utc=NOW.isoformat(),
        )
    toolkit.schedule.host.capabilities = BackendCapabilities("opencode", operations=frozenset({OP_CONTINUE}))
    assert "session_continue" in {card["name"] for card in toolkit.get_tool_definitions()}
    for inbound in (at(user="bob"), at(workspace="two")):
        refused = read(await toolkit.invoke_at("session_continue", {
            "session_id": session_id, "prompt": "second",
        }, at=inbound))
        assert refused["error"] == "session_not_found"
    places.readable = False
    refused = read(await toolkit.invoke_at("session_continue", {
        "session_id": session_id, "prompt": "second",
    }, at=at(event_id="second-event")))
    assert refused["error"] == "session_not_found"
    places.readable = True
    toolkit.schedule.host.model = "new-selection"
    stale = read(await toolkit.invoke_at("session_continue", {
        "session_id": session_id, "prompt": "second", "turn": {"model": "old-selection"},
    }, at=at(event_id="second-event")))
    assert stale["error"] == "turn_out_of_scope"
    before = toolkit.schedule.host.scope_calls
    continued = read(await toolkit.invoke_at("session_continue", {
        "session_id": session_id, "prompt": "second",
    }, at=at(event_id="second-event")))
    assert continued["ok"] and continued["run_id"] == "run-continued1"
    assert toolkit.schedule.host.scope_calls > before
    assert "first result" not in json.dumps(continued)
    async with store.transaction() as snapshot:
        assert snapshot.sessions[session_id]["runs"][-1]["turn"] == {"model": "", "skills": [],
                                                         "expert": "", "subagents": None}
    async with store.transaction() as snapshot:
        first_id = snapshot.sessions[session_id]["runs"][0]["run_id"]
    old = read(await toolkit.invoke_at("session_result", {
        "session_id": session_id, "run_id": first_id,
    }, at=dm_at()))
    assert old["result"] == "first result"


@pytest.mark.asyncio
async def test_unsupported_backend_refuses_continuation(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, _, _, _ = rig
    toolkit.schedule.host.capabilities = BackendCapabilities("docker-agent")
    assert "session_continue" not in {card["name"] for card in toolkit.get_tool_definitions()}
    answer = read(await toolkit.invoke_at("session_continue", {
        "session_id": "ses-1234567890", "prompt": "next",
    }, at=at()))
    assert answer["error"] == "capability_unavailable"


@pytest.mark.asyncio
async def test_operator_can_continue_from_origin_under_requester_scope(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "first"}, at=at()))
    async with store.transaction() as snapshot:
        snapshot.sessions[started["session_id"]]["runs"][0].update(
            state="succeeded", ended_iso_utc=NOW.isoformat()
        )
    toolkit.schedule.settings = SchedulingSettings(operator_role="operators")
    toolkit.schedule.host.directory = PeopleDirectory(
        people={"bob": {"loopback": ("bob",)}}, roles={"operators": ("bob",)}
    )
    toolkit.schedule.host.capabilities = BackendCapabilities("opencode", operations=frozenset({OP_CONTINUE}))
    answer = read(await toolkit.invoke_at("session_continue", {
        "session_id": started["session_id"], "prompt": "next",
    }, at=at(user="bob", event_id="operator-event")))
    assert answer["ok"]
    async with store.transaction() as snapshot:
        assert snapshot.sessions[started["session_id"]]["requested_by_user_id"] == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["command", "tool"])
async def test_continue_retries_one_authenticated_call_once(rig, source: str) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    started = read(await toolkit.invoke_at("session_start", {"prompt": "first"}, at=at()))
    session_id = started["session_id"]
    async with store.transaction() as snapshot:
        snapshot.sessions[session_id]["runs"][0].update(
            state="succeeded", ended_iso_utc=NOW.isoformat()
        )
    toolkit.schedule.host.capabilities = BackendCapabilities("opencode", operations=frozenset({OP_CONTINUE}))
    args = {"session_id": session_id, "prompt": "next"}
    if source == "command":
        inbound = at(event_id="continue-event")
        first = read(await toolkit.invoke_at("session_continue", args, at=inbound))
        again = read(await toolkit.invoke_at("session_continue", args, at=inbound))
    else:
        host = ToolHost([toolkit])
        host.bind("backend-conversation", BoundTurn(
            "turn-one", metadata={"belle_place": {
                "platform": "loopback", "workspace": "one", "conversation": "room", "thread": "",
                "sender": "alice", "chat_type": "channel",
            }},
        ))
        call = ToolCall("backend-conversation", "session_continue", args, "continue-call")
        first = read(await host.call(call))
        again = read(await host.call(call))
    assert first["ok"] and first["run_id"] == again["run_id"]
    async with store.transaction() as snapshot:
        assert len(snapshot.sessions[session_id]["runs"]) == 2


@pytest.mark.asyncio
async def test_command_and_card_contracts(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, _, _, _ = rig
    cards = {card["name"]: card for card in session_cards()}
    assert set(cards) == {"session_start", "session_continue", "session_list", "session_get",
                          "session_result", "session_cancel"}
    assert "session_continue" not in {card["name"] for card in toolkit.get_tool_definitions()}
    assert cards["session_start"]["input_params"]["required"] == ["prompt"]
    for name in ("session_get", "session_result", "session_cancel"):
        assert cards[name]["input_params"]["required"] == ["session_id"]
    bg = SESSION_SUBCOMMANDS[0]
    answer, outcome = await session_command(toolkit, at(), bg, "write a note", "/belle")
    assert outcome == "answered:session-start" and "ses-" in answer
    answer, outcome = await session_command(toolkit, at(event_id=""), bg, "write a note", "/belle")
    assert outcome == "refused:capability_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["command", "tool"])
async def test_missing_posting_capability_refuses_before_storing(rig, source: str) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, runner, _ = rig
    toolkit.schedule.host.platforms["loopback"].posting_tool = ""
    if source == "command":
        answer = read(await toolkit.invoke_at("session_start", {"prompt": "task"}, at=at()))
    else:
        host = ToolHost([toolkit])
        host.bind("backend-conversation", BoundTurn("turn-one", metadata={"belle_place": {
            "platform": "loopback", "workspace": "one", "conversation": "room", "thread": "",
            "sender": "alice", "chat_type": "channel",
        }}))
        answer = read(await host.call(ToolCall("backend-conversation", "session_start", {"prompt": "task"}, "call")))
    assert answer["error"] == "capability_unavailable" and "posting tool" in answer["detail"]
    assert runner.starts == 0
    async with store.transaction() as snapshot:
        assert snapshot.sessions == {}


async def test_continue_authority(rig) -> None:  # type: ignore[no-untyped-def]
    from belle_agent.scheduling.records import NO_DELETION, ClockStart, new_run

    toolkit, store, _, _ = rig
    origin = {"platform": "loopback", "workspace": "one", "conversation": "room", "thread": ""}
    run = new_run(started_by="clock", prompt="", turn={}, questions="refuse", place=origin, delivery="post_final",
                  owner="", clock=ClockStart("2026-10-18T11:00:00Z", 0, 0, "job-0000000001:1"))
    session = DetachedSession("ses-sched00001", "scheduled", "job-0000000001", "fresh", "opencode", origin, "channel",
                      "alice", "2026-10-18T11:00:00Z", NO_DELETION, [replace(
                          run, state="succeeded", ended_iso_utc="2026-10-18T11:00:01Z")])
    async with store.transaction() as snapshot:
        snapshot.deleted["job-0000000001"] = {"origin": origin, "deliver_to": origin, "created_by_user_id": "alice",
                                              "history_kept": True, "unrun": [], "dropped_through_iso_utc": "", "backend": "opencode"}
        snapshot.sessions[session.session_id] = session.to_json()
    toolkit.schedule.host.capabilities = BackendCapabilities("opencode", operations=frozenset({OP_CONTINUE}))
    toolkit.schedule.settings = SchedulingSettings(operator_role="operators")
    toolkit.schedule.host.directory = PeopleDirectory(
        people={"bob": {"loopback": ("bob",)}}, roles={"operators": ("bob",)}
    )
    args = {"session_id": session.session_id, "prompt": "next"}
    refused = read(await toolkit.invoke_at("session_continue", args, at=at(user="carol", event_id="c")))
    assert refused["error"] == "session_not_found"
    for user in ("alice", "bob"):
        async with store.transaction() as snapshot:
            snapshot.sessions[session.session_id]["runs"] = snapshot.sessions[session.session_id]["runs"][:1]
        answer = read(await toolkit.invoke_at("session_continue", args, at=at(user=user, event_id=user)))
        assert answer["ok"], answer


async def test_session_list_refuses_turn_filters(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit = rig[0]
    for arguments, field, accepted in (({"kinds": ["awaited"]}, "kinds", "scheduled, standalone"),
                                       ({"started_by": "message"}, "started_by", "clock, request")):
        answer = read(await toolkit.invoke_at("session_list", arguments, at=at()))
        assert answer["error"] == "invalid_arguments"
        assert field in answer["detail"] and accepted in answer["detail"]
        assert "The session tools serve detached sessions only." in answer["detail"]


async def test_session_list_has_no_page_cap(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit = rig[0]
    for number in range(3):
        assert read(await toolkit.invoke_at("session_start", {"prompt": "task"}, at=at(event_id=f"e{number}")))["ok"]
    assert len(read(await toolkit.invoke_at("session_list", {"page_size": 500}, at=at()))["sessions"]) == 3
    toolkit.schedule.host.capabilities = replace(toolkit.schedule.host.capabilities, tool_result_max_chars=200)
    refused = read(await toolkit.invoke_at("session_list", {"page_size": 500}, at=at()))
    assert refused["error"] == "result_too_large" and "Lower page_size." in refused["detail"]


async def test_session_result_has_no_length_cap(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    session_id = read(await toolkit.invoke_at("session_start", {"prompt": "task"}, at=at()))["session_id"]
    async with store.transaction() as snapshot:
        snapshot.sessions[session_id]["runs"][0].update(
            state="succeeded", result="x" * 10000, result_available=True, ended_iso_utc="2026-10-18T12:00:01Z")
    whole = read(await toolkit.invoke_at("session_result", {"session_id": session_id, "result_max_chars": 100000},
                                         at=dm_at()))
    assert whole["result"] == "x" * 10000 and whole["result_truncated"] is False
    toolkit.schedule.host.capabilities = replace(toolkit.schedule.host.capabilities, tool_result_max_chars=5000)
    refused = read(await toolkit.invoke_at("session_result", {"session_id": session_id, "result_max_chars": 100000},
                                           at=dm_at()))
    assert refused["error"] == "result_too_large" and "Lower result_max_chars." in refused["detail"]


def test_cards_match_the_contract() -> None:
    cards = {card["name"]: card["input_params"] for card in session_cards()}
    listing = cards["session_list"]["properties"]
    for values, accepted in ((listing["kinds"]["items"]["enum"], _KINDS), (listing["started_by"]["enum"], _STARTED_BY),
                             (listing["states"]["items"]["enum"], RUN_STATES)):
        assert len(values) == len(set(values)) and set(values) == accepted
    assert "maximum" not in listing["page_size"]
    assert "maximum" not in cards["session_result"]["properties"]["result_max_chars"]
    assert "run_id" in cards["session_result"]["properties"]
    assert cards["session_continue"]["required"] == ["session_id", "prompt"]
    assert "required" not in cards["session_list"]


async def test_session_get_shows_deletion_and_expiry(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    session_id = read(await toolkit.invoke_at("session_start", {"prompt": "task", "notify": False}, at=at()))["session_id"]
    shown = read(await toolkit.invoke_at("session_get", {"session_id": session_id}, at=at()))["session"]
    assert shown["expires_iso_utc"] is None and shown["deletion"]["state"] == "none"
    assert set(shown["deletion"]) == {"state", "attempts", "last_error", "last_attempt_iso_utc"}
    async with store.transaction() as snapshot:
        snapshot.sessions[session_id]["runs"][0].update(state="succeeded", ended_iso_utc="2026-10-18T12:00:01Z")
    shown = read(await toolkit.invoke_at("session_get", {"session_id": session_id}, at=at()))["session"]
    assert shown["expires_iso_utc"] == "2026-10-21T12:00:01Z"
    assert [run["run_id"] for run in shown["runs"]] == [shown["run_id"]]


async def test_session_get_pages_the_runs_newest_first(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    session_id = read(await toolkit.invoke_at("session_start", {"prompt": "task", "notify": False}, at=at()))["session_id"]
    async with store.transaction() as snapshot:
        runs = snapshot.sessions[session_id]["runs"]
        runs[0].update(state="succeeded", ended_iso_utc="2026-10-18T12:00:01Z")
        runs.extend({**runs[0], "run_id": f"run-000000000{number}"} for number in (2, 3))
    first = read(await toolkit.invoke_at("session_get", {"session_id": session_id, "page_size": 2}, at=at()))
    assert [run["run_id"] for run in first["session"]["runs"]] == ["run-0000000003", "run-0000000002"]
    second = read(await toolkit.invoke_at("session_get", {"session_id": session_id, "page_size": 2,
                                                          "cursor": first["next_cursor"]}, at=at()))
    assert [run["run_id"] for run in second["session"]["runs"]] == [runs[0]["run_id"]]
    assert second["next_cursor"] is None
    async with store.transaction() as snapshot:
        snapshot.sessions[session_id]["runs"].pop(1)
    gone = read(await toolkit.invoke_at("session_get", {"session_id": session_id, "cursor": first["next_cursor"]},
                                        at=at()))
    assert gone["error"] == "invalid_arguments" and "without cursor" in gone["detail"]


async def test_sessions_result_command_takes_a_run_id(rig) -> None:  # type: ignore[no-untyped-def]
    toolkit, store, _, _ = rig
    session_id = read(await toolkit.invoke_at("session_start", {"prompt": "task"}, at=at()))["session_id"]
    async with store.transaction() as snapshot:
        run = snapshot.sessions[session_id]["runs"][0]
        run.update(state="succeeded", result="the answer", result_available=True, ended_iso_utc="2026-10-18T12:00:01Z")
    sub = next(item for item in SESSION_SUBCOMMANDS if item.words == ("sessions", "result"))
    answer, outcome = await session_command(toolkit, dm_at(), sub, f"{session_id} {run['run_id']}", "/belle")
    assert (answer, outcome) == ("the answer", "answered:session-result")
    _, outcome = await session_command(toolkit, dm_at(), sub, f"{session_id} {run['run_id']} a b", "/belle")
    assert outcome == "refused:session-usage"
