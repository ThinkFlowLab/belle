"""Bind shared content tools to OpenJiuwen's public tool contract."""

from openjiuwen.core.foundation.tool import Tool

from belle_agent.tools.content import ContentToolkit

from ._binding import bind_tool_definitions


def content_tools(toolkit: ContentToolkit) -> tuple[Tool, ...]:
    """Keep shared definitions and methods intact for native permission checks."""
    return tuple(bind_tool_definitions(toolkit))
