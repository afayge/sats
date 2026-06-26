from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from sats.config import Settings
from sats.data.akshare_provider import AkShareDataProvider
from sats.data.base import MarketDataProvider
from sats.data.limit_sentiment import build_limit_sentiment_payload, build_quote_limit_sentiment_payload
from sats.minute_periods import (
    aggregate_minute_klines,
    ensure_minute_frame_period,
    looks_like_minute_period,
    native_minute_base_period,
    native_minute_count_for,
    normalize_minute_period,
    tail_minute_klines,
)
from sats.data.tickflow_provider import DEFAULT_A_SHARE_UNIVERSE_ID, TickFlowDataProvider
from sats.data.provider_capabilities import list_provider_capabilities
from sats.data.tushare_provider import SCREENING_TRADE_DAYS, TushareDataProvider
from sats.data.tushare_stock_datasets import (
    get_tushare_dataset,
    get_tushare_stock_dataset,
    list_tushare_datasets,
    list_tushare_stock_datasets,
)
from sats.indicators import IndicatorInput
from sats.screening.base import ScreeningInput
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols, normalize_ts_code


class AStockDataProvider(MarketDataProvider):
    """Unified A-share data facade.

    Business modules should depend on this provider. TickFlow, Tushare and
    AkShare remain backend adapters hidden behind this facade.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        tickflow_provider: Any | None = None,
        tushare_provider: Any | None = None,
        akshare_provider: Any | None = None,
    ) -> None:
        self.settings = settings
        self._tickflow_provider = tickflow_provider
        self._tushare_provider = tushare_provider
        self._akshare_provider = akshare_provider
        self._tickflow_failed = False
        self._tushare_failed = False
        self._akshare_failed = False

    @property
    def tickflow(self) -> Any | None:
        if self._tickflow_failed:
            return None
        if self._tickflow_provider is None:
            try:
                self._tickflow_provider = TickFlowDataProvider(self.settings)
            except Exception:
                self._tickflow_failed = True
                return None
        return self._tickflow_provider

    @property
    def tushare(self) -> Any | None:
        if self._tushare_failed:
            return None
        if self._tushare_provider is None:
            try:
                self._tushare_provider = TushareDataProvider(self.settings)
            except Exception:
                self._tushare_failed = True
                return None
        return self._tushare_provider

    @property
    def akshare(self) -> Any | None:
        if self._akshare_failed:
            return None
        if self._akshare_provider is None:
            try:
                self._akshare_provider = AkShareDataProvider()
            except Exception:
                self._akshare_failed = True
                return None
        return self._akshare_provider

    def list_a_share_symbols(self) -> list[str]:
        tick = self.tickflow
        if tick is not None and hasattr(tick, "load_universe_symbols"):
            try:
                symbols = tick.load_universe_symbols(DEFAULT_A_SHARE_UNIVERSE_ID)
            except Exception:
                symbols = []
            if symbols:
                return sorted(normalize_symbols(symbols, required=False))
        tushare = self.tushare
        if tushare is not None:
            try:
                return tushare.list_a_share_symbols()
            except Exception:
                return []
        return []

    def list_universes(self) -> pd.DataFrame:
        tick = self.tickflow
        if tick is None or not hasattr(tick, "list_universes"):
            return pd.DataFrame()
        frame = _safe_frame(tick.list_universes)
        if not frame.empty:
            frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_universes")
        return frame

    def load_universe_symbols(self, universe_id: str = DEFAULT_A_SHARE_UNIVERSE_ID) -> list[str]:
        tick = self.tickflow
        if tick is None or not hasattr(tick, "load_universe_symbols"):
            return self.list_a_share_symbols() if universe_id == DEFAULT_A_SHARE_UNIVERSE_ID else []
        values = _safe_list(lambda: tick.load_universe_symbols(universe_id))
        return sorted(normalize_symbols(values, required=False))

    def load_instruments(self, symbols: list[str]) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        tick = self.tickflow
        if tick is None or not hasattr(tick, "load_instruments") or not clean_symbols:
            return pd.DataFrame()
        frame = _safe_frame(lambda: tick.load_instruments(clean_symbols))
        if not frame.empty:
            frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_instruments")
        return frame

    def load_klines(
        self,
        symbols: list[str],
        *,
        period: str,
        start_time: int | str | None = None,
        end_time: int | str | None = None,
        count: int | None = None,
        adjust: str = "none",
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        if looks_like_minute_period(period):
            return self.load_historical_minute_klines(
                clean_symbols,
                period=normalize_minute_period(period),
                start_time=start_time,
                end_time=end_time,
                count=count,
            )
        tick = self.tickflow
        if tick is not None and hasattr(tick, "load_klines") and clean_symbols:
            frame = _safe_frame(
                lambda: tick.load_klines(
                    clean_symbols,
                    period=period,
                    start_time=start_time,
                    end_time=end_time,
                    count=count,
                    adjust=adjust,
                )
            )
            if not frame.empty:
                frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_klines")
                return frame
        normalized_period = str(period or "").strip().lower()
        if normalized_period in {"1d", "d", "day", "daily"}:
            return self.load_historical_daily_klines(
                clean_symbols,
                start_date=start_time,
                end_date=end_time,
            )
        if looks_like_minute_period(period):
            return self.load_historical_minute_klines(
                clean_symbols,
                period=normalize_minute_period(period),
                start_time=start_time,
                end_time=end_time,
                count=count,
            )
        return pd.DataFrame()

    def load_realtime_daily_basic_like(self, symbols: list[str], *, trade_date: str) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        tick = self.tickflow
        if tick is None or not hasattr(tick, "load_realtime_daily_basic_like") or not clean_symbols:
            return pd.DataFrame()
        frame = _safe_frame(
            lambda: tick.load_realtime_daily_basic_like(
                clean_symbols,
                trade_date=trade_date,
            )
        )
        if not frame.empty:
            frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_realtime_daily_basic_like")
        return frame

    def load_intraday_timeshare(
        self,
        symbols: list[str],
        *,
        period: str = "1m",
        count: int | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        target_period = normalize_minute_period(period)
        base_period = native_minute_base_period(target_period)
        request_count = native_minute_count_for(count, target_period=target_period, base_period=base_period)
        tick = self.tickflow
        if tick is not None and hasattr(tick, "load_intraday_timeshare") and clean_symbols:
            frame = _safe_frame(lambda: tick.load_intraday_timeshare(clean_symbols, period=base_period, count=request_count))
            if not frame.empty:
                frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_intraday_timeshare")
                return _finalize_minute_period_frame(frame, target_period=target_period, base_period=base_period, count=count)
        return self.load_realtime_minute_klines(clean_symbols, period=target_period, count=count)

    def load_market_depth(self, symbols: list[str]) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        tick = self.tickflow
        if tick is None or not hasattr(tick, "load_market_depth") or not clean_symbols:
            return pd.DataFrame()
        frame = _safe_frame(lambda: tick.load_market_depth(clean_symbols))
        if not frame.empty:
            frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_market_depth")
        return frame

    def load_ex_factors(
        self,
        symbols: list[str],
        *,
        start_time: int | str | None = None,
        end_time: int | str | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        tick = self.tickflow
        if tick is None or not hasattr(tick, "load_ex_factors") or not clean_symbols:
            return pd.DataFrame()
        frame = _safe_frame(
            lambda: tick.load_ex_factors(
                clean_symbols,
                start_time=start_time,
                end_time=end_time,
            )
        )
        if not frame.empty:
            frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_ex_factors")
        return frame

    def load_provider_capabilities(
        self,
        *,
        provider: str | None = None,
        category: str | None = None,
        realtime: bool | None = None,
        compact: bool = False,
    ) -> list[dict[str, Any]]:
        return list_provider_capabilities(
            provider=provider,
            category=category,
            realtime=realtime,
            compact=compact,
        )

    def list_data_operations(
        self,
        *,
        provider: str | None = None,
        query: str | None = None,
        category: str | None = None,
        realtime: bool | None = None,
        writes_db: bool | None = None,
        limit: int = 50,
        offset: int = 0,
        compact: bool = True,
    ) -> dict[str, Any]:
        from sats.data.astock_operations import list_astock_capabilities

        return list_astock_capabilities(
            provider=provider,
            query=query,
            category=category,
            realtime=realtime,
            writes_db=writes_db,
            limit=limit,
            offset=offset,
            compact=compact,
        )

    def fetch_data_operation(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | tuple[str, ...] | None = None,
        limit: int = 200,
        storage: DuckDBStorage | None = None,
    ) -> dict[str, Any]:
        from sats.data.astock_operations import execute_astock_operation

        return execute_astock_operation(
            operation,
            params or {},
            fields=fields or (),
            limit=limit,
            provider=self,
            storage=storage,
        )

    def list_akshare_datasets(
        self,
        *,
        domain: str | None = None,
        category: str | None = None,
        tags: list[str] | tuple[str, ...] | str | None = None,
        query: str | None = None,
        realtime: bool | None = None,
        compact: bool = False,
    ) -> list[dict[str, Any]]:
        ak = self.akshare
        if ak is not None and hasattr(ak, "list_akshare_datasets"):
            return ak.list_akshare_datasets(
                domain=domain,
                category=category,
                tags=tags,
                query=query,
                realtime=realtime,
                compact=compact,
            )
        return []

    def describe_akshare_dataset(self, dataset: str) -> dict[str, Any]:
        ak = self.akshare
        if ak is not None and hasattr(ak, "describe_akshare_dataset"):
            return ak.describe_akshare_dataset(dataset)
        from sats.data.akshare_datasets import get_akshare_dataset

        return get_akshare_dataset(dataset).to_dict(compact=False)

    def fetch_akshare_dataset(
        self,
        dataset: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        spec = self.describe_akshare_dataset(dataset)
        ak = self.akshare
        if ak is None or not hasattr(ak, "fetch_akshare_dataset"):
            return {
                "dataset": spec.get("dataset", dataset),
                "function_name": spec.get("function_name", dataset),
                "title": spec.get("title", dataset),
                "domain": spec.get("domain", "AkShare"),
                "category": spec.get("category", ""),
                "tags": list(spec.get("tags") or []),
                "params": dict(params or {}),
                "doc_url": spec.get("doc_url", ""),
                "realtime": bool(spec.get("realtime", False)),
                "columns": [],
                "rows": [],
                "head": [],
                "tail": [],
                "latest": {},
                "row_count": 0,
                "returned_row_count": 0,
                "data_source": "unavailable",
                "missing_fields": ["akshare:unavailable"],
                "market_data_provenance": [],
            }
        return ak.fetch_akshare_dataset(dataset, params or {}, fields=fields, limit=limit)

    def load_stock_basic(self, *, storage: DuckDBStorage | None = None) -> pd.DataFrame:
        tick = self.tickflow
        if tick is not None and hasattr(tick, "load_stock_basic"):
            frame = _safe_frame(lambda: tick.load_stock_basic(storage=storage))
            if not frame.empty:
                frame.attrs["data_source"] = "tickflow_stock_basic"
                return frame
        tushare = self.tushare
        if tushare is not None:
            if hasattr(tushare, "load_stock_basic"):
                frame = _safe_frame(lambda: tushare.load_stock_basic(storage=storage))
            elif hasattr(tushare, "_stock_basic_frame"):
                frame = _safe_frame(lambda: tushare._stock_basic_frame(storage=storage))
            else:
                frame = pd.DataFrame()
            if not frame.empty:
                frame.attrs["data_source"] = "tushare_stock_basic"
                if storage is not None:
                    storage.upsert_stock_basic(frame)
                return frame
        if storage is not None:
            frame = _safe_frame(lambda: storage.get_stock_basic())
            if not frame.empty:
                frame.attrs["data_source"] = "duckdb_stock_basic"
                return frame
        return pd.DataFrame()

    def load_all_screening_inputs(
        self,
        trade_date: str,
        *,
        storage: DuckDBStorage,
        trade_days: int = SCREENING_TRADE_DAYS,
        rule_name: str | None = None,
    ) -> list[ScreeningInput]:
        provider = self.tushare
        if provider is None:
            return []
        return provider.load_all_screening_inputs(
            trade_date,
            storage=storage,
            trade_days=trade_days,
            rule_name=rule_name,
        )

    def load_screening_input(self, ts_code: str, trade_date: str) -> ScreeningInput:
        provider = self.tushare
        if provider is None:
            return ScreeningInput(
                ts_code=normalize_ts_code(ts_code),
                trade_date=trade_date,
                daily=pd.DataFrame(),
                daily_basic=pd.DataFrame(),
                metadata={"data_source": "unavailable"},
            )
        return provider.load_screening_input(ts_code, trade_date)

    def load_screening_inputs(
        self,
        symbols: list[str],
        trade_date: str,
        *,
        storage: DuckDBStorage,
        trade_days: int = SCREENING_TRADE_DAYS,
        rule_name: str | None = None,
    ) -> list[ScreeningInput]:
        provider = self.tushare
        if provider is None:
            return []
        return provider.load_screening_inputs(
            symbols,
            trade_date,
            storage=storage,
            trade_days=trade_days,
            rule_name=rule_name,
        )

    def load_indicator_inputs(
        self,
        symbols: list[str],
        trade_date: str,
        *,
        lookback_days: int = 180,
        storage: DuckDBStorage | None = None,
    ) -> list[IndicatorInput]:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return []
        storage = storage or DuckDBStorage(self.settings.db_path)
        tick_items = _safe_list(
            lambda: self.tickflow.load_indicator_inputs(
                clean_symbols,
                trade_date,
                lookback_days=lookback_days,
                storage=storage,
            )
        ) if self.tickflow is not None else []
        tushare_items = _safe_list(
            lambda: self.tushare.load_indicator_inputs(
                clean_symbols,
                trade_date,
                lookback_days=lookback_days,
                storage=storage,
            )
        ) if self.tushare is not None else []
        tick_lookup = _indicator_lookup(tick_items)
        tushare_lookup = _indicator_lookup(tushare_items)
        result: list[IndicatorInput] = []
        for ts_code in clean_symbols:
            tick = tick_lookup.get(ts_code)
            tushare = tushare_lookup.get(ts_code)
            if tick is None and tushare is None:
                result.append(_cache_indicator_input(ts_code, trade_date, storage=storage))
            else:
                result.append(_merge_indicator_input(ts_code, trade_date, tick, tushare))
        return result

    def load_historical_daily_klines(
        self,
        symbols: list[str],
        *,
        start_date: int | str | None = None,
        end_date: int | str | None = None,
        storage: DuckDBStorage | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return pd.DataFrame()
        tick = self.tickflow
        if tick is not None:
            frame = _safe_frame(
                lambda: tick.load_historical_daily_klines(
                    clean_symbols,
                    start_date=start_date,
                    end_date=end_date,
                    storage=storage,
                )
            )
            if not frame.empty:
                frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_daily")
                return frame
        tushare = self.tushare
        if tushare is not None and hasattr(tushare, "load_indicator_inputs") and end_date is not None:
            inputs = _safe_list(
                lambda: tushare.load_indicator_inputs(
                    clean_symbols,
                    str(end_date),
                    lookback_days=_historical_daily_lookback_days(start_date, end_date),
                    storage=storage,
                )
            )
            frame = _combine_frames([item.daily for item in inputs])
            if start_date is not None and "trade_date" in frame.columns:
                frame = frame[frame["trade_date"].astype(str) >= str(start_date)]
            if not frame.empty:
                frame.attrs["data_source"] = "tushare_daily"
                if storage is not None:
                    storage.upsert_stock_daily(frame)
                return frame
        return pd.DataFrame()

    def load_realtime_quotes(
        self,
        *,
        symbols: list[str] | None = None,
        universe_id: str | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False) if symbols is not None else None
        tick = self.tickflow
        if tick is None:
            return pd.DataFrame()
        frame = (
            tick.load_realtime_quotes(symbols=clean_symbols)
            if clean_symbols is not None
            else tick.load_realtime_quotes(universe_id=universe_id)
        )
        if frame.empty:
            return frame
        frame = _with_cached_previous_close(
            frame,
            storage=DuckDBStorage(self.settings.db_path),
        )
        frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_current_1d_quote")
        return frame

    def load_realtime_daily_quotes(self, symbols: list[str], *, trade_date: str) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        tick = self.tickflow
        if tick is None or not hasattr(tick, "load_realtime_daily_quotes"):
            return pd.DataFrame()
        frame = tick.load_realtime_daily_quotes(clean_symbols, trade_date=trade_date)
        if not frame.empty:
            frame.attrs["data_source"] = str(frame.attrs.get("data_source") or "tickflow_current_1d")
        return frame

    def load_current_klines(
        self,
        symbols: list[str],
        *,
        period: str,
        trade_date: str,
        count: int | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return pd.DataFrame()
        if period == "1d":
            target_period = "1d"
            base_period = "1d"
            request_count = count
        else:
            target_period = normalize_minute_period(period)
            base_period = native_minute_base_period(target_period)
            request_count = native_minute_count_for(count, target_period=target_period, base_period=base_period)
        tick = self.tickflow
        if tick is None or not hasattr(tick, "load_current_klines"):
            return pd.DataFrame()
        frame = tick.load_current_klines(
            clean_symbols,
            period=base_period,
            trade_date=trade_date,
            count=request_count,
        )
        if target_period == "1d":
            return frame
        return _finalize_minute_period_frame(frame, target_period=target_period, base_period=base_period, count=count)

    def load_realtime_minute_klines(
        self,
        symbols: list[str],
        *,
        period: str = "1m",
        count: int | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return pd.DataFrame()
        target_period = normalize_minute_period(period)
        base_period = native_minute_base_period(target_period)
        request_count = native_minute_count_for(count, target_period=target_period, base_period=base_period)
        tick = self.tickflow
        if tick is None:
            return pd.DataFrame()
        frame = tick.load_realtime_minute_klines(clean_symbols, period=base_period, count=request_count)
        return _finalize_minute_period_frame(frame, target_period=target_period, base_period=base_period, count=count)

    def load_historical_minute_klines(
        self,
        symbols: list[str],
        *,
        period: str = "1m",
        start_time: int | str | None = None,
        end_time: int | str | None = None,
        count: int | None = None,
    ) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return pd.DataFrame()
        target_period = normalize_minute_period(period)
        base_period = native_minute_base_period(target_period)
        request_count = native_minute_count_for(count, target_period=target_period, base_period=base_period)
        tick = self.tickflow
        if tick is None:
            return pd.DataFrame()
        frame = tick.load_historical_minute_klines(
            clean_symbols,
            period=base_period,
            start_time=start_time,
            end_time=end_time,
            count=request_count,
        )
        return _finalize_minute_period_frame(frame, target_period=target_period, base_period=base_period, count=count)

    def load_index_daily(self, index_codes: list[str], *, start_date: str, end_date: str) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        sources: list[str] = []
        missing = normalize_symbols(index_codes, required=False)
        tick = self.tickflow
        if tick is not None and missing:
            frame = _safe_frame(lambda: tick.load_historical_daily_klines(missing, start_date=start_date, end_date=end_date))
            if not frame.empty:
                frames.append(frame)
                sources.append("tickflow_index_daily")
                missing = _missing_symbols(missing, frame)
        tushare = self.tushare
        if tushare is not None and missing and hasattr(tushare, "load_index_daily"):
            frame = _safe_frame(lambda: tushare.load_index_daily(missing, start_date=start_date, end_date=end_date))
            if not frame.empty:
                frames.append(frame)
                sources.append("tushare_index_daily")
        result = _combine_frames(frames)
        result.attrs["data_source"] = "+".join(sources) if sources else "unavailable"
        return result

    def load_market_breadth(self) -> tuple[dict[str, Any], str]:
        frame = _safe_frame(lambda: self.load_realtime_quotes(universe_id=DEFAULT_A_SHARE_UNIVERSE_ID))
        if frame.empty:
            ak = self.akshare
            if ak is not None and hasattr(ak, "load_a_share_realtime_quotes"):
                frame = _safe_frame(lambda: ak.load_a_share_realtime_quotes())
                if not frame.empty:
                    frame.attrs["data_source"] = "akshare_spot_em"
        source = str(frame.attrs.get("data_source") or "unavailable") if not frame.empty else "unavailable"
        if frame.empty:
            return {}, "unavailable"
        return _breadth_metrics(frame), source

    def load_limit_sentiment(self, trade_date: str, storage: DuckDBStorage | None = None) -> dict[str, Any]:
        tushare = self.tushare
        if tushare is not None and hasattr(tushare, "load_limit_sentiment"):
            payload = _safe_payload(lambda: tushare.load_limit_sentiment(trade_date, storage=storage))
            if payload and not _limit_sentiment_needs_fallback(payload):
                return payload
        frame = _safe_frame(lambda: self.load_realtime_quotes(universe_id=DEFAULT_A_SHARE_UNIVERSE_ID))
        source = str(frame.attrs.get("data_source") or "realtime_quote_fallback") if not frame.empty else "unavailable"
        if not frame.empty:
            payload = build_quote_limit_sentiment_payload(
                trade_date=trade_date,
                frame=frame,
                data_source="realtime_quote_fallback" if source == "unavailable" else f"{source}+realtime_quote_fallback",
            )
            if tushare is None:
                payload["missing_fields"].append("limit_sentiment:tushare_unavailable")
            return payload
        return build_limit_sentiment_payload(
            trade_date=trade_date,
            data_source="unavailable",
            missing_fields=["limit_sentiment:unavailable"],
        )

    def load_hot_sector_context(
        self,
        trade_date: str,
        *,
        storage: DuckDBStorage,
        lookback_days: int = 5,
        top_industries: int = 10,
        top_concepts: int = 20,
    ) -> dict[str, Any]:
        provider = self.tushare
        if provider is None or not hasattr(provider, "load_hot_sector_context"):
            return {
                "trade_date": trade_date,
                "lookback_days": lookback_days,
                "hot_industries": [],
                "hot_concepts": [],
                "stock_hot_sectors": {},
                "missing_fields": ["hot_sector_context: provider_unavailable"],
                "data_sources": {},
            }
        return provider.load_hot_sector_context(
            trade_date,
            storage=storage,
            lookback_days=lookback_days,
            top_industries=top_industries,
            top_concepts=top_concepts,
        )

    def load_ths_sector_basic(self, *, storage: DuckDBStorage) -> pd.DataFrame:
        provider = self.tushare
        if provider is not None and hasattr(provider, "_load_ths_sector_basic"):
            return provider._load_ths_sector_basic(storage=storage)
        return storage.get_sector_basic(sector_types=["industry", "concept"])

    def load_ths_sector_members(self, sector_codes: list[str], *, storage: DuckDBStorage) -> pd.DataFrame:
        provider = self.tushare
        if provider is not None and hasattr(provider, "_load_ths_sector_members"):
            return provider._load_ths_sector_members(sector_codes, storage=storage)
        return storage.get_sector_members(sector_codes)

    def load_sw_sector_basic(self, *, storage: DuckDBStorage) -> pd.DataFrame:
        provider = self.tushare
        if provider is not None and hasattr(provider, "_load_sw_sector_basic"):
            return provider._load_sw_sector_basic(storage=storage)
        return storage.get_sector_basic(sector_types=["sw_l1", "sw_l2", "sw_l3"])

    def load_sw_sector_members(self, sector_codes: list[str], *, storage: DuckDBStorage) -> pd.DataFrame:
        provider = self.tushare
        if provider is not None and hasattr(provider, "_load_sw_sector_members"):
            return provider._load_sw_sector_members(sector_codes, storage=storage)
        return storage.get_sector_members(sector_codes)

    def load_chip_context(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        ak = self.akshare
        if ak is None or not hasattr(ak, "load_chip_context"):
            return {}
        return ak.load_chip_context(symbols)

    def load_fundamental_context(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        ak = self.akshare
        if ak is None or not hasattr(ak, "load_fundamental_context"):
            return {}
        return ak.load_fundamental_context(symbols)

    def load_company_fundamentals(
        self,
        symbols: list[str],
        *,
        trade_date: str = "",
        storage: DuckDBStorage | None = None,
        periods: int = 4,
    ) -> dict[str, dict[str, Any]]:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return {}
        names = _stock_name_lookup(self.load_stock_basic(storage=storage))
        result: dict[str, dict[str, Any]] = {}
        for ts_code in clean_symbols:
            data_sources: dict[str, str] = {}
            missing_fields: list[str] = []
            profile = _dataset_rows(
                self.fetch_tushare_stock_dataset(
                    "stock_company",
                    {"ts_code": ts_code},
                    fields=[
                        "ts_code",
                        "com_name",
                        "exchange",
                        "chairman",
                        "manager",
                        "secretary",
                        "reg_capital",
                        "setup_date",
                        "province",
                        "city",
                        "main_business",
                    ],
                    limit=1,
                ),
                dataset="stock_company",
                sources=data_sources,
                missing=missing_fields,
            )
            business = _latest_period_rows(
                _dataset_rows(
                    self.fetch_tushare_stock_dataset(
                        "fina_mainbz",
                        {"ts_code": ts_code},
                        fields=["ts_code", "end_date", "bz_item", "bz_code", "bz_sales", "bz_profit", "bz_cost", "curr_type"],
                        limit=200,
                    ),
                    dataset="fina_mainbz",
                    sources=data_sources,
                    missing=missing_fields,
                ),
                trade_date=trade_date,
                limit=12,
            )
            valuation = _latest_rows(
                _dataset_rows(
                    self.fetch_tushare_stock_dataset(
                        "daily_basic",
                        {"ts_code": ts_code, "end_date": trade_date},
                        fields=["ts_code", "trade_date", "close", "pe", "pe_ttm", "pb", "ps", "total_mv", "circ_mv"],
                        limit=20,
                    ),
                    dataset="daily_basic",
                    sources=data_sources,
                    missing=missing_fields,
                ),
                trade_date=trade_date,
                limit=1,
            )
            statements: dict[str, list[dict[str, Any]]] = {}
            statement_fields = {
                "fina_indicator": ["ts_code", "ann_date", "end_date", "eps", "grossprofit_margin", "netprofit_margin", "roe", "roa", "roic", "debt_to_assets", "current_ratio", "quick_ratio", "rd_exp"],
                "income": ["ts_code", "ann_date", "end_date", "basic_eps", "total_revenue", "revenue", "operate_profit", "total_profit", "n_income", "ebit", "ebitda"],
                "balancesheet": ["ts_code", "ann_date", "end_date", "total_assets", "total_cur_assets", "total_liab", "total_cur_liab", "total_hldr_eqy_inc_min_int"],
                "cashflow": ["ts_code", "ann_date", "end_date", "net_profit", "c_inf_fr_operate_a", "n_cashflow_act", "n_cashflow_inv_act", "n_cash_flows_fnc_act", "free_cashflow"],
            }
            for dataset, fields in statement_fields.items():
                rows = _dataset_rows(
                    self.fetch_tushare_stock_dataset(
                        dataset,
                        {"ts_code": ts_code, "end_date": trade_date},
                        fields=fields,
                        limit=max(8, periods * 2),
                    ),
                    dataset=dataset,
                    sources=data_sources,
                    missing=missing_fields,
                )
                statements[dataset] = _latest_rows(rows, trade_date=trade_date, limit=max(1, int(periods)))

            akshare_profile: dict[str, Any] = {}
            akshare_business: list[dict[str, Any]] = []
            if not profile:
                payload = self.fetch_akshare_dataset("stock_individual_info_em", {"symbol": ts_code[:6]}, limit=50)
                akshare_profile = _akshare_item_value_payload(payload.get("rows") or [])
                _record_optional_dataset(payload, "stock_individual_info_em", data_sources, missing_fields, available=bool(akshare_profile))
            if not business:
                payload = self.fetch_akshare_dataset("stock_zygc_em", {"symbol": ts_code[:6]}, limit=50)
                akshare_business = [dict(row) for row in payload.get("rows") or [] if isinstance(row, dict)]
                _record_optional_dataset(payload, "stock_zygc_em", data_sources, missing_fields, available=bool(akshare_business))

            company_profile = dict(profile[0]) if profile else akshare_profile
            name = names.get(ts_code) or str(company_profile.get("com_name") or company_profile.get("股票简称") or "")
            result[ts_code] = {
                "ts_code": ts_code,
                "name": name,
                "company_profile": _jsonable(company_profile),
                "main_business": str(company_profile.get("main_business") or company_profile.get("主营业务") or ""),
                "business_composition": _jsonable(business or akshare_business),
                "valuation": _jsonable(valuation[0] if valuation else {}),
                "financial_indicators": _jsonable(statements["fina_indicator"]),
                "income": _jsonable(statements["income"]),
                "balance_sheet": _jsonable(statements["balancesheet"]),
                "cashflow": _jsonable(statements["cashflow"]),
                "data_sources": data_sources,
                "missing_fields": list(dict.fromkeys(missing_fields)),
            }
        return result

    def load_statement_context(self, symbols: list[str], *, trade_date: str, limit: int = 8) -> dict[str, dict[str, Any]]:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for ts_code in clean_symbols:
            items: list[dict[str, Any]] = []
            sources: dict[str, str] = {}
            missing: list[str] = []
            for dataset in ("income", "balancesheet", "cashflow", "fina_indicator"):
                payload = self.fetch_tushare_stock_dataset(dataset, {"ts_code": ts_code}, limit=limit)
                rows = _rows_on_or_before(payload.get("rows") or [], trade_date)
                if rows:
                    items.extend({"dataset": dataset, **row} for row in rows[:limit])
                    sources[dataset] = str(payload.get("data_source") or f"tushare_{dataset}")
                else:
                    missing.append(f"statement:{dataset}:unavailable")
            result[ts_code] = _context_payload(
                items,
                data_source="+".join(sources.values()) if sources else "unavailable",
                missing_fields=missing,
                data_sources=sources,
            )
        return result

    def load_company_news_context(self, symbols: list[str], *, trade_date: str, lookback_days: int = 7, limit: int = 20) -> dict[str, dict[str, Any]]:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return {}
        start_date = _days_before(str(trade_date), max(1, int(lookback_days or 7)))
        result: dict[str, dict[str, Any]] = {}
        for ts_code in clean_symbols:
            items: list[dict[str, Any]] = []
            sources: dict[str, str] = {}
            missing: list[str] = []
            for dataset in ("anns_d", "research_report", "irm_qa_sh", "irm_qa_sz"):
                payload = self.fetch_tushare_dataset(
                    dataset,
                    {"ts_code": ts_code, "start_date": start_date, "end_date": str(trade_date)},
                    limit=limit,
                )
                rows = payload.get("rows") or []
                if rows:
                    items.extend({"dataset": dataset, **row} for row in rows[:limit])
                    sources[dataset] = str(payload.get("data_source") or f"tushare_{dataset}")
                else:
                    missing.append(f"company_news:{dataset}:unavailable")
            ak_payload = self.fetch_akshare_dataset("stock_news_em", {"symbol": ts_code.split(".", 1)[0]}, limit=limit)
            ak_rows = ak_payload.get("rows") or []
            if ak_rows:
                items.extend({"dataset": "stock_news_em", **row} for row in ak_rows[:limit])
                sources["stock_news_em"] = str(ak_payload.get("data_source") or "akshare_stock_news_em")
            result[ts_code] = _context_payload(
                items[:limit],
                data_source="+".join(sources.values()) if sources else "unavailable",
                missing_fields=missing if not items else [],
                data_sources=sources,
            )
        return result

    def load_macro_news_context(self, *, trade_date: str, lookback_days: int = 7, limit: int = 30) -> dict[str, Any]:
        start_date = _days_before(str(trade_date), max(1, int(lookback_days or 7)))
        items: list[dict[str, Any]] = []
        sources: dict[str, str] = {}
        missing: list[str] = []
        for dataset in ("news", "major_news", "cctv_news"):
            payload = self.fetch_tushare_dataset(
                dataset,
                {"start_date": start_date, "end_date": str(trade_date), "date": str(trade_date)},
                limit=limit,
            )
            rows = payload.get("rows") or []
            if rows:
                items.extend({"dataset": dataset, **row} for row in rows[:limit])
                sources[dataset] = str(payload.get("data_source") or f"tushare_{dataset}")
            else:
                missing.append(f"macro_news:{dataset}:unavailable")
        return _context_payload(
            items[:limit],
            data_source="+".join(sources.values()) if sources else "unavailable",
            missing_fields=missing if not items else [],
            data_sources=sources,
        )

    def load_holder_activity_context(self, symbols: list[str], *, trade_date: str, lookback_days: int = 90, limit: int = 20) -> dict[str, dict[str, Any]]:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return {}
        start_date = _days_before(str(trade_date), max(1, int(lookback_days or 90)))
        result: dict[str, dict[str, Any]] = {}
        for ts_code in clean_symbols:
            items: list[dict[str, Any]] = []
            sources: dict[str, str] = {}
            missing: list[str] = []
            for dataset in ("stk_holdertrade", "pledge_stat", "repurchase", "block_trade"):
                payload = self.fetch_tushare_stock_dataset(
                    dataset,
                    {"ts_code": ts_code, "start_date": start_date, "end_date": str(trade_date), "ann_date": str(trade_date)},
                    limit=limit,
                )
                rows = payload.get("rows") or []
                if rows:
                    items.extend({"dataset": dataset, **row} for row in rows[:limit])
                    sources[dataset] = str(payload.get("data_source") or f"tushare_{dataset}")
                else:
                    missing.append(f"holder_activity:{dataset}:unavailable")
            result[ts_code] = _context_payload(
                items[:limit],
                data_source="+".join(sources.values()) if sources else "unavailable",
                missing_fields=missing if not items else [],
                data_sources=sources,
            )
        return result

    def load_social_sentiment_context(self, symbols: list[str], *, limit: int = 50) -> dict[str, dict[str, Any]]:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return {}
        names = _stock_name_lookup(self.load_stock_basic())
        result: dict[str, dict[str, Any]] = {}
        for ts_code in clean_symbols:
            keywords = [ts_code.split(".", 1)[0]]
            name = names.get(ts_code)
            if name:
                keywords.append(name)
            try:
                from sats.web.social_hot import hot_mentions

                payload = hot_mentions(
                    keywords[0],
                    platforms=("xueqiu_stock", "xueqiu_spot", "weibo", "zhihu", "baidu"),
                    limit=limit,
                    extra_keywords=keywords[1:],
                    settings=self.settings,
                    use_cache=True,
                )
            except Exception as exc:
                payload = {
                    "status": "error",
                    "items": [],
                    "data_source": "unavailable",
                    "missing_fields": [f"social_sentiment:unavailable:{type(exc).__name__}"],
                }
            result[ts_code] = {
                **payload,
                "data_source": payload.get("source") or payload.get("data_source") or "social_hot",
                "missing_fields": list(payload.get("missing_fields") or ([] if payload.get("total_hits") else ["social_sentiment:empty_or_unavailable"])),
            }
        return result

    def load_realtime_quote_lookup(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        frame = self.load_realtime_quotes(symbols=symbols)
        return _records_by_symbol(frame)

    def load_news_context(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        symbols = kwargs.get("symbols")
        trade_date = str(kwargs.get("trade_date") or "")
        if symbols and trade_date:
            per_symbol = self.load_company_news_context(list(symbols), trade_date=trade_date)
            items = []
            for ts_code, payload in per_symbol.items():
                for item in payload.get("items") or []:
                    items.append({"ts_code": ts_code, **item})
            return _context_payload(items, data_source="company_news_context" if items else "unavailable", missing_fields=[] if items else ["news_context:unavailable"])
        if trade_date:
            return self.load_macro_news_context(trade_date=trade_date)
        return {"items": [], "missing_fields": ["news_context: provider_unavailable"], "data_source": "unavailable"}

    def load_event_context(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        symbols = kwargs.get("symbols")
        trade_date = str(kwargs.get("trade_date") or "")
        if symbols and trade_date:
            per_symbol = self.load_holder_activity_context(list(symbols), trade_date=trade_date)
            items = []
            for ts_code, payload in per_symbol.items():
                for item in payload.get("items") or []:
                    items.append({"ts_code": ts_code, **item})
            return _context_payload(items, data_source="holder_activity_context" if items else "unavailable", missing_fields=[] if items else ["event_context:unavailable"])
        return {"items": [], "missing_fields": ["event_context: provider_unavailable"], "data_source": "unavailable"}

    def list_tushare_stock_datasets(
        self,
        *,
        category: str | None = None,
        include_deprecated: bool = True,
    ) -> list[dict[str, Any]]:
        provider = self.tushare
        if provider is not None and hasattr(provider, "list_tushare_stock_datasets"):
            return provider.list_tushare_stock_datasets(
                category=category,
                include_deprecated=include_deprecated,
            )
        return list_tushare_stock_datasets(category=category, include_deprecated=include_deprecated)

    def list_tushare_datasets(
        self,
        *,
        domain: str | None = None,
        category: str | None = None,
        include_deprecated: bool = True,
        tags: list[str] | tuple[str, ...] | str | None = None,
    ) -> list[dict[str, Any]]:
        provider = self.tushare
        if provider is not None and hasattr(provider, "list_tushare_datasets"):
            return provider.list_tushare_datasets(
                domain=domain,
                category=category,
                include_deprecated=include_deprecated,
                tags=tags,
            )
        return list_tushare_datasets(
            domain=domain,
            category=category,
            include_deprecated=include_deprecated,
            tags=tags,
        )

    def fetch_tushare_dataset(
        self,
        dataset: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        spec = get_tushare_dataset(dataset)
        provider = self.tushare
        if provider is None:
            return _unavailable_tushare_dataset_payload(spec, params or {})
        if hasattr(provider, "fetch_dataset"):
            return provider.fetch_dataset(dataset, params or {}, fields=fields, limit=limit)
        if spec.domain == "股票数据" and hasattr(provider, "fetch_stock_dataset"):
            return provider.fetch_stock_dataset(dataset, params or {}, fields=fields, limit=limit)
        return _unavailable_tushare_dataset_payload(spec, params or {})

    def fetch_tushare_stock_dataset(
        self,
        dataset: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        spec = get_tushare_stock_dataset(dataset)
        provider = self.tushare
        if provider is None or not hasattr(provider, "fetch_stock_dataset"):
            if provider is not None and hasattr(provider, "fetch_dataset"):
                return provider.fetch_dataset(dataset, params or {}, fields=fields, limit=limit)
            return _unavailable_tushare_dataset_payload(spec, params or {})
        return provider.fetch_stock_dataset(dataset, params or {}, fields=fields, limit=limit)

    def _recent_trade_dates(self, trade_date: str, count: int) -> list[str]:
        provider = self.tushare
        if provider is not None and hasattr(provider, "_recent_trade_dates"):
            try:
                return provider._recent_trade_dates(trade_date, count=count)
            except Exception:
                pass
        dates = []
        cursor = datetime.strptime(str(trade_date), "%Y%m%d")
        while len(dates) < count:
            if cursor.weekday() < 5:
                dates.append(cursor.strftime("%Y%m%d"))
            cursor -= timedelta(days=1)
        return sorted(dates)


def _context_payload(
    items: list[dict[str, Any]],
    *,
    data_source: str,
    missing_fields: list[str] | tuple[str, ...] | None = None,
    data_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "items": _jsonable(items),
        "data_source": data_source or "unavailable",
        "data_sources": dict(data_sources or {}),
        "missing_fields": list(missing_fields or []),
    }


def _rows_on_or_before(rows: Any, trade_date: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    cutoff = str(trade_date or "").replace("-", "")[:8]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_value = ""
        for key in ("f_ann_date", "ann_date", "pub_date", "end_date", "trade_date", "datetime", "date"):
            raw = row.get(key)
            if raw:
                date_value = str(raw).replace("-", "")[:8]
                break
        if not cutoff or not date_value or date_value <= cutoff:
            result.append(row)
    return result


def _dataset_rows(
    payload: dict[str, Any],
    *,
    dataset: str,
    sources: dict[str, str],
    missing: list[str],
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in payload.get("rows") or [] if isinstance(row, dict)]
    source = str(payload.get("data_source") or "")
    if rows:
        sources[dataset] = source or dataset
    else:
        reasons = [str(item) for item in payload.get("missing_fields") or [] if str(item)]
        missing.extend(reasons or [f"{dataset}:unavailable"])
    return rows


def _record_optional_dataset(
    payload: dict[str, Any],
    dataset: str,
    sources: dict[str, str],
    missing: list[str],
    *,
    available: bool,
) -> None:
    if available:
        sources[dataset] = str(payload.get("data_source") or f"akshare_{dataset}")
        return
    reasons = [str(item) for item in payload.get("missing_fields") or [] if str(item)]
    missing.extend(reasons or [f"{dataset}:unavailable"])


def _latest_rows(rows: list[dict[str, Any]], *, trade_date: str, limit: int) -> list[dict[str, Any]]:
    filtered = _rows_on_or_before(rows, trade_date)
    ordered = sorted(
        filtered,
        key=lambda row: str(row.get("end_date") or row.get("trade_date") or row.get("ann_date") or ""),
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for row in ordered:
        date_value = str(row.get("end_date") or row.get("trade_date") or row.get("ann_date") or "")
        if date_value and date_value in seen_dates:
            continue
        if date_value:
            seen_dates.add(date_value)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def _latest_period_rows(rows: list[dict[str, Any]], *, trade_date: str, limit: int) -> list[dict[str, Any]]:
    filtered = _rows_on_or_before(rows, trade_date)
    dates = sorted(
        {str(row.get("end_date") or "") for row in filtered if str(row.get("end_date") or "")},
        reverse=True,
    )
    latest = dates[0] if dates else ""
    selected = [row for row in filtered if not latest or str(row.get("end_date") or "") == latest]
    return selected[:limit]


def _akshare_item_value_payload(rows: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("item") if row.get("item") is not None else row.get("项目")
        value = row.get("value") if row.get("value") is not None else row.get("值")
        if key not in (None, ""):
            result[str(key)] = value
    return result


def _stock_name_lookup(frame: pd.DataFrame | None) -> dict[str, str]:
    if frame is None or frame.empty or "ts_code" not in frame.columns or "name" not in frame.columns:
        return {}
    return {
        str(row.get("ts_code")): str(row.get("name") or "").strip()
        for _, row in frame.iterrows()
        if str(row.get("ts_code") or "").strip() and str(row.get("name") or "").strip()
    }


def _with_cached_previous_close(frame: pd.DataFrame, *, storage: DuckDBStorage) -> pd.DataFrame:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return frame
    data = frame.copy()
    trade_date = (
        str(data["trade_date"].dropna().astype(str).max())
        if "trade_date" in data.columns and not data["trade_date"].dropna().empty
        else datetime.now().strftime("%Y%m%d")
    )
    symbols = sorted({str(value) for value in data["ts_code"].dropna().tolist() if str(value)})
    try:
        history = storage.get_stock_daily_range(
            symbols,
            start_date=_days_before(trade_date, 400),
            end_date=trade_date,
            with_meta=False,
        )
    except Exception:
        history = pd.DataFrame()
    previous_close: dict[str, float] = {}
    if not history.empty:
        history = history[history["trade_date"].astype(str) < trade_date]
        history = history.sort_values(["ts_code", "trade_date"]).drop_duplicates("ts_code", keep="last")
        previous_close = {
            str(row["ts_code"]): _safe_float(row.get("close"))
            for _, row in history.iterrows()
        }
    if "pre_close" not in data.columns:
        data["pre_close"] = None
    if "pct_chg" not in data.columns:
        data["pct_chg"] = None
    for index, row in data.iterrows():
        ts_code = str(row.get("ts_code") or "")
        pre_close = _safe_float(row.get("pre_close")) or previous_close.get(ts_code, 0.0)
        close = _safe_float(row.get("close"))
        if pre_close > 0:
            data.at[index, "pre_close"] = pre_close
            data.at[index, "pct_chg"] = (close / pre_close - 1.0) * 100.0 if close > 0 else None
    data.attrs.update(frame.attrs)
    return data


def _days_before(trade_date: str, calendar_days: int) -> str:
    raw = str(trade_date or "").strip()
    fmt = "%Y%m%d" if "-" not in raw else "%Y-%m-%d"
    try:
        end = datetime.strptime(raw, fmt)
    except ValueError:
        end = datetime.now()
    return (end - timedelta(days=calendar_days)).strftime("%Y%m%d")


def _historical_daily_lookback_days(start_date: int | str | None, end_date: int | str) -> int:
    if start_date is None:
        return 180
    try:
        start = datetime.strptime("".join(char for char in str(start_date) if char.isdigit())[:8], "%Y%m%d")
        end = datetime.strptime("".join(char for char in str(end_date) if char.isdigit())[:8], "%Y%m%d")
    except ValueError:
        return 180
    calendar_days = max(0, (end - start).days)
    return max(180, (calendar_days + 1) // 2 + 5)


def _safe_frame(loader: Callable[[], Any]) -> pd.DataFrame:
    try:
        frame = loader()
    except Exception:
        return pd.DataFrame()
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def _finalize_minute_period_frame(
    frame: pd.DataFrame,
    *,
    target_period: str,
    base_period: str,
    count: int | None,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame
    data = frame
    if target_period != base_period:
        data = aggregate_minute_klines(data, target_period=target_period, source_period=base_period)
    else:
        data = ensure_minute_frame_period(data, period=target_period)
    return tail_minute_klines(data, count)


def _safe_list(loader: Callable[[], Any]) -> list[Any]:
    try:
        value = loader()
    except Exception:
        return []
    return value if isinstance(value, list) else []


def _safe_payload(loader: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = loader()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _unavailable_tushare_dataset_payload(spec: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset": spec.dataset,
        "api": spec.api,
        "domain": spec.domain,
        "category": spec.category,
        "title": spec.title,
        "doc_id": spec.doc_id,
        "tags": list(getattr(spec, "tags", ())),
        "params": dict(params or {}),
        "columns": [],
        "rows": [],
        "row_count": 0,
        "returned_row_count": 0,
        "data_source": "unavailable",
        "missing_fields": ["tushare:unavailable"],
    }


def _limit_sentiment_needs_fallback(payload: dict[str, Any]) -> bool:
    if str(payload.get("data_source") or "") in {"", "unavailable"}:
        return True
    missing = {str(item) for item in payload.get("missing_fields") or []}
    return "limit_sentiment:tushare_empty" in missing


def _combine_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    data = pd.concat(valid, ignore_index=True, sort=False)
    subset = [column for column in ("ts_code", "trade_date") if column in data.columns]
    if subset:
        data = data.drop_duplicates(subset=subset, keep="last")
    return data.reset_index(drop=True)


def _indicator_lookup(items: list[IndicatorInput]) -> dict[str, IndicatorInput]:
    return {item.ts_code: item for item in items}


def _merge_indicator_input(
    ts_code: str,
    trade_date: str,
    tick: IndicatorInput | None,
    tushare: IndicatorInput | None,
) -> IndicatorInput:
    daily = pd.DataFrame()
    daily_source = "unavailable"
    if tick is not None and not _is_empty(tick.daily):
        daily = tick.daily
        daily_source = tick.data_sources.get("daily", "tickflow_daily")
    elif tushare is not None and not _is_empty(tushare.daily):
        daily = tushare.daily
        daily_source = tushare.data_sources.get("daily", "tushare_daily")

    daily_basic = pd.DataFrame()
    daily_basic_source = "unavailable"
    if tushare is not None and not _is_empty(tushare.daily_basic):
        daily_basic = tushare.daily_basic
        daily_basic_source = tushare.data_sources.get("daily_basic", "tushare_daily_basic")
    elif tick is not None and not _is_empty(tick.daily_basic):
        daily_basic = tick.daily_basic
        daily_basic_source = tick.data_sources.get("daily_basic", "tickflow_realtime_basic_like")

    moneyflow = tushare.moneyflow if tushare is not None and not _is_empty(tushare.moneyflow) else pd.DataFrame()
    fundamentals = tushare.fundamentals if tushare is not None and not _is_empty(tushare.fundamentals) else pd.DataFrame()
    stock_basic = _merge_dicts(tick.stock_basic if tick is not None else {}, tushare.stock_basic if tushare is not None else {})
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


def _missing_symbols(symbols: list[str], frame: pd.DataFrame) -> list[str]:
    if frame.empty or "ts_code" not in frame.columns:
        return symbols
    present = {str(value) for value in frame["ts_code"].dropna().unique()}
    return [symbol for symbol in symbols if symbol not in present]


def _records_by_symbol(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    return {
        str(row.get("ts_code")): _jsonable(row.dropna().to_dict())
        for _, row in frame.iterrows()
        if str(row.get("ts_code") or "").strip()
    }


def _breadth_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    data = frame.copy()
    pct = pd.to_numeric(data.get("pct_chg"), errors="coerce") if "pct_chg" in data.columns else pd.Series(dtype=float)
    close = pd.to_numeric(data.get("close"), errors="coerce") if "close" in data.columns else pd.Series(dtype=float)
    amount = pd.to_numeric(data.get("amount"), errors="coerce") if "amount" in data.columns else pd.Series(dtype=float)
    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    return {
        "total": int(len(data)),
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "advancing_count": up,
        "declining_count": down,
        "unchanged_count": flat,
        "limit_up_count": int((pct >= 9.8).sum()),
        "limit_down_count": int((pct <= -9.8).sum()),
        "median_pct_chg": _safe_float(pct.median()),
        "average_pct_chg": _safe_float(pct.mean()),
        "total_amount": _safe_float(amount.sum()),
        "valid_price_count": int((close > 0).sum()),
        "trade_date": _latest_text(data, "trade_date"),
        "latest_trade_time": _latest_text(data, "trade_time"),
    }


def _latest_text(frame: pd.DataFrame, column: str) -> str:
    if frame is None or frame.empty or column not in frame.columns:
        return ""
    values = frame[column].dropna().astype(str)
    if values.empty:
        return ""
    return str(values.max() or "")


def _calendar_dates(trade_date: str, calendar_days: int) -> list[str]:
    end = datetime.strptime(trade_date, "%Y%m%d")
    return [(end - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(calendar_days, -1, -1)]


def _is_empty(frame: pd.DataFrame | None) -> bool:
    return frame is None or frame.empty


def _merge_dicts(*items: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        result.update({key: value for key, value in item.items() if value not in (None, "")})
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
