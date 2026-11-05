"""Decision questions about one message, advisory answers, and shadow records.

A gate asks a typed-decision model a fixed set of questions about a message
that arrived, with the messages before it. The answers are logged, or handed
to the turn the message starts as advice. They decide nothing: whether a
message gets a turn, and whether the turn speaks, is settled elsewhere.
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from typing import Any, NoReturn

from belle_agent.typed_decision import (
    CHOICE,
    Answer,
    Question,
    TypedDecisionClient,
    TypedDecisionError,
)

logger = logging.getLogger(__name__)

#: The kinds of conversation the question state names. A platform maps its own words to these.
KIND_DIRECT = "one_to_one_dm"
KIND_GROUP = "group_dm"
KIND_PUBLIC = "public_channel"
KIND_PRIVATE = "private_channel"


def bot_identity(bot_name: str, mention: str) -> str:
    """Name the bot as the question state does: its name, and how a message mentions it, where that is known."""
    name = str(bot_name or "").strip() or "the assistant"
    return f"{name} (mentioned as {mention})" if mention else name


#: The question ids. Each one's wording is in :data:`QUESTIONS` below.
AUDIENCE = "audience"
ALREADY_ANSWERED = "already_answered"
RESPONSE_REQUESTED = "response_requested"
CLAIM = "claim"
#: Read with ``audience`` to tell a silent disposition from a later one.
NEEDS_WORK = "needs_work"
WORTH_REMEMBERING = "worth_remembering"
INDEPENDENT_WORK = "independent_work"
CONTINUATION_CONTEXT = "continuation_context"
DESERVES_EMOJI = "deserves_emoji"

AUDIENCE_NOBODY = "nobody"
AUDIENCE_ROOM = "the room"
AUDIENCE_PERSON = "a person"
AUDIENCE_BOT = "you"

QUESTIONS: Mapping[str, Question] = {
    AUDIENCE: Question(
        type="choice",
        instructions=(
            "Who is the request in `last_message` directed at? Judge"
            " `last_message` on its own. An earlier message that was directed"
            " at someone is not a reason to answer for this one."
        ),
        criteria={
            AUDIENCE_NOBODY: (
                "It asks nothing of anybody. A remark, a joke, an observation,"
                " or a statement of what the sender has done or intends to do."
            ),
            AUDIENCE_ROOM: (
                "It puts something to everyone present, and any of them could"
                " take it up. Nobody in particular is named."
            ),
            AUDIENCE_PERSON: (
                "It is aimed at one named participant other than the one named"
                " in `bot_name`, and nobody else is expected to answer."
            ),
            AUDIENCE_BOT: (
                "It is aimed at the participant named in `bot_name`. Mentioning"
                " that name in passing is not this: the request itself has to"
                " be for that participant."
            ),
        },
    ),
    ALREADY_ANSWERED: Question(
        type="noul",
        instructions=(
            "Someone in `recent_messages` already gave what `last_message`"
            " specifically asks for."
        ),
    ),
    RESPONSE_REQUESTED: Question(
        type="noul",
        instructions=(
            "`last_message` requests an answer or acknowledgement for this"
            " occurrence. Merely referring to an earlier request or answer"
            " does not count."
        ),
    ),
    CLAIM: Question(
        type="noul",
        instructions="`last_message` asserts something as fact.",
    ),
    NEEDS_WORK: Question(
        type="noul",
        instructions=(
            "`last_message` asks for something to be done or changed, not only"
            " for something to be said."
        ),
    ),
    WORTH_REMEMBERING: Question(
        type="noul",
        instructions=(
            "`last_message` holds something that is still needed later in this"
            " conversation."
        ),
    ),
    INDEPENDENT_WORK: Question(
        type="noul",
        instructions=(
            "The work requested in `last_message` can proceed to a useful"
            " result without more input from the sender after acknowledgement."
        ),
    ),
    CONTINUATION_CONTEXT: Question(
        type="noul",
        instructions=(
            "The request in `last_message` continues or changes work described"
            " in `recent_messages`, and details from that work are needed for a"
            " useful result."
        ),
    ),
    DESERVES_EMOJI: Question(
        type="noul",
        instructions=(
            "A member of this conversation would add an emoji reaction to"
            " `last_message`."
        ),
    ),
}

class QuestionConfigurationError(ValueError):
    """The operator question file is invalid or unreadable."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QuestionConfigurationError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise QuestionConfigurationError("non-finite JSON constant")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise QuestionConfigurationError("non-finite JSON number")
    return number


def load_questions(path: str | Path | None = None) -> dict[str, Question]:
    """Merge an optional JSON object into the defaults. Null disables a question."""
    questions = deepcopy(dict(QUESTIONS))
    if path is None or path == "":
        return questions
    if not isinstance(path, (str, Path)):
        raise QuestionConfigurationError("question file path must be a string")
    try:
        with Path(path).expanduser().open(encoding="utf-8") as stream:
            overrides = json.load(
                stream,
                object_pairs_hook=_json_object,
                parse_constant=_reject_constant,
                parse_float=_json_float,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QuestionConfigurationError("cannot read question file as JSON") from exc
    if not isinstance(overrides, dict):
        raise QuestionConfigurationError("question file must contain an object")
    for question_id, definition in overrides.items():
        if not question_id.strip():
            raise QuestionConfigurationError("question ID must not be empty")
        if definition is None:
            questions.pop(question_id, None)
            continue
        if not isinstance(definition, dict):
            raise QuestionConfigurationError("question definition must be an object or null")
        if set(definition) - {field.name for field in fields(Question)}:
            raise QuestionConfigurationError("unknown question field")
        if not isinstance(definition.get("type"), str):
            raise QuestionConfigurationError("question type must be a string")
        if not isinstance(definition.get("instructions"), str):
            raise QuestionConfigurationError("question instructions must be a string")
        criteria = definition.get("criteria")
        if (
            definition["type"] == CHOICE
            and isinstance(criteria, dict)
            and any(
                not key.strip()
                or not isinstance(value, (str, dict, list))
                or (isinstance(value, str) and not value.strip())
                for key, value in criteria.items()
            )
        ):
            raise QuestionConfigurationError(
                "choice options need names and string, object, or array descriptions"
            )
        try:
            questions[question_id] = Question(**definition)
        except (TypedDecisionError, TypeError) as exc:
            raise QuestionConfigurationError(
                "invalid question type, instructions, or criteria"
            ) from exc
    return questions


def question_definitions(questions: Mapping[str, Question]) -> dict[str, Any]:
    """Return the effective definitions for observation records."""
    return {key: question.as_request_value() for key, question in questions.items()}


def question_set_hash(questions: Mapping[str, Question]) -> str:
    payload = format_decision(question_definitions(questions))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def question_changes(questions: Mapping[str, Question]) -> Iterable[tuple[str, str]]:
    """Describe effective differences from the built-in questions."""
    for question_id in sorted(set(QUESTIONS) | set(questions)):
        if question_id not in questions:
            yield "disabled", question_id
        elif question_id not in QUESTIONS:
            yield "added", question_id
        elif (
            questions[question_id].as_request_value()
            != QUESTIONS[question_id].as_request_value()
        ):
            yield "replaced", question_id


def disposition_is_compatible(questions: Mapping[str, Question]) -> bool:
    """Check every definition that the disposition rule reads."""
    return all(
        key in questions
        and questions[key].as_request_value() == QUESTIONS[key].as_request_value()
        for key in (AUDIENCE, ALREADY_ANSWERED, NEEDS_WORK, CLAIM)
    )


#: A message can lack a request even when the agent knows something useful.
DISPOSITION_CONTEXT = "context"
DISPOSITION_SILENT = "silent"
DISPOSITION_HERE = "here"
DISPOSITION_LATER = "later"

DISPOSITIONS = (
    DISPOSITION_CONTEXT,
    DISPOSITION_SILENT,
    DISPOSITION_HERE,
    DISPOSITION_LATER,
)

#: Observational midpoint, not a fitted threshold.
HIGH = 0.5


def _noul(answers: Mapping[str, Answer], question_id: str) -> float:
    answer = answers.get(question_id)
    if answer is None or answer.number is None:
        raise ValueError(f"the rule needs a numeric {question_id!r} answer")
    return float(answer.number)


def disposition(answers: Mapping[str, Answer], *, high: float = HIGH) -> str:
    """Apply the first matching rule to the model's answers."""
    audience = answers.get(AUDIENCE)
    if audience is None or not isinstance(audience.value, str):
        raise ValueError("the rule needs an audience answer")
    if audience.value == AUDIENCE_PERSON:
        return DISPOSITION_CONTEXT
    if _noul(answers, ALREADY_ANSWERED) >= high:
        return DISPOSITION_CONTEXT
    if _noul(answers, NEEDS_WORK) >= high and audience.value in (
        AUDIENCE_BOT,
        AUDIENCE_ROOM,
    ):
        return DISPOSITION_SILENT
    if _noul(answers, CLAIM) >= high:
        return DISPOSITION_LATER
    if audience.value == AUDIENCE_BOT:
        return DISPOSITION_HERE
    return DISPOSITION_LATER


#: Prior messages supplied with ``last_message``.
RECENT_MESSAGE_DEPTH = 12

def message_entry(
    author: str,
    text: str,
    *,
    ts: str = "",
    thread_ts: str | None = None,
    file_count: int | None = None,
    attachment_count: int | None = None,
) -> dict[str, Any]:
    """Keep message facts that the model can use to interpret the text."""
    entry: dict[str, Any] = {
        "author": str(author or "someone"), "text": str(text or "")
    }
    if ts:
        entry["ts"] = str(ts)
    if thread_ts is not None:
        entry["thread_ts"] = str(thread_ts)
        if not thread_ts or thread_ts == ts:
            entry["thread_position"] = "root"
        else:
            entry["thread_position"] = "reply" if ts else "unknown"
    for key, count in (("file_count", file_count), ("attachment_count", attachment_count)):
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            entry[key] = count
    return entry


def _copy_message(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only supported message facts into the model input."""
    return message_entry(
        entry.get("author", ""),
        entry.get("text", ""),
        ts=entry.get("ts", ""),
        thread_ts=entry.get("thread_ts"),
        file_count=entry.get("file_count"),
        attachment_count=entry.get("attachment_count"),
    )


def build_state(
    *,
    channel: str,
    bot_name: str,
    recent_messages: Sequence[Mapping[str, Any]],
    last_message: Mapping[str, Any],
    members: Iterable[str] | None = None,
    conversation_kind: str = "unknown",
) -> dict[str, Any]:
    """Build question state from supported message facts, including bot turns."""
    window = [_copy_message(entry) for entry in recent_messages]
    if members is None:
        members = dict.fromkeys(
            author
            for entry in (*window, last_message)
            if (author := str(entry.get("author") or "").strip())
        )
    state = {
        "channel": str(channel or ""),
        "members": list(members),
        "recent_messages": window,
        "last_message": _copy_message(last_message),
        "bot_name": str(bot_name or ""),
        "conversation_kind": conversation_kind,
        "event_kind": "message",
        # Channel history can omit thread replies.
        "history_scope": "channel",
    }
    return state


#: What a one-line JSON shadow record follows, after the tag of whoever logs it.
SHADOW_LABEL = "decision shadow:"

#: Shadow record format version.
LOG_VERSION = 1

#: Shadow record schema versions. A reader matches on these rather than on the
#: question or state keys it happens to find.
QUESTION_SET_VERSION = 2
STATE_VERSION = 2

#: Reader instructions for the advisory payload. Held apart from the payload
#: builder because a surface outside this module needs the same words later.
ADVISORY_PREAMBLE = (
    "The JSON below holds estimates about this event. They are advisory.\n"
    "\n"
    "Each entry is one question and its answer. `instructions` is the question"
    " that was asked, and `criteria` defines its possible answers where it has"
    " them. `answer` holds the estimate.\n"
    "\n"
    "Read each answer by its `type`:\n"
    "\n"
    "- `noul`: `value` is the probability, from 0 to 1, that `instructions`"
    " holds. This type has no `criteria` and returns no confidence.\n"
    "- `choice`: `value` is one key of `criteria`, and `probabilities` gives a"
    " value for every key.\n"
    "- `score`: `value` is on the ordered entries of `criteria`, indexed from"
    " 0. A fractional value can summarize probabilities across levels. It"
    " need not describe a case between levels.\n"
    "\n"
    "`confidence`, where present, says how concentrated the estimate is. It"
    " does not say whether the answer is correct."
)


def answer_record(answer: Answer) -> dict[str, Any]:
    """Record the raw answer and any optional fields the adapter returned."""
    record: dict[str, Any] = {"type": answer.type, "value": answer.value}
    for name in ("confidence", "probabilities", "legend"):
        value = getattr(answer, name)
        if value is not None:
            record[name] = dict(value) if isinstance(value, Mapping) else value
    return record


def decision_record(
    *,
    identity: Mapping[str, Any],
    answers: Mapping[str, Answer] | None,
    outcome: str | None,
    adapter: str,
    latency_ms: int,
    window: int,
    error: str = "",
    question_hash: str = "",
    outcome_reason: str = "",
    advice_produced: bool = False,
    advisory_mode: bool = False,
) -> dict[str, Any]:
    """Record a decision without message text, including failed calls."""
    # Set identity first so record fields win on key collisions.
    record: dict[str, Any] = {str(key): value for key, value in identity.items()}
    record.update(
        {
            "v": LOG_VERSION,
            "question_set_version": QUESTION_SET_VERSION,
            "state_version": STATE_VERSION,
            # The mode is recorded separately from whether advice was produced.
            "shadow": not advisory_mode,
            "outcome": outcome,
            "adapter": adapter,
            "latency_ms": latency_ms,
            "window": window,
        }
    )
    if question_hash:
        record["question_set_hash"] = question_hash
    if advice_produced:
        record["advice_produced"] = True
    if outcome_reason:
        record["outcome_reason"] = outcome_reason
    if error:
        record["error"] = error
    if answers:
        record["answers"] = {
            question_id: answer_record(answer)
            for question_id, answer in sorted(answers.items())
        }
    return record


def format_decision(record: Mapping[str, Any]) -> str:
    """The payload as one line, ordered so a diff of two lines is readable."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def format_advisory(
    answers: Mapping[str, Answer], questions: Mapping[str, Question]
) -> str:
    """Render full question definitions before their answers, without outcomes."""
    items: dict[str, dict[str, Any]] = {}
    for question_id, answer in sorted(answers.items()):
        question = questions.get(question_id)
        if question is None:
            continue
        definition = question.as_request_value()
        item: dict[str, Any] = {"instructions": definition["instructions"]}
        if "criteria" in definition:
            item["criteria"] = definition["criteria"]
        item["answer"] = answer_record(answer)
        items[question_id] = item
    return ADVISORY_PREAMBLE + "\n\n" + json.dumps(
        items, ensure_ascii=False, indent=2, default=str
    )


#: Overall budget, including history retrieval and the client's own deadline.
SHADOW_BUDGET_SECONDS = 8.0

RecentMessages = Callable[[str, int, str], Awaitable[Sequence[Mapping[str, Any]]]]


class ShadowDecisionGate:
    """Log shadow decisions or return raw answers as advisory context."""

    #: Where the gate logs, and what its lines start with. Whoever uses the gate names its own.
    _log = logger
    _tag = "[decisions]"

    def __init__(
        self,
        client: TypedDecisionClient,
        *,
        recent_messages: RecentMessages | None = None,
        depth: int = RECENT_MESSAGE_DEPTH,
        budget: float = SHADOW_BUDGET_SECONDS,
        questions: Mapping[str, Question] = QUESTIONS,
    ) -> None:
        self._client = client
        self._recent_messages = recent_messages
        self._depth = depth
        self._budget = budget
        self._questions = deepcopy(dict(questions))
        self._question_set_hash = question_set_hash(self._questions)
        self._disposition_compatible = disposition_is_compatible(self._questions)
        self._log.info(
            "%s decision questions: %s",
            self._tag,
            format_decision({
                "question_set_hash": self._question_set_hash,
                "questions": question_definitions(self._questions),
            }),
        )
        # Keep pending tasks for shutdown cancellation.
        self._tasks: set[asyncio.Task[Mapping[str, Answer] | None]] = set()

    async def advise(self, **facts: Any) -> str:
        """Evaluate this message and return advisory text only on success. ``facts`` are those of ``_observe``."""
        answers = await self._observe(forward=True, **facts)
        return format_advisory(answers, self._questions) if answers else ""

    def observe(self, **facts: Any) -> None:
        """Start a detached shadow decision without exposing its result. ``facts`` are those of ``_observe``."""
        try:
            task = asyncio.get_running_loop().create_task(self._observe(**facts))
        except Exception:  # noqa: BLE001 - a shadow may not raise into a turn
            self._log.debug("%s shadow decision not started", self._tag, exc_info=True)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _observe(
        self,
        *,
        channel: str,
        bot_name: str,
        last_message: Mapping[str, Any],
        identity: Mapping[str, Any],
        conversation_kind: str = "unknown",
        forward: bool = False,
    ) -> Mapping[str, Answer] | None:
        """Evaluate and log one message; return answers only in forward mode."""
        started = time.monotonic()
        window: Sequence[Mapping[str, Any]] = ()
        answers: Mapping[str, Answer] | None = None
        outcome: str | None = "error"
        error = ""
        try:
            async with asyncio.timeout(self._budget):
                window = await self._window(channel, str(identity.get("ts") or ""))
                state = build_state(
                    channel=channel,
                    bot_name=bot_name,
                    recent_messages=window,
                    last_message=last_message,
                    conversation_kind=conversation_kind,
                )
                answers = await self._client.decide(state, self._questions)
                outcome = (
                    disposition(answers) if self._disposition_compatible else None
                )
        except asyncio.CancelledError:
            # Shutdown cancellation ends the observation.
            raise
        except Exception as exc:  # noqa: BLE001 - a shadow may not raise into a turn
            # Any observation failure must stay outside the turn.
            outcome = "declined"
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            self._log.info(
                "%s %s %s",
                self._tag,
                SHADOW_LABEL,
                format_decision(
                    decision_record(
                        identity=identity,
                        answers=answers,
                        outcome=outcome,
                        adapter=self._client.name,
                        latency_ms=latency_ms,
                        window=len(window),
                        error=error,
                        question_hash=self._question_set_hash,
                        outcome_reason=(
                            "question_definitions_changed"
                            if outcome is None else ""
                        ),
                        advice_produced=bool(forward and answers),
                        advisory_mode=forward,
                    )
                ),
            )
        except Exception:  # noqa: BLE001 - a shadow may not raise into a turn
            self._log.debug("%s shadow decision unlogged", self._tag, exc_info=True)
        return answers if forward and answers else None

    async def _window(self, channel: str, before_ts: str) -> Sequence[Mapping[str, Any]]:
        """Return prior messages, or an empty window after a read failure."""
        if self._recent_messages is None or self._depth <= 0:
            return ()
        try:
            return await self._recent_messages(channel, self._depth, before_ts)
        except Exception:  # noqa: BLE001 - a shadow may not raise into a turn
            self._log.debug(
                "%s shadow decision read no window: channel=%s",
                self._tag,
                channel,
                exc_info=True,
            )
            return ()

    async def close(self) -> None:
        """Cancel whatever is still in flight, and wait for it to stop."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
