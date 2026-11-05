"""Bind Belle tool definitions to OpenJiuwen tools."""

from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard


def bind_tool_definitions(toolkit: Any) -> list[Tool]:
    return [
        LocalFunction(card=ToolCard(**definition), func=getattr(toolkit, definition["name"]))
        for definition in toolkit.get_tool_definitions()
    ]


class OpenJiuwenToolMixin:
    def get_tools(self) -> list[Tool]:
        return bind_tool_definitions(self)
