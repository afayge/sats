from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from sats.config import Settings, load_settings
from sats.data.astock_provider import AStockDataProvider
from sats.indicators import IndicatorCalculator, IndicatorInput, IndicatorResult
from sats.llm import ChatLLM
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols, parse_symbol_csv


PORTFOLIO_RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
SUPPORTED_MARKETS = (".SH", ".SZ")


@dataclass(frozen=True, slots=True)
class LLMDiagnostics:
    provider: str = ""
    profile: str = ""
    model: str = ""
    base_url: str = ""
    available: bool = True
    disabled: bool = False
    failure_stage: str = ""
    error_type: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def failed(self) -> bool:
        return bool(self.error_type or self.error_message)


@dataclass(frozen=True, slots=True)
class TradingCommitteeRequest:
    symbols: tuple[str, ...]
    trade_date: str
    lookback_days: int = 180
    debate_rounds: int = 1
    risk_rounds: int = 1
    report: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbols"] = list(self.symbols)
        return payload


@dataclass(frozen=True, slots=True)
class TradingCommitteeStockReport:
    ts_code: str
    name: str
    trade_date: str
    analyst_reports: dict[str, str]
    investment_debate: str
    investment_plan: str
    trader_proposal: str
    risk_debate: str
    final_decision: str
    final_rating: str
    missing_fields: tuple[str, ...] = ()
    data_sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["missing_fields"] = list(self.missing_fields)
        return payload


@dataclass(frozen=True, slots=True)
class TradingCommitteeResult:
    request: TradingCommitteeRequest
    reports: tuple[TradingCommitteeStockReport, ...] = ()
    message: str = ""
    llm_unavailable: bool = False
    llm_diagnostics: LLMDiagnostics = field(default_factory=LLMDiagnostics)
    markdown_report_path: Path | None = None
    json_artifact_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "reports": [item.to_dict() for item in self.reports],
            "message": self.message,
            "llm_unavailable": self.llm_unavailable,
            "llm_diagnostics": self.llm_diagnostics.to_dict(),
            "markdown_report_path": str(self.markdown_report_path or ""),
            "json_artifact_path": str(self.json_artifact_path or ""),
        }

    def to_markdown(self) -> str:
        if not self.reports:
            return self.message or "无投资委员会结果"
        lines = [
            "# SATS 投资委员会报告",
            "",
            "> 内容基于 SATS 已获取的真实结构化数据和明确标记的数据缺口生成，不构成投资建议。",
            "",
        ]
        if self.llm_unavailable:
            lines.extend([_llm_unavailable_notice(self.llm_diagnostics), ""])
        elif self.llm_diagnostics.disabled:
            lines.extend(["提示: 已按参数 --no-llm 跳过大模型，使用本地确定性摘要。", ""])
        for item in self.reports:
            lines.extend(_stock_markdown(item))
        return "\n".join(lines).rstrip() + "\n"


def run_trading_committee(
    symbols: list[str] | tuple[str, ...] | str,
    *,
    trade_date: str,
    settings: Settings | None = None,
    storage: DuckDBStorage | None = None,
    astock_provider: Any | None = None,
    lookback_days: int = 180,
    debate_rounds: int = 1,
    risk_rounds: int = 1,
    llm_enabled: bool = True,
    llm: Any | None = None,
    llm_timeout_seconds: int | None = None,
    report: bool = True,
    reports_dir: Path | None = None,
    progress: Any | None = None,
) -> TradingCommitteeResult:
    settings = settings or load_settings()
    storage = storage or DuckDBStorage(settings.db_path)
    provider = astock_provider or AStockDataProvider(settings)
    llm_timeout = _resolve_llm_timeout(settings, llm_timeout_seconds)
    llm_diagnostics = _llm_diagnostics_from_settings(settings, disabled=not llm_enabled)
    clean_symbols = (
        parse_symbol_csv(symbols, required=False)
        if isinstance(symbols, str)
        else normalize_symbols(symbols, required=False)
    )
    supported, rejected = _supported_common_a_symbols(clean_symbols)
    if rejected:
        raise ValueError(f"trading-committee v1 仅支持 A 股沪深普通股票，不支持: {', '.join(rejected)}")

    request = TradingCommitteeRequest(
        symbols=tuple(supported),
        trade_date=str(trade_date),
        lookback_days=int(lookback_days or 180),
        debate_rounds=max(1, int(debate_rounds or 1)),
        risk_rounds=max(1, int(risk_rounds or 1)),
        report=bool(report),
    )
    if not supported:
        return TradingCommitteeResult(request=request, message="无可分析股票", llm_diagnostics=llm_diagnostics)

    llm_client, llm_failure = _build_llm(
        llm,
        llm_enabled=llm_enabled,
        timeout_seconds=llm_timeout,
        diagnostics=llm_diagnostics,
    )

    if progress is None:
        inputs = provider.load_indicator_inputs(supported, request.trade_date, lookback_days=request.lookback_days, storage=storage)
        extra = _load_extra_context(provider, supported, request.trade_date, storage=storage)
    else:
        with progress.step("投委会数据") as step:
            inputs = provider.load_indicator_inputs(supported, request.trade_date, lookback_days=request.lookback_days, storage=storage)
            extra = _load_extra_context(provider, supported, request.trade_date, storage=storage)
            step.complete(message=f"{len(inputs)} 只")

    valid_inputs = [item for item in inputs if _has_daily_data(item)]
    if not valid_inputs:
        return TradingCommitteeResult(
            request=request,
            message="无可分析股票",
            llm_unavailable=bool(llm_failure),
            llm_diagnostics=llm_failure or llm_diagnostics,
        )

    reports: list[TradingCommitteeStockReport] = []
    if progress is None:
        for item in valid_inputs:
            stock_report, failure = _run_stock_committee(
                item,
                request=request,
                extra=extra,
                llm=llm_client,
                llm_timeout_seconds=llm_timeout,
                llm_diagnostics=llm_diagnostics,
            )
            llm_failure = _first_llm_failure(llm_failure, failure)
            reports.append(stock_report)
    else:
        with progress.step("分析师/研究员/风控团队", total=len(valid_inputs)) as step:
            for index, item in enumerate(valid_inputs, start=1):
                stock_report, failure = _run_stock_committee(
                    item,
                    request=request,
                    extra=extra,
                    llm=llm_client,
                    llm_timeout_seconds=llm_timeout,
                    llm_diagnostics=llm_diagnostics,
                )
                llm_failure = _first_llm_failure(llm_failure, failure)
                reports.append(stock_report)
                step.update(index, message=item.ts_code)

    result = TradingCommitteeResult(
        request=request,
        reports=tuple(reports),
        llm_unavailable=bool(llm_failure),
        llm_diagnostics=llm_failure or llm_diagnostics,
    )
    if report:
        if progress is None:
            result = write_trading_committee_artifacts(result, reports_dir=reports_dir or Path(settings.project_root) / "reports" / "trading_committee")
        else:
            with progress.step("投委会报告写入") as step:
                result = write_trading_committee_artifacts(result, reports_dir=reports_dir or Path(settings.project_root) / "reports" / "trading_committee")
                step.complete(message="done")
    return result


def write_trading_committee_artifacts(result: TradingCommitteeResult, *, reports_dir: Path) -> TradingCommitteeResult:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    codes = "_".join(symbol.split(".", 1)[0] for symbol in result.request.symbols) or "stocks"
    base = f"trading_committee_{result.request.trade_date}_{codes}_{stamp}"
    markdown_path = reports_dir / f"{base}.md"
    json_path = reports_dir / f"{base}.json"
    updated = replace(result, markdown_report_path=markdown_path, json_artifact_path=json_path)
    markdown_path.write_text(updated.to_markdown(), encoding="utf-8")
    json_path.write_text(json.dumps(updated.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return updated


def _run_stock_committee(
    item: IndicatorInput,
    *,
    request: TradingCommitteeRequest,
    extra: dict[str, Any],
    llm: Any | None,
    llm_timeout_seconds: int,
    llm_diagnostics: LLMDiagnostics,
) -> tuple[TradingCommitteeStockReport, LLMDiagnostics | None]:
    indicator = IndicatorCalculator().calculate(item)
    context = _StockCommitteeContext(item=item, indicator=indicator, extra=extra)
    analyst_reports, failure = _run_analysts(
        context,
        llm=llm,
        timeout=llm_timeout_seconds,
        diagnostics=llm_diagnostics,
    )
    active_llm = None if failure else llm
    debate, investment_plan, more_failure = _run_research_team(
        context,
        analyst_reports,
        llm=active_llm,
        timeout=llm_timeout_seconds,
        rounds=request.debate_rounds,
        diagnostics=llm_diagnostics,
    )
    failure = _first_llm_failure(failure, more_failure)
    active_llm = None if failure else active_llm
    trader_proposal, more_failure = _run_trader(
        context,
        analyst_reports,
        investment_plan,
        llm=active_llm,
        timeout=llm_timeout_seconds,
        diagnostics=llm_diagnostics,
    )
    failure = _first_llm_failure(failure, more_failure)
    active_llm = None if failure else active_llm
    risk_debate, final_decision, more_failure = _run_risk_team(
        context,
        analyst_reports,
        investment_plan,
        trader_proposal,
        llm=active_llm,
        timeout=llm_timeout_seconds,
        rounds=request.risk_rounds,
        diagnostics=llm_diagnostics,
    )
    failure = _first_llm_failure(failure, more_failure)
    return (
        TradingCommitteeStockReport(
            ts_code=item.ts_code,
            name=context.name,
            trade_date=request.trade_date,
            analyst_reports=analyst_reports,
            investment_debate=debate,
            investment_plan=investment_plan,
            trader_proposal=trader_proposal,
            risk_debate=risk_debate,
            final_decision=final_decision,
            final_rating=_extract_rating(final_decision),
            missing_fields=tuple(_dedupe(context.missing_fields)),
            data_sources=context.data_sources,
        ),
        failure,
    )


class _StockCommitteeContext:
    def __init__(self, *, item: IndicatorInput, indicator: IndicatorResult, extra: dict[str, Any]) -> None:
        self.item = item
        self.indicator = indicator
        self.extra = extra
        self.ts_code = item.ts_code
        self.name = str(item.stock_basic.get("name") or "")
        self.quote = _symbol_payload(extra.get("quotes"), item.ts_code)
        self.chip = _symbol_payload(extra.get("chips"), item.ts_code)
        self.fundamental_extra = _symbol_payload(extra.get("fundamentals"), item.ts_code)
        self.statements = _symbol_payload(extra.get("statements"), item.ts_code)
        self.holder_activity = _symbol_payload(extra.get("holder_activity"), item.ts_code)
        self.news = _symbol_payload(extra.get("company_news"), item.ts_code)
        self.social = _symbol_payload(extra.get("social_sentiment"), item.ts_code)
        self.hot_sectors = _stock_hot_sectors(extra.get("hot_sector"), item.ts_code)
        self.market_breadth = extra.get("breadth") or ({}, "unavailable")
        self.limit_sentiment = extra.get("limit_sentiment") or {}
        self.macro_news = extra.get("macro_news") if isinstance(extra.get("macro_news"), dict) else {}
        self.events = _symbol_payload(extra.get("events"), item.ts_code)
        self.technical_extra = _technical_extra(item.daily)
        self.missing_fields = self._missing_fields()
        self.data_sources = self._data_sources()

    def evidence_pack(self) -> dict[str, Any]:
        return _jsonable(
            {
                "ts_code": self.ts_code,
                "name": self.name,
                "stock_basic": self.item.stock_basic,
                "indicator": self.indicator.to_dict(),
                "technical_extra": self.technical_extra,
                "quote": self.quote,
                "chip": self.chip,
                "statements": self.statements,
                "fundamental_extra": self.fundamental_extra,
                "company_news": self.news,
                "macro_news": self.macro_news,
                "events": self.events,
                "holder_activity": self.holder_activity,
                "social_sentiment": self.social,
                "market_breadth": self.market_breadth,
                "limit_sentiment": self.limit_sentiment,
                "hot_sectors": self.hot_sectors,
                "missing_fields": self.missing_fields,
                "data_sources": self.data_sources,
            }
        )

    def compact_evidence(self) -> str:
        return json.dumps(_compact_payload(self.evidence_pack(), item_limit=6, text_limit=240), ensure_ascii=False, default=str)

    def _missing_fields(self) -> list[str]:
        missing: list[str] = []
        if self.indicator.close <= 0:
            missing.append("daily")
        for name, payload in (
            ("quote", self.quote),
            ("chip", self.chip),
            ("statements", self.statements),
            ("holder_activity", self.holder_activity),
            ("company_news", self.news),
            ("social_sentiment", self.social),
        ):
            missing.extend(_payload_missing(name, payload))
        if isinstance(self.extra.get("hot_sector"), dict):
            missing.extend(_payload_missing("hot_sector", self.extra.get("hot_sector") or {}))
        if not self.hot_sectors:
            missing.append("hot_sector_membership")
        if not isinstance(self.market_breadth, tuple) or not self.market_breadth[0]:
            missing.append("market_breadth")
        missing.extend(_payload_missing("macro_news", self.macro_news))
        return missing

    def _data_sources(self) -> dict[str, str]:
        sources = dict(self.item.data_sources or {})
        for name, payload in (
            ("quote", self.quote),
            ("chip", self.chip),
            ("statements", self.statements),
            ("fundamental_extra", self.fundamental_extra),
            ("company_news", self.news),
            ("macro_news", self.macro_news),
            ("events", self.events),
            ("holder_activity", self.holder_activity),
            ("social_sentiment", self.social),
        ):
            source = _payload_source(payload)
            if source:
                sources[name] = source
        if isinstance(self.market_breadth, tuple) and len(self.market_breadth) > 1:
            sources["market_breadth"] = str(self.market_breadth[1] or "unavailable")
        if self.limit_sentiment.get("data_source"):
            sources["limit_sentiment"] = str(self.limit_sentiment.get("data_source"))
        if isinstance(self.extra.get("hot_sector"), dict):
            sources["hot_sector"] = str((self.extra.get("hot_sector") or {}).get("data_source") or "available")
        return sources


def _run_analysts(
    context: _StockCommitteeContext,
    *,
    llm: Any | None,
    timeout: int,
    diagnostics: LLMDiagnostics,
) -> tuple[dict[str, str], LLMDiagnostics | None]:
    evidence = context.compact_evidence()
    prompts = {
        "market": "你是市场/技术分析师。只基于证据包分析趋势、量价、指标、资金流、市场环境和关键风险。",
        "fundamentals": "你是基本面分析师。只基于证据包分析财务质量、估值、资产负债、盈利和数据缺口。",
        "news": "你是新闻/事件分析师。只基于证据包分析公司新闻、公告、宏观新闻、事件催化和缺口。",
        "sentiment": "你是情绪分析师。只基于证据包分析市场热度、社交热榜、涨跌停情绪、分歧和置信度。",
    }
    fallbacks = {
        "market": _fallback_market_report(context),
        "fundamentals": _fallback_fundamental_report(context),
        "news": _fallback_news_report(context),
        "sentiment": _fallback_sentiment_report(context),
    }
    result: dict[str, str] = {}
    failure: LLMDiagnostics | None = None
    active_llm = llm
    for key, role in prompts.items():
        text, failed = _llm_or_fallback(
            active_llm,
            f"{role}\n\n证据包:\n{evidence}\n\n输出中文 Markdown，结论要克制，最后给出一个表格。",
            fallback=fallbacks[key],
            stage="analyst",
            timeout=timeout,
            diagnostics=diagnostics,
        )
        result[key] = text
        failure = _first_llm_failure(failure, failed)
        if failed:
            active_llm = None
    return result, failure


def _run_research_team(
    context: _StockCommitteeContext,
    reports: dict[str, str],
    *,
    llm: Any | None,
    timeout: int,
    rounds: int,
    diagnostics: LLMDiagnostics,
) -> tuple[str, str, LLMDiagnostics | None]:
    history = ""
    failure: LLMDiagnostics | None = None
    active_llm = llm
    for _ in range(max(1, rounds)):
        bull, failed = _llm_or_fallback(
            active_llm,
            _debate_prompt(context, reports, history, side="bull"),
            fallback=f"Bull Analyst: {_fallback_bull_case(context)}",
            stage="researcher",
            timeout=timeout,
            diagnostics=diagnostics,
        )
        failure = _first_llm_failure(failure, failed)
        if failed:
            active_llm = None
        history = f"{history}\n{_ensure_prefix(bull, 'Bull Analyst:')}".strip()
        bear, failed = _llm_or_fallback(
            active_llm,
            _debate_prompt(context, reports, history, side="bear"),
            fallback=f"Bear Analyst: {_fallback_bear_case(context)}",
            stage="researcher",
            timeout=timeout,
            diagnostics=diagnostics,
        )
        failure = _first_llm_failure(failure, failed)
        if failed:
            active_llm = None
        history = f"{history}\n{_ensure_prefix(bear, 'Bear Analyst:')}".strip()
    plan, failed = _llm_or_fallback(
        active_llm,
        "你是研究经理。根据分析师报告和多空辩论，给出清晰投资计划。评级只能使用 Buy / Overweight / Hold / Underweight / Sell。\n\n"
        f"标的: {context.ts_code} {context.name}\n\n分析师报告:\n{_reports_text(reports)}\n\n辩论历史:\n{history}",
        fallback=_fallback_investment_plan(context),
        stage="researcher",
        timeout=timeout,
        diagnostics=diagnostics,
    )
    return history, plan, _first_llm_failure(failure, failed)


def _run_trader(
    context: _StockCommitteeContext,
    reports: dict[str, str],
    investment_plan: str,
    *,
    llm: Any | None,
    timeout: int,
    diagnostics: LLMDiagnostics,
) -> tuple[str, LLMDiagnostics | None]:
    fallback = _fallback_trader_proposal(context)
    return _llm_or_fallback(
        llm,
        "你是交易员。把研究经理计划转换成 Buy/Hold/Sell 交易提案，包含理由、观察价位、止损/失效条件和仓位建议。"
        f"\n\n标的: {context.ts_code} {context.name}\n\n研究计划:\n{investment_plan}\n\n分析师报告:\n{_reports_text(reports)}",
        fallback=fallback,
        stage="trader",
        timeout=timeout,
        diagnostics=diagnostics,
    )


def _run_risk_team(
    context: _StockCommitteeContext,
    reports: dict[str, str],
    investment_plan: str,
    trader_proposal: str,
    *,
    llm: Any | None,
    timeout: int,
    rounds: int,
    diagnostics: LLMDiagnostics,
) -> tuple[str, str, LLMDiagnostics | None]:
    history = ""
    failure: LLMDiagnostics | None = None
    active_llm = llm
    for _ in range(max(1, rounds)):
        for role, fallback in (
            ("Aggressive Analyst", _fallback_aggressive_case(context)),
            ("Conservative Analyst", _fallback_conservative_case(context)),
            ("Neutral Analyst", _fallback_neutral_case(context)),
        ):
            text, failed = _llm_or_fallback(
                active_llm,
                _risk_prompt(context, reports, investment_plan, trader_proposal, history, role=role),
                fallback=f"{role}: {fallback}",
                stage="risk",
                timeout=timeout,
                diagnostics=diagnostics,
            )
            failure = _first_llm_failure(failure, failed)
            if failed:
                active_llm = None
            history = f"{history}\n{_ensure_prefix(text, role + ':')}".strip()
    decision, failed = _llm_or_fallback(
        active_llm,
        "你是组合经理。综合研究计划、交易提案和风控辩论，给最终交易决策。"
        "必须包含一行 `**Rating**: Buy/Overweight/Hold/Underweight/Sell`，并说明执行策略、仓位、风险边界和时间周期。"
        f"\n\n标的: {context.ts_code} {context.name}\n\n研究计划:\n{investment_plan}\n\n交易提案:\n{trader_proposal}\n\n风控辩论:\n{history}",
        fallback=_fallback_final_decision(context),
        stage="final",
        timeout=timeout,
        diagnostics=diagnostics,
    )
    return history, decision, _first_llm_failure(failure, failed)


def _load_extra_context(provider: Any, symbols: list[str], trade_date: str, *, storage: DuckDBStorage) -> dict[str, Any]:
    return {
        "quotes": _safe_call(lambda: provider.load_realtime_quote_lookup(symbols), {}),
        "chips": _safe_call(lambda: provider.load_chip_context(symbols), {}),
        "fundamentals": _safe_call(lambda: provider.load_fundamental_context(symbols), {}),
        "statements": _safe_call(lambda: provider.load_statement_context(symbols, trade_date=trade_date), {}),
        "company_news": _safe_call(lambda: provider.load_company_news_context(symbols, trade_date=trade_date), {}),
        "macro_news": _safe_call(lambda: provider.load_macro_news_context(trade_date=trade_date), {}),
        "holder_activity": _safe_call(lambda: provider.load_holder_activity_context(symbols, trade_date=trade_date), {}),
        "social_sentiment": _safe_call(lambda: provider.load_social_sentiment_context(symbols), {}),
        "events": _safe_call(lambda: provider.load_event_context(symbols=symbols, trade_date=trade_date), {}),
        "hot_sector": _unavailable_context(
            "hot_sector_context: skipped_by_default_for_terminal_latency",
            data_source="skipped_for_terminal_latency",
            trade_date=trade_date,
        ),
        "breadth": _safe_call(lambda: provider.load_market_breadth(), ({}, "unavailable")),
        "limit_sentiment": _safe_call(lambda: provider.load_limit_sentiment(trade_date, storage=storage), {}),
    }


def _fallback_market_report(context: _StockCommitteeContext) -> str:
    indicator = context.indicator
    tech = indicator.technical
    volume = indicator.volume
    return "\n".join(
        [
            f"### 市场分析师报告 - {context.ts_code} {context.name}",
            f"- 收盘/最新参考: {indicator.close:.2f}",
            f"- 均线结构: {(tech.get('ma') or {})}；50/200日/VWMA: {context.technical_extra}",
            f"- MACD: {(tech.get('macd') or {}).get('signal', '-')}; RSI: {(tech.get('rsi') or {})}; BOLL: {(tech.get('boll') or {})}",
            f"- 量能: {volume.get('status', '-')}; 资金流: {indicator.moneyflow}",
            f"- 支撑压力: {indicator.support_resistance}; 热点板块: {context.hot_sectors or 'unavailable'}",
        ]
    )


def _fallback_fundamental_report(context: _StockCommitteeContext) -> str:
    return "\n".join(
        [
            f"### 基本面分析师报告 - {context.ts_code} {context.name}",
            f"- 每日估值/基本面摘要: {_compact_payload(context.indicator.fundamentals)}",
            f"- 公司基础资料: {_compact_payload(context.item.stock_basic)}",
            f"- 财报明细: {_compact_payload(context.statements or {'status': 'unavailable'}, item_limit=4)}",
            f"- 补充基本面: {_compact_payload(context.fundamental_extra or {'status': 'unavailable'})}",
        ]
    )


def _fallback_news_report(context: _StockCommitteeContext) -> str:
    return "\n".join(
        [
            f"### 新闻/事件分析师报告 - {context.ts_code} {context.name}",
            f"- 公司新闻/公告: {_compact_payload(context.news or {'status': 'unavailable'}, item_limit=4)}",
            f"- 宏观新闻: {_compact_payload(context.macro_news or {'status': 'unavailable'}, item_limit=4)}",
            f"- 事件/股东活动: {_compact_payload(context.events or context.holder_activity or {'status': 'unavailable'}, item_limit=4)}",
        ]
    )


def _fallback_sentiment_report(context: _StockCommitteeContext) -> str:
    breadth = context.market_breadth[0] if isinstance(context.market_breadth, tuple) else {}
    return "\n".join(
        [
            f"### 情绪分析师报告 - {context.ts_code} {context.name}",
            f"- 社交/热榜命中: {_compact_payload(context.social or {'status': 'unavailable'}, item_limit=3)}",
            f"- 市场宽度: {_compact_payload(breadth or {'status': 'unavailable'})}",
            f"- 涨跌停情绪: {_compact_payload(context.limit_sentiment or {'status': 'unavailable'})}",
        ]
    )


def _fallback_bull_case(context: _StockCommitteeContext) -> str:
    reasons = []
    if str((context.indicator.technical.get("ma") or {})):
        reasons.append(f"技术结构有可观察趋势，收盘 {context.indicator.close:.2f}")
    if _num((context.indicator.moneyflow or {}).get("main_net_amount_5d")) > 0:
        reasons.append("近 5 日主力资金为正")
    if context.hot_sectors:
        reasons.append("所属热点板块有热度")
    return "；".join(reasons or ["可关注但证据不足，需等待更强确认"])


def _fallback_bear_case(context: _StockCommitteeContext) -> str:
    risks = []
    if context.missing_fields:
        risks.append(f"数据缺口较多: {', '.join(context.missing_fields[:5])}")
    if "空头" in str(context.indicator.technical.get("ma_alignment")):
        risks.append("均线结构偏弱")
    if _num((context.indicator.fundamentals or {}).get("pe")) <= 0:
        risks.append("估值/盈利指标不足")
    return "；".join(risks or ["主要风险是信号置信度不足，不能追高"])


def _fallback_investment_plan(context: _StockCommitteeContext) -> str:
    rating = _local_rating(context)
    return "\n".join(
        [
            f"**Recommendation**: {rating}",
            "",
            f"**Rationale**: 看多理由包括 {_fallback_bull_case(context)}。看空理由包括 {_fallback_bear_case(context)}。",
            "",
            "**Strategic Actions**: 仅作为观察名单处理，等待价格、量能和风险边界共同确认后再决策。",
        ]
    )


def _fallback_trader_proposal(context: _StockCommitteeContext) -> str:
    action = "Buy" if _local_rating(context) in {"Buy", "Overweight"} else ("Sell" if _local_rating(context) in {"Sell", "Underweight"} else "Hold")
    close = context.indicator.close
    support = (context.indicator.support_resistance or {}).get("support") or []
    stop = support[-1] if support else round(close * 0.97, 2)
    return "\n".join(
        [
            f"**Action**: {action}",
            "",
            f"**Reasoning**: 本地证据给出 {_local_rating(context)} 倾向，但仍需遵守风险边界。",
            "",
            f"**Entry Price**: {round(close, 2)}",
            f"**Stop Loss**: {stop}",
            "**Position Sizing**: 轻仓/观察仓，缺失新闻或财报证据时不加仓。",
            "",
            f"FINAL TRANSACTION PROPOSAL: **{action.upper()}**",
        ]
    )


def _fallback_aggressive_case(context: _StockCommitteeContext) -> str:
    return f"若趋势和资金继续共振，可小仓跟随；多头证据: {_fallback_bull_case(context)}"


def _fallback_conservative_case(context: _StockCommitteeContext) -> str:
    return f"优先保护本金，不因单一技术信号扩大仓位；风险: {_fallback_bear_case(context)}"


def _fallback_neutral_case(context: _StockCommitteeContext) -> str:
    return "当前适合把结论拆成观察条件、触发条件和失效条件，避免把不完整数据解释为确定性机会。"


def _fallback_final_decision(context: _StockCommitteeContext) -> str:
    rating = _local_rating(context)
    return "\n".join(
        [
            f"**Rating**: {rating}",
            "",
            f"**Executive Summary**: {context.ts_code} {context.name} 当前评级为 {rating}。执行上以条件单思维处理，不追高。",
            "",
            f"**Investment Thesis**: {_fallback_bull_case(context)}；主要约束是 {_fallback_bear_case(context)}。",
            "",
            "**Risk Boundary**: 若跌破关键支撑、量能恶化或新公告证伪基本面假设，应降低评级。",
        ]
    )


def _local_rating(context: _StockCommitteeContext) -> str:
    score = 50.0
    ma_text = str((context.indicator.technical or {}).get("ma_alignment") or "")
    macd_text = str(((context.indicator.technical or {}).get("macd") or {}).get("signal") or "")
    money_5d = _num((context.indicator.moneyflow or {}).get("main_net_amount_5d"))
    if "多头" in ma_text:
        score += 15
    if "空头" in ma_text:
        score -= 15
    if "金叉" in macd_text or "多头" in macd_text:
        score += 8
    if "死叉" in macd_text or "空头" in macd_text:
        score -= 8
    if money_5d > 0:
        score += 6
    if context.hot_sectors:
        score += 4
    score -= min(15, len(context.missing_fields) * 2)
    if score >= 75:
        return "Buy"
    if score >= 62:
        return "Overweight"
    if score <= 25:
        return "Sell"
    if score <= 38:
        return "Underweight"
    return "Hold"


def _debate_prompt(context: _StockCommitteeContext, reports: dict[str, str], history: str, *, side: str) -> str:
    role = "看多研究员" if side == "bull" else "看空研究员"
    focus = "强调增长潜力、优势、正面信号，并回应看空观点" if side == "bull" else "强调风险、弱点、负面信号，并回应看多观点"
    return (
        f"你是{role}，请{focus}。只引用给定报告，不得编造数据。\n\n"
        f"标的: {context.ts_code} {context.name}\n\n分析师报告:\n{_reports_text(reports)}\n\n辩论历史:\n{history or '暂无'}"
    )


def _risk_prompt(
    context: _StockCommitteeContext,
    reports: dict[str, str],
    investment_plan: str,
    trader_proposal: str,
    history: str,
    *,
    role: str,
) -> str:
    return (
        f"你是 {role}。请围绕交易员提案进行风控辩论，必须引用报告证据和风险边界，不得编造数据。\n\n"
        f"标的: {context.ts_code} {context.name}\n\n研究计划:\n{investment_plan}\n\n交易提案:\n{trader_proposal}"
        f"\n\n分析师报告:\n{_reports_text(reports)}\n\n当前风控辩论:\n{history or '暂无'}"
    )


def _reports_text(reports: dict[str, str]) -> str:
    return "\n\n".join(f"## {key}\n{value}" for key, value in reports.items())


def _llm_or_fallback(
    llm: Any | None,
    prompt: str,
    *,
    fallback: str,
    stage: str,
    timeout: int,
    diagnostics: LLMDiagnostics,
) -> tuple[str, LLMDiagnostics | None]:
    if llm is None:
        return fallback, None
    messages = [
        {
            "role": "system",
            "content": (
                "你是 SATS 投资委员会成员。只能基于用户给出的 SATS 证据包和报告推理；"
                "缺失数据必须明确说缺失，不得估算、补编或声称已经验证。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        response = llm.chat(messages, timeout=timeout)
    except Exception as exc:
        return fallback, _llm_error_diagnostics(diagnostics, stage=stage, exc=exc)
    content = _content_text(getattr(response, "content", response))
    return (content.strip() or fallback), None


def _build_llm(
    llm: Any | None,
    *,
    llm_enabled: bool,
    timeout_seconds: int,
    diagnostics: LLMDiagnostics,
) -> tuple[Any | None, LLMDiagnostics | None]:
    if not llm_enabled:
        return None, None
    if llm is not None:
        return llm, None
    try:
        return ChatLLM(timeout_seconds=timeout_seconds), None
    except Exception as exc:
        return None, _llm_error_diagnostics(diagnostics, stage="build", exc=exc)


def _resolve_llm_timeout(settings: Settings, value: int | None) -> int:
    if value is not None:
        return max(1, int(value))
    return max(1, int(getattr(settings, "llm_timeout_seconds", 120) or 120))


def _llm_diagnostics_from_settings(settings: Settings, *, disabled: bool = False) -> LLMDiagnostics:
    return LLMDiagnostics(
        provider=str(getattr(settings, "llm_provider", "") or ""),
        profile=str(getattr(settings, "llm_profile", "") or ""),
        model=str(getattr(settings, "openai_model", "") or ""),
        base_url=str(getattr(settings, "openai_base_url", "") or ""),
        available=not disabled,
        disabled=bool(disabled),
    )


def _llm_error_diagnostics(diagnostics: LLMDiagnostics, *, stage: str, exc: Exception) -> LLMDiagnostics:
    return replace(
        diagnostics,
        available=False,
        disabled=False,
        failure_stage=str(stage or ""),
        error_type=exc.__class__.__name__,
        error_message=_short_error_message(exc),
    )


def _first_llm_failure(current: LLMDiagnostics | None, candidate: LLMDiagnostics | None) -> LLMDiagnostics | None:
    if current is not None and current.failed:
        return current
    if candidate is not None and candidate.failed:
        return candidate
    return current or candidate


def _short_error_message(exc: Exception, *, limit: int = 500) -> str:
    text = str(exc or "").strip() or exc.__class__.__name__
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _llm_unavailable_notice(diagnostics: LLMDiagnostics) -> str:
    if diagnostics.failed:
        reason = f"{diagnostics.error_type}: {diagnostics.error_message}".strip(": ").rstrip("。.")
        stage = f"，阶段: {diagnostics.failure_stage}" if diagnostics.failure_stage else ""
        return f"提示: 大模型不可用{stage}，原因: {reason}。建议检查网络访问或 provider base_url，已使用本地确定性摘要。"
    return "提示: 大模型不可用，已使用本地确定性摘要。"


def _stock_markdown(item: TradingCommitteeStockReport) -> list[str]:
    lines = [
        f"## {item.ts_code} {item.name or ''}".rstrip(),
        "",
        f"- 交易日: {item.trade_date}",
        f"- 最终评级: {item.final_rating}",
        "",
        "### 最终决策",
        item.final_decision.strip(),
        "",
        "### 分析师团队",
    ]
    for key, title in (("market", "市场/技术"), ("fundamentals", "基本面"), ("news", "新闻/事件"), ("sentiment", "情绪")):
        lines.extend(["", f"#### {title}", item.analyst_reports.get(key, "unavailable").strip()])
    lines.extend(
        [
            "",
            "### 研究员团队",
            item.investment_debate.strip() or "无辩论记录",
            "",
            "### 研究经理计划",
            item.investment_plan.strip(),
            "",
            "### 交易员提案",
            item.trader_proposal.strip(),
            "",
            "### 风控团队",
            item.risk_debate.strip() or "无风控辩论记录",
            "",
            "### 数据来源与缺口",
        ]
    )
    if item.data_sources:
        lines.extend(f"- {key}: {value}" for key, value in sorted(item.data_sources.items()))
    if item.missing_fields:
        lines.append(f"- missing_fields: {', '.join(item.missing_fields)}")
    else:
        lines.append("- missing_fields: 无")
    lines.append("")
    return lines


def _technical_extra(daily: pd.DataFrame) -> dict[str, Any]:
    if daily is None or daily.empty:
        return {"status": "unavailable"}
    data = daily.copy().sort_values("trade_date")
    close = pd.to_numeric(data.get("close"), errors="coerce")
    vol = pd.to_numeric(data.get("vol"), errors="coerce") if "vol" in data.columns else pd.Series(dtype=float)
    result = {
        "ma50": _optional_num(close.rolling(50, min_periods=50).mean().iloc[-1] if len(close) else None),
        "ma200": _optional_num(close.rolling(200, min_periods=200).mean().iloc[-1] if len(close) else None),
        "vwma20": None,
    }
    if len(close) >= 20 and len(vol) >= len(close):
        denom = vol.rolling(20, min_periods=20).sum().iloc[-1]
        if pd.notna(denom) and float(denom) != 0:
            result["vwma20"] = _optional_num((close * vol).rolling(20, min_periods=20).sum().iloc[-1] / denom)
    return result


def _supported_common_a_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    supported = []
    rejected = []
    for symbol in normalize_symbols(symbols, required=False):
        if symbol.endswith(SUPPORTED_MARKETS):
            supported.append(symbol)
        else:
            rejected.append(symbol)
    return supported, rejected


def _has_daily_data(item: IndicatorInput) -> bool:
    return item.daily is not None and not item.daily.empty


def _safe_call(loader, default):
    try:
        value = loader()
    except Exception:
        return default
    return value if value is not None else default


def _unavailable_context(reason: str, **extra: Any) -> dict[str, Any]:
    return {"missing_fields": [reason], "data_sources": {}, **extra}


def _compact_payload(value: Any, *, item_limit: int = 3, text_limit: int = 180, depth: int = 4) -> Any:
    if depth <= 0:
        return _short_scalar(value, text_limit=text_limit)
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, child in value.items():
            if isinstance(child, list):
                compact[f"{key}_total"] = len(child)
                compact[key] = [_compact_payload(item, item_limit=item_limit, text_limit=text_limit, depth=depth - 1) for item in child[:item_limit]]
            elif isinstance(child, dict):
                compact[key] = _compact_payload(child, item_limit=item_limit, text_limit=text_limit, depth=depth - 1)
            else:
                compact[key] = _short_scalar(child, text_limit=text_limit)
        return compact
    if isinstance(value, list):
        return [_compact_payload(item, item_limit=item_limit, text_limit=text_limit, depth=depth - 1) for item in value[:item_limit]]
    return _short_scalar(value, text_limit=text_limit)


def _short_scalar(value: Any, *, text_limit: int) -> Any:
    if isinstance(value, str):
        text = re.sub(r"<[^>]+>", " ", value)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > text_limit:
            return text[:text_limit].rstrip() + "..."
        return text
    return value


def _symbol_payload(value: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    direct = value.get(symbol)
    if isinstance(direct, dict):
        return direct
    if value.get("items") is not None or value.get("data_source") is not None:
        return value
    return {}


def _stock_hot_sectors(value: Any, symbol: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    mapping = value.get("stock_hot_sectors")
    if isinstance(mapping, dict):
        items = mapping.get(symbol)
        return list(items) if isinstance(items, list) else []
    return []


def _payload_missing(name: str, payload: dict[str, Any]) -> list[str]:
    if not payload:
        return [name]
    missing = [str(item) for item in payload.get("missing_fields") or []]
    if str(payload.get("data_source") or "") == "unavailable" and not missing:
        missing.append(f"{name}:unavailable")
    if "items" in payload and not payload.get("items"):
        missing.append(f"{name}:empty")
    return missing


def _payload_source(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    source = payload.get("data_source")
    if source:
        return str(source)
    sources = payload.get("data_sources")
    if isinstance(sources, dict):
        return "+".join(str(value) for value in sources.values() if value)
    return ""


def _extract_rating(text: str) -> str:
    lowered = str(text or "")
    for rating in PORTFOLIO_RATINGS:
        if f"Rating**: {rating}" in lowered or f"Rating: {rating}" in lowered or f"评级: {rating}" in lowered:
            return rating
    for rating in PORTFOLIO_RATINGS:
        if rating.lower() in lowered.lower():
            return rating
    return "Hold"


def _ensure_prefix(text: str, prefix: str) -> str:
    value = str(text or "").strip()
    return value if value.startswith(prefix) else f"{prefix} {value}"


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(value)


def _optional_num(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float:
    try:
        if value is None or pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: list[str]) -> list[str]:
    result = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _days_before(trade_date: str, days: int) -> str:
    raw = str(trade_date)
    fmt = "%Y%m%d" if "-" not in raw else "%Y-%m-%d"
    return (datetime.strptime(raw, fmt) - timedelta(days=days)).strftime("%Y%m%d")
