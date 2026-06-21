from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.data.resolver import MARKET_BREADTH_MIN_COUNT, MarketDataResolver
from sats.storage.duckdb import DuckDBStorage
from sats.stock_question import extract_trade_date
from sats.symbols import normalize_ts_code


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MARKET_LOOKBACK_DAYS = 120
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
)
SUPPORTED_MARKET_DIMENSIONS: tuple[str, ...] = (
    "core_indices",
    "market_breadth",
    "limit_sentiment",
    "hot_sectors",
)
MARKET_DIMENSION_ALIASES = {
    "indices": "core_indices",
    "index": "core_indices",
    "breadth": "market_breadth",
    "sentiment": "limit_sentiment",
    "sectors": "hot_sectors",
    "sector": "hot_sectors",
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
    payload = get_a_share_market_context(
        settings=settings,
        trade_date=trade_date or extract_trade_date(message) or _today(),
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

    daily = pd.DataFrame()
    quotes = pd.DataFrame()
    daily_source = "not_requested"
    quote_source = "not_requested"
    effective_trade_date = trade_date
    if "core_indices" in requested_dimensions:
        start_date = _days_before(trade_date, max(lookback_days * 2, 180))
        daily = _market_index_daily_frame(
            resolver.load_index_daily(symbols, start_date=start_date, end_date=trade_date)
        )
        daily_source = str(daily.attrs.get("data_source") or "unavailable") if not daily.empty else "unavailable"
        if daily.empty:
            raise ValueError(f"{trade_date} 无法获取 A 股核心指数真实日线数据，已停止调用 LLM。")
        effective_trade_date = str(daily["trade_date"].astype(str).max() or trade_date)
        quotes = resolver.load_realtime_quotes(symbols)
        quote_source = str(quotes.attrs.get("data_source") or "unavailable") if not quotes.empty else "unavailable"
    breadth: dict[str, Any] = {}
    breadth_source = "not_requested"
    if "market_breadth" in requested_dimensions:
        breadth, breadth_source = resolver.load_market_breadth(
            as_of_date=effective_trade_date,
            min_count=int(getattr(settings, "market_breadth_min_count", MARKET_BREADTH_MIN_COUNT)),
        )
    limit_sentiment: dict[str, Any] = {}
    limit_sentiment_source = "not_requested"
    if "limit_sentiment" in requested_dimensions:
        limit_sentiment = _safe_limit_sentiment(provider, effective_trade_date)
        limit_sentiment_source = str(limit_sentiment.get("data_source") or "unavailable")
    hot_sector_context: dict[str, Any] = {}
    hot_sector_source = "not_requested"
    if "hot_sectors" in requested_dimensions:
        hot_sector_context = _safe_hot_sector_context(provider, effective_trade_date, settings=settings)
        hot_sector_source = _hot_sector_source(hot_sector_context)
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
        "data_sources": {
            "index_daily": daily_source,
            "index_quote": quote_source,
            "market_breadth": breadth_source,
            "limit_sentiment": limit_sentiment_source,
            "hot_sector_context": hot_sector_source,
        },
        "missing_fields": _missing_fields(
            symbols,
            daily,
            quotes,
            breadth,
            limit_sentiment,
            hot_sector_context,
            requested_dimensions=requested_dimensions,
        ),
        "data_policy": "SATS uses DuckDB-first resolvers for real A-share index and breadth data before calling the LLM.",
    }
    return _jsonable(payload)


def extract_market_horizons(message: str) -> tuple[str, ...]:
    text = str(message or "")
    horizons: list[str] = []
    if any(term in text for term in ("今天", "今日", "当前", "现在", "盘中", "盘后")):
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
        close = _safe_float(quote.get("close")) or _safe_float(latest.get("close"))
        pct_chg = _safe_float(quote.get("pct_chg")) or _safe_float(latest.get("pct_chg"))
        payloads.append(
            {
                "ts_code": symbol,
                "name": str(quote.get("name") or names.get(symbol) or symbol),
                "trade_date": str(latest.get("trade_date") or ""),
                "latest": {
                    "close": close,
                    "pct_chg": pct_chg,
                    "amount": _safe_float(quote.get("amount")) or _safe_float(latest.get("amount")),
                    "vol": _safe_float(quote.get("vol")) or _safe_float(latest.get("vol")),
                },
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
    if "market_breadth" in requested and not breadth:
        missing.append("market_breadth")
    if "limit_sentiment" in requested:
        if not limit_sentiment:
            missing.append("limit_sentiment")
        else:
            if str(limit_sentiment.get("data_source") or "") in {"", "unavailable"}:
                missing.append("limit_sentiment")
            missing.extend(str(item) for item in limit_sentiment.get("missing_fields") or [])
    if "hot_sectors" in requested:
        if not hot_sector_context:
            missing.append("hot_sector_context")
        else:
            missing.extend(str(item) for item in hot_sector_context.get("missing_fields") or [])
    return missing


def _safe_limit_sentiment(provider: Any, trade_date: str) -> dict[str, Any]:
    if not hasattr(provider, "load_limit_sentiment"):
        return {}
    try:
        payload = provider.load_limit_sentiment(trade_date)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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
        "不得编造价格、涨跌幅、成交量、新闻、政策、公告、题材或资金数据。"
        "limit_sentiment 来自涨停、跌停、炸板统计；若相关字段缺失，不得补猜炸板数或情绪阶段。"
        "hot_sector_context 来自同花顺行业/概念热点；若缺失，不得补猜热点板块。"
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
