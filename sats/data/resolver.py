from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.indicators import IndicatorInput
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols


ANALYSIS_QUOTE_TTL_SECONDS = 60
TRADING_QUOTE_TTL_SECONDS = 30


@dataclass(frozen=True, slots=True)
class MarketDataProvenance:
    dataset: str
    source: str
    symbols: tuple[str, ...] = ()
    start: str = ""
    end: str = ""
    rows: int = 0
    cache_hit: bool = False
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketDataResolver:
    """DuckDB-first market data access with AStockDataProvider fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        storage: DuckDBStorage | None = None,
        provider: AStockDataProvider | None = None,
        provider_factory: Callable[[Settings], AStockDataProvider] = AStockDataProvider,
    ) -> None:
        self.settings = settings
        self.storage = storage or DuckDBStorage(settings.db_path)
        self.provider = provider or provider_factory(settings)

    def load_stock_daily(self, symbols: list[str], *, start_date: str, end_date: str) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        cached = self.storage.get_stock_daily_range(clean_symbols, start_date=str(start_date), end_date=str(end_date), with_meta=True)
        if _has_daily_coverage(cached, clean_symbols, str(start_date), str(end_date), symbol_column="ts_code"):
            return _mark(cached, dataset="stock_daily", source="duckdb_cache", cache_hit=True)
        fetched = self.provider.load_historical_daily_klines(
            clean_symbols,
            start_date=str(start_date),
            end_date=str(end_date),
            storage=self.storage,
        )
        if not fetched.empty:
            fetched.attrs["data_source"] = str(fetched.attrs.get("data_source") or "astock_provider")
            self.storage.upsert_stock_daily(fetched)
            combined = self.storage.get_stock_daily_range(clean_symbols, start_date=str(start_date), end_date=str(end_date), with_meta=True)
            if not combined.empty:
                return _mark(combined, dataset="stock_daily", source="astock_provider_cached", cache_hit=False)
            return _mark(fetched, dataset="stock_daily", source=str(fetched.attrs.get("data_source") or "astock_provider"), cache_hit=False)
        return _mark(cached, dataset="stock_daily", source="duckdb_cache_incomplete", cache_hit=not cached.empty)

    def load_index_daily(self, index_codes: list[str], *, start_date: str, end_date: str) -> pd.DataFrame:
        clean_codes = normalize_symbols(index_codes, required=False)
        cached = self.storage.get_index_daily_range(clean_codes, start_date=str(start_date), end_date=str(end_date), with_meta=True)
        if _has_daily_coverage(cached, clean_codes, str(start_date), str(end_date), symbol_column="index_code"):
            return _mark(cached, dataset="index_daily", source="duckdb_cache", cache_hit=True)
        fetched = self.provider.load_index_daily(clean_codes, start_date=str(start_date), end_date=str(end_date))
        if not fetched.empty:
            fetched = _normalize_index_frame(fetched)
            fetched.attrs["data_source"] = str(fetched.attrs.get("data_source") or "astock_provider")
            for code in clean_codes:
                frame = fetched[fetched["index_code"].astype(str) == code] if "index_code" in fetched.columns else fetched
                if not frame.empty:
                    self.storage.upsert_industry_daily(code, frame)
            combined = self.storage.get_index_daily_range(clean_codes, start_date=str(start_date), end_date=str(end_date), with_meta=True)
            if not combined.empty:
                return _mark(combined, dataset="index_daily", source="astock_provider_cached", cache_hit=False)
            return _mark(fetched, dataset="index_daily", source=str(fetched.attrs.get("data_source") or "astock_provider"), cache_hit=False)
        return _mark(cached, dataset="index_daily", source="duckdb_cache_incomplete", cache_hit=not cached.empty)

    def load_stock_minute(
        self,
        symbols: list[str],
        *,
        period: str = "1m",
        start_time: str | None = None,
        end_time: str | None = None,
        count: int | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        cached = self.storage.get_stock_minute_cache(clean_symbols, period=period, start_time=start_time, end_time=end_time)
        if count is None and start_time and end_time and _has_symbol_rows(cached, clean_symbols, "ts_code"):
            return _mark(cached, dataset="stock_minute", source="duckdb_cache", cache_hit=True)
        if count is not None and _has_minute_count(cached, clean_symbols, count):
            return _mark(cached.groupby("ts_code", group_keys=False).tail(count), dataset="stock_minute", source="duckdb_cache", cache_hit=True)
        fetched = self.provider.load_historical_minute_klines(
            clean_symbols,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
        )
        if fetched.empty and not start_time and not end_time:
            fetched = self.provider.load_realtime_minute_klines(clean_symbols, period=period, count=count)
        if not fetched.empty:
            fetched.attrs["data_source"] = str(fetched.attrs.get("data_source") or "astock_provider")
            self.storage.upsert_stock_minute_cache(fetched, period=period)
            cached = self.storage.get_stock_minute_cache(clean_symbols, period=period, start_time=start_time, end_time=end_time)
            return _mark(cached if not cached.empty else fetched, dataset="stock_minute", source="astock_provider_cached", cache_hit=False)
        return _mark(cached, dataset="stock_minute", source="duckdb_cache_incomplete", cache_hit=not cached.empty)

    def load_realtime_quotes(self, symbols: list[str], *, for_trading: bool = False, ttl_seconds: int | None = None) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        ttl = int(ttl_seconds if ttl_seconds is not None else (TRADING_QUOTE_TTL_SECONDS if for_trading else ANALYSIS_QUOTE_TTL_SECONDS))
        cached = self.storage.get_realtime_quote_cache(clean_symbols)
        fresh = _fresh_quotes(cached, ttl)
        if _has_symbol_rows(fresh, clean_symbols, "ts_code"):
            return _mark(fresh, dataset="realtime_quote", source="duckdb_cache", cache_hit=True)
        fetched = self.provider.load_realtime_quotes(symbols=clean_symbols)
        if not fetched.empty:
            fetched = _normalize_quote_frame(fetched)
            fetched.attrs["data_source"] = str(fetched.attrs.get("data_source") or "astock_provider")
            self.storage.upsert_realtime_quote_cache(fetched)
            cached = self.storage.get_realtime_quote_cache(clean_symbols)
            return _mark(cached if not cached.empty else fetched, dataset="realtime_quote", source="astock_provider_cached", cache_hit=False)
        return _mark(fresh, dataset="realtime_quote", source="duckdb_cache_incomplete", cache_hit=not fresh.empty)

    def load_indicator_inputs(self, symbols: list[str], trade_date: str, *, lookback_days: int = 180) -> list[IndicatorInput]:
        clean_symbols = normalize_symbols(symbols, required=False)
        start = _date_days_before(str(trade_date), int(lookback_days))
        daily = self.load_stock_daily(clean_symbols, start_date=start, end_date=str(trade_date))
        stock_basic = self.storage.get_stock_basic()
        result: list[IndicatorInput] = []
        for symbol in clean_symbols:
            item_daily = daily[daily["ts_code"].astype(str) == symbol] if not daily.empty else pd.DataFrame()
            basic_row = stock_basic[stock_basic["ts_code"].astype(str) == symbol].head(1) if not stock_basic.empty else pd.DataFrame()
            result.append(
                IndicatorInput(
                    ts_code=symbol,
                    trade_date=str(trade_date),
                    daily=item_daily,
                    stock_basic=basic_row.iloc[0].to_dict() if not basic_row.empty else {},
                    data_sources={"daily": _frame_source(daily)},
                )
            )
        return result


def require_market_data_provenance(frame: pd.DataFrame, *, dataset: str = "market_data") -> None:
    if frame.empty:
        raise ValueError(f"{dataset} is empty")
    provenance = frame.attrs.get("market_data_provenance")
    if not provenance:
        raise ValueError(f"{dataset} missing SATS market data provenance")


def _mark(frame: pd.DataFrame, *, dataset: str, source: str, cache_hit: bool) -> pd.DataFrame:
    data = frame.copy()
    fetched_at = ""
    if "fetched_at" in data.columns and not data.empty:
        fetched_at = str(data["fetched_at"].dropna().astype(str).max() or "")
    data.attrs["market_data_provenance"] = [
        MarketDataProvenance(
            dataset=dataset,
            source=source,
            symbols=tuple(_symbols_from_frame(data)),
            rows=len(data),
            cache_hit=cache_hit,
            fetched_at=fetched_at,
        ).to_dict()
    ]
    data.attrs["data_source"] = source
    return data


def _has_daily_coverage(frame: pd.DataFrame, symbols: list[str], start_date: str, end_date: str, *, symbol_column: str) -> bool:
    if frame.empty or not symbols:
        return False
    expected_dates = _business_dates(start_date, end_date)
    if not expected_dates:
        return _has_symbol_rows(frame, symbols, symbol_column)
    for symbol in symbols:
        subset = frame[frame[symbol_column].astype(str) == symbol]
        dates = set(subset["trade_date"].astype(str))
        if not set(expected_dates).issubset(dates):
            return False
    return True


def _has_symbol_rows(frame: pd.DataFrame, symbols: list[str], symbol_column: str) -> bool:
    if frame.empty:
        return False
    available = set(frame[symbol_column].astype(str))
    return set(symbols).issubset(available)


def _has_minute_count(frame: pd.DataFrame, symbols: list[str], count: int) -> bool:
    if frame.empty or count <= 0:
        return False
    counts = frame.groupby("ts_code").size().to_dict()
    return all(int(counts.get(symbol, 0)) >= count for symbol in symbols)


def _fresh_quotes(frame: pd.DataFrame, ttl_seconds: int) -> pd.DataFrame:
    if frame.empty or "fetched_at" not in frame.columns:
        return pd.DataFrame(columns=frame.columns)
    now = datetime.now()
    data = frame.copy()
    data["_fetched_dt"] = pd.to_datetime(data["fetched_at"], errors="coerce").dt.tz_localize(None)
    fresh = data[data["_fetched_dt"].notna() & ((now - data["_fetched_dt"]).dt.total_seconds() <= max(0, ttl_seconds))]
    return fresh.drop(columns=["_fetched_dt"], errors="ignore")


def _normalize_index_frame(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    if "index_code" not in data.columns and "ts_code" in data.columns:
        data["index_code"] = data["ts_code"]
    return data


def _normalize_quote_frame(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    if "price" not in data.columns:
        for candidate in ("last_price", "latest", "latest_price", "current", "close"):
            if candidate in data.columns:
                data["price"] = data[candidate]
                break
    if "volume" not in data.columns and "vol" in data.columns:
        data["volume"] = data["vol"]
    return data


def _symbols_from_frame(frame: pd.DataFrame) -> list[str]:
    for column in ("ts_code", "index_code"):
        if column in frame.columns:
            return sorted(str(item) for item in frame[column].dropna().astype(str).unique())
    return []


def _frame_source(frame: pd.DataFrame) -> str:
    provenance = frame.attrs.get("market_data_provenance")
    if isinstance(provenance, list) and provenance:
        return str(provenance[0].get("source") or "")
    return str(frame.attrs.get("data_source") or "")


def _business_dates(start_date: str, end_date: str) -> list[str]:
    try:
        start = datetime.strptime(str(start_date), "%Y%m%d")
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        return []
    if start > end:
        start, end = end, start
    dates = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def _date_days_before(end_date: str, days: int) -> str:
    try:
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        end = datetime.now()
    return (end - timedelta(days=max(1, int(days)))).strftime("%Y%m%d")
