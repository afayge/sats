from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema
from sats.data.astock_provider import AStockDataProvider
from sats.data.resolver import current_shanghai_trade_date
from sats.portfolio import DailyPortfolioAgent, PortfolioConfig
from sats.natural_task import (
    ScreenedAnalysisMode,
    build_screened_natural_task_spec,
    choose_screened_analysis_mode,
    default_limit_for_mode,
    extract_candidate_limit,
    requested_screened_analysis_mode,
)
from sats.screening.registry import get_rule, list_rules, rule_metadata
from sats.screening.semantic import evaluate_semantic_inputs, semantic_spec_from_payload
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
                    "semantic_spec": {"type": "object"},
                    "zero_result_policy": {"type": "string", "enum": ["near_miss"]},
                }
            ),
            executor=_screened_stock_analysis,
            metadata={"domain": "workflow", "subject_grain": "security", "output_shape": "ranked_candidates", "enumerates_universe": True, "writes_db": True},
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
    requested_phase = str(arguments.get("phase") or "afternoon-buy").strip().lower()
    agent = DailyPortfolioAgent(
        settings=context.settings,
        storage=context.storage,
    )
    if requested_phase in {"report", "close"}:
        status_payload = agent.status(mode=mode)
        payload = {
            "phase": requested_phase,
            "trade_date": str(arguments.get("trade_date") or "").strip(),
            "trading_mode": mode,
            "report_policy": "Agent 工具只返回组合日报素材；如需落盘报告，请在最终汇总后调用 research.write_report。",
            "status": status_payload,
        }
        lines = [
            "组合日报素材已返回，未生成 Markdown 报告文件。",
            f"mode={mode}",
            f"pending_intents={status_payload.get('pending_intents', 0)}",
        ]
        latest_run = status_payload.get("latest_run") or {}
        if latest_run:
            lines.append(
                "latest_run="
                + " ".join(
                    str(latest_run.get(key) or "")
                    for key in ("trade_date", "phase", "status")
                    if latest_run.get(key)
                )
            )
        return AgentToolResult(
            status="done",
            content="\n".join(lines),
            payload={"daily_portfolio": payload},
            data_names=("盘中组合", "组合日报素材"),
        )

    result = agent.run(
        phase=requested_phase,
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
    if any(term in source for term in ("新增", "增加", "生成", "创建", "新建", "设计")) and any(
        term in source for term in ("筛选规则", "选股规则", "筛选功能", "新决策")
    ):
        return False
    has_screening = any(term in source for term in ("筛选", "筛出", "筛出来", "筛选结果", "选出", "选股", "候选", "入选"))
    has_analysis = any(term in source for term in ("分析", "评价", "排序", "计划", "明天", "次日", "关注", "交易"))
    has_screening_semantics = any(term in source for term in ("趋势", "回踩", "均线", "突破", "放量", "缩量", "低位", "相对强"))
    return has_screening and (has_analysis or has_screening_semantics)


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule_name: str = ""
    confidence: float = 0.0
    reason: str = ""
    uncovered_requirements: tuple[str, ...] = ()


def infer_screening_rule(text: str) -> RuleMatch:
    source = str(text or "")
    lowered = source.lower()
    for rule_name in sorted(list_rules(), key=len, reverse=True):
        normalized = str(rule_name or "")
        variants = {normalized, normalized.replace("_", "-"), normalized.replace("-", "_")}
        if any(variant and variant.lower() in lowered for variant in variants):
            return RuleMatch(get_rule(normalized).name, 1.0, "用户明确指定已注册规则名")
    matches: list[tuple[int, str, list[str]]] = []
    generic_tags = {"趋势", "均线", "突破", "买点", "信号"}
    for rule_name in list_rules():
        metadata = rule_metadata(rule_name)
        tags = [tag for tag in metadata.semantic_tags if str(tag).lower() in lowered]
        score = sum(3 if tag not in generic_tags else 1 for tag in tags)
        if score:
            matches.append((score, rule_name, tags))
    matches.sort(key=lambda item: (-item[0], item[1]))
    if matches and matches[0][0] >= 3 and (len(matches) == 1 or matches[0][0] > matches[1][0]):
        score, rule_name, tags = matches[0]
        return RuleMatch(rule_name, min(0.95, 0.7 + score * 0.05), f"语义标签匹配: {', '.join(tags)}")
    uncovered = tuple(term for term in ("回踩", "关键均线", "缩量") if term in source)
    return RuleMatch("", 0.0, "现有规则没有高置信语义匹配", uncovered)


def _screened_stock_analysis(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    message = str(arguments.get("message") or context.message or "")
    trade_date = str(arguments.get("trade_date") or "").strip() or (extract_trade_date(message) or current_shanghai_trade_date())
    requested_mode = _analysis_mode(arguments.get("analysis_mode")) or requested_screened_analysis_mode(message)
    requested_limit = _candidate_limit(arguments.get("candidate_limit"), message)
    dry_run = bool(getattr(context.policy, "dry_run", False))
    explicit_rule = str(arguments.get("rule") or "").strip()
    inferred = infer_screening_rule(message)
    if explicit_rule:
        canonical = _canonical_rule(explicit_rule)
        lowered = message.lower()
        variants = {canonical, canonical.replace("_", "-"), canonical.replace("-", "_")} if canonical else set()
        user_named = any(variant and variant.lower() in lowered for variant in variants)
        try:
            stored_context = bool(_screening_rows(context, trade_date=trade_date, rule_name=canonical)) if canonical else False
        except Exception:
            stored_context = False
        if canonical and (user_named or stored_context or (inferred.rule_name == canonical and inferred.confidence >= 0.7)):
            reason = "用户明确指定规则名" if user_named else ("复用已存在的筛选结果" if stored_context else inferred.reason)
            confidence = 1.0 if user_named or stored_context else inferred.confidence
            rule_match = RuleMatch(canonical, confidence, reason)
        else:
            rule_match = RuleMatch("", 0.0, "工具提供的规则未被用户文本或高置信语义匹配支持", inferred.uncovered_requirements)
    else:
        rule_match = inferred
    semantic_spec = semantic_spec_from_payload(arguments.get("semantic_spec"), message=message) if (arguments.get("semantic_spec") or not rule_match.rule_name) else None
    if semantic_spec is not None:
        return _run_semantic_screened_analysis(
            context,
            message=message,
            trade_date=trade_date,
            requested_mode=requested_mode,
            requested_limit=requested_limit,
            dry_run=dry_run,
            spec=semantic_spec,
            rule_match=rule_match,
        )
    if not rule_match.rule_name:
        payload = {
            "workflow": "screened_stock_analysis_plan",
            "business_status": "data_unavailable",
            "selection_strategy": "unresolved_semantics",
            "rule": "",
            "trade_date": trade_date,
            "candidate_count": 0,
            "near_misses": [],
            "rule_match": _rule_match_payload(rule_match),
            "message": "现有筛选规则与用户语义不匹配，且当前描述无法生成受支持的临时规则。",
        }
        return AgentToolResult(status="done", content=_format_screened_workflow_result(payload), payload=payload, data_names=("筛选股票分析工作流",))

    rule_name = rule_match.rule_name
    rows = _screening_rows(context, trade_date=trade_date, rule_name=rule_name)
    screen_result: dict[str, Any] = {"executed": False, "status": "not_run", "returncode": None, "stdout": "", "stderr": ""}
    if not rows and trade_date and not dry_run and bool(arguments.get("run_screen", True)):
        screen = context.command_runner.run(
            ["screen", "--trade-date", trade_date, "--rule", rule_name, "--no-select-watchlist"],
            timeout=getattr(context.policy, "command_timeout", 120),
        )
        screen_result = {
            "executed": True,
            "status": str(getattr(screen, "status", "")),
            "returncode": int(getattr(screen, "returncode", 0)),
            "stdout": str(getattr(screen, "stdout", "")),
            "stderr": str(getattr(screen, "stderr", "")),
        }
        if screen_result["returncode"] != 0:
            payload = {
                "workflow": "screened_stock_analysis_plan",
                "business_status": "execution_error",
                "selection_strategy": "existing_rule",
                "rule": rule_name,
                "trade_date": trade_date,
                "candidate_count": 0,
                "near_misses": [],
                "screen_result": screen_result,
                "rule_match": _rule_match_payload(rule_match),
            }
            return AgentToolResult(status="error", content=_format_screened_workflow_result(payload), payload=payload, data_names=("筛选股票分析工作流",))
        rows = _screening_rows(context, trade_date=trade_date, rule_name=rule_name)
    mode, reason = choose_screened_analysis_mode(message, candidate_count=len(rows) if rows else None, requested=requested_mode)
    limit = default_limit_for_mode(mode, requested_limit)
    selected = rows[:limit]
    near_misses, data_coverage = _existing_near_misses(context, trade_date=trade_date, rule_name=rule_name, limit=10) if not rows else ([], {})
    analysis_output = "" if dry_run else _run_analysis(context, selected, mode=mode, trade_date=trade_date)
    payload = {
        "workflow": "screened_stock_analysis_plan",
        "business_status": "matched" if rows else ("data_unavailable" if data_coverage.get("evaluated_count", 0) == 0 else "zero_results"),
        "selection_strategy": "existing_rule",
        "rule": rule_name,
        "trade_date": trade_date,
        "candidate_count": len(rows),
        "near_miss_count": len(near_misses),
        "near_misses": near_misses,
        "data_coverage": data_coverage,
        "candidate_limit": limit,
        "analysis_mode": mode.value,
        "analysis_mode_reason": reason,
        "selected_symbols": [str(row.get("ts_code") or "") for row in selected],
        "selected_rows": selected,
        "dry_run": dry_run,
        "screen_ran": bool(screen_result["executed"]),
        "screen_output": "\n".join(part for part in (screen_result["stdout"].rstrip(), screen_result["stderr"].rstrip()) if part),
        "screen_result": screen_result,
        "rule_match": _rule_match_payload(rule_match),
        "zero_result_policy": "near_miss",
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


def _run_semantic_screened_analysis(
    context: AgentToolContext,
    *,
    message: str,
    trade_date: str,
    requested_mode: ScreenedAnalysisMode | None,
    requested_limit: int,
    dry_run: bool,
    spec: Any,
    rule_match: RuleMatch,
) -> AgentToolResult:
    if not trade_date:
        payload = {
            "workflow": "screened_stock_analysis_plan",
            "business_status": "data_unavailable",
            "selection_strategy": "ephemeral_spec",
            "rule": spec.generated_rule_name,
            "trade_date": "",
            "candidate_count": 0,
            "near_misses": [],
            "semantic_spec": spec.to_dict(),
            "rule_match": _rule_match_payload(rule_match),
            "message": "临时全市场筛选需要明确交易日。",
        }
        return AgentToolResult(status="done", content=_format_screened_workflow_result(payload), payload=payload, data_names=("自然语义临时筛选",))
    provider = AStockDataProvider(context.settings)
    required_days = max(60, max((int(item.get("window") or 0) + int(item.get("lookback") or 0) for item in spec.conditions), default=60))
    inputs = provider.load_all_screening_inputs(trade_date, storage=context.storage, trade_days=required_days, rule_name=None)
    if not inputs:
        payload = {
            "workflow": "screened_stock_analysis_plan",
            "business_status": "data_unavailable",
            "selection_strategy": "ephemeral_spec",
            "rule": spec.generated_rule_name,
            "trade_date": trade_date,
            "candidate_count": 0,
            "near_misses": [],
            "semantic_spec": spec.to_dict(),
            "data_coverage": {"input_count": 0, "current_trade_date_count": 0, "data_issue_count": 0, "sources": {}},
            "rule_match": _rule_match_payload(rule_match),
        }
        return AgentToolResult(status="done", content=_format_screened_workflow_result(payload), payload=payload, data_names=("自然语义临时筛选",))
    evaluated = evaluate_semantic_inputs(inputs, spec, near_miss_limit=10)
    rows = list(evaluated["strict_rows"])
    mode, reason = choose_screened_analysis_mode(message, candidate_count=len(rows) if rows else None, requested=requested_mode)
    limit = default_limit_for_mode(mode, requested_limit)
    selected = rows[:limit]
    analysis_output = "" if dry_run else _run_analysis(context, selected, mode=mode, trade_date=trade_date)
    payload = {
        "workflow": "screened_stock_analysis_plan",
        "business_status": "matched" if rows else "zero_results",
        "selection_strategy": "ephemeral_spec",
        "rule": spec.generated_rule_name,
        "trade_date": trade_date,
        "candidate_count": len(rows),
        "near_miss_count": len(evaluated["near_misses"]),
        "near_misses": evaluated["near_misses"],
        "data_coverage": evaluated["data_coverage"],
        "candidate_limit": limit,
        "analysis_mode": mode.value,
        "analysis_mode_reason": reason,
        "selected_symbols": [str(row.get("ts_code") or "") for row in selected],
        "selected_rows": selected,
        "dry_run": dry_run,
        "screen_ran": True,
        "screen_result": {"executed": True, "status": "done", "returncode": 0, "stdout": "", "stderr": ""},
        "analysis_output": analysis_output,
        "semantic_spec": spec.to_dict(),
        "spec_hash": spec.spec_hash,
        "rule_match": _rule_match_payload(rule_match),
        "zero_result_policy": "near_miss",
        "verification": _verification(rows=rows, selected=selected, limit=limit, mode=mode, analysis_output=analysis_output, dry_run=dry_run),
        "natural_task": build_screened_natural_task_spec(message, candidate_count=len(rows) if rows else None, candidate_limit=limit).to_dict(),
    }
    remembered_action_id = _remember_semantic_spec(context, payload) if not dry_run else ""
    if remembered_action_id:
        payload["semantic_spec_action_id"] = remembered_action_id
    return AgentToolResult(
        status="done",
        content=_format_screened_workflow_result(payload),
        payload=payload,
        data_names=("自然语义临时筛选", "交易计划素材"),
    )


def _screening_rows(context: AgentToolContext, *, trade_date: str, rule_name: str) -> list[dict[str, Any]]:
    rows = context.storage.list_screening_stocks(
        trade_date=trade_date or None,
        rule_name=rule_name or None,
        passed=True,
    )
    return [dict(row) for row in rows]


def _existing_near_misses(
    context: AgentToolContext,
    *,
    trade_date: str,
    rule_name: str,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_rows = context.storage.list_screening_results(trade_date=trade_date or None, rule_name=rule_name or None, passed=None)
    stock_basic = context.storage.get_stock_basic()
    names: dict[str, str] = {}
    if stock_basic is not None and not stock_basic.empty and {"ts_code", "name"}.issubset(stock_basic.columns):
        names = {str(row["ts_code"]): str(row["name"] or "") for _index, row in stock_basic[["ts_code", "name"]].iterrows()}
    rows: list[dict[str, Any]] = []
    data_issue_count = 0
    current_count = 0
    sources: dict[str, int] = {}
    for raw in raw_rows:
        failed = [str(item) for item in raw.get("failed_conditions") or []]
        metrics = dict(raw.get("metrics") or {})
        latest = str(metrics.get("latest_daily_trade_date") or trade_date or "")
        data_issue = any(item in {"data_window", "daily_trade_date_current"} or item.startswith("daily_rows_") for item in failed)
        if data_issue:
            data_issue_count += 1
        if latest == trade_date:
            current_count += 1
        source = str(metrics.get("data_source") or "unknown")
        sources[source] = sources.get(source, 0) + 1
        rows.append(
            {
                "ts_code": str(raw.get("ts_code") or ""),
                "name": names.get(str(raw.get("ts_code") or ""), ""),
                "score": float(raw.get("score") or 0.0),
                "passed": bool(raw.get("passed")),
                "required_matched_count": max(0, len(raw.get("matched_conditions") or [])),
                "required_failed_count": len(failed),
                "matched_conditions": [str(item) for item in raw.get("matched_conditions") or []],
                "failed_conditions": failed,
                "soft_failed_conditions": [],
                "condition_details": [],
                "latest_trade_date": latest,
                "data_source": source,
                "data_issue": data_issue,
            }
        )
    rows.sort(key=lambda row: (int(row["data_issue"]), int(row["required_failed_count"]), -float(row["score"]), row["ts_code"]))
    coverage = {
        "evaluated_count": len(raw_rows),
        "current_trade_date_count": current_count,
        "data_issue_count": data_issue_count,
        "sources": sources,
    }
    return rows[: max(1, int(limit or 10))], coverage


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
        f"- business_status: {payload.get('business_status') or 'matched'}",
        f"- selection_strategy: {payload.get('selection_strategy') or 'existing_rule'}",
        f"- rule: {payload.get('rule') or 'none'}",
        f"- trade_date: {payload.get('trade_date') or 'latest/unspecified'}",
        f"- candidate_count: {payload.get('candidate_count', 0)}",
    ]
    if "candidate_limit" in payload:
        lines.append(f"- candidate_limit: {payload.get('candidate_limit')}")
    if payload.get("analysis_mode"):
        lines.append(f"- analysis_mode: {payload.get('analysis_mode')} ({payload.get('analysis_mode_reason') or ''})")
    if "dry_run" in payload:
        lines.append(f"- dry_run: {payload.get('dry_run')}")
    if payload.get("semantic_spec"):
        semantic_spec = payload["semantic_spec"]
        lines.append(f"- spec_hash: {semantic_spec.get('spec_hash') or payload.get('spec_hash') or ''}")
        assumptions = semantic_spec.get("assumptions") or []
        if assumptions:
            lines.append("- assumptions:")
            lines.extend(f"  - {item}" for item in assumptions)
    if payload.get("message"):
        lines.append(f"- message: {payload.get('message')}")
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
    near_misses = payload.get("near_misses") or []
    if near_misses:
        lines.append("- near_misses (hard conditions unchanged):")
        for index, row in enumerate(near_misses, start=1):
            failed = ", ".join(str(item) for item in row.get("failed_conditions") or []) or "none"
            lines.append(
                f"  {index}. {row.get('ts_code')} {row.get('name') or ''} score={float(row.get('score') or 0):g} "
                f"failed={failed} data_date={row.get('latest_trade_date') or '-'}"
            )
    coverage = payload.get("data_coverage") or {}
    if coverage:
        lines.append(
            "- data_coverage: "
            f"evaluated={coverage.get('evaluated_count', coverage.get('input_count', 0))} "
            f"current={coverage.get('current_trade_date_count', 0)} data_issues={coverage.get('data_issue_count', 0)}"
        )
    screen_result = payload.get("screen_result") or {}
    if screen_result.get("executed") and int(screen_result.get("returncode") or 0) != 0:
        lines.append(f"- execution_error: returncode={screen_result.get('returncode')} stderr={screen_result.get('stderr') or ''}")
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
        return ""


def _rule_match_payload(match: RuleMatch) -> dict[str, Any]:
    return {
        "rule_name": match.rule_name,
        "confidence": match.confidence,
        "reason": match.reason,
        "uncovered_requirements": list(match.uncovered_requirements),
    }


def _remember_semantic_spec(context: AgentToolContext, payload: dict[str, Any]) -> str:
    store = context.store
    if store is None or not hasattr(store, "create_pending_action"):
        return ""
    action_payload = {
        "semantic_spec": dict(payload.get("semantic_spec") or {}),
        "execution": {
            "business_status": payload.get("business_status"),
            "trade_date": payload.get("trade_date"),
            "candidate_count": payload.get("candidate_count", 0),
            "near_miss_count": payload.get("near_miss_count", 0),
            "data_coverage": payload.get("data_coverage") or {},
        },
    }
    existing = store.list_pending_actions(
        session_id=str(context.session_id or "agent"),
        action_type="semantic_screen_spec",
        status="pending",
        limit=1,
    )
    if existing:
        action_id = str(existing[0].get("action_id") or "")
        store.update_pending_action_payload(
            action_id,
            payload=action_payload,
            title=str((payload.get("semantic_spec") or {}).get("goal") or "自然语义临时筛选"),
            status="pending",
        )
        return action_id
    return store.create_pending_action(
        session_id=str(context.session_id or "agent"),
        turn_id=str(context.turn_id or ""),
        action_type="semantic_screen_spec",
        title=str((payload.get("semantic_spec") or {}).get("goal") or "自然语义临时筛选"),
        payload=action_payload,
    )
