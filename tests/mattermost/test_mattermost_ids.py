"""Every tool argument that holds a Mattermost id is refused, with its code and form, unless it has that form."""

import asyncio
import json

import pytest

from belle_agent.platforms.mattermost.ids import ID_ARGUMENTS, checks_mattermost_ids, mattermost_id_error
from belle_agent.platforms.slack.tool_context import slack_id_error

VALID = "4xp9fdt77pncbef59f4k1qe83o"
MALFORMED = ["null", "a" * 25, "a" * 27, "4xp9fdt77pncbef59f4k1qe8-o", "#town-square"]


@pytest.mark.parametrize("name", sorted(ID_ARGUMENTS))
@pytest.mark.parametrize("value", MALFORMED)
def test_each_id_argument_refuses_a_value_of_another_form(name, value):
    kind = ID_ARGUMENTS[name]
    code, detail = mattermost_id_error({name: [VALID, value] if name.endswith("s") else value})

    assert code == f"{kind}_id_malformed"
    assert detail == (
        f"{name} is not a Mattermost {kind} id. A Mattermost {kind} id is 26 letters and digits,"
        f" such as {VALID}"
    )


@pytest.mark.parametrize("name", sorted(name for name in ID_ARGUMENTS if name.endswith("s")))
def test_an_empty_list_member_is_refused(name):
    assert mattermost_id_error({name: [VALID, ""]})[0] == f"{ID_ARGUMENTS[name]}_id_malformed"


def test_well_formed_and_absent_ids_pass():
    arguments = {name: [VALID, VALID.upper()] if name.endswith("s") else VALID for name in ID_ARGUMENTS}
    assert mattermost_id_error(arguments) is None
    assert mattermost_id_error({"chat_id": None, "message_id": "", "text": "null"}) is None


def test_a_tool_is_refused_before_it_runs():
    ran = []

    class Kit:
        @checks_mattermost_ids
        async def read(self, chat_id: str, limit: int = 10) -> str:
            ran.append(chat_id)
            return "{}"

    result = json.loads(asyncio.run(Kit().read(chat_id="null")))

    assert result == {"ok": False, "error": "chat_id_malformed", "detail": mattermost_id_error({"chat_id": "null"})[1]}
    assert not ran


@pytest.mark.parametrize("name", sorted(ID_ARGUMENTS))
def test_an_argument_slack_takes_too_is_refused_with_slack_s_code(name):
    value = ["null"] if name.endswith("s") else "null"
    slack = slack_id_error({name: value})
    if slack is None:
        pytest.skip(f"Slack takes no id in {name}")
    assert mattermost_id_error({name: value})[0] == slack[0]
