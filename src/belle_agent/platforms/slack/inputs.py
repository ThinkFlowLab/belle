"""Render Slack question inputs and read their submitted state."""

import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

from belle_agent.platforms.slack.text import clamp

logger = logging.getLogger(__name__)

MAX_INPUTS_PER_QUESTION = 10
MAX_LABEL_LENGTH = 150
MAX_HINT_LENGTH = 150
MAX_PLACEHOLDER_LENGTH = 150
MAX_HEADER_LENGTH = 150
MAX_OPTION_TEXT_LENGTH = 75
MAX_OPTION_VALUE_LENGTH = 75
MAX_OPTION_DESCRIPTION_LENGTH = 75
MAX_OPTIONS_PER_ELEMENT = 100
MAX_CONTEXT_ELEMENTS = 10

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class InputRenderError(ValueError):
    """Invalid Block Kit input declaration."""


def plain_text(text: str, *, emoji: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "plain_text", "text": text}
    if emoji:
        value["emoji"] = True
    return value


def _read_value(key: str, *, multi: bool = False, option: bool = False) -> Callable[[Mapping[str, Any]], list[str]]:
    def read(entry: Mapping[str, Any]) -> list[str]:
        raw = entry.get(key)
        if multi:
            if not isinstance(raw, list):
                return []
            return [value for item in raw if (value := _option_value(item) if option else str(item or "").strip())]
        value = _option_value(raw) if option else str(raw).strip() if raw is not None else ""
        return [value] if value else []

    return read


def _option_value(option: Any) -> str:
    """Read an option value, or its visible label when the value is absent."""
    if not isinstance(option, Mapping):
        return ""
    value = str(option.get("value") or "").strip()
    if value:
        return value
    text = option.get("text")
    if isinstance(text, Mapping):
        return str(text.get("text") or "").strip()
    return ""


def _flatten_rich_text(node: Any) -> str:
    """Read visible text from a rich-text input, without styling."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_rich_text(item) for item in node)
    if not isinstance(node, Mapping):
        return ""

    node_type = str(node.get("type") or "")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "link":
        return str(node.get("text") or node.get("url") or "")
    if node_type == "emoji":
        return f":{node.get('name')}:" if node.get("name") else ""
    if node_type in {"user", "usergroup", "channel"}:
        for key in ("user_id", "usergroup_id", "channel_id"):
            if node.get(key):
                return str(node[key])
        return ""
    children = node.get("elements")
    if not isinstance(children, list):
        return ""
    if node_type in {"rich_text", "rich_text_list"}:
        return "\n".join(part for part in (_flatten_rich_text(child) for child in children) if part)
    return "".join(_flatten_rich_text(child) for child in children)


def _read_rich_text(entry: Mapping[str, Any]) -> list[str]:
    text = _flatten_rich_text(entry.get("rich_text_value")).strip()
    return [text] if text else []


def _build_option(option: Mapping[str, Any]) -> dict[str, Any]:
    """Build an option without truncating its submitted value."""
    label = str(option.get("label") or "").strip()
    value = str(option.get("value") or "").strip() or label
    if not label:
        label = value
    if not label or not value:
        raise InputRenderError("option.label or option.value must contain a non-empty string")
    if len(value) > MAX_OPTION_VALUE_LENGTH:
        raise InputRenderError(
            f"option value is {len(value)} characters, over Slack's {MAX_OPTION_VALUE_LENGTH}: {value[:40]!r}"
        )
    built: dict[str, Any] = {"text": plain_text(clamp(label, MAX_OPTION_TEXT_LENGTH), emoji=True), "value": value}
    description = str(option.get("description") or "").strip()
    if description:
        built["description"] = plain_text(clamp(description, MAX_OPTION_DESCRIPTION_LENGTH), emoji=True)
    return built


def _build_options(declaration: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = declaration.get("options")
    if not isinstance(raw, list):
        raise InputRenderError("options must be a non-empty list of option objects")
    if len(raw) > MAX_OPTIONS_PER_ELEMENT:
        raise InputRenderError(f"options must contain at most {MAX_OPTIONS_PER_ELEMENT} entries, received {len(raw)}")
    options: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise InputRenderError(f"options[{index}] must be an option object")
        for name in ("label", "value", "description"):
            if name in entry and not isinstance(entry[name], str):
                raise InputRenderError(f"options[{index}].{name} must be a string")
        try:
            options.append(_build_option(entry))
        except InputRenderError as exc:
            raise InputRenderError(f"options[{index}]: {exc}") from exc
    return options


@dataclass(frozen=True)
class ElementSpec:
    """Slack element fields and state reader."""

    read: Callable[[Mapping[str, Any]], list[str]]
    multi: bool = False
    initial_key: str = ""
    initial_kind: str = "text"
    needs_options: bool = False
    supports_placeholder: bool = True
    passthrough: tuple[tuple[str, str], ...] = ()
    required_fields: tuple[tuple[str, Any], ...] = ()
    string_typed: frozenset[str] = frozenset()


ELEMENT_SPECS: dict[str, ElementSpec] = {
    "datepicker": ElementSpec(
        read=_read_value("selected_date"),
        initial_key="initial_date",
    ),
    "timepicker": ElementSpec(
        read=_read_value("selected_time"),
        initial_key="initial_time",
        passthrough=(("timezone", "timezone"),),
    ),
    "datetimepicker": ElementSpec(
        read=_read_value("selected_date_time"),
        initial_key="initial_date_time",
        initial_kind="int",
        supports_placeholder=False,
    ),
    "static_select": ElementSpec(
        read=_read_value("selected_option", option=True),
        initial_key="initial_option",
        initial_kind="option",
        needs_options=True,
    ),
    "multi_static_select": ElementSpec(
        read=_read_value("selected_options", multi=True, option=True),
        multi=True,
        initial_key="initial_options",
        initial_kind="options",
        needs_options=True,
        passthrough=(("max_selected", "max_selected_items"),),
    ),
    "checkboxes": ElementSpec(
        read=_read_value("selected_options", multi=True, option=True),
        multi=True,
        initial_key="initial_options",
        initial_kind="options",
        needs_options=True,
        supports_placeholder=False,
    ),
    "radio_buttons": ElementSpec(
        read=_read_value("selected_option", option=True),
        initial_key="initial_option",
        initial_kind="option",
        needs_options=True,
        supports_placeholder=False,
    ),
    "plain_text_input": ElementSpec(
        read=_read_value("value"),
        initial_key="initial_value",
        passthrough=(
            ("multiline", "multiline"),
            ("min_length", "min_length"),
            ("max_length", "max_length"),
        ),
    ),
    "email_text_input": ElementSpec(
        read=_read_value("value"),
        initial_key="initial_value",
    ),
    "url_text_input": ElementSpec(
        read=_read_value("value"),
        initial_key="initial_value",
    ),
    "number_input": ElementSpec(
        read=_read_value("value"),
        initial_key="initial_value",
        passthrough=(
            ("decimal", "is_decimal_allowed"),
            ("min", "min_value"),
            ("max", "max_value"),
        ),
        required_fields=(("is_decimal_allowed", True),),
        string_typed=frozenset({"min_value", "max_value"}),
    ),
    "rich_text_input": ElementSpec(
        read=_read_rich_text,
        initial_key="initial_value",
        initial_kind="rich_text",
    ),
}
for singular, plural in (("user", "users"), ("conversation", "conversations"), ("channel", "channels")):
    ELEMENT_SPECS[f"{plural}_select"] = ElementSpec(
        read=_read_value(f"selected_{singular}"), initial_key=f"initial_{singular}"
    )
    ELEMENT_SPECS[f"multi_{plural}_select"] = ElementSpec(
        read=_read_value(f"selected_{plural}", multi=True),
        multi=True,
        initial_key=f"initial_{plural}",
        initial_kind="options",
        passthrough=(("max_selected", "max_selected_items"),),
    )


def _slack_string(value: Any) -> str:
    """Convert a number to the string required by Slack bounds."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _build_element(
    element_type: str,
    spec: ElementSpec,
    declaration: Mapping[str, Any],
    *,
    action_id: str,
    initial: Any = None,
) -> dict[str, Any]:
    """Build a declared Slack element."""
    element: dict[str, Any] = {"type": element_type, "action_id": action_id}

    if spec.needs_options:
        options = _build_options(declaration)
        if not options:
            raise InputRenderError(f"{element_type} needs a non-empty options list")
        element["options"] = options

    placeholder = str(declaration.get("placeholder") or "").strip()
    if placeholder and spec.supports_placeholder:
        element["placeholder"] = plain_text(clamp(placeholder, MAX_PLACEHOLDER_LENGTH), emoji=True)

    if initial is None:
        initial = declaration.get("initial")
    if initial not in (None, "", [], {}):
        rendered = _render_initial(spec, initial, element.get("options"))
        if rendered is not None:
            element[spec.initial_key] = rendered

    for declared_name, slack_name in spec.passthrough:
        value = declaration.get(declared_name)
        if value is not None:
            element[slack_name] = _slack_string(value) if slack_name in spec.string_typed else value

    for slack_name, default in spec.required_fields:
        element.setdefault(slack_name, default)

    return element


def _render_initial(spec: ElementSpec, initial: Any, options: list[dict[str, Any]] | None) -> Any:
    """Match an initial option to a rendered option when required."""
    if spec.initial_kind == "int":
        try:
            return int(initial)
        except (TypeError, ValueError):
            return None
    if spec.initial_kind == "rich_text":
        return initial if isinstance(initial, Mapping) else None
    if spec.initial_kind == "option":
        wanted = str(initial).strip()
        for option in options or []:
            if option.get("value") == wanted:
                return option
        return None
    if spec.initial_kind == "options":
        wanted_list = initial if isinstance(initial, list) else [initial]
        wanted = {str(item).strip() for item in wanted_list}
        if options is None:
            return [value for item in wanted_list if (value := str(item).strip())]
        picked = [option for option in options if option.get("value") in wanted]
        return picked or None
    return str(initial)


class InputValueError(ValueError):
    """Raised when what came back for an input is not a value it can answer with."""


def _format_passthrough(values: list[str], _entries: list[Mapping[str, Any]], _zone: str) -> list[str]:
    return values


def _format_date(values: list[str], _entries: list[Mapping[str, Any]], _zone: str) -> list[str]:
    for value in values:
        try:
            if not _DATE_RE.fullmatch(value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as exc:
            raise InputValueError(f"{value!r} is not a valid YYYY-MM-DD date") from exc
    return values


def _zone_suffix(entries: Sequence[Mapping[str, Any]], declared: str) -> str:
    """Use the reported time zone, falling back to the declared zone."""
    for entry in entries:
        reported = str(entry.get("timezone") or "").strip()
        if reported:
            return reported
    return declared.strip()


def _format_time(values: list[str], entries: list[Mapping[str, Any]], declared_zone: str) -> list[str]:
    """Format a bare time with its zone name and no UTC offset."""
    formatted: list[str] = []
    zone = _zone_suffix(entries, declared_zone)
    for value in values:
        try:
            if not _TIME_RE.fullmatch(value):
                raise ValueError
            time.fromisoformat(value)
        except ValueError as exc:
            raise InputValueError(f"{value!r} is not a valid HH:mm time") from exc
        formatted.append(f"{value}:00[{zone}]" if zone else f"{value}:00")
    return formatted


def _format_epoch(values: list[str], _entries: list[Mapping[str, Any]], _zone: str) -> list[str]:
    """Format a Unix timestamp as a UTC instant."""
    formatted: list[str] = []
    for value in values:
        try:
            seconds = int(value)
            instant = datetime.fromtimestamp(seconds, UTC)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise InputValueError(f"{value!r} is not a Unix timestamp representable as a UTC datetime") from exc
        formatted.append(instant.isoformat())
    return formatted


def _format_number(values: list[str], _entries: list[Mapping[str, Any]], _zone: str) -> list[str]:
    """Reject non-finite and malformed numeric values."""
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise InputValueError(f"{value!r} is not a number") from exc
        if not math.isfinite(number):
            raise InputValueError(f"{value!r} is not a number")
    return values


def compose_datetime(date_value: str, time_value: str, zone: str) -> str:
    """Resolve a local date and time against its IANA zone."""
    if not zone:
        return f"{date_value}T{time_value}:00"
    try:
        from zoneinfo import ZoneInfo

        moment = datetime.fromisoformat(f"{date_value}T{time_value}:00").replace(tzinfo=ZoneInfo(zone))
    except Exception:  # noqa: BLE001 - unknown zone, or no tz database installed
        logger.warning(
            "Unknown Slack timezone %s. Keeping its name without an offset",
            zone,
        )
        return f"{date_value}T{time_value}:00[{zone}]"
    return f"{moment.isoformat()}[{zone}]"


@dataclass(frozen=True)
class InputSpec:
    """Input type, element parts, and value formatter."""

    name: str
    element_types: tuple[str, ...]
    parts: tuple[str, ...] = ("",)
    format: Callable[[list[str], list[Mapping[str, Any]], str], list[str]] = _format_passthrough
    composite: bool = False

    @property
    def multi(self) -> bool:
        return any(ELEMENT_SPECS[name].multi for name in self.element_types)


INPUT_SPECS: dict[str, InputSpec] = {
    name: InputSpec(name, (element,))
    for name, element in {
        "select": "static_select",
        "multi_select": "multi_static_select",
        "user": "users_select",
        "users": "multi_users_select",
        "conversation": "conversations_select",
        "conversations": "multi_conversations_select",
        "channel": "channels_select",
        "channels": "multi_channels_select",
        "checkboxes": "checkboxes",
        "radio": "radio_buttons",
        "text": "plain_text_input",
        "email": "email_text_input",
        "url": "url_text_input",
        "rich_text": "rich_text_input",
    }.items()
}
INPUT_SPECS.update(
    date=InputSpec("date", ("datepicker",), format=_format_date),
    time=InputSpec("time", ("timepicker",), format=_format_time),
    datetime=InputSpec("datetime", ("datepicker", "timepicker"), parts=("date", "time"), composite=True),
    datetime_unix=InputSpec("datetime_unix", ("datetimepicker",), format=_format_epoch),
    number=InputSpec("number", ("number_input",), format=_format_number),
)


@dataclass
class QuestionInput:
    """Resolved input declaration and its action ID."""

    spec: InputSpec
    name: str
    label: str
    declaration: Mapping[str, Any] = field(default_factory=dict)
    optional: bool = False
    index: int = 0

    def action_id(self, prefix: str, part: str) -> str:
        return f"{prefix}{self.index}.{part}" if part else f"{prefix}{self.index}"


def parse_inputs(question: Mapping[str, Any]) -> list[QuestionInput]:
    """Resolve input declarations, or return an empty list when absent."""
    raw = question.get("inputs")
    if not isinstance(raw, (list, tuple)) or not raw:
        return []

    inputs: list[QuestionInput] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"type": entry}
        if not isinstance(entry, Mapping):
            logger.warning("Slack question input is not a declaration: %r", entry)
            continue
        type_name = str(entry.get("type") or "").strip()
        spec = INPUT_SPECS.get(type_name)
        if spec is None:
            raise InputRenderError(
                f"unknown question input type {type_name!r}. Known types: " + ", ".join(sorted(INPUT_SPECS))
            )
        name = str(entry.get("name") or "").strip() or spec.name
        label = str(entry.get("label") or "").strip() or name
        inputs.append(
            QuestionInput(
                spec=spec,
                name=name,
                label=clamp(label, MAX_LABEL_LENGTH),
                declaration=entry,
                optional=bool(entry.get("optional")),
                index=len(inputs),
            )
        )

    if len(inputs) > MAX_INPUTS_PER_QUESTION:
        raise InputRenderError(
            f"a question declares {len(inputs)} inputs, over the {MAX_INPUTS_PER_QUESTION} one message carries"
        )
    names = [entry.name for entry in inputs]
    if len(set(names)) != len(names):
        raise InputRenderError(f"question inputs do not have distinct names: {names}")
    return inputs


def divider_block() -> dict[str, Any]:
    """A horizontal rule. Non-interactive and holds nothing."""
    return {"type": "divider"}


def header_block(text: str) -> dict[str, Any]:
    """A large plain-text heading."""
    return {"type": "header", "text": plain_text(clamp(text, MAX_HEADER_LENGTH), emoji=True)}


def context_block(*lines: str) -> dict[str, Any]:
    """Small grey text beneath a block, for notes that are not the message."""
    elements = [{"type": "mrkdwn", "text": line} for line in lines[:MAX_CONTEXT_ELEMENTS] if line]
    return {"type": "context", "elements": elements}


def render_input_blocks(inputs: Sequence[QuestionInput], *, action_id_prefix: str) -> list[dict[str, Any]]:
    """Render declared inputs as labeled Block Kit input blocks."""
    blocks: list[dict[str, Any]] = []
    for entry in inputs:
        for part, element_type in zip(entry.spec.parts, entry.spec.element_types):
            spec = ELEMENT_SPECS[element_type]
            action_id = entry.action_id(action_id_prefix, part)
            element = _build_element(
                element_type,
                spec,
                entry.declaration,
                action_id=action_id,
                initial=_initial_for_part(entry, part),
            )
            block: dict[str, Any] = {
                "type": "input",
                "block_id": action_id,
                "label": plain_text(clamp(_label_for_part(entry, part), MAX_LABEL_LENGTH), emoji=True),
                "element": element,
                "optional": entry.optional,
            }
            hint = str(entry.declaration.get("hint") or "").strip()
            if hint:
                block["hint"] = plain_text(clamp(hint, MAX_HINT_LENGTH), emoji=True)
            blocks.append(block)
    return blocks


def _label_for_part(entry: QuestionInput, part: str) -> str:
    """Return the label for one part of a composite input."""
    if not part:
        return entry.label
    declared = str(entry.declaration.get(f"{part}_label") or "").strip()
    if declared:
        return declared
    if part == entry.spec.parts[0]:
        return entry.label
    return part


def _initial_for_part(entry: QuestionInput, part: str) -> Any:
    if not part:
        return entry.declaration.get("initial")
    return entry.declaration.get(f"initial_{part}")


@dataclass
class InputResult:
    """Submitted values and error for one input."""

    name: str
    label: str
    values: list[str]
    error: str = ""

    @property
    def empty(self) -> bool:
        return not self.values


def flatten_state(state_values: Any) -> dict[str, Mapping[str, Any]]:
    """Index Slack state entries by action ID."""
    flattened: dict[str, Mapping[str, Any]] = {}
    if not isinstance(state_values, Mapping):
        return flattened
    for block in state_values.values():
        if not isinstance(block, Mapping):
            continue
        for action_id, entry in block.items():
            if isinstance(entry, Mapping):
                flattened[str(action_id)] = entry
    return flattened


def read_state(
    inputs: Sequence[QuestionInput],
    state_values: Any,
    *,
    action_id_prefix: str,
) -> list[InputResult]:
    """Read submitted inputs in declaration order."""
    flattened = flatten_state(state_values)
    results: list[InputResult] = []
    for entry in inputs:
        raw: list[str] = []
        entries: list[Mapping[str, Any]] = []
        for part, element_type in zip(entry.spec.parts, entry.spec.element_types):
            state_entry = flattened.get(entry.action_id(action_id_prefix, part), {})
            entries.append(state_entry)
            raw.extend(ELEMENT_SPECS[element_type].read(state_entry))
        results.append(_finish(entry, raw, entries))
    return results


def _finish(entry: QuestionInput, raw: list[str], entries: list[Mapping[str, Any]]) -> InputResult:
    declared_zone = str(entry.declaration.get("timezone") or "").strip()
    if entry.spec.composite:
        return _finish_composite(entry, raw, entries, declared_zone)
    result = InputResult(entry.name, entry.label, [])
    if not raw:
        return result
    try:
        result.values = entry.spec.format(raw, entries, declared_zone)
    except InputValueError as exc:
        result.error = str(exc)
    return result


def _finish_composite(
    entry: QuestionInput,
    raw: list[str],
    entries: list[Mapping[str, Any]],
    declared_zone: str,
) -> InputResult:
    """Join date and time or report the missing part."""
    date_values = [value for value in raw if _DATE_RE.match(value)]
    time_values = [value for value in raw if _TIME_RE.match(value)]
    malformed = [value for value in raw if value not in date_values and value not in time_values]
    result = InputResult(entry.name, entry.label, [])
    if malformed:
        result.error = f"{malformed[0]!r} is neither a YYYY-MM-DD date nor an HH:mm time"
    if not date_values and not time_values:
        return result
    if not result.error:
        if not date_values:
            result.error = "no date was picked"
        elif not time_values:
            result.error = "no time was picked"
        else:
            try:
                _format_date(date_values, entries, declared_zone)
                _format_time(time_values, entries, declared_zone)
            except InputValueError as exc:
                result.error = str(exc)
                return result
            zone = _zone_suffix(entries, declared_zone)
            result.values = [compose_datetime(date_values[0], time_values[0], zone)]
    return result


def submit_problem(inputs: Sequence[QuestionInput], results: Sequence[InputResult]) -> str:
    """Return the first submit error, or an empty string."""
    for result in results:
        if result.error:
            return f"{result.label}: {result.error}"
    for entry, result in zip(inputs, results):
        if result.empty and not entry.optional:
            return f"{result.label}: nothing was selected"
    if all(result.empty for result in results):
        return "nothing was selected"
    return ""
