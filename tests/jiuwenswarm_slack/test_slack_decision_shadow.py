"""Test Slack decision observation and forwarding."""

import asyncio
import json
import logging
from typing import Any

import pytest
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

from belle_agent.integrations.jiuwenswarm import slack_channel as slack_connect
from belle_agent import message_decisions
from belle_agent.integrations.jiuwenswarm.slack_channel import (
    SlackChannel,
    SlackChannelConfig,
)
from belle_agent.platforms.slack import decision_questions as dq
from belle_agent.typed_decision import (
    Answer,
    Question,
    TypedDecisionClient,
    TypedDecisionError,
)

ASKER = "U0ASKER001"
BOT = "U0BOT00001"
TEAM = "T0TESTTEAM"
ROOM = "C0CHANNEL1"
TS = "1710000001.000100"


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """Give each connector an isolated dedup file."""
    built = [0]

    def path():
        built[0] += 1
        return tmp_path / f"seen-{built[0]}.json"

    monkeypatch.setattr(slack_connect, "get_slack_dedup_path", path)


@pytest.fixture(autouse=True)
def _no_workspace_config(monkeypatch):
    """Prevent tests from reading the operator's config."""
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(config_module, "get_config", lambda *a, **kw: {})


class RecordingSlackClient:
    """Every outward Slack call, in the order it was made."""

    def __init__(self, *, history: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.history = history if history is not None else []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("chat_postMessage", kwargs))
        return {"ts": "1710000099.000001"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("chat_update", kwargs))
        return {"ts": kwargs.get("ts", "")}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append(("reactions_add", kwargs))
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append(("reactions_remove", kwargs))
        return {"ok": True}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        # Recorded under its own name so the trace can show that a shadow read
        # happened without letting it count as an outward act of the turn.
        self.calls.append(("conversations_history", kwargs))
        return {"messages": list(self.history)}


class StubAdapter:
    """A decision model that answers, fails or never returns."""

    name = "stub"
    modalities = frozenset({"text"})

    def __init__(self, *, answers=None, error=None, delay=0.0) -> None:
        self._answers = answers
        self._error = error
        self._delay = delay
        self.states: list[Any] = []

    async def ask(self, state, questions: dict[str, Question]):
        self.states.append(state)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return dict(self._answers or {})


def quiet_answers() -> dict[str, Answer]:
    """The answer set that would skip the turn outright under enforcement."""
    return {
        dq.AUDIENCE: Answer(type="choice", value=dq.AUDIENCE_PERSON, confidence=0.97),
        dq.ALREADY_ANSWERED: Answer(type="noul", value=0.96),
        dq.RESPONSE_REQUESTED: Answer(type="noul", value=0.91),
        dq.CLAIM: Answer(type="noul", value=0.02),
        dq.NEEDS_WORK: Answer(type="noul", value=0.01),
        dq.WORTH_REMEMBERING: Answer(type="noul", value=0.00),
        message_decisions.INDEPENDENT_WORK: Answer(type="noul", value=0.01),
        message_decisions.CONTINUATION_CONTEXT: Answer(type="noul", value=0.01),
        dq.DESERVES_EMOJI: Answer(type="noul", value=0.01),
    }


def channel(
    *, make_gate=None, history=None, forward=False
) -> tuple[SlackChannel, RecordingSlackClient, list[Message], list[tuple]]:
    """Record outward calls with the connector's decision window."""
    built = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=[ASKER], bot_name="jiuwen", group_chat_mode="all",
            decision_forward=forward,
        ),
        RobotMessageRouter(),
    )
    built._running = True
    built._identity.bot_user_id = BOT
    client = RecordingSlackClient(history=history)
    built._client = client
    acknowledged: list[tuple] = []

    async def record_acknowledgement(*args: Any, **kwargs: Any) -> None:
        acknowledged.append((args, tuple(sorted(kwargs.items()))))

    built._acknowledge_request = record_acknowledgement  # type: ignore[method-assign]
    # ``False`` is the connector's own word for "asked, and there is none".
    gate = make_gate(built._decision_window) if make_gate is not None else None
    built._decision_gate = gate if gate is not None else False
    received: list[Message] = []
    built.on_message(received.append)
    return built, client, received, acknowledged, gate


def event() -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": ROOM,
        "user": ASKER,
        "text": "anyone remember what the retry budget ended up as",
        "ts": TS,
    }


def body() -> dict[str, Any]:
    return {"team_id": TEAM, "event_id": "Ev0000000001"}


async def trace(make_gate=None, history=None, *, is_dm=False, forward=False) -> tuple:
    """Everything one message makes this connector do, outward."""
    built, client, received, acknowledged, gate = channel(
        make_gate=make_gate, history=history, forward=forward
    )
    incoming = event()
    if is_dm:
        incoming.update(channel_type="im", channel="D0DIRECT1", text="ping")
    outcome = await built._handle_slack_event(
        incoming, body(), is_dm=is_dm, trigger="dm" if is_dm else "all"
    )
    # Let anything the shadow started run to completion before the trace is
    # read, so a difference it could make has every chance to appear.
    await asyncio.sleep(0.05)
    if gate is not None:
        await gate.close()
    turn_calls = [
        (name, kwargs)
        for name, kwargs in client.calls
        if name != "conversations_history"
    ]
    return (
        outcome,
        acknowledged,
        turn_calls,
        [(msg.req_method, msg.session_id, msg.params) for msg in received],
    )


# The trace


@pytest.mark.parametrize(
    "make_gate",
    [
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(answers=quiet_answers()), attempts=1, timeout=1.0
                ),
                recent_messages=window,
            ),
            id="a-model-that-says-stay-quiet",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(error=TypedDecisionError("endpoint refused")),
                    attempts=1,
                    timeout=1.0,
                    retry_delay=0.0,
                ),
                recent_messages=window,
            ),
            id="a-model-that-fails",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(error=RuntimeError("something nobody anticipated")),
                    attempts=1,
                    timeout=1.0,
                    retry_delay=0.0,
                ),
                recent_messages=window,
            ),
            id="a-model-that-raises-something-unexpected",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(delay=30.0), attempts=1, timeout=0.02
                ),
                recent_messages=window,
                budget=0.05,
            ),
            id="a-model-that-never-returns",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(answers={}), attempts=1, timeout=1.0
                ),
                recent_messages=window,
            ),
            id="a-model-that-answers-nothing",
        ),
    ],
)
@pytest.mark.parametrize("is_dm", [False, True], ids=["channel", "dm"])
async def test_no_decision_model_changes_what_the_connector_does(make_gate, is_dm) -> None:
    """The trace with a gate equals the trace without one, exactly."""
    without = await trace(None, is_dm=is_dm)
    with_gate = await trace(make_gate, is_dm=is_dm)
    assert with_gate == without


async def test_a_slow_model_does_not_delay_the_acknowledgement() -> None:
    built, _client, _received, acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(StubAdapter(delay=30.0), attempts=1, timeout=30.0),
            recent_messages=window,
            budget=30.0,
        )
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    await built._handle_slack_event(event(), body(), is_dm=False, trigger="all")
    elapsed = loop.time() - started
    assert acknowledged, "the acknowledgement was not sent"
    assert elapsed < 1.0, f"the handler waited {elapsed:.2f}s on the decision model"
    await gate.close()


async def test_stopping_the_channel_cancels_pending_shadow_observations() -> None:
    built, _client, _received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(StubAdapter(delay=30.0), attempts=1, timeout=30.0),
            recent_messages=window,
            budget=30.0,
        )
    )
    await built._handle_slack_event(event(), body(), is_dm=False, trigger="all")
    assert gate._tasks

    await built.stop()

    assert built._decision_gate is None
    assert not gate._tasks


@pytest.mark.parametrize("trigger,is_dm", [
    ("all", False), ("mention", False), ("reply", False),
    ("link", False), ("dm", True),
])
async def test_forwarded_answers_accompany_only_the_triggering_turn(
    trigger, is_dm, caplog
) -> None:
    """The raw answers reach each ordinary Slack turn without its disposition."""
    adapter = StubAdapter(answers=quiet_answers())
    built, _client, received, acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        ),
        forward=True,
    )
    incoming = event()
    if is_dm:
        incoming.update(channel_type="im", channel="D0DIRECT1", text="ping")
    with caplog.at_level(logging.INFO, logger=dq.logger.name):
        await built._handle_slack_event(
            incoming, body(), is_dm=is_dm, trigger=trigger
        )
    assert acknowledged
    assert len(received) == 1
    payload = received[0].params["content"]
    assert dq.ADVISORY_PREAMBLE in payload
    assert '"response_requested"' in payload
    assert '"instructions"' in payload
    assert '"criteria"' in payload
    assert '"selected_choice_meaning"' not in payload
    assert '"outcome"' not in payload
    assert '"silent"' not in payload
    assert adapter.states[0]["last_message"]["text"] == incoming["text"]
    records = [
        json.loads(line[len(dq.LOG_PREFIX):].strip())
        for line in caplog.messages if line.startswith(dq.LOG_PREFIX)
    ]
    assert len(records) == 1
    assert records[0]["advice_produced"] is True
    assert records[0]["shadow"] is False
    assert records[0]["trigger"] == trigger
    await gate.close()


@pytest.mark.parametrize("case", ["absent", "failure", "timeout", "empty"])
async def test_forwarding_unavailable_keeps_the_turn_unchanged(case) -> None:
    """An optional or failed decision call cannot block a Slack request."""
    baseline = await trace(None)
    if case == "absent":
        make_gate = None
    else:
        adapter = StubAdapter(
            answers={} if case == "empty" else None,
            error=TypedDecisionError("unavailable") if case == "failure" else None,
            delay=30.0 if case == "timeout" else 0.0,
        )
        make_gate = lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=0.02),
            recent_messages=window, budget=0.05,
        )
    assert await trace(make_gate, forward=True) == baseline


async def test_forwarding_does_not_delay_acknowledgement() -> None:
    adapter = StubAdapter(answers=quiet_answers(), delay=0.05)
    built, _client, received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        ),
        forward=True,
    )
    acknowledged_before_decision = []

    async def record_ack(*args, **kwargs):
        acknowledged_before_decision.append(not adapter.states)

    built._acknowledge_request = record_ack  # type: ignore[method-assign]
    await built._handle_slack_event(event(), body(), is_dm=False, trigger="all")
    assert acknowledged_before_decision == [True]
    assert received
    await gate.close()


async def test_failed_followup_does_not_reuse_previous_advice() -> None:
    class FirstOnlyAdapter(StubAdapter):
        async def ask(self, state, questions):
            self.states.append(state)
            if len(self.states) == 1:
                return quiet_answers()
            raise TypedDecisionError("unavailable")

    adapter = FirstOnlyAdapter()
    built, _client, received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(
                adapter, attempts=1, timeout=1.0, retry_delay=0.0
            ),
            recent_messages=window,
        ),
        forward=True,
    )
    await built._handle_slack_event(event(), body(), is_dm=False, trigger="all")
    followup = event()
    followup.update(ts="1710000002.000200", text="and now?")
    followup_body = {**body(), "event_id": "Ev0000000002"}
    await built._handle_slack_event(
        followup, followup_body, is_dm=False, trigger="all"
    )
    assert len(received) == 2
    assert dq.ADVISORY_PREAMBLE in received[0].params["content"]
    assert dq.ADVISORY_PREAMBLE not in received[1].params["content"]
    assert adapter.states[1]["last_message"]["text"] == "and now?"
    await gate.close()


async def test_queued_request_keeps_arrival_decision_in_shadow() -> None:
    adapter = StubAdapter(answers=quiet_answers())
    built, _client, received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        ),
        forward=True,
    )
    built._mid_turn_mode = lambda *args, **kwargs: "queue"  # type: ignore[method-assign]
    built.turn_initiator = lambda session_id: object()  # type: ignore[method-assign]
    outcome = await built._handle_slack_event(
        event(), body(), is_dm=False, trigger="all"
    )
    assert outcome.startswith("queued:")
    assert not received
    queued = next(iter(built._queued_messages.values()))[0].request
    assert dq.ADVISORY_PREAMBLE not in queued.params["content"]
    await asyncio.gather(*tuple(gate._tasks))
    assert len(adapter.states) == 1
    await gate.close()


async def test_forwarding_preserves_optional_reply_contract() -> None:
    built, _client, received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(
                StubAdapter(answers=quiet_answers()), attempts=1, timeout=1.0
            ),
            recent_messages=window,
        ),
        forward=True,
    )
    built._channel_reply_is_optional = lambda *args: True  # type: ignore[method-assign]
    await built._handle_slack_event(event(), body(), is_dm=False, trigger="all")
    content = received[0].params["content"]
    instruction = slack_connect.post_tool_instruction()
    assert content.endswith(instruction)
    assert content.index(dq.ADVISORY_PREAMBLE) < content.index(
        instruction
    )
    await gate.close()


@pytest.mark.parametrize("mode", ["queue", "steer"])
async def test_mid_turn_state_is_read_again_after_decision_wait(mode) -> None:
    adapter = StubAdapter(answers=quiet_answers())
    built, _client, received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        ),
        forward=True,
    )
    built._mid_turn_mode = lambda *args, **kwargs: mode  # type: ignore[method-assign]
    checks = 0

    def changing_turn(session_id):
        nonlocal checks
        checks += 1
        if mode == "queue":
            return None if checks == 1 else object()
        return object() if checks == 1 else None

    built.turn_initiator = changing_turn  # type: ignore[method-assign]
    outcome = await built._handle_slack_event(
        event(), body(), is_dm=False, trigger="all"
    )
    assert checks >= 2
    if mode == "queue":
        assert outcome.startswith("queued:")
        queued = next(iter(built._queued_messages.values()))[0].request
        assert dq.ADVISORY_PREAMBLE not in queued.params["content"]
        assert not received
    else:
        assert outcome.startswith("dispatched:")
        assert len(received) == 1
        assert received[0].params.get("input_mode") != "steer"
        assert dq.ADVISORY_PREAMBLE in received[0].params["content"]
    assert len(adapter.states) == 1
    await gate.close()


async def test_a_gate_that_cannot_even_be_started_is_swallowed() -> None:
    """``observe`` raises nothing, whatever is wrong under it."""

    class Exploding:
        def observe(self, **kwargs: Any) -> None:
            raise RuntimeError("no loop, no client, no anything")

    built, _client, _received, acknowledged, _gate = channel()
    built._decision_gate = Exploding()  # type: ignore[assignment]
    outcome = await built._handle_slack_event(
        event(), body(), is_dm=False, trigger="all"
    )
    assert outcome == (await trace(None))[0]
    assert acknowledged


# The log line


async def test_the_record_holds_the_answers_the_disposition_and_no_text(
    caplog,
) -> None:
    """The log line is the deliverable, so it is checked as one."""
    adapter = StubAdapter(answers=quiet_answers())

    def make_gate(window):
        return dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        )

    # Newest first, as Slack returns it.
    history = [
        {"user": BOT, "text": "the retry budget is four seconds", "ts": "1710000001.000099"},
        {"user": ASKER, "text": "standup in five", "ts": "1710000000.000100"},
    ]
    with caplog.at_level(logging.INFO, logger=dq.logger.name):
        await trace(make_gate, history=history)

    lines = [
        message
        for message in caplog.messages
        if message.startswith(dq.LOG_PREFIX)
    ]
    assert len(lines) == 1
    record = json.loads(lines[0][len(dq.LOG_PREFIX) :].strip())

    assert record["v"] == dq.LOG_VERSION
    assert record["question_set_version"] == 2
    assert record["shadow"] is True
    assert record["outcome"] == dq.DISPOSITION_CONTEXT
    assert record["adapter"] == "stub"
    assert record["window"] == 2
    assert isinstance(record["latency_ms"], int)
    # Enough identity to find the message again.
    assert record["team"] == TEAM
    assert record["channel"] == ROOM
    assert record["ts"] == TS
    assert record["user"] == ASKER
    assert record["chat_type"] == "channel"
    # Every answer, with its raw value and whatever scale came back with it.
    assert set(record["answers"]) == set(dq.QUESTIONS)
    assert record["answers"][dq.RESPONSE_REQUESTED] == {
        "type": "noul", "value": 0.91,
    }
    assert record["answers"][dq.AUDIENCE] == {
        "type": "choice",
        "value": dq.AUDIENCE_PERSON,
        "confidence": 0.97,
    }
    assert record["answers"][dq.CLAIM] == {"type": "noul", "value": 0.02}
    # No message text, here or anywhere: these lines persist and the content
    # is the users'.
    rendered = lines[0]
    assert "retry budget" not in rendered
    assert "standup" not in rendered


async def test_a_failed_decision_is_recorded_so_the_denominator_survives(
    caplog,
) -> None:
    def make_gate(window):
        return dq.ShadowDecisionGate(
            TypedDecisionClient(
                StubAdapter(error=TypedDecisionError("endpoint refused")),
                attempts=1,
                timeout=1.0,
                retry_delay=0.0,
            ),
            recent_messages=window,
        )

    with caplog.at_level(logging.INFO, logger=dq.logger.name):
        await trace(make_gate)
    lines = [m for m in caplog.messages if m.startswith(dq.LOG_PREFIX)]
    assert len(lines) == 1
    record = json.loads(lines[0][len(dq.LOG_PREFIX) :].strip())
    assert record["outcome"] == "declined"
    assert "TypedDecisionError" in record["error"]
    assert "answers" not in record


async def test_the_window_holds_the_bot_s_own_turns_into_the_state() -> None:
    adapter = StubAdapter(answers=quiet_answers())
    await trace(
        lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        ),
        # Newest first, which is what ``conversations.history`` returns. The
        # state reads oldest first, in the order the conversation happened.
        history=[
            {"user": BOT, "text": "I do, since tuesday", "ts": "1710000000.000200"},
            {"user": ASKER, "text": "who owns the harness", "ts": "1710000000.000100"},
        ],
    )
    assert adapter.states, "the decision model was never asked"
    state = adapter.states[0]
    authors = [entry["author"] for entry in state["recent_messages"]]
    assert authors == [f"<@{ASKER}>", "jiuwen"]
    assert state["bot_name"] == f"jiuwen (mentioned as <@{BOT}>)"
    assert state["channel"] == ROOM
    assert state["last_message"]["author"] == f"<@{ASKER}>"


async def test_shadow_preserves_the_leading_bot_mention() -> None:
    adapter = StubAdapter(answers=quiet_answers())
    built, _client, received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        )
    )
    incoming = event()
    incoming["text"] = f"<@{BOT}> are you available?"
    await built._handle_slack_event(incoming, body(), is_dm=False, trigger="mention")
    await asyncio.gather(*tuple(gate._tasks))
    assert adapter.states[0]["last_message"]["text"] == incoming["text"]
    assert received
    await gate.close()


async def test_shadow_history_excludes_the_target_and_newer_messages() -> None:
    built, client, _received, _acknowledged, _gate = channel(history=[
        {"user": BOT, "text": "new reply", "ts": "1710000001.000101"},
        {"user": ASKER, "text": "target", "ts": TS},
        {"user": BOT, "text": "earlier reply", "ts": "1710000001.000099"},
        {"user": ASKER, "text": "unknown time"},
        {"user": ASKER, "text": "invalid time", "ts": "NaN"},
    ])
    window = await built._decision_window(ROOM, 12, TS)
    assert [entry["text"] for entry in window] == ["earlier reply"]
    assert client.calls == [("conversations_history", {
        "channel": ROOM, "limit": 12, "latest": TS, "inclusive": False,
    })]


@pytest.mark.parametrize("timestamp", ["", "invalid", "NaN", "Infinity", "-1.0"])
async def test_shadow_history_skips_reads_without_a_valid_cutoff(timestamp) -> None:
    built, client, _received, _acknowledged, _gate = channel()
    assert await built._decision_window(ROOM, 12, timestamp) == []
    assert client.calls == []
