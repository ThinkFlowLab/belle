"""Present answerable questions as Slack Block Kit messages."""

import json
import logging
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent import click_notices
from belle_agent.platforms.slack import inputs
from belle_agent.platforms.slack.text import clamp, normalize_slack_mrkdwn

MAX_BUTTON_VALUE_LENGTH = 2000
MAX_BUTTON_TEXT_LENGTH = 75
MAX_SECTION_TEXT_LENGTH = 3000
MAX_ACTIONS_ELEMENTS = 25
MAX_OPTION_DESCRIPTION_LENGTH = 200
MAX_QUESTION_BLOCKS = 50
_INTERACTIVE_BLOCK_TYPES = frozenset({"actions", "input"})

# Button action IDs share a prefix so Bolt can route them to one listener.
QUESTION_ACTION_ID_PREFIX = "jiuwenswarm_answer:"
# Only submit answers a question. Input events update form state.
QUESTION_SUBMIT_ACTION_ID = f"{QUESTION_ACTION_ID_PREFIX}submit"
QUESTION_INPUT_ACTION_ID_PREFIX = f"{QUESTION_ACTION_ID_PREFIX}input:"
QUESTION_ACTION_ID_RE = re.compile(
    rf"^{re.escape(QUESTION_ACTION_ID_PREFIX)}"
    rf"(?:\d+|submit|input:\d+(?:\.[a-z_]+)?)$"
)

# Shown, to the person who pressed submit and to nobody else, when what they
# submitted cannot be answered with. The question stays posted and its elements
# stay on screen, so the fix is to pick and press again.
INCOMPLETE_SUBMIT_NOTICE = "This cannot be submitted yet —"



def extract_request(payload: Any) -> tuple[str, str, dict[str, Any], list[str]] | None:
    """Read the first question and retain the prompts omitted from its message."""
    if not isinstance(payload, dict):
        return None
    request_id = str(payload.get("request_id") or "").strip()
    questions = payload.get("questions")
    if not request_id or not isinstance(questions, list) or not questions:
        return None
    question = questions[0]
    if not isinstance(question, Mapping):
        return None
    dropped = [
        QuestionPresenter.prompt_text(entry) if isinstance(entry, Mapping) else ""
        for entry in questions[1:]
    ]
    return request_id, str(payload.get("source") or "").strip(), dict(question), dropped


def decode_button_value(action: Mapping[str, Any], logger: logging.Logger) -> dict[str, Any] | None:
    """Accept only button values with a decodable request identity."""
    raw = action.get("value")
    if not isinstance(raw, str) or not raw:
        logger.warning("Slack answer button carried no value")
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Slack answer button value was not valid JSON")
        return None
    if not isinstance(decoded, dict) or not str(decoded.get("request_id") or ""):
        logger.warning("Slack answer button value identified no question")
        return None
    return decoded


def resolve_answer(
    labels: list[str], values: list[str], decoded: Mapping[str, Any], action: Mapping[str, Any]
) -> tuple[str, str]:
    """Prefer the full stored option over Slack's shortened button value."""
    index = decoded.get("index")
    if isinstance(index, int) and 0 <= index < len(values):
        return labels[index], values[index]
    text = action.get("text")
    label = str((text or {}).get("text") or "").strip() if isinstance(text, Mapping) else ""
    value = str(decoded.get("value") or "").strip() or label
    return label or value, value


def answered_note(label: str, user_id: str) -> str:
    return clamp(click_notices.answered_note(label, f"<@{user_id}>" if user_id else ""), MAX_BUTTON_TEXT_LENGTH)


def closed_blocks(raw_blocks: Any, note: str) -> list[dict[str, Any]]:
    """Keep the question content and replace controls with a completion note."""
    blocks = [
        block
        for block in (raw_blocks if isinstance(raw_blocks, list) else [])
        if isinstance(block, dict) and block.get("type") not in _INTERACTIVE_BLOCK_TYPES
    ]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return blocks


def read_submitted_inputs(
    declared: list[inputs.QuestionInput],
    body: Mapping[str, Any],
    *,
    request_id: str,
    action_id_prefix: str,
    logger: logging.Logger,
) -> tuple[list[str], str, str]:
    """Read one submit snapshot as answer values, label, or user-facing problem."""
    state = body.get("state")
    state_values = state.get("values") if isinstance(state, Mapping) else None
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Slack submit state: request_id=%s %s",
            request_id,
            "; ".join(
                "{}={{{}}}".format(
                    action_id,
                    ",".join(f"{key}:{type(value).__name__}" for key, value in sorted(entry.items())),
                )
                for action_id, entry in sorted(inputs.flatten_state(state_values).items())
            )
            or "(empty)",
        )
    results = inputs.read_state(declared, state_values, action_id_prefix=action_id_prefix)
    problem = inputs.submit_problem(declared, results)
    if problem:
        return [], "", problem
    if len(declared) <= 1:
        filled = next((result.values for result in results if result.values), [])
        values = list(filled)
    else:
        payload = {
            result.name: result.values if entry.spec.multi else result.values[0]
            for entry, result in zip(declared, results)
            if result.values
        }
        values = [json.dumps(payload, separators=(",", ":"), ensure_ascii=False)]
    if not values:
        return [], "", "nothing was selected"
    return values, ", ".join(values), ""


def text_route(source: str, interrupt_sources: Collection[str]) -> str:
    """Name a text reply path only when the question resumes a paused turn."""
    if source in interrupt_sources:
        return "Reply in this thread and the waiting task will read your answer."
    return ""


@dataclass(frozen=True)
class QuestionOption:
    """One answer choice and its Slack presentation."""

    label: str
    value: str
    description: str = ""
    style: str = ""


@dataclass(frozen=True)
class QuestionPresenter:
    """Build question blocks using the connector's action IDs and option policy."""

    action_id_prefix: str
    option_style: Callable[[str, str], str]
    logger: logging.Logger

    @staticmethod
    def prompt_text(question: Mapping[str, Any]) -> str:
        for candidate in (question.get("question"), question.get("header")):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @staticmethod
    def fallback_text(question: Mapping[str, Any]) -> str:
        header = str(question.get("header") or "").strip()
        body = str(question.get("question") or "").strip()
        if body == header:
            body = ""
        joined = f"{header}: {body}" if header and body else (body or header)
        return clamp(joined or "A question is waiting for an answer", 500)

    def options(self, question: Mapping[str, Any]) -> list[QuestionOption]:
        """Read valued options, omitting the free-form choice with no button answer."""
        raw = question.get("options")
        if not isinstance(raw, list):
            return []
        options: list[QuestionOption] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            value = str(entry.get("value") or "").strip()
            label = str(entry.get("label") or "").strip() or value
            if not label:
                continue
            if not value:
                if label == "Other":
                    continue
                value = label
            description = str(entry.get("description") or "").strip()
            intent = str(entry.get("intent") or "").strip()
            options.append(
                QuestionOption(
                    label=label,
                    value=value,
                    description=description,
                    style=self.option_style(intent, value),
                )
            )
        return options

    def answer_choices(
        self, question: Mapping[str, Any], *, allow_text_input: bool, request_id: str
    ) -> tuple[list[inputs.QuestionInput], list[QuestionOption]]:
        """Choose declared inputs or valued buttons for one answerable question."""
        declared = inputs.parse_inputs(question)
        if allow_text_input and not declared and question.get("options", []) == [] and question.get("inputs") in (None, []):
            declared = inputs.parse_inputs({"inputs": [{"type": "text", "label": "Answer", "multiline": True}]})
        options = [] if declared else self.options(question)
        if declared and isinstance(question.get("options"), list) and question["options"]:
            self.logger.warning(
                "Slack question declares both inputs and options; the options are not rendered: request_id=%s",
                request_id,
            )
        return declared, options

    def answer_blocks(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        declared: list[inputs.QuestionInput],
        options: list[QuestionOption],
        dropped: list[str],
    ) -> list[dict[str, Any]] | None:
        """Render answer controls and name omitted prompts when space allows."""
        if declared:
            blocks = self.input_blocks(
                request_id=request_id, source=source, session_id=session_id,
                question=question, inputs_for_question=declared,
            )
        else:
            blocks = self.option_blocks(
                request_id=request_id, source=source, session_id=session_id,
                question=question, options=options,
            )
        if blocks is None:
            return None
        if dropped:
            if len(blocks) < MAX_QUESTION_BLOCKS:
                blocks.append(self.dropped_questions_block(dropped))
            else:
                self.logger.warning(
                    "Slack has no room to name the dropped questions on the message, which already holds %d blocks: request_id=%s",
                    len(blocks), request_id,
                )
        return blocks

    @staticmethod
    def encode_button_value(*, request_id: str, index: int, value: str, source: str, session_id: str) -> str:
        """Keep request identity even when optional click fields exceed Slack's limit."""
        fields: dict[str, Any] = {
            "request_id": request_id,
            "index": index,
            "value": value,
            "source": source,
            "session_id": session_id,
        }
        for shed in ((), ("value",), ("value", "session_id"), ("value", "session_id", "source")):
            candidate = {key: item for key, item in fields.items() if key not in shed}
            encoded = json.dumps(candidate, separators=(",", ":"), ensure_ascii=False)
            if len(encoded) <= MAX_BUTTON_VALUE_LENGTH:
                return encoded
        return ""

    def option_blocks(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        options: list[QuestionOption],
    ) -> list[dict[str, Any]] | None:
        """Return None if a button cannot carry its request identity."""
        blocks = self._prompt_blocks(question)
        legend = self._options_legend(options)
        if legend:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": legend}})

        elements: list[dict[str, Any]] = []
        for index, option in enumerate(options):
            encoded = self.encode_button_value(
                request_id=request_id,
                index=index,
                value=option.value,
                source=source,
                session_id=session_id,
            )
            if not encoded:
                self.logger.warning(
                    "Slack question option %d has no value that fits in %d characters: request_id=%s",
                    index,
                    MAX_BUTTON_VALUE_LENGTH,
                    request_id,
                )
                return None
            elements.append(self._button(option.label, encoded, f"{self.action_id_prefix}{index}", style=option.style))

        for start in range(0, len(elements), MAX_ACTIONS_ELEMENTS):
            blocks.append({"type": "actions", "elements": elements[start : start + MAX_ACTIONS_ELEMENTS]})
        return blocks

    def input_blocks(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        inputs_for_question: list[inputs.QuestionInput],
    ) -> list[dict[str, Any]] | None:
        """Render input fields and a submit button, or return None on failure."""
        blocks = self._prompt_blocks(question)
        blocks.append(inputs.divider_block())
        try:
            blocks.extend(
                inputs.render_input_blocks(inputs_for_question, action_id_prefix=f"{self.action_id_prefix}input:")
            )
        except inputs.InputRenderError as exc:
            self.logger.warning("Slack could not render a question's inputs: request_id=%s error=%s", request_id, exc)
            return None

        note = str(question.get("note") or "").strip()
        if note:
            blocks.append(inputs.context_block(clamp(normalize_slack_mrkdwn(note), MAX_SECTION_TEXT_LENGTH)))

        # A negative index tells the resolver to read submitted input state.
        encoded = self.encode_button_value(
            request_id=request_id, index=-1, value="", source=source, session_id=session_id
        )
        if not encoded:
            self.logger.warning(
                "Slack question submit has no value that fits in %d characters: request_id=%s",
                MAX_BUTTON_VALUE_LENGTH,
                request_id,
            )
            return None
        label = str(question.get("submit_label") or "").strip() or "Submit"
        blocks.append(
            {
                "type": "actions",
                "elements": [self._button(label, encoded, f"{self.action_id_prefix}submit", style="primary")],
            }
        )
        if len(blocks) > MAX_QUESTION_BLOCKS:
            self.logger.warning(
                "Slack question renders %d blocks, over the %d a message holds: request_id=%s",
                len(blocks),
                MAX_QUESTION_BLOCKS,
                request_id,
            )
            return None
        return blocks

    @staticmethod
    def _button(label: str, value: str, action_id: str, *, style: str = "") -> dict[str, Any]:
        button: dict[str, Any] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": clamp(label, MAX_BUTTON_TEXT_LENGTH), "emoji": True},
            "value": value,
        }
        if style:
            button["style"] = style
        return button

    @classmethod
    def _prompt_blocks(cls, question: Mapping[str, Any]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if question.get("header_block"):
            header = str(question.get("header") or "").strip()
            if header:
                blocks.append(inputs.header_block(header))
            body = normalize_slack_mrkdwn(str(question.get("question") or "").strip())
        else:
            body = cls._section_text(question)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": clamp(body or "…", MAX_SECTION_TEXT_LENGTH)}}
        )
        return blocks

    @staticmethod
    def dropped_questions_block(dropped: list[str]) -> dict[str, Any]:
        named = [prompt for prompt in dropped if prompt]
        quoted = ", ".join(f'"{normalize_slack_mrkdwn(p)}"' for p in named)
        count = len(dropped)
        tail = (
            f"{count} more question that is not on this message"
            if count == 1
            else f"{count} more questions that are not on this message"
        )
        body = f"Slack shows one question at a time. This request also asked {tail}"
        body = f"{body}: {quoted}." if quoted else f"{body}."
        return {"type": "context", "elements": [{"type": "mrkdwn", "text": clamp(body, MAX_SECTION_TEXT_LENGTH)}]}

    @staticmethod
    def _options_legend(options: list[QuestionOption]) -> str:
        lines: list[str] = []
        for option in options:
            if not option.description:
                continue
            shown = clamp(option.description, MAX_OPTION_DESCRIPTION_LENGTH)
            lines.append(f"*{normalize_slack_mrkdwn(option.label)}* — {normalize_slack_mrkdwn(shown)}")
        return clamp("\n".join(lines), MAX_SECTION_TEXT_LENGTH) if lines else ""

    @staticmethod
    def _section_text(question: Mapping[str, Any]) -> str:
        header = str(question.get("header") or "").strip()
        raw_body = str(question.get("question") or "").strip()
        if header and raw_body == header:
            raw_body = ""
        body = normalize_slack_mrkdwn(raw_body)
        if header:
            body = f"*{normalize_slack_mrkdwn(header)}*\n{body}".strip()
        return clamp(body or "…", MAX_SECTION_TEXT_LENGTH)
