from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from sats.config import Settings, load_settings
from sats.analysis.dsa_decision import build_local_dsa_decision, decision_type_for_advice, normalize_operation_advice
from sats.data.astock_provider import AStockDataProvider
from sats.indicators import IndicatorCalculator, IndicatorInput, IndicatorResult
from sats.llm import ChatLLM
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_BSE_PREFIXES = ("43", "81", "82", "83", "87", "88", "92")


@dataclass(frozen=True, slots=True)
class DsaAnalysisRanking:
    code: str
    name: str
    score: float
    advice: str
    trend: str
    decision_type: str = ""
    confidence_level: str = ""
    raw_advice: str = ""
    rating_adjustment: str = ""
    external_supported: bool = True
    external_skip_reason: str = ""


@dataclass(frozen=True, slots=True)
class DsaStockAnalysis:
    ts_code: str
    name: str
    score: float
    advice: str
    trend: str
    summary: str
    risk: str
    indicator: dict[str, Any]
    quote: dict[str, Any]
    chip: dict[str, Any]
    akshare_context: dict[str, Any]
    dashboard: dict[str, Any]
    context_pack: dict[str, Any]
    missing_fields: list[str]
    market_phase: dict[str, Any]
    hot_sectors: list[dict[str, Any]]
    data_sources: dict[str, str]
    decision_type: str = ""
    confidence_level: str = ""
    signal_reasons: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)
    raw_score: float = 0.0
    raw_advice: str = ""
    rating_adjustment: str = ""
    external_supported: bool = True
    external_skip_reason: str = ""
    llm_unavailable: bool = False
    llm_disabled: bool = False


@dataclass(frozen=True, slots=True)
class DsaAnalysisRunResult:
    analyzed_codes: list[str]
    skipped_codes: list[str]
    rankings: list[DsaAnalysisRanking]
    source_report: Path | None
    archived_report: Path | None
    message: str = ""
    analyses: list[DsaStockAnalysis] = field(default_factory=list)
    llm_unavailable: bool = False


def run_dsa_analysis(
    symbols: Sequence[str],
    *,
    trade_date: str | None = None,
    reports_dir: Path | None = None,
    settings: Settings | None = None,
    storage: DuckDBStorage | None = None,
    lookback_days: int = 180,
    source_label: str = "stocks",
    llm: Any | None = None,
    llm_timeout_seconds: int = 20,
    llm_enabled: bool = True,
    astock_provider: Any | None = None,
    tickflow_provider: Any | None = None,
    tushare_provider: Any | None = None,
    akshare_provider: Any | None = None,
    progress: Any | None = None,
) -> DsaAnalysisRunResult:
    settings = settings or load_settings()
    storage = storage or DuckDBStorage(settings.db_path)
    reports_dir = reports_dir or settings.project_root / "reports"
    clean_symbols = normalize_symbols(symbols, required=False)
    if not clean_symbols:
        return DsaAnalysisRunResult([], [], [], None, None, message="无可分析股票")
    trade_date = trade_date or _today()

    provider = astock_provider or AStockDataProvider(
        settings,
        tickflow_provider=tickflow_provider,
        tushare_provider=tushare_provider,
        akshare_provider=akshare_provider,
    )

    if progress is None:
        inputs = provider.load_indicator_inputs(
            clean_symbols,
            trade_date,
            lookback_days=lookback_days,
            storage=storage,
        )
    else:
        with progress.step("DSA 股票数据") as step:
            inputs = provider.load_indicator_inputs(
                clean_symbols,
                trade_date,
                lookback_days=lookback_days,
                storage=storage,
            )
            step.complete(message=f"{len(inputs)} 只")
    if progress is None:
        quotes = _load_quote_lookup(clean_symbols, astock_provider=provider)
    else:
        with progress.step("实时行情") as step:
            quotes = _load_quote_lookup(clean_symbols, astock_provider=provider)
            step.complete()
    if progress is None:
        chips = _safe_provider_dict(lambda: provider.load_chip_context(clean_symbols))
        akshare_context = _safe_provider_dict(lambda: provider.load_fundamental_context(clean_symbols))
    else:
        with progress.step("AStock 补充数据") as step:
            chips = _safe_provider_dict(lambda: provider.load_chip_context(clean_symbols))
            akshare_context = _safe_provider_dict(lambda: provider.load_fundamental_context(clean_symbols))
            step.complete()
    if progress is None:
        hot_sector_context, hot_sector_missing = _safe_hot_sector_context(provider, storage, trade_date)
    else:
        with progress.step("DSA 热点板块") as step:
            hot_sector_context, hot_sector_missing = _safe_hot_sector_context(provider, storage, trade_date)
            step.complete(message="可用" if hot_sector_context else "缺失")
    market_phase = _market_phase_context(trade_date)
    stock_hot_sector_map = hot_sector_context.get("stock_hot_sectors", {}) if isinstance(hot_sector_context, dict) else {}

    calculator = IndicatorCalculator()
    chat_llm = None
    if llm_enabled:
        chat_llm = llm if llm is not None else _build_optional(lambda: ChatLLM(timeout_seconds=llm_timeout_seconds))
    llm_failed = chat_llm is None and llm_enabled
    analyses: list[DsaStockAnalysis] = []
    analysis_step = None
    if progress is not None:
        label = f"{getattr(settings, 'openai_model', 'LLM')} DSA复核" if llm_enabled else "DSA 指标评级"
        analysis_step = progress.step(label, total=len(inputs))
    for index, item in enumerate(inputs, start=1):
        indicator = calculator.calculate(item)
        quote = quotes.get(item.ts_code, {})
        chip = chips.get(item.ts_code, {})
        extra = akshare_context.get(item.ts_code, {})
        hot_sectors = _stock_hot_sectors(stock_hot_sector_map, item.ts_code)
        local_decision = build_local_dsa_decision(indicator, chip=chip)
        local_payload = local_decision.to_dict()
        context_pack = _build_context_pack(
            indicator,
            quote=quote,
            chip=chip,
            akshare_context=extra,
            hot_sectors=hot_sectors,
            market_phase=market_phase,
            hot_sector_missing=hot_sector_missing,
        )
        llm_payload = None
        llm_unavailable = False
        if llm_enabled and not llm_failed:
            llm_payload = _llm_stock_payload(
                indicator,
                quote=quote,
                chip=chip,
                akshare_context=extra,
                context_pack=context_pack,
                local_decision=local_payload,
                llm=chat_llm,
                timeout_seconds=llm_timeout_seconds,
            )
            if llm_payload is None:
                llm_failed = True
                llm_unavailable = True
        elif llm_enabled and llm_failed:
            llm_unavailable = True
        if llm_payload is None:
            llm_payload = _fallback_stock_payload(indicator, quote=quote, chip=chip, local_decision=local_payload)
        final_payload = _final_stock_payload(local_payload, llm_payload)
        dashboard = _build_dashboard(
            indicator,
            final_payload=final_payload,
            quote=quote,
            chip=chip,
            akshare_context=extra,
            hot_sectors=hot_sectors,
            market_phase=market_phase,
            missing_fields=context_pack["missing_fields"],
            local_decision=local_payload,
        )
        external_supported, external_reason = _daily_stock_analysis_support(item.ts_code)
        analyses.append(
            DsaStockAnalysis(
                ts_code=item.ts_code,
                name=indicator.name or str(quote.get("name") or extra.get("name") or ""),
                score=float(final_payload["score"]),
                advice=str(final_payload["advice"]),
                trend=str(final_payload["trend"]),
                summary=str(final_payload["summary"]),
                risk=str(final_payload["risk"]),
                indicator=indicator.to_dict(),
                quote=quote,
                chip=chip,
                akshare_context=extra,
                dashboard=dashboard,
                context_pack=context_pack,
                missing_fields=list(context_pack["missing_fields"]),
                market_phase=market_phase,
                hot_sectors=hot_sectors,
                data_sources=dict(context_pack.get("data_sources") or _analysis_sources(indicator, quote, chip, extra)),
                decision_type=str(final_payload["decision_type"]),
                confidence_level=str(final_payload["confidence_level"]),
                signal_reasons=list(final_payload["signal_reasons"]),
                risk_factors=list(final_payload["risk_factors"]),
                raw_score=float(final_payload["raw_score"]),
                raw_advice=str(final_payload["raw_advice"]),
                rating_adjustment=str(final_payload["rating_adjustment"]),
                external_supported=external_supported,
                external_skip_reason=external_reason,
                llm_unavailable=llm_unavailable,
                llm_disabled=not llm_enabled,
            )
        )
        if analysis_step is not None:
            analysis_step.update(index)
    if analysis_step is not None and not analysis_step.done:
        analysis_step.complete()

    analyses = sorted(analyses, key=lambda item: (0 if item.external_supported else 1, -item.score, item.ts_code))
    rankings = [
        DsaAnalysisRanking(
            code=item.ts_code,
            name=item.name,
            score=item.score,
            advice=item.advice,
            trend=item.trend,
            decision_type=item.decision_type,
            confidence_level=item.confidence_level,
            raw_advice=item.raw_advice,
            rating_adjustment=item.rating_adjustment,
            external_supported=item.external_supported,
            external_skip_reason=item.external_skip_reason,
        )
        for item in analyses
    ]
    if progress is None:
        report_path = _write_report(analyses, trade_date=trade_date, reports_dir=reports_dir, source_label=source_label)
    else:
        with progress.step("报告生成", total=1) as step:
            report_path = _write_report(analyses, trade_date=trade_date, reports_dir=reports_dir, source_label=source_label)
            step.update(1)
    return DsaAnalysisRunResult(
        analyzed_codes=[item.ts_code for item in analyses],
        skipped_codes=[],
        rankings=rankings,
        source_report=report_path,
        archived_report=report_path,
        analyses=analyses,
        llm_unavailable=any(item.llm_unavailable for item in analyses),
    )


def _load_indicator_inputs(
    symbols: list[str],
    trade_date: str,
    *,
    lookback_days: int,
    storage: DuckDBStorage,
    tickflow_provider: Any | None,
    tushare_provider: Any | None,
) -> list[IndicatorInput]:
    tick_items = _safe_provider_list(
        lambda: tickflow_provider.load_indicator_inputs(symbols, trade_date, lookback_days=lookback_days, storage=storage)
    ) if tickflow_provider is not None else []
    tushare_items = _safe_provider_list(
        lambda: tushare_provider.load_indicator_inputs(symbols, trade_date, lookback_days=lookback_days, storage=storage)
    ) if tushare_provider is not None else []

    tick_lookup = _input_lookup(tick_items)
    tushare_lookup = _input_lookup(tushare_items)
    result: list[IndicatorInput] = []
    for ts_code in symbols:
        tick = tick_lookup.get(ts_code)
        tushare = tushare_lookup.get(ts_code)
        if tick is None and tushare is None:
            result.append(_cache_indicator_input(ts_code, trade_date, storage=storage))
            continue
        result.append(_merge_indicator_input(ts_code, trade_date, tick, tushare))
    return result


def _merge_indicator_input(
    ts_code: str,
    trade_date: str,
    tick: IndicatorInput | None,
    tushare: IndicatorInput | None,
) -> IndicatorInput:
    daily_source = "unavailable"
    daily = _empty_frame()
    if tick is not None and not _is_empty(tick.daily):
        daily = tick.daily
        daily_source = tick.data_sources.get("daily", "tickflow_daily")
    elif tushare is not None and not _is_empty(tushare.daily):
        daily = tushare.daily
        daily_source = tushare.data_sources.get("daily", "tushare_daily")

    daily_basic_source = "unavailable"
    daily_basic = _empty_frame()
    if tushare is not None and not _is_empty(tushare.daily_basic):
        daily_basic = tushare.daily_basic
        daily_basic_source = tushare.data_sources.get("daily_basic", "tushare_daily_basic")
    elif tick is not None and not _is_empty(tick.daily_basic):
        daily_basic = tick.daily_basic
        daily_basic_source = tick.data_sources.get("daily_basic", "tickflow_realtime_basic_like")

    moneyflow = tushare.moneyflow if tushare is not None and not _is_empty(tushare.moneyflow) else _empty_frame()
    fundamentals = tushare.fundamentals if tushare is not None and not _is_empty(tushare.fundamentals) else _empty_frame()
    stock_basic = _merge_dicts(
        tick.stock_basic if tick is not None else {},
        tushare.stock_basic if tushare is not None else {},
    )
    return IndicatorInput(
        ts_code=ts_code,
        trade_date=trade_date,
        daily=daily,
        daily_basic=daily_basic,
        moneyflow=moneyflow,
        fundamentals=fundamentals,
        stock_basic=stock_basic,
        data_sources={
            "daily": daily_source,
            "daily_basic": daily_basic_source,
            "moneyflow": tushare.data_sources.get("moneyflow", "tushare_moneyflow_dc") if tushare is not None and not _is_empty(moneyflow) else "unavailable",
            "fundamentals": tushare.data_sources.get("fundamentals", "tushare_fundamentals") if tushare is not None and not _is_empty(fundamentals) else "unavailable",
        },
    )


def _cache_indicator_input(ts_code: str, trade_date: str, *, storage: DuckDBStorage) -> IndicatorInput:
    dates = _calendar_dates(trade_date, 400)
    daily = storage.get_stock_daily(dates)
    if not daily.empty:
        daily = daily[daily["ts_code"].astype(str) == ts_code]
    daily_basic = storage.get_stock_daily_basic(dates)
    if not daily_basic.empty:
        daily_basic = daily_basic[daily_basic["ts_code"].astype(str) == ts_code]
    stock_basic = storage.get_stock_basic()
    stock_row = {}
    if not stock_basic.empty:
        rows = stock_basic[stock_basic["ts_code"].astype(str) == ts_code]
        if not rows.empty:
            stock_row = rows.iloc[-1].dropna().to_dict()
    return IndicatorInput(
        ts_code=ts_code,
        trade_date=trade_date,
        daily=daily,
        daily_basic=daily_basic,
        moneyflow=storage.get_stock_moneyflow([ts_code], start_date=dates[0], end_date=trade_date),
        fundamentals=storage.get_stock_fundamentals([ts_code], as_of=trade_date),
        stock_basic=stock_row,
        data_sources={
            "daily": "duckdb_cache_or_unavailable",
            "daily_basic": "duckdb_cache_or_unavailable",
            "moneyflow": "duckdb_cache_or_unavailable",
            "fundamentals": "duckdb_cache_or_unavailable",
        },
    )


def _load_quote_lookup(
    symbols: list[str],
    *,
    astock_provider: Any | None = None,
    tickflow_provider: Any | None = None,
    akshare_provider: Any | None = None,
) -> dict[str, dict[str, Any]]:
    if astock_provider is not None:
        frame = _safe_provider_frame(lambda: astock_provider.load_realtime_quotes(symbols=symbols))
        return _records_by_symbol(frame)
    lookup: dict[str, dict[str, Any]] = {}
    tick_quotes = _safe_provider_frame(lambda: tickflow_provider.load_realtime_quotes(symbols=symbols)) if tickflow_provider is not None else _empty_frame()
    for row in _records_by_symbol(tick_quotes).values():
        lookup[str(row.get("ts_code"))] = row
    ak_quotes = _safe_provider_frame(lambda: akshare_provider.load_realtime_quotes(symbols)) if akshare_provider is not None else _empty_frame()
    for ts_code, row in _records_by_symbol(ak_quotes).items():
        current = lookup.get(ts_code, {})
        lookup[ts_code] = _merge_dicts(current, row)
    return lookup


def _llm_stock_payload(
    indicator: IndicatorResult,
    *,
    quote: dict[str, Any],
    chip: dict[str, Any],
    akshare_context: dict[str, Any],
    context_pack: dict[str, Any],
    local_decision: dict[str, Any],
    llm: Any | None,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    if llm is None:
        return None
    context = {
        "trade_date": indicator.trade_date,
        "data_sources": dict(indicator.data_sources or {}),
        "missing_fields": list(context_pack.get("missing_fields") or []),
        "indicator": indicator.to_dict(),
        "quote": quote,
        "chip": chip,
        "akshare_context": akshare_context,
        "dsa_context_pack": context_pack,
        "local_rule_rating": local_decision,
        "news": {"status": "unavailable", "reason": "新闻/舆情未启用"},
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是 SATS 原生 DSA 股票分析器。基于给定结构化行情、技术指标、资金流和基本面，"
                "输出严格 JSON。只能使用用户提供的 trade_date、data_sources、indicator、quote、dsa_context_pack 等真实结构化数据；"
                "不要声称已读取未提供的数据，不得编造价格、涨跌幅、成交量、新闻、公告、题材或基本面。"
                "字段缺失时必须在风险提示中说明。投资相关内容必须包含风险提示。"
                "operation_advice 必须是以下之一：强烈买入、买入、持有、观望、减仓、卖出、强烈卖出。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请分析这只股票，返回 JSON 字段：sentiment_score(0-100数字), "
                "operation_advice(强烈买入/买入/持有/观望/减仓/卖出/强烈卖出), "
                "decision_type(buy/hold/sell), trend_prediction(强烈看多/看多/震荡/看空/强烈看空), "
                "confidence_level(高/中/低), analysis_summary(80字内摘要), risk_warning(风险提示)。\n"
                f"{json.dumps(_jsonable(context), ensure_ascii=False)}"
            ),
        },
    ]
    try:
        response = llm.chat(messages, timeout=timeout_seconds) if hasattr(llm, "chat") else llm(messages)
    except Exception:
        return None
    content = getattr(response, "content", response)
    payload = _extract_json(str(content or ""))
    if not payload:
        return None
    normalized = _normalize_llm_payload(payload, local_decision=local_decision)
    return normalized or None


def _missing_llm_context_fields(
    indicator: IndicatorResult,
    quote: dict[str, Any],
    chip: dict[str, Any],
    akshare_context: dict[str, Any],
) -> list[str]:
    missing = []
    if not indicator.data_sources:
        missing.append("data_sources")
    if not quote:
        missing.append("quote")
    if not chip:
        missing.append("chip")
    if not akshare_context:
        missing.append("akshare_context")
    return missing


def _build_context_pack(
    indicator: IndicatorResult,
    *,
    quote: dict[str, Any],
    chip: dict[str, Any],
    akshare_context: dict[str, Any],
    hot_sectors: list[dict[str, Any]],
    market_phase: dict[str, Any],
    hot_sector_missing: list[str],
) -> dict[str, Any]:
    missing = _missing_llm_context_fields(indicator, quote, chip, akshare_context)
    if not hot_sectors:
        missing.append("hot_sector_context")
    missing.extend(hot_sector_missing)
    missing.append("news_context: provider_unavailable")
    data_sources = _analysis_sources(indicator, quote, chip, akshare_context)
    if hot_sectors:
        data_sources["hot_sector"] = "tushare_ths"
    data_sources["news"] = "unavailable"
    return {
        "ts_code": indicator.ts_code,
        "name": indicator.name,
        "trade_date": indicator.trade_date,
        "market_phase": market_phase,
        "quote": quote,
        "indicator": indicator.to_dict(),
        "chip": chip,
        "akshare_context": akshare_context,
        "hot_sectors": hot_sectors,
        "news": {"status": "unavailable", "missing_fields": ["news_context: provider_unavailable"]},
        "missing_fields": _dedupe_text(missing),
        "data_sources": data_sources,
    }


def _fallback_stock_payload(
    indicator: IndicatorResult,
    *,
    quote: dict[str, Any],
    chip: dict[str, Any],
    local_decision: dict[str, Any],
) -> dict[str, Any]:
    tech = indicator.technical or {}
    money = indicator.moneyflow or {}
    ma_alignment = str(tech.get("ma_alignment") or "")
    macd_signal = str((tech.get("macd") or {}).get("signal") or "")
    close = _safe_float(quote.get("close")) or indicator.close
    summary = f"收盘/现价 {close:.2f}，{ma_alignment}，MACD {macd_signal}，资金流 {money.get('status', 'unavailable')}。"
    if chip:
        summary += f" 筹码获利比例 {_fmt(chip.get('profit_ratio'))}。"
    return {
        "score": float(local_decision["score"]),
        "advice": str(local_decision["operation_advice"]),
        "trend": str(local_decision["trend_prediction"]),
        "summary": summary,
        "risk": "新闻/舆情未启用，结论仅基于行情、技术指标和已获取的基本面数据，不构成投资建议。",
        "decision_type": str(local_decision["decision_type"]),
        "confidence_level": str(local_decision["confidence_level"]),
        "signal_reasons": list(local_decision.get("signal_reasons") or []),
        "risk_factors": list(local_decision.get("risk_factors") or []),
    }


def _final_stock_payload(local_decision: dict[str, Any], llm_payload: dict[str, Any]) -> dict[str, Any]:
    summary = str(llm_payload.get("summary") or "").strip()
    risk = str(llm_payload.get("risk") or "").strip()
    if not summary:
        summary = "本地规则已完成评级，LLM 未提供有效摘要。"
    if not risk:
        risk = "新闻/舆情未启用，结论仅基于行情、技术指标和已获取的基本面数据，不构成投资建议。"
    adjustments = [str(item).strip() for item in (local_decision.get("adjustment_reasons") or []) if str(item).strip()]
    return {
        "score": float(local_decision["score"]),
        "advice": str(local_decision["operation_advice"]),
        "trend": str(local_decision["trend_prediction"]),
        "summary": summary,
        "risk": risk,
        "decision_type": str(local_decision["decision_type"]),
        "confidence_level": str(local_decision["confidence_level"]),
        "signal_reasons": list(local_decision.get("signal_reasons") or []),
        "risk_factors": list(local_decision.get("risk_factors") or []),
        "raw_score": float(local_decision.get("raw_score") or local_decision["score"]),
        "raw_advice": str(local_decision.get("raw_operation_advice") or local_decision["operation_advice"]),
        "rating_adjustment": "；".join(adjustments),
    }


def _build_dashboard(
    indicator: IndicatorResult,
    *,
    final_payload: dict[str, Any],
    quote: dict[str, Any],
    chip: dict[str, Any],
    akshare_context: dict[str, Any],
    hot_sectors: list[dict[str, Any]],
    market_phase: dict[str, Any],
    missing_fields: list[str],
    local_decision: dict[str, Any],
) -> dict[str, Any]:
    tech = indicator.technical or {}
    ma = tech.get("ma") if isinstance(tech.get("ma"), dict) else {}
    bias = tech.get("bias") if isinstance(tech.get("bias"), dict) else {}
    volume = indicator.volume or {}
    fundamentals = indicator.fundamentals or {}
    support_resistance = indicator.support_resistance or {}
    close = _first_safe_float(quote.get("close"), quote.get("price"), indicator.close) or indicator.close
    signal_type = str(final_payload.get("decision_type") or "hold")
    position_advice = _position_advice(final_payload, missing_fields=missing_fields)
    sniper = _sniper_points(
        close=close,
        ma=ma,
        support=support_resistance.get("support"),
        resistance=support_resistance.get("resistance"),
        atr=(tech.get("atr") or {}).get("atr14") if isinstance(tech.get("atr"), dict) else None,
        decision_type=signal_type,
    )
    risk_alerts = _dedupe_text(
        list(final_payload.get("risk_factors") or [])
        + [str(final_payload.get("risk") or "")]
        + _missing_field_alerts(missing_fields)
    )
    catalysts = _dedupe_text(
        list(final_payload.get("signal_reasons") or [])
        + [f"热点板块：{item.get('name')}" for item in hot_sectors[:3] if item.get("name")]
    )
    return {
        "core_conclusion": {
            "one_sentence": str(final_payload.get("summary") or "").strip(),
            "signal_type": signal_type,
            "time_sensitivity": _time_sensitivity(signal_type, market_phase),
            "position_advice": position_advice,
        },
        "data_perspective": {
            "trend_status": {
                "ma_alignment": tech.get("ma_alignment") or local_decision.get("trend_status") or "-",
                "trend_score": local_decision.get("trend_strength"),
                "is_bullish": signal_type == "buy",
                "market_phase": market_phase,
            },
            "price_position": {
                "current_price": _round_price(close),
                "ma5": _round_price(ma.get("ma5")),
                "ma10": _round_price(ma.get("ma10")),
                "ma20": _round_price(ma.get("ma20")),
                "ma60": _round_price(ma.get("ma60")),
                "bias_ma5": _round_price(bias.get("ma5")),
                "bias_ma10": _round_price(bias.get("ma10")),
                "bias_ma20": _round_price(bias.get("ma20")),
                "bias_status": _bias_status(bias.get("ma5")),
                "support_level": _first_or_none(support_resistance.get("support")),
                "resistance_level": _first_or_none(support_resistance.get("resistance")),
            },
            "volume_analysis": {
                "volume_ratio": volume.get("volume_ratio_5d"),
                "volume_status": volume.get("status"),
                "turnover_rate": fundamentals.get("turnover_rate") or quote.get("turnover_rate"),
                "volume_meaning": _volume_meaning(str(volume.get("status") or "")),
            },
            "chip_structure": _chip_structure(chip),
            "moneyflow": indicator.moneyflow or {},
            "fundamentals": fundamentals,
            "hot_sectors": hot_sectors,
            "akshare_context": akshare_context,
        },
        "intelligence": {
            "latest_news": "新闻/舆情未启用",
            "risk_alerts": risk_alerts,
            "positive_catalysts": catalysts[:8],
            "earnings_outlook": _earnings_outlook(fundamentals),
            "sentiment_summary": "未接入新闻/舆情搜索；当前结论仅基于行情、技术指标、资金流、基本面和筹码数据。",
            "missing_fields": _dedupe_text(missing_fields),
        },
        "battle_plan": {
            "sniper_points": sniper,
            "position_strategy": _position_strategy(signal_type, final_payload, market_phase),
            "action_checklist": _action_checklist(signal_type, sniper, missing_fields),
        },
    }


def _safe_hot_sector_context(provider: Any, storage: DuckDBStorage, trade_date: str) -> tuple[dict[str, Any], list[str]]:
    if not hasattr(provider, "load_hot_sector_context"):
        return {}, ["hot_sector_context: provider_unavailable"]
    try:
        payload = provider.load_hot_sector_context(trade_date, storage=storage, lookback_days=5)
    except Exception as exc:
        return {}, [f"hot_sector_context: {exc}"]
    if not isinstance(payload, dict):
        return {}, ["hot_sector_context: invalid_payload"]
    missing = [str(item) for item in payload.get("missing_fields", []) if str(item).strip()]
    return payload, missing


def _stock_hot_sectors(stock_hot_sector_map: Any, ts_code: str) -> list[dict[str, Any]]:
    if not isinstance(stock_hot_sector_map, dict):
        return []
    sectors = stock_hot_sector_map.get(ts_code) or stock_hot_sector_map.get(ts_code.split(".", 1)[0]) or []
    if not isinstance(sectors, list):
        return []
    result = [item for item in sectors if isinstance(item, dict)]
    result.sort(key=lambda item: (-_safe_float(item.get("heat_score")) if _safe_float(item.get("heat_score")) is not None else 0, str(item.get("name") or "")))
    return result[:5]


def _market_phase_context(trade_date: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(SHANGHAI_TZ)
    today = now.strftime("%Y%m%d")
    if str(trade_date) < today:
        phase = "historical_replay"
        label = "历史复盘"
    elif str(trade_date) > today:
        phase = "future_date"
        label = "未来交易日"
    else:
        hhmm = now.hour * 100 + now.minute
        if hhmm < 930:
            phase, label = "pre_market", "盘前"
        elif 930 <= hhmm <= 1130 or 1300 <= hhmm <= 1500:
            phase, label = "market_open", "盘中"
        elif 1130 < hhmm < 1300:
            phase, label = "midday_break", "午间休市"
        else:
            phase, label = "post_market", "盘后"
    return {
        "market": "A-share",
        "trade_date": str(trade_date),
        "as_of": now.isoformat(),
        "phase": phase,
        "label": label,
        "guideline": "盘中数据需结合实时 quote；历史复盘不得假设已获取未提供的盘后新闻。",
    }


def _position_advice(final_payload: dict[str, Any], *, missing_fields: list[str]) -> dict[str, str]:
    advice = str(final_payload.get("advice") or "观望")
    decision_type = str(final_payload.get("decision_type") or "hold")
    missing_suffix = "；关键数据缺失时降低仓位" if missing_fields else ""
    if decision_type == "buy":
        return {
            "no_position": f"{advice}，只在触发买点且量价确认时分批介入{missing_suffix}",
            "has_position": f"持仓者可继续跟踪，跌破止损或资金转弱时减仓{missing_suffix}",
        }
    if decision_type == "sell":
        return {
            "no_position": "无仓位不追入，等待趋势修复",
            "has_position": f"{advice}，优先控制回撤和仓位风险",
        }
    return {
        "no_position": f"{advice}，等待更清晰买点{missing_suffix}",
        "has_position": f"持有观察，跌破关键支撑或风险放大时减仓{missing_suffix}",
    }


def _sniper_points(
    *,
    close: float,
    ma: dict[str, Any],
    support: Any,
    resistance: Any,
    atr: Any,
    decision_type: str,
) -> dict[str, Any]:
    ma5 = _safe_float(ma.get("ma5"))
    ma10 = _safe_float(ma.get("ma10"))
    support_level = _safe_float(_first_or_none(support))
    resistance_level = _safe_float(_first_or_none(resistance))
    atr_value = _safe_float(atr)
    ideal_buy = support_level or ma5 or close
    secondary_buy = ma10 or support_level or close
    stop_loss_base = support_level or ma10 or ma5 or close
    stop_loss = stop_loss_base - atr_value if atr_value is not None and atr_value > 0 else stop_loss_base * 0.97
    if resistance_level is not None and resistance_level > close:
        take_profit = resistance_level
    elif atr_value is not None and atr_value > 0:
        take_profit = close + atr_value * (2 if decision_type == "buy" else 1)
    else:
        take_profit = close * (1.08 if decision_type == "buy" else 1.04)
    return {
        "ideal_buy": _round_price(ideal_buy),
        "secondary_buy": _round_price(secondary_buy),
        "stop_loss": _round_price(stop_loss),
        "take_profit": _round_price(take_profit),
    }


def _position_strategy(decision_type: str, final_payload: dict[str, Any], market_phase: dict[str, Any]) -> dict[str, str]:
    if decision_type == "buy":
        position = "试探仓 20%-30%，确认后再加仓"
        entry = "靠近理想买点且量能/资金不恶化时分批执行"
    elif decision_type == "sell":
        position = "降低仓位或空仓观察"
        entry = "反弹不能收复关键均线时优先减仓"
    else:
        position = "轻仓或观察仓"
        entry = "等待价格回到支撑位、资金转正或突破压力后再行动"
    return {
        "suggested_position": position,
        "entry_plan": f"{entry}；当前市场阶段：{market_phase.get('label', '-')}",
        "risk_control": str(final_payload.get("risk") or "严格设置止损，不构成投资建议。"),
    }


def _action_checklist(decision_type: str, sniper: dict[str, Any], missing_fields: list[str]) -> list[str]:
    checklist = [
        f"确认价格未跌破止损位 {sniper.get('stop_loss', '-')}",
        "确认量能、资金流和大盘环境没有同步转弱",
    ]
    if decision_type == "buy":
        checklist.insert(0, f"等待价格接近理想买点 {sniper.get('ideal_buy', '-')}")
    elif decision_type == "sell":
        checklist.insert(0, "优先确认是否跌破关键均线或支撑")
    else:
        checklist.insert(0, "等待更清晰的买卖触发条件")
    if missing_fields:
        checklist.append("存在缺失字段，结论按保守口径执行")
    return checklist


def _chip_structure(chip: dict[str, Any]) -> dict[str, Any]:
    if not chip:
        return {"status": "unavailable", "reason": "筹码数据缺失"}
    profit_ratio = _safe_float(chip.get("profit_ratio"))
    concentration = _first_safe_float(chip.get("concentration_90"), chip.get("concentration"))
    return {
        "profit_ratio": _round_price(profit_ratio * 100 if profit_ratio is not None and 0 <= profit_ratio <= 1 else profit_ratio),
        "avg_cost": _round_price(chip.get("avg_cost")),
        "concentration": _round_price(concentration),
        "chip_health": _chip_health(profit_ratio, concentration),
    }


def _chip_health(profit_ratio: float | None, concentration: float | None) -> str:
    profit_pct = profit_ratio * 100 if profit_ratio is not None and 0 <= profit_ratio <= 1 else profit_ratio
    conc_pct = concentration * 100 if concentration is not None and 0 <= concentration <= 1 else concentration
    if profit_pct is not None and profit_pct >= 90:
        return "警惕"
    if conc_pct is not None and conc_pct >= 25:
        return "分散"
    if profit_pct is not None and 35 <= profit_pct <= 80:
        return "健康"
    return "一般"


def _bias_status(value: Any) -> str:
    bias = _safe_float(value)
    if bias is None:
        return "数据不足"
    if bias > 5:
        return "偏离过高，严禁追高"
    if bias > 2:
        return "略偏高"
    if bias >= -3:
        return "接近均线"
    return "偏弱或回踩较深"


def _volume_meaning(status: str) -> str:
    return {
        "缩量回调": "抛压减轻，若趋势未破坏更接近回踩买点",
        "放量上涨": "多头进攻较强，但需防止高位追涨",
        "缩量上涨": "上攻动能不足，需等待补量",
        "放量下跌": "抛压放大，优先控制风险",
        "量能正常": "量能无明显极端信号",
    }.get(status, "量能数据不足")


def _earnings_outlook(fundamentals: dict[str, Any]) -> str:
    profit = _safe_float(fundamentals.get("profit"))
    roe = _safe_float(fundamentals.get("roe"))
    debt = _safe_float(fundamentals.get("debt_to_assets"))
    if profit is None and roe is None and debt is None:
        return "财务数据缺失，无法判断业绩趋势"
    parts = []
    if profit is not None:
        parts.append("利润为正" if profit >= 0 else "利润为负")
    if roe is not None:
        parts.append(f"ROE {_fmt(roe)}")
    if debt is not None:
        parts.append(f"负债率 {_fmt(debt)}")
    return "；".join(parts)


def _time_sensitivity(decision_type: str, market_phase: dict[str, Any]) -> str:
    phase = str(market_phase.get("label") or "")
    if decision_type == "buy":
        return f"{phase}信号需在 1-3 个交易日内确认，失效后不追"
    if decision_type == "sell":
        return f"{phase}风险信号优先处理，等待修复后再评估"
    return f"{phase}以观察为主，等待触发条件"


def _missing_field_alerts(missing_fields: list[str]) -> list[str]:
    return [f"缺失字段：{item}" for item in missing_fields if str(item).strip()]


def _first_or_none(values: Any) -> Any:
    if isinstance(values, (list, tuple)):
        return values[0] if values else None
    return values


def _round_price(value: Any) -> float | str:
    number = _safe_float(value)
    if number is None:
        return "N/A"
    return round(number, 2)


def _dedupe_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _write_report(
    analyses: list[DsaStockAnalysis],
    *,
    trade_date: str,
    reports_dir: Path,
    source_label: str,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_label or "stocks")
    path = reports_dir / f"dsa_native_{trade_date}_{safe_source}_{timestamp}.md"
    path.write_text(_format_report(analyses, trade_date=trade_date), encoding="utf-8")
    return path


def _format_report(analyses: list[DsaStockAnalysis], *, trade_date: str) -> str:
    buy_count = sum(1 for item in analyses if item.decision_type == "buy")
    sell_count = sum(1 for item in analyses if item.decision_type == "sell")
    hold_count = len(analyses) - buy_count - sell_count
    comparable_count = sum(1 for item in analyses if item.external_supported)
    native_extra_count = len(analyses) - comparable_count
    skipped_by_external = [item.ts_code for item in analyses if not item.external_supported]
    lines = [
        f"# SATS DSA 原生分析报告 {trade_date}",
        "",
        "> 数据源优先级：TickFlow 行情/K线，Tushare 资金流/基本面，AkShare 可选补充。新闻/舆情未启用。",
        "",
        f"共分析 {len(analyses)} 只股票 | 买入类:{buy_count} 持有/观望类:{hold_count} 卖出类:{sell_count}",
        f"可比股票数:{comparable_count} | 原生额外股票数:{native_extra_count} | skipped_by_external:{','.join(skipped_by_external) or '-'}",
        "",
        "## 排名",
        "",
        "| 排名 | 代码 | 名称 | 评分 | 评级 | 决策 | 置信度 | 趋势 | 备注 |",
        "|---:|---|---|---:|---|---|---|---|---|",
    ]
    for index, item in enumerate(analyses, start=1):
        note = item.external_skip_reason if not item.external_supported else ""
        lines.append(
            f"| {index} | {item.ts_code} | {item.name or '-'} | {_score(item.score)} | "
            f"{item.advice} | {item.decision_type} | {item.confidence_level or '-'} | {item.trend} | {note} |"
        )
    lines.extend(["", "## 个股分析", ""])
    for item in analyses:
        indicator = item.indicator
        tech = indicator.get("technical", {})
        money = indicator.get("moneyflow", {})
        fundamentals = indicator.get("fundamentals", {})
        volume = indicator.get("volume", {})
        sr = indicator.get("support_resistance", {})
        dashboard = item.dashboard or {}
        core = dashboard.get("core_conclusion") if isinstance(dashboard.get("core_conclusion"), dict) else {}
        data_perspective = dashboard.get("data_perspective") if isinstance(dashboard.get("data_perspective"), dict) else {}
        intelligence = dashboard.get("intelligence") if isinstance(dashboard.get("intelligence"), dict) else {}
        battle = dashboard.get("battle_plan") if isinstance(dashboard.get("battle_plan"), dict) else {}
        price_position = data_perspective.get("price_position") if isinstance(data_perspective.get("price_position"), dict) else {}
        volume_analysis = data_perspective.get("volume_analysis") if isinstance(data_perspective.get("volume_analysis"), dict) else {}
        chip_structure = data_perspective.get("chip_structure") if isinstance(data_perspective.get("chip_structure"), dict) else {}
        sniper = battle.get("sniper_points") if isinstance(battle.get("sniper_points"), dict) else {}
        position = battle.get("position_strategy") if isinstance(battle.get("position_strategy"), dict) else {}
        checklist = battle.get("action_checklist") if isinstance(battle.get("action_checklist"), list) else []
        position_advice = core.get("position_advice") if isinstance(core.get("position_advice"), dict) else {}
        lines.extend(
            [
                f"### {item.name or item.ts_code} ({item.ts_code})",
                "",
                "#### 信息面",
                "",
                f"- 新闻/舆情：未启用",
                f"- 热点板块：{_format_hot_sectors(item.hot_sectors)}",
                f"- 风险警报：{_join_text([str(value) for value in intelligence.get('risk_alerts', [])]) if isinstance(intelligence.get('risk_alerts'), list) else '-'}",
                f"- 缺失字段：{_join_text(item.missing_fields)}",
                "",
                "#### 核心结论",
                "",
                f"- 结论：{core.get('one_sentence') or item.summary}",
                f"- 操作：{item.advice}；决策：{item.decision_type}；置信度：{item.confidence_level or '-'}；趋势：{item.trend}；评分：{_score(item.score)}",
                f"- 时效：{core.get('time_sensitivity') or '-'}",
                f"- 无仓位建议：{position_advice.get('no_position') or '-'}",
                f"- 持仓建议：{position_advice.get('has_position') or '-'}",
                f"- 本地原始评级：{item.raw_advice or item.advice}；原始评分：{_score(item.raw_score or item.score)}",
                f"- 稳定性调整后评级：{item.advice}；调整原因：{item.rating_adjustment or '无'}",
                f"- 外部可比：{'是' if item.external_supported else item.external_skip_reason or '否'}",
                f"- LLM: {_llm_status_text(item)}",
                f"- 系统评级理由：{_join_text(item.signal_reasons)}",
                f"- 风险因素：{_join_text(item.risk_factors)}",
                "",
                "#### 数据视角",
                "",
                f"- 市场阶段：{item.market_phase.get('label', '-')} ({item.market_phase.get('phase', '-')})",
                f"- 价格位置：现价 {price_position.get('current_price', _fmt(indicator.get('close')))}；MA5 {price_position.get('ma5', '-')}；MA10 {price_position.get('ma10', '-')}；MA20 {price_position.get('ma20', '-')}；MA60 {price_position.get('ma60', '-')}",
                f"- 乖离率：MA5 {price_position.get('bias_ma5', '-')}%；MA10 {price_position.get('bias_ma10', '-')}%；MA20 {price_position.get('bias_ma20', '-')}%；状态 {price_position.get('bias_status', '-')}",
                f"- 技术：MA {tech.get('ma_alignment')}；MACD {(tech.get('macd') or {}).get('signal')}；BOLL {(tech.get('boll') or {}).get('position')}；KDJ {_fmt((tech.get('kdj') or {}).get('k'))}/{_fmt((tech.get('kdj') or {}).get('d'))}/{_fmt((tech.get('kdj') or {}).get('j'))}",
                f"- 量能：{volume_analysis.get('volume_status', volume.get('status'))}；量比 {volume_analysis.get('volume_ratio', volume.get('volume_ratio_5d'))}；换手率 {volume_analysis.get('turnover_rate', fundamentals.get('turnover_rate'))}；说明 {volume_analysis.get('volume_meaning', '-')}",
                f"- 支撑/压力：{_join_levels(sr.get('support'))} / {_join_levels(sr.get('resistance'))}",
                f"- 资金流：1d {_fmt(money.get('main_net_amount'))}；5d {_fmt(money.get('main_net_amount_5d'))}；10d {_fmt(money.get('main_net_amount_10d'))}",
                f"- 基本面：PE {_fmt(fundamentals.get('pe'))}；PB {_fmt(fundamentals.get('pb'))}；市值 {_fmt(fundamentals.get('total_mv'))}；ROE {_fmt(fundamentals.get('roe'))}；负债率 {_fmt(fundamentals.get('debt_to_assets'))}",
                f"- 筹码：获利比例 {_fmt(chip_structure.get('profit_ratio'))}%；平均成本 {_fmt(chip_structure.get('avg_cost'))}；集中度 {_fmt(chip_structure.get('concentration'))}；健康度 {chip_structure.get('chip_health', '-')}",
                "",
                "#### 战术计划",
                "",
                f"- 理想买点：{sniper.get('ideal_buy', '-')}；次级买点：{sniper.get('secondary_buy', '-')}；止损：{sniper.get('stop_loss', '-')}；止盈：{sniper.get('take_profit', '-')}",
                f"- 仓位：{position.get('suggested_position', '-')}",
                f"- 执行：{position.get('entry_plan', '-')}",
                f"- 风控：{position.get('risk_control', '-')}",
                f"- 行动清单：{_join_text([str(value) for value in checklist])}",
                "",
                "#### 风险提示与数据源",
                "",
                f"- 风险：{item.risk}",
                f"- 数据源：{', '.join(f'{key}={value}' for key, value in item.data_sources.items() if value)}",
                "",
            ]
        )
    lines.append("*以上内容仅为数据分析辅助，不构成投资建议。*")
    return "\n".join(lines)


def _analysis_sources(
    indicator: IndicatorResult,
    quote: dict[str, Any],
    chip: dict[str, Any],
    akshare_context: dict[str, Any],
) -> dict[str, str]:
    sources = dict(indicator.data_sources or {})
    if quote.get("data_source"):
        sources["quote"] = str(quote.get("data_source"))
    if chip.get("data_source"):
        sources["chip"] = str(chip.get("data_source"))
    if akshare_context.get("data_source"):
        sources["akshare_context"] = str(akshare_context.get("data_source"))
    sources.setdefault("news", "unavailable")
    return sources


def _llm_status_text(item: DsaStockAnalysis) -> str:
    if item.llm_unavailable:
        return "unavailable，本地规则评级"
    if item.llm_disabled:
        return "disabled，本地规则评级"
    return "available"


def _input_lookup(items: list[IndicatorInput]) -> dict[str, IndicatorInput]:
    return {item.ts_code: item for item in items}


def _records_by_symbol(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    return {
        str(row["ts_code"]): {key: value for key, value in row.dropna().to_dict().items()}
        for _, row in frame.iterrows()
        if str(row.get("ts_code") or "").strip()
    }


def _safe_provider_list(loader: Callable[[], Any]) -> list[IndicatorInput]:
    try:
        value = loader()
    except Exception:
        return []
    return list(value or [])


def _safe_provider_frame(loader: Callable[[], Any]) -> pd.DataFrame:
    try:
        value = loader()
    except Exception:
        return _empty_frame()
    return value if isinstance(value, pd.DataFrame) else _empty_frame()


def _safe_provider_dict(loader: Callable[[], Any]) -> dict[str, dict[str, Any]]:
    try:
        value = loader()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _build_optional(factory: Callable[[], Any]) -> Any | None:
    try:
        return factory()
    except Exception:
        return None


def _merge_dicts(primary: dict[str, Any], supplement: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in (primary or {}).items() if value not in (None, "")}
    for key, value in (supplement or {}).items():
        if value not in (None, ""):
            result[key] = value
    return result


def _normalize_llm_payload(payload: dict[str, Any], *, local_decision: dict[str, Any]) -> dict[str, Any]:
    score = _first_safe_float(payload.get("sentiment_score"), payload.get("score"))
    if score is None:
        return {}
    advice = normalize_operation_advice(payload.get("operation_advice") or payload.get("advice"))
    if advice is None:
        return {}
    decision_type = str(payload.get("decision_type") or decision_type_for_advice(advice)).strip().lower()
    if decision_type not in {"buy", "hold", "sell"}:
        decision_type = decision_type_for_advice(advice)
    confidence = str(payload.get("confidence_level") or local_decision.get("confidence_level") or "中").strip()
    if confidence not in {"高", "中", "低"}:
        confidence = str(local_decision.get("confidence_level") or "中")
    return {
        "score": max(0.0, min(100.0, score)),
        "advice": advice,
        "trend": str(payload.get("trend_prediction") or payload.get("trend") or local_decision.get("trend_prediction") or "震荡").strip(),
        "summary": str(payload.get("analysis_summary") or payload.get("summary") or "").strip()[:240],
        "risk": str(payload.get("risk_warning") or payload.get("risk") or "不构成投资建议。").strip(),
        "decision_type": decision_type,
        "confidence_level": confidence,
        "signal_reasons": list(local_decision.get("signal_reasons") or []),
        "risk_factors": list(local_decision.get("risk_factors") or []),
    }


def _daily_stock_analysis_support(ts_code: str) -> tuple[bool, str]:
    raw = (ts_code or "").strip().upper()
    if not raw:
        return False, "daily_stock_analysis 不支持"
    if raw.endswith(".BJ"):
        return False, "daily_stock_analysis 不支持"
    code = raw.split(".", 1)[0]
    if not code.isdigit():
        return False, "daily_stock_analysis 不支持"
    if code.startswith("688") or code.startswith(_BSE_PREFIXES):
        return False, "daily_stock_analysis 不支持"
    return True, ""


def _extract_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if not isinstance(value, (dict, list, tuple, str, bytes)):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    return value


def _calendar_dates(trade_date: str, days: int) -> list[str]:
    end = datetime.strptime(trade_date, "%Y%m%d")
    return [(end - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days, -1, -1)]


def _today() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame()


def _is_empty(frame: pd.DataFrame | None) -> bool:
    return frame is None or frame.empty


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_safe_float(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _score(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "-"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _join_levels(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "-"
    return ",".join(_fmt(value) for value in values[:3])


def _join_text(values: list[str]) -> str:
    clean = [str(value).strip() for value in values if str(value).strip()]
    return "；".join(clean) if clean else "-"


def _format_hot_sectors(values: list[dict[str, Any]]) -> str:
    if not values:
        return "-"
    parts = []
    for item in values[:5]:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        heat = _fmt(item.get("heat_score"))
        sector_type = str(item.get("sector_type") or "").strip()
        suffix = f"({sector_type},热度{heat})" if sector_type or heat != "-" else ""
        parts.append(f"{name}{suffix}")
    return "，".join(parts) if parts else "-"
