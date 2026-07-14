from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from sats.agent.date_policy import agent_today, is_forecast_without_intraday, resolve_agent_time_context
from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok
from sats.analysis.market_llm_context import SUPPORTED_MARKET_DIMENSIONS
from sats.analysis.stock_llm_context import ensure_stock_analysis_data, extract_period_return_requests, minute_curve_metadata
from sats.analysis.stock_picking_agent import resolve_theme_universe
from sats.backtesting.service import format_backtest_report, run_strategy_backtest
from sats.backtesting.strategy_spec import strategy_draft_python, strategy_spec_from_request, validate_strategy_spec
from sats.chat_components import (
    build_chan_context_component,
    build_knowledge_context_component,
    build_market_context_component,
    build_opportunity_component,
    build_stock_context_component,
    run_internal_analysis_component,
    run_rule_generation_component,
)
from sats.chat_artifacts import save_markdown_artifact
from sats.data.astock_provider import AStockDataProvider
from sats.deep_analysis import run_deep_analysis
from sats.memory import ChatMemoryStore
from sats.minute_periods import extract_minute_periods, normalize_minute_periods
from sats.rag.chan_knowledge import search_chan_knowledge
from sats.serenity import run_serenity_screen
from sats.signals import SignalInput, analyze_signal_inputs
from sats.stock_basic_lookup import match_stock_name
from sats.stock_question import StockQuestion, extract_stock_symbols
from sats.symbols import normalize_symbols, normalize_ts_code
from sats.web import search as web_search


THEME_STOCK_RETURN_LIMIT = 30
THEME_STOCK_LIST_LIMIT = 80
SECTOR_RETURN_RANKING_LIMIT = 50
DEFAULT_INTRADAY_MINUTE_PERIODS = ("15m", "30m")
OPTIONAL_MINUTE_FIELDS = ("optional_minute_15m", "optional_minute_30m")
INTRADAY_FORECAST_TERMS = (
    "今天走势",
    "今日走势",
    "当日走势",
    "明天走势",
    "明日走势",
    "次日走势",
    "日内",
    "盘中",
    "分时",
    "分钟",
)


def research_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="research.market_context",
            description="获取真实 A 股大盘指数、市场宽度、涨跌停情绪、热点板块和资金流上下文。",
            category="research",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "trade_date": {"type": "string"},
                    "horizon": {"type": "string"},
                    "horizons": {"type": "array", "items": {"type": "string"}},
                    "indices": {"type": "array", "items": {"type": "string"}},
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(SUPPORTED_MARKET_DIMENSIONS)},
                    },
                }
            ),
            executor=_market_context,
        ),
        AgentToolSpec(
            name="research.stock_context",
            description="获取指定 A 股个股真实研究上下文。",
            category="research",
            side_effect="readonly",
            timeout=60,
            input_schema=object_schema(
                {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "trade_date": {"type": "string"},
                    "horizons": {"type": "array", "items": {"type": "string"}},
                    "minute_periods": {"type": "array", "items": {"type": "string"}},
                },
                ["symbols"],
            ),
            executor=_stock_context,
        ),
        AgentToolSpec(
            name="research.serenity_screen",
            description=(
                "运行 SATS 原生 Serenity A 股 AI/科技供应链卡位筛选，"
                "返回确定性评分、证据等级、风险罚分和研究优先级。"
            ),
            category="research_workflow",
            side_effect="readonly",
            timeout=300,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "theme": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "trade_date": {"type": "string"},
                    "limit": {"type": "integer"},
                    "candidate_limit": {"type": "integer"},
                    "lookback_days": {"type": "integer"},
                    "llm_review": {"type": "boolean"},
                }
            ),
            executor=_serenity_screen,
        ),
        AgentToolSpec(
            name="research.theme_stock_list",
            description="解析主题相关 A 股股票池并返回代码、名称、行业、市场和主题关联说明；不做短线机会筛选或预测。",
            category="research",
            side_effect="readonly",
            timeout=90,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "theme": {"type": "string"},
                    "trade_date": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["query"],
            ),
            executor=_theme_stock_list,
        ),
        AgentToolSpec(
            name="research.theme_stock_returns",
            description="解析主题相关 A 股股票池，并计算全量候选的区间涨跌幅；用于“相关股票/概念股 + 近N个月涨跌幅/表现”类问题，不做短线机会筛选。",
            category="research",
            side_effect="readonly",
            timeout=180,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "theme": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "trade_date": {"type": "string"},
                    "period": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["query"],
            ),
            executor=_theme_stock_returns,
            metadata={
                "domain": "market",
                "subject_grain": "stock",
                "metric_grain": "stock_period_return",
                "time_scope": "period",
                "output_shape": "ranked_stock_table",
                "enumerates_universe": False,
                "requires_symbols": False,
                "writes_db": False,
            },
        ),
        AgentToolSpec(
            name="research.sector_return_ranking",
            description="枚举 A 股概念/行业板块指数并计算区间涨跌幅排行；用于“概念板块/行业板块 + 一年/近N月 + 涨幅/跌幅排行”类问题。",
            category="research",
            side_effect="readonly",
            timeout=240,
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "source": {"type": "string", "enum": ["ths", "em"]},
                    "sector_type": {"type": "string", "enum": ["concept", "industry"]},
                    "period": {"type": "string"},
                    "direction": {"type": "string", "enum": ["bottom", "top"]},
                    "trade_date": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["query"],
            ),
            executor=_sector_return_ranking,
            metadata={
                "domain": "market",
                "subject_grain": "sector",
                "metric_grain": "sector_index_return",
                "time_scope": "period",
                "output_shape": "ranked_sector_table",
                "enumerates_universe": True,
                "requires_symbols": False,
                "writes_db": False,
            },
        ),
        AgentToolSpec(
            name="research.internal_analysis",
            description="运行 SATS 白名单内部分析：指标、信号分析、native DSA、因子摘要、公司介绍与基本面。",
            category="research",
            side_effect="readonly",
            timeout=90,
            input_schema=object_schema(
                {
                    "kind": {"type": "string", "enum": ["indicators", "analyze_signals", "native_dsa", "factor_summary", "company_fundamentals"]},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "trade_date": {"type": "string"},
                    "signals": {"type": "string"},
                    "profile": {"type": "string"},
                    "lookback_days": {"type": "integer"},
                    "horizons": {"type": "array", "items": {"type": "string"}},
                    "minute_periods": {"type": "array", "items": {"type": "string"}},
                },
                ["kind", "symbols"],
            ),
            executor=_internal_analysis,
        ),
        AgentToolSpec(
            name="research.deep_stock_analysis",
            description="运行 SATS 原生 A 股个股深研闭环：采集、评分、投资人面板和综合摘要数据。",
            category="research_workflow",
            side_effect="readonly",
            timeout=240,
            input_schema=object_schema(
                {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "trade_date": {"type": "string"},
                    "phase": {"type": "string", "enum": ["run", "collect", "score", "panel", "report"]},
                    "lookback_days": {"type": "integer"},
                    "llm_review": {"type": "boolean"},
                },
                ["symbols"],
            ),
            executor=_deep_stock_analysis,
        ),
        AgentToolSpec(
            name="research.chan_context",
            description="获取缠论分析所需的本地 skill 与知识卡上下文。",
            category="research",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema({"message": {"type": "string"}}),
            executor=_chan_context,
        ),
        AgentToolSpec(
            name="research.knowledge_context",
            description="按知识库或 collection 构建本地研究上下文。",
            category="knowledge",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "message": {"type": "string"},
                    "knowledge": {"type": "string"},
                    "collections": {"type": "array", "items": {"type": "string"}},
                }
            ),
            executor=_knowledge_context,
        ),
        AgentToolSpec(
            name="research.rule_generation",
            description="运行筛选规则计划、修订或确认生成。",
            category="research_workflow",
            side_effect="write_artifact",
            timeout=60,
            input_schema=object_schema(
                {
                    "message": {"type": "string"},
                    "action": {"type": "string", "enum": ["auto", "plan", "revise", "confirm"]},
                    "rule_name": {"type": "string"},
                }
            ),
            executor=_rule_generation,
        ),
        AgentToolSpec(
            name="research.chan_kb_search",
            description="搜索本地缠论知识卡。",
            category="knowledge",
            side_effect="readonly",
            timeout=20,
            input_schema=object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
            executor=_chan_kb_search,
        ),
        AgentToolSpec(
            name="research.strategy_draft",
            description="生成受限 SATS 策略 spec 和可读策略草稿数据。",
            category="research_workflow",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(
                {
                    "request": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                },
                ["request"],
            ),
            executor=_strategy_draft,
        ),
        AgentToolSpec(
            name="research.backtest",
            description="运行 SATS-native 轻量回测；行情通过 DuckDB-first resolver 获取。",
            category="research_workflow",
            side_effect="readonly",
            timeout=120,
            input_schema=object_schema(
                {
                    "request": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "spec": {"type": "object"},
                }
            ),
            executor=_backtest,
        ),
        AgentToolSpec(
            name="research.write_report",
            description="把 agent 当前研究结论写成 Markdown 报告产物。",
            category="research_workflow",
            side_effect="write_artifact",
            timeout=30,
            input_schema=object_schema(
                {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                }
            ),
            executor=_write_report,
        ),
    ]


def _disabled_discover_opportunities_tool_spec() -> AgentToolSpec:
    """Kept for quick re-enable; intentionally not returned by research_tool_specs."""
    return AgentToolSpec(
        name="research.discover_opportunities",
        description="运行 SATS 自然语言 A 股机会发现，返回候选排序和研究数据。",
        category="research_workflow",
        side_effect="readonly",
        timeout=180,
        input_schema=object_schema(
            {
                "query": {"type": "string"},
                "trade_date": {"type": "string"},
                "signals": {"type": "string"},
                "limit": {"type": "integer"},
                "candidate_limit": {"type": "integer"},
                "hot_sector": {"type": "boolean"},
                "hot_sector_days": {"type": "integer"},
            },
            ["query"],
        ),
        executor=_discover_opportunities,
    )


def _market_context(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = _context_payload(
        build_market_context_component(
            str(arguments.get("message") or context.message or "获取大盘上下文"),
            settings=context.settings,
            trade_date=str(arguments.get("trade_date") or "").strip() or None,
            indices=arguments.get("indices") if isinstance(arguments.get("indices"), list) else (),
            dimensions=arguments.get("dimensions") if isinstance(arguments.get("dimensions"), list) else (),
            horizons=arguments.get("horizons") if isinstance(arguments.get("horizons"), list) else ([arguments.get("horizon")] if arguments.get("horizon") else ()),
        )
    )
    result_payload = {"status": "ok", "market_context": payload}
    return AgentToolResult(
        status="done",
        content=json.dumps(result_payload, ensure_ascii=False, default=str),
        payload=result_payload,
        data_names=("market_context",),
    )


def _discover_opportunities(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    result = build_opportunity_component(
        str(arguments.get("query") or context.message or ""),
        settings=context.settings,
        skills=list(context.skills),
        trade_date=str(arguments.get("trade_date") or "").strip() or None,
        limit=int(arguments.get("limit")) if arguments.get("limit") is not None else None,
        candidate_limit=int(arguments.get("candidate_limit") or 50),
        hot_sector_enabled=bool(arguments.get("hot_sector", True)),
        hot_sector_days=int(arguments.get("hot_sector_days") or 5),
        report=False,
        progress=None,
    )
    payload = _context_payload(result)
    legacy = result.discovery.to_llm_context() if hasattr(result, "discovery") else payload
    result_payload = {"status": "ok", "stock_picking_agent": payload, "opportunity_discovery": legacy}
    return AgentToolResult(
        status="done",
        content=json.dumps(result_payload, ensure_ascii=False, default=str),
        payload=result_payload,
        data_names=("opportunity_discovery",),
    )


def _theme_stock_list(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    query = str(arguments.get("query") or context.message or "").strip()
    theme = str(arguments.get("theme") or "").strip()
    trade_date = str(arguments.get("trade_date") or "").strip() or agent_today()
    limit = _positive_int(arguments.get("limit"), default=50, maximum=THEME_STOCK_LIST_LIMIT)
    provider = AStockDataProvider(context.settings)
    stock_basic = _load_stock_basic_for_theme_returns(provider=provider, storage=context.storage)
    warnings: list[str] = []
    try:
        universe = resolve_theme_universe(
            f"{theme or query} 相关股票",
            provider,
            context.storage,
            context.llm_factory,
            settings=context.settings,
            trade_date=trade_date,
            llm_enabled=context.llm_factory is not None,
            max_symbols=limit,
        )
        warnings.extend(str(item) for item in (getattr(universe, "warnings", ()) or ()))
    except Exception as exc:
        universe = None
        warnings.append(f"theme_universe: {exc}")
    stocks = _theme_stock_list_rows(universe, stock_basic=stock_basic)
    payload = {
        "status": "ok",
        "theme_stock_list": _drop_empty(
            {
                "query": query,
                "theme": str(getattr(universe, "theme", "") or theme or _clean_theme_label(query)) if universe is not None else theme or _clean_theme_label(query),
                "source": str(getattr(universe, "source", "") or "none") if universe is not None else "none",
                "matched_sector": str(getattr(universe, "matched_sector", "") or "") if universe is not None else "",
                "theme_universe_count": int(getattr(universe, "count", 0) or 0) if universe is not None else 0,
                "returned_count": len(stocks),
                "stocks": stocks,
                "warnings": _dedupe(warnings),
                "policy": "仅返回主题相关股票池和基础信息；未运行 short_up、Analyze 信号筛选或短期机会预测。",
            }
        ),
    }
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=("theme_stock_list",),
    )


def _serenity_screen(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    symbols = normalize_symbols(arguments.get("symbols") or [], required=False)
    result = run_serenity_screen(
        query=str(arguments.get("query") or context.message or ""),
        theme=str(arguments.get("theme") or ""),
        symbols=symbols,
        trade_date=str(arguments.get("trade_date") or agent_today()),
        limit=int(arguments.get("limit") or 10),
        candidate_limit=int(arguments.get("candidate_limit") or 30),
        lookback_days=int(arguments.get("lookback_days") or 180),
        llm_review=bool(arguments.get("llm_review", True)),
        report=False,
        settings=context.settings,
        storage=context.storage,
        astock_provider=AStockDataProvider(context.settings),
        llm_factory=context.llm_factory,
    )
    payload = {"status": "ok", "serenity_screen": result.to_dict()}
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=("serenity_screen",),
    )


def _theme_stock_returns(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    query = str(arguments.get("query") or context.message or "").strip()
    theme = str(arguments.get("theme") or "").strip()
    trade_date = str(arguments.get("trade_date") or "").strip() or agent_today()
    period = str(arguments.get("period") or "").strip()
    focus_symbols = normalize_symbols(arguments.get("symbols") or [], required=False)
    limit = _positive_int(arguments.get("limit"), default=THEME_STOCK_RETURN_LIMIT, maximum=THEME_STOCK_RETURN_LIMIT)
    provider = AStockDataProvider(context.settings)
    stock_basic = _load_stock_basic_for_theme_returns(provider=provider, storage=context.storage)
    search_query = _theme_web_query(query, theme=theme)
    web_payload = web_search(
        search_query,
        limit=limit,
        context_size="medium",
        settings=context.settings,
    )
    warnings: list[str] = []
    candidates = _candidates_from_symbols(focus_symbols, stock_basic=stock_basic)
    candidates.extend(_candidates_from_web_search(web_payload))
    try:
        universe = resolve_theme_universe(
            f"{theme or query} 相关股票",
            provider,
            context.storage,
            context.llm_factory,
            settings=context.settings,
            trade_date=trade_date,
            llm_enabled=context.llm_factory is not None,
            max_symbols=limit,
        )
        candidates.extend(_candidates_from_theme_universe(universe))
        warnings.extend(str(item) for item in (getattr(universe, "warnings", ()) or ()))
        if not theme:
            theme = str(getattr(universe, "theme", "") or "").strip()
    except Exception as exc:
        warnings.append(f"theme_universe: {exc}")
    validated, validation_warnings = _validate_theme_return_candidates(candidates, stock_basic=stock_basic, limit=limit)
    warnings.extend(validation_warnings)
    period_message = _period_message(query, period)
    period_requests = extract_period_return_requests(period_message)
    if not period_requests:
        period_message = f"{query} 6个月涨跌幅"
        period_requests = extract_period_return_requests(period_message)
    lookback_days = _lookback_days_for_period_requests(period_requests)
    stocks: list[dict[str, Any]] = []
    for candidate in validated:
        stock_row = {
            "ts_code": candidate["ts_code"],
            "name": candidate["name"],
            "relation_reason": candidate.get("reason"),
            "candidate_sources": candidate.get("candidate_sources"),
            "source_ids": candidate.get("source_ids"),
        }
        try:
            contexts = ensure_stock_analysis_data(
                [candidate["ts_code"]],
                trade_date,
                settings=context.settings,
                storage=context.storage,
                periods=(),
                lookback_days=lookback_days,
                period_requests=period_requests,
            )
            stock_context = contexts.get(candidate["ts_code"], {})
            stock_row.update(
                {
                    "requested_trade_date": stock_context.get("requested_trade_date"),
                    "trade_date": stock_context.get("trade_date"),
                    "price_context": stock_context.get("price_context"),
                    "period_returns": stock_context.get("period_returns"),
                    "missing_fields": stock_context.get("missing_fields"),
                }
            )
        except Exception as exc:
            stock_row["missing_fields"] = [f"period_returns: {exc}"]
        stocks.append(_drop_empty(stock_row))
    requested_symbols = [item["ts_code"] for item in validated]
    returned_symbols = [
        str(item.get("ts_code"))
        for item in stocks
        if isinstance(item.get("period_returns"), dict) and item.get("period_returns")
    ]
    ranking = _theme_return_ranking(stocks, period_requests[0].key if period_requests else period or "6m")
    payload = {
        "status": "ok",
        "theme_stock_returns": _drop_empty(
            {
                "query": query,
                "theme": theme or _clean_theme_label(query),
                "period": period_requests[0].key if period_requests else period or "6m",
                "trade_date": trade_date,
                "focus_symbols": focus_symbols,
                "candidate_count": len(validated),
                "stocks": stocks,
                "ranking": ranking,
                "coverage": {
                    "requested_count": len(requested_symbols),
                    "returned_count": len(returned_symbols),
                    "missing_symbols": [symbol for symbol in requested_symbols if symbol not in returned_symbols],
                    "policy": "主题候选全量展示；web/LLM 只用于发现股票池，涨跌幅必须来自 SATS 结构化行情 period_returns。",
                },
                "web_search": {
                    "query": web_payload.get("query"),
                    "backend": web_payload.get("backend"),
                    "status": web_payload.get("status"),
                    "warnings": web_payload.get("warnings"),
                },
                "sources": web_payload.get("sources") or web_payload.get("results") or [],
                "warnings": _dedupe(warnings),
            }
        ),
    }
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=("theme_stock_returns",),
    )


def _sector_return_ranking(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    query = str(arguments.get("query") or context.message or "").strip()
    source = _sector_source(arguments.get("source"), query=query)
    sector_type = _sector_type(arguments.get("sector_type"), query=query)
    period = str(_sector_period_from_text(query) or arguments.get("period") or "").strip()
    if not period:
        content = "research.sector_return_ranking requires a recognizable period such as 1d, 5d, 6m, 1y, 今天, 近5个交易日, 半年 or 一年"
        return AgentToolResult(
            status="error",
            content=json.dumps({"status": "error", "error": content}, ensure_ascii=False),
            payload={"status": "error", "error": content},
            data_names=("sector_return_ranking",),
        )
    direction = _sector_direction(arguments.get("direction"), query=query)
    trade_date = str(arguments.get("trade_date") or "").strip() or agent_today()
    limit = _positive_int(arguments.get("limit"), default=10, maximum=SECTOR_RETURN_RANKING_LIMIT)
    single_day = _sector_is_single_day_period(period)
    start_date = _sector_single_day_lookup_start_date(trade_date) if single_day else _sector_start_date(trade_date, period)
    provider = AStockDataProvider(context.settings)
    sectors_payload = _load_sector_index_rows(provider, source=source, sector_type=sector_type, trade_date=trade_date)
    sector_rows = sectors_payload["rows"]
    warnings: list[str] = list(sectors_payload.get("warnings") or [])
    results: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    actual_trade_date = ""
    if single_day:
        candidates: list[dict[str, Any]] = []
        for sector in sector_rows:
            sector_code = str(sector.get("sector_code") or sector.get("ts_code") or "").strip().upper()
            name = str(sector.get("name") or "").strip()
            if not sector_code:
                continue
            daily_payload = _load_sector_daily_rows(
                provider,
                source=source,
                sector_code=sector_code,
                start_date=start_date,
                end_date=trade_date,
            )
            daily = _sector_daily_frame(daily_payload.get("rows") or [])
            if daily.empty:
                missing.append(
                    _drop_empty(
                        {
                            "sector_code": sector_code,
                            "name": name,
                            "reason": "sector_daily_insufficient",
                            "data_source": daily_payload.get("data_source"),
                            "missing_fields": daily_payload.get("missing_fields"),
                        }
                    )
                )
                continue
            selected = daily.iloc[-1]
            previous = daily.iloc[-2] if len(daily) >= 2 else None
            candidates.append(
                {
                    "sector_code": sector_code,
                    "name": name,
                    "daily_payload": daily_payload,
                    "selected": selected,
                    "previous": previous,
                }
            )
        actual_dates = [str(item["selected"].get("trade_date") or "") for item in candidates if str(item["selected"].get("trade_date") or "")]
        actual_trade_date = max(actual_dates) if actual_dates else ""
        if actual_trade_date and actual_trade_date != trade_date:
            warnings.append(f"sector_daily_actual_trade_date:{actual_trade_date};requested:{trade_date}")
        for candidate in candidates:
            selected = candidate["selected"]
            selected_date = str(selected.get("trade_date") or "")
            if actual_trade_date and selected_date != actual_trade_date:
                missing.append(
                    {
                        "sector_code": candidate["sector_code"],
                        "name": candidate["name"],
                        "reason": "sector_daily_not_latest_actual_trade_date",
                        "latest_trade_date": selected_date,
                        "actual_trade_date": actual_trade_date,
                    }
                )
                continue
            close = _safe_float(selected.get("close"))
            if close is None:
                missing.append({"sector_code": candidate["sector_code"], "name": candidate["name"], "reason": "invalid_close"})
                continue
            pct_change = _safe_float(selected.get("pct_change"))
            pct_change_source = "pct_change"
            if pct_change is None:
                previous = candidate.get("previous")
                previous_close = _safe_float(previous.get("close")) if previous is not None else None
                if previous_close is None or previous_close <= 0:
                    missing.append(
                        {
                            "sector_code": candidate["sector_code"],
                            "name": candidate["name"],
                            "reason": "sector_daily_pct_change_missing",
                        }
                    )
                    continue
                pct_change = (close / previous_close - 1.0) * 100.0
                pct_change_source = "close_fallback"
            daily_payload = candidate["daily_payload"]
            results.append(
                {
                    "sector_code": candidate["sector_code"],
                    "name": candidate["name"],
                    "source": source,
                    "sector_type": sector_type,
                    "trade_date": selected_date,
                    "start_trade_date": selected_date,
                    "end_trade_date": selected_date,
                    "close": round(close, 4),
                    "start_close": round(close, 4),
                    "end_close": round(close, 4),
                    "pct_change": round(float(pct_change), 4),
                    "pct_change_source": pct_change_source,
                    "sample_days": 1,
                    "data_source": daily_payload.get("data_source"),
                    "data_status": "ok",
                }
            )
    else:
        for sector in sector_rows:
            sector_code = str(sector.get("sector_code") or sector.get("ts_code") or "").strip().upper()
            name = str(sector.get("name") or "").strip()
            if not sector_code:
                continue
            daily_payload = _load_sector_daily_rows(
                provider,
                source=source,
                sector_code=sector_code,
                start_date=start_date,
                end_date=trade_date,
            )
            daily = _sector_daily_frame(daily_payload.get("rows") or [])
            if daily.empty or len(daily) < 2:
                missing.append(
                    _drop_empty(
                        {
                            "sector_code": sector_code,
                            "name": name,
                            "reason": "sector_daily_insufficient",
                            "data_source": daily_payload.get("data_source"),
                            "missing_fields": daily_payload.get("missing_fields"),
                        }
                    )
                )
                continue
            start = daily.iloc[0]
            end = daily.iloc[-1]
            start_close = float(start["close"])
            end_close = float(end["close"])
            if start_close <= 0:
                missing.append({"sector_code": sector_code, "name": name, "reason": "invalid_start_close"})
                continue
            pct_change = (end_close / start_close - 1.0) * 100.0
            results.append(
                {
                    "sector_code": sector_code,
                    "name": name,
                    "source": source,
                    "sector_type": sector_type,
                    "start_trade_date": str(start["trade_date"]),
                    "end_trade_date": str(end["trade_date"]),
                    "start_close": round(start_close, 4),
                    "end_close": round(end_close, 4),
                    "pct_change": round(pct_change, 4),
                    "sample_days": int(len(daily)),
                    "data_source": daily_payload.get("data_source"),
                    "data_status": "ok",
                }
            )
    results = sorted(results, key=lambda item: float(item.get("pct_change") or 0), reverse=direction == "top")
    ranking = []
    for rank, row in enumerate(results[:limit], start=1):
        ranked = dict(row)
        ranked["rank"] = rank
        ranking.append(ranked)
    payload = {
        "status": "ok",
        "sector_return_ranking": _drop_empty(
            {
                "query": query,
                "source": source,
                "sector_type": sector_type,
                "period": period,
                "direction": direction,
                "trade_date": actual_trade_date if single_day and actual_trade_date else trade_date,
                "requested_trade_date": trade_date if single_day else "",
                "actual_trade_date": actual_trade_date if single_day else "",
                "start_date": actual_trade_date if single_day and actual_trade_date else start_date,
                "lookup_start_date": start_date if single_day else "",
                "limit": limit,
                "ranking": ranking,
                "coverage": {
                    "sector_count": len(sector_rows),
                    "computed_count": len(results),
                    "returned_count": len(ranking),
                    "missing_count": len(missing),
                    "policy": (
                        "单日板块排行按最新可用交易日 pct_change 排序；pct_change 缺失时用相邻收盘价兜底。"
                        if single_day
                        else "按板块指数窗口内最早/最晚有效收盘价计算区间涨跌幅；不使用股票池个股均值替代板块指数。"
                    ),
                },
                "missing": missing[:50],
                "warnings": _dedupe(warnings),
                "data_sources": {
                    "sector_index": sectors_payload.get("data_source"),
                    "sector_daily": f"{source}_sector_daily",
                },
            }
        ),
    }
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=("sector_return_ranking",),
    )


def _stock_context(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    minute_periods = _minute_periods_from_arguments(arguments, context.message)
    if not is_forecast_without_intraday(context.message, arguments):
        symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
        stock_context = build_stock_context_component(
            context.message or " ".join(symbols),
            settings=context.settings,
            question=StockQuestion(
                symbols=symbols,
                trade_date=str(arguments.get("trade_date") or "").strip() or None,
                has_stock_question=True,
            ),
            minute_periods=minute_periods,
        )
        payload = _context_payload(stock_context)
        result_payload = {"status": "ok", "stock_context": payload}
        return AgentToolResult(
            status="done",
            content=json.dumps(result_payload, ensure_ascii=False, default=str),
            payload=result_payload,
            data_names=("stock_context",),
        )
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    trade_date = str(arguments.get("trade_date") or agent_today())
    requested_periods = minute_periods or _default_intraday_periods_for_forecast(context.message)
    stock_contexts = ensure_stock_analysis_data(
        symbols,
        trade_date,
        settings=context.settings,
        storage=context.storage,
        periods=requested_periods,
        period_requests=extract_period_return_requests(context.message),
    )
    payload = _stock_context_payload(
        context,
        symbols,
        trade_date,
        stock_contexts,
        arguments,
        requested_minute_periods=requested_periods,
    )
    return AgentToolResult(
        status="done",
        content=json.dumps({"status": "ok", "stock_context": payload}, ensure_ascii=False, default=str),
        payload={"status": "ok", "stock_context": payload},
        data_names=("stock_context",),
    )


def _internal_analysis(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    kind = str(arguments.get("kind") or "").strip()
    if kind == "company_fundamentals":
        symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
        provider = AStockDataProvider(context.settings)
        companies = provider.load_company_fundamentals(
            symbols,
            trade_date=str(arguments.get("trade_date") or agent_today()),
            storage=context.storage,
            periods=4,
        )
        rows = [companies[symbol] for symbol in symbols if symbol in companies]
        payload = {
            "kind": kind,
            "trade_date": str(arguments.get("trade_date") or agent_today()),
            "companies": rows,
            "missing_fields": list(
                dict.fromkeys(
                    field
                    for company in rows
                    for field in company.get("missing_fields") or []
                )
            ),
        }
        return AgentToolResult(
            status="done",
            content=json.dumps({"status": "ok", "analysis": payload}, ensure_ascii=False, default=str),
            payload={"status": "ok", "analysis": payload},
            data_names=("company_fundamentals",),
        )
    if kind not in {"indicators", "analyze_signals"} or not is_forecast_without_intraday(context.message, arguments):
        payload = run_internal_analysis_component(context.settings, {**arguments, "_message": context.message})
        result_payload = {"status": "ok", "analysis": payload}
        return AgentToolResult(
            status="done",
            content=json.dumps(result_payload, ensure_ascii=False, default=str),
            payload=result_payload,
            data_names=("internal_analysis",),
        )
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    trade_date = str(arguments.get("trade_date") or agent_today())
    stock_contexts = ensure_stock_analysis_data(
        symbols,
        trade_date,
        settings=context.settings,
        storage=context.storage,
        periods=(),
        lookback_days=int(arguments.get("lookback_days") or 180),
        period_requests=extract_period_return_requests(context.message),
    )
    effective_trade_date = _effective_stock_context_trade_date(stock_contexts, trade_date)
    if kind == "indicators":
        payload = {
            "kind": kind,
            "trade_date": effective_trade_date,
            "forecast_horizons": list(resolve_agent_time_context(context.message, arguments=arguments).horizons),
            "indicators": [
                {
                    "ts_code": item.get("ts_code"),
                    "name": item.get("name"),
                    "trade_date": item.get("trade_date"),
                    "indicator_result": item.get("indicator_result", {}),
                    "period_returns": item.get("period_returns"),
                    "missing_fields": item.get("missing_fields"),
                }
                for item in stock_contexts.values()
            ],
            "missing_fields": _combined_missing_fields(stock_contexts),
        }
    else:
        inputs = [_signal_input_from_context(item) for item in stock_contexts.values()]
        run = analyze_signal_inputs(
            inputs,
            selected_signals=str(arguments.get("signals") or "short_up"),
            trade_date=effective_trade_date,
            report=False,
        )
        payload = {
            "kind": kind,
            "trade_date": run.trade_date,
            "forecast_horizons": list(resolve_agent_time_context(context.message, arguments=arguments).horizons),
            "results": [result.to_dict() for result in run.results],
            "missing_fields": _combined_missing_fields(stock_contexts),
        }
    return AgentToolResult(
        status="done",
        content=json.dumps({"status": "ok", "analysis": payload}, ensure_ascii=False, default=str),
        payload={"status": "ok", "analysis": payload},
        data_names=("internal_analysis",),
    )


def _deep_stock_analysis(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    trade_date = str(arguments.get("trade_date") or agent_today())
    result = run_deep_analysis(
        symbols,
        trade_date=trade_date,
        phase=str(arguments.get("phase") or "run"),
        settings=context.settings,
        storage=context.storage,
        lookback_days=int(arguments.get("lookback_days") or 180),
        llm_review=bool(arguments.get("llm_review", True)),
        report=False,
    )
    payload = {"status": "ok", "deep_stock_analysis": result.to_dict()}
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=("deep_stock_analysis",),
    )


def _chan_context(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    payload = _context_payload(build_chan_context_component(str(arguments.get("message") or context.message or ""), skills=list(context.skills)))
    result_payload = {"status": "ok", "chan_context": payload}
    return AgentToolResult(
        status="done",
        content=json.dumps(result_payload, ensure_ascii=False, default=str),
        payload=result_payload,
        data_names=("chan_context",),
    )


def _knowledge_context(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    knowledge_context = build_knowledge_context_component(
        str(arguments.get("message") or context.message or ""),
        settings=context.settings,
        explicit_knowledge=str(arguments.get("knowledge") or "").strip() or None,
        collections=arguments.get("collections") if isinstance(arguments.get("collections"), list) else (),
    )
    payload = {}
    if knowledge_context is not None:
        payload = {
            "collections": list(knowledge_context.collections),
            "sources": list(knowledge_context.sources),
            "system_message": knowledge_context.system_message,
        }
    result_payload = {"status": "ok", "knowledge_context": payload}
    return AgentToolResult(
        status="done",
        content=json.dumps(result_payload, ensure_ascii=False, default=str),
        payload=result_payload,
        data_names=("knowledge_context",),
    )


def _rule_generation(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    store = context.store or ChatMemoryStore(getattr(context.settings, "db_path", None))
    outcome = run_rule_generation_component(
        str(arguments.get("message") or context.message or ""),
        settings=context.settings,
        store=store,
        session_id=context.session_id or "agent",
        action=str(arguments.get("action") or "auto"),
        rule_name=str(arguments.get("rule_name") or ""),
    )
    payload = {
        "status": "ok",
        "rule_generation": {
            "content": outcome.content,
            "data_names": list(outcome.data_names),
            "payload": outcome.payload,
            "pending_action_id": outcome.pending_action_id or "",
            "requires_confirmation": outcome.requires_confirmation,
            "artifacts": list(outcome.artifacts),
        },
    }
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=tuple(outcome.data_names),
        artifacts=tuple(outcome.artifacts),
    )


def _stock_context_payload(
    context: AgentToolContext,
    symbols: list[str],
    trade_date: str,
    stock_contexts: dict[str, dict[str, Any]],
    arguments: dict[str, Any],
    *,
    requested_minute_periods: tuple[str, ...] = (),
) -> dict[str, Any]:
    horizons = list(resolve_agent_time_context(context.message, arguments=arguments).horizons)
    effective_trade_date = _effective_stock_context_trade_date(stock_contexts, trade_date)
    optional_fields_not_requested = [] if requested_minute_periods else list(OPTIONAL_MINUTE_FIELDS)
    stocks = []
    for symbol in symbols:
        item = dict(stock_contexts.get(symbol) or {})
        item["missing_fields"] = list(item.get("missing_fields") or [])
        if optional_fields_not_requested:
            item["optional_fields_not_requested"] = list(optional_fields_not_requested)
        item.setdefault("minute_curves", {})
        stocks.append(item)
    if requested_minute_periods:
        data_policy = (
            "SATS fetched real daily/quote data and requested minute periods "
            f"{', '.join(requested_minute_periods)} before calling the LLM; "
            "missing_fields only contains unavailable requested data."
        )
    else:
        data_policy = (
            "SATS fetched real daily/quote data before calling the LLM; optional minute data was not requested "
            "and is reported separately from missing_fields."
        )
    return {
        "user_question": context.message,
        "requested_trade_date": trade_date,
        "trade_date": effective_trade_date,
        "symbols": symbols,
        "requested_horizons": horizons,
        "requested_minute_periods": list(requested_minute_periods),
        "optional_fields_not_requested": optional_fields_not_requested,
        "data_policy": data_policy,
        "stocks": stocks,
    }


def _effective_stock_context_trade_date(stock_contexts: dict[str, dict[str, Any]], fallback: str) -> str:
    dates = [str(item.get("trade_date") or "") for item in stock_contexts.values() if isinstance(item, dict) and item.get("trade_date")]
    return max(dates) if dates else fallback


def _minute_periods_from_arguments(arguments: dict[str, Any], message: str) -> tuple[str, ...]:
    return normalize_minute_periods(arguments.get("minute_periods") or ()) or extract_minute_periods(message)


def _default_intraday_periods_for_forecast(message: str) -> tuple[str, ...]:
    text = str(message or "")
    if any(term in text for term in INTRADAY_FORECAST_TERMS):
        return DEFAULT_INTRADAY_MINUTE_PERIODS
    return ()


def _signal_input_from_context(context: dict[str, Any]) -> SignalInput:
    return SignalInput(
        ts_code=str(context.get("ts_code") or ""),
        trade_date=str(context.get("trade_date") or ""),
        daily=pd.DataFrame(context.get("daily_tail") or []),
        stock_basic={"name": context.get("name") or ""},
        metadata=minute_curve_metadata(
            context.get("minute_curves") or {},
            preferred_period=str(context.get("chan_minute_period") or ""),
        ),
    )


def _combined_missing_fields(stock_contexts: dict[str, dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for item in stock_contexts.values():
        for field in item.get("missing_fields") or []:
            if field not in fields:
                fields.append(str(field))
    return fields


def _chan_kb_search(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    rows = search_chan_knowledge(str(arguments.get("query") or ""), limit=max(1, int(arguments.get("limit") or 6)))
    payload = {"results": rows}
    return ok(json.dumps(payload, ensure_ascii=False, default=str), payload=payload, data_names=("Chan KB",))


def _strategy_draft(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    request = str(arguments.get("request") or context.message or "")
    symbols = normalize_symbols(arguments.get("symbols") or extract_stock_symbols(request), required=False)
    spec = strategy_spec_from_request(request, symbols=symbols)
    draft = strategy_draft_python(spec)
    payload = {"spec": spec.to_dict(), "draft": draft}
    return ok(draft, payload=payload, data_names=("策略草稿",))


def _backtest(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    if isinstance(arguments.get("spec"), dict) and arguments.get("spec"):
        spec = validate_strategy_spec(arguments["spec"])
    else:
        request = str(arguments.get("request") or context.message or "")
        symbols = normalize_symbols(arguments.get("symbols") or extract_stock_symbols(request), required=False)
        spec = strategy_spec_from_request(request, symbols=symbols)
    result = run_strategy_backtest(spec, settings=context.settings, storage=context.storage, resolver=context.resolver)
    report_text = format_backtest_report(result)
    return ok(
        report_text,
        payload={
            "backtest": result.to_dict(),
            "spec": result.spec.to_dict(),
            "strategy_draft": strategy_draft_python(result.spec),
            "report_text": report_text,
        },
        data_names=("轻量回测",),
    )


def _write_report(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    title = str(arguments.get("title") or "SATS Agent Report")
    content = str(arguments.get("content") or "").strip()
    if not content:
        lines = [f"# {title}", "", f"目标: {context.message}", ""]
        for obs in context.observations:
            lines.append(f"- {getattr(obs, 'step_id', '')}: {getattr(obs, 'content', '')}")
        content = "\n".join(lines)
    report = save_markdown_artifact(
        project_root=_project_root(context),
        session_id=context.session_id,
        turn_id=context.turn_id or "agent",
        title=title,
        content=content,
        filename="agent_report.md",
        report=True,
        summary="Agent 研究报告",
    )
    artifact = _artifact_dict(context, report)
    return ok(f"报告已保存: {artifact.get('path')}", payload={"report": artifact}, data_names=("报告",), artifacts=(artifact,))


def _artifact_dict(context: AgentToolContext, write: Any) -> dict[str, Any]:
    artifact = {
        "kind": write.kind,
        "title": write.title,
        "path": str(write.path),
        "mime_type": write.mime_type,
        "summary": write.summary,
    }
    if context.store is not None:
        try:
            artifact["artifact_id"] = context.store.add_chat_artifact(
                session_id=context.session_id,
                turn_id=context.turn_id or "agent",
                kind=write.kind,
                title=write.title,
                path=str(write.path),
                mime_type=write.mime_type,
                summary=write.summary,
                meta=write.meta or {},
            )
        except Exception:
            artifact["artifact_id"] = ""
    return artifact


def _theme_web_query(query: str, *, theme: str = "") -> str:
    text = str(theme or query or "").strip()
    if "A股" in text or "a股" in text:
        return text
    return f"{text} A股股票"


def _candidates_from_web_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    answer = str(payload.get("answer") or "")
    source_ids = _source_ids(payload)
    if answer:
        candidates.extend(_extract_stock_mentions(answer, source="web_search_answer", source_id=""))
    for item in payload.get("results") if isinstance(payload.get("results"), list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        source_id = str(item.get("source_id") or source_ids.get(url) or "")
        text = "\n".join(str(item.get(key) or "") for key in ("title", "snippet", "body", "content"))
        candidates.extend(_extract_stock_mentions(text, source="web_search_result", source_id=source_id))
    for item in payload.get("evidence") if isinstance(payload.get("evidence"), list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        source_id = str(item.get("source_id") or source_ids.get(url) or "")
        text = "\n".join(str(item.get(key) or "") for key in ("title", "snippet", "content", "text"))
        candidates.extend(_extract_stock_mentions(text, source="web_search_evidence", source_id=source_id))
    return candidates


def _source_ids(payload: dict[str, Any]) -> dict[str, str]:
    result = {}
    for index, item in enumerate(payload.get("sources") if isinstance(payload.get("sources"), list) else [], start=1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            result[url] = str(item.get("id") or f"S{index}")
    return result


def _extract_stock_mentions(text: str, *, source: str, source_id: str = "") -> list[dict[str, Any]]:
    value = str(text or "")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    patterns = (
        r"(?P<name>[\u4e00-\u9fffA-Za-z]{2,16})[（(]\s*(?P<code>[034689]\d{5})(?:\.(?P<exchange>SH|SZ|BJ))?\s*[）)]",
        r"(?P<code>[034689]\d{5})(?:\.(?P<exchange>SH|SZ|BJ))?\s*(?:[）)、:：\-—\s]+)\s*(?P<name>[\u4e00-\u9fffA-Za-z]{2,16})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            raw_code = match.group("code")
            exchange = str(match.groupdict().get("exchange") or "").upper()
            code = normalize_ts_code(f"{raw_code}.{exchange}" if exchange else raw_code)
            name = _clean_mention_name(match.group("name"))
            key = (code, name)
            if not name or key in seen:
                continue
            seen.add(key)
            rows.append(_drop_empty({"ts_code": code, "name": name, "source": source, "source_id": source_id}))
    return rows


def _clean_mention_name(value: str) -> str:
    text = str(value or "").strip(" 　，,。；;：:、（）()[]【】“”\"'")
    for prefix in ("核心公司之一", "公司", "股票", "此外", "以及"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text if 2 <= len(text) <= 16 else ""


def _candidates_from_theme_universe(universe: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stock in getattr(universe, "stocks", ()) or ():
        rows.append(
            _drop_empty(
                {
                    "ts_code": getattr(stock, "ts_code", ""),
                    "name": getattr(stock, "name", ""),
                    "reason": getattr(stock, "reason", ""),
                    "source": getattr(stock, "source", "") or "theme_universe",
                }
            )
        )
    return rows


def _theme_stock_list_rows(universe: Any, *, stock_basic: pd.DataFrame) -> list[dict[str, Any]]:
    if universe is None:
        return []
    basic = _clean_stock_basic(stock_basic)
    by_code = {str(row["ts_code"]): row for _, row in basic.iterrows()} if not basic.empty else {}
    rows: list[dict[str, Any]] = []
    for stock in getattr(universe, "stocks", ()) or ():
        ts_code = normalize_ts_code(str(getattr(stock, "ts_code", "") or ""))
        if not _is_a_share_ts_code(ts_code):
            continue
        basic_row = by_code.get(ts_code, {})
        rows.append(
            _drop_empty(
                {
                    "ts_code": ts_code,
                    "name": str(getattr(stock, "name", "") or basic_row.get("name") or "").strip(),
                    "industry": str(basic_row.get("industry") or "").strip(),
                    "market": str(basic_row.get("market") or "").strip(),
                    "exchange": str(basic_row.get("exchange") or "").strip(),
                    "relation_type": str(getattr(stock, "relation_type", "") or "").strip(),
                    "source": str(getattr(stock, "source", "") or getattr(universe, "source", "") or "").strip(),
                    "reason": str(getattr(stock, "reason", "") or "").strip(),
                }
            )
        )
    return rows


def _candidates_from_symbols(symbols: list[str], *, stock_basic: pd.DataFrame) -> list[dict[str, Any]]:
    if not symbols:
        return []
    basic = _clean_stock_basic(stock_basic)
    by_code = {str(row["ts_code"]): row for _, row in basic.iterrows()} if not basic.empty else {}
    rows: list[dict[str, Any]] = []
    for symbol in normalize_symbols(symbols, required=False):
        basic_row = by_code.get(symbol, {})
        rows.append(
            _drop_empty(
                {
                    "ts_code": symbol,
                    "name": str(basic_row.get("name") or ""),
                    "reason": "用户关注股票",
                    "source": "requested_symbol",
                }
            )
        )
    return rows


def _validate_theme_return_candidates(candidates: list[dict[str, Any]], *, stock_basic: pd.DataFrame, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    basic = _clean_stock_basic(stock_basic)
    by_code = {str(row["ts_code"]): row for _, row in basic.iterrows()} if not basic.empty else {}
    result: list[dict[str, Any]] = []
    by_symbol: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        raw_code = str(candidate.get("ts_code") or "").strip()
        raw_name = str(candidate.get("name") or "").strip()
        code = normalize_ts_code(raw_code)
        local_name = ""
        if _is_a_share_ts_code(code) and (not by_code or code in by_code):
            local_name = str(by_code.get(code, {}).get("name") or raw_name).strip()
        elif raw_name and not basic.empty:
            matched = match_stock_name(raw_name, basic)
            if len(matched) == 1:
                code = str(matched.iloc[0]["ts_code"])
                local_name = str(matched.iloc[0].get("name") or raw_name).strip()
        if not _is_a_share_ts_code(code):
            warnings.append(f"theme_candidate_{index}: unrecognized:{raw_code or raw_name}")
            continue
        existing = by_symbol.get(code)
        if existing is None:
            existing = {
                "ts_code": code,
                "name": local_name or raw_name,
                "reason": str(candidate.get("reason") or "").strip(),
                "candidate_sources": [],
                "source_ids": [],
            }
            by_symbol[code] = existing
            result.append(existing)
        source = str(candidate.get("source") or "").strip()
        source_id = str(candidate.get("source_id") or "").strip()
        if source:
            existing["candidate_sources"] = _dedupe([*existing.get("candidate_sources", []), source])
        if source_id:
            existing["source_ids"] = _dedupe([*existing.get("source_ids", []), source_id])
        if not existing.get("reason") and candidate.get("reason"):
            existing["reason"] = str(candidate.get("reason") or "").strip()
        if len(result) >= limit:
            break
    return [_drop_empty(item) for item in result], warnings


def _theme_return_ranking(stocks: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in stocks:
        if not isinstance(item, dict):
            continue
        selected = _selected_period_return(item.get("period_returns"), period)
        pct_change = _safe_float(selected.get("pct_change"))
        if pct_change is None:
            continue
        rows.append(
            _drop_empty(
                {
                    "ts_code": item.get("ts_code"),
                    "name": item.get("name"),
                    "period": period,
                    "start_trade_date": selected.get("start_trade_date"),
                    "end_trade_date": selected.get("end_trade_date"),
                    "pct_change": pct_change,
                }
            )
        )
    rows.sort(key=lambda row: float(row.get("pct_change") or 0), reverse=True)
    peer_count = len(rows)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
        row["peer_count"] = peer_count
        row["is_bottom"] = index == peer_count
    return rows


def _selected_period_return(period_returns: Any, period: str) -> dict[str, Any]:
    if not isinstance(period_returns, dict):
        return {}
    selected = period_returns.get(period) if isinstance(period_returns.get(period), dict) else {}
    if selected:
        return selected
    for value in period_returns.values():
        if isinstance(value, dict):
            return value
    return {}


def _safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_stock_basic_for_theme_returns(*, provider: Any, storage: Any) -> pd.DataFrame:
    if hasattr(provider, "load_stock_basic"):
        try:
            frame = provider.load_stock_basic(storage=storage)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        except Exception:
            pass
    if hasattr(storage, "get_stock_basic"):
        try:
            frame = storage.get_stock_basic()
            if isinstance(frame, pd.DataFrame):
                return frame
        except Exception:
            pass
    return pd.DataFrame()


def _sector_source(value: Any, *, query: str) -> str:
    raw = str(value or "").strip().lower()
    text = str(query or "").lower()
    if raw in {"em", "eastmoney", "东方财富", "东财"} or any(term in text for term in ("东方财富", "东财", "eastmoney", " em")):
        return "em"
    return "ths"


def _sector_type(value: Any, *, query: str) -> str:
    raw = str(value or "").strip().lower()
    text = str(query or "")
    if raw in {"industry", "行业"} or ("行业" in text and "概念" not in text):
        return "industry"
    return "concept"


def _sector_direction(value: Any, *, query: str) -> str:
    raw = str(value or "").strip().lower()
    text = str(query or "")
    if raw in {"top", "up", "best"} or any(term in text for term in ("涨幅最大", "涨幅最高", "领涨", "最好", "最强")):
        return "top"
    return "bottom"


def _sector_period_from_text(text: str) -> str:
    value = str(text or "")
    trading_day_match = re.search(r"(?:过去|最近|近)?\s*([0-9一二两三四五六七八九十]+)\s*(个)?\s*交易日", value)
    if trading_day_match:
        amount = _chinese_number(trading_day_match.group(1))
        return f"{amount}d" if amount > 0 else ""
    if "半年" in value:
        return "6m"
    if "一年" in value or "1年" in value or "近年" in value:
        return "1y"
    match = re.search(r"([0-9一二两三四五六七八九十]+)\s*(个)?\s*(月|年|周|天|日)", value)
    if match:
        amount = _chinese_number(match.group(1))
        suffix = {"月": "m", "年": "y", "周": "w", "天": "d", "日": "d"}.get(match.group(3), "d")
        return f"{amount}{suffix}" if amount > 0 else ""
    if any(term in value for term in ("今天", "今日", "当天", "当日")):
        return "1d"
    return ""


def _sector_is_single_day_period(period: str) -> bool:
    return str(period or "").strip().lower() in {"1d", "d", "day", "daily", "today", "当日", "当天", "今天", "今日"}


def _sector_single_day_lookup_start_date(end_date: str) -> str:
    try:
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        end = datetime.strptime(agent_today(), "%Y%m%d")
    return (end - timedelta(days=14)).strftime("%Y%m%d")


def _sector_start_date(end_date: str, period: str) -> str:
    clean = str(period or "").strip().lower()
    match = re.fullmatch(r"(\d+)\s*([ymwd])", clean)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
    elif clean in {"half_year", "half-year", "半年"}:
        amount, unit = 6, "m"
    else:
        amount, unit = 1, "y"
    days = amount
    if unit == "y":
        days = amount * 366
    elif unit == "m":
        days = amount * 31
    elif unit == "w":
        days = amount * 7
    try:
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        end = datetime.strptime(agent_today(), "%Y%m%d")
    return (end - timedelta(days=max(1, days))).strftime("%Y%m%d")


def _chinese_number(value: str) -> int:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(text, 0)


def _load_sector_index_rows(provider: AStockDataProvider, *, source: str, sector_type: str, trade_date: str) -> dict[str, Any]:
    warnings: list[str] = []
    if source == "ths":
        dataset = "ths_index"
        params = {"type": "N" if sector_type == "concept" else "I"}
        fields = ["ts_code", "name", "count", "exchange", "list_date", "type"]
    else:
        dataset = "dc_index"
        params = {"trade_date": trade_date}
        fields = ["trade_date", "ts_code", "name", "close", "chg_pct", "num", "up_num"]
        if sector_type == "industry":
            warnings.append("em_industry_source: 东方财富白名单当前只登记 dc_index 概念板块，industry 口径可能不完整。")
    payload = _fetch_tushare_stock_dataset(provider, dataset, params=params, fields=fields, limit=6000)
    rows = _sector_index_records(payload.get("rows") or [], sector_type=sector_type, source=source)
    if source == "em" and not rows:
        fallback = _fetch_tushare_stock_dataset(provider, dataset, params={"end_date": trade_date}, fields=fields, limit=6000)
        rows = _sector_index_records(fallback.get("rows") or [], sector_type=sector_type, source=source)
        if rows:
            payload = fallback
    warnings.extend(str(item) for item in (payload.get("missing_fields") or []))
    return {"rows": rows, "warnings": _dedupe(warnings), "data_source": payload.get("data_source")}


def _sector_index_records(rows: list[Any], *, sector_type: str, source: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("sector_code") or row.get("ts_code") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        records.append({"sector_code": code, "name": name, "sector_type": sector_type, "source": source})
    return records


def _load_sector_daily_rows(
    provider: AStockDataProvider,
    *,
    source: str,
    sector_code: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    dataset = "ths_daily" if source == "ths" else "dc_daily"
    fields = (
        ["ts_code", "trade_date", "close", "open", "high", "low", "pct_change", "vol", "turnover_rate"]
        if source == "ths"
        else ["ts_code", "trade_date", "name", "close", "open", "high", "low", "pct_change", "vol", "amount"]
    )
    return _fetch_tushare_stock_dataset(
        provider,
        dataset,
        params={"ts_code": sector_code, "start_date": start_date, "end_date": end_date},
        fields=fields,
        limit=6000,
    )


def _fetch_tushare_stock_dataset(
    provider: AStockDataProvider,
    dataset: str,
    *,
    params: dict[str, Any],
    fields: list[str],
    limit: int,
) -> dict[str, Any]:
    try:
        payload = provider.fetch_tushare_dataset(dataset, params, fields=fields, limit=limit)
    except Exception as exc:
        return {"dataset": dataset, "rows": [], "data_source": "unavailable", "missing_fields": [f"{dataset}: {exc}"]}
    if not isinstance(payload, dict):
        return {"dataset": dataset, "rows": [], "data_source": "unavailable", "missing_fields": [f"{dataset}: invalid_payload"]}
    payload.setdefault("rows", [])
    payload.setdefault("missing_fields", [])
    return payload


def _sector_daily_frame(rows: list[Any]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["trade_date", "close", "pct_change"])
    data = pd.DataFrame([row for row in rows if isinstance(row, dict)])
    if data.empty:
        return pd.DataFrame(columns=["trade_date", "close", "pct_change"])
    date_col = _first_existing_column(data, ("trade_date", "date"))
    close_col = _first_existing_column(data, ("close", "收盘"))
    if not date_col or not close_col:
        return pd.DataFrame(columns=["trade_date", "close", "pct_change"])
    pct_col = _first_existing_column(data, ("pct_change", "pct_chg", "chg_pct", "涨跌幅"))
    result = pd.DataFrame(
        {
            "trade_date": data[date_col].astype(str).str.replace("-", "", regex=False).str[:8],
            "close": pd.to_numeric(data[close_col], errors="coerce"),
            "pct_change": pd.to_numeric(data[pct_col], errors="coerce") if pct_col else None,
        }
    )
    return (
        result.dropna(subset=["trade_date", "close"])
        .sort_values("trade_date")
        .drop_duplicates(subset=["trade_date"], keep="last")
        .reset_index(drop=True)
    )


def _first_existing_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    for column in candidates:
        if column in frame.columns:
            return column
    return ""


def _clean_stock_basic(stock_basic: pd.DataFrame) -> pd.DataFrame:
    if stock_basic is None or stock_basic.empty:
        return pd.DataFrame(columns=["ts_code", "symbol", "name"])
    data = stock_basic.copy()
    for column in ("ts_code", "symbol", "name"):
        if column not in data.columns:
            data[column] = ""
        data[column] = data[column].fillna("").astype(str)
    data["ts_code"] = data["ts_code"].map(normalize_ts_code)
    data["symbol"] = data["symbol"].where(data["symbol"].astype(bool), data["ts_code"].str[:6])
    data = data[data["ts_code"].map(_is_a_share_ts_code)]
    return data.drop_duplicates(subset=["ts_code"]).reset_index(drop=True)


def _is_a_share_ts_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(value or "").strip().upper()))


def _period_message(query: str, period: str) -> str:
    if extract_period_return_requests(query):
        return query
    clean = str(period or "").strip().lower()
    if clean in {"6m", "6month", "6months"}:
        return f"{query} 6个月涨跌幅"
    if clean in {"half_year", "half-year", "半年"}:
        return f"{query} 半年涨跌幅"
    return f"{query} {clean or '6个月'}涨跌幅"


def _lookback_days_for_period_requests(requests: Any) -> int:
    days = 260
    for request in requests or ():
        unit = str(getattr(request, "unit", "") or "")
        amount = int(getattr(request, "amount", 0) or 0)
        if unit == "day":
            days = max(days, amount + 30)
        elif unit == "week":
            days = max(days, amount * 7 + 45)
        elif unit == "month":
            days = max(days, amount * 31 + 60)
        elif unit == "quarter":
            days = max(days, amount * 93 + 60)
        elif unit == "year":
            days = max(days, amount * 366 + 60)
    return days


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _clean_theme_label(query: str) -> str:
    text = str(query or "").strip()
    for suffix in ("相关股票", "相关个股", "A股股票", "股票", "个股", "涨跌幅情况", "涨跌幅"):
        text = text.replace(suffix, " ")
    return " ".join(text.split())


def _dedupe(values: Any) -> list[str]:
    result = []
    seen = set()
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _project_root(context: AgentToolContext) -> Path:
    return Path(getattr(context.settings, "project_root", "."))


def _context_payload(context: Any) -> Any:
    if context is None:
        return {}
    builder = getattr(context, "to_llm_context", None)
    if callable(builder):
        return builder()
    payload = getattr(context, "payload", None)
    if payload is not None:
        return payload
    to_dict = getattr(context, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {}
