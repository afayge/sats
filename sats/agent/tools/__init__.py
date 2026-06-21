from __future__ import annotations

from sats.agent.tools.base import AgentToolContext, AgentToolRegistry, AgentToolResult, AgentToolSpec
from sats.agent.tools.chat_tools import chat_tool_specs
from sats.agent.tools.command_tools import command_tool_specs
from sats.agent.tools.data_tools import data_tool_specs
from sats.agent.tools.factor_tools import factor_tool_specs
from sats.agent.tools.research_tools import research_tool_specs
from sats.agent.tools.trade_tools import trade_tool_specs
from sats.agent.tools.web_tools import web_tool_specs
from sats.agent.tools.workflow_tools import workflow_tool_specs


def build_default_tool_registry() -> AgentToolRegistry:
    registry = AgentToolRegistry()
    for spec in (
        *chat_tool_specs(),
        *data_tool_specs(),
        *research_tool_specs(),
        *factor_tool_specs(),
        *web_tool_specs(),
        *workflow_tool_specs(),
        *command_tool_specs(),
        *trade_tool_specs(),
    ):
        registry.register(spec)
    return registry


__all__ = [
    "AgentToolContext",
    "AgentToolRegistry",
    "AgentToolResult",
    "AgentToolSpec",
    "build_default_tool_registry",
]
