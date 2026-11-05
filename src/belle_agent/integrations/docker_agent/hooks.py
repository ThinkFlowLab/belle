"""Answer the relay hooks of Belle's agent YAML from the turn's settled scope.

Every relay hook posts its event here and Docker Agent applies the answer.
The answer translates the scope the core settled for the turn and the tool
host's verdict on a call. It decides nothing of its own.
"""

from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.backend import VERDICT_ALLOW, VERDICT_ASK, ToolCall, ToolVerdict, TurnScope
from belle_agent.capability_catalog import scope_allows
from belle_agent.scopes import LEVEL_ASK

USER_PROMPT_SUBMIT = "user_prompt_submit"
#: Fires when a turn takes the messages steered into it. Its context replaces the prompt's for the rest of the turn.
USER_STEERING = "user_steering_messages_submit"
#: The hooks that put the turn's system additions into the model calls that follow them.
CONTEXT_HOOKS = frozenset({USER_PROMPT_SUBMIT, USER_STEERING})
TOOL_GUARD = "tool_guard"
SKILL_CONTENT_GUARD = "skill_content_guard"
BEFORE_AGENT_RUN = "before_agent_run"
#: The hooks that must answer for a call, a skill or an agent to go on.
GUARDS = frozenset({TOOL_GUARD, SKILL_CONTENT_GUARD, BEFORE_AGENT_RUN})
#: The hooks Belle's agent YAML must relay. ``before_agent_run`` is relayed only where the YAML declares experts.
RELAYED = frozenset({USER_PROMPT_SUBMIT, TOOL_GUARD, SKILL_CONTENT_GUARD})
#: Docker Agent's tool that hands a task to a subagent. Its ``agent`` argument names the subagent.
TRANSFER_TOOL = "transfer_task"

NO_TURN = "No Belle turn runs in this session."

#: The turn that runs in a session: its conversation and its scope.
Turn = tuple[str, TurnScope]
#: Asks the tool host to decide one call, and waits for the verdict.
Authorize = Callable[[ToolCall], ToolVerdict]


def _block(reason: str) -> dict[str, Any]:
    return {"decision": "block", "reason": reason}


def _tool_decision(decision: str, reason: str) -> dict[str, Any]:
    output = {"hook_event_name": TOOL_GUARD, "permission_decision": decision, "permission_decision_reason": reason}
    return {"hook_specific_output": output}


def answer(
    event: Mapping[str, Any], turn: Turn | None, authorize: Authorize, core_name: Callable[[str], str], entry_agent: str
) -> dict[str, Any]:
    """The answer to one hook event. ``turn`` is the one that runs in the event's session, or ``None``.

    ``core_name`` gives the name the core knows a tool by, from the name Docker Agent advertises it under.
    ``entry_agent`` is the agent each turn starts on.

    - ``user_prompt_submit`` puts the turn's system additions into each model call of the turn. Docker
      Agent stores none of them. ``user_steering_messages_submit`` puts them back after a steered message.
    - ``tool_guard`` refuses a call the tool host denies, and a ``transfer_task`` to a subagent the
      scope does not allow. A tool the scope sets to ``ask`` asks. A question of a tool's own, such as
      a post that widens its audience, is left to the tool server, which holds the call.
    - ``skill_content_guard`` lets a skill load only where the scope allows it.
    - ``before_agent_run`` routes the turn from the entry agent to the scope's expert.

    A guard outside a Belle turn refuses.
    """
    name = event.get("hook_event_name")
    if turn is None:
        if name == TOOL_GUARD:
            return _tool_decision("deny", NO_TURN)
        return _block(NO_TURN) if name in GUARDS else {}
    conversation, scope = turn
    if name in CONTEXT_HOOKS and scope.system_additions:
        context = "\n\n".join(scope.system_additions)
        return {"hook_specific_output": {"hook_event_name": name, "additional_context": context}}
    if name == TOOL_GUARD:
        tool = core_name(str(event.get("tool_name") or ""))
        tool_input = event.get("tool_input")
        arguments: Mapping[str, Any] = tool_input if isinstance(tool_input, Mapping) else {}
        agent = str(arguments.get("agent") or "")
        if tool == TRANSFER_TOOL and scope.subagents_available is not None and agent not in scope.subagents_available:
            return _tool_decision(
                "deny", f"The subagent {agent or '(none named)'} is not available in this conversation."
            )
        verdict = authorize(ToolCall(conversation, tool, arguments, str(event.get("tool_use_id") or "")))
        if verdict.action == VERDICT_ALLOW or (verdict.action == VERDICT_ASK and scope.tools.get(tool) != LEVEL_ASK):
            return {}
        if verdict.action == VERDICT_ASK:
            return _tool_decision("ask", verdict.message)
        return _tool_decision("deny", verdict.message or f"{tool} cannot run here.")
    if name == SKILL_CONTENT_GUARD:
        skill = str((event.get("skill") or {}).get("name") or "")
        if scope_allows(skill, scope.skills_available, scope.skills_unavailable):
            return {"continue": True}
        return _block(f"The skill {skill} is not available in this conversation.")
    if name == BEFORE_AGENT_RUN and scope.expert and event.get("agent_name") == entry_agent:
        return {"hook_specific_output": {"transition": {"action": "route", "agent": scope.expert}}}
    return {}
