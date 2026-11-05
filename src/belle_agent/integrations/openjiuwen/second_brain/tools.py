"""Expose second-brain actions as OpenJiuwen tools."""

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from belle_agent.second_brain.tools import SecondBrainToolContext, second_brain_actions, second_brain_tool_definitions


def create_second_brain_tools(ctx: SecondBrainToolContext) -> list[Tool]:
    return [
        LocalFunction(card=ToolCard(**definition), func=action)
        for definition, action in zip(second_brain_tool_definitions(), second_brain_actions(ctx), strict=True)
    ]
