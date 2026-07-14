from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.data.resolver import MARKET_BREADTH_MIN_COUNT, MarketDataResolver, should_refresh_current_day
from sats.storage.duckdb import DuckDBStorage
from sats.stock_question import extract_natural_trade_date
from sats.symbols import normalize_ts_code


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MARKET_LOOKBACK_DAYS = 120
DEFAULT_MARKET_DIMENSION_TIMEOUT_SECONDS = 30.0
DEFAULT_MARKET_INDICES: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("399330.SZ", "深证100"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000688.SH", "科创50"),
    ("899050.BJ", "北证50"),
)
DEFAULT_MARKET_DIMENSIONS: tuple[str, ...] = (
    "core_indices",
    "market_breadth",
    "limit_sentiment",
    "hot_sectors",
    "fund_flow",
    "catalysts",
)
SUPPORTED_MARKET_DIMENSIONS: tuple[str, ...] = (
    "core_indices",
    "market_breadth",
    "limit_sentiment",
    "hot_sectors",
    "fund_flow",
    "catalysts",
)
MARKET_DIMENSION_ALIASES = {
    "indices": "core_indices",
    "index": "core_indices",
    "breadth": "market_breadth",
    "sentiment": "limit_sentiment",
    "sectors": "hot_sectors",
    "sector": "hot_sectors",
    "fund_flow": "fund_flow",
    "moneyflow": "fund_flow",
    "资金流": "fund_flow",
    "catalysts": "catalysts",
    "news": "catalysts",
    "催化": "catalysts",
    "新闻公告": "catalysts",
}
SUPPORTED_MARKET_HORIZONS: tuple[str, ...] = ("today", "tomorrow", "day_after_tomorrow", "next_week")
_INDEX_ALIASES = {
    "上证": "000001.SH",
    "上证指数": "000001.SH",
    "沪指": "000001.SH",
    "深成指": "399001.SZ",
    "深证成指": "399001.SZ",
    "创业板": "399006.SZ",
    "创业板指": "399006.SZ",
    "深证100": "399330.SZ",
    "深100": "399330.SZ",
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "科创50": "000688.SH",
    "北证50": "899050.BJ",
}
_MARKET_KEYWORDS = (
    "大盘",
    "上证",
    "沪指",
    "深成指",
    "深证成指",
    "创业板",
    "深证100",
    "深100",
    "沪深300",
    "中证500",
    "科创50",
    "北证50",
    "指数",
    "明天走势",
    "下周走势",
)
_A_SHARE_CONTEXT_WORDS = ("走势", "分析", "预测", "行情", "市场", "今天", "明天", "下周", "涨跌")


@dataclass(frozen=True, slots=True)
class MarketLLMContext:
    trade_date: str
    payload: dict[str, Any]
    system_message: str


def build_market_llm_context(
    message: str,
    *,
    settings: Settings,
    trade_date: str | None = None,
    indices: Sequence[str] | None = None,
    dimensions: Sequence[str] | None = None,
    horizons: Sequence[str] | None = None,
    market_plan_source: str | None = None,
    force: bool = False,
    tickflow_provider: Any | None = None,
    tushare_provider: Any | None = None,
    akshare_provider: Any | None = None,
) -> MarketLLMContext | None:
    if not force and not is_market_question(message):
        return None
    resolved_trade_date = trade_date or extract_natural_trade_date(message) or _today()
    payload = get_a_share_market_context(
        settings=settings,
        trade_date=resolved_trade_date,
        horizons=horizons or extract_market_horizons(message),
        indices=indices,
        dimensions=dimensions,
        market_plan_source=market_plan_source,
        require_complete_market=is_market_question(message),
        tickflow_provider=tickflow_provider,
        tushare_provider=tushare_provider,
        akshare_provider=akshare_provider,
    )
    return MarketLLMContext(
        trade_date=str(payload["trade_date"]),
        payload=payload,
        system_message=_system_message(payload),
    )


def is_market_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "a股" in lowered and any(word in text for word in _A_SHARE_CONTEXT_WORDS):
        return True
    if "市场" in text and any(word in text for word in ("走势", "行情", "涨跌", "怎么看", "怎么走", "表现", "分析", "预测")):
        return True
    return any(keyword in text for keyword in _MARKET_KEYWORDS)


def get_a_share_market_context(
    *,
    settings: Settings,
    trade_date: str | None = None,
    horizon: str | None = None,
    horizons: Sequence[str] | None = None,
    indices: Sequence[str] | None = None,
    dimensions: Sequence[str] | None = None,
    market_plan_source: str | None = None,
    require_complete_market: bool = False,
    lookback_days: int = MARKET_LOOKBACK_DAYS,
    astock_provider: Any | None = None,
    market_data_resolver: Any | None = None,
    refresh_current_day: bool = True,
    tickflow_provider: Any | None = None,
    tushare_provider: Any | None = None,
    akshare_provider: Any | None = None,
) -> dict[str, Any]:
    trade_date = trade_date or _today()
    resolved_dimensions, unsupported_dimensions = resolve_market_dimensions_with_warnings(dimensions)
    requested_dimensions = list(resolved_dimensions)
    if require_complete_market:
        requested_dimensions = _dedupe([*DEFAULT_MARKET_DIMENSIONS, *requested_dimensions])
    requested_horizons = resolve_market_horizons(horizons or ([horizon] if horizon else None))
    requested_indices = list(resolve_market_indices(indices))
    index_map = _resolve_indices(requested_indices) if "core_indices" in requested_dimensions else {}
    if "core_indices" in requested_dimensions and not requested_indices:
        requested_indices = list(index_map)
    symbols = list(index_map)
    provider = astock_provider or AStockDataProvider(
        settings,
        tickflow_provider=tickflow_provider,
        tushare_provider=tushare_provider,
        akshare_provider=akshare_provider,
    )
    storage = DuckDBStorage(Path(getattr(settings, "db_path")))
    resolver = market_data_resolver or MarketDataResolver(settings, storage=storage, provider=provider)
    dimension_timeout = _dimension_timeout_seconds(settings)

    daily = pd.DataFrame()
    quotes = pd.DataFrame()
    daily_source = "not_requested"
    quote_source = "not_requested"
    effective_trade_date = trade_date
    if "core_indices" in requested_dimensions:
        start_date = _days_before(trade_date, max(lookback_days * 2, 180))
        daily = _market_index_daily_frame(
            resolver.load_index_daily(
                symbols,
                start_date=start_date,
                end_date=trade_date,
                refresh_current_day=refresh_current_day,
            )
        )
        daily_source = str(daily.attrs.get("data_source") or "unavailable") if not daily.empty else "unavailable"
        if daily.empty:
            raise ValueError(f"{trade_date} 无法获取 A 股核心指数真实日线数据，已停止调用 LLM。")
        effective_trade_date = str(daily["trade_date"].astype(str).max() or trade_date)
        quotes = resolver.load_index_quotes(symbols, index_daily=daily)
        quote_source = str(quotes.attrs.get("data_source") or "unavailable") if not quotes.empty else "unavailable"
        quote_trade_date = _latest_frame_text(quotes, "trade_date")
        if quote_trade_date and quote_trade_date > effective_trade_date:
            effective_trade_date = quote_trade_date
    dimension_specs: dict[str, tuple[Any, Any]] = {}
    if "market_breadth" in requested_dimensions:
        dimension_specs["market_breadth"] = (
            lambda: resolver.load_market_breadth(
                as_of_date=effective_trade_date,
                min_count=int(getattr(settings, "market_breadth_min_count", MARKET_BREADTH_MIN_COUNT)),
            ),
            lambda reason: (
                {"trade_date": effective_trade_date, "data_source": "unavailable", "missing_fields": [reason]},
                "unavailable",
            ),
        )
    if "limit_sentiment" in requested_dimensions:
        dimension_specs["limit_sentiment"] = (
            lambda: _safe_limit_sentiment(provider, effective_trade_date),
            lambda reason: {"trade_date": effective_trade_date, "data_source": "unavailable", "missing_fields": [reason]},
        )
    if "hot_sectors" in requested_dimensions:
        dimension_specs["hot_sector_context"] = (
            lambda: _safe_hot_sector_context(provider, effective_trade_date, settings=settings),
            lambda reason: {"trade_date": effective_trade_date, "missing_fields": [reason], "data_sources": {}},
        )
    if "fund_flow" in requested_dimensions:
        dimension_specs["fund_flow"] = (
            lambda: _safe_fund_flow(provider, effective_trade_date),
            lambda reason: _empty_fund_flow(effective_trade_date, [reason, "hsgt_fund_flow:unavailable"]),
        )
    if "catalysts" in requested_dimensions:
        dimension_specs["catalysts"] = (
            lambda: _safe_catalysts(provider, effective_trade_date),
            lambda reason: _empty_catalysts(effective_trade_date, [reason]),
        )
    dimension_results = _load_dimensions_with_deadline(dimension_specs, timeout_seconds=dimension_timeout)

    breadth: dict[str, Any] = {}
    breadth_source = "not_requested"
    if "market_breadth" in requested_dimensions:
        breadth, breadth_source = dimension_results["market_breadth"]

    limit_sentiment: dict[str, Any] = {}
    limit_sentiment_source = "not_requested"
    if "limit_sentiment" in requested_dimensions:
        limit_sentiment = dimension_results["limit_sentiment"]
        if not _limit_sentiment_is_usable(limit_sentiment):
            breadth_fallback = _limit_sentiment_from_breadth(effective_trade_date, breadth)
            if breadth_fallback:
                existing_missing = list(limit_sentiment.get("missing_fields") or []) if isinstance(limit_sentiment, dict) else []
                breadth_fallback["missing_fields"] = _dedupe([*existing_missing, *list(breadth_fallback.get("missing_fields") or [])])
                limit_sentiment = breadth_fallback
        limit_sentiment_source = str(limit_sentiment.get("data_source") or "unavailable")

    hot_sector_context: dict[str, Any] = {}
    hot_sector_source = "not_requested"
    if "hot_sectors" in requested_dimensions:
        hot_sector_context = dimension_results["hot_sector_context"]
        hot_sector_source = _hot_sector_source(hot_sector_context)

    fund_flow: dict[str, Any] = {}
    fund_flow_source = "not_requested"
    if "fund_flow" in requested_dimensions:
        fund_flow = dimension_results["fund_flow"]
        fund_flow_source = _fund_flow_source(fund_flow)
    hot_sectors = _market_hot_sector_rows(hot_sector_context, fund_flow=fund_flow)
    hot_sectors_source = hot_sector_source if hot_sector_context.get("hot_industries") or hot_sector_context.get("hot_concepts") else (
        str(((fund_flow.get("data_sources") or {}).get("sector")) or "unavailable") if hot_sectors else "unavailable"
    )

    catalysts: dict[str, Any] = {}
    catalysts_source = "not_requested"
    if "catalysts" in requested_dimensions:
        catalysts = dimension_results["catalysts"]
        catalysts_source = str(catalysts.get("data_source") or "unavailable")
    payload = {
        "user_intent": "a_share_market_analysis",
        "requested_as_of_date": trade_date,
        "trade_date": effective_trade_date,
        "periods": _market_periods(trade_date, effective_trade_date),
        "requested_indices": requested_indices,
        "requested_dimensions": requested_dimensions,
        "requested_horizons": requested_horizons,
        "market_plan_source": market_plan_source or ("default" if not indices else "explicit"),
        "warnings": [f"unsupported_market_dimension:{item}" for item in unsupported_dimensions],
        "indices": _index_payloads(symbols, index_map, daily, quotes, lookback_days=lookback_days),
        "market_breadth": breadth,
        "limit_sentiment": limit_sentiment,
        "hot_sector_context": hot_sector_context,
        "hot_sectors": hot_sectors,
        "fund_flow": fund_flow,
        "catalysts": catalysts,
        "data_sources": {
            "index_daily": daily_source,
            "index_quote": quote_source,
            "market_breadth": breadth_source,
            "limit_sentiment": limit_sentiment_source,
            "hot_sector_context": hot_sector_source,
            "hot_sectors": hot_sectors_source,
            "fund_flow": fund_flow_source,
            "catalysts": catalysts_source,
        },
        "freshness": _freshness_payload(
            requested_date=trade_date,
            effective_trade_date=effective_trade_date,
            refresh_current_day=refresh_current_day,
            index_daily=daily,
            index_quote=quotes,
            market_breadth=breadth,
            hot_sector_context=hot_sector_context,
            fund_flow=fund_flow,
            catalysts=catalysts,
        ),
        "missing_fields": _missing_fields(
            symbols,
            daily,
            quotes,
            breadth,
            limit_sentiment,
            hot_sector_context,
            fund_flow,
            catalysts,
            requested_dimensions=requested_dimensions,
        ),
        "data_policy": "SATS uses DuckDB-first resolvers for real A-share index and breadth data before calling the LLM.",
    }
    return _jsonable(payload)


def extract_market_horizons(message: str) -> tuple[str, ...]:
    text = str(message or "")
    horizons: list[str] = []
    if any(term in text for term in ("今天", "今日", "当前", "现在", "盘中", "盘后", "这几天", "最近几天", "近几天", "近几日")):
        horizons.append("today")
    if any(term in text for term in ("明后天", "明后", "明天后天", "未来两天")):
        horizons.extend(["tomorrow", "day_after_tomorrow"])
    else:
        if "明天" in text or "次日" in text:
            horizons.append("tomorrow")
        if "后天" in text:
            horizons.append("day_after_tomorrow")
    if "下周" in text or "未来一周" in text:
        horizons.append("next_week")
    if not horizons:
        horizons.append("today")
    return tuple(resolve_market_horizons(horizons))


def extract_explicit_market_indices(message: str) -> tuple[str, ...]:
    text = str(message or "")
    matched: list[tuple[int, str]] = []
    for alias, ts_code in sorted(_INDEX_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if not alias:
            continue
        position = text.find(alias)
        if position >= 0:
            matched.append((position, ts_code))
    matched.sort(key=lambda item: item[0])
    return tuple(_dedupe([ts_code for _, ts_code in matched]))


def resolve_market_indices(indices: Sequence[str] | None) -> tuple[str, ...]:
    if not indices:
        return ()
    result: list[str] = []
    for raw in indices:
        value = str(raw or "").strip()
        if not value:
            continue
        code = _INDEX_ALIASES.get(value) or normalize_ts_code(value)
        if code in _INDEX_ALIASES_NAME_LOOKUP:
            result.append(code)
    return tuple(_dedupe(result))


def resolve_market_dimensions(dimensions: Sequence[str] | None) -> tuple[str, ...]:
    resolved, _ = resolve_market_dimensions_with_warnings(dimensions)
    return resolved


def resolve_market_dimensions_with_warnings(
    dimensions: Sequence[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    values = [str(item or "").strip() for item in dimensions or ()]
    result: list[str] = []
    unsupported: list[str] = []
    for item in values:
        canonical = MARKET_DIMENSION_ALIASES.get(item, item)
        if canonical in SUPPORTED_MARKET_DIMENSIONS:
            result.append(canonical)
        elif item:
            unsupported.append(item)
    return tuple(_dedupe(result)) or DEFAULT_MARKET_DIMENSIONS, tuple(_dedupe(unsupported))


def resolve_market_horizons(horizons: Sequence[str] | None) -> list[str]:
    values = [str(item or "").strip() for item in horizons or ()]
    result = [item for item in values if item in SUPPORTED_MARKET_HORIZONS]
    return _dedupe(result) or ["today"]


def _resolve_indices(indices: Sequence[str] | None) -> dict[str, str]:
    if not indices:
        return dict(DEFAULT_MARKET_INDICES)
    result: dict[str, str] = {}
    for raw in indices:
        value = str(raw or "").strip()
        if not value:
            continue
        code = _INDEX_ALIASES.get(value) or normalize_ts_code(value)
        name = _INDEX_ALIASES_NAME_LOOKUP.get(code, "")
        result[code] = name or value
    return result or dict(DEFAULT_MARKET_INDICES)


def _index_payloads(
    symbols: list[str],
    names: dict[str, str],
    daily: pd.DataFrame,
    quotes: pd.DataFrame,
    *,
    lookback_days: int,
) -> list[dict[str, Any]]:
    quote_lookup = _records_by_symbol(quotes)
    payloads = []
    for symbol in symbols:
        data = daily[daily["ts_code"].astype(str) == symbol].sort_values("trade_date") if not daily.empty else pd.DataFrame()
        quote = quote_lookup.get(symbol, {})
        latest = data.tail(1).iloc[0].dropna().to_dict() if not data.empty else {}
        latest_daily_date = str(latest.get("trade_date") or "")
        quote_date = str(quote.get("trade_date") or "")
        latest_daily_close = _safe_float(latest.get("close"))
        quote_close = _safe_float(quote.get("close"))
        close = _first_not_none(quote_close, latest_daily_close)
        computed_pct_chg = _computed_current_day_pct_chg(
            latest_daily_close=latest_daily_close,
            quote_close=quote_close,
            latest_daily_date=latest_daily_date,
            quote_date=quote_date,
        )
        pct_chg = _first_not_none(computed_pct_chg, _safe_float(latest.get("pct_chg")), _safe_float(quote.get("pct_chg")))
        latest_data_source = str(latest.get("data_source") or daily.attrs.get("data_source") or "")
        quote_data_source = str(quote.get("data_source") or quotes.attrs.get("data_source") or "")
        payloads.append(
            {
                "ts_code": symbol,
                "name": str(quote.get("name") or names.get(symbol) or symbol),
                "trade_date": quote_date or latest_daily_date,
                "latest": {
                    "close": close,
                    "pct_chg": pct_chg,
                    "amount": _first_not_none(_safe_float(quote.get("amount")), _safe_float(latest.get("amount"))),
                    "vol": _first_not_none(_safe_float(quote.get("vol")), _safe_float(latest.get("vol"))),
                    "pct_chg_source": "computed_current_day_vs_previous_close"
                    if computed_pct_chg is not None
                    else ("index_daily" if _safe_float(latest.get("pct_chg")) is not None else "index_quote"),
                },
                "latest_daily": {
                    "trade_date": latest_daily_date,
                    "close": latest_daily_close,
                    "pct_chg": _safe_float(latest.get("pct_chg")),
                    "amount": _safe_float(latest.get("amount")),
                    "vol": _safe_float(latest.get("vol")),
                    "data_source": latest_data_source,
                },
                "current_day_quote": {
                    "trade_date": quote_date,
                    "close": quote_close,
                    "open": _safe_float(quote.get("open")),
                    "high": _safe_float(quote.get("high")),
                    "low": _safe_float(quote.get("low")),
                    "amount": _safe_float(quote.get("amount")),
                    "vol": _safe_float(quote.get("vol")),
                    "data_source": quote_data_source,
                },
                "computed_pct_chg": computed_pct_chg,
                "technical": _technical_metrics(data),
                "weekly": _weekly_metrics(data),
                "daily_tail": _records_tail(
                    data,
                    limit=lookback_days,
                    columns=["trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"],
                ),
                "quote": quote,
                "missing_fields": _index_missing_fields(symbol, data, quote),
            }
        )
    return payloads


def _computed_current_day_pct_chg(
    *,
    latest_daily_close: float | None,
    quote_close: float | None,
    latest_daily_date: str,
    quote_date: str,
) -> float | None:
    if not quote_date or not latest_daily_date or quote_date <= latest_daily_date:
        return None
    if quote_close is None or latest_daily_close in (None, 0):
        return None
    return round((quote_close / latest_daily_close - 1.0) * 100.0, 4)


def _weekly_metrics(data: pd.DataFrame) -> dict[str, Any]:
    if data is None or data.empty or "trade_date" not in data.columns:
        return {}
    ordered = data.sort_values("trade_date").copy()
    latest_date = str(ordered["trade_date"].astype(str).max())
    try:
        latest = datetime.strptime(latest_date, "%Y%m%d")
    except ValueError:
        return {}
    week_start = (latest - timedelta(days=latest.weekday())).strftime("%Y%m%d")
    week_end = (latest + timedelta(days=4 - latest.weekday())).strftime("%Y%m%d")
    dates = ordered["trade_date"].astype(str)
    week = ordered[(dates >= week_start) & (dates <= latest_date)]
    if week.empty:
        return {}
    first = week.iloc[0]
    last = week.iloc[-1]
    previous = ordered[dates < week_start].tail(1)
    base_close = _safe_float(previous.iloc[-1].get("close")) if not previous.empty else _safe_float(first.get("open"))
    last_close = _safe_float(last.get("close"))
    return {
        "calendar_start": week_start,
        "calendar_end": week_end,
        "data_start": str(first.get("trade_date") or ""),
        "data_end": str(last.get("trade_date") or ""),
        "trading_days": int(len(week)),
        "open": _safe_float(first.get("open")),
        "close": last_close,
        "high": _safe_float(pd.to_numeric(week.get("high"), errors="coerce").max()) if "high" in week.columns else None,
        "low": _safe_float(pd.to_numeric(week.get("low"), errors="coerce").min()) if "low" in week.columns else None,
        "pct_chg": ((last_close / base_close - 1.0) * 100.0) if last_close is not None and base_close not in (None, 0) else None,
        "vol": _safe_float(pd.to_numeric(week.get("vol"), errors="coerce").sum()) if "vol" in week.columns else None,
        "amount": _safe_float(pd.to_numeric(week.get("amount"), errors="coerce").sum()) if "amount" in week.columns else None,
    }


def _technical_metrics(data: pd.DataFrame) -> dict[str, Any]:
    if data is None or data.empty or "close" not in data.columns:
        return {"ma": {}, "volume_status": "unavailable"}
    closes = pd.to_numeric(data["close"], errors="coerce")
    result = {"ma": {f"ma{window}": _rolling_last(closes, window) for window in (5, 10, 20, 60)}}
    if "vol" not in data.columns or len(data) < 21:
        result["volume_status"] = "unavailable"
        return result
    vols = pd.to_numeric(data["vol"], errors="coerce")
    latest = _safe_float(vols.iloc[-1])
    avg20 = _safe_float(vols.iloc[-21:-1].mean())
    if latest is None or avg20 is None or avg20 <= 0:
        result["volume_status"] = "unavailable"
    elif latest >= avg20 * 1.3:
        result["volume_status"] = "放量"
    elif latest <= avg20 * 0.7:
        result["volume_status"] = "缩量"
    else:
        result["volume_status"] = "量能正常"
    return result


def _breadth_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    pct = pd.to_numeric(frame.get("pct_chg"), errors="coerce") if "pct_chg" in frame.columns else pd.Series(dtype=float)
    amount = pd.to_numeric(frame.get("amount"), errors="coerce") if "amount" in frame.columns else pd.Series(dtype=float)
    return {
        "total_count": int(len(frame)),
        "advancing_count": int((pct > 0).sum()),
        "declining_count": int((pct < 0).sum()),
        "flat_count": int((pct == 0).sum()),
        "limit_up_count": int((pct >= 9.8).sum()),
        "limit_down_count": int((pct <= -9.8).sum()),
        "total_amount": _safe_float(amount.sum()),
        "median_pct_chg": _safe_float(pct.median()),
    }


def _missing_fields(
    symbols: list[str],
    daily: pd.DataFrame,
    quotes: pd.DataFrame,
    breadth: dict[str, Any],
    limit_sentiment: dict[str, Any] | None = None,
    hot_sector_context: dict[str, Any] | None = None,
    fund_flow: dict[str, Any] | None = None,
    catalysts: dict[str, Any] | None = None,
    *,
    requested_dimensions: Sequence[str],
) -> list[str]:
    missing = []
    requested = set(requested_dimensions)
    if "core_indices" in requested:
        for symbol in _missing_symbols(symbols, daily):
            missing.append(f"index_daily:{symbol}")
        for symbol in _missing_symbols(symbols, quotes):
            missing.append(f"index_quote:{symbol}")
    if "market_breadth" in requested:
        if not breadth:
            missing.append("market_breadth")
        else:
            if str(breadth.get("data_source") or "") in {"", "unavailable"}:
                missing.append("market_breadth")
            missing.extend(str(item) for item in breadth.get("missing_fields") or [])
    if "limit_sentiment" in requested:
        if not limit_sentiment:
            missing.append("limit_sentiment")
        else:
            if str(limit_sentiment.get("data_source") or "") in {"", "unavailable"}:
                missing.append("limit_sentiment")
            missing.extend(str(item) for item in limit_sentiment.get("missing_fields") or [])
    if "hot_sectors" in requested:
        primary_hot = bool(
            (hot_sector_context or {}).get("hot_industries")
            or (hot_sector_context or {}).get("hot_concepts")
        )
        if not _market_hot_sector_rows(hot_sector_context or {}, fund_flow=fund_flow or {}):
            missing.append("hot_sector_context")
        elif primary_hot and hot_sector_context:
            missing.extend(str(item) for item in hot_sector_context.get("missing_fields") or [])
    if "fund_flow" in requested:
        if not fund_flow:
            missing.append("fund_flow")
        else:
            if str(fund_flow.get("data_source") or "") in {"", "unavailable"}:
                missing.append("fund_flow")
            missing.extend(str(item) for item in fund_flow.get("missing_fields") or [])
    if "catalysts" in requested:
        if not catalysts or str(catalysts.get("data_source") or "") in {"", "unavailable"}:
            missing.append("catalysts")
        if catalysts:
            missing.extend(str(item) for item in catalysts.get("missing_fields") or [])
    return missing


def _safe_limit_sentiment(provider: Any, trade_date: str, *, market_breadth: dict[str, Any] | None = None) -> dict[str, Any]:
    if not hasattr(provider, "load_limit_sentiment"):
        return _limit_sentiment_from_breadth(trade_date, market_breadth)
    try:
        payload = provider.load_limit_sentiment(
            trade_date,
            market_breadth=market_breadth,
            allow_realtime_fallback=False,
        )
    except TypeError:
        try:
            payload = provider.load_limit_sentiment(trade_date)
        except Exception:
            return _limit_sentiment_from_breadth(trade_date, market_breadth)
    except Exception:
        return _limit_sentiment_from_breadth(trade_date, market_breadth)
    if _limit_sentiment_is_usable(payload):
        return payload
    fallback = _limit_sentiment_from_breadth(trade_date, market_breadth)
    if fallback:
        missing = list(payload.get("missing_fields") or []) if isinstance(payload, dict) else []
        fallback["missing_fields"] = _dedupe([*missing, *list(fallback.get("missing_fields") or [])])
        return fallback
    return payload if isinstance(payload, dict) else {}


def _dimension_timeout_seconds(settings: Settings) -> float:
    raw = getattr(settings, "market_context_dimension_timeout_seconds", DEFAULT_MARKET_DIMENSION_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MARKET_DIMENSION_TIMEOUT_SECONDS
    return max(0.1, value)


def _load_dimensions_with_deadline(
    specs: dict[str, tuple[Any, Any]],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not specs:
        return {}
    results: queue.Queue[tuple[str, str, Any]] = queue.Queue(maxsize=len(specs))

    def _target(label: str, loader: Any) -> None:
        try:
            results.put((label, "ok", loader()), block=False)
        except Exception as exc:
            results.put((label, "error", exc), block=False)

    pending = set(specs)
    loaded: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_seconds
    for label, (loader, _fallback) in specs.items():
        threading.Thread(
            target=_target,
            args=(label, loader),
            name=f"sats-market-context-{label}",
            daemon=True,
        ).start()

    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            label, status, value = results.get(timeout=remaining)
        except queue.Empty:
            break
        if label not in pending:
            continue
        pending.remove(label)
        fallback = specs[label][1]
        loaded[label] = value if status == "ok" else fallback(f"{label}:{value}")

    for label in pending:
        fallback = specs[label][1]
        loaded[label] = fallback(f"{label}:timeout")
    return loaded


def _limit_sentiment_is_usable(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    source = str(payload.get("data_source") or "")
    if source in {"", "unavailable"}:
        return False
    missing = {str(item) for item in payload.get("missing_fields") or []}
    if "limit_sentiment:tushare_empty" in missing:
        return False
    return any(payload.get(key) not in (None, "") for key in ("limit_up_count", "limit_down_count", "broken_limit_count"))


def _limit_sentiment_from_breadth(trade_date: str, market_breadth: dict[str, Any] | None) -> dict[str, Any]:
    breadth = market_breadth if isinstance(market_breadth, dict) else {}
    if not breadth:
        return {}
    up = _first_not_none(breadth.get("limit_up_count"), breadth.get("up_limit_count"))
    down = _first_not_none(breadth.get("limit_down_count"), breadth.get("down_limit_count"))
    if up is None and down is None:
        return {}
    return {
        "trade_date": str(breadth.get("trade_date") or trade_date),
        "limit_up_count": int(up or 0),
        "limit_down_count": int(down or 0),
        "broken_limit_count": None,
        "data_source": "market_breadth_fallback",
        "missing_fields": ["broken_limit_count:tushare_unavailable"],
    }


def _safe_fund_flow(provider: Any, trade_date: str) -> dict[str, Any]:
    if not hasattr(provider, "fetch_tushare_dataset"):
        return _empty_fund_flow(trade_date, ["fund_flow:tushare_unavailable", "hsgt_fund_flow:unavailable"])
    market_payload = _safe_tushare_dataset(
        provider,
        "moneyflow_mkt_dc",
        {"trade_date": trade_date},
        fields=[
            "trade_date",
            "close",
            "pct_change",
            "net_amount",
            "net_amount_rate",
            "buy_elg_amount",
            "buy_lg_amount",
            "buy_md_amount",
            "buy_sm_amount",
        ],
        limit=20,
    )
    sector_payload = _safe_tushare_dataset(
        provider,
        "moneyflow_ind_dc",
        {"trade_date": trade_date},
        fields=[
            "trade_date",
            "ts_code",
            "name",
            "pct_change",
            "close",
            "net_amount",
            "net_amount_rate",
            "buy_elg_amount",
            "buy_lg_amount",
            "buy_md_amount",
            "buy_sm_amount",
        ],
        limit=1000,
    )
    northbound = _load_northbound_fund_flow(provider, trade_date)
    market_rows = _payload_rows(market_payload)
    sector_rows = _payload_rows(sector_payload)
    sector_top = sorted(sector_rows, key=lambda row: _safe_float(row.get("net_amount")) or 0.0, reverse=True)
    sector_bottom = sorted(sector_rows, key=lambda row: _safe_float(row.get("net_amount")) or 0.0)
    missing_fields = [
        *_payload_missing_fields(market_payload, "market_fund_flow"),
        *_payload_missing_fields(sector_payload, "sector_fund_flow"),
    ]
    if not market_rows:
        missing_fields.append("market_fund_flow:empty")
    if not sector_rows:
        missing_fields.append("sector_fund_flow:empty")
    if str(northbound.get("data_source") or "") == "unavailable":
        missing_fields.append("hsgt_fund_flow:unavailable")
    market_source = str(market_payload.get("data_source") or "unavailable")
    sector_source = str(sector_payload.get("data_source") or "unavailable")
    if market_source == "unavailable" and sector_source == "unavailable":
        missing_fields.append("fund_flow:tushare_unavailable")
    northbound_source = str(northbound.get("data_source") or "unavailable")
    data_source = "+".join(
        dict.fromkeys(
            item for item in (market_source, sector_source, northbound_source) if item and item != "unavailable"
        )
    ) or "unavailable"
    return {
        "trade_date": str(trade_date),
        "data_source": data_source,
        "market": _fund_flow_market_payload(
            market_rows[0] if market_rows else {},
            trade_date=trade_date,
            source=market_source,
            payload=market_payload,
        ),
        "sector_top": [_fund_flow_sector_row(row) for row in sector_top[:20]],
        "sector_bottom": [_fund_flow_sector_row(row) for row in sector_bottom[:20]],
        "northbound": northbound,
        "data_sources": {
            "market": market_source,
            "sector": sector_source,
            "northbound": str(northbound.get("data_source") or "unavailable"),
        },
        "missing_fields": _dedupe(missing_fields),
        "truncated": _payload_truncated(market_payload) or _payload_truncated(sector_payload),
    }


def _empty_fund_flow(trade_date: str, missing_fields: list[str]) -> dict[str, Any]:
    return {
        "trade_date": str(trade_date),
        "data_source": "unavailable",
        "market": {
            "trade_date": str(trade_date),
            "data_source": "unavailable",
            "missing_fields": list(missing_fields),
            "truncated": False,
        },
        "sector_top": [],
        "sector_bottom": [],
        "northbound": {
            "trade_date": str(trade_date),
            "data_source": "unavailable",
            "missing_fields": ["hsgt_fund_flow:unavailable"],
        },
        "data_sources": {"market": "unavailable", "sector": "unavailable", "northbound": "unavailable"},
        "missing_fields": list(missing_fields),
        "truncated": False,
    }


def _load_northbound_fund_flow(provider: Any, trade_date: str) -> dict[str, Any]:
    payload = _safe_tushare_dataset(
        provider,
        "moneyflow_hsgt",
        {"trade_date": trade_date},
        fields=["trade_date", "hgt", "sgt", "north_money", "ggt_ss", "ggt_sz", "south_money"],
        limit=10,
    )
    rows = _payload_rows(payload)
    if rows:
        row = rows[0]
        return _jsonable(
            {
                "trade_date": str(row.get("trade_date") or trade_date),
                "hgt": _safe_float(row.get("hgt")),
                "sgt": _safe_float(row.get("sgt")),
                "north_money": _safe_float(row.get("north_money")),
                "south_money": _safe_float(row.get("south_money")),
                "data_source": str(payload.get("data_source") or "tushare_moneyflow_hsgt"),
                "missing_fields": [],
            }
        )

    try:
        ak_payload = provider.fetch_akshare_dataset("stock_hsgt_fund_flow_summary_em", {}, limit=20)
    except Exception as exc:
        return {
            "trade_date": str(trade_date),
            "data_source": "unavailable",
            "missing_fields": [f"hsgt_fund_flow: {exc}"],
        }
    ak_rows = _payload_rows(ak_payload)
    hgt = None
    sgt = None
    row_date = ""
    for row in ak_rows:
        board = str(row.get("板块") or row.get("类型") or "")
        value = _safe_float(_first_not_none(row.get("成交净买额"), row.get("资金净流入")))
        row_date = max(row_date, str(row.get("交易日") or ""))
        if "沪股通" in board:
            hgt = value
        elif "深股通" in board:
            sgt = value
    if hgt is not None or sgt is not None:
        north_money = (hgt or 0.0) + (sgt or 0.0)
        return {
            "trade_date": row_date or str(trade_date),
            "hgt": hgt,
            "sgt": sgt,
            "north_money": north_money,
            "south_money": None,
            "data_source": str(ak_payload.get("data_source") or "akshare_stock_hsgt_fund_flow_summary_em"),
            "missing_fields": [],
        }
    return {
        "trade_date": str(trade_date),
        "data_source": "unavailable",
        "missing_fields": _dedupe(
            [
                *_payload_missing_fields(payload, "hsgt_fund_flow"),
                *_payload_missing_fields(ak_payload, "hsgt_fund_flow"),
            ]
        ),
    }


def _safe_catalysts(provider: Any, trade_date: str) -> dict[str, Any]:
    if not hasattr(provider, "load_market_catalysts"):
        return _empty_catalysts(trade_date, ["catalysts:provider_unavailable"])
    try:
        payload = provider.load_market_catalysts(trade_date, limit=20)
    except Exception as exc:
        return _empty_catalysts(trade_date, [f"catalysts:{exc}"])
    return payload if isinstance(payload, dict) else _empty_catalysts(trade_date, ["catalysts:invalid_payload"])


def _empty_catalysts(trade_date: str, missing_fields: list[str]) -> dict[str, Any]:
    return {
        "trade_date": str(trade_date),
        "news": [],
        "announcements": [],
        "data_source": "unavailable",
        "data_sources": {"news": "unavailable", "announcements": "unavailable"},
        "source_diagnostics": {},
        "missing_fields": list(missing_fields),
        "truncated": False,
    }


def _market_hot_sector_rows(hot_sector_context: dict[str, Any], *, fund_flow: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("hot_industries", "hot_concepts"):
        for item in hot_sector_context.get(key) or []:
            if not isinstance(item, dict):
                continue
            rows.append({**item, "ranking_basis": "tushare_ths_heat"})
    if rows:
        return sorted(rows, key=lambda item: _safe_float(item.get("heat_score")) or 0.0, reverse=True)[:30]
    for rank, item in enumerate(fund_flow.get("sector_top") or [], start=1):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        rows.append(
            {
                "rank": rank,
                "sector_code": item.get("ts_code"),
                "name": item.get("name"),
                "sector_type": "fund_flow_fallback",
                "latest_pct_chg": item.get("pct_change"),
                "net_amount": item.get("net_amount"),
                "ranking_basis": "sector_net_amount_fallback",
                "data_source": (fund_flow.get("data_sources") or {}).get("sector"),
            }
        )
    return rows[:20]


def _safe_tushare_dataset(provider: Any, dataset: str, params: dict[str, Any], *, fields: list[str], limit: int) -> dict[str, Any]:
    try:
        payload = provider.fetch_tushare_dataset(dataset, params, fields=fields, limit=limit)
    except Exception as exc:
        return {
            "dataset": dataset,
            "rows": [],
            "row_count": 0,
            "returned_row_count": 0,
            "data_source": "unavailable",
            "missing_fields": [f"{dataset}: {exc}"],
        }
    return payload if isinstance(payload, dict) else {}


def _fund_flow_market_payload(row: dict[str, Any], *, trade_date: str, source: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["trade_date"] = str(result.get("trade_date") or trade_date)
    result["data_source"] = source or "unavailable"
    result["missing_fields"] = _payload_missing_fields(payload, "market_fund_flow")
    result["truncated"] = _payload_truncated(payload)
    return _jsonable(result)


def _fund_flow_sector_row(row: dict[str, Any]) -> dict[str, Any]:
    return _jsonable(
        {
            "trade_date": row.get("trade_date"),
            "ts_code": row.get("ts_code"),
            "name": row.get("name"),
            "pct_change": _safe_float(row.get("pct_change")),
            "close": _safe_float(row.get("close")),
            "net_amount": _safe_float(row.get("net_amount")),
            "net_amount_rate": _safe_float(row.get("net_amount_rate")),
            "buy_elg_amount": _safe_float(row.get("buy_elg_amount")),
            "buy_lg_amount": _safe_float(row.get("buy_lg_amount")),
            "buy_md_amount": _safe_float(row.get("buy_md_amount")),
            "buy_sm_amount": _safe_float(row.get("buy_sm_amount")),
        }
    )


def _payload_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("data") if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _payload_missing_fields(payload: dict[str, Any], prefix: str) -> list[str]:
    missing = [str(item) for item in payload.get("missing_fields") or []] if isinstance(payload, dict) else []
    return missing or ([] if _payload_rows(payload) else [f"{prefix}:unavailable"])


def _payload_truncated(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if bool(payload.get("truncated", False)):
        return True
    try:
        return int(payload.get("row_count") or 0) > int(payload.get("returned_row_count") or 0)
    except (TypeError, ValueError):
        return False


def _fund_flow_source(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict) or not payload:
        return "unavailable"
    return str(payload.get("data_source") or "unavailable")


def _index_missing_fields(symbol: str, daily: pd.DataFrame, quote: dict[str, Any]) -> list[str]:
    missing = []
    if daily is None or daily.empty:
        missing.append(f"index_daily:{symbol}")
    if not quote:
        missing.append(f"index_quote:{symbol}")
    return missing


def _system_message(payload: dict[str, Any]) -> str:
    return (
        "以下是 SATS 在调用 LLM 前获取的真实 A 股大盘结构化数据。你只能基于这些数据回答；"
        "当这些结构化数据与用户粘贴数据、历史消息或会话摘要冲突时，以本次 SATS 结构化数据为准。"
        "不得编造价格、涨跌幅、成交量、新闻、政策、公告、题材或资金数据。"
        "limit_sentiment 来自涨停、跌停、炸板统计；若相关字段缺失，不得补猜炸板数或情绪阶段。"
        "hot_sector_context 来自同花顺行业/概念热点；若缺失，不得补猜热点板块。"
        "fund_flow 来自 Tushare 东方财富口径资金流；若缺失，不得补猜大盘、行业或北向资金。"
        "catalysts 来自 Tushare/AkShare 新闻公告；只能引用其中实际提供的标题、日期和来源。"
        "data.astock_fetch 的 rows/data 是有界样本；全市场涨跌分布、成交额和宽度只能使用聚合字段或明确未截断全量结果。"
        "market_breadth.total_amount 若 amount_basis=intraday_cumulative，只能表述为截至当前累计成交额；"
        "只有 turnover_comparison.status=ok 时，才能基于 turnover_comparison 写放量、缩量、成交萎缩或成交放大。"
        "不得把盘中累计成交额直接与前一交易日全天成交额比较。"
        "requested_indices、requested_dimensions 和 requested_horizons 代表本次大盘研究篮子与分析范围。"
        "如果字段在 missing_fields 中，必须说明该数据缺失。"
        "对明天、后天或下周走势只能使用情景、概率、关键点位和失效条件表达，不得断言必然走势。"
        "输出任何投资相关判断时必须说明仅供研究，不构成投资建议。\n"
        f"{json.dumps(_jsonable(payload), ensure_ascii=False, default=str)}"
    )


def _combine_daily_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    columns = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
    if not valid:
        return pd.DataFrame(columns=columns)
    data = pd.concat(valid, ignore_index=True, sort=False)
    for column in columns:
        if column not in data.columns:
            data[column] = None
    data["ts_code"] = data["ts_code"].astype(str).str.strip().str.upper()
    data["trade_date"] = data["trade_date"].astype(str)
    return data[columns].drop_duplicates(subset=["ts_code", "trade_date"], keep="last").sort_values(
        ["ts_code", "trade_date"]
    ).reset_index(drop=True)


def _market_index_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    source = str(frame.attrs.get("data_source") or "unavailable")
    provenance = frame.attrs.get("market_data_provenance") or []
    data = frame.copy()
    if "ts_code" not in data.columns and "index_code" in data.columns:
        data["ts_code"] = data["index_code"]
    data.attrs["data_source"] = source
    data.attrs["market_data_provenance"] = provenance
    return data


def _latest_frame_text(frame: pd.DataFrame, column: str) -> str:
    if frame is None or frame.empty or column not in frame.columns:
        return ""
    values = frame[column].dropna().astype(str)
    if values.empty:
        return ""
    return str(values.max() or "")


def _freshness_payload(
    *,
    requested_date: str,
    effective_trade_date: str,
    refresh_current_day: bool,
    index_daily: pd.DataFrame,
    index_quote: pd.DataFrame,
    market_breadth: dict[str, Any],
    hot_sector_context: dict[str, Any],
    fund_flow: dict[str, Any],
    catalysts: dict[str, Any],
) -> dict[str, Any]:
    refresh_requested = should_refresh_current_day(str(requested_date), refresh_current_day=refresh_current_day)
    warnings = []
    index_daily_freshness = _frame_freshness(index_daily, refresh_requested=refresh_requested)
    if refresh_requested and index_daily_freshness.get("cache_fallback"):
        warnings.append("index_daily:stale_cache_after_refresh_failure")
    hot_freshness = _hot_sector_freshness(hot_sector_context)
    if hot_freshness.get("cache_fallback"):
        warnings.append("hot_sector_context:cache_fallback")
    fund_flow_sources = fund_flow.get("data_sources") if isinstance(fund_flow, dict) else {}
    if not isinstance(fund_flow_sources, dict):
        fund_flow_sources = {}
    return {
        "requested_as_of_date": str(requested_date),
        "effective_trade_date": str(effective_trade_date),
        "refresh_current_day": bool(refresh_current_day),
        "current_day_refresh_requested": bool(refresh_requested),
        "index_daily": index_daily_freshness,
        "index_quote": _frame_freshness(index_quote, refresh_requested=True),
        "market_breadth": {
            "source": str(market_breadth.get("data_source") or ""),
            "is_fallback": bool(market_breadth.get("is_fallback", False)) if market_breadth else False,
            "trade_date": str(market_breadth.get("trade_date") or "") if market_breadth else "",
        },
        "hot_sector_context": hot_freshness,
        "fund_flow": {
            "source": _fund_flow_source(fund_flow),
            "trade_date": str(fund_flow.get("trade_date") or "") if fund_flow else "",
            "truncated": bool(fund_flow.get("truncated", False)) if fund_flow else False,
            "data_sources": fund_flow_sources,
        },
        "catalysts": {
            "source": str(catalysts.get("data_source") or "unavailable") if catalysts else "unavailable",
            "trade_date": str(catalysts.get("trade_date") or "") if catalysts else "",
            "truncated": bool(catalysts.get("truncated", False)) if catalysts else False,
            "data_sources": catalysts.get("data_sources") if isinstance(catalysts, dict) else {},
        },
        "warnings": warnings,
    }


def _frame_freshness(frame: pd.DataFrame, *, refresh_requested: bool) -> dict[str, Any]:
    source = str(frame.attrs.get("data_source") or "unavailable") if frame is not None and not frame.empty else "unavailable"
    provenance = frame.attrs.get("market_data_provenance") if frame is not None else None
    item = provenance[0] if isinstance(provenance, list) and provenance else {}
    fetched_at = str(item.get("fetched_at") or "") if isinstance(item, dict) else ""
    cache_hit = bool(item.get("cache_hit", False)) if isinstance(item, dict) else False
    return {
        "source": source,
        "refresh_requested": bool(refresh_requested),
        "cache_hit": cache_hit,
        "cache_fallback": source == "duckdb_cache_stale_current_day",
        "fetched_at": fetched_at,
    }


def _hot_sector_freshness(payload: dict[str, Any]) -> dict[str, Any]:
    source = _hot_sector_source(payload)
    data_sources = payload.get("data_sources") if isinstance(payload, dict) else {}
    fetched_at = payload.get("fetched_at") if isinstance(payload, dict) else ""
    return {
        "source": source,
        "cache_fallback": "duckdb_cache_or_unavailable" in source,
        "fetched_at": str(fetched_at or ""),
        "data_sources": data_sources if isinstance(data_sources, dict) else {},
    }


def _market_periods(requested_date: str, effective_trade_date: str) -> dict[str, Any]:
    try:
        requested = datetime.strptime(str(requested_date), "%Y%m%d")
    except ValueError:
        requested = datetime.strptime(str(effective_trade_date), "%Y%m%d")
    current_start = requested - timedelta(days=requested.weekday())
    current_end = current_start + timedelta(days=4)
    next_start = current_start + timedelta(days=7)
    next_end = next_start + timedelta(days=4)
    return {
        "current_week": {
            "start": current_start.strftime("%Y%m%d"),
            "end": current_end.strftime("%Y%m%d"),
            "data_through": str(effective_trade_date),
        },
        "next_week": {
            "start": next_start.strftime("%Y%m%d"),
            "end": next_end.strftime("%Y%m%d"),
        },
    }


def _records_by_symbol(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    return {
        str(row.get("ts_code")): _jsonable(row.dropna().to_dict())
        for _, row in frame.iterrows()
        if str(row.get("ts_code") or "").strip()
    }


def _records_tail(frame: pd.DataFrame, *, limit: int, columns: list[str]) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    data = frame.copy()
    existing = [column for column in columns if column in data.columns]
    return [_jsonable(row.dropna().to_dict()) for _, row in data[existing].tail(limit).iterrows()]


def _missing_symbols(symbols: list[str], frame: pd.DataFrame) -> list[str]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return list(symbols)
    present = set(frame["ts_code"].astype(str))
    return [symbol for symbol in symbols if symbol not in present]


def _rolling_last(values: pd.Series, window: int) -> float | None:
    if len(values) < window:
        return None
    return _safe_float(values.rolling(window).mean().iloc[-1])


def _safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _build_optional(factory):
    try:
        return factory()
    except Exception:
        return None


def _today() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")


def _days_before(trade_date: str, days: int) -> str:
    return (datetime.strptime(str(trade_date), "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _safe_hot_sector_context(provider: Any, trade_date: str, *, settings: Settings) -> dict[str, Any]:
    if not hasattr(provider, "load_hot_sector_context"):
        return {}
    db_path = getattr(settings, "db_path", None)
    if db_path is None:
        return {"missing_fields": ["hot_sector_context: db_path_unavailable"], "data_sources": {}}
    try:
        storage = DuckDBStorage(Path(db_path))
        payload = provider.load_hot_sector_context(trade_date, storage=storage, lookback_days=5)
    except Exception as exc:
        return {"missing_fields": [f"hot_sector_context: {exc}"], "data_sources": {}}
    return payload if isinstance(payload, dict) else {}


def _hot_sector_source(payload: dict[str, Any]) -> str:
    if not payload:
        return "unavailable"
    sources = payload.get("data_sources")
    if isinstance(sources, dict):
        values = [str(item).strip() for item in sources.values() if str(item).strip()]
        if values:
            return "+".join(values)
    return "unavailable"


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


_INDEX_ALIASES_NAME_LOOKUP = dict(DEFAULT_MARKET_INDICES)
