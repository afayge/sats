from __future__ import annotations

from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema
from sats.portfolio import DailyPortfolioAgent, PortfolioConfig
from sats.natural_task import (
    ScreenedAnalysisMode,
    build_screened_natural_task_spec,
    choose_screened_analysis_mode,
    default_limit_for_mode,
    extract_candidate_limit,
    requested_screened_analysis_mode,
)
from sats.screening.registry import get_rule, list_rules
from sats.stock_question import extract_trade_date


DEFAULT_SCREENED_WORKFLOW_RULE = "ma_volume_relative_strength"


def workflow_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="workflow.daily_portfolio",
            description=(
                "运行 Portfolio 分时 10 选 5 组合工作流。paper 模式尾盘自动模拟买入、"
                "早盘/盘中条件卖出并生成日报；live 模式只创建逐笔待确认委托，不直接调用 QMT。"
            ),
            category="workflow",
            side_effect="write_db",
            timeout=600,
            input_schema=object_schema(
                {
                    "phase": {
                        "type": "string",
                        "enum": [
                            "morning",
                            "morning-final",
                            "review",
                            "afternoon-scan",
                            "afternoon-buy",
                            "plan-finalize",
                            "recheck",
                            "report",
                            "scan",
                            "close",
                        ],
                    },
                    "trading_mode": {"type": "string", "enum": ["paper", "live"]},
                    "trade_date": {"type": "string"},
                    "llm_enabled": {"type": "boolean"},
                }
            ),
            executor=_daily_portfolio,
        ),
        AgentToolSpec(
            name="workflow.screened_stock_analysis",
            description=(
                "固定工作流：解析筛选规则/交易日，读取或执行筛选结果，按 batch/group/per_stock "
                "选择模式分析筛选股票集合，并产出次日交易计划素材。"
            ),
            category="workflow",
            side_effect="write_db",
            timeout=240,
            input_schema=object_schema(
                {
                    "message": {"type": "string"},
                    "rule": {"type": "string"},
                    "trade_date": {"type": "string"},
                    "candidate_limit": {"type": "integer"},
                    "analysis_mode": {"type": "string", "enum": ["auto", "batch", "group", "per_stock"]},
                    "run_screen": {"type": "boolean"},
                }
            ),
            executor=_screened_stock_analysis,
        )
    ]


def is_daily_portfolio_request(text: str) -> bool:
    source = str(text or "").lower()
    return any(
        term in source
        for term in (
            "10选5",
            "10 选 5",
            "十选五",
            "组合agent",
            "组合 agent",
            "自动模拟交易",
            "每日组合",
            "盘中动态选股",
            "daily portfolio",
        )
    )


def _daily_portfolio(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    mode = str(arguments.get("trading_mode") or "paper").strip().lower()
    result = DailyPortfolioAgent(
        settings=context.settings,
        storage=context.storage,
    ).run(
        phase=str(arguments.get("phase") or "afternoon-buy"),
        trade_date=str(arguments.get("trade_date") or "").strip() or None,
        config=PortfolioConfig(
            trading_mode=mode,
            llm_enabled=bool(arguments.get("llm_enabled", True)),
        ),
    )
    payload = result.to_dict()
    selected = [row for row in payload["candidates"] if row.get("selected")]
    lines = [
        result.message,
        (
            f"大盘评分 {result.market_regime.score:.1f}，"
            f"仓位上限 {result.market_regime.exposure_limit:.0%}"
        ),
    ]
    for row in selected:
        lines.append(
            f"- {row['ts_code']} {row['name']} 评分 {row['total_score']:.2f} "
            f"止损 {row['stop_loss']:.2f} 止盈 {row['take_profit_1']:.2f}/{row['take_profit_2']:.2f}"
        )
    return AgentToolResult(
        status="done" if result.status in {"done", "partial", "skipped"} else "error",
        content="\n".join(lines),
        payload={"daily_portfolio": payload},
        data_names=("盘中组合", "模拟交易" if mode == "paper" else "实盘待确认委托"),
    )


def is_screened_stock_analysis_request(text: str) -> bool:
    source = str(text or "")
    if not source:
        return False
    has_screening = any(term in source for term in ("筛选", "筛出", "筛出来", "筛选结果", "候选", "入选"))
    has_analysis = any(term in source for term in ("分析", "评价", "排序", "计划", "明天", "次日", "关注", "交易"))
    return has_screening and has_analysis


def infer_screening_rule(text: str) -> str:
    source = str(text or "")
    lowered = source.lower()
    for rule_name in sorted(list_rules(), key=len, reverse=True):
        normalized = str(rule_name or "")
        variants = {normalized, normalized.replace("_", "-"), normalized.replace("-", "_")}
        if any(variant and variant.lower() in lowered for variant in variants):
            return get_rule(normalized).name
    return DEFAULT_SCREENED_WORKFLOW_RULE


def _screened_stock_analysis(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    message = str(arguments.get("message") or context.message or "")
    rule_name = _canonical_rule(str(arguments.get("rule") or "") or infer_screening_rule(message))
    trade_date = str(arguments.get("trade_date") or "").strip() or (extract_trade_date(message) or "")
    requested_mode = _analysis_mode(arguments.get("analysis_mode")) or requested_screened_analysis_mode(message)
    requested_limit = _candidate_limit(arguments.get("candidate_limit"), message)
    dry_run = bool(getattr(context.policy, "dry_run", False))
    rows = _screening_rows(context, trade_date=trade_date, rule_name=rule_name)
    screen_output = ""
    if not rows and trade_date and not dry_run and bool(arguments.get("run_screen", True)):
        screen = context.command_runner.run(
            ["screen", "--trade-date", trade_date, "--rule", rule_name, "--no-select-watchlist"],
            timeout=getattr(context.policy, "command_timeout", 120),
        )
        screen_output = screen.output
        rows = _screening_rows(context, trade_date=trade_date, rule_name=rule_name)
    mode, reason = choose_screened_analysis_mode(message, candidate_count=len(rows) if rows else None, requested=requested_mode)
    limit = default_limit_for_mode(mode, requested_limit)
    selected = rows[:limit]
    analysis_output = "" if dry_run else _run_analysis(context, selected, mode=mode, trade_date=trade_date)
    payload = {
        "workflow": "screened_stock_analysis_plan",
        "rule": rule_name,
        "trade_date": trade_date,
        "candidate_count": len(rows),
        "candidate_limit": limit,
        "analysis_mode": mode.value,
        "analysis_mode_reason": reason,
        "selected_symbols": [str(row.get("ts_code") or "") for row in selected],
        "selected_rows": selected,
        "dry_run": dry_run,
        "screen_ran": bool(screen_output),
        "screen_output": screen_output,
        "analysis_output": analysis_output,
        "verification": _verification(rows=rows, selected=selected, limit=limit, mode=mode, analysis_output=analysis_output, dry_run=dry_run),
        "natural_task": build_screened_natural_task_spec(
            message,
            candidate_count=len(rows) if rows else None,
            candidate_limit=limit,
        ).to_dict(),
    }
    return AgentToolResult(
        status="done",
        content=_format_screened_workflow_result(payload),
        payload=payload,
        data_names=("筛选股票分析工作流", "交易计划素材"),
    )


def _screening_rows(context: AgentToolContext, *, trade_date: str, rule_name: str) -> list[dict[str, Any]]:
    rows = context.storage.list_screening_stocks(
        trade_date=trade_date or None,
        rule_name=rule_name or None,
        passed=True,
    )
    return [dict(row) for row in rows]


def _run_analysis(
    context: AgentToolContext,
    rows: list[dict[str, Any]],
    *,
    mode: ScreenedAnalysisMode,
    trade_date: str,
) -> str:
    symbols = [str(row.get("ts_code") or "").strip() for row in rows if str(row.get("ts_code") or "").strip()]
    if not symbols:
        return ""
    outputs: list[str] = []
    if mode == ScreenedAnalysisMode.PER_STOCK:
        for symbol in symbols:
            outputs.append(_run_analyze_command(context, [symbol], trade_date=trade_date))
    else:
        outputs.append(_run_analyze_command(context, symbols, trade_date=trade_date))
    return "\n\n".join(part for part in outputs if part.strip())


def _run_analyze_command(context: AgentToolContext, symbols: list[str], *, trade_date: str) -> str:
    argv = ["analyze", "--stocks", ",".join(symbols), "--signals", "all", "--noreport"]
    if trade_date:
        argv.extend(["--trade-date", trade_date])
    result = context.command_runner.run(argv, timeout=getattr(context.policy, "command_timeout", 120))
    return result.output


def _format_screened_workflow_result(payload: dict[str, Any]) -> str:
    lines = [
        "SATS screened_stock_analysis_plan:",
        f"- rule: {payload['rule']}",
        f"- trade_date: {payload['trade_date'] or 'latest/unspecified'}",
        f"- candidate_count: {payload['candidate_count']}",
        f"- candidate_limit: {payload['candidate_limit']}",
        f"- analysis_mode: {payload['analysis_mode']} ({payload['analysis_mode_reason']})",
        f"- dry_run: {payload['dry_run']}",
    ]
    selected = payload.get("selected_rows") or []
    if selected:
        lines.append("- selected:")
        for index, row in enumerate(selected, start=1):
            name = str(row.get("name") or "")
            score = row.get("score")
            labels = ", ".join(str(item) for item in row.get("matched_labels") or [])
            suffix = f" score={score:g}" if isinstance(score, (int, float)) else ""
            label_text = f" labels={labels}" if labels else ""
            lines.append(f"  {index}. {row.get('ts_code')} {name}{suffix}{label_text}".rstrip())
    else:
        lines.append("- selected: none")
    if payload.get("analysis_output"):
        lines.extend(["", "analysis_output:", str(payload["analysis_output"]).strip()])
    lines.extend(
        [
            "",
            "trade_plan_policy:",
            "- 输出次日观察/买入触发/止损失效/仓位建议，不执行实盘下单。",
            "- 必须基于筛选结果和 SATS 分析输出，不得编造行情数据。",
            "- 不构成投资建议。",
        ]
    )
    return "\n".join(lines)


def _verification(
    *,
    rows: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    limit: int,
    mode: ScreenedAnalysisMode,
    analysis_output: str,
    dry_run: bool,
) -> list[dict[str, str]]:
    return [
        {
            "name": "screening_context_resolved",
            "status": "passed" if rows else "failed",
            "message": f"{len(rows)} candidates",
        },
        {
            "name": "candidate_limit_applied",
            "status": "passed" if len(selected) <= limit else "failed",
            "message": f"{len(selected)} selected <= {limit}",
        },
        {"name": "analysis_mode_selected", "status": "passed", "message": mode.value},
        {
            "name": "trade_plan_generated",
            "status": "skipped" if dry_run else ("passed" if analysis_output.strip() else "failed"),
            "message": "dry-run" if dry_run else "analysis command output available",
        },
    ]


def _candidate_limit(value: Any, message: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    return number if number > 0 else extract_candidate_limit(message, default=0)


def _analysis_mode(value: Any) -> ScreenedAnalysisMode | None:
    try:
        return ScreenedAnalysisMode(str(value or "").strip())
    except ValueError:
        return None


def _canonical_rule(value: str) -> str:
    try:
        return get_rule(value).name
    except Exception:
        return DEFAULT_SCREENED_WORKFLOW_RULE
