from __future__ import annotations

from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok


SATS_COMMANDS = (
    "init",
    "screen",
    "results",
    "result-rules",
    "quote",
    "period-change",
    "analyze",
    "analyze-dsa",
    "dsa",
    "deep-analysis",
    "serenity-screen",
    "trading-committee",
    "analyze-chan",
    "chan-kb",
    "discover",
    "web",
    "model",
    "memory",
    "history",
    "knowledge",
    "indicators",
    "factor",
    "skills",
    "watchlist",
    "monitor",
    "monitor-display",
    "schedule",
    "qmt",
    "serve",
)

RECURSIVE_SATS_COMMANDS = ("agent", "chat")


def command_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="sats_command.catalog",
            description="列出 Agent 可通过 argv runner 调用的全部非递归 SATS CLI 命令。",
            category="command",
            side_effect="readonly",
            timeout=5,
            input_schema=object_schema(),
            executor=_catalog,
        ),
        AgentToolSpec(
            name="sats_command.run",
            description=(
                "通过 SATS argv runner 执行任一非递归 SATS CLI 命令；不走 shell，禁止递归 agent/chat。"
                f"可用顶层命令：{', '.join(SATS_COMMANDS)}。"
            ),
            category="command",
            side_effect="command",
            timeout=120,
            input_schema=object_schema({"argv": {"type": "array", "items": {"type": "string"}}}, ["argv"]),
            executor=_run,
        ),
    ]


def _catalog(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    return ok("\n".join(SATS_COMMANDS), payload={"commands": list(SATS_COMMANDS)}, data_names=("SATS Commands",))


def _run(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    argv = [str(item) for item in arguments.get("argv") or [] if str(item).strip()]
    if not argv:
        return AgentToolResult(status="error", content="sats_command.run requires argv")
    result = context.command_runner.run(argv)
    return AgentToolResult(
        status="done" if result.returncode == 0 else "error",
        content=result.output,
        payload={"argv": list(result.argv), "returncode": result.returncode, "status": result.status},
        data_names=("SATS Command",),
    )
