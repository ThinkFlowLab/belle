"""The Docker Agent adapter receives Discord context and can use its mounted tools."""

import pytest
from discord_harness_smoke import approval_through_discord, post_through_discord
from discord_kit import DiscordKit
from docker_agent_kit import DockerAgentKit

pytestmark = pytest.mark.docker_agent_server


async def test_docker_agent_can_post_through_discord(tmp_path, monkeypatch):
    await post_through_discord(DockerAgentKit(), tmp_path / "tools", DiscordKit(monkeypatch))
    for approved in (True, False):
        await approval_through_discord(
            DockerAgentKit(), tmp_path / f"approval-{approved}", DiscordKit(monkeypatch), approved=approved
        )
