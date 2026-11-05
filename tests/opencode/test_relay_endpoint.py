"""The relay endpoint answers each hook from the turn's scope, and decides nothing else.

Unit checks of ``_answer``, with no OpenCode and no network.
"""

import asyncio

from belle_agent.backend import ToolVerdict, TurnScope
from belle_agent.integrations.opencode.relay import _answer


class _Tools:
    def __init__(self, verdict: ToolVerdict) -> None:
        self._verdict = verdict
        self.calls: list = []

    async def authorize(self, call) -> ToolVerdict:
        self.calls.append(call)
        return self._verdict


def _run(coro):
    return asyncio.run(coro)


def test_context_delivers_the_system_additions_and_edits_no_roster():
    scope = TurnScope(system_additions=("Answer in French.",), tools={"shell": "deny", "post_message": "ask"})
    event = {"sessionID": "s", "tools": ["shell", "read", "post_message"]}

    answer = _answer("context", event, ("C1", scope), _Tools(ToolVerdict.allow()), _run)

    # The roster is the session ruleset's in both adapter modes. A second filter here, by name, once
    # made `write: deny` remove `write` behind the plugin and nothing without it.
    assert answer == {"system_append": ["Answer in French."]}


def test_context_without_a_turn_answers_nothing():
    assert _answer("context", {"sessionID": "s", "tools": ["read"]}, None, _Tools(ToolVerdict.allow()), _run) == {}


def test_execute_before_denies_or_rewrites_from_the_tool_host():
    scope = TurnScope()
    event = {"sessionID": "s", "tool": "post_message", "input": {"text": "hi"}, "id": "c1"}

    deny = _answer("execute.before", event, ("C1", scope), _Tools(ToolVerdict.deny("no")), _run)
    assert deny == {"deny": "no"}

    rewrite = _answer("execute.before", event, ("C1", scope), _Tools(ToolVerdict.rewrite({"text": "clean"})), _run)
    assert rewrite == {"input": {"text": "clean"}}

    allow = _answer("execute.before", event, ("C1", scope), _Tools(ToolVerdict.allow()), _run)
    assert allow == {}


def test_prompt_and_http_response_pass_through():
    scope = TurnScope(system_additions=("x",))
    for hook in ("prompt", "evaluate", "http.response"):
        assert _answer(hook, {"sessionID": "s"}, ("C1", scope), _Tools(ToolVerdict.allow()), _run) == {}
