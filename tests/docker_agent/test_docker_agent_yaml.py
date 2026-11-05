"""The rule of Belle's agent YAML: every hook it declares relays to Belle the same way, and fails closed.

The YAML the harness serves is held to the same rule, as an operator's own YAML must be.
"""

import pytest
from agent_yaml import harness, shipped

from belle_agent.integrations.docker_agent import hooks


def _entries(agent: dict) -> dict[str, list[dict]]:
    """Each hook event of an agent, with its hook entries. Entries under a matcher are listed with it."""
    return {
        event: [hook for item in items for hook in item.get("hooks", [item])]
        for event, items in (agent.get("hooks") or {}).items()
    }


@pytest.mark.parametrize("config", [shipped(), harness()], ids=["shipped", "harness"])
def test_every_hook_relays_to_belle_with_one_command_and_blocks_when_the_relay_fails(config):
    agents = config["agents"]
    entries = [
        (event, hook) for agent in agents.values() for event, hooks_ in _entries(agent).items() for hook in hooks_
    ]

    assert {hook["type"] for _, hook in entries} == {"command"}
    assert len({hook["command"] for _, hook in entries}) == 1
    # Under sh whatever the shell, and a relay that fails gives its reason rather than its command line.
    command = entries[0][1]["command"]
    assert command.startswith("sh -c '") and "exit 2" in command
    assert [event for event, hook in entries if hook.get("on_error") != "block"] == []
    for name, agent in agents.items():
        relayed = set(_entries(agent))
        routed = {hooks.BEFORE_AGENT_RUN} if agent.get("routing") else set()
        assert relayed == hooks.RELAYED | {hooks.USER_STEERING} | routed, name
        # A tool guard that matches every tool, so Docker Agent's own tools are guarded too.
        assert [item.get("matcher") for item in agent["hooks"][hooks.TOOL_GUARD]] == ["*"], name


@pytest.mark.parametrize("config", [shipped(), harness()], ids=["shipped", "harness"])
def test_the_agents_reach_no_file_or_shell_and_leave_belles_tool_traffic_as_it_is(config):
    for name, agent in config["agents"].items():
        assert [toolset["type"] for toolset in agent["toolsets"]] == ["mcp"], name
        # Docker Agent's default secret redaction would rewrite what Belle's tools receive and return.
        assert agent["redact_secrets"] is False, name
