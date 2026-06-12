from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from sats.config import Settings
from sats.data.akshare_provider import AkShareDataProvider
from sats.data.base import MarketDataProvider
from sats.data.limit_sentiment import build_limit_sentiment_payload, build_quote_limit_sentiment_payload
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
                    lookback_days=180,
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
        if tick is not None:
            if clean_symbols is not None:
                frame = _safe_frame(lambda: tick.load_realtime_quotes(symbols=clean_symbols))
            else:
                frame = _safe_frame(lambda: tick.load_realtime_quotes(universe_id=universe_id))
            if not frame.empty:
                frame.attrs["data_source"] = "tickflow_quote" if clean_symbols is not None else "tickflow_universe_quote"
                return frame
        if clean_symbols is not None:
            ak = self.akshare
            if ak is not None:
                frame = _safe_frame(lambda: ak.load_realtime_quotes(clean_symbols))
                if not frame.empty:
                    frame.attrs["data_source"] = "akshare_spot_em"
                    return frame
        elif universe_id is not None:
            ak = self.akshare
            if ak is not None and hasattr(ak, "load_a_share_realtime_quotes"):
                frame = _safe_frame(lambda: ak.load_a_share_realtime_quotes())
                if not frame.empty:
                    frame.attrs["data_source"] = "akshare_spot_em"
                    return frame
        return pd.DataFrame()

    def load_realtime_daily_quotes(self, symbols: list[str], *, trade_date: str) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        tick = self.tickflow
        if tick is not None and hasattr(tick, "load_realtime_daily_quotes"):
            frame = _safe_frame(lambda: tick.load_realtime_daily_quotes(clean_symbols, trade_date=trade_date))
            if not frame.empty:
                frame.attrs["data_source"] = "tickflow_quote_daily"
                return frame
        quotes = self.load_realtime_quotes(symbols=clean_symbols)
        if quotes.empty:
            return pd.DataFrame()
        frame = quotes.copy()
        frame["trade_date"] = str(trade_date)
        frame.attrs["data_source"] = str(quotes.attrs.get("data_source") or "quote_daily")
        return frame

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
        tick = self.tickflow
        if tick is None:
            return pd.DataFrame()
        return tick.load_realtime_minute_klines(clean_symbols, period=period, count=count)

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
        tick = self.tickflow
        if tick is None:
            return pd.DataFrame()
        return tick.load_historical_minute_klines(
            clean_symbols,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
        )

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
        frame = self.load_realtime_quotes(universe_id=DEFAULT_A_SHARE_UNIVERSE_ID)
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
        frame = self.load_realtime_quotes(universe_id=DEFAULT_A_SHARE_UNIVERSE_ID)
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

    def load_realtime_quote_lookup(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        frame = self.load_realtime_quotes(symbols=symbols)
        return _records_by_symbol(frame)

    def load_news_context(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"items": [], "missing_fields": ["news_context: provider_unavailable"], "data_source": "unavailable"}

    def load_event_context(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
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


def _safe_frame(loader: Callable[[], Any]) -> pd.DataFrame:
    try:
        frame = loader()
    except Exception:
        return pd.DataFrame()
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


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
    }


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
