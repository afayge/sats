from __future__ import annotations

from dataclasses import replace
import re
import shlex
from datetime import datetime, timedelta
from typing import Any, Callable

from sats.analysis.market_llm_context import DEFAULT_MARKET_DIMENSIONS, resolve_market_dimensions_with_warnings
from sats.agent.date_policy import agent_today, sanitize_agent_tool_arguments
from sats.agent.models import AgentExecutionPolicy, AgentPlan, AgentStep
from sats.agent.tools import AgentToolRegistry
from sats.chat_reference import is_reference_question
from sats.config import Settings
from sats.llm import ChatLLM, build_light_fallback_llm, build_standard_llm, extract_json_object
from sats.minute_periods import extract_minute_periods
from sats.natural_task import build_screened_natural_task_spec, extract_candidate_limit, requested_screened_analysis_mode
from sats.rag.knowledge import infer_stock_collections
from sats.screening.registry import list_rules
from sats.screening.rule_composer import is_rule_generation_request, parse_rule_generation_confirmation
from sats.stock_basic_lookup import load_stock_basic_frame, resolve_stock_mentions
from sats.stock_question import extract_stock_symbols, extract_trade_date
from sats.agent.tools.workflow_tools import (
    infer_screening_rule,
    is_daily_portfolio_request,
    is_screened_stock_analysis_request,
)


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
    planning_message = policy_message or message
    resolved_symbols = _resolve_message_stock_symbols(planning_message, settings=settings, reference_context=reference_context)
    local_plan = _with_planner_model_meta(
        _normalize_chat_answer_steps(
            _augment_plan(
                _fallback_plan(
                    planning_message,
                    policy=policy,
                    tool_registry=tool_registry,
                    reference_context=reference_context,
                    resolved_symbols=resolved_symbols,
                ),
                message=planning_message,
                reference_context=reference_context,
                resolved_symbols=resolved_symbols,
            )
        ),
        model_policy="local",
        model_profile="local",
        model_name="local",
    )
    if llm_factory is not None:
        model_policy = "standard" if _requires_standard_planner(local_plan) else "light"
        payload, model_meta = _llm_plan_payload(
            message,
            settings=settings,
            policy=policy,
            llm_factory=llm_factory,
            tool_registry=tool_registry,
            reference_context=reference_context,
            model_policy=model_policy,
            resolved_symbols=resolved_symbols,
        )
        plan = _plan_from_payload(
            payload,
            message=planning_message,
            tool_registry=tool_registry,
            resolved_symbols=resolved_symbols,
        )
        if plan.steps:
            return _with_planner_model_meta(
                _normalize_chat_answer_steps(
                    _augment_plan(
                        plan,
                        message=planning_message,
                        reference_context=reference_context,
                        resolved_symbols=resolved_symbols,
                    )
                ),
                **model_meta,
            )
        return _with_planner_model_meta(local_plan, **model_meta)
    return local_plan


def _llm_plan_payload(
    message: str,
    *,
    settings: Settings,
    policy: AgentExecutionPolicy,
    llm_factory: Callable[..., Any],
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None,
    model_policy: str,
    resolved_symbols: list[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    model_profile = "default" if model_policy == "standard" else "light"
    model_name = _main_model_name(settings) if model_policy == "standard" else _light_model_name(settings)
    try:
        if model_policy == "standard":
            llm = build_standard_llm(
                llm_factory,
                model_name=_main_model_name(settings),
                timeout_seconds=_llm_timeout_seconds(settings),
            )
        else:
            llm = build_light_fallback_llm(
                llm_factory,
                light_model_name=_light_model_name(settings),
                default_model_name=_main_model_name(settings),
                timeout_seconds=_llm_timeout_seconds(settings),
            )
    except TypeError:
        llm = llm_factory()
    model_meta = {
        "model_policy": model_policy,
        "model_profile": model_profile,
        "model_name": model_name,
    }
    try:
        response = llm.chat(
            _planner_messages(
                message,
                policy=policy,
                tool_registry=tool_registry,
                reference_context=reference_context,
                resolved_symbols=resolved_symbols,
            ),
            timeout=_llm_timeout_seconds(settings),
        )
    except TypeError:
        response = llm.chat(
            _planner_messages(
                message,
                policy=policy,
                tool_registry=tool_registry,
                reference_context=reference_context,
                resolved_symbols=resolved_symbols,
            )
        )
    except Exception:
        return {}, model_meta
    model_meta = {
        "model_policy": model_policy,
        "model_profile": str(getattr(llm, "last_profile", "") or getattr(llm, "profile", "") or model_profile),
        "model_name": str(getattr(llm, "last_model_name", "") or getattr(llm, "model_name", "") or model_name),
    }
    payload = extract_json_object(str(getattr(response, "content", "") or ""))
    return (payload if isinstance(payload, dict) else {}), model_meta


def _planner_messages(
    message: str,
    *,
    policy: AgentExecutionPolicy,
    tool_registry: AgentToolRegistry | None,
    reference_context: Any | None = None,
    resolved_symbols: list[str] | None = None,
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
                "需要真实行情、财务、资金流、板块、指数、宏观、新闻公告或 TickFlow 盘口/分钟K时，先查看可用工具里的 data_capabilities；"
                "已知常规能力可直接选择现有 data/research 工具，不确定 operation 或 dataset 时先调用 data.astock_catalog，再用 data.astock_fetch；"
                "如果目录没有对应能力，只能说明缺口，不能编造 provider 接口。"
                "TickFlow 已覆盖的 A 股主行情优先使用 TickFlow/SATS resolver；Tushare 白名单数据优先用 Tushare 工具；"
                "只有需要 TickFlow/Tushare 未覆盖的数据，或用户明确要求 AkShare/数据字典接口时，才用 AkShare catalog 工具。"
                "AkShare 必须先通过 data.astock_catalog、data.list_akshare_datasets 或 data.describe_akshare_dataset 确认 dataset，"
                "再用 data.astock_fetch 或 data.get_akshare_data 取数；不要编造 AkShare 函数名。"
                "需要公开网页、最新报道、公告线索、社交热榜、社媒舆情或主题发酵证据时，使用 web.search / web.open / web.social_hot / web.hot_mentions；"
                "web 工具只提供公开网络证据，不能替代行情、K线、资金流或财务数据。"
                f"当前日期是 {today}（Asia/Shanghai）。"
                "用户说“明天/后天/下周/未来几天/未来一周”时，这是 forecast horizon，不是历史 trade_date；"
                "只有用户明确写出 YYYYMMDD 或 YYYY-MM-DD 日期时，才把它作为 trade_date。"
                "trade_date/start_date/end_date 必须使用 YYYYMMDD。"
                "SATS 命令必须输出 argv 数组，不要输出 shell 字符串。"
                "交易步骤只有在 policy auto_trade 允许对应 side 时才可规划；否则规划 dry-run/说明步骤。"
                "只有不需要其他工具的单步骤普通解释、总结、命令帮助才规划 chat.answer；不要规划 sats_command.run chat。"
                "chat.answer 不读取前序步骤 observations，计划中已有 research/data/web/factor/workflow 等工具时，"
                "不要再用 chat.answer 汇总“以上数据”，由 final 步骤统一综合。"
                "需要本地知识库时使用 research.knowledge_context；其中 knowledge 只能是真实存在的知识库名称或 ID，"
                "自然语言分析要求应放在 message 中。"
                "默认优先使用确定性 research 组件工具："
                "个股问题 -> research.stock_context + research.internal_analysis(kind=indicators)；"
                "深度个股研究、估值、DCF、投委会或首次覆盖 -> research.deep_stock_analysis；"
                "大盘问题 -> research.market_context；"
                "research.market_context 的 dimensions 只使用 core_indices、market_breadth、limit_sentiment、hot_sectors；"
                "Serenity、供应链卡位/卡脖子/瓶颈筛选，或 AI 科技主题选股 -> research.serenity_screen；"
                "主题相关股票列表、概念股有哪些、列出相关股票或简单信息 -> research.theme_stock_list；"
                "只有用户明确要求短线、上涨潜力、预测走势、明天/未来几天、大概率上涨、机会或候选排序时，选股问题才 -> research.discover_opportunities；"
                "缠论问题 -> research.chan_context；"
                "规则生成 -> research.rule_generation。"
                "除非用户明确要求，否则不要默认追加 factor_summary、analyze_signals、knowledge_context 或额外 data.* 取数步骤。"
                "公司介绍、公司概况、主营业务、业务构成或基本面介绍请求，使用 research.internal_analysis(kind=company_fundamentals)，"
                "该工具不依赖日线行情；不要为这类请求改用 stock_context 或 deep_stock_analysis。"
                "如果用户说上面、上述、刚才、这些、它或它们，并且可用上文上下文提供 symbols，必须把这些 symbols 当作本轮股票输入。"
            ),
        },
        {
            "role": "user",
            "content": (
                "按 JSON 输出字段：objective, success_criteria, assumptions, risk_level, requires_live_trading, steps。"
                "steps 每项字段：step_id, kind(tool|command|python|trade|final), title, tool_name, arguments, command, code, trade, side_effect, requires_confirmation, success_criteria。"
                f"\npolicy={policy.to_dict()}\n已解析股票代码={', '.join(resolved_symbols or []) or 'none'}"
                f"\n可用工具={tools}\n{reference_summary}用户目标：{message}"
            ),
        },
    ]


def _plan_from_payload(
    payload: dict[str, Any],
    *,
    message: str,
    tool_registry: AgentToolRegistry | None,
    resolved_symbols: list[str] | None = None,
) -> AgentPlan:
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
            arguments = _replace_unresolved_step_symbols(arguments, resolved_symbols or [])
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
        natural_task=dict(payload.get("natural_task") or {}) if isinstance(payload.get("natural_task"), dict) else {},
        analysis_mode=str(payload.get("analysis_mode") or ""),
        verification_checks=tuple(dict(item) for item in payload.get("verification_checks") or [] if isinstance(item, dict)),
    )


def _fallback_plan(
    message: str,
    *,
    policy: AgentExecutionPolicy,
    tool_registry: AgentToolRegistry | None = None,
    reference_context: Any | None = None,
    resolved_symbols: list[str] | None = None,
) -> AgentPlan:
    text = str(message or "").strip()
    lowered = text.lower()
    steps: list[AgentStep] = []
    symbols = list(resolved_symbols or ()) or extract_stock_symbols(text) or _reference_symbols_for_message(text, reference_context)
    clean_symbols = symbols or ["000001"] if _needs_symbol_default(text) else symbols
    confirmed_rule_name = parse_rule_generation_confirmation(text)
    natural_task = {}
    analysis_mode = ""
    verification_checks: tuple[dict[str, Any], ...] = ()
    if is_daily_portfolio_request(text):
        phase = (
            "report"
            if any(term in text for term in ("日报", "总结", "收盘总结"))
            else "review"
            if any(term in text for term in ("复核", "收盘复核", "收盘检查"))
            else "afternoon-buy"
        )
        trading_mode = "live" if any(term in lowered for term in ("实盘", "live")) else "paper"
        steps.append(
            _tool_step(
                "daily_portfolio",
                "workflow.daily_portfolio",
                "运行盘中 10 选 5 组合工作流",
                {
                    "phase": phase,
                    "trading_mode": trading_mode,
                    "trade_date": extract_trade_date(text) or "",
                    "llm_enabled": "不使用llm" not in lowered and "no-llm" not in lowered,
                },
                side_effect="write_db",
            )
        )
    elif _is_serenity_screen_request(text):
        steps.append(
            _tool_step(
                "serenity_screen",
                "research.serenity_screen",
                "Serenity AI 卡位筛选",
                _serenity_screen_args(text, clean_symbols),
                side_effect="write_artifact",
            )
        )
    elif is_screened_stock_analysis_request(text):
        spec = build_screened_natural_task_spec(text)
        natural_task = spec.to_dict()
        analysis_mode = spec.analysis_mode.value
        verification_checks = tuple(item.to_dict() for item in spec.verification_checks)
        steps.append(
            _tool_step(
                "screened_stock_analysis_plan",
                "workflow.screened_stock_analysis",
                "筛选股票集合分析工作流",
                {
                    "message": text,
                    "rule": infer_screening_rule(text),
                    "trade_date": extract_trade_date(text) or "",
                    "candidate_limit": extract_candidate_limit(text, default=0),
                    "analysis_mode": requested_screened_analysis_mode(text).value,
                    "run_screen": True,
                },
                side_effect="write_db",
            )
        )
    elif confirmed_rule_name is not None:
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
    elif _is_theme_stock_return_request(text):
        steps.append(_theme_stock_returns_step(text, clean_symbols))
    elif _is_theme_stock_list_request(text):
        steps.append(_theme_stock_list_step(text))
    elif _is_market_analysis_request(text):
        steps.append(_tool_step("market_context", "research.market_context", "获取大盘上下文", _market_context_args(text), side_effect="readonly"))
    elif clean_symbols and _is_company_fundamentals_request(text):
        steps.append(
            _tool_step(
                "company_fundamentals",
                "research.internal_analysis",
                "获取公司介绍、主营业务与基本面",
                {
                    "kind": "company_fundamentals",
                    "symbols": clean_symbols,
                    "trade_date": _analysis_trade_date(text, reference_context),
                },
                side_effect="readonly",
            )
        )
    elif clean_symbols and _is_deep_stock_analysis_request(text):
        trade_date = _analysis_trade_date(text, reference_context)
        steps.append(
            _tool_step(
                "deep_stock_analysis",
                "research.deep_stock_analysis",
                "原生个股深研",
                {
                    "symbols": clean_symbols,
                    "trade_date": trade_date,
                    "phase": "run",
                    "lookback_days": 180,
                    "llm_review": True,
                },
                side_effect="write_artifact",
            )
        )
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
                    {"kind": "factor_summary", "symbols": clean_symbols, "trade_date": trade_date, "profile": _factor_profile(text)},
                    side_effect="readonly",
                )
            )
        knowledge_args = _knowledge_context_args(text)
        if knowledge_args.get("collections"):
            steps.append(_tool_step("knowledge_context", "research.knowledge_context", "补充知识库上下文", knowledge_args, side_effect="readonly"))
    elif _is_opportunity_request(text):
        limit = extract_candidate_limit(text, default=0) or _top_n(text)
        steps.append(_tool_step("discover", "research.discover_opportunities", "运行机会发现", {"query": text, "limit": limit}, side_effect="write_artifact"))
        if any(term in text for term in ("报告", "保存", "导出")):
            steps.append(_tool_step("write_report", "research.write_report", "保存报告", {"title": "SATS Agent 机会发现报告"}, side_effect="write_artifact"))
    elif any(term in text for term in ("报价", "quote", "实时")) and clean_symbols:
        steps.append(_tool_step("quotes", "data.realtime_quotes", "获取实时报价", {"symbols": clean_symbols}, side_effect="write_db"))
    elif any(term in text for term in ("指标", "indicators")) and clean_symbols:
        steps.append(_tool_step("indicator_inputs", "data.indicator_inputs", "获取指标输入", {"symbols": clean_symbols, "trade_date": _today()}, side_effect="write_db"))
    elif any(term in text for term in ("k线", "K线", "日线", "daily")) and clean_symbols:
        steps.append(_tool_step("stock_daily", "data.stock_daily", "获取日 K", {"symbols": clean_symbols, "start_date": _date_days_before(_today(), 180), "end_date": _today()}, side_effect="write_db"))
    elif _is_akshare_data_request(text):
        dataset = _akshare_dataset_id(text)
        if dataset:
            steps.append(_tool_step("akshare_describe", "data.describe_akshare_dataset", "确认 AkShare 数据集", {"dataset": dataset}, side_effect="readonly"))
            steps.append(_tool_step("akshare_data", "data.get_akshare_data", "获取 AkShare 数据", {"dataset": dataset, "params": {}, "limit": _top_n(text)}, side_effect="readonly"))
        else:
            steps.append(
                _tool_step(
                    "akshare_catalog",
                    "data.list_akshare_datasets",
                    "查询 AkShare 数据字典",
                    {"query": _akshare_query(text), "compact": True},
                    side_effect="readonly",
                )
            )
    elif any(term in text for term in ("持仓", "positions")) and ("qmt" in lowered or "账户" in text):
        steps.append(_tool_step("trade_positions", "trade.positions", "查询 QMT 持仓", {}, side_effect="readonly"))
    elif any(term in text for term in ("资金", "资产", "asset")) and ("qmt" in lowered or "账户" in text):
        steps.append(_tool_step("trade_asset", "trade.asset", "查询 QMT 资产", {}, side_effect="readonly"))
    elif _needs_web_evidence(text):
        steps.extend(_web_steps(text))
    elif _looks_like_sats_command(text):
        steps.append(_tool_step("sats_command", "sats_command.run", "执行 SATS 命令", {"argv": _command_from_text(text)}, side_effect="command"))
    else:
        steps.append(_tool_step("chat", "chat.answer", "普通问答", {"message": text}, side_effect="readonly"))
    steps.append(AgentStep(step_id="final", kind="final", title="总结结果"))
    return AgentPlan(
        objective=text,
        success_criteria=tuple(natural_task.get("success_criteria") or ("完成可审计的 SATS agent 执行。",)),
        assumptions=tuple(natural_task.get("assumptions") or ("市场数据只接受 SATS resolver 的 DuckDB/provider provenance。",)),
        steps=tuple(steps),
        risk_level="high" if policy.auto_trade else "medium",
        requires_live_trading=bool(policy.live_trading),
        natural_task=natural_task,
        analysis_mode=analysis_mode,
        verification_checks=verification_checks,
    )


def _requires_standard_planner(plan: AgentPlan) -> bool:
    meaningful = [step for step in plan.steps if step.kind != "final"]
    if not meaningful:
        return False
    return not all(step.kind == "tool" and str(step.tool_name).startswith("chat.") for step in meaningful)


def _normalize_chat_answer_steps(plan: AgentPlan) -> AgentPlan:
    steps = list(plan.steps)
    has_other_work = any(
        step.kind != "final"
        and not (step.kind == "tool" and str(step.tool_name or "") == "chat.answer")
        for step in steps
    )
    if not has_other_work:
        return plan
    filtered = [
        step
        for step in steps
        if not (step.kind == "tool" and str(step.tool_name or "") == "chat.answer")
    ]
    if len(filtered) == len(steps):
        return plan
    if not any(step.kind == "final" for step in filtered):
        filtered.append(AgentStep(step_id="final", kind="final", title="总结结果"))
    return replace(plan, steps=tuple(filtered))


def _with_planner_model_meta(
    plan: AgentPlan,
    *,
    model_policy: str,
    model_profile: str,
    model_name: str,
) -> AgentPlan:
    return replace(
        plan,
        phase="planner",
        model_policy=model_policy,
        model_profile=model_profile,
        model_name=model_name,
    )


def _main_model_name(settings: Settings) -> str:
    return str(getattr(settings, "openai_model", "") or "LLM")


def _light_model_name(settings: Settings) -> str:
    return str(getattr(settings, "light_model_name", "") or getattr(settings, "openai_model", "") or "LLM")


def _llm_timeout_seconds(settings: Settings) -> int | None:
    value = getattr(settings, "llm_timeout_seconds", None)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _augment_plan(
    plan: AgentPlan,
    *,
    message: str,
    reference_context: Any | None = None,
    resolved_symbols: list[str] | None = None,
) -> AgentPlan:
    steps = [_rewrite_invalid_stock_basic_fetch_step(_with_resolved_step_symbols(step, resolved_symbols or [])) for step in plan.steps]
    if not steps:
        return plan
    insert_at = _final_index(steps)
    text = str(message or "")
    steps = [_with_explicit_minute_periods(step, text) for step in steps]
    symbols = (
        list(resolved_symbols or ())
        or _plan_stock_symbols(steps)
        or extract_stock_symbols(text)
        or _reference_symbols_for_message(text, reference_context)
    )
    if _is_serenity_screen_request(text):
        steps = [
            step
            for step in steps
            if step.tool_name not in {"research.discover_opportunities", "workflow.screened_stock_analysis"}
        ]
        if not _has_tool(steps, "research.serenity_screen"):
            steps.insert(
                _final_index(steps),
                _tool_step(
                    "serenity_screen",
                    "research.serenity_screen",
                    "Serenity AI 卡位筛选",
                    _serenity_screen_args(text, symbols),
                    side_effect="write_artifact",
                ),
            )
        return replace(plan, steps=tuple(steps))
    if is_screened_stock_analysis_request(text):
        spec = build_screened_natural_task_spec(text)
        if not _has_tool(steps, "workflow.screened_stock_analysis"):
            steps.insert(
                insert_at,
                _tool_step(
                    "screened_stock_analysis_plan",
                    "workflow.screened_stock_analysis",
                    "筛选股票集合分析工作流",
                    {
                        "message": text,
                        "rule": infer_screening_rule(text),
                        "trade_date": extract_trade_date(text) or "",
                        "candidate_limit": extract_candidate_limit(text, default=0),
                        "analysis_mode": requested_screened_analysis_mode(text).value,
                        "run_screen": True,
                    },
                    side_effect="write_db",
                ),
            )
        return replace(
            plan,
            steps=tuple(steps),
            natural_task=spec.to_dict(),
            analysis_mode=spec.analysis_mode.value,
            verification_checks=tuple(item.to_dict() for item in spec.verification_checks),
        )
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
    if _is_theme_stock_return_request(text):
        return replace(plan, steps=(_theme_stock_returns_step(text, symbols), AgentStep(step_id="final", kind="final", title="总结结果")))
    if _is_theme_stock_list_request(text):
        return replace(plan, steps=(_theme_stock_list_step(text), AgentStep(step_id="final", kind="final", title="总结结果")))
    if symbols and _is_company_fundamentals_request(text):
        company_step = _tool_step(
            "company_fundamentals",
            "research.internal_analysis",
            "获取公司介绍、主营业务与基本面",
            {
                "kind": "company_fundamentals",
                "symbols": symbols,
                "trade_date": _analysis_trade_date(text, reference_context),
            },
            side_effect="readonly",
        )
        return replace(
            plan,
            steps=(company_step, AgentStep(step_id="final", kind="final", title="总结结果")),
        )
    if _is_market_analysis_request(text) and not _has_tool(steps, "research.market_context"):
        steps.insert(insert_at, _tool_step("market_context", "research.market_context", "获取大盘上下文", _market_context_args(text), side_effect="readonly"))
        insert_at += 1
    elif _is_market_analysis_request(text):
        steps = [_with_default_market_dimensions(step) for step in steps]
    symbols = (
        list(resolved_symbols or ())
        or _plan_stock_symbols(steps)
        or extract_stock_symbols(text)
        or _reference_symbols_for_message(text, reference_context)
    )
    if symbols and _needs_hot_sector_context(text) and not _is_market_analysis_request(text) and not _has_tool(steps, "research.market_context"):
        steps.insert(_final_index(steps), _hot_sector_context_step(text))
    if symbols and _is_deep_stock_analysis_request(text):
        trade_date = _analysis_trade_date(text, reference_context)
        if not _has_tool(steps, "research.deep_stock_analysis"):
            steps.insert(
                _final_index(steps),
                _tool_step(
                    "deep_stock_analysis",
                    "research.deep_stock_analysis",
                    "原生个股深研",
                    {
                        "symbols": symbols,
                        "trade_date": trade_date,
                        "phase": "run",
                        "lookback_days": 180,
                        "llm_review": True,
                    },
                    side_effect="write_artifact",
                ),
            )
        return replace(plan, steps=tuple(steps))
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
                    {"kind": "factor_summary", "symbols": symbols, "trade_date": trade_date, "profile": _factor_profile(text)},
                    side_effect="readonly",
                ),
            )
        knowledge_args = _knowledge_context_args(text)
        if knowledge_args.get("collections") and not _has_tool(steps, "research.knowledge_context"):
            insert_at = _final_index(steps)
            steps.insert(insert_at, _tool_step("knowledge_context", "research.knowledge_context", "补充知识库上下文", knowledge_args, side_effect="readonly"))
    if symbols and _needs_web_evidence(text) and not _has_tool(steps, "research.stock_context") and not _is_market_analysis_request(text):
        insert_at = _final_index(steps)
        trade_date = _analysis_trade_date(text, reference_context)
        steps.insert(insert_at, _tool_step("stock_context", "research.stock_context", "获取个股上下文", {"symbols": symbols, "trade_date": trade_date}, side_effect="readonly"))
    if _needs_web_evidence(text):
        insert_at = _final_index(steps)
        for step in _web_steps(text):
            if not _has_tool(steps, step.tool_name):
                steps.insert(insert_at, step)
                insert_at += 1
    return AgentPlan(
        objective=plan.objective,
        success_criteria=plan.success_criteria,
        assumptions=plan.assumptions,
        steps=tuple(_with_explicit_minute_periods(step, text) for step in steps),
        risk_level=plan.risk_level,
        requires_live_trading=plan.requires_live_trading,
        natural_task=plan.natural_task,
        analysis_mode=plan.analysis_mode,
        verification_checks=plan.verification_checks,
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


def _with_explicit_minute_periods(step: AgentStep, text: str) -> AgentStep:
    periods = list(extract_minute_periods(text))
    if not periods or step.kind != "tool":
        return step
    if step.arguments.get("minute_periods"):
        return step
    applies = step.tool_name == "research.stock_context"
    if step.tool_name == "research.internal_analysis":
        applies = str(step.arguments.get("kind") or "") in {"indicators", "analyze_signals", "native_dsa"}
    if not applies:
        return step
    return replace(step, arguments={**step.arguments, "minute_periods": periods})


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


def _hot_sector_context_step(text: str) -> AgentStep:
    return _tool_step(
        "market_hot_sectors",
        "research.market_context",
        "补充热点板块上下文",
        {"horizons": _market_horizons(text), "dimensions": ["hot_sectors"]},
        side_effect="readonly",
    )


def _theme_stock_returns_step(text: str, symbols: list[str] | None = None) -> AgentStep:
    arguments: dict[str, Any] = {
        "query": text,
        "period": _theme_return_period(text),
        "limit": 30,
    }
    theme = _theme_return_peer_theme(text)
    if theme:
        arguments["theme"] = theme
    if symbols:
        arguments["symbols"] = symbols
    return _tool_step(
        "theme_stock_returns",
        "research.theme_stock_returns",
        "解析主题股票池并计算区间涨跌幅",
        arguments,
        side_effect="readonly",
    )


def _theme_stock_list_step(text: str) -> AgentStep:
    return _tool_step(
        "theme_stock_list",
        "research.theme_stock_list",
        "解析主题相关股票池",
        {"query": text, "limit": 50},
        side_effect="readonly",
    )


def _web_steps(text: str) -> list[AgentStep]:
    url_match = re.search(r"https?://[^\s<>()]+", str(text or ""))
    if url_match:
        url = url_match.group(0).rstrip(".,，。；;")
        query = str(text or "").replace(url_match.group(0), " ").strip()
        return [
            _tool_step(
                "web_open",
                "web.open",
                "抓取并检索指定公开网页",
                {"url": url, "query": query},
                side_effect="readonly",
            )
        ]
    if _needs_social_mentions(text):
        return [
            _tool_step(
                "web_hot_mentions",
                "web.hot_mentions",
                "检索社交热榜命中",
                {"keyword": _web_keyword(text), "platforms": _web_platforms(text), "limit": _web_limit(text, default=50)},
                side_effect="readonly",
            )
        ]
    if _needs_social_hot(text):
        return [
            _tool_step(
                "web_social_hot",
                "web.social_hot",
                "获取社交热榜",
                {"platforms": _web_platforms(text), "limit": _web_limit(text, default=20)},
                side_effect="readonly",
            )
        ]
    return [
        _tool_step(
            "web_search",
            "web.search",
            "搜索公开网络证据",
            {
                "query": text,
                "limit": _web_limit(text, default=5),
                "freshness": _web_freshness(text),
                "context_size": _web_context_size(text),
            },
            side_effect="readonly",
        )
    ]


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
    if _is_hot_sector_lookup_request(text):
        return True
    lowered = text.lower()
    market_terms = ("大盘", "指数", "市场", "板块", "行业", "上证", "沪指", "深成指", "创业板", "沪深300", "中证500", "科创50", "a股")
    analysis_terms = ("分析", "走势", "预测", "行情", "涨跌", "怎么看", "怎么走", "本周", "下周", "明天", "有哪些", "哪些", "排名", "排行")
    return any(term in lowered or term in text for term in market_terms) and any(term in text for term in analysis_terms)


def _is_hot_sector_lookup_request(text: str) -> bool:
    value = _compact_theme_request_text(text)
    if not value:
        return False
    if _is_hot_sector_opportunity_request(text):
        return False
    stock_terms = ("相关股票", "相关个股", "相关a股", "概念股", "产业链股票", "题材股", "哪些股票", "哪些个股", "有哪些股票", "有哪些个股")
    if any(term in value for term in stock_terms):
        return False
    hot_terms = ("热点板块", "热门板块", "热点行业", "热门行业", "热点题材", "领涨板块", "领涨行业", "强势板块", "强势行业")
    lookup_terms = ("有哪些", "哪些", "哪个", "排名", "排行", "榜", "领涨", "最强")
    return any(term in value for term in hot_terms) and any(term in value for term in lookup_terms)


def _is_hot_sector_opportunity_request(text: str) -> bool:
    value = _compact_theme_request_text(text)
    hot_terms = ("热点板块", "热门板块", "热点题材", "强势板块", "领涨板块")
    opportunity_terms = ("机会", "上涨潜力", "大概率上涨", "候选", "推荐", "选股", "明天可能涨", "未来几天可能涨")
    return any(term in value for term in hot_terms) and any(term in value for term in opportunity_terms)


def _is_stock_analysis_request(text: str) -> bool:
    lowered = text.lower()
    if any(term in text for term in ("报价", "实时报价", "持仓", "资金")):
        return False
    return (
        any(term in lowered for term in ("analyze", "analysis", "signal", "dsa", "das"))
        or any(term in text for term in ("分析", "走势", "预测", "技术面", "信号", "因子", "缠论", "风险"))
        or bool(_strategy_intents(text))
    )


def _is_company_fundamentals_request(text: str) -> bool:
    lowered = str(text or "").lower()
    company_terms = (
        "company profile",
        "business overview",
        "company fundamentals",
        "公司介绍",
        "公司概况",
        "企业介绍",
        "主营业务",
        "业务介绍",
        "业务构成",
        "基本面介绍",
    )
    return any(term in lowered for term in company_terms)


def _is_deep_stock_analysis_request(text: str) -> bool:
    lowered = text.lower()
    deep_terms = (
        "deep analysis",
        "dcf",
        "valuation",
        "initiation",
        "initiating coverage",
        "ic memo",
    )
    cn_terms = (
        "深度分析",
        "全面分析",
        "个股深研",
        "深研",
        "DCF",
        "估值",
        "投委会",
        "首次覆盖",
        "投研报告",
        "全面基本面",
        "基本面拆解",
        "风险拆解",
    )
    return any(term in lowered for term in deep_terms) or any(term in text for term in cn_terms)


def _is_serenity_screen_request(text: str) -> bool:
    source = str(text or "")
    lowered = source.lower()
    if _has_explicit_non_serenity_rule(source):
        return False
    explicit_terms = (
        "serenity",
        "卡位",
        "卡脖子",
        "供应链卡点",
        "供应链瓶颈",
        "稀缺层",
        "瓶颈筛选",
    )
    if any(term in lowered or term in source for term in explicit_terms):
        return True
    ai_terms = (
        "ai",
        "人工智能",
        "半导体",
        "光通信",
        "光模块",
        "cpo",
        "先进封装",
        "hbm",
        "算力",
        "数据中心",
        "液冷",
        "服务器电源",
        "人形机器人",
        "具身智能",
        "机器人核心部件",
    )
    pick_terms = ("选股", "筛选", "推荐", "找标的", "找股票", "优先研究", "候选")
    return any(term in lowered or term in source for term in ai_terms) and any(
        term in source for term in pick_terms
    )


def _has_explicit_non_serenity_rule(text: str) -> bool:
    lowered = str(text or "").lower()
    for rule_name in list_rules():
        variants = {rule_name.lower(), rule_name.replace("_", "-").lower()}
        if any(variant in lowered for variant in variants):
            return True
    return False


def _serenity_screen_args(text: str, symbols: list[str]) -> dict[str, Any]:
    limit = extract_candidate_limit(text, default=10)
    return {
        "query": text,
        "theme": "",
        "symbols": symbols,
        "trade_date": extract_trade_date(text) or agent_today(),
        "limit": limit,
        "candidate_limit": max(30, limit),
        "lookback_days": 180,
        "llm_review": True,
    }


def _is_opportunity_request(text: str) -> bool:
    return any(term in text for term in ("筛选", "上涨", "机会", "推荐", "候选"))


def _is_chan_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("chan",)) or any(term in text for term in ("缠论", "中枢", "背驰", "一买", "二买", "三买"))


def _is_dsa_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("dsa", "das", "daily_stock_analysis")) or any(term in text for term in ("买卖点", "交易策略")) or bool(_strategy_intents(text))


def _needs_signal_analysis(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("signal", "analyze")) or any(term in text for term in ("信号", "策略", "买卖点")) or bool(_strategy_signals(text))


def _needs_factor_summary(text: str) -> bool:
    lowered = text.lower()
    return _needs_factor_strategy_summary(text) or _is_factor_request(text) or any(term in lowered for term in ("valuation", "fundamental", "pe", "roe")) or any(
        term in text for term in ("估值", "基本面", "财报", "盈利", "ROE", "PE")
    )


def _knowledge_context_args(text: str) -> dict[str, Any]:
    collections: list[str] = []
    lowered = text.lower()
    inferred = infer_stock_collections(text)
    if "price-action" in inferred:
        collections.append("price-action")
    if any(term in lowered for term in ("valuation", "fundamental", "pe", "roe")) or any(term in text for term in ("估值", "基本面", "财报", "盈利", "ROE", "PE")):
        collections.append("fundamental")
    if _needs_factor_strategy_summary(text):
        collections.append("fundamental")
    if _needs_hot_sector_context(text) or _needs_public_strategy_evidence(text):
        collections.extend(["market", "sentiment"])
    if any(term in lowered for term in ("risk", "drawdown", "volatility")) or any(term in text for term in ("风险", "回撤", "波动", "适当性")):
        collections.append("risk")
    return {"message": text, "collections": list(dict.fromkeys(collections))}


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


def _needs_web_evidence(text: str) -> bool:
    return _needs_web_search(text) or _needs_social_hot(text)


def _is_theme_stock_return_request(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or "").strip())
    if not value:
        return False
    has_theme_stock = any(term in value for term in ("相关股票", "相关个股", "相关A股", "概念股", "产业链股票", "题材股"))
    has_peer_theme = bool(_theme_return_peer_theme(value))
    has_period = bool(
        re.search(r"(近|过去|最近|内)?([0-9一二两三四五六七八九十]+)(个)?月", value)
        or "半年" in value
        or re.search(r"([0-9一二两三四五六七八九十]+)(日|天|周|季度|年)", value)
    )
    has_return_metric = any(term in value for term in ("涨跌幅", "涨幅", "跌幅", "收益", "表现"))
    return (has_theme_stock or has_peer_theme) and has_period and has_return_metric


def _is_theme_stock_list_request(text: str) -> bool:
    value = _compact_theme_request_text(text)
    if not value:
        return False
    if _is_hot_sector_lookup_request(text) or _is_hot_sector_opportunity_request(text):
        return False
    has_stock_topic = any(term in value for term in ("相关股票", "相关个股", "相关a股", "概念股", "产业链股票", "题材股"))
    has_list_intent = any(term in value for term in ("有哪些", "列出", "名单", "简单信息", "主营", "行业", "股票池"))
    has_market_subject = any(term in value for term in ("股票", "个股", "a股", "概念", "板块", "行业", "题材", "产业链", "内存", "存储", "芯片", "半导体", "dram", "nand", "flash", "ssd"))
    if not (has_stock_topic or (has_list_intent and has_market_subject)):
        return False
    has_forecast_or_pick = any(term in value for term in ("短线", "上涨潜力", "大概率上涨", "预测", "明天", "未来几天", "未来几日", "机会发现", "候选排序", "推荐候选"))
    if has_forecast_or_pick and not _negates_short_opportunity_prediction(value):
        return False
    return True


def _compact_theme_request_text(text: str) -> str:
    return re.sub(r"[\s的]+", "", str(text or "").strip().lower())


def _negates_short_opportunity_prediction(value: str) -> bool:
    return any(
        phrase in value
        for phrase in (
            "不用进行短期机会预测",
            "不用短期机会预测",
            "不用机会预测",
            "不进行短期机会预测",
            "不做短期机会预测",
            "不要短期机会预测",
            "无需短期机会预测",
            "不用预测",
            "不预测",
            "不要预测",
            "无需预测",
        )
    )


def _theme_return_period(text: str) -> str:
    value = re.sub(r"\s+", "", str(text or "").strip())
    year = re.search(r"([0-9一二两三四五六七八九十]{1,3})年", value)
    if year:
        amount = _small_period_amount(year.group(1))
        if amount:
            return f"{amount}y"
    if "半年" in value or re.search(r"(近|过去|最近|内)?6(个)?月", value) or re.search(r"(近|过去|最近|内)?六(个)?月", value):
        return "6m"
    match = re.search(r"([0-9]{1,3})个?月", value)
    if match:
        return f"{match.group(1)}m"
    return "6m"


def _theme_return_peer_theme(text: str) -> str:
    value = re.sub(r"\s+", "", str(text or "").strip())
    patterns = (
        r"(?:在|于)(?P<theme>[\u4e00-\u9fffA-Za-z0-9+·/.\-]{2,30}?)(?:中|里|里面|板块|行业|概念)",
        r"(?P<theme>[\u4e00-\u9fffA-Za-z0-9+·/.\-]{2,30}?)(?:板块|行业|概念)(?:中|里|里面)?",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        theme = _clean_peer_theme(match.group("theme"))
        if theme:
            return theme
    return ""


def _clean_peer_theme(value: str) -> str:
    theme = str(value or "").strip(" ，,。？?；;：:")
    for prefix in ("为什么", "为何", "请问"):
        if theme.startswith(prefix):
            theme = theme[len(prefix) :]
    for suffix in ("相关", "相关股票", "相关个股", "股票", "个股"):
        if theme.endswith(suffix):
            theme = theme[: -len(suffix)]
    return theme.strip()


def _small_period_amount(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if text == "十":
        return 10
    if text.startswith("十") and len(text) == 2 and text[1] in digits:
        return 10 + digits[text[1]]
    if text.endswith("十") and len(text) == 2 and text[0] in digits:
        return digits[text[0]] * 10
    if "十" in text:
        head, tail = text.split("十", 1)
        if head in digits and tail in digits:
            return digits[head] * 10 + digits[tail]
    return None


def _is_akshare_data_request(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("akshare", "ak share")) or any(term in text for term in ("数据字典", "AkShare接口", "akshare接口"))


def _akshare_dataset_id(text: str) -> str:
    import re

    for token in re.findall(r"\b([a-z][a-z0-9]+(?:_[a-z0-9]+){2,})\b", str(text or ""), flags=re.IGNORECASE):
        lowered = token.lower()
        if lowered.startswith(("stock_", "fund_", "bond_", "futures_", "option_", "macro_", "index_", "news_", "spot_", "fx_", "forex_", "crypto_")):
            return lowered
    return ""


def _akshare_query(text: str) -> str:
    value = str(text or "")
    for token in ("AkShare", "akshare", "ak share", "接口", "数据字典", "查一下", "查询", "看看", "有哪些", "列出", "获取"):
        value = value.replace(token, " ")
    return " ".join(value.split())[:60]


def _needs_web_search(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"https?://[^\s<>()]+", str(text or "")):
        return True
    explicit = ("web", "网页", "网络搜索", "网上", "网络上", "搜索", "查一下", "搜一下")
    public_info = (
        "最新",
        "新闻",
        "公告",
        "报道",
        "消息",
        "公开信息",
        "监管",
        "政策",
        "事件",
        "近况",
        "近期动态",
        "最近发生",
        "目前情况",
    )
    today_with_info = ("今天", "今日")
    if any(term in lowered or term in text for term in explicit):
        return True
    if _is_market_data_only_request(text):
        return False
    if _needs_public_strategy_evidence(text):
        return True
    if any(term in text for term in public_info):
        return True
    return any(term in text for term in today_with_info) and any(term in text for term in ("新闻", "公告", "消息", "热搜", "热榜"))


def _needs_social_hot(text: str) -> bool:
    lowered = text.lower()
    social_terms = (
        "热搜",
        "热榜",
        "社交",
        "社媒",
        "舆情",
        "发酵",
        "微博",
        "知乎",
        "抖音",
        "头条",
        "b站",
        "b 站",
        "bilibili",
        "小红书",
        "雪球",
        "xueqiu",
    )
    return any(term in lowered or term in text for term in social_terms)


def _needs_social_mentions(text: str) -> bool:
    if not _needs_social_hot(text):
        return False
    return bool(_web_keyword(text))


def _web_platforms(text: str) -> list[str]:
    lowered = text.lower()
    platforms = []
    xueqiu_specific = False
    if "雪球热股" in text or "xueqiu_stock" in lowered or "xueqiustock" in lowered:
        platforms.append("xueqiu_stock")
        xueqiu_specific = True
    if "雪球热点" in text or "xueqiu_spot" in lowered or "xueqiuspot" in lowered:
        platforms.append("xueqiu_spot")
        xueqiu_specific = True
    if not xueqiu_specific and ("雪球" in text or "xueqiu" in lowered):
        platforms.extend(["xueqiu_stock", "xueqiu_spot"])
    mapping = (
        ("微博", "weibo"),
        ("weibo", "weibo"),
        ("知乎", "zhihu"),
        ("zhihu", "zhihu"),
        ("百度", "baidu"),
        ("baidu", "baidu"),
        ("抖音", "douyin"),
        ("douyin", "douyin"),
        ("头条", "toutiao"),
        ("toutiao", "toutiao"),
        ("b站", "bilibili"),
        ("b 站", "bilibili"),
        ("bilibili", "bilibili"),
    )
    for token, platform in mapping:
        if (token in lowered or token in text) and platform not in platforms:
            platforms.append(platform)
    return platforms


def _web_keyword(text: str) -> str:
    import re

    value = str(text or "")
    value = re.sub(r"\b(web|search|hot|mentions)\b", " ", value, flags=re.IGNORECASE)
    for token in ("最新", "新闻", "公告", "报道", "消息", "公开信息", "网络搜索", "网上", "网络上", "搜索", "查一下", "搜一下", "社交", "社媒", "热搜", "热榜", "热点", "舆情", "发酵", "雪球热股", "雪球热点", "雪球", "xueqiu_stock", "xueqiu_spot", "xueqiu", "怎么看", "如何看", "看看", "今天", "今日", "是否", "有没有", "吗"):
        value = value.replace(token, " ")
    value = re.sub(r"\s+", " ", value).strip(" ，,。？?：:")
    return value[:40]


def _web_freshness(text: str) -> str:
    if any(term in text for term in ("今天", "今日", "最新", "刚刚")):
        return "d"
    if any(term in text for term in ("本周", "这周", "最近一周")):
        return "w"
    if any(term in text for term in ("本月", "最近一个月")):
        return "m"
    return ""


def _web_context_size(text: str) -> str:
    lowered = str(text or "").lower()
    high_terms = ("深入", "全面", "详细", "深度", "调研", "研究报告", "对比", "比较", "综合分析", "deep research", "in-depth")
    return "high" if any(term in lowered for term in high_terms) else "medium"


def _is_market_data_only_request(text: str) -> bool:
    lowered = str(text or "").lower()
    market_terms = (
        "最新价",
        "当前价格",
        "实时价格",
        "实时行情",
        "股价",
        "收盘价",
        "涨跌幅",
        "成交量",
        "成交额",
        "k线",
        "k 线",
        "资金流",
        "盘口",
        "分钟线",
        "quote",
    )
    public_terms = ("新闻", "公告", "报道", "消息", "政策", "监管", "事件", "公开信息", "近况", "动态")
    return any(term in lowered for term in market_terms) and not any(term in lowered for term in public_terms)


def _web_limit(text: str, *, default: int) -> int:
    import re

    match = re.search(r"(?:top|前|limit|返回)\s*(\d{1,3})", text, flags=re.IGNORECASE)
    if not match:
        return default
    return max(1, min(50, int(match.group(1))))


def _with_default_market_dimensions(step: AgentStep) -> AgentStep:
    if step.kind != "tool" or step.tool_name != "research.market_context":
        return step
    arguments = dict(step.arguments)
    resolved, unsupported = resolve_market_dimensions_with_warnings(arguments.get("dimensions"))
    arguments["dimensions"] = list(dict.fromkeys([*DEFAULT_MARKET_DIMENSIONS, *resolved, *unsupported]))
    return replace(step, arguments=arguments)


def _analysis_trade_date(text: str, reference_context: Any | None = None) -> str:
    try:
        return extract_trade_date(text) or _reference_trade_date(reference_context) or agent_today()
    except ValueError:
        return _reference_trade_date(reference_context) or agent_today()


def _analysis_signals(text: str) -> str:
    return _explicit_signal_selection(text) or _strategy_signals(text) or "short_up"


def _strategy_intents(text: str) -> list[str]:
    lowered = text.lower()
    intents: list[str] = []

    def add(intent: str) -> None:
        if intent not in intents:
            intents.append(intent)

    if any(term in text for term in ("均线金叉", "金蜘蛛", "MA金叉", "ma金叉")) or "ma golden cross" in lowered:
        add("ma-golden-cross")
    if any(term in text for term in ("多头趋势", "多头排列", "强势多头")) or "bull trend" in lowered:
        add("bull-trend")
    if any(term in text for term in ("回踩低吸", "缩量回调", "低吸")):
        add("shrink-pullback")
    if any(term in text for term in ("放量突破", "突破放量", "量价突破")):
        add("volume-breakout")
    if _is_chan_request(text):
        add("chan-theory")
    if any(term in text for term in ("波浪理论", "艾略特", "C浪", "B浪", "c浪", "b浪")) or any(term in lowered for term in ("elliott", "wave")):
        add("elliott-wave")
    if any(term in text for term in ("热点题材", "题材发酵", "题材", "热点")) or "hot theme" in lowered:
        add("hot-theme")
    if "龙头" in text:
        add("dragon-head")
    if "情绪周期" in text:
        add("emotion-cycle")
    if any(term in text for term in ("事件驱动", "公告", "并购", "订单", "政策催化")):
        add("event-driven-detector")
    if any(term in text for term in ("成长品质", "成长质量", "利润质量", "ROE")) or any(term in lowered for term in ("growth quality", "roe")):
        add("growth-quality")
    if any(term in text for term in ("预期重估", "预期差", "估值修复")) or "repricing" in lowered:
        add("expectation-repricing")
    return intents


def _strategy_signals(text: str) -> str:
    signals: list[str] = []

    def add(value: str) -> None:
        if value not in signals:
            signals.append(value)

    intents = set(_strategy_intents(text))
    if "ma-golden-cross" in intents:
        add("ma")
    if intents & {"bull-trend", "shrink-pullback", "volume-breakout"}:
        for value in ("ma", "trendline", "kline"):
            add(value)
    if "chan-theory" in intents:
        add("chan")
    if "elliott-wave" in intents:
        add("wave")
    return ",".join(signals)


def _needs_hot_sector_context(text: str) -> bool:
    return bool(set(_strategy_intents(text)) & {"hot-theme", "dragon-head", "emotion-cycle"})


def _needs_factor_strategy_summary(text: str) -> bool:
    return bool(set(_strategy_intents(text)) & {"growth-quality", "expectation-repricing"})


def _needs_public_strategy_evidence(text: str) -> bool:
    return bool(set(_strategy_intents(text)) & {"event-driven-detector", "expectation-repricing"})


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


def _resolve_message_stock_symbols(message: str, *, settings: Settings, reference_context: Any | None) -> list[str]:
    explicit = extract_stock_symbols(message)
    if not _message_may_contain_stock_name(message):
        return explicit or _reference_symbols_for_message(message, reference_context)
    try:
        stock_basic = load_stock_basic_frame(settings)
        mentioned = resolve_stock_mentions(message, stock_basic)
    except Exception:
        mentioned = []
    symbols = list(dict.fromkeys([*mentioned, *explicit]))
    if not symbols:
        symbols.extend(_reference_symbols_for_message(message, reference_context))
    return symbols


def _message_may_contain_stock_name(message: str) -> bool:
    text = str(message or "")
    return any(
        term in text
        for term in (
            "公司",
            "股票",
            "个股",
            "分析",
            "走势",
            "基本面",
            "财务",
            "估值",
            "主营",
            "业务",
            "公告",
            "新闻",
            "报价",
            "股价",
            "涨幅",
            "涨跌幅",
            "跌幅",
            "收益",
            "表现",
            "排名",
            "垫底",
            "倒数",
            "同行",
            "同业",
            "板块",
            "行业",
            "概念",
            "持仓",
            "买入",
            "卖出",
            "缠论",
            "DSA",
            "DAS",
        )
    )


def _rewrite_invalid_stock_basic_fetch_step(step: AgentStep) -> AgentStep:
    if step.kind != "tool" or step.tool_name != "data.astock_fetch":
        return step
    arguments = dict(step.arguments or {})
    if str(arguments.get("operation") or "").strip() != "astock.stock_basic":
        return step
    params = arguments.get("params") if isinstance(arguments.get("params"), dict) else {}
    lookup: dict[str, Any] = {}
    for key in ("name", "query"):
        value = str(params.get(key) or "").strip()
        if value:
            lookup[key] = value
    symbols = params.get("symbols") if isinstance(params.get("symbols"), list) else []
    symbol = str(params.get("symbol") or "").strip()
    if symbol:
        symbols = [*symbols, symbol]
    if symbols:
        lookup["symbols"] = [str(item) for item in symbols if str(item or "").strip()]
    if arguments.get("limit") not in (None, ""):
        lookup["limit"] = arguments.get("limit")
    if not lookup:
        return step
    return replace(
        step,
        tool_name="data.stock_basic",
        title="安全查询股票基础信息",
        arguments=lookup,
        side_effect="write_db",
    )


def _replace_unresolved_step_symbols(arguments: dict[str, Any], resolved_symbols: list[str]) -> dict[str, Any]:
    result = dict(arguments)
    values = result.get("symbols")
    if not isinstance(values, list) or not values or not resolved_symbols:
        return result
    if any(not _looks_like_stock_symbol(value) for value in values):
        result["symbols"] = list(resolved_symbols)
    return result


def _with_resolved_step_symbols(step: AgentStep, resolved_symbols: list[str]) -> AgentStep:
    if step.kind != "tool":
        return step
    arguments = _replace_unresolved_step_symbols(step.arguments, resolved_symbols)
    return replace(step, arguments=arguments) if arguments != step.arguments else step


def _looks_like_stock_symbol(value: Any) -> bool:
    text = str(value or "").strip().upper()
    return (len(text) == 6 and text.isdigit()) or (
        len(text) == 9
        and text[:6].isdigit()
        and text[6] == "."
        and text[7:] in {"SH", "SZ", "BJ"}
    )


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
    if any(term in text for term in ("短线", "短期", "量价")) or "short" in lowered:
        return "short_term"
    if any(term in text for term in ("成长", "质量", "品质", "ROE", "利润质量")) or any(term in lowered for term in ("growth", "quality", "roe")):
        return "growth_quality"
    if any(term in text for term in ("价值", "估值", "低估", "预期重估", "预期差", "估值修复")) or "value" in lowered:
        return "fundamental_quality"
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
