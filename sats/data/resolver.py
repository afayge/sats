from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.minute_periods import normalize_minute_period
from sats.indicators import IndicatorInput
from sats.market_clock import (
    current_shanghai_trade_date as _current_shanghai_trade_date,
    is_a_share_trading_time_now as _is_a_share_trading_time_now,
)
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols


MARKET_BREADTH_MIN_COUNT = 3000


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

    def load_stock_daily(
        self,
        symbols: list[str],
        *,
        start_date: str,
        end_date: str,
        refresh_current_day: bool = False,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        cached = self.storage.get_stock_daily_range(clean_symbols, start_date=str(start_date), end_date=str(end_date), with_meta=True)
        refresh_date = _refresh_date(str(end_date), refresh_current_day=refresh_current_day)
        if not refresh_date and _has_daily_coverage(cached, clean_symbols, str(start_date), str(end_date), symbol_column="ts_code"):
            return _mark(cached, dataset="stock_daily", source="duckdb_cache", cache_hit=True)
        if refresh_date:
            fetched_today = self.provider.load_historical_daily_klines(
                clean_symbols,
                start_date=refresh_date,
                end_date=refresh_date,
                storage=self.storage,
            )
            if not fetched_today.empty:
                fetched_today.attrs["data_source"] = str(fetched_today.attrs.get("data_source") or "astock_provider")
                self.storage.upsert_stock_daily(fetched_today)
                cached_after = self.storage.get_stock_daily_range(clean_symbols, start_date=str(start_date), end_date=str(end_date), with_meta=True)
                combined = _combine_trade_date_overlay(cached_after, fetched_today, refresh_date, symbol_column="ts_code")
                if _has_daily_coverage(combined, clean_symbols, str(start_date), str(end_date), symbol_column="ts_code"):
                    return _mark(combined, dataset="stock_daily", source="astock_provider_cached", cache_hit=False)
            elif _has_daily_coverage(cached, clean_symbols, str(start_date), str(end_date), symbol_column="ts_code"):
                return _mark(cached, dataset="stock_daily", source="duckdb_cache_stale_current_day", cache_hit=True)
        fetched = self.provider.load_historical_daily_klines(
            clean_symbols,
            start_date=str(start_date),
            end_date=str(end_date),
            storage=self.storage,
        )
        if not fetched.empty:
            fetched.attrs["data_source"] = str(fetched.attrs.get("data_source") or "astock_provider")
            self.storage.upsert_stock_daily(fetched)
            cached_after = self.storage.get_stock_daily_range(clean_symbols, start_date=str(start_date), end_date=str(end_date), with_meta=True)
            combined = _combine_current_trading_rows(cached_after, fetched, symbol_column="ts_code")
            if not combined.empty:
                return _mark(combined, dataset="stock_daily", source="astock_provider_cached", cache_hit=False)
            return _mark(fetched, dataset="stock_daily", source=str(fetched.attrs.get("data_source") or "astock_provider"), cache_hit=False)
        return _mark(cached, dataset="stock_daily", source="duckdb_cache_incomplete", cache_hit=not cached.empty)

    def load_index_daily(
        self,
        index_codes: list[str],
        *,
        start_date: str,
        end_date: str,
        refresh_current_day: bool = False,
    ) -> pd.DataFrame:
        clean_codes = normalize_symbols(index_codes, required=False)
        cached = self.storage.get_index_daily_range(clean_codes, start_date=str(start_date), end_date=str(end_date), with_meta=True)
        refresh_date = _refresh_date(str(end_date), refresh_current_day=refresh_current_day)
        if not refresh_date and _has_daily_coverage(cached, clean_codes, str(start_date), str(end_date), symbol_column="index_code"):
            return _mark(cached, dataset="index_daily", source="duckdb_cache", cache_hit=True)
        if refresh_date:
            fetched_today = self.provider.load_index_daily(clean_codes, start_date=refresh_date, end_date=refresh_date)
            if not fetched_today.empty:
                fetched_today = _normalize_index_frame(fetched_today)
                fetched_today.attrs["data_source"] = str(fetched_today.attrs.get("data_source") or "astock_provider")
                for code in clean_codes:
                    frame = fetched_today[fetched_today["index_code"].astype(str) == code] if "index_code" in fetched_today.columns else fetched_today
                    if not frame.empty:
                        self.storage.upsert_industry_daily(code, frame)
                cached_after = self.storage.get_index_daily_range(clean_codes, start_date=str(start_date), end_date=str(end_date), with_meta=True)
                combined = _combine_trade_date_overlay(cached_after, fetched_today, refresh_date, symbol_column="index_code")
                if _has_daily_coverage(combined, clean_codes, str(start_date), str(end_date), symbol_column="index_code"):
                    return _mark(combined, dataset="index_daily", source="astock_provider_cached", cache_hit=False)
            elif _has_daily_coverage(cached, clean_codes, str(start_date), str(end_date), symbol_column="index_code"):
                return _mark(cached, dataset="index_daily", source="duckdb_cache_stale_current_day", cache_hit=True)
        fetched = self.provider.load_index_daily(clean_codes, start_date=str(start_date), end_date=str(end_date))
        if not fetched.empty:
            fetched = _normalize_index_frame(fetched)
            fetched.attrs["data_source"] = str(fetched.attrs.get("data_source") or "astock_provider")
            for code in clean_codes:
                frame = fetched[fetched["index_code"].astype(str) == code] if "index_code" in fetched.columns else fetched
                if not frame.empty:
                    self.storage.upsert_industry_daily(code, frame)
            cached_after = self.storage.get_index_daily_range(clean_codes, start_date=str(start_date), end_date=str(end_date), with_meta=True)
            combined = _combine_current_trading_rows(cached_after, fetched, symbol_column="index_code")
            if not combined.empty:
                return _mark(combined, dataset="index_daily", source="astock_provider_cached", cache_hit=False)
            return _mark(fetched, dataset="index_daily", source=str(fetched.attrs.get("data_source") or "astock_provider"), cache_hit=False)
        return _mark(cached, dataset="index_daily", source="duckdb_cache_incomplete", cache_hit=not cached.empty)

    def load_index_quotes(self, index_codes: list[str], *, index_daily: pd.DataFrame | None = None) -> pd.DataFrame:
        clean_codes = normalize_symbols(index_codes, required=False)
        frames: list[pd.DataFrame] = []
        sources: list[str] = []
        try:
            fetched = self.provider.load_realtime_quotes(symbols=clean_codes)
        except Exception:
            fetched = pd.DataFrame()
        if fetched is not None and not fetched.empty:
            fetched = _normalize_quote_frame(fetched)
            if "ts_code" not in fetched.columns and "index_code" in fetched.columns:
                fetched["ts_code"] = fetched["index_code"]
            if "ts_code" in fetched.columns:
                fetched = fetched[fetched["ts_code"].astype(str).isin(clean_codes)].copy()
            if not fetched.empty and "ts_code" in fetched.columns:
                fetched = fetched.sort_values("ts_code").drop_duplicates(subset=["ts_code"], keep="last")
                source = str(fetched.attrs.get("data_source") or "astock_provider")
                fetched.attrs["data_source"] = source
                frames.append(fetched)
                sources.append(source)
        present = set(_symbols_from_frame(frames[0])) if frames else set()
        missing = [code for code in clean_codes if code not in present]
        fallback = _index_daily_latest_quote_frame(index_daily, missing)
        if not fallback.empty:
            frames.append(fallback)
            sources.append("index_daily_latest_quote_fallback")
        source = "+".join(dict.fromkeys(item for item in sources if item)) or "unavailable"
        result = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
        return _mark(result, dataset="index_quote", source=source, cache_hit=False)

    def load_market_breadth(
        self,
        *,
        as_of_date: str,
        min_count: int = MARKET_BREADTH_MIN_COUNT,
    ) -> tuple[dict[str, Any], str]:
        try:
            live_payload, live_source = self.provider.load_market_breadth()
        except Exception:
            live_payload, live_source = {}, "unavailable"
        if live_payload and int(live_payload.get("total_count") or live_payload.get("total") or 0) > 0:
            payload = dict(live_payload)
            if not str(payload.get("trade_date") or "").strip():
                payload["trade_date"] = str(as_of_date)
            source = str(live_source or "realtime_quote")
            payload["data_source"] = source
            payload["is_fallback"] = False
            _attach_turnover_context(
                payload,
                storage=self.storage,
                as_of_date=str(as_of_date),
                min_count=max(1, int(min_count)),
                source=source,
                full_day_snapshot=False,
            )
            return payload, source

        snapshot = self.storage.get_latest_stock_daily_snapshot(
            end_date=str(as_of_date),
            min_count=max(1, int(min_count)),
        )
        if snapshot.empty:
            return {}, "unavailable"
        trade_date = str(snapshot["trade_date"].astype(str).max())
        source = "duckdb_stock_daily_snapshot"
        payload = _breadth_metrics(snapshot)
        payload.update(
            {
                "trade_date": trade_date,
                "data_source": source,
                "is_fallback": True,
                "stale_calendar_days": _calendar_day_gap(trade_date, str(as_of_date)),
            }
        )
        _attach_turnover_context(
            payload,
            storage=self.storage,
            as_of_date=trade_date,
            min_count=max(1, int(min_count)),
            source=source,
            full_day_snapshot=True,
        )
        return payload, source

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
        period = normalize_minute_period(period)
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
            fetched_for_return = _minute_cache_shape(fetched, period=period)
            combined = _combine_minute_overlay(cached, fetched_for_return)
            return _mark(combined if not combined.empty else fetched_for_return, dataset="stock_minute", source="astock_provider_cached", cache_hit=False)
        return _mark(cached, dataset="stock_minute", source="duckdb_cache_incomplete", cache_hit=not cached.empty)

    def load_realtime_quotes(self, symbols: list[str], *, for_trading: bool = False, ttl_seconds: int | None = None) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        fetched = self.provider.load_realtime_quotes(symbols=clean_symbols)
        if not fetched.empty:
            fetched = _normalize_quote_frame(fetched)
            source = str(fetched.attrs.get("data_source") or "astock_provider")
            fetched.attrs["data_source"] = source
            self.storage.upsert_realtime_quote_cache(fetched)
            return _mark(fetched, dataset="realtime_quote", source=source, cache_hit=False)
        return _mark(fetched, dataset="realtime_quote", source="unavailable", cache_hit=False)

    def load_indicator_inputs(self, symbols: list[str], trade_date: str, *, lookback_days: int = 180) -> list[IndicatorInput]:
        clean_symbols = normalize_symbols(symbols, required=False)
        start = _date_days_before(str(trade_date), int(lookback_days))
        daily = self.load_stock_daily(clean_symbols, start_date=start, end_date=str(trade_date), refresh_current_day=True)
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


def should_refresh_current_day(trade_date: str, *, refresh_current_day: bool = True) -> bool:
    if not refresh_current_day:
        return False
    return str(trade_date) == current_shanghai_trade_date() and is_a_share_trading_time_now()


def current_shanghai_trade_date() -> str:
    return _current_shanghai_trade_date()


def is_a_share_trading_time_now() -> bool:
    return _is_a_share_trading_time_now()


def _refresh_date(end_date: str, *, refresh_current_day: bool) -> str:
    return str(end_date) if should_refresh_current_day(str(end_date), refresh_current_day=refresh_current_day) else ""


def _combine_current_trading_rows(cached: pd.DataFrame, fetched: pd.DataFrame, *, symbol_column: str) -> pd.DataFrame:
    if fetched.empty:
        return cached
    today = current_shanghai_trade_date()
    if not is_a_share_trading_time_now():
        return cached if not cached.empty else fetched
    return _combine_trade_date_overlay(cached, fetched, today, symbol_column=symbol_column)


def _combine_trade_date_overlay(
    cached: pd.DataFrame,
    overlay: pd.DataFrame,
    trade_date: str,
    *,
    symbol_column: str,
) -> pd.DataFrame:
    if overlay.empty or "trade_date" not in overlay.columns:
        return cached
    overlay_rows = overlay[overlay["trade_date"].astype(str) == str(trade_date)].copy()
    if overlay_rows.empty:
        return cached if not cached.empty else overlay
    base = cached.copy()
    if not base.empty and "trade_date" in base.columns and symbol_column in base.columns and symbol_column in overlay_rows.columns:
        overlay_symbols = set(overlay_rows[symbol_column].dropna().astype(str))
        keep = ~(
            (base["trade_date"].astype(str) == str(trade_date))
            & (base[symbol_column].astype(str).isin(overlay_symbols))
        )
        base = base[keep]
    combined = pd.concat([base, overlay_rows], ignore_index=True, sort=False)
    if symbol_column in combined.columns and "trade_date" in combined.columns:
        combined = combined.sort_values([symbol_column, "trade_date"])
    return combined.reset_index(drop=True)


def _combine_minute_overlay(cached: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    if cached.empty:
        return fetched
    if fetched.empty:
        return cached
    columns = [column for column in ("ts_code", "period", "datetime") if column in cached.columns and column in fetched.columns]
    combined = pd.concat([cached, fetched], ignore_index=True, sort=False)
    if columns:
        combined = combined.drop_duplicates(subset=columns, keep="last")
    sort_columns = [column for column in ("ts_code", "datetime") if column in combined.columns]
    if sort_columns:
        combined = combined.sort_values(sort_columns)
    return combined.reset_index(drop=True)


def _minute_cache_shape(frame: pd.DataFrame, *, period: str) -> pd.DataFrame:
    data = frame.copy()
    if data.empty:
        return data
    if "datetime" not in data.columns:
        for candidate in ("trade_time", "time", "bar_time"):
            if candidate in data.columns:
                data["datetime"] = data[candidate]
                break
    if "trade_date" not in data.columns and "datetime" in data.columns:
        data["trade_date"] = data["datetime"].astype(str).str.replace("-", "", regex=False).str[:8]
    if "period" not in data.columns:
        data["period"] = str(period or "1m")
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


def _index_daily_latest_quote_frame(index_daily: pd.DataFrame | None, symbols: list[str]) -> pd.DataFrame:
    if index_daily is None or index_daily.empty or not symbols:
        return pd.DataFrame()
    data = _normalize_index_frame(index_daily)
    if "index_code" not in data.columns or "trade_date" not in data.columns:
        return pd.DataFrame()
    data = data[data["index_code"].astype(str).isin(symbols)].copy()
    if data.empty:
        return pd.DataFrame()
    data = data.sort_values(["index_code", "trade_date"]).drop_duplicates(subset=["index_code"], keep="last")
    data["ts_code"] = data["index_code"].astype(str)
    if "price" not in data.columns and "close" in data.columns:
        data["price"] = data["close"]
    if "volume" not in data.columns and "vol" in data.columns:
        data["volume"] = data["vol"]
    data["data_source"] = "index_daily_latest_quote_fallback"
    columns = [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "price",
        "vol",
        "volume",
        "amount",
        "pct_chg",
        "data_source",
    ]
    for column in columns:
        if column not in data.columns:
            data[column] = None
    result = data[columns].sort_values("ts_code").reset_index(drop=True)
    result.attrs["data_source"] = "index_daily_latest_quote_fallback"
    return result


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


def _attach_turnover_context(
    payload: dict[str, Any],
    *,
    storage: DuckDBStorage,
    as_of_date: str,
    min_count: int,
    source: str,
    full_day_snapshot: bool,
) -> None:
    current_date = str(payload.get("trade_date") or as_of_date or "")
    total_amount = _safe_float(payload.get("total_amount"))
    progress = 1.0 if full_day_snapshot else _market_session_progress(
        payload.get("latest_trade_time") or payload.get("trade_time"),
    )
    if progress is not None and progress >= 1.0:
        full_day_snapshot = True
        progress = 1.0
    amount_basis = "full_day_snapshot" if full_day_snapshot else "intraday_cumulative"
    payload["amount_basis"] = amount_basis
    if progress is not None:
        payload["session_progress"] = round(float(progress), 4)
    if total_amount is None:
        payload["turnover_comparison"] = {
            "status": "unavailable",
            "reason": "total_amount_unavailable",
            "basis": _turnover_comparison_basis(amount_basis),
        }
        return
    if _amount_unit_unverified(source):
        payload["turnover_comparison"] = {
            "status": "unavailable",
            "reason": "amount_unit_unverified",
            "basis": _turnover_comparison_basis(amount_basis),
        }
        return
    projected_amount = _projected_full_day_amount(total_amount, progress)
    if projected_amount is not None:
        payload["projected_full_day_amount"] = projected_amount
    previous_date, previous_amount = _previous_full_day_amount(
        storage,
        before_date=current_date or as_of_date,
        min_count=min_count,
    )
    if previous_date:
        payload["previous_trade_date"] = previous_date
    if previous_amount is not None:
        payload["previous_full_day_amount"] = previous_amount
    if projected_amount is None:
        payload["turnover_comparison"] = {
            "status": "unavailable",
            "reason": "session_progress_unavailable",
            "basis": _turnover_comparison_basis(amount_basis),
        }
        return
    if previous_amount in (None, 0):
        payload["turnover_comparison"] = {
            "status": "unavailable",
            "reason": "previous_full_day_amount_unavailable",
            "basis": _turnover_comparison_basis(amount_basis),
        }
        return
    pct_change = (projected_amount / previous_amount - 1.0) * 100.0
    payload["turnover_comparison"] = {
        "status": "ok",
        "basis": _turnover_comparison_basis(amount_basis),
        "pct_change": _safe_float(pct_change),
        "current_amount": total_amount,
        "projected_full_day_amount": projected_amount,
        "previous_trade_date": previous_date,
        "previous_full_day_amount": previous_amount,
        "direction": _turnover_direction(pct_change),
    }


def _market_session_progress(trade_time: Any | None) -> float | None:
    parsed = _parse_market_datetime(trade_time)
    if parsed is None or (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0):
        parsed = datetime.now()
    minutes = parsed.hour * 60 + parsed.minute + parsed.second / 60.0
    morning_start = 9 * 60 + 30
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60
    afternoon_end = 15 * 60
    if minutes <= morning_start:
        elapsed = 0.0
    elif minutes <= morning_end:
        elapsed = minutes - morning_start
    elif minutes <= afternoon_start:
        elapsed = 120.0
    elif minutes <= afternoon_end:
        elapsed = 120.0 + minutes - afternoon_start
    else:
        elapsed = 240.0
    return max(0.0, min(1.0, elapsed / 240.0))


def _parse_market_datetime(value: Any | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = pd.to_datetime(text, errors="coerce")
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    if isinstance(parsed, pd.Timestamp):
        if parsed.tzinfo is not None:
            parsed = parsed.tz_convert(None)
        return parsed.to_pydatetime()
    return None


def _projected_full_day_amount(total_amount: float, progress: float | None) -> float | None:
    if progress is None or progress <= 0:
        return None
    return _safe_float(total_amount / progress)


def _previous_full_day_amount(
    storage: DuckDBStorage,
    *,
    before_date: str,
    min_count: int,
) -> tuple[str, float | None]:
    previous_end = _date_days_before(str(before_date), 1)
    snapshot = storage.get_latest_stock_daily_snapshot(
        end_date=previous_end,
        min_count=max(1, int(min_count)),
    )
    if snapshot.empty:
        return "", None
    trade_date = str(snapshot["trade_date"].astype(str).max() or "")
    amount = pd.to_numeric(snapshot.get("amount"), errors="coerce") if "amount" in snapshot.columns else pd.Series(dtype=float)
    return trade_date, _safe_float(amount.sum())


def _turnover_comparison_basis(amount_basis: str) -> str:
    if amount_basis == "intraday_cumulative":
        return "projected_full_day_vs_previous_full_day"
    return "full_day_vs_previous_full_day"


def _turnover_direction(pct_change: float) -> str:
    if pct_change >= 5.0:
        return "higher"
    if pct_change <= -5.0:
        return "lower"
    return "flat"


def _amount_unit_unverified(source: str) -> bool:
    return "akshare" in str(source or "").lower()


def _calendar_day_gap(start_date: str, end_date: str) -> int:
    try:
        start = datetime.strptime(str(start_date), "%Y%m%d")
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        return 0
    return max(0, (end - start).days)


def _safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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
