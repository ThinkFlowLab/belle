"""The adapter as the first caller of an OpenCode server that has just started.

Run this check alone with OCB_COLDSTART=1, beside an OpenCode container that
has just started, so nothing has asked the server anything before the adapter
does. OpenCode loads a location's plugins when first asked for them, and it
answers that first question before they are loaded. A stand-in run does not
show it: there the first answer is already whole. A provider run does.
"""

import os

import pytest
from conformance_kit import worlds
from opencode_kit import PLUGIN, OpenCodeKit

from belle_agent.host import OUTCOME_POSTED_FINAL

pytestmark = pytest.mark.skipif(
    os.environ.get("OCB_COLDSTART") != "1", reason="needs a server nothing has called: OCB_COLDSTART=1"
)


async def test_the_first_caller_of_a_cold_server_gets_a_turn_through(tmp_path):
    kit = OpenCodeKit()
    async with worlds(kit, tmp_path) as build:
        world = await build()

        # Behind the relay image the plugin is found and its endpoint listens. Beside the plain image neither.
        assert (kit.backend._plugin, kit.backend._relay is not None) == (PLUGIN, PLUGIN)

        await world.send(world.direct("Reply with the single word pong."))

        assert world.outcomes() == [OUTCOME_POSTED_FINAL]
        assert len(world.posts()) == 1
