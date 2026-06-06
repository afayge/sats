from __future__ import annotations

from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema


def factor_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="factor.list",
            description="列出 SATS 已注册因子，可按 zoo/theme/universe 过滤。",
            category="factor",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "zoo": {"type": "string"},
                    "theme": {"type": "string"},
                    "universe": {"type": "string"},
                    "json": {"type": "boolean"},
                }
            ),
            executor=_factor_list,
        ),
        AgentToolSpec(
            name="factor.show",
            description="显示一个因子的 metadata。",
            category="factor",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema({"factor_id": {"type": "string"}, "json": {"type": "boolean"}}, ["factor_id"]),
            executor=_factor_show,
        ),
        AgentToolSpec(
            name="factor.analyze",
            description="分析单个因子的 IC 和分组收益，并可生成报告。",
            category="factor",
            side_effect="write_artifact",
            timeout=180,
            input_schema=object_schema(
                {
                    "factor": {"type": "string"},
                    "trade_date": {"type": "string"},
                    "lookback_days": {"type": "integer"},
                    "horizon": {"type": "integer"},
                    "groups": {"type": "integer"},
                    "symbols": {"type": "string"},
                    "json": {"type": "boolean"},
                    "noreport": {"type": "boolean"},
                },
                ["factor"],
            ),
            executor=_factor_analyze,
        ),
        AgentToolSpec(
            name="factor.pick",
            description="用一个或多个因子/画像选 TopN 股票，可写入 screening_results。",
            category="factor",
            side_effect="write_artifact",
            timeout=180,
            input_schema=object_schema(
                {
                    "factors": {"type": "string"},
                    "profile": {"type": "string"},
                    "trade_date": {"type": "string"},
                    "lookback_days": {"type": "integer"},
                    "top": {"type": "integer"},
                    "neutralize": {"type": "string"},
                    "weight": {"type": "string"},
                    "write_screening": {"type": "boolean"},
                    "json": {"type": "boolean"},
                    "noreport": {"type": "boolean"},
                }
            ),
            executor=_factor_pick,
        ),
        AgentToolSpec(
            name="factor.ml",
            description="执行 SATS factor ml 子命令：status/setup/train/evaluate/predict。",
            category="factor_ml",
            side_effect="long_running",
            timeout=600,
            input_schema=object_schema(
                {
                    "command": {"type": "string", "enum": ["status", "setup", "train", "evaluate", "predict"]},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
                ["command"],
            ),
            executor=_factor_ml,
        ),
    ]


def _factor_list(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    argv = ["factor", "list"]
    _add(argv, "--zoo", arguments.get("zoo"))
    _add(argv, "--theme", arguments.get("theme"))
    _add(argv, "--universe", arguments.get("universe"))
    _flag(argv, "--json", arguments.get("json"))
    return _run(context, argv, "因子列表")


def _factor_show(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    argv = ["factor", "show", str(arguments.get("factor_id") or "")]
    _flag(argv, "--json", arguments.get("json"))
    return _run(context, argv, "因子")


def _factor_analyze(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    argv = ["factor", "analyze", "--factor", str(arguments.get("factor") or "")]
    _add(argv, "--trade-date", arguments.get("trade_date"))
    _add(argv, "--lookback-days", arguments.get("lookback_days"))
    _add(argv, "--horizon", arguments.get("horizon"))
    _add(argv, "--groups", arguments.get("groups"))
    _add(argv, "--symbols", arguments.get("symbols"))
    _flag(argv, "--json", arguments.get("json"))
    _flag(argv, "--noreport", arguments.get("noreport"))
    return _run(context, argv, "因子分析")


def _factor_pick(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    argv = ["factor", "pick"]
    _add(argv, "--factors", arguments.get("factors"))
    _add(argv, "--profile", arguments.get("profile"))
    _add(argv, "--trade-date", arguments.get("trade_date"))
    _add(argv, "--lookback-days", arguments.get("lookback_days"))
    _add(argv, "--top", arguments.get("top"))
    _add(argv, "--neutralize", arguments.get("neutralize"))
    _add(argv, "--weight", arguments.get("weight"))
    _flag(argv, "--write-screening", arguments.get("write_screening"))
    _flag(argv, "--json", arguments.get("json"))
    _flag(argv, "--noreport", arguments.get("noreport"))
    return _run(context, argv, "因子选股")


def _factor_ml(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    args = [str(item) for item in arguments.get("args") or [] if str(item).strip()]
    return _run(context, ["factor", "ml", str(arguments.get("command") or ""), *args], "因子 ML")


def _run(context: AgentToolContext, argv: list[str], name: str) -> AgentToolResult:
    result = context.command_runner.run(argv)
    return AgentToolResult(
        status="done" if result.returncode == 0 else "error",
        content=result.output,
        payload={"argv": list(result.argv), "returncode": result.returncode, "status": result.status},
        data_names=(name,),
    )


def _add(argv: list[str], flag: str, value: Any) -> None:
    if value not in (None, ""):
        argv.extend([flag, str(value)])


def _flag(argv: list[str], flag: str, enabled: Any) -> None:
    if bool(enabled):
        argv.append(flag)
