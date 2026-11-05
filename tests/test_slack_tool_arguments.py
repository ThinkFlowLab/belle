"""What each Slack tool declares it needs, and how it refuses an argument that is not a Slack id."""

import asyncio
import inspect
import json
import re

import pytest

from belle_agent.platforms.loopback.platform import LoopbackToolkit
from belle_agent.platforms.slack import (
    bookmarks,
    canvases,
    directory,
    files,
    home_tab,
    lists,
    pins,
    post,
    reactions,
    scheduled,
    search,
)
from belle_agent.platforms.slack.tool_context import slack_id_error

SLACK_KITS = (
    post.SlackPostToolkit,
    scheduled.SlackScheduledToolkit,
    reactions.SlackReactionToolkit,
    files.SlackInlineHistoryToolkit,
    directory.SlackDirectoryToolkit,
    pins.SlackPinToolkit,
    bookmarks.SlackBookmarkToolkit,
    canvases.SlackCanvasToolkit,
    lists.SlackListToolkit,
    home_tab.SlackHomeTabToolkit,
    search.SlackSearchToolkit,
    files.SlackFileToolkit,
)

#: Arguments named as ids whose shape Slack does not document. No shape is checked for them.
NO_DOCUMENTED_SHAPE = {
    "bookmark_id",
    "parent_bookmark_id",
    "entity_id",
    "quip_section_id",
    "section_id",
    "item_id",
    "item_ids",
    "duplicated_item_id",
    "parent_item_id",
    "row_id",
    "column_id",
    "export_job_id",
    "scheduled_message_id",
}


def _cards(kit):
    # A card needs no connection, so a toolkit built without one lists it.
    return kit.__new__(kit).get_tool_definitions()


def _tools():
    return [(kit, card) for kit in (*SLACK_KITS, LoopbackToolkit) for card in _cards(kit)]


@pytest.mark.parametrize(("kit", "card"), _tools(), ids=lambda value: value["name"] if isinstance(value, dict) else "")
def test_a_card_requires_exactly_the_arguments_its_tool_cannot_do_without(kit, card):
    parameters = inspect.signature(getattr(kit, card["name"])).parameters
    needed = {
        name
        for name, parameter in parameters.items()
        if name != "self" and parameter.default is parameter.empty and parameter.kind is not parameter.VAR_KEYWORD
    }
    schema = card["input_params"]

    assert set(schema.get("required", [])) == needed
    assert set(schema.get("properties", {})) <= set(parameters)


def _valid(name):
    if "user" in name:
        return "U0123456789"
    if "message" in name:
        return "1758123456.123456"
    if "chat" in name:
        return "C0123456789"
    return "F0123456789"


def _id_arguments():
    for kit, card in _tools():
        if kit is LoopbackToolkit:
            continue
        properties = card["input_params"].get("properties", {})
        for name in properties:
            if re.search(r"_ids?\Z", name) and name not in NO_DOCUMENTED_SHAPE:
                yield pytest.param(kit, card, name, id=f"{card['name']}.{name}")


@pytest.mark.parametrize(("kit", "card", "name"), list(_id_arguments()))
def test_every_slack_id_argument_is_refused_by_its_shape_before_anything_else(kit, card, name):
    required = card["input_params"].get("required", [])
    arguments = {other: _valid(other) if re.search(r"_ids?\Z", other) else "x" for other in required}
    arguments[name] = ["null"] if name.endswith("s") else "null"
    # The toolkit holds no connection and no metadata, so any other check would raise here.
    result = json.loads(asyncio.run(getattr(kit.__new__(kit), card["name"])(**arguments)))

    assert result["ok"] is False
    assert result["error"].endswith("_id_malformed") and name in result["detail"]


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"chat_id": "null", "reply_to_message_id": None, "text": "hi"}, "chat_id_malformed"),
        ({"reply_to_message_id": "null"}, "message_id_malformed"),
        ({"visible_only_to_user_id": "null"}, "user_id_malformed"),
        ({"visible_only_to_user_id": "C0123456789"}, "user_id_malformed"),
        ({"user_ids": ["U0123456789", "null"]}, "user_id_malformed"),
        ({"chat_ids": ["C0123456789", "#general"]}, "chat_id_malformed"),
        ({"canvas_id": "null"}, "canvas_id_malformed"),
        ({"copy_from_list_id": "f0123"}, "list_id_malformed"),
        ({"file_id": "F0123 or F0456"}, "file_id_malformed"),
    ],
)
def test_a_malformed_id_gets_the_code_of_its_kind(arguments, code):
    assert slack_id_error(arguments)[0] == code


def test_well_formed_and_absent_ids_pass():
    arguments = {
        "chat_id": "D0123456789",
        "chat_ids": ["G0123456789", "U0123456789"],
        "visible_only_to_user_id": "W0123456789",
        "user_ids": ["U0123456789"],
        "message_id": "1758123456.123456",
        "reply_to_message_id": None,
        "file_id": "",
        "canvas_id": "F0123456789",
        "item_id": "null",
        "text": "null",
    }

    assert slack_id_error(arguments) is None
