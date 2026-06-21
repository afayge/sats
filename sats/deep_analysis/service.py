from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from sats.config import Settings, load_settings
from sats.data.astock_provider import AStockDataProvider
from sats.deep_analysis.models import (
    DeepAnalysisRequest,
    DeepAnalysisResult,
    DeepDimensionResult,
    DeepInvestorVote,
    DeepStockAnalysis,
)
from sats.indicators import IndicatorCalculator, IndicatorInput, IndicatorResult
from sats.llm import ChatLLM
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols, parse_symbol_csv


# Native SATS implementation inspired by UZI-Skill's staged research workflow
# (MIT licensed), but deliberately limited to A-share provider boundaries.
VALID_PHASES = {"run", "collect", "score", "panel", "report"}
SUPPORTED_MARKETS = (".SH", ".SZ")


DIMENSION_SPECS: tuple[tuple[str, str, float], ...] = (
    ("0_basic", "基础信息", 1.0),
    ("1_financial_quality", "财务质量", 3.0),
    ("2_kline_technical", "K线技术", 2.0),
    ("3_market_macro", "市场/宏观", 1.0),
    ("4_peers", "同行对比", 0.6),
    ("5_fund_holders", "基金持仓", 0.6),
    ("6_industry", "行业景气", 1.0),
    ("7_valuation", "估值", 2.0),
    ("8_governance", "治理", 0.7),
    ("9_capital_flow", "资金流", 1.0),
    ("10_moat_risk", "护城河/风险", 1.4),
    ("11_events", "事件催化", 0.7),
)


FLAGSHIP_INVESTORS: tuple[dict[str, str], ...] = (
    {"investor_id": "buffett", "name": "巴菲特", "group": "A"},
    {"investor_id": "munger", "name": "芒格", "group": "A"},
    {"investor_id": "graham", "name": "格雷厄姆", "group": "A"},
    {"investor_id": "fisher", "name": "费雪", "group": "A"},
    {"investor_id": "lynch", "name": "林奇", "group": "B"},
    {"investor_id": "oneill", "name": "欧奈尔", "group": "B"},
    {"investor_id": "soros", "name": "索罗斯", "group": "C"},
    {"investor_id": "dalio", "name": "达里奥", "group": "C"},
    {"investor_id": "livermore", "name": "利弗莫尔", "group": "D"},
    {"investor_id": "minervini", "name": "米内尔维尼", "group": "D"},
    {"investor_id": "duan", "name": "段永平", "group": "E"},
    {"investor_id": "zhao_lg", "name": "赵老哥", "group": "F"},
)


def run_deep_analysis(
    symbols: list[str] | tuple[str, ...] | str,
    *,
    trade_date: str,
    phase: str = "run",
    settings: Settings | None = None,
    storage: DuckDBStorage | None = None,
    astock_provider: Any | None = None,
    lookback_days: int = 180,
    llm_review: bool = True,
    llm: Any | None = None,
    llm_timeout_seconds: int = 45,
    report: bool = True,
    reports_dir: Path | None = None,
    progress: Any | None = None,
) -> DeepAnalysisResult:
    settings = settings or load_settings()
    storage = storage or DuckDBStorage(settings.db_path)
    provider = astock_provider or AStockDataProvider(settings)
    phase = str(phase or "run").strip() or "run"
    if phase not in VALID_PHASES:
        raise ValueError(f"unsupported deep-analysis phase: {phase}")

    clean_symbols = (
        parse_symbol_csv(symbols, required=False)
        if isinstance(symbols, str)
        else normalize_symbols(symbols, required=False)
    )
    supported, rejected = _supported_common_a_symbols(clean_symbols)
    if rejected:
        raise ValueError(f"deep-analysis v1 仅支持 A 股普通股票，不支持: {', '.join(rejected)}")
    request = DeepAnalysisRequest(
        symbols=tuple(supported),
        trade_date=str(trade_date),
        phase=phase,
        lookback_days=int(lookback_days or 180),
        llm_review=bool(llm_review),
        report=bool(report),
    )
    if not supported:
        return DeepAnalysisResult(request=request, message="无可分析股票")

    inputs = _load_inputs(provider, supported, request.trade_date, storage=storage, lookback_days=request.lookback_days, progress=progress)
    valid_inputs = [item for item in inputs if _has_daily_data(item)]
    if not valid_inputs:
        return DeepAnalysisResult(request=request, message="无可分析股票")

    extra = _load_extra_context(provider, supported, request.trade_date, storage=storage, progress=progress)
    analyses = tuple(
        _analyze_stock(item, request=request, extra=extra, phase=phase)
        for item in valid_inputs
    )
    if request.llm_review and phase in {"run", "panel", "report"}:
        analyses = _attach_llm_reviews(analyses, llm=llm, timeout_seconds=llm_timeout_seconds, progress=progress)
    result = DeepAnalysisResult(request=request, analyses=analyses)
    if report and phase in {"run", "report"}:
        result = write_deep_analysis_artifacts(result, reports_dir=reports_dir or Path(settings.project_root) / "reports" / "deep_analysis")
    return result


def write_deep_analysis_artifacts(result: DeepAnalysisResult, *, reports_dir: Path) -> DeepAnalysisResult:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    codes = "_".join(symbol.split(".", 1)[0] for symbol in result.request.symbols) or "stocks"
    base = f"deep_analysis_{result.request.trade_date}_{codes}_{stamp}"
    markdown_path = reports_dir / f"{base}.md"
    json_path = reports_dir / f"{base}.json"
    updated = replace(result, markdown_report_path=markdown_path, json_artifact_path=json_path)
    markdown_path.write_text(updated.to_markdown(), encoding="utf-8")
    json_path.write_text(json.dumps(updated.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return updated


def _load_inputs(
    provider: Any,
    symbols: list[str],
    trade_date: str,
    *,
    storage: DuckDBStorage,
    lookback_days: int,
    progress: Any | None,
) -> list[IndicatorInput]:
    if progress is None:
        return provider.load_indicator_inputs(symbols, trade_date, lookback_days=lookback_days, storage=storage)
    with progress.step("深研数据底座") as step:
        inputs = provider.load_indicator_inputs(symbols, trade_date, lookback_days=lookback_days, storage=storage)
        step.complete(message=f"{len(inputs)} 只")
        return inputs


def _load_extra_context(provider: Any, symbols: list[str], trade_date: str, *, storage: DuckDBStorage, progress: Any | None) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        return {
            "quotes": _safe_call(lambda: provider.load_realtime_quote_lookup(symbols), {}),
            "chips": _safe_call(lambda: provider.load_chip_context(symbols), {}),
            "fundamentals": _safe_call(lambda: provider.load_fundamental_context(symbols), {}),
            "hot_sector": _safe_call(lambda: provider.load_hot_sector_context(trade_date, storage=storage), {}),
            "breadth": _safe_call(lambda: provider.load_market_breadth(), ({}, "unavailable")),
            "limit_sentiment": _safe_call(lambda: provider.load_limit_sentiment(trade_date, storage=storage), {}),
            "events": _safe_call(lambda: provider.load_event_context(symbols=symbols, trade_date=trade_date), {}),
            "news": _safe_call(lambda: provider.load_news_context(symbols=symbols, trade_date=trade_date), {}),
        }

    if progress is None:
        return load()
    with progress.step("深研补充上下文") as step:
        payload = load()
        step.complete()
        return payload


def _analyze_stock(item: IndicatorInput, *, request: DeepAnalysisRequest, extra: dict[str, Any], phase: str) -> DeepStockAnalysis:
    indicator = IndicatorCalculator().calculate(item)
    context = _StockContext(
        item=item,
        indicator=indicator,
        quote=(extra.get("quotes") or {}).get(item.ts_code, {}),
        chip=(extra.get("chips") or {}).get(item.ts_code, {}),
        fundamental_extra=(extra.get("fundamentals") or {}).get(item.ts_code, {}),
        hot_sector=extra.get("hot_sector") or {},
        breadth=extra.get("breadth") or ({}, "unavailable"),
        limit_sentiment=extra.get("limit_sentiment") or {},
        events=extra.get("events") or {},
        news=extra.get("news") or {},
    )
    dimensions = _build_dimensions(context)
    features = _features(context, dimensions)
    votes: tuple[DeepInvestorVote, ...] = ()
    if phase in {"panel", "run", "report"}:
        votes = tuple(_investor_vote(meta, features) for meta in FLAGSHIP_INVESTORS)
    synthesis = _synthesize(context, dimensions, votes)
    missing = tuple(_dedupe(field for dim in dimensions for field in dim.missing_fields))
    return DeepStockAnalysis(
        ts_code=item.ts_code,
        name=_stock_name(context),
        trade_date=request.trade_date,
        overall_score=float(synthesis["overall_score"]),
        verdict_label=str(synthesis["verdict_label"]),
        dimensions=tuple(dimensions),
        investor_votes=votes,
        synthesis=synthesis,
        missing_fields=missing,
        data_sources=_data_sources(context),
    )


class _StockContext:
    def __init__(
        self,
        *,
        item: IndicatorInput,
        indicator: IndicatorResult,
        quote: dict[str, Any],
        chip: dict[str, Any],
        fundamental_extra: dict[str, Any],
        hot_sector: dict[str, Any],
        breadth: Any,
        limit_sentiment: dict[str, Any],
        events: dict[str, Any],
        news: dict[str, Any],
    ) -> None:
        self.item = item
        self.indicator = indicator
        self.quote = quote if isinstance(quote, dict) else {}
        self.chip = chip if isinstance(chip, dict) else {}
        self.fundamental_extra = fundamental_extra if isinstance(fundamental_extra, dict) else {}
        self.hot_sector = hot_sector if isinstance(hot_sector, dict) else {}
        self.breadth = breadth
        self.limit_sentiment = limit_sentiment if isinstance(limit_sentiment, dict) else {}
        self.events = events if isinstance(events, dict) else {}
        self.news = news if isinstance(news, dict) else {}


def _build_dimensions(context: _StockContext) -> list[DeepDimensionResult]:
    builders: dict[str, Callable[[_StockContext, str, float], DeepDimensionResult]] = {
        "0_basic": _dim_basic,
        "1_financial_quality": _dim_financial_quality,
        "2_kline_technical": _dim_kline_technical,
        "3_market_macro": _dim_market_macro,
        "4_peers": _dim_peers,
        "5_fund_holders": _dim_fund_holders,
        "6_industry": _dim_industry,
        "7_valuation": _dim_valuation,
        "8_governance": _dim_governance,
        "9_capital_flow": _dim_capital_flow,
        "10_moat_risk": _dim_moat_risk,
        "11_events": _dim_events,
    }
    return [builders[key](context, label, weight) for key, label, weight in DIMENSION_SPECS]


def _dim_basic(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    basic = context.item.stock_basic or {}
    price = _first_number(context.indicator.close, context.quote.get("close"), context.quote.get("price"))
    market_cap = _first_number(
        context.indicator.fundamentals.get("total_mv"),
        context.quote.get("total_mv"),
        context.fundamental_extra.get("total_mv"),
    )
    industry = _first_text(basic.get("industry"), basic.get("sw_industry"), basic.get("sector"), context.fundamental_extra.get("industry"))
    evidence = {
        "name": _stock_name(context),
        "price": price,
        "market_cap": market_cap,
        "industry": industry,
        "listed_date": _first_text(basic.get("list_date"), basic.get("listed_date")),
    }
    missing = _missing(evidence, ["name", "price", "industry"])
    score = _score_presence(evidence, ["name", "price", "market_cap", "industry"], base=4.0, per_field=1.5)
    return _dimension("0_basic", label, score, weight, evidence, missing, _data_sources(context), _basic_summary(evidence))


def _dim_financial_quality(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    fund = context.indicator.fundamentals
    revenue_growth = _growth(context.item.fundamentals, ["revenue", "total_revenue"])
    profit_growth = _growth(context.item.fundamentals, ["profit", "net_profit"])
    evidence = {
        "roe": fund.get("roe"),
        "revenue": fund.get("revenue"),
        "profit": fund.get("profit"),
        "revenue_growth": revenue_growth,
        "profit_growth": profit_growth,
        "debt_to_assets": fund.get("debt_to_assets"),
    }
    score = 0.0
    roe = _num(evidence.get("roe"))
    debt = _num(evidence.get("debt_to_assets"))
    if roe is not None:
        score += 3.5 if roe >= 15 else 2.5 if roe >= 10 else 1.0 if roe > 0 else 0
    if revenue_growth is not None:
        score += 2.0 if revenue_growth >= 15 else 1.3 if revenue_growth >= 5 else 0.5 if revenue_growth > 0 else 0
    if profit_growth is not None:
        score += 2.0 if profit_growth >= 15 else 1.2 if profit_growth >= 5 else 0.4 if profit_growth > 0 else 0
    if debt is not None:
        score += 1.5 if debt <= 45 else 0.8 if debt <= 65 else 0.2
    score = min(score, 10.0)
    missing = _missing(evidence, ["roe", "revenue", "profit", "debt_to_assets"])
    summary = f"ROE {_fmt(evidence.get('roe'))}，营收增速 {_fmt_pct(revenue_growth)}，利润增速 {_fmt_pct(profit_growth)}，负债率 {_fmt(evidence.get('debt_to_assets'))}"
    return _dimension("1_financial_quality", label, score, weight, evidence, missing, _data_sources(context), summary)


def _dim_kline_technical(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    tech = context.indicator.technical
    volume = context.indicator.volume
    rsi = tech.get("rsi") or {}
    evidence = {
        "ma_alignment": tech.get("ma_alignment"),
        "macd_signal": (tech.get("macd") or {}).get("signal"),
        "rsi6": rsi.get("rsi6"),
        "volume_status": volume.get("status"),
        "support": context.indicator.support_resistance.get("support"),
        "resistance": context.indicator.support_resistance.get("resistance"),
    }
    score = 5.0
    if evidence["ma_alignment"] == "多头排列":
        score += 2.0
    elif evidence["ma_alignment"] == "空头排列":
        score -= 2.0
    if "金叉" in str(evidence["macd_signal"]) or str(evidence["macd_signal"]) == "多头":
        score += 1.5
    elif "死叉" in str(evidence["macd_signal"]) or str(evidence["macd_signal"]) == "空头":
        score -= 1.5
    rsi6 = _num(evidence.get("rsi6"))
    if rsi6 is not None:
        if 45 <= rsi6 <= 70:
            score += 1.0
        elif rsi6 > 80:
            score -= 1.0
    if str(evidence["volume_status"]) == "放量上涨":
        score += 1.0
    elif str(evidence["volume_status"]) == "放量下跌":
        score -= 1.0
    score = _clamp(score, 0, 10)
    missing = _missing(evidence, ["ma_alignment", "macd_signal", "rsi6"])
    summary = f"{evidence.get('ma_alignment') or '均线未知'}，MACD {evidence.get('macd_signal') or '未知'}，量能 {evidence.get('volume_status') or '未知'}"
    return _dimension("2_kline_technical", label, score, weight, evidence, missing, _data_sources(context), summary)


def _dim_market_macro(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    breadth_payload, breadth_source = context.breadth if isinstance(context.breadth, tuple) and len(context.breadth) == 2 else ({}, "unavailable")
    limit = context.limit_sentiment or {}
    evidence = {
        "breadth": breadth_payload,
        "breadth_source": breadth_source,
        "limit_up_count": limit.get("limit_up_count"),
        "limit_down_count": limit.get("limit_down_count"),
        "hot_industries": (context.hot_sector.get("hot_industries") or [])[:5],
    }
    score = 5.0
    if isinstance(breadth_payload, dict) and breadth_payload.get("total"):
        up = _num(breadth_payload.get("up_count") or breadth_payload.get("advancing_count")) or 0
        down = _num(breadth_payload.get("down_count") or breadth_payload.get("declining_count")) or 0
        total = max(float(breadth_payload.get("total") or 1), 1.0)
        score += ((up - down) / total) * 3.0
    lu = _num(evidence.get("limit_up_count"))
    ld = _num(evidence.get("limit_down_count"))
    if lu is not None and ld is not None:
        score += 1.0 if lu > ld * 2 else -1.0 if ld > lu else 0.0
    missing = []
    if not isinstance(breadth_payload, dict) or not breadth_payload:
        missing.append("market_breadth")
    if not limit:
        missing.append("limit_sentiment")
    return _dimension("3_market_macro", label, _clamp(score, 0, 10), weight, evidence, tuple(missing), {"breadth": str(breadth_source)}, "市场宽度与涨跌停情绪用于校准个股风险偏好")


def _dim_peers(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    industry = _industry(context)
    evidence = {"industry": industry, "peer_table": []}
    return _dimension("4_peers", label, 0.0, weight, evidence, ("peer_table",), _data_sources(context), "SATS v1 尚未接入同业估值表，保留维度缺口")


def _dim_fund_holders(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    fund_keys = {k: v for k, v in context.fundamental_extra.items() if "fund" in str(k).lower() or "holder" in str(k).lower()}
    evidence = {"fund_holders": fund_keys}
    missing = () if fund_keys else ("fund_holders",)
    score = 6.0 if fund_keys else 0.0
    summary = "已有基金/持有人补充字段" if fund_keys else "SATS v1 provider 未返回基金持仓明细"
    return _dimension("5_fund_holders", label, score, weight, evidence, missing, _data_sources(context), summary)


def _dim_industry(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    industry = _industry(context)
    hot = _stock_hot_sectors(context)
    evidence = {"industry": industry, "hot_sectors": hot}
    score = 5.0 + (2.0 if hot else 0.0)
    missing = () if industry or hot else ("industry", "hot_sectors")
    summary = f"行业 {industry or '未知'}" + (f"，命中热点 {len(hot)} 个" if hot else "，未命中热点板块上下文")
    return _dimension("6_industry", label, score if industry or hot else 0.0, weight, evidence, missing, _data_sources(context), summary)


def _dim_valuation(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    fund = context.indicator.fundamentals
    evidence = {
        "pe": fund.get("pe"),
        "pb": fund.get("pb"),
        "ps": fund.get("ps"),
        "total_mv": fund.get("total_mv"),
        "roe": fund.get("roe"),
    }
    pe = _num(evidence.get("pe"))
    pb = _num(evidence.get("pb"))
    roe = _num(evidence.get("roe"))
    score = 0.0
    if pe is not None:
        score += 3.0 if 0 < pe <= 20 else 2.0 if pe <= 35 else 0.8 if pe <= 60 else 0.2
    if pb is not None:
        score += 2.0 if 0 < pb <= 2 else 1.2 if pb <= 4 else 0.3
    if roe is not None and pe is not None and pe > 0:
        score += 3.0 if roe / pe >= 0.7 else 1.8 if roe / pe >= 0.35 else 0.5
    score = min(score + 1.0, 10.0) if score else 0.0
    missing = _missing(evidence, ["pe", "pb", "total_mv"])
    summary = f"PE {_fmt(pe)}，PB {_fmt(pb)}，ROE/PE {_fmt(None if not pe or not roe else roe / pe)}"
    return _dimension("7_valuation", label, score, weight, evidence, missing, _data_sources(context), summary)


def _dim_governance(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    basic = context.item.stock_basic or {}
    evidence = {
        "actual_controller": _first_text(basic.get("actual_controller"), context.fundamental_extra.get("actual_controller")),
        "chairman": _first_text(basic.get("chairman"), context.fundamental_extra.get("chairman")),
        "pledge": context.chip.get("pledge") or context.fundamental_extra.get("pledge"),
    }
    present = [key for key, value in evidence.items() if value not in (None, "", [])]
    score = 6.0 if len(present) >= 2 else 4.0 if present else 0.0
    missing = tuple(key for key in ("actual_controller", "pledge") if evidence.get(key) in (None, "", []))
    summary = "治理字段可用" if present else "SATS v1 未取得治理结构/质押字段"
    return _dimension("8_governance", label, score, weight, evidence, missing, _data_sources(context), summary)


def _dim_capital_flow(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    mf = context.indicator.moneyflow
    evidence = {
        "main_net_amount": mf.get("main_net_amount"),
        "main_net_amount_5d": mf.get("main_net_amount_5d"),
        "main_net_amount_10d": mf.get("main_net_amount_10d"),
    }
    score = 0.0
    value = _num(evidence.get("main_net_amount_5d"))
    if value is not None:
        score = 7.0 if value > 0 else 3.0 if value < 0 else 5.0
    missing = _missing(evidence, ["main_net_amount_5d"])
    summary = f"5日主力净额 {_fmt(evidence.get('main_net_amount_5d'))}，10日 {_fmt(evidence.get('main_net_amount_10d'))}"
    return _dimension("9_capital_flow", label, score, weight, evidence, missing, _data_sources(context), summary)


def _dim_moat_risk(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    fund = context.indicator.fundamentals
    tech = context.indicator.technical
    roe = _num(fund.get("roe"))
    debt = _num(fund.get("debt_to_assets"))
    pe = _num(fund.get("pe"))
    ma = tech.get("ma_alignment")
    risks: list[str] = []
    if debt is not None and debt > 70:
        risks.append("资产负债率偏高")
    if pe is not None and pe > 60:
        risks.append("估值偏高")
    if ma == "空头排列":
        risks.append("技术趋势偏弱")
    score = 5.0
    if roe is not None and roe >= 15:
        score += 2.0
    if debt is not None and debt <= 45:
        score += 1.0
    score -= min(len(risks) * 1.5, 4.0)
    evidence = {"roe": roe, "debt_to_assets": debt, "pe": pe, "ma_alignment": ma, "risk_flags": risks}
    missing = _missing(evidence, ["roe", "debt_to_assets", "pe"])
    summary = "，".join(risks) if risks else "未触发核心财务/估值/趋势风险红灯"
    return _dimension("10_moat_risk", label, _clamp(score, 0, 10), weight, evidence, missing, _data_sources(context), summary)


def _dim_events(context: _StockContext, label: str, weight: float) -> DeepDimensionResult:
    events = context.events.get("items") if isinstance(context.events, dict) else []
    news = context.news.get("items") if isinstance(context.news, dict) else []
    evidence = {"events": events or [], "news": news or []}
    count = len(evidence["events"]) + len(evidence["news"])
    score = 6.0 if count else 0.0
    missing = () if count else ("events", "news")
    summary = f"取得 {count} 条事件/新闻线索" if count else "SATS v1 未取得事件催化上下文"
    return _dimension("11_events", label, score, weight, evidence, missing, _data_sources(context), summary)


def _features(context: _StockContext, dimensions: list[DeepDimensionResult]) -> dict[str, Any]:
    by_key = {item.key: item for item in dimensions}
    fund = context.indicator.fundamentals
    tech = context.indicator.technical
    support = context.indicator.support_resistance.get("support") or []
    return {
        "name": _stock_name(context),
        "price": _first_number(context.indicator.close, context.quote.get("close"), context.quote.get("price")),
        "roe": _num(fund.get("roe")),
        "debt_to_assets": _num(fund.get("debt_to_assets")),
        "pe": _num(fund.get("pe")),
        "pb": _num(fund.get("pb")),
        "revenue_growth": (by_key.get("1_financial_quality") or DeepDimensionResult("", "", 0, 0, "", "")).evidence.get("revenue_growth"),
        "profit_growth": (by_key.get("1_financial_quality") or DeepDimensionResult("", "", 0, 0, "", "")).evidence.get("profit_growth"),
        "ma_alignment": tech.get("ma_alignment"),
        "macd_signal": (tech.get("macd") or {}).get("signal"),
        "rsi6": (tech.get("rsi") or {}).get("rsi6"),
        "main_net_amount_5d": context.indicator.moneyflow.get("main_net_amount_5d"),
        "industry": _industry(context),
        "hot_sectors": _stock_hot_sectors(context),
        "support": support[0] if support else None,
        "dimension_scores": {item.key: item.score for item in dimensions},
    }


def _investor_vote(meta: dict[str, str], f: dict[str, Any]) -> DeepInvestorVote:
    investor_id = meta["investor_id"]
    rules = _rules_for_investor(investor_id, f)
    matched = tuple(label for label, passed in rules if passed)
    failed = tuple(label for label, passed in rules if not passed)
    score = round(len(matched) / max(len(rules), 1) * 100, 1)
    signal = "bullish" if score >= 65 else "bearish" if score <= 40 else "neutral"
    headline = _vote_headline(investor_id, f, signal)
    reasoning = f"{len(matched)}/{len(rules)} 条核心准则通过；" + ("；".join(matched[:2]) if matched else "主要准则未通过")
    return DeepInvestorVote(
        investor_id=investor_id,
        name=meta["name"],
        group=meta["group"],
        signal=signal,
        score=score,
        headline=headline,
        reasoning=reasoning,
        matched_rules=matched,
        failed_rules=failed,
    )


def _attach_llm_reviews(
    analyses: tuple[DeepStockAnalysis, ...],
    *,
    llm: Any | None,
    timeout_seconds: int,
    progress: Any | None,
) -> tuple[DeepStockAnalysis, ...]:
    chat_llm = llm if llm is not None else _build_optional(lambda: ChatLLM(timeout_seconds=timeout_seconds))
    if chat_llm is None:
        return tuple(_with_llm_review(item, status="unavailable", comment="") for item in analyses)
    if progress is not None:
        with progress.step("深研 LLM 复核", total=len(analyses)) as step:
            reviewed = _run_llm_review_loop(analyses, llm=chat_llm, timeout_seconds=timeout_seconds, step=step)
            step.complete()
            return reviewed
    return _run_llm_review_loop(analyses, llm=chat_llm, timeout_seconds=timeout_seconds, step=None)


def _run_llm_review_loop(
    analyses: tuple[DeepStockAnalysis, ...],
    *,
    llm: Any,
    timeout_seconds: int,
    step: Any | None,
) -> tuple[DeepStockAnalysis, ...]:
    reviewed: list[DeepStockAnalysis] = []
    for item in analyses:
        try:
            comment = _llm_review_comment(item, llm, timeout_seconds=timeout_seconds)
        except Exception:
            reviewed.append(_with_llm_review(item, status="unavailable", comment=""))
            if step is not None:
                step.advance(message=f"{item.ts_code} unavailable")
            continue
        reviewed.append(_with_llm_review(item, status="ok" if comment else "empty", comment=comment))
        if step is not None:
            step.advance(message=item.ts_code)
    return tuple(reviewed)


def _with_llm_review(item: DeepStockAnalysis, *, status: str, comment: str) -> DeepStockAnalysis:
    synthesis = dict(item.synthesis)
    synthesis["llm_review"] = {"status": status, "comment": comment}
    return replace(item, synthesis=synthesis)


def _llm_review_comment(item: DeepStockAnalysis, llm: Any, *, timeout_seconds: int) -> str:
    payload = {
        "ts_code": item.ts_code,
        "name": item.name,
        "trade_date": item.trade_date,
        "overall_score": item.overall_score,
        "verdict_label": item.verdict_label,
        "dimensions": [
            {
                "label": dim.label,
                "score": dim.score,
                "quality": dim.quality,
                "summary": dim.summary,
                "missing_fields": list(dim.missing_fields),
            }
            for dim in item.dimensions
        ],
        "vote_distribution": item.synthesis.get("vote_distribution"),
        "bull_reasons": item.synthesis.get("bull_reasons"),
        "bear_reasons": item.synthesis.get("bear_reasons"),
        "risks": item.synthesis.get("risks"),
        "missing_fields": list(item.missing_fields),
        "data_sources": item.data_sources,
    }
    prompt = (
        "请只基于以下 SATS 原生个股深研结构化证据，给出不超过120字的投委会补充评语；"
        "不得编造价格、财务数字、新闻、公告、目标价或确定性投资承诺，必须点明主要数据缺口：\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    response = llm.chat([{"role": "user", "content": prompt}], timeout=timeout_seconds)
    return str(getattr(response, "content", "") or "").strip()


def _build_optional(factory: Callable[[], Any]) -> Any | None:
    try:
        return factory()
    except Exception:
        return None


def _rules_for_investor(investor_id: str, f: dict[str, Any]) -> list[tuple[str, bool]]:
    roe = _num(f.get("roe")) or 0
    debt = _num(f.get("debt_to_assets")) or 999
    pe = _num(f.get("pe")) or 999
    pb = _num(f.get("pb")) or 999
    rev_g = _num(f.get("revenue_growth")) or 0
    profit_g = _num(f.get("profit_growth")) or 0
    ma_bull = f.get("ma_alignment") == "多头排列"
    macd = str(f.get("macd_signal") or "")
    main_flow = _num(f.get("main_net_amount_5d")) or 0
    hot = bool(f.get("hot_sectors"))
    base = {
        "buffett": [("ROE>=15", roe >= 15), ("负债率<=50", debt <= 50), ("估值不过热", pe <= 35), ("业务质量可见", bool(f.get("industry")))],
        "munger": [("高质量ROE", roe >= 12), ("低杠杆", debt <= 45), ("不过度交易", pe <= 40), ("风险红灯少", not _risk_flags(f))],
        "graham": [("PE<15", pe < 15), ("PB<1.5", pb < 1.5), ("负债率可控", debt <= 60), ("盈利为正", roe > 0)],
        "fisher": [("营收增长", rev_g >= 10), ("利润增长", profit_g >= 10), ("行业信息可见", bool(f.get("industry"))), ("ROE不弱", roe >= 10)],
        "lynch": [("成长性为正", rev_g > 0), ("PE未失控", pe <= 45), ("盈利增长", profit_g > 0), ("业务可理解", bool(f.get("industry")))],
        "oneill": [("趋势多头", ma_bull), ("MACD不弱", "金叉" in macd or macd == "多头"), ("成长较快", profit_g >= 15), ("热点加成", hot)],
        "soros": [("热点/反身性", hot), ("资金正反馈", main_flow > 0), ("趋势确认", ma_bull), ("估值可承受", pe <= 60)],
        "dalio": [("杠杆风险低", debt <= 60), ("市场环境不差", True), ("现金流/盈利可见", roe > 0), ("风险分散", pe <= 60)],
        "livermore": [("趋势多头", ma_bull), ("MACD多头", "金叉" in macd or macd == "多头"), ("资金配合", main_flow > 0), ("不过度超买", (_num(f.get("rsi6")) or 50) <= 80)],
        "minervini": [("均线强势", ma_bull), ("盈利增长", profit_g > 0), ("相对强势线索", hot), ("风险可控", debt <= 70)],
        "duan": [("好生意线索", roe >= 12), ("价格不离谱", pe <= 40), ("负债温和", debt <= 55), ("行业清楚", bool(f.get("industry")))],
        "zhao_lg": [("热点题材", hot), ("资金流入", main_flow > 0), ("趋势可打", ma_bull), ("市值/估值不过重", pe <= 80)],
    }
    return base.get(investor_id, [])


def _synthesize(context: _StockContext, dimensions: list[DeepDimensionResult], votes: tuple[DeepInvestorVote, ...]) -> dict[str, Any]:
    total_weight = sum(item.weight for item in dimensions) or 1.0
    weighted = sum(item.score * item.weight for item in dimensions)
    overall = round(weighted / total_weight * 10, 1)
    verdict = _verdict(overall)
    strong = sorted([item for item in dimensions if item.score >= 7], key=lambda item: item.score, reverse=True)
    weak = sorted([item for item in dimensions if item.score <= 3], key=lambda item: item.score)
    bullish_votes = sum(1 for vote in votes if vote.signal == "bullish")
    bearish_votes = sum(1 for vote in votes if vote.signal == "bearish")
    core = f"{_stock_name(context)} 综合评分 {overall:.1f}/100，{verdict}；面板看多 {bullish_votes}、看空 {bearish_votes}。"
    risks = [item.summary for item in weak[:4] if item.summary]
    if not risks:
        risks = ["未发现显著低分维度，但仍需结合仓位和交易计划控制风险"]
    return {
        "overall_score": overall,
        "verdict_label": verdict,
        "core_conclusion": core,
        "bull_reasons": [f"{item.label}: {item.summary}" for item in strong[:3]],
        "bear_reasons": [f"{item.label}: {item.summary}" for item in weak[:3]],
        "risks": risks,
        "vote_distribution": {
            "bullish": bullish_votes,
            "neutral": sum(1 for vote in votes if vote.signal == "neutral"),
            "bearish": bearish_votes,
        },
        "observation_zones": _observation_zones(context),
        "data_completeness": {
            "full": sum(1 for item in dimensions if item.quality == "full"),
            "partial": sum(1 for item in dimensions if item.quality == "partial"),
            "missing": sum(1 for item in dimensions if item.quality == "missing"),
        },
    }


def _observation_zones(context: _StockContext) -> dict[str, str]:
    fund = context.indicator.fundamentals
    pe = _num(fund.get("pe"))
    pb = _num(fund.get("pb"))
    support = context.indicator.support_resistance.get("support") or []
    zones: dict[str, str] = {}
    if pe is not None:
        zones["valuation"] = f"当前 PE {_fmt(pe)}；若盈利质量不改善，高 PE 区间需降低仓位假设"
    if pb is not None:
        zones["balance_sheet"] = f"当前 PB {_fmt(pb)}；资产重行业需结合 ROE 判断 PB 是否合理"
    if support:
        zones["technical"] = f"最近支撑位约 {support[0]}，跌破后技术面需重新评估"
    return zones


def _dimension(
    key: str,
    label: str,
    score: float,
    weight: float,
    evidence: dict[str, Any],
    missing: tuple[str, ...] | list[str],
    data_sources: dict[str, str],
    summary: str,
) -> DeepDimensionResult:
    quality = "full" if not missing else "partial" if _has_any_value(evidence) else "missing"
    return DeepDimensionResult(
        key=key,
        label=label,
        score=round(_clamp(score, 0, 10), 1),
        weight=weight,
        quality=quality,
        summary=summary,
        evidence={key: _jsonable(value) for key, value in evidence.items()},
        missing_fields=tuple(missing),
        data_sources=data_sources,
    )


def _supported_common_a_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    supported: list[str] = []
    rejected: list[str] = []
    for symbol in symbols:
        raw = str(symbol or "").strip().upper()
        code = raw.split(".", 1)[0]
        if not raw.endswith(SUPPORTED_MARKETS) or not code.isdigit() or not code.startswith(("0", "3", "6")):
            rejected.append(raw)
            continue
        supported.append(raw)
    return supported, rejected


def _has_daily_data(item: IndicatorInput) -> bool:
    return item.daily is not None and not item.daily.empty


def _safe_call(loader: Callable[[], Any], default: Any) -> Any:
    try:
        value = loader()
    except Exception:
        return default
    return default if value is None else value


def _stock_name(context: _StockContext) -> str:
    return _first_text(
        context.indicator.name,
        context.item.stock_basic.get("name"),
        context.quote.get("name"),
        context.fundamental_extra.get("name"),
    )


def _industry(context: _StockContext) -> str:
    return _first_text(
        context.item.stock_basic.get("industry"),
        context.item.stock_basic.get("sw_industry"),
        context.item.stock_basic.get("sector"),
        context.fundamental_extra.get("industry"),
    )


def _stock_hot_sectors(context: _StockContext) -> list[dict[str, Any]]:
    mapping = context.hot_sector.get("stock_hot_sectors") if isinstance(context.hot_sector, dict) else {}
    sectors = mapping.get(context.item.ts_code, []) if isinstance(mapping, dict) else []
    if isinstance(sectors, list):
        return sectors[:5]
    return []


def _data_sources(context: _StockContext) -> dict[str, str]:
    sources = dict(context.item.data_sources or {})
    sources.update({k: str(v) for k, v in context.indicator.data_sources.items() if v})
    if context.quote:
        sources.setdefault("quote", "AStockDataProvider.load_realtime_quote_lookup")
    if context.hot_sector:
        sources.setdefault("hot_sector", str(context.hot_sector.get("data_source") or "AStockDataProvider.load_hot_sector_context"))
    return sources


def _growth(frame: pd.DataFrame, columns: list[str]) -> float | None:
    if frame is None or frame.empty:
        return None
    data = frame.copy()
    if "end_date" in data.columns:
        data = data.sort_values("end_date")
    elif "ann_date" in data.columns:
        data = data.sort_values("ann_date")
    for column in columns:
        if column not in data.columns:
            continue
        values = pd.to_numeric(data[column], errors="coerce").dropna()
        if len(values) < 2:
            return None
        prev = float(values.iloc[-2])
        latest = float(values.iloc[-1])
        if prev == 0:
            return None
        return round((latest / abs(prev) - 1.0) * 100, 2)
    return None


def _risk_flags(f: dict[str, Any]) -> list[str]:
    flags = []
    debt = _num(f.get("debt_to_assets"))
    pe = _num(f.get("pe"))
    if debt is not None and debt > 70:
        flags.append("高负债")
    if pe is not None and pe > 60:
        flags.append("高估值")
    if f.get("ma_alignment") == "空头排列":
        flags.append("空头趋势")
    return flags


def _vote_headline(investor_id: str, f: dict[str, Any], signal: str) -> str:
    name = str(f.get("name") or "")
    if investor_id in {"buffett", "munger", "duan"}:
        return f"{name} ROE {_fmt(f.get('roe'))}、PE {_fmt(f.get('pe'))}，质量与价格共同决定态度"
    if investor_id in {"graham"}:
        return f"PE {_fmt(f.get('pe'))} / PB {_fmt(f.get('pb'))} 是防御型安全边际核心"
    if investor_id in {"lynch", "fisher"}:
        return f"营收增速 {_fmt_pct(f.get('revenue_growth'))}、利润增速 {_fmt_pct(f.get('profit_growth'))}"
    if investor_id in {"oneill", "livermore", "minervini"}:
        return f"趋势 {f.get('ma_alignment') or '未知'}，MACD {f.get('macd_signal') or '未知'}"
    if investor_id in {"zhao_lg", "soros"}:
        return f"热点 {'有' if f.get('hot_sectors') else '无'}，5日资金 {_fmt(f.get('main_net_amount_5d'))}"
    return f"{signal}，基于 SATS v1 深研规则骨架"


def _verdict(score: float) -> str:
    if score >= 75:
        return "值得重点跟踪"
    if score >= 60:
        return "可以蹲一蹲"
    if score >= 45:
        return "观望优先"
    if score >= 30:
        return "风险偏高"
    return "证据不足或暂不适合"


def _basic_summary(evidence: dict[str, Any]) -> str:
    return f"{evidence.get('name') or '未知公司'}，价格 {_fmt(evidence.get('price'))}，行业 {evidence.get('industry') or '未知'}"


def _score_presence(evidence: dict[str, Any], fields: list[str], *, base: float, per_field: float) -> float:
    return min(base + sum(per_field for field in fields if evidence.get(field) not in (None, "", [])), 10.0)


def _missing(evidence: dict[str, Any], fields: list[str]) -> tuple[str, ...]:
    return tuple(field for field in fields if evidence.get(field) in (None, "", []))


def _has_any_value(evidence: dict[str, Any]) -> bool:
    return any(value not in (None, "", [], {}) for value in evidence.values())


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def _first_number(*values: Any) -> float | None:
    for value in values:
        num = _num(value)
        if num is not None and num > 0:
            return num
    return None


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    num = _num(value)
    return "N/A" if num is None else f"{num:.2f}"


def _fmt_pct(value: Any) -> str:
    num = _num(value)
    return "N/A" if num is None else f"{num:.2f}%"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value
