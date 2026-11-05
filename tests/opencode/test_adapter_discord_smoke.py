"""The OpenCode adapter receives Discord context and can use its mounted tools."""

import pytest
from discord_harness_smoke import approval_through_discord, post_through_discord
from discord_kit import DiscordKit
from opencode_kit import OpenCodeKit

pytestmark = pytest.mark.adapter


async def test_opencode_can_post_through_discord(tmp_path, monkeypatch):
    await post_through_discord(OpenCodeKit(), tmp_path / "tools", DiscordKit(monkeypatch))
    for approved in (True, False):
        await approval_through_discord(
            OpenCodeKit(), tmp_path / f"approval-{approved}", DiscordKit(monkeypatch), approved=approved
        )
