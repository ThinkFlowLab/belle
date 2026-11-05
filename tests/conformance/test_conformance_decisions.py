"""The decision gate beside a turn: what Belle asks a typed-decision model about a message, and what it does with the answer.

The decision model is ``decision_standin``: an endpoint of its own in the
test process, whose log holds nothing a turn sent the conversation model.
The answers are logged or handed to the turn as advice. They decide nothing:
every check that counts turns finds one per message, with the gate and
without it.
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest
from conformance_kit import ModelTurn, World
from decision_standin import KEY, KEY_ENV, DecisionStandIn, logged

from belle_agent import host, message_decisions
from belle_agent.config import BelleConfigError
from belle_agent.host import decisions as host_decisions
from belle_agent.message_decisions import ADVISORY_PREAMBLE, AUDIENCE, AUDIENCE_PERSON, QUESTIONS
from belle_agent.platforms.base import InboundEvent

AMBIENT = """
scopes:
  - match: {channel: loopback}
    delivery: {mode: [all], reply: optional}
"""


@pytest.fixture
def decisions(monkeypatch):
    """A decision endpoint, and its key where the host reads it."""
    monkeypatch.setenv(KEY_ENV, KEY)
    endpoint = DecisionStandIn()
    yield endpoint
    endpoint.close()


async def asked(endpoint: DecisionStandIn, count: int) -> list[dict]:
    """Wait until the endpoint has been asked ``count`` times. A logged decision waits for nobody."""
    async with asyncio.timeout(10):
        while len(endpoint.log) < count:
            await asyncio.sleep(0.01)
    return endpoint.states()


def advised(world: World) -> list[bool]:
    return [ADVISORY_PREAMBLE in request.text for request in world.requests]


# ── no decision endpoint ────────────────────────────────────────────────


@pytest.mark.parametrize("credentials", ["no block", "no key", "no endpoint"])
async def test_with_no_decision_endpoint_nothing_is_asked_and_a_turn_is_what_it_was(build_world, decisions, monkeypatch, credentials):
    settings = {"no block": "", "no key": decisions.config(forward=True), "no endpoint": 'models: {decision: {entries: [{id: empty, api: openrouter, api_base: "", api_key: "", model_name: ""}]}}\n'}
    if credentials == "no key":
        monkeypatch.delenv(KEY_ENV)
    world = await build_world(settings[credentials] + AMBIENT)

    await world.send(world.message("is anybody there?"))

    assert world.host._on().decisions is None
    assert decisions.log == []
    assert world.texts() == ["is anybody there?"]
    assert world.outcomes() == [host.OUTCOME_WITHHELD]


# ── logged ───────────────────────────────────────────────────────────────


async def test_a_logged_decision_asks_about_each_admitted_message_and_changes_no_turn(build_world, decisions, caplog):
    with caplog.at_level(logging.INFO, logger="belle_agent.message_decisions"):
        world = await build_world(decisions.config(forward=False) + AMBIENT)
        await world.send(world.message("could someone check the deploy?"))
        (state,) = await asked(decisions, 1)
        (record,) = await logged(caplog)

    assert state["last_message"]["text"].endswith("could someone check the deploy?")
    assert state["event_kind"] == "message"
    assert decisions.log[0]["path"] == "/api/alpha/decisions" and decisions.log[0]["authorized"]
    assert set(decisions.log[0]["body"]["questions"]) == set(QUESTIONS)
    # The turn ran as it would have, and was told nothing of the answers.
    assert world.texts() == ["could someone check the deploy?"]
    # The record holds the answers and no word of the message.
    assert record["shadow"] is True and set(record["answers"]) == set(QUESTIONS)
    assert "deploy" not in caplog.text


async def test_a_message_that_is_refused_or_repeated_is_not_put_to_the_decision_model(build_world, decisions):
    world = await build_world("platforms: {loopback: {allow_from: [U1]}}\n" + decisions.config() + AMBIENT)
    said = world.message("hello")

    await world.send(world.message("a stranger writes", sender="U9"))
    await world.send(said)
    await world.send(said)
    await world.send(world.message("just talking", conversation="C9", sender="U1", sender_is_bot=True))

    assert len(await asked(decisions, 1)) == 1
    await asyncio.sleep(0.2)
    assert len(decisions.log) == 1
    # With no forwarding setting, the admitted message gets advice.
    assert advised(world) == [True]


async def test_a_forward_setting_that_is_not_boolean_uses_default(build_world, decisions, caplog):
    with caplog.at_level(logging.WARNING, logger=host_decisions.__name__):
        world = await build_world(decisions.config().rstrip() + '\nplatforms: {loopback: {decision_forward: "yes"}}\n' + AMBIENT)

    await world.send(world.message("hello"))

    assert any("must be a boolean" in record.getMessage() for record in caplog.records)
    assert advised(world) == [True]


# ── forwarded ────────────────────────────────────────────────────────────


async def test_a_forwarded_decision_is_told_to_the_turn_of_its_message_as_advice(build_world, decisions):
    world = await build_world(decisions.config() + AMBIENT)
    decisions.values[AUDIENCE] = AUDIENCE_PERSON

    await world.send(world.message("Dana, can you check the deploy?"))

    text = world.requests[0].text
    said, advice = text.split(ADVISORY_PREAMBLE)
    # After the message and what the platform adds to it: the preamble, each question with its answer.
    assert said.startswith("Dana, can you check the deploy?")
    told = json.loads(advice)
    assert set(told) == set(QUESTIONS)
    assert told[AUDIENCE]["answer"] == {"type": "choice", "value": AUDIENCE_PERSON}
    assert told[AUDIENCE]["instructions"] == QUESTIONS[AUDIENCE].instructions
    # How a fractional score reads is said once, in the preamble the turn was given.
    assert "A fractional value can summarize probabilities across levels" in text
    # The reply rule is untouched: the turn still speaks only through the posting tool.
    assert world.requests[0].scope.withhold_final


@pytest.mark.parametrize("gate", ["with the gate", "without it"])
async def test_no_answer_keeps_a_message_from_its_turn(build_world, decisions, gate):
    """The measurement: with the gate and without it, every message of a room that reads all starts a turn."""
    asks = gate == "with the gate"
    world = await build_world((decisions.config(forward=True) if asks else "") + AMBIENT)
    # Everything the disposition rule reads says each message is for somebody else and already answered.
    decisions.values.update({AUDIENCE: AUDIENCE_PERSON, "already_answered": 1.0, "response_requested": 0.0})
    chatter = ["morning", "Dana, lunch?", "sure", "the build is green", "thanks"]

    for line in chatter:
        await world.send(world.message(line))

    assert len(world.requests) == len(chatter)
    assert len(decisions.log) == (len(chatter) if asks else 0)
    assert world.outcomes() == [host.OUTCOME_WITHHELD] * len(chatter)


async def test_a_message_that_waits_is_logged_on_arrival_and_its_turn_gets_no_advice(build_world, decisions):
    world = await build_world(decisions.config(forward=True) + AMBIENT)
    world.kit.play(ModelTurn(final="one", hold=True))

    await world.platform.inject(world.message("first", thread="root"))
    await world.kit.holding()
    queued = await world.platform.inject(world.message("second", thread="root"))
    # It was put to the model when it arrived, while the first turn still ran.
    states = await asked(decisions, 2)
    world.kit.release()
    await world.host.idle()

    assert queued.startswith("queued:")
    assert [state["last_message"]["text"].split("\n")[0] for state in states] == ["first", "second"]
    assert advised(world) == [True, False]
    assert len(decisions.log) == 2


async def test_a_turn_an_event_starts_is_not_put_to_the_decision_model(build_world, decisions):
    world = await build_world(
        decisions.config(forward=True)
        + "scopes:\n  - match: {channel: loopback}\n    delivery: {mode: [all], reply: optional, events: {member: turn}}\n"
    )

    await world.send(
        InboundEvent(platform=world.platform_kit.name, workspace="W1", conversation="C1", kind="member", actor="U7", event_id="ev-join")
    )

    assert len(world.requests) == 1 and advised(world) == [False]
    assert decisions.log == []


def test_what_a_turn_is_told_of_reading_an_answer_is_written_once():
    package = Path(message_decisions.__file__).parent
    holders = [path.name for path in package.rglob("*.py") if "summarize probabilities across levels" in path.read_text(encoding="utf-8")]

    assert holders == ["message_decisions.py"]


# ── slow, failing, wrong ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    ["refuses", "answers nonsense", "answers some questions", "answers the wrong type", "is slow"],
)
async def test_a_decision_that_cannot_be_had_leaves_the_turn_as_it_was(build_world, decisions, failure, caplog):
    world = await build_world(decisions.config(forward=True) + AMBIENT)
    client = world.host._on().decisions._client
    timeout = client.timeout
    if failure == "refuses":
        decisions.status = 500
    elif failure == "answers nonsense":
        decisions.raw = ["not", "a", "record"]
    elif failure == "answers some questions":
        decisions.raw = {"answers": {AUDIENCE: {"type": "choice", "choice": AUDIENCE_PERSON}}}
    elif failure == "answers the wrong type":
        decisions.raw = {"answers": {name: {"type": "noul", "noul": 0.5} for name in QUESTIONS}}
    else:
        decisions.delay = 1.0
        # The client gives up after four seconds, and the gate after eight. A check need not wait that long.
        client.timeout = 0.2

    with caplog.at_level(logging.INFO, logger="belle_agent.message_decisions"):
        await world.send(world.message("is anybody there?"))

    assert decisions.log, "the decision model was not asked, so this check proved nothing"
    assert world.texts() == ["is anybody there?"] and advised(world) == [False]
    assert world.outcomes() == [host.OUTCOME_WITHHELD]
    # The failure is recorded, so a count of decisions keeps its denominator.
    (record,) = await logged(caplog)
    assert record["outcome"] == "declined" and "error" in record

    # Control: the next message, with the endpoint well again, is advised.
    # The control gets the client's own timeout, so that a pause of the test process cannot fail it.
    decisions.status, decisions.raw, decisions.delay, client.timeout = 200, None, 0.0, timeout
    await world.send(world.message("and now?"))
    assert advised(world) == [False, True]


async def test_a_message_the_platform_cannot_describe_to_the_gate_still_gets_its_turn(build_world, decisions, monkeypatch):
    world = await build_world(decisions.config(forward=True) + AMBIENT)

    def broken(inbound):
        raise KeyError("nothing is known of this message")

    monkeypatch.setattr(world.host._on().decisions, "_facts", broken)
    await world.send(world.message("hello"))

    assert world.texts() == ["hello"] and decisions.log == []
    assert world.outcomes() == [host.OUTCOME_WITHHELD]


# ── the configuration ────────────────────────────────────────────────────


@pytest.mark.parametrize(("api", "name", "path"), [("", "openrouter", "/api/alpha/decisions"), ("openai", "openai", "/v1/decisions")])
async def test_the_decision_block_selects_the_api_the_gate_speaks(build_world, decisions, api, name, path):
    world = await build_world(decisions.config(forward=True, api=api) + AMBIENT)

    await world.send(world.message("hello"))

    assert world.host._on().decisions._client.name == name
    assert decisions.log[0]["path"] == path
    # The stand-in answers as OpenRouter does. What the other API would not have said is no advice.
    assert advised(world) == [not api]


async def test_a_decision_api_nobody_knows_stops_the_load(build_world, decisions):
    with pytest.raises(BelleConfigError, match=r"models.decision.entries\[standin\].api must be one of"):
        await build_world(decisions.config(api="another") + AMBIENT)


async def test_a_decision_key_written_in_the_file_is_refused_and_not_shown(build_world, decisions):
    written = decisions.config().replace(f"${{{KEY_ENV}}}", "sk-written-in-the-file")

    with pytest.raises(BelleConfigError, match=r"models.decision.entries\[0\].api_key must be an environment reference") as raised:
        await build_world(written + AMBIENT)

    assert "sk-written-in-the-file" not in str(raised.value) and KEY_ENV in str(raised.value)


async def test_a_question_file_that_cannot_be_read_turns_the_gate_off_and_leaves_chat_on(build_world, decisions, tmp_path, caplog):
    broken = tmp_path / "questions.json"
    broken.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger=host_decisions.__name__):
        world = await build_world(decisions.config(forward=True, extra=f", decision_questions_file: {broken}") + AMBIENT)
    await world.send(world.message("hello"))

    assert any("decision disabled" in record.getMessage() for record in caplog.records)
    assert world.host._on().decisions is None and decisions.log == []
    assert world.texts() == ["hello"]


async def test_the_question_file_changes_what_is_asked(build_world, decisions, tmp_path):
    chosen = tmp_path / "questions.json"
    chosen.write_text(
        json.dumps({"claim": None, "needs_review": {"type": "noul", "instructions": "The last message asks for a review."}}),
        encoding="utf-8",
    )
    world = await build_world(decisions.config(forward=True, extra=f", decision_questions_file: {chosen}") + AMBIENT)

    await world.send(world.message("please review the patch"))

    asked_for = set(decisions.log[0]["body"]["questions"])
    assert asked_for == set(QUESTIONS) - {"claim"} | {"needs_review"}
    assert '"needs_review"' in world.requests[0].text
