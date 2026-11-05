"""Block Kit inputs for private slash-command tasks."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from belle_agent.platforms.slack.inputs import (
    MAX_OPTION_DESCRIPTION_LENGTH,
    MAX_OPTION_TEXT_LENGTH,
    MAX_OPTIONS_PER_ELEMENT,
    plain_text,
)

ACTION_PREFIX = "belle_slash_task:"
FORM_LIFETIME_SECONDS = 3600
SUBMIT_ACTION = ACTION_PREFIX + "submit"
CANCEL_ACTION = ACTION_PREFIX + "cancel"
NAME_ACTION = ACTION_PREFIX + "name"
TASK_ACTION = ACTION_PREFIX + "task"
NAME_BLOCK = ACTION_PREFIX + "name_block"
TASK_BLOCK = ACTION_PREFIX + "task_block"

MAX_INPUT_LENGTH = 3000
MAX_INTRO_SECTION_LENGTH = 2900
MAX_INTRO_SECTIONS = 45


@dataclass(frozen=True)
class TaskForm:
    kind: str
    team_id: str
    user_id: str
    channel_id: str
    created_at: float
    initial_name: str = ""
    names: tuple[str, ...] = ()


def external_name_action_id(nonce: str) -> str:
    return f"{NAME_ACTION}:{nonce}"


def external_name_option(name: str, index: int) -> dict[str, Any]:
    """Use a short index value so long names can still be selected."""
    option: dict[str, Any] = {
        "text": plain_text(name[:MAX_OPTION_TEXT_LENGTH]),
        "value": str(index),
    }
    if len(name) > MAX_OPTION_TEXT_LENGTH:
        prefix = f"#{index + 1} "
        width = max(0, MAX_OPTION_DESCRIPTION_LENGTH - len(prefix))
        option["description"] = plain_text(prefix[:MAX_OPTION_DESCRIPTION_LENGTH] + (name[-width:] if width else ""))
    return option


def _name_element(names: Sequence[str], initial_name: str, nonce: str) -> dict[str, Any]:
    if names and len(names) <= MAX_OPTIONS_PER_ELEMENT and all(len(name) <= MAX_OPTION_TEXT_LENGTH for name in names):
        options = [{"text": plain_text(name), "value": name} for name in names]
        element: dict[str, Any] = {
            "type": "static_select",
            "action_id": NAME_ACTION,
            "placeholder": plain_text("Choose a name"),
            "options": options,
        }
        selected = next((item for item in options if item["value"] == initial_name), None)
        if selected is not None:
            element["initial_option"] = selected
        return element
    if names:
        element = {
            "type": "external_select",
            "action_id": external_name_action_id(nonce),
            "placeholder": plain_text("Search names"),
            "min_query_length": 0,
        }
        if initial_name in names:
            element["initial_option"] = external_name_option(initial_name, names.index(initial_name))
        return element
    element = {
        "type": "plain_text_input",
        "action_id": NAME_ACTION,
        "placeholder": plain_text("Enter a name from the list"),
    }
    if initial_name:
        element["initial_value"] = initial_name[:MAX_INPUT_LENGTH]
    return element


def task_form_blocks(
    form: TaskForm,
    nonce: str,
    intro_text: str = "",
    *,
    bot_name: str,
) -> list[dict[str, Any]]:
    """Render a task form; the submit value identifies server-side form state."""
    kind, names, initial_name = form.kind, form.names, form.initial_name
    label = {"ask": "Question", "skill": "Skill", "subagent": "Subagent"}[kind]
    blocks: list[dict[str, Any]] = []
    if intro_text:
        chunks = [
            intro_text[index : index + MAX_INTRO_SECTION_LENGTH]
            for index in range(0, len(intro_text), MAX_INTRO_SECTION_LENGTH)
        ]
        blocks.extend(
            {"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks[:MAX_INTRO_SECTIONS]
        )
        if len(chunks) > MAX_INTRO_SECTIONS:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "The list is too long for this form. Use the typed command to see it.",
                    },
                }
            )
    if kind != "ask":
        blocks.append(
            {
                "type": "input",
                "block_id": NAME_BLOCK,
                "label": plain_text(label),
                "optional": True,
                "element": _name_element(names, initial_name, nonce),
            }
        )
    blocks.extend(
        (
            {
                "type": "input",
                "block_id": TASK_BLOCK,
                "label": plain_text("Question" if kind == "ask" else "Task"),
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": TASK_ACTION,
                    "multiline": True,
                    "max_length": MAX_INPUT_LENGTH,
                    "placeholder": plain_text(f"What should {bot_name} do?"),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": SUBMIT_ACTION,
                        "text": plain_text("Ask privately" if kind == "ask" else "Run task privately"),
                        "value": nonce,
                    },
                    {
                        "type": "button",
                        "action_id": CANCEL_ACTION,
                        "text": plain_text("Cancel"),
                        "value": nonce,
                    },
                ],
            },
        )
    )
    return blocks


def _entry(state: Any, block_id: str, action_id: str) -> Mapping[str, Any]:
    if not isinstance(state, Mapping):
        return {}
    block = state.get(block_id)
    if not isinstance(block, Mapping):
        return {}
    value = block.get(action_id)
    return value if isinstance(value, Mapping) else {}


def read_task_state(
    state: Any,
    initial_name: str = "",
    *,
    nonce: str = "",
    names: Sequence[str] = (),
) -> tuple[str, str]:
    """Read the name and task from a submit action's complete state."""
    name_entry = _entry(state, NAME_BLOCK, NAME_ACTION)
    option = name_entry.get("selected_option")
    if isinstance(option, Mapping):
        name = str(option.get("value") or "").strip()
    else:
        external = _entry(state, NAME_BLOCK, external_name_action_id(nonce))
        option = external.get("selected_option")
        if isinstance(option, Mapping):
            index = str(option.get("value") or "")
            valid = index.isascii() and index.isdecimal() and len(index) <= len(str(len(names)))
            position = int(index) if valid else -1
            name = names[position] if 0 <= position < len(names) else ""
        else:
            name = str(name_entry.get("value") or initial_name).strip()
    task = str(_entry(state, TASK_BLOCK, TASK_ACTION).get("value") or "").strip()
    return name, task
