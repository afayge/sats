from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from sats.agent.date_policy import agent_today, is_forecast_without_intraday, resolve_agent_time_context
from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema, ok
from sats.analysis.stock_llm_context import ensure_stock_analysis_data
from sats.backtesting.service import format_backtest_report, run_strategy_backtest
from sats.backtesting.strategy_spec import strategy_draft_python, strategy_spec_from_request, validate_strategy_spec
from sats.chat_artifacts import save_json_artifact, save_markdown_artifact
from sats.rag.chan_knowledge import search_chan_knowledge
from sats.signals import SignalInput, analyze_signal_inputs
from sats.stock_question import extract_stock_symbols
from sats.symbols import normalize_symbols


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
                    "dimensions": {"type": "array", "items": {"type": "string"}},
                }
            ),
            executor=_chat_registry_tool("get_a_share_market_context", "market_context"),
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
            executor=_chat_registry_tool("discover_a_share_opportunities", "opportunity_discovery"),
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


def _chat_registry_tool(name: str, data_name: str):
    def execute(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
        from sats.chat import ChatToolRegistry

        registry = ChatToolRegistry(list(context.skills), context.settings)
        text = registry.execute(name, arguments)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw": text}
        status = str(payload.get("status") or "done") if isinstance(payload, dict) else "done"
        return AgentToolResult(
            status="done" if status == "ok" else status,
            content=text,
            payload=payload if isinstance(payload, dict) else {"raw": payload},
            data_names=(data_name,),
        )

    return execute


def _stock_context(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    if not is_forecast_without_intraday(context.message, arguments):
        return _chat_registry_tool("get_stock_research_context", "stock_context")(context, arguments)
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    trade_date = str(arguments.get("trade_date") or agent_today())
    stock_contexts = ensure_stock_analysis_data(
        symbols,
        trade_date,
        settings=context.settings,
        storage=context.storage,
        periods=(),
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
        return _chat_registry_tool("run_internal_analysis", "internal_analysis")(context, arguments)
    symbols = normalize_symbols(arguments.get("symbols") or [], required=True)
    trade_date = str(arguments.get("trade_date") or agent_today())
    stock_contexts = ensure_stock_analysis_data(
        symbols,
        trade_date,
        settings=context.settings,
        storage=context.storage,
        periods=(),
        lookback_days=int(arguments.get("lookback_days") or 180),
    )
    if kind == "indicators":
        payload = {
            "kind": kind,
            "trade_date": trade_date,
            "forecast_horizons": list(resolve_agent_time_context(context.message, arguments=arguments).horizons),
            "indicators": [item.get("indicator_result", {}) for item in stock_contexts.values()],
            "missing_fields": _combined_missing_fields(stock_contexts),
        }
    else:
        inputs = [_signal_input_from_context(item) for item in stock_contexts.values()]
        run = analyze_signal_inputs(
            inputs,
            selected_signals=str(arguments.get("signals") or "short_up"),
            trade_date=trade_date,
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


def _stock_context_payload(
    context: AgentToolContext,
    symbols: list[str],
    trade_date: str,
    stock_contexts: dict[str, dict[str, Any]],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    horizons = list(resolve_agent_time_context(context.message, arguments=arguments).horizons)
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
        "trade_date": trade_date,
        "symbols": symbols,
        "requested_horizons": horizons,
        "data_policy": "SATS fetched real daily/quote data before calling the LLM; optional minute data was not required for this forecast.",
        "stocks": stocks,
    }


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


def _project_root(context: AgentToolContext) -> Path:
    return Path(getattr(context.settings, "project_root", "."))
