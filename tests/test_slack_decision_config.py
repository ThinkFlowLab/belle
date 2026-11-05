"""Test configurable Slack decision questions and advisory answers."""

import json
import logging
from unittest.mock import AsyncMock

import pytest

from belle_agent.platforms.slack import decision_questions as dq
from belle_agent.typed_decision import QUESTION_TYPES, Answer, Question


def load(tmp_path, value):
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return dq.load_questions(path)


def test_defaults_and_whole_definition_replacement(tmp_path):
    assert dq.load_questions() == dq.QUESTIONS
    questions = load(tmp_path, {
        "audience": {"type": "noul", "instructions": "A response fits."},
        "claim": None,
        "custom": {"type": "score", "instructions": "Rate usefulness.",
                   "criteria": ["low", "high"]},
    })
    assert questions["audience"].criteria is None
    assert "claim" not in questions
    assert questions["custom"].type == "score"
    assert questions["deserves_emoji"] == dq.QUESTIONS["deserves_emoji"]
    assert dq.QUESTIONS["audience"].type == "choice"


@pytest.mark.parametrize("value", [[], {"": None}, {"x": []},
    {"x": {"type": "noul", "instructions": 42}},
    {"x": {"type": "noul", "instructions": "ok", "extra": True}},
    {"x": {"type": "choice", "instructions": "ok", "criteria": {"a": 1}}},
    {"x": {"type": "score", "instructions": "ok", "criteria": ["one"]}},
    {"x": {"type": "noul", "instructions": "ok", "criteria": []}},
    {"x": {"type": "unknown", "instructions": "ok"}},
])
def test_invalid_definitions_are_rejected(tmp_path, value):
    with pytest.raises(dq.QuestionConfigurationError):
        load(tmp_path, value)


@pytest.mark.parametrize("text", ['{"x":null,"x":null}', '{"x":NaN}', '{broken', '\udcff'])
def test_invalid_json_is_rejected_without_echoing_content(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_bytes(text.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(dq.QuestionConfigurationError) as exc:
        dq.load_questions(path)
    assert text not in str(exc.value)


def test_required_definition_guard(tmp_path):
    assert dq.disposition_is_compatible(load(tmp_path, {
        "audience": dq.QUESTIONS["audience"].as_request_value(),
        "deserves_emoji": None,
        "extra": {"type": "noul", "instructions": "A response fits."},
    }))
    for key in (dq.AUDIENCE, dq.ALREADY_ANSWERED, dq.NEEDS_WORK, dq.CLAIM):
        assert not dq.disposition_is_compatible(load(tmp_path, {key: None}))
        definition = dq.QUESTIONS[key].as_request_value()
        definition["instructions"] += " Changed."
        assert not dq.disposition_is_compatible(load(tmp_path, {key: definition}))
    definition = dq.QUESTIONS[dq.AUDIENCE].as_request_value()
    definition["criteria"]["you"] = "Changed option."
    assert not dq.disposition_is_compatible(load(tmp_path, {dq.AUDIENCE: definition}))


def test_hash_ignores_object_order_but_preserves_score_order(tmp_path):
    questions = dq.load_questions()
    assert dq.question_set_hash(questions) == dq.question_set_hash(dict(reversed(list(questions.items()))))
    first = load(tmp_path, {"x": {"type": "score", "instructions": "Rate it.", "criteria": ["low", "high"]}})
    second = load(tmp_path, {"x": {"type": "score", "instructions": "Rate it.", "criteria": ["high", "low"]}})
    assert dq.question_set_hash(first) != dq.question_set_hash(second)


def test_question_count_has_no_application_cap(tmp_path):
    questions = load(tmp_path, {f"q{i}": {"type": "noul", "instructions": "A response fits."} for i in range(1000)})
    assert len(questions) == 1000 + len(dq.QUESTIONS)


async def test_changed_required_question_retains_answers_without_outcome(tmp_path, caplog):
    questions = load(tmp_path, {"audience": None})
    client = AsyncMock()
    client.name = "test"
    client.decide.return_value = {"claim": Answer(type="noul", value=.7)}
    with caplog.at_level(logging.INFO, logger=dq.__name__):
        gate = dq.ShadowDecisionGate(client, questions=questions)
        await gate._observe(channel="C1", bot_name="bot", last_message={"text": "PRIVATE"}, identity={})
    records = [json.loads(record.message.split(dq.LOG_PREFIX, 1)[1]) for record in caplog.records if dq.LOG_PREFIX in record.message]
    assert records[0]["outcome"] is None
    assert records[0]["outcome_reason"] == "question_definitions_changed"
    assert records[0]["answers"]["claim"]["value"] == .7
    assert records[0]["question_set_hash"] == dq.question_set_hash(questions)
    assert "PRIVATE" not in caplog.text
    assert '"questions"' in caplog.text


def test_gate_owns_definitions_snapshot():
    questions = dq.load_questions()
    gate = dq.ShadowDecisionGate(AsyncMock(), questions=questions)
    digest = gate._question_set_hash
    questions["audience"].criteria["you"] = "Changed."
    assert dq.question_set_hash(gate._questions) == digest
    assert gate._questions["audience"] == dq.QUESTIONS["audience"]


def advisory_body(advisory):
    """Return the JSON payload, and check the reader instructions precede it."""
    assert advisory.startswith(dq.ADVISORY_PREAMBLE)
    return advisory[len(dq.ADVISORY_PREAMBLE):]


def test_advisory_preamble_covers_every_question_type():
    """A type the reader cannot look up leaves a value on an invisible scale."""
    for question_type in QUESTION_TYPES:
        assert f"`{question_type}`" in dq.ADVISORY_PREAMBLE


def test_advisory_forwards_each_question_verbatim():
    """The whole rubric reaches the reader, not the one option that won."""
    questions = {
        "temperature": Question(
            type="score", instructions="How warm is the message?",
            criteria=["cold, and distant", "warm, and friendly", "hot, and urgent"],
        ),
        "reaction": Question(
            type="choice", instructions="Choose a reaction.",
            criteria={
                "other": {"meaning": "A different reaction"},
                "none": "No reaction fits.",
            },
        ),
        "claim": Question(type="noul", instructions="It asserts a fact."),
    }
    answers = {
        "temperature": Answer(type="score", value=1.88),
        "reaction": Answer(
            type="choice", value="other", confidence=0.8,
            probabilities={"other": 0.9, "none": 0.1},
        ),
        "claim": Answer(type="noul", value=0.07),
    }
    advisory = dq.format_advisory(answers, questions)
    body = advisory_body(advisory)
    rendered = json.loads(body)
    assert set(rendered) == set(questions)
    assert rendered["temperature"]["instructions"] == "How warm is the message?"
    # Every level definition, not the bare keys.
    assert rendered["temperature"]["criteria"] == [
        "cold, and distant", "warm, and friendly", "hot, and urgent",
    ]
    assert rendered["temperature"]["answer"] == {"type": "score", "value": 1.88}
    # Every option, not only the one the answer named.
    assert rendered["reaction"]["criteria"] == {
        "other": {"meaning": "A different reaction"},
        "none": "No reaction fits.",
    }
    assert rendered["reaction"]["answer"]["confidence"] == 0.8
    assert rendered["reaction"]["answer"]["probabilities"] == {
        "other": 0.9, "none": 0.1,
    }
    # A noul question has no criteria, so its entry states none.
    assert "criteria" not in rendered["claim"]
    assert rendered["claim"]["answer"] == {"type": "noul", "value": 0.07}
    assert "selected_choice_meaning" not in body
    assert "score_levels" not in body
    assert "outcome" not in advisory
    assert "channel" not in rendered


def test_advisory_sorts_question_ids_and_keeps_entries_in_reading_order():
    """Sorted ids keep two payloads diffable. The answer follows its question."""
    questions = {
        "zulu": Question(type="noul", instructions="Last by id."),
        "alpha": Question(
            type="choice", instructions="First by id.",
            criteria={"yes": "It holds.", "no": "It does not."},
        ),
    }
    answers = {
        "zulu": Answer(type="noul", value=0.5),
        "alpha": Answer(type="choice", value="yes"),
    }
    body = advisory_body(dq.format_advisory(answers, questions))
    pairs = json.loads(body, object_pairs_hook=list)
    assert [key for key, _ in pairs] == ["alpha", "zulu"]
    entries = dict(pairs)
    assert [key for key, _ in entries["alpha"]] == [
        "instructions", "criteria", "answer",
    ]
    assert [key for key, _ in entries["zulu"]] == ["instructions", "answer"]


def test_advisory_keeps_configured_levels_beside_a_provider_legend():
    questions = {
        "value": Question(
            type="score", instructions="How useful?",
            criteria=["none", "some", "substantial"],
        ),
    }
    answers = {
        "value": Answer(
            type="score", value=1.88,
            legend={"0": "low", "1": "medium", "2": "high"},
        ),
    }
    rendered = json.loads(advisory_body(dq.format_advisory(answers, questions)))
    assert rendered["value"]["criteria"] == ["none", "some", "substantial"]
    assert rendered["value"]["answer"]["legend"] == {
        "0": "low", "1": "medium", "2": "high",
    }


def test_advisory_explains_an_operator_question_this_module_never_saw(tmp_path):
    """The words come from the operator file, so a key alone can mean nothing."""
    questions = load(tmp_path, {"blorf": {
        "type": "choice", "instructions": "Which blorf is this?",
        "criteria": {
            "gronk": "It repeats an earlier blorf.",
            "skree": "It opens a new blorf.",
        },
    }})
    answers = {"blorf": Answer(
        type="choice", value="skree", confidence=0.61,
        probabilities={"skree": 0.61, "gronk": 0.39},
    )}
    rendered = json.loads(advisory_body(dq.format_advisory(answers, questions)))
    assert rendered == {"blorf": {
        "instructions": "Which blorf is this?",
        "criteria": {
            "gronk": "It repeats an earlier blorf.",
            "skree": "It opens a new blorf.",
        },
        "answer": {
            "type": "choice", "value": "skree", "confidence": 0.61,
            "probabilities": {"skree": 0.61, "gronk": 0.39},
        },
    }}


def test_choice_descriptions_accept_structured_json(tmp_path):
    questions = load(tmp_path, {"custom": {
        "type": "choice", "instructions": "Choose a reaction.",
        "criteria": {"a": {"meaning": "acknowledge"}, "b": ["joy", "surprise"]},
    }})
    assert questions["custom"].criteria["a"] == {"meaning": "acknowledge"}
