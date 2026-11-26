"""Read a Docker Agent elicitation as one Belle question, and a person's answer as the content it takes."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Docker Agent's tool that asks a person. Its ``schema`` argument is the elicitation's schema.
USER_PROMPT = "user_prompt"
#: The meta key of the title ``user_prompt`` gives.
TITLE_META = "cagent/title"
#: Docker Agent's content keys: of a free-text answer, and of a primitive schema without a title.
FREE_TEXT_KEY = "response"
VALUE_KEY = "value"
BOOLEAN = ("true", "false")
# A platform shows text or options and checks nothing else, so Belle takes only the keywords it shows or applies.
#: The keywords of the one answer.
KEYWORDS = frozenset({"type", "enum", "title", "description", "default"})
#: The keywords of a schema that holds the answer as its one property, or that asks for free text.
OBJECT_KEYWORDS = frozenset({"type", "properties", "required", "title", "description", "additionalProperties"})
#: Says nothing of the answer.
META_KEYWORDS = frozenset({"$schema"})
ASKS = (
    "Belle asks one question, answered by free text, a string, one value of an enum of strings, or a boolean."
    " Give no schema, a schema of one of these types, or an object schema with one such property"
)


@dataclass(frozen=True)
class Form:
    """The one answer an elicitation asks for: its content key, its options where it is a choice, and whether
    the answer is a boolean."""

    key: str
    options: tuple[str, ...] = ()
    description: str = ""
    boolean: bool = False

    def content(self, values: Sequence[str]) -> dict[str, Any]:
        """The content of an accepted answer. An answer that is not one value of the options raises ValueError."""
        if len(values) != 1:
            raise ValueError(f"the answer has {len(values)} values, and the question takes one")
        (value,) = values
        if self.options and value not in self.options:
            raise ValueError(f"the answer {value!r} is none of the options {list(self.options)}")
        return {self.key: value == "true" if self.boolean else value}


def form(schema: Any) -> Form:
    """The one answer ``schema`` asks for, as Docker Agent's own form reads it. Raise ValueError for a schema
    that Belle cannot ask, with what it expects."""
    if schema is None:
        return Form(FREE_TEXT_KEY)
    if not isinstance(schema, Mapping):
        raise ValueError(f"schema is {type(schema).__name__}, not a JSON object. {ASKS}")  # noqa: TRY004
    properties = schema.get("properties")
    if properties is None and ("enum" in schema or schema.get("type") not in (None, "object")):
        # A schema of the answer itself.
        return _answer(str(schema.get("title") or VALUE_KEY), schema, "schema")
    _known(schema, OBJECT_KEYWORDS, "schema")
    if schema.get("type") not in (None, "object"):
        raise ValueError(f"schema has type {schema.get('type')!r}. {ASKS}")
    if schema.get("additionalProperties", False) is not False:
        raise ValueError(f"schema.additionalProperties is not false. {ASKS}")
    description = str(schema.get("description") or "")
    if not properties:
        # Docker Agent's own form asks for free text where the schema names no property.
        if properties is not None and not isinstance(properties, Mapping):
            raise ValueError(f"schema.properties is not a JSON object. {ASKS}")
        if schema.get("required"):
            raise ValueError(f"schema.required names properties that schema.properties lacks. {ASKS}")
        return Form(FREE_TEXT_KEY, description=description)
    if not isinstance(properties, Mapping) or len(properties) != 1:
        count = len(properties) if isinstance(properties, Mapping) else "no object of"
        raise ValueError(f"schema.properties has {count} properties. {ASKS}")
    ((name, field),) = properties.items()
    required = schema.get("required", [])
    if not isinstance(required, list) or any(item != name for item in required):
        raise ValueError(f"schema.required is not a list that names only the property {name}. {ASKS}")
    asked = _answer(str(name), field, f"schema.properties.{name}")
    return asked if asked.description else Form(asked.key, asked.options, description, asked.boolean)


def _known(schema: Mapping[str, Any], keywords: frozenset[str], path: str) -> None:
    if unknown := sorted(set(schema) - keywords - META_KEYWORDS):
        raise ValueError(f"{path} has {', '.join(unknown)}, which Belle cannot check. {ASKS}")


def _answer(key: str, field: Any, path: str) -> Form:
    if not isinstance(field, Mapping):
        raise ValueError(f"{path} is not a JSON object. {ASKS}")  # noqa: TRY004
    _known(field, KEYWORDS, path)
    description = str(field.get("description") or "")
    kind = field.get("type", "string")
    enum = field.get("enum")
    if kind == "string" and enum is not None:
        if not isinstance(enum, list) or not enum or not all(isinstance(value, str) for value in enum):
            raise ValueError(f"{path}.enum is not a list of strings. {ASKS}")
        return Form(key, tuple(enum), description)
    if kind == "string":
        return Form(key, description=description)
    if kind == "boolean" and enum is None:
        return Form(key, BOOLEAN, description, boolean=True)
    raise ValueError(f"{path} has type {kind!r}{' with an enum' if enum is not None else ''}. {ASKS}")


def question_text(message: str, meta: Any, asked: Form) -> str:
    """What a person reads: the title, the message, and the answer's description."""
    title = str(meta.get(TITLE_META) or "") if isinstance(meta, Mapping) else ""
    text = f"{title}: {message}" if title else message
    return f"{text}\n{asked.description}" if asked.description else text
