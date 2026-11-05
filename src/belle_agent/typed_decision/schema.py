"""Typed-decision question and answer shapes."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

NOUL = "noul"
CHOICE = "choice"
SCORE = "score"
QUESTION_TYPES = (NOUL, CHOICE, SCORE)


class TypedDecisionError(RuntimeError):
    """A question, transport, or response prevented a typed decision."""


@dataclass(frozen=True)
class Question:
    """One independent question."""

    type: str
    instructions: str
    criteria: Mapping[str, Any] | Sequence[Any] | None = None

    def __post_init__(self) -> None:
        if self.type not in QUESTION_TYPES:
            raise TypedDecisionError(f"question type must be one of {QUESTION_TYPES}: {self.type!r}")
        if not str(self.instructions).strip():
            raise TypedDecisionError("question instructions must not be empty")
        if self.type == NOUL:
            if self.criteria is not None:
                raise TypedDecisionError("a noul question takes no criteria")
        elif self.type == CHOICE:
            if not isinstance(self.criteria, Mapping) or not self.criteria:
                raise TypedDecisionError("a choice question needs criteria as a non-empty {option: description} record")
        elif self.type == SCORE:
            if isinstance(self.criteria, (str, bytes, Mapping)) or not isinstance(self.criteria, Sequence):
                raise TypedDecisionError(
                    "a score question needs criteria as an ordered sequence of levels, lowest first"
                )
            if len(self.criteria) < 2:
                raise TypedDecisionError("a score rubric needs at least two levels")

    def as_request_value(self) -> dict[str, Any]:
        """The wire form of this question, without its id."""
        payload: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            payload["criteria"] = dict(self.criteria) if isinstance(self.criteria, Mapping) else list(self.criteria)
        return payload


@dataclass(frozen=True)
class Answer:
    """A typed value and any optional adapter metadata."""

    type: str
    value: float | str
    confidence: float | None = None
    probabilities: Mapping[str, float] | None = None
    legend: Mapping[str, Any] | None = None

    @property
    def number(self) -> float | None:
        """Return a numeric value, or ``None`` for a choice."""
        return self.value if isinstance(self.value, (int, float)) else None


def parse_answer(payload: Mapping[str, Any]) -> Answer:
    """Parse the type declared in one response answer."""
    answer_type = str(payload.get("type") or "").strip()
    if answer_type not in QUESTION_TYPES:
        raise TypedDecisionError(f"answer stated no known type: {payload!r}")
    if answer_type not in payload:
        raise TypedDecisionError(f"a {answer_type} answer had no {answer_type!r} field: {payload!r}")
    raw = payload[answer_type]
    value: float | str
    if answer_type == CHOICE:
        value = str(raw)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise TypedDecisionError(f"a {answer_type} answer was not numeric: {raw!r}") from exc
    probabilities = payload.get("probabilities")
    legend = payload.get("legend")
    confidence = payload.get("confidence")
    return Answer(
        type=answer_type,
        value=value,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        probabilities=(
            MappingProxyType({str(key): float(item) for key, item in probabilities.items()})
            if isinstance(probabilities, Mapping)
            else None
        ),
        legend=(MappingProxyType(dict(legend)) if isinstance(legend, Mapping) else None),
    )


def parse_answers(payload: Mapping[str, Any]) -> dict[str, Answer]:
    """Read every answer out of a decision response body."""
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise TypedDecisionError("decision response had no answers record")
    parsed: dict[str, Answer] = {}
    for question_id, entry in answers.items():
        if not isinstance(entry, Mapping):
            raise TypedDecisionError(f"answer for {question_id!r} was not a record: {entry!r}")
        parsed[str(question_id)] = parse_answer(entry)
    return parsed
