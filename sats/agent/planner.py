from __future__ import annotations

from dataclasses import replace
import shlex
from datetime import datetime, timedelta
from typing import Any, Callable

from sats.analysis.market_llm_context import DEFAULT_MARKET_DIMENSIONS
from sats.agent.date_policy import agent_today, sanitize_agent_tool_arguments
from sats.agent.models import AgentExecutionPolicy, AgentPlan, AgentStep
from sats.agent.tools import AgentToolRegistry
from sats.chat_reference import is_reference_question
from sats.config import Settings
from sats.llm import ChatLLM, extract_json_object
from sats.screening.rule_composer import is_rule_generation_request, parse_rule_generation_confirmation
from sats.stock_question import extract_stock_symbols, extract_trade_date


SHELL_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "$(", "`"}


def build_agent_plan(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy,
    llm_factory: Callable[..., Any] | None = ChatLLM,
    tool_registry: AgentToolRegistry | None = None,
    policy_message: str | None = None,
    reference_context: Any | None = None,
) -> AgentPlan:
    if llm_factory is not None:
        payload = _llm_plan_payload(
            message,
            settings=settings,
            policy=policy,
            llm_factory=llm_factory,
            tool_registry=tool_registry,
            reference_context=reference_context,
        )
        plan = _plan_from_payload(payload, message=policy_message or message, tool_registry=tool_registry)
        if plan.steps:
            return _augment_plan(plan, message=policy_message or message, reference_context=reference_context)
    return _augment_plan(
        _fallback_plan(policy_message or message, policy=policy, tool_registry=tool_registry, reference_context=reference_context),
        message=policy_message or message,
        reference_context=reference_context,
    )


def _llm_plan_payload(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy,
    llm_factory: Callable[..., Any],
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None,
) -> dict[str, Any]:
    try:
        llm = llm_factory(model_name=getattr(settings, "openai_model", None), profile="default", timeout_seconds=getattr(settings, "llm_timeout_seconds", None))
    except TypeError:
        llm = llm_factory()
    try:
        response = llm.chat(
            _planner_messages(message, policy=policy, tool_registry=tool_registry, reference_context=reference_context),
            timeout=getattr(settings, "llm_timeout_seconds", None),
        )
    except TypeError:
        response = llm.chat(_planner_messages(message, policy=policy, tool_registry=tool_registry, reference_context=reference_context))
    except Exception:
        return {}
    payload = extract_json_object(str(getattr(response, "content", "") or ""))
    return payload if isinstance(payload, dict) else {}


def _planner_messages(
    message: str,
    *,
    policy: AgentExecutionPolicy,
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None = None,
) -> list[dict[str, str]]:
    tools = tool_registry.planner_context() if tool_registry is not None else "[]"
    today = agent_today()
    reference_summary = _reference_context_prompt(message, reference_context)
    return [
        {
            "role": "system",
            "content": (
                "你是 SATS autonomous agent planner，只能输出一个 JSON 对象。"
                "你优先规划 tool step，也可以兼容规划受限 Python、交易意图和最终总结。"
                "市场数据必须由 DuckDB cache 或 AStockDataProvider 经 SATS resolver 获取，不能让 LLM 填价格、成交量、K线或报价。"
                "需要真实行情、财务、资金流、板块、指数、宏观、新闻公告或 TickFlow 盘口/分钟K时，先查看可用工具里的 data_capabilities，再选择对应工具；"
                "如果目录没有对应能力，只能说明缺口，不能编造 provider 接口。"
                f"当前日期是 {today}（Asia/Shanghai）。"
                "用户说“明天/后天/下周/未来几天/未来一周”时，这是 forecast horizon，不是历史 trade_date；"
                "只有用户明确写出 YYYYMMDD 或 YYYY-MM-DD 日期时，才把它作为 trade_date。"
                "trade_date/start_date/end_date 必须使用 YYYYMMDD。"
                "SATS 命令必须输出 argv 数组，不要输出 shell 字符串。"
                "交易步骤只有在 policy auto_trade 允许对应 side 时才可规划；否则规划 dry-run/说明步骤。"
                "普通解释、总结、命令帮助请规划 chat.answer 工具；不要规划 sats_command.run chat。"
                "默认优先使用确定性 research 组件工具："
                "个股问题 -> research.stock_context + research.internal_analysis(kind=indicators)；"
                "大盘问题 -> research.market_context；"
                "选股问题 -> research.discover_opportunities；"
                "缠论问题 -> research.chan_context；"
                "规则生成 -> research.rule_generation。"
                "除非用户明确要求，否则不要默认追加 factor_summary、analyze_signals、knowledge_context 或额外 data.* 取数步骤。"
                "如果用户说上面、上述、刚才、这些、它或它们，并且可用上文上下文提供 symbols，必须把这些 symbols 当作本轮股票输入。"
            ),
        },
        {
            "role": "user",
            "content": (
                "按 JSON 输出字段：objective, success_criteria, assumptions, risk_level, requires_live_trading, steps。"
                "steps 每项字段：step_id, kind(tool|command|python|trade|final), title, tool_name, arguments, command, code, trade, side_effect, requires_confirmation, success_criteria。"
                f"\npolicy={policy.to_dict()}\n可用工具={tools}\n{reference_summary}用户目标：{message}"
            ),
        },
    ]


def _plan_from_payload(payload: dict[str, Any], *, message: str, tool_registry: AgentToolRegistry | None) -> AgentPlan:
    if not payload:
        return AgentPlan(objective=message)
    steps = []
    for index, raw in enumerate(payload.get("steps") if isinstance(payload.get("steps"), list) else []):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in {"tool", "command", "python", "trade", "final"}:
            continue
        command = _clean_command(raw.get("command"))
        tool_name = str(raw.get("tool_name") or "").strip()
        if kind == "tool" and tool_registry is not None and tool_registry.get(tool_name) is None:
            continue
        arguments = dict(raw.get("arguments") or {}) if isinstance(raw.get("arguments"), dict) else {}
        if kind == "tool":
            sanitized = sanitize_agent_tool_arguments(tool_name, arguments, message)
            if sanitized.error:
                continue
            arguments = sanitized.arguments
        steps.append(
            AgentStep(
                step_id=str(raw.get("step_id") or f"step_{index + 1}"),
                kind=kind,
                title=str(raw.get("title") or kind),
                tool_name=tool_name,
                arguments=arguments,
                command=tuple(command),
                code=str(raw.get("code") or ""),
                trade=dict(raw.get("trade") or {}) if isinstance(raw.get("trade"), dict) else {},
                requires_confirmation=bool(raw.get("requires_confirmation", False)),
                side_effect=str(raw.get("side_effect") or "readonly"),
                success_criteria=str(raw.get("success_criteria") or ""),
            )
        )
    return AgentPlan(
        objective=str(payload.get("objective") or message),
        success_criteria=tuple(_string_list(payload.get("success_criteria"))),
        assumptions=tuple(_string_list(payload.get("assumptions"))),
        steps=tuple(steps),
        risk_level=str(payload.get("risk_level") or "medium"),
        requires_live_trading=bool(payload.get("requires_live_trading", False)),
    )


def _fallback_plan(
    message: str,
    *,
    policy: AgentExecutionPolicy,
    tool_registry: AgentToolRegistry | None = None,
    reference_context: Any | None = None,
) -> AgentPlan:
    text = str(message or "").strip()
    lowered = text.lower()
    steps: list[AgentStep] = []
    symbols = extract_stock_symbols(text) or _reference_symbols_for_message(text, reference_context)
    clean_symbols = symbols or ["000001"] if _needs_symbol_default(text) else symbols
    confirmed_rule_name = parse_rule_generation_confirmation(text)
    if confirmed_rule_name is not None:
        steps.append(
            _tool_step(
                "rule_generation_confirm",
                "research.rule_generation",
                "确认生成规则",
                {"message": text, "action": "confirm", "rule_name": confirmed_rule_name},
                side_effect="write_artifact",
            )
        )
    elif is_rule_generation_request(text):
        steps.append(
            _tool_step(
                "rule_generation_plan",
                "research.rule_generation",
                "生成规则计划",
                {"message": text, "action": "plan"},
                side_effect="write_artifact",
            )
        )
    elif _is_trade_request(text):
        side = "sell" if "卖" in text or "sell" in lowered else "buy"
        if clean_symbols:
            steps.append(
                _tool_step(
                    "quote_for_trade",
                    "data.realtime_quotes",
                    "获取交易 quote",
                    {"symbols": clean_symbols, "for_trading": True},
                    side_effect="write_db",
                )
            )
        steps.append(
            _tool_step(
                "trade_intent",
                "trade.submit_intent",
                "校验并执行交易意图",
                {"side": side, "ts_code": (clean_symbols or [""])[0], "reason": text},
                side_effect="live_trade",
            )
        )
    elif "回测" in text or "backtest" in lowered:
        steps.append(
            _tool_step(
                "strategy_draft",
                "research.strategy_draft",
                "生成策略草稿",
                {"request": text, "symbols": clean_symbols},
                side_effect="write_artifact",
            )
        )
        steps.append(
            _tool_step(
                "backtest",
                "research.backtest",
                "运行轻量回测",
                {"request": text, "symbols": clean_symbols},
                side_effect="write_artifact",
            )
        )
    elif "策略" in text or "strategy" in lowered:
        steps.append(_tool_step("strategy_draft", "research.strategy_draft", "生成策略草稿", {"request": text, "symbols": clean_symbols}, side_effect="write_artifact"))
    elif _is_factor_request(text):
        if _is_factor_ml_request(text):
            steps.append(_tool_step("factor_ml", "factor.ml", "运行因子 ML", {"command": _factor_ml_command(text), "args": []}, side_effect="long_running"))
        elif _is_factor_analyze_request(text):
            factor = _factor_id(text)
            if factor:
                steps.append(_tool_step("factor_analyze", "factor.analyze", "运行因子分析", {"factor": factor}, side_effect="write_artifact"))
            else:
                steps.append(_tool_step("factor_list", "factor.list", "查看因子能力", {}, side_effect="readonly"))
        elif any(term in text for term in ("选股", "pick", "top", "前")):
            steps.append(_tool_step("factor_pick", "factor.pick", "运行因子选股", {"profile": _factor_profile(text), "top": _top_n(text)}, side_effect="write_artifact"))
        else:
            steps.append(_tool_step("factor_list", "factor.list", "查看因子能力", {}, side_effect="readonly"))
    elif _is_chan_request(text):
        steps.append(_tool_step("chan_context", "research.chan_context", "获取缠论上下文", {"message": text}, side_effect="readonly"))
        if clean_symbols:
            trade_date = _analysis_trade_date(text, reference_context)
            steps.append(_tool_step("stock_context", "research.stock_context", "获取个股上下文", {"symbols": clean_symbols, "trade_date": trade_date}, side_effect="readonly"))
            steps.append(
                _tool_step(
                    "indicators",
                    "research.internal_analysis",
                    "补充技术指标",
                    {"kind": "indicators", "symbols": clean_symbols, "trade_date": trade_date, "horizons": _market_horizons(text)},
                    side_effect="readonly",
                )
            )
    elif _is_market_analysis_request(text):
        steps.append(_tool_step("market_context", "research.market_context", "获取大盘上下文", _market_context_args(text), side_effect="readonly"))
    elif clean_symbols and _is_stock_analysis_request(text):
        trade_date = _analysis_trade_date(text, reference_context)
        steps.append(_tool_step("stock_context", "research.stock_context", "获取个股上下文", {"symbols": clean_symbols, "trade_date": trade_date}, side_effect="readonly"))
        steps.append(
            _tool_step(
                "indicators",
                "research.internal_analysis",
                "补充技术指标",
                {"kind": "indicators", "symbols": clean_symbols, "trade_date": trade_date, "horizons": _market_horizons(text)},
                side_effect="readonly",
            )
        )
        if _needs_signal_analysis(text):
            steps.append(_analyze_signals_step(clean_symbols, trade_date, text))
        if _is_dsa_request(text):
            steps.append(
                _tool_step(
                    "native_dsa",
                    "research.internal_analysis",
                    "Native DSA 分析",
                    {"kind": "native_dsa", "symbols": clean_symbols, "trade_date": trade_date, "horizons": _market_horizons(text)},
                    side_effect="readonly",
                )
            )
        if _needs_factor_summary(text):
            steps.append(
                _tool_step(
                    "factor_summary",
                    "research.internal_analysis",
                    "补充因子画像",
                    {"kind": "factor_summary", "symbols": clean_symbols, "trade_date": trade_date},
                    side_effect="readonly",
                )
            )
        knowledge_args = _knowledge_context_args(text)
        if knowledge_args.get("collections"):
            steps.append(_tool_step("knowledge_context", "research.knowledge_context", "补充知识库上下文", knowledge_args, side_effect="readonly"))
    elif _is_opportunity_request(text):
        steps.append(_tool_step("discover", "research.discover_opportunities", "运行机会发现", {"query": text, "limit": _top_n(text)}, side_effect="write_artifact"))
        if any(term in text for term in ("报告", "保存", "导出")):
            steps.append(_tool_step("write_report", "research.write_report", "保存报告", {"title": "SATS Agent 机会发现报告"}, side_effect="write_artifact"))
    elif any(term in text for term in ("报价", "quote", "实时")) and clean_symbols:
        steps.append(_tool_step("quotes", "data.realtime_quotes", "获取实时报价", {"symbols": clean_symbols}, side_effect="write_db"))
    elif any(term in text for term in ("指标", "indicators")) and clean_symbols:
        steps.append(_tool_step("indicator_inputs", "data.indicator_inputs", "获取指标输入", {"symbols": clean_symbols, "trade_date": _today()}, side_effect="write_db"))
    elif any(term in text for term in ("k线", "K线", "日线", "daily")) and clean_symbols:
        steps.append(_tool_step("stock_daily", "data.stock_daily", "获取日 K", {"symbols": clean_symbols, "start_date": _date_days_before(_today(), 180), "end_date": _today()}, side_effect="write_db"))
    elif any(term in text for term in ("持仓", "positions")) and ("qmt" in lowered or "账户" in text):
        steps.append(_tool_step("trade_positions", "trade.positions", "查询 QMT 持仓", {}, side_effect="readonly"))
    elif any(term in text for term in ("资金", "资产", "asset")) and ("qmt" in lowered or "账户" in text):
        steps.append(_tool_step("trade_asset", "trade.asset", "查询 QMT 资产", {}, side_effect="readonly"))
    elif _looks_like_sats_command(text):
        steps.append(_tool_step("sats_command", "sats_command.run", "执行 SATS 命令", {"argv": _command_from_text(text)}, side_effect="command"))
    else:
        steps.append(_tool_step("chat", "chat.answer", "普通问答", {"message": text}, side_effect="readonly"))
    steps.append(AgentStep(step_id="final", kind="final", title="总结结果"))
    return AgentPlan(
        objective=text,
        success_criteria=("完成可审计的 SATS agent 执行。",),
        assumptions=("市场数据只接受 SATS resolver 的 DuckDB/provider provenance。",),
        steps=tuple(steps),
        risk_level="high" if policy.auto_trade else "medium",
        requires_live_trading=bool(policy.live_trading),
    )


def _augment_plan(plan: AgentPlan, *, message: str, reference_context: Any | None = None) -> AgentPlan:
    steps = list(plan.steps)
    if not steps:
        return plan
    insert_at = _final_index(steps)
    text = str(message or "")
    if parse_rule_generation_confirmation(text) is not None:
        if not _has_tool(steps, "research.rule_generation"):
            steps.insert(
                insert_at,
                _tool_step(
                    "rule_generation_confirm",
                    "research.rule_generation",
                    "确认生成规则",
                    {"message": text, "action": "confirm", "rule_name": parse_rule_generation_confirmation(text)},
                    side_effect="write_artifact",
                ),
            )
        return replace(plan, steps=tuple(steps))
    if is_rule_generation_request(text):
        if not _has_tool(steps, "research.rule_generation"):
            steps.insert(
                insert_at,
                _tool_step(
                    "rule_generation_plan",
                    "research.rule_generation",
                    "生成规则计划",
                    {"message": text, "action": "plan"},
                    side_effect="write_artifact",
                ),
            )
        return replace(plan, steps=tuple(steps))
    if _is_market_analysis_request(text) and not _has_tool(steps, "research.market_context"):
        steps.insert(insert_at, _tool_step("market_context", "research.market_context", "获取大盘上下文", _market_context_args(text), side_effect="readonly"))
        insert_at += 1
    elif _is_market_analysis_request(text):
        steps = [_with_default_market_dimensions(step) for step in steps]
    symbols = _plan_stock_symbols(steps) or extract_stock_symbols(text) or _reference_symbols_for_message(text, reference_context)
    if _is_chan_request(text):
        if not _has_tool(steps, "research.chan_context"):
            steps.insert(insert_at, _tool_step("chan_context", "research.chan_context", "获取缠论上下文", {"message": text}, side_effect="readonly"))
            insert_at += 1
    if symbols and (_is_stock_analysis_request(text) or _is_chan_request(text)) and not _is_market_analysis_request(text):
        trade_date = _analysis_trade_date(text, reference_context)
        if _is_pure_factor_workflow(text):
            return plan
        if not _has_tool(steps, "research.stock_context"):
            insert_at = _insert_before_factor_or_final(steps)
            steps.insert(insert_at, _tool_step("stock_context", "research.stock_context", "获取个股上下文", {"symbols": symbols, "trade_date": trade_date}, side_effect="readonly"))
        if not _has_internal_analysis_kind(steps, "indicators"):
            insert_at = _insert_before_factor_or_final(steps)
            steps.insert(
                insert_at,
                _tool_step(
                    "indicators",
                    "research.internal_analysis",
                    "补充技术指标",
                    {"kind": "indicators", "symbols": symbols, "trade_date": trade_date, "horizons": _market_horizons(text)},
                    side_effect="readonly",
                ),
            )
        if _needs_signal_analysis(text) and not _has_internal_analysis_kind(steps, "analyze_signals"):
            insert_at = _insert_before_factor_or_final(steps)
            steps.insert(insert_at, _analyze_signals_step(symbols, trade_date, text))
        if _is_dsa_request(text) and not _has_internal_analysis_kind(steps, "native_dsa"):
            insert_at = _insert_before_factor_or_final(steps)
            steps.insert(
                insert_at,
                _tool_step(
                    "native_dsa",
                    "research.internal_analysis",
                    "Native DSA 分析",
                    {"kind": "native_dsa", "symbols": symbols, "trade_date": trade_date, "horizons": _market_horizons(text)},
                    side_effect="readonly",
                ),
            )
        if _needs_factor_summary(text) and not _has_internal_analysis_kind(steps, "factor_summary") and not _has_tool(steps, "factor.pick"):
            insert_at = _final_index(steps)
            steps.insert(
                insert_at,
                _tool_step(
                    "factor_summary",
                    "research.internal_analysis",
                    "补充因子画像",
                    {"kind": "factor_summary", "symbols": symbols, "trade_date": trade_date},
                    side_effect="readonly",
                ),
            )
        knowledge_args = _knowledge_context_args(text)
        if knowledge_args.get("collections") and not _has_tool(steps, "research.knowledge_context"):
            insert_at = _final_index(steps)
            steps.insert(insert_at, _tool_step("knowledge_context", "research.knowledge_context", "补充知识库上下文", knowledge_args, side_effect="readonly"))
    return AgentPlan(
        objective=plan.objective,
        success_criteria=plan.success_criteria,
        assumptions=plan.assumptions,
        steps=tuple(steps),
        risk_level=plan.risk_level,
        requires_live_trading=plan.requires_live_trading,
    )


def _tool_step(step_id: str, tool_name: str, title: str, arguments: dict[str, Any], *, side_effect: str) -> AgentStep:
    return AgentStep(
        step_id=step_id,
        kind="tool",
        title=title,
        tool_name=tool_name,
        arguments=arguments,
        side_effect=side_effect,
    )


def _analyze_signals_step(symbols: list[str], trade_date: str, text: str) -> AgentStep:
    return _tool_step(
        "analyze_signals",
        "research.internal_analysis",
        "Analyze 信号分析",
        {
            "kind": "analyze_signals",
            "symbols": symbols,
            "trade_date": trade_date,
            "signals": _analysis_signals(text),
            "horizons": _market_horizons(text),
        },
        side_effect="readonly",
    )


def _clean_command(value: Any) -> list[str]:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        try:
            parts = shlex.split(str(value or ""))
        except ValueError:
            return []
    if any(part in SHELL_TOKENS or any(token in part for token in ("$(", "`")) for part in parts):
        return []
    return parts


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def _reference_symbols_for_message(message: str, reference_context: Any | None) -> list[str]:
    if reference_context is None or not is_reference_question(message):
        return []
    return _string_list(getattr(reference_context, "symbols", ()) or ())


def _reference_trade_date(reference_context: Any | None) -> str:
    return str(getattr(reference_context, "trade_date", "") or "").strip()


def _reference_context_prompt(message: str, reference_context: Any | None) -> str:
    symbols = _reference_symbols_for_message(message, reference_context)
    if not symbols:
        return ""
    lines = [
        "可用上文上下文：",
        f"- source={getattr(reference_context, 'source', '') or 'output'}",
        f"- data_name={getattr(reference_context, 'data_name', '') or '上条输出'}",
        f"- trade_date={_reference_trade_date(reference_context) or 'none'}",
        f"- symbols={', '.join(symbols)}",
        "- policy=本轮用户引用上文时，必须使用这些 symbols，不要要求用户重复输入股票。",
    ]
    summary = _short_text(getattr(reference_context, "system_message", "") or "", 800)
    if summary:
        lines.extend(["- 上文摘要:", summary])
    return "\n".join(lines) + "\n"


def _short_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


def _is_trade_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("买入", "卖出", "下单", "自动交易", "buy", "sell"))


def _is_factor_request(text: str) -> bool:
    lowered = text.lower()
    return "因子" in text or "factor" in lowered or "barra" in lowered or "alpha101" in lowered or "gtja" in lowered


def _is_factor_analyze_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("ic", "rank_ic", "analyze", "analysis")) or any(term in text for term in ("分析", "分组收益", "评价"))


def _is_factor_ml_request(text: str) -> bool:
    lowered = text.lower()
    return "factor ml" in lowered or "因子模型" in text or "因子ml" in lowered or "机器学习因子" in text


def _factor_ml_command(text: str) -> str:
    lowered = text.lower()
    if "train" in lowered or "训练" in text:
        return "train"
    if "evaluate" in lowered or "评估" in text:
        return "evaluate"
    if "predict" in lowered or "预测" in text:
        return "predict"
    if "setup" in lowered or "初始化" in text:
        return "setup"
    return "status"


def _factor_id(text: str) -> str:
    import re

    patterns = [
        r"\b(alpha[_-]?\d{1,3})\b",
        r"\b(gtja[_-]?\d{1,3})\b",
        r"\b(barra[_-][A-Za-z0-9_]+)\b",
        r"\b([A-Za-z][A-Za-z0-9_]*(?:momentum|value|quality|growth|volatility)[A-Za-z0-9_]*)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).replace("-", "_")
    return ""


def _is_market_analysis_request(text: str) -> bool:
    lowered = text.lower()
    market_terms = ("大盘", "指数", "市场", "上证", "沪指", "深成指", "创业板", "沪深300", "中证500", "科创50", "a股")
    analysis_terms = ("分析", "走势", "预测", "行情", "涨跌", "怎么看", "怎么走", "本周", "下周", "明天")
    return any(term in lowered or term in text for term in market_terms) and any(term in text for term in analysis_terms)


def _is_stock_analysis_request(text: str) -> bool:
    lowered = text.lower()
    if any(term in text for term in ("报价", "实时报价", "持仓", "资金")):
        return False
    return any(term in lowered for term in ("analyze", "analysis", "signal")) or any(term in text for term in ("分析", "走势", "预测", "技术面", "信号", "因子", "缠论", "风险"))


def _is_opportunity_request(text: str) -> bool:
    return any(term in text for term in ("筛选", "上涨", "机会", "推荐", "候选"))


def _is_chan_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("chan",)) or any(term in text for term in ("缠论", "中枢", "背驰", "一买", "二买", "三买"))


def _is_dsa_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("dsa", "daily_stock_analysis")) or any(term in text for term in ("买卖点", "交易策略", "多头趋势", "回踩低吸", "放量突破", "均线金叉"))


def _needs_signal_analysis(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("signal", "analyze")) or any(term in text for term in ("信号", "策略", "买卖点"))


def _needs_factor_summary(text: str) -> bool:
    lowered = text.lower()
    return _is_factor_request(text) or any(term in lowered for term in ("valuation", "fundamental", "pe", "roe")) or any(
        term in text for term in ("估值", "基本面", "财报", "盈利", "ROE", "PE")
    )


def _knowledge_context_args(text: str) -> dict[str, Any]:
    collections: list[str] = []
    lowered = text.lower()
    if any(term in lowered for term in ("valuation", "fundamental", "pe", "roe")) or any(term in text for term in ("估值", "基本面", "财报", "盈利", "ROE", "PE")):
        collections.append("fundamental")
    if any(term in lowered for term in ("risk", "drawdown", "volatility")) or any(term in text for term in ("风险", "回撤", "波动", "适当性")):
        collections.append("risk")
    return {"message": text, "collections": collections}


def _market_horizons(text: str) -> list[str]:
    horizons = []
    if any(term in text for term in ("今天", "今日", "本周", "这周", "当前", "现在")):
        horizons.append("today")
    if "明天" in text:
        horizons.append("tomorrow")
    if "后天" in text:
        horizons.append("day_after_tomorrow")
    if "下周" in text or "未来一周" in text:
        horizons.append("next_week")
    return horizons or ["today"]


def _market_context_args(text: str) -> dict[str, Any]:
    return {"horizons": _market_horizons(text), "dimensions": list(DEFAULT_MARKET_DIMENSIONS)}


def _with_default_market_dimensions(step: AgentStep) -> AgentStep:
    if step.kind != "tool" or step.tool_name != "research.market_context":
        return step
    if step.arguments.get("dimensions"):
        return step
    arguments = dict(step.arguments)
    arguments["dimensions"] = list(DEFAULT_MARKET_DIMENSIONS)
    return replace(step, arguments=arguments)


def _analysis_trade_date(text: str, reference_context: Any | None = None) -> str:
    try:
        return extract_trade_date(text) or _reference_trade_date(reference_context) or agent_today()
    except ValueError:
        return _reference_trade_date(reference_context) or agent_today()


def _analysis_signals(text: str) -> str:
    return _explicit_signal_selection(text) or "short_up"


def _explicit_signal_selection(text: str) -> str:
    import re

    tokens = re.findall(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_-]{1,64})(?![A-Za-z0-9_])", str(text or ""))
    if not tokens:
        return ""
    try:
        from sats.signals.registry import GROUP_ALIASES, list_signal_definitions

        definitions = list_signal_definitions()
        valid = {"all", *GROUP_ALIASES.keys()}
        valid.update(item.signal_id for item in definitions)
        valid.update(item.category for item in definitions)
    except Exception:
        valid = {"all", "short_up", "ma_kline", "kline_graph", "ma_graph", "graph_graph", "chan", "trendline"}
    selected: list[str] = []
    for token in tokens:
        normalized = token.replace("-", "_").lower()
        if normalized in valid and normalized not in selected:
            selected.append(normalized)
    return ",".join(selected)


def _final_index(steps: list[AgentStep]) -> int:
    for index, step in enumerate(steps):
        if step.kind == "final":
            return index
    return len(steps)


def _insert_before_factor_or_final(steps: list[AgentStep]) -> int:
    for index, step in enumerate(steps):
        if step.kind == "tool" and step.tool_name == "research.internal_analysis" and str(step.arguments.get("kind") or "") == "factor_summary":
            return index
        if step.kind == "final":
            return index
    return len(steps)


def _has_tool(steps: list[AgentStep], tool_name: str) -> bool:
    return any(step.kind == "tool" and step.tool_name == tool_name for step in steps)


def _has_internal_analysis_kind(steps: list[AgentStep], kind: str) -> bool:
    return any(step.kind == "tool" and step.tool_name == "research.internal_analysis" and str(step.arguments.get("kind") or "") == kind for step in steps)


def _plan_stock_symbols(steps: list[AgentStep]) -> list[str]:
    symbols: list[str] = []
    for step in steps:
        if step.kind != "tool" or step.tool_name.startswith("factor."):
            continue
        values = step.arguments.get("symbols")
        if isinstance(values, list):
            for item in values:
                symbol = str(item or "").strip()
                if symbol and symbol not in symbols and not _looks_like_index_symbol(symbol):
                    symbols.append(symbol)
    return symbols


def _looks_like_index_symbol(symbol: str) -> bool:
    value = str(symbol or "").upper()
    return value.startswith(("000001.SH", "399001.", "399006.", "399330.", "000300.", "000905.", "000688.", "899050."))


def _is_pure_factor_workflow(text: str) -> bool:
    lowered = text.lower()
    return _is_factor_request(text) and (
        any(term in text for term in ("选股", "分组收益", "评价"))
        or any(term in lowered for term in ("top", "pick", "ic", "rank_ic", "factor ml"))
    )


def _factor_profile(text: str) -> str:
    lowered = text.lower()
    if "价值" in text or "value" in lowered:
        return "value"
    if "成长" in text or "growth" in lowered:
        return "growth"
    if "质量" in text or "quality" in lowered:
        return "quality"
    return "balanced"


def _top_n(text: str) -> int:
    import re

    match = re.search(r"(?:top|前)\s*(\d{1,3})", text, flags=re.IGNORECASE)
    return max(1, min(200, int(match.group(1)))) if match else 5


def _needs_symbol_default(text: str) -> bool:
    return any(term in text for term in ("回测", "策略", "报价", "指标", "K线", "k线", "日线"))


def _looks_like_sats_command(text: str) -> bool:
    lowered = text.lower().strip()
    return lowered.startswith("sats ") or lowered.startswith("/")


def _command_from_text(text: str) -> list[str]:
    raw = str(text or "").strip()
    if raw.startswith("/"):
        raw = raw[1:]
    if raw.lower().startswith("sats "):
        raw = raw[5:]
    return _clean_command(raw)


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _date_days_before(end_date: str, days: int) -> str:
    try:
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        end = datetime.now()
    return (end - timedelta(days=max(1, int(days)))).strftime("%Y%m%d")
