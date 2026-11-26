"""Present answerable questions as Slack Block Kit messages."""

import json
import logging
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from belle_agent import click_notices
from belle_agent.platforms.rendering import markdown_parts
from belle_agent.platforms.slack import inputs
from belle_agent.platforms.slack.blocks import MAX_BLOCKS_PER_MESSAGE
from belle_agent.platforms.slack.text import MAX_SLACK_TEXT_LENGTH, clamp, normalize_slack_mrkdwn

MAX_BUTTON_VALUE_LENGTH = 2000
MAX_BUTTON_TEXT_LENGTH = 75
MAX_SECTION_TEXT_LENGTH = 3000
# Slack's text object limit, which applies to each mrkdwn element of a context block.
MAX_CONTEXT_TEXT_LENGTH = 3000
MAX_ACTIONS_ELEMENTS = 25
# Room for the notice that delivery adds to the text of a message whose blocks Slack refuses.
_FALLBACK_NOTICE_ROOM = 500
# The text of each message of a question holds all of its blocks' text, also after a refusal of the blocks.
MAX_QUESTION_TEXT_LENGTH = MAX_SLACK_TEXT_LENGTH - _FALLBACK_NOTICE_ROOM
_INTERACTIVE_BLOCK_TYPES = frozenset({"actions", "input"})
# A block tagged True holds question text, which can move to a message before the controls.
_Entry = tuple[bool, dict[str, Any]]

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


class QuestionRenderError(ValueError):
    """A question that Slack messages cannot show. The message says why."""



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

    def answer_messages(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        declared: list[inputs.QuestionInput],
        options: list[QuestionOption],
        dropped: list[str],
        max_messages: int,
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """Render the text and blocks of each message to post in order. The last message holds the controls.

        Raise ``QuestionRenderError`` if the question needs more than ``max_messages`` messages, or cannot be shown.
        """
        if declared:
            entries = self._input_entries(
                request_id=request_id, source=source, session_id=session_id,
                question=question, inputs_for_question=declared,
            )
        else:
            entries = self._option_entries(
                request_id=request_id, source=source, session_id=session_id,
                question=question, options=options,
            )
        if dropped:
            entries.extend(_context_entries(self._dropped_questions_text(dropped)))
        messages = self._messages(entries)
        if len(messages) == 1:
            fallback = self.fallback_text(question)
            return [(fallback if len(fallback) <= MAX_QUESTION_TEXT_LENGTH else message_text(messages[0]), messages[0])]
        if len(messages) > max_messages:
            size = sum(len(message_text(blocks)) for blocks in messages)
            raise QuestionRenderError(
                f"the question has {size} characters and needs {len(messages)} Slack messages, "
                f"over the {max_messages} that platforms.slack.max_question_messages allows"
            )
        self.logger.info("Slack question takes %d messages: request_id=%s", len(messages), request_id)
        return [(message_text(blocks), blocks) for blocks in messages]

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

    def _option_entries(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        options: list[QuestionOption],
    ) -> list[_Entry]:
        """Raise ``QuestionRenderError`` if a button cannot carry its request identity."""
        entries = self._prompt_entries(question)
        entries.extend(_section_entries(self._options_legend(options)))

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
                raise QuestionRenderError(
                    f"option {index} has no button value that fits in Slack's {MAX_BUTTON_VALUE_LENGTH} characters"
                )
            elements.append(self._button(option.label, encoded, f"{self.action_id_prefix}{index}", style=option.style))

        for start in range(0, len(elements), MAX_ACTIONS_ELEMENTS):
            entries.append((False, {"type": "actions", "elements": elements[start : start + MAX_ACTIONS_ELEMENTS]}))
        return entries

    def _input_entries(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        inputs_for_question: list[inputs.QuestionInput],
    ) -> list[_Entry]:
        """Render input fields and a submit button, or raise ``QuestionRenderError``."""
        entries = self._prompt_entries(question)
        entries.append((False, inputs.divider_block()))
        try:
            rendered = inputs.render_input_blocks(inputs_for_question, action_id_prefix=f"{self.action_id_prefix}input:")
        except inputs.InputRenderError as exc:
            raise QuestionRenderError(f"Slack cannot render its inputs: {exc}") from exc
        entries.extend((False, block) for block in rendered)
        entries.extend(_context_entries(normalize_slack_mrkdwn(str(question.get("note") or "").strip())))

        # A negative index tells the resolver to read submitted input state.
        encoded = self.encode_button_value(
            request_id=request_id, index=-1, value="", source=source, session_id=session_id
        )
        if not encoded:
            raise QuestionRenderError(
                f"the submit button has no value that fits in Slack's {MAX_BUTTON_VALUE_LENGTH} characters"
            )
        label = str(question.get("submit_label") or "").strip() or "Submit"
        button = self._button(label, encoded, f"{self.action_id_prefix}submit", style="primary")
        entries.append((False, {"type": "actions", "elements": [button]}))
        return entries

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
    def _prompt_entries(cls, question: Mapping[str, Any]) -> list[_Entry]:
        header = cls._header_block_text(question)
        entries: list[_Entry] = [(False, inputs.header_block(header))] if header else []
        body = normalize_slack_mrkdwn(str(question.get("question") or "").strip()) if header else cls._section_text(question)
        entries.extend(_section_entries(body or "…"))
        return entries

    @staticmethod
    def _header_block_text(question: Mapping[str, Any]) -> str:
        """Return the header for a header block, or "" to show it in the section text."""
        header = str(question.get("header") or "").strip()
        # A Slack header block holds at most 150 characters, so a longer header goes in the section text.
        return header if question.get("header_block") and len(header) <= inputs.MAX_HEADER_LENGTH else ""

    @staticmethod
    def _messages(entries: list[_Entry]) -> list[list[dict[str, Any]]]:
        """Split a question so that each message fits, with all of its text before the controls.

        Each earlier message starts with its part number. The last one says which parts come before it.
        """
        blocks = [block for _, block in entries]
        if len(blocks) <= MAX_BLOCKS_PER_MESSAGE and len(message_text(blocks)) <= MAX_QUESTION_TEXT_LENGTH:
            return [blocks]
        controls = next(
            (index for index, (_, block) in enumerate(entries) if block["type"] in _INTERACTIVE_BLOCK_TYPES), len(entries)
        )
        lead, rest = entries[:controls], entries[controls:]
        room, budget = MAX_BLOCKS_PER_MESSAGE - 1, MAX_QUESTION_TEXT_LENGTH - _PART_LABEL_ROOM
        rest_blocks = [block for _, block in rest]
        if len(rest) > room or len(message_text(rest_blocks)) > budget:
            # The text after the first control moves to the end of the text before it.
            at = max((index + 1 for index, (text, _) in enumerate(lead) if text), default=0)
            lead = [*lead[:at], *(entry for entry in rest if entry[0]), *lead[at:]]
            rest = [entry for entry in rest if not entry[0]]
            rest_blocks = [block for _, block in rest]
        if len(rest) > room:
            raise QuestionRenderError(
                f"its controls take {len(rest)} blocks, over the {room} that a Slack message holds beside a notice"
            )
        kept, size = 0, len(message_text(rest_blocks))
        for _, block in reversed(lead):
            size += len(message_text([block])) + 2
            if len(rest) + kept == room or size > budget:
                break
            kept += 1
        earlier = _group(block for _, block in lead[: len(lead) - kept])
        total = len(earlier) + 1
        last = [block for _, block in [*lead[len(lead) - kept :], *rest]]
        notice = (
            f"*Part {total} of {total} of this question.* Parts 1 to {total - 1} are above in this thread. "
            "Read them before you answer."
        )
        return [
            *([_section(f"*Part {number} of {total} of this question.*"), *part] for number, part in enumerate(earlier, 1)),
            [_section(notice), *last],
        ]

    @staticmethod
    def _dropped_questions_text(dropped: list[str]) -> str:
        named = [prompt for prompt in dropped if prompt]
        quoted = ", ".join(f'"{normalize_slack_mrkdwn(p)}"' for p in named)
        count = len(dropped)
        tail = (
            f"{count} more question that is not on this message"
            if count == 1
            else f"{count} more questions that are not on this message"
        )
        body = f"Slack shows one question at a time. This request also asked {tail}"
        return f"{body}: {quoted}." if quoted else f"{body}."

    @staticmethod
    def _options_legend(options: list[QuestionOption]) -> str:
        """List each option with a description, or with a label longer than its button shows."""
        return "\n".join(
            " — ".join(
                filter(None, (f"*{normalize_slack_mrkdwn(option.label)}*", normalize_slack_mrkdwn(option.description)))
            )
            for option in options
            if option.description or len(option.label) > MAX_BUTTON_TEXT_LENGTH
        )

    @staticmethod
    def _section_text(question: Mapping[str, Any]) -> str:
        header = str(question.get("header") or "").strip()
        raw_body = str(question.get("question") or "").strip()
        if header and raw_body == header:
            raw_body = ""
        body = normalize_slack_mrkdwn(raw_body)
        if header:
            body = f"*{normalize_slack_mrkdwn(header)}*\n{body}".strip()
        return body


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _parts(text: str, limit: int, block: Callable[[str], dict[str, Any]]) -> list[_Entry]:
    """Split text over as many blocks as its length needs."""
    if not text:
        return []
    parts = [text] if len(text) <= limit else markdown_parts(text, limit, len)
    return [(True, block(part)) for part in parts]


def _section_entries(text: str) -> list[_Entry]:
    return _parts(text, MAX_SECTION_TEXT_LENGTH, _section)


def _context_entries(text: str) -> list[_Entry]:
    # One part per context block: Slack sets elements of one block side by side.
    return _parts(text, MAX_CONTEXT_TEXT_LENGTH, inputs.context_block)


def message_text(blocks: list[dict[str, Any]]) -> str:
    """Return the text of header, section and context blocks, for the text of their message."""
    texts: list[str] = []
    for block in blocks:
        if isinstance(block.get("text"), Mapping):
            texts.append(str(block["text"].get("text") or ""))
        if block.get("type") == "context":
            texts.extend(str(element.get("text") or "") for element in block.get("elements", []))
    return "\n\n".join(text for text in texts if text)


# Room for the part number at the start of a message of a split question.
_PART_LABEL_ROOM = 200


def _group(blocks: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group blocks into messages that each leave room for a part number."""
    messages: list[list[dict[str, Any]]] = []
    size = 0
    for block in blocks:
        length = len(message_text([block])) + 2
        if not messages or len(messages[-1]) == MAX_BLOCKS_PER_MESSAGE - 1 or size + length > MAX_QUESTION_TEXT_LENGTH - _PART_LABEL_ROOM:
            messages.append([])
            size = 0
        messages[-1].append(block)
        size += length
    return messages
