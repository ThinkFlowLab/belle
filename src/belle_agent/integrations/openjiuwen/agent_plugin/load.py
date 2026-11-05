"""Mount Belle resources through OpenJiuwen's agent plugin API."""

from importlib.resources import files
from typing import Any

from openjiuwen.harness.resources.extension_loader import load_plugin_package
from openjiuwen.harness.schema.deep_agent_spec import (
    BuiltinToolSpec,
    RailSpec,
    register_rail_provider,
    register_tool_provider,
)
from openjiuwen.harness.schema.extension_spec import SkillSpec

_RAIL = "belle.agent_plugin.rail"
_TOOL = "belle.agent_plugin.tool"


def _existing(params: dict[str, Any], _context: Any) -> Any:
    return params["instance"]


def plugin_spec(*, rails: tuple[Any, ...] = (), tools: tuple[Any, ...] = (), skills: bool = True):
    """Declare packaged skills and runtime-bound capabilities as one plugin."""
    manifest = files("belle_agent.integrations.openjiuwen.agent_plugin").joinpath("manifest.json")
    spec = load_plugin_package(manifest)
    if skills:
        root = files("belle_agent.skills")
        spec.skills = [
            SkillSpec(dir=str(directory), mode="all")
            for directory in sorted(root.iterdir(), key=lambda entry: entry.name)
            if directory.joinpath("SKILL.md").is_file()
        ]
    if rails:
        register_rail_provider(_RAIL, _existing)
        spec.rails.extend(RailSpec(type=_RAIL, params={"instance": rail}) for rail in rails)
    if tools:
        register_tool_provider(_TOOL, _existing)
        spec.tools.extend(BuiltinToolSpec(type=_TOOL, params={"instance": tool}) for tool in tools)
    return spec


async def load_plugin(agent: Any, *, rails: tuple[Any, ...] = (), tools: tuple[Any, ...] = (), skills: bool = True):
    """Mount Belle's plugin; return its OpenJiuwen unload record."""
    return await agent.load_plugin_spec(plugin_spec(rails=rails, tools=tools, skills=skills))
