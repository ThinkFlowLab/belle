"""Complete Slack member lists are required for audience checks."""

import asyncio

import pytest

from belle_agent.platforms.slack.tool_common import IncompleteMembers, conversation_members


def test_member_pages_collect_all_members() -> None:
    calls = []

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if "cursor" not in kwargs:
            return {"members": [" U1 ", ""], "response_metadata": {"next_cursor": "next"}}
        return {"members": ["U2", "U1"]}

    assert asyncio.run(conversation_members(call, "C1", max_pages=2)) == frozenset({"U1", "U2"})
    assert calls[1][1]["cursor"] == "next"


@pytest.mark.parametrize(
    "first_page,limit,reason",
    [
        ({"members": ["U1"], "has_more": True}, None, "missing_cursor"),
        ({"members": ["U1"], "response_metadata": {"next_cursor": "next"}}, 1, "page_limit"),
    ],
)
def test_incomplete_member_pages_are_refused(first_page, limit, reason) -> None:
    async def call(method, **kwargs):
        return first_page

    with pytest.raises(IncompleteMembers, match=reason):
        asyncio.run(conversation_members(call, "C1", max_pages=limit))


def test_repeated_member_cursor_is_refused() -> None:
    calls = 0

    async def call(method, **kwargs):
        nonlocal calls
        calls += 1
        return {"members": ["U1"], "response_metadata": {"next_cursor": "same"}}

    with pytest.raises(IncompleteMembers, match="repeated_cursor"):
        asyncio.run(conversation_members(call, "C1"))
    assert calls == 2
