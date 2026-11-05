"""Belle's Mattermost tools on JiuwenSwarm's agent: mounted for Mattermost requests, run on the turn's server."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "mattermost"))

from transport import TOKEN, FakeRest

from belle_agent.integrations.jiuwenswarm import mattermost_tools
from belle_agent.integrations.jiuwenswarm.mattermost_tools import (
    MattermostRuntimeTools,
    mount_mattermost_tools,
)
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.settings import TOKEN_ENV
from belle_agent.platforms.mattermost.toolkit import WIDENING_TOOLS

CHANNEL = "c" * 26
ALICE = "a" * 26


class Agent:
    """The agent's tool registry, as the runtime tools manager uses it."""

    def __init__(self) -> None:
        self.cards: dict[str, object] = {}
        self.rails: list[object] = []
        self.ability_manager = SimpleNamespace(get=self.cards.get)

    async def load_plugin_spec(self, spec):
        tools = [entry.params["instance"] for entry in spec.tools]
        for tool in tools:
            self.cards[tool.card.name] = tool.card
        self.rails.extend(entry.params["instance"] for entry in spec.rails)
        return tools

    async def unload_extension(self, record) -> None:
        for tool in record:
            self.cards.pop(tool.card.name, None)


@pytest.fixture
def servers(monkeypatch):
    """One fake server for each address. The manager's clients reach it and no network."""
    found: dict[str, FakeRest] = {}

    def client(url: str, token: str) -> MattermostClient:
        # The bot of http://a.invalid is aaa..., of http://b.invalid bbb...
        rest = found.setdefault(url, FakeRest(bot=url.removeprefix("http://")[0] * 26))
        return MattermostClient(url, token, transport=rest.transport())

    monkeypatch.setattr(mattermost_tools, "MattermostClient", client)
    return found


def context(channel_id: str = "mattermost", **metadata) -> SimpleNamespace:
    return SimpleNamespace(channel_id=channel_id, metadata={
        "mattermost_channel_id": CHANNEL, "mattermost_channel_type": "D", "mattermost_user_id": ALICE, **metadata,
    })


async def test_tools_are_mounted_for_mattermost_requests_alone_with_their_confirmation(servers, monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    agent, request = Agent(), context("slack")
    manager = MattermostRuntimeTools(agent, request, {"platforms": {"mattermost": {"url": "http://one.invalid"}}})
    await manager.refresh()
    assert not agent.cards and not manager.rails()

    request.channel_id = "mattermost"
    await manager.refresh()
    await manager.refresh()
    mounted = set(agent.cards)
    assert {"post_message", "react_to_message", "read_conversation", "search_messages"} <= mounted
    (rail,) = manager.rails()
    assert agent.rails == [rail]
    assert rail._tool_names == set(WIDENING_TOOLS)

    assert {tool.card.name for tool in await manager.unmount()} == mounted
    assert not agent.cards


async def test_a_tool_of_another_plugin_keeps_its_name(servers, monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    agent = Agent()
    agent.cards["post_message"] = "the operator's post_message"
    await MattermostRuntimeTools(agent, context(), {"platforms": {"mattermost": {"url": "http://one.invalid"}}}).refresh()
    assert agent.cards["post_message"] == "the operator's post_message"
    assert "react_to_message" in agent.cards


async def test_a_call_runs_on_the_server_of_its_turn_as_its_bot(servers, monkeypatch) -> None:
    monkeypatch.setenv("MM_A_TOKEN", TOKEN)
    monkeypatch.setenv("MM_B_TOKEN", TOKEN)
    config = {"platforms": {"mattermost": {"servers": [
        {"name": "a", "url": "http://a.invalid", "bot_token_env": "MM_A_TOKEN"},
        {"name": "b", "url": "http://b.invalid", "bot_token_env": "MM_B_TOKEN"},
    ]}}}
    agent, request = Agent(), context(mattermost_server="b", mattermost_message_id="m" * 26)
    manager = MattermostRuntimeTools(agent, request, config)
    await manager.refresh()
    answer = json.loads(await manager._toolkit.react_to_message(emoji="tada"))
    assert answer["ok"], answer
    a, b = servers["http://a.invalid"], servers["http://b.invalid"]
    assert not a.calls("POST", "/reactions")
    (reaction,) = b.calls("POST", "/reactions")
    assert reaction.body["user_id"] == "b" * 26  # The bot of server b, learned from b.


async def test_a_call_whose_turn_names_no_configured_server_is_refused(servers, monkeypatch) -> None:
    # The servers are started by the channel's host, never here, so the turn's server is all that routes a call.
    monkeypatch.setenv("MM_A_TOKEN", TOKEN)
    monkeypatch.setenv("MM_B_TOKEN", TOKEN)
    config = {"platforms": {"mattermost": {"servers": [
        {"name": "a", "url": "http://a.invalid", "bot_token_env": "MM_A_TOKEN"},
        {"name": "b", "url": "http://b.invalid", "bot_token_env": "MM_B_TOKEN"},
    ]}}}
    manager = MattermostRuntimeTools(Agent(), context(mattermost_server="c", mattermost_message_id="m" * 26), config)
    await manager.refresh()
    answer = json.loads(await manager._toolkit.react_to_message(emoji="tada"))
    assert answer["error"] == "mattermost_server_unknown" and "server c" in answer["detail"]
    assert not any(server.calls("POST", "/reactions") for server in servers.values())


async def test_a_section_that_is_off_mounts_nothing_and_the_agents_plugins_still_load(tmp_path, monkeypatch) -> None:
    path = tmp_path / "belle.yaml"
    path.write_text("platforms:\n  mattermost:\n    url: http://one.invalid\n", encoding="utf-8")
    monkeypatch.setenv("BELLE_AGENT_CONFIG_FILE", str(path))
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    registered: list[object] = []
    services = SimpleNamespace(agent=Agent(), runtime_context=context(), register_runtime_tools=registered.append)
    assert await mount_mattermost_tools(services) is None
    assert not registered

    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    await mount_mattermost_tools(services)
    assert [type(manager) for manager in registered] == [MattermostRuntimeTools]
