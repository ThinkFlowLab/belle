"""The Matrix channel must not offer tools that cannot reach its Gateway helper."""

from pathlib import Path

from jiuwenswarm.extensions.channel_contributions import contributed_spec_for, unregister_channel_spec
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from belle_agent.integrations.jiuwenswarm.matrix_channel import create_matrix_channels

EXTENSION = Path(__file__).resolve().parents[2] / "src/belle_agent/integrations/jiuwenswarm/extensions/matrix"


async def test_matrix_extension_registers_channel_without_agent_tools() -> None:
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    try:
        await ExtensionLoader(registry).load_extension(EXTENSION)
        spec = contributed_spec_for("matrix")
        assert (spec.replaces, spec.source, spec.factory) == ("", "belle-agent", create_matrix_channels)
        assert not registry.get_agent_plugins()
    finally:
        unregister_channel_spec("matrix")
