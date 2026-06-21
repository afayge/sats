from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

from sats.agent.date_policy import agent_today, is_forecast_without_intraday, resolve_agent_time_context
from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok
from sats.analysis.market_llm_context import SUPPORTED_MARKET_DIMENSIONS
from sats.analysis.stock_llm_context import ensure_stock_analysis_data, extract_period_return_requests
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
from sats.chat_artifacts import save_json_artifact, save_markdown_artifact
from sats.data.astock_provider import AStockDataProvider
from sats.deep_analysis import run_deep_analysis
from sats.memory import ChatMemoryStore
from sats.rag.chan_knowledge import search_chan_knowledge
from sats.serenity import run_serenity_screen
from sats.signals import SignalInput, analyze_signal_inputs
from sats.stock_basic_lookup import match_stock_name
from sats.stock_question import StockQuestion, extract_stock_symbols
from sats.symbols import normalize_symbols, normalize_ts_code
from sats.web import search as web_search


THEME_STOCK_RETURN_LIMIT = 30


def research_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="research.market_context",
            description="获取真实 A 股大盘指数、市场宽度、涨跌停情绪、热点板块上下文。",
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
            side_effect="write_artifact",
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
                    "report": {"type": "boolean"},
                }
            ),
            executor=_serenity_screen,
        ),
        AgentToolSpec(
            name="research.discover_opportunities",
            description="运行 SATS 自然语言 A 股机会发现，返回候选排序并可生成报告。",
            category="research_workflow",
            side_effect="write_artifact",
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
                    "trade_date": {"type": "string"},
                    "period": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["query"],
            ),
            executor=_theme_stock_returns,
        ),
        AgentToolSpec(
            name="research.internal_analysis",
            description="运行 SATS 白名单内部分析：指标、信号分析、native DSA、因子摘要。",
            category="research",
            side_effect="readonly",
            timeout=90,
            input_schema=object_schema(
                {
                    "kind": {"type": "string", "enum": ["indicators", "analyze_signals", "native_dsa", "factor_summary"]},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "trade_date": {"type": "string"},
                    "signals": {"type": "string"},
                    "profile": {"type": "string"},
                    "lookback_days": {"type": "integer"},
                    "horizons": {"type": "array", "items": {"type": "string"}},
                },
                ["kind", "symbols"],
            ),
            executor=_internal_analysis,
        ),
        AgentToolSpec(
            name="research.deep_stock_analysis",
            description="运行 SATS 原生 A 股个股深研闭环：采集、评分、投资人面板、综合摘要和报告 artifact。",
            category="research_workflow",
            side_effect="write_artifact",
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
            description="生成受限 SATS 策略 spec 和可读策略草稿产物。",
            category="research_workflow",
            side_effect="write_artifact",
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
            side_effect="write_artifact",
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
        progress=None,
    )
    payload = _context_payload(result)
    legacy = result.discovery.to_llm_context() if hasattr(result, "discovery") else payload
    artifacts: list[dict[str, Any]] = []
    report_path = str(getattr(result, "report_path", "") or "").strip()
    if report_path:
        artifacts.append({"kind": "report", "title": "opportunity_report", "path": report_path, "mime_type": "text/markdown"})
    result_payload = {"status": "ok", "stock_picking_agent": payload, "opportunity_discovery": legacy}
    return AgentToolResult(
        status="done",
        content=json.dumps(result_payload, ensure_ascii=False, default=str),
        payload=result_payload,
        data_names=("opportunity_discovery",),
        artifacts=tuple(artifacts),
    )


def _serenity_screen(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    symbols = normalize_symbols(arguments.get("symbols") or [], required=False)
    report = bool(arguments.get("report", True))
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
    result = replace(result, request=replace(result.request, report=report))
    payload = {"status": "ok", "serenity_screen": result.to_dict()}
    artifacts: list[dict[str, Any]] = []
    if report and result.candidates:
        json_write = save_json_artifact(
            project_root=_project_root(context),
            session_id=context.session_id,
            turn_id=context.turn_id or "agent",
            title="serenity_screen",
            payload=result.to_dict(),
            filename="serenity_screen.json",
            summary="SATS Serenity AI 卡位筛选 JSON",
        )
        markdown_write = save_markdown_artifact(
            project_root=_project_root(context),
            session_id=context.session_id,
            turn_id=context.turn_id or "agent",
            title="SATS Serenity AI 卡位筛选",
            content=result.to_markdown(),
            filename="serenity_screen.md",
            summary="SATS Serenity AI 卡位筛选 Markdown",
        )
        artifacts.extend([_artifact_dict(context, json_write), _artifact_dict(context, markdown_write)])
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=("serenity_screen",),
        artifacts=tuple(artifacts),
    )


def _theme_stock_returns(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    query = str(arguments.get("query") or context.message or "").strip()
    theme = str(arguments.get("theme") or "").strip()
    trade_date = str(arguments.get("trade_date") or "").strip() or agent_today()
    period = str(arguments.get("period") or "").strip()
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
    candidates = _candidates_from_web_search(web_payload)
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
    payload = {
        "status": "ok",
        "theme_stock_returns": _drop_empty(
            {
                "query": query,
                "theme": theme or _clean_theme_label(query),
                "period": period_requests[0].key if period_requests else period or "6m",
                "trade_date": trade_date,
                "candidate_count": len(validated),
                "stocks": stocks,
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


def _stock_context(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
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
    stock_contexts = ensure_stock_analysis_data(
        symbols,
        trade_date,
        settings=context.settings,
        storage=context.storage,
        periods=(),
        period_requests=extract_period_return_requests(context.message),
    )
    payload = _stock_context_payload(context, symbols, trade_date, stock_contexts, arguments)
    return AgentToolResult(
        status="done",
        content=json.dumps({"status": "ok", "stock_context": payload}, ensure_ascii=False, default=str),
        payload={"status": "ok", "stock_context": payload},
        data_names=("stock_context",),
    )


def _internal_analysis(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    kind = str(arguments.get("kind") or "").strip()
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
    artifacts: list[dict[str, Any]] = []
    if result.analyses:
        json_write = save_json_artifact(
            project_root=_project_root(context),
            session_id=context.session_id,
            turn_id=context.turn_id or "agent",
            title="deep_stock_analysis",
            payload=result.to_dict(),
            filename="deep_stock_analysis.json",
            summary="SATS 原生个股深研 JSON",
        )
        markdown_write = save_markdown_artifact(
            project_root=_project_root(context),
            session_id=context.session_id,
            turn_id=context.turn_id or "agent",
            title="SATS 原生个股深研报告",
            content=result.to_markdown(),
            filename="deep_stock_analysis.md",
            summary="SATS 原生个股深研 Markdown",
        )
        artifacts.extend([_artifact_dict(context, json_write), _artifact_dict(context, markdown_write)])
    return AgentToolResult(
        status="done",
        content=json.dumps(payload, ensure_ascii=False, default=str),
        payload=payload,
        data_names=("deep_stock_analysis",),
        artifacts=tuple(artifacts),
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
) -> dict[str, Any]:
    horizons = list(resolve_agent_time_context(context.message, arguments=arguments).horizons)
    effective_trade_date = _effective_stock_context_trade_date(stock_contexts, trade_date)
    stocks = []
    for symbol in symbols:
        item = dict(stock_contexts.get(symbol) or {})
        missing = list(item.get("missing_fields") or [])
        for field in ("optional_minute_15m", "optional_minute_30m"):
            if field not in missing:
                missing.append(field)
        item["missing_fields"] = missing
        item.setdefault("minute_curves", {})
        stocks.append(item)
    return {
        "user_question": context.message,
        "requested_trade_date": trade_date,
        "trade_date": effective_trade_date,
        "symbols": symbols,
        "requested_horizons": horizons,
        "data_policy": "SATS fetched real daily/quote data before calling the LLM; optional minute data was not required for this forecast.",
        "stocks": stocks,
    }


def _effective_stock_context_trade_date(stock_contexts: dict[str, dict[str, Any]], fallback: str) -> str:
    dates = [str(item.get("trade_date") or "") for item in stock_contexts.values() if isinstance(item, dict) and item.get("trade_date")]
    return max(dates) if dates else fallback


def _signal_input_from_context(context: dict[str, Any]) -> SignalInput:
    return SignalInput(
        ts_code=str(context.get("ts_code") or ""),
        trade_date=str(context.get("trade_date") or ""),
        daily=pd.DataFrame(context.get("daily_tail") or []),
        stock_basic={"name": context.get("name") or ""},
    )


def _combined_missing_fields(stock_contexts: dict[str, dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for item in stock_contexts.values():
        for field in item.get("missing_fields") or []:
            if field not in fields:
                fields.append(str(field))
    for field in ("optional_minute_15m", "optional_minute_30m"):
        if field not in fields:
            fields.append(field)
    return fields


def _chan_kb_search(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    rows = search_chan_knowledge(str(arguments.get("query") or ""), limit=max(1, int(arguments.get("limit") or 6)))
    payload = {"results": rows}
    return ok(json.dumps(payload, ensure_ascii=False, default=str), payload=payload, data_names=("Chan KB",))


def _strategy_draft(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    request = str(arguments.get("request") or context.message or "")
    symbols = normalize_symbols(arguments.get("symbols") or extract_stock_symbols(request), required=False)
    spec = strategy_spec_from_request(request, symbols=symbols)
    artifacts = _write_strategy_artifacts(context, spec, run_backtest=False)
    return ok("策略草稿已生成。", payload={"spec": spec.to_dict()}, data_names=("策略草稿",), artifacts=tuple(artifacts))


def _backtest(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    if isinstance(arguments.get("spec"), dict) and arguments.get("spec"):
        spec = validate_strategy_spec(arguments["spec"])
    else:
        request = str(arguments.get("request") or context.message or "")
        symbols = normalize_symbols(arguments.get("symbols") or extract_stock_symbols(request), required=False)
        spec = strategy_spec_from_request(request, symbols=symbols)
    result = run_strategy_backtest(spec, settings=context.settings, storage=context.storage, resolver=context.resolver)
    artifacts = _write_strategy_artifacts(context, result.spec, run_backtest=False)
    report = save_markdown_artifact(
        project_root=_project_root(context),
        session_id=context.session_id,
        turn_id=context.turn_id or "agent",
        title="backtest_report",
        content=format_backtest_report(result),
        filename="backtest_report.md",
        report=True,
        summary="轻量回测报告",
    )
    metrics = save_json_artifact(
        project_root=_project_root(context),
        session_id=context.session_id,
        turn_id=context.turn_id or "agent",
        title="backtest_metrics",
        payload=result.to_dict(),
        filename="backtest_metrics.json",
        summary="轻量回测指标",
    )
    artifacts.extend([_artifact_dict(context, report), _artifact_dict(context, metrics)])
    return ok(
        format_backtest_report(result),
        payload={"backtest": result.to_dict()},
        data_names=("轻量回测",),
        artifacts=tuple(artifacts),
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


def _write_strategy_artifacts(context: AgentToolContext, spec: Any, *, run_backtest: bool) -> list[dict[str, Any]]:
    spec_write = save_json_artifact(
        project_root=_project_root(context),
        session_id=context.session_id,
        turn_id=context.turn_id or "agent",
        title="strategy_spec",
        payload=spec.to_dict(),
        filename="strategy_spec.json",
        summary="受限策略 spec",
    )
    draft_write = save_markdown_artifact(
        project_root=_project_root(context),
        session_id=context.session_id,
        turn_id=context.turn_id or "agent",
        title="strategy_draft.py",
        content=strategy_draft_python(spec),
        filename="strategy_draft.py.md",
        report=False,
        summary="可读策略草稿，不由 agent 直接执行",
    )
    return [_artifact_dict(context, spec_write), _artifact_dict(context, draft_write)]


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
