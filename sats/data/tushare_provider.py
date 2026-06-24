from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import warnings
from zoneinfo import ZoneInfo

import pandas as pd

from sats.config import Settings
from sats.data.base import MarketDataProvider
from sats.data.limit_sentiment import build_limit_sentiment_payload
from sats.data.tickflow_provider import TickFlowDataProvider
from sats.data.tushare_stock_datasets import (
    get_tushare_dataset,
    get_tushare_stock_dataset,
    list_tushare_datasets,
    list_tushare_stock_datasets,
)
from sats.indicators import IndicatorInput
from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule
from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_ts_code

try:
    import tushare as ts
except ImportError as exc:  # pragma: no cover - dependency guard
    ts = None  # type: ignore[assignment]
    _TUSHARE_IMPORT_ERROR = exc
else:
    _TUSHARE_IMPORT_ERROR = None

SCREENING_TRADE_DAYS = 80
TUSHARE_ROW_WARNING_LIMIT = 5900
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PRICE_VOLUME_MA_RULE_NAME = "price_volume_ma"
CHAN_THIRD_BUY_RULE_NAME = "chan_third_buy"
CHAN_COMPOSITE_RULE_NAME = "chan_composite"
CHAN_SIGNALS_RULE_NAME = "chan_signals"
MONTHLY_BASE_BREAKOUT_RULE_NAME = "monthly_base_breakout"
SIGNAL_COMPOSITE_RULE_NAME = "signal_composite"
SIGNAL_DISCOVERY_RULE_NAME = "signal_discovery"
FACTOR_OVERLAY_RULE_NAME = "factor_overlay"
INTERNAL_SCREENING_INPUT_RULE_NAMES = {
    FACTOR_OVERLAY_RULE_NAME,
    SIGNAL_DISCOVERY_RULE_NAME,
}
CHAN_DAILY_BASIC_OPTIONAL_RULE_NAMES = {
    CHAN_THIRD_BUY_RULE_NAME,
    CHAN_COMPOSITE_RULE_NAME,
    CHAN_SIGNALS_RULE_NAME,
    MONTHLY_BASE_BREAKOUT_RULE_NAME,
    SIGNAL_COMPOSITE_RULE_NAME,
    SIGNAL_DISCOVERY_RULE_NAME,
}
MONTHLY_BASE_BREAKOUT_PERIOD = "1M"
MONTHLY_BASE_BREAKOUT_MONTH_COUNT = 120
MONTHLY_BASE_BREAKOUT_DAILY_CALENDAR_DAYS = 365 * 11
PRICE_VOLUME_PCT_CHG_RANGE = (3.0, 5.0)
PRICE_VOLUME_TURNOVER_RANGE = (5.0, 10.0)
PRICE_VOLUME_CIRC_MV_RANGE = (500_000.0, 2_000_000.0)
HOT_SECTOR_TYPES = {"industry": "I", "concept": "N"}
HOT_SECTOR_DATA_SOURCE = "tushare_ths"
SW_SECTOR_LEVELS = {"sw_l1": "L1", "sw_l2": "L2", "sw_l3": "L3"}
SW_SECTOR_SRC = "SW2021"
SW_SECTOR_BASIC_DATA_SOURCE = "tushare_sw_index_classify"
SW_SECTOR_MEMBER_DATA_SOURCE = "tushare_sw_index_member_all"


class TushareDataProvider(MarketDataProvider):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pro = None
        if settings.tushare_token and ts is not None:
            self.pro = ts.pro_api(
                settings.tushare_token,
                timeout=getattr(settings, "tushare_timeout_seconds", 30),
            )
        self._stock_basic_cache: dict[str, dict[str, Any]] = {}

    def list_a_share_symbols(self) -> list[str]:
        frame = self._stock_basic_frame()
        return sorted(frame["ts_code"].tolist()) if not frame.empty else []

    def load_all_screening_inputs(
        self,
        trade_date: str,
        *,
        storage: DuckDBStorage,
        trade_days: int = SCREENING_TRADE_DAYS,
        rule_name: str | None = None,
    ) -> list[ScreeningInput]:
        stock_basic = self._stock_basic_frame(storage=storage)
        if stock_basic.empty:
            return []

        return self._load_screening_inputs_for_stock_basic(
            stock_basic,
            trade_date,
            storage=storage,
            trade_days=trade_days,
            rule_name=rule_name,
        )

    def load_screening_inputs(
        self,
        symbols: list[str],
        trade_date: str,
        *,
        storage: DuckDBStorage,
        trade_days: int = SCREENING_TRADE_DAYS,
        rule_name: str | None = None,
    ) -> list[ScreeningInput]:
        clean_symbols = [_normalize_ts_code(symbol) for symbol in symbols if str(symbol).strip()]
        if not clean_symbols:
            return []
        try:
            stock_basic = self._stock_basic_frame(storage=storage)
        except Exception:
            stock_basic = pd.DataFrame()
        rows: list[dict[str, Any]] = []
        lookup = {
            str(row["ts_code"]): row.dropna().to_dict()
            for _, row in stock_basic.iterrows()
        } if not stock_basic.empty else {}
        for ts_code in clean_symbols:
            row = lookup.get(ts_code) or self._stock_basic(ts_code) or {}
            if not row:
                row = {"ts_code": ts_code, "symbol": ts_code.split(".", 1)[0], "name": ""}
            row["ts_code"] = ts_code
            rows.append(row)
        return self._load_screening_inputs_for_stock_basic(
            pd.DataFrame(rows),
            trade_date,
            storage=storage,
            trade_days=trade_days,
            rule_name=rule_name,
        )

    def _load_screening_inputs_for_stock_basic(
        self,
        stock_basic: pd.DataFrame,
        trade_date: str,
        *,
        storage: DuckDBStorage,
        trade_days: int = SCREENING_TRADE_DAYS,
        rule_name: str | None = None,
    ) -> list[ScreeningInput]:
        stock_basic = _normalize_stock_basic_frame(stock_basic)
        if stock_basic.empty:
            return []
        storage.upsert_stock_basic(stock_basic)
        trade_dates = self._recent_trade_dates(trade_date, count=trade_days)
        if not trade_dates:
            raise ValueError(f"No open trade dates found before {trade_date}")
        if trade_dates[-1] != str(trade_date):
            raise ValueError(f"{trade_date} is not an open trade date; latest open trade date is {trade_dates[-1]}")

        force_realtime = _should_force_realtime(trade_date)
        screening_rule = (
            None
            if rule_name in INTERNAL_SCREENING_INPUT_RULE_NAMES
            else get_rule(rule_name) if rule_name else None
        )
        require_daily_basic = rule_name not in CHAN_DAILY_BASIC_OPTIONAL_RULE_NAMES
        cache_trade_dates = [date for date in trade_dates if not (force_realtime and date == str(trade_date))]
        self._ensure_stock_daily(storage, cache_trade_dates, stock_basic=stock_basic, defer_dates={str(trade_date)})
        if require_daily_basic:
            self._ensure_stock_daily_basic(storage, cache_trade_dates, defer_dates={str(trade_date)})
        data_source = self._ensure_current_trade_date_data(
            storage,
            trade_date,
            stock_basic,
            force_realtime=force_realtime,
            require_daily_basic=require_daily_basic,
            previous_trade_date=trade_dates[-2] if len(trade_dates) >= 2 else None,
        )

        daily_frame = storage.get_stock_daily(trade_dates)
        basic_frame = storage.get_stock_daily_basic(trade_dates) if require_daily_basic else pd.DataFrame()
        realtime_daily = data_source.get("realtime_daily")
        realtime_basic = data_source.get("realtime_basic") if require_daily_basic else None
        if realtime_daily is not None:
            daily_frame = _replace_trade_date_rows(daily_frame, realtime_daily, trade_date)
        if realtime_basic is not None:
            basic_frame = _replace_trade_date_rows(basic_frame, realtime_basic, trade_date)

        daily_groups = _group_by_ts_code(daily_frame)
        basic_groups = _group_by_ts_code(basic_frame)
        price_volume_metadata = {}
        intraday_metadata = {}
        monthly_metadata = {}
        monthly_sources = {}
        if rule_name == PRICE_VOLUME_MA_RULE_NAME:
            price_volume_metadata = self._build_price_volume_ma_metadata(
                stock_basic=stock_basic,
                daily_frame=daily_frame,
                daily_basic_frame=basic_frame,
                trade_date=trade_date,
                force_realtime=data_source["source"] != "tushare_daily",
            )
        if screening_rule is not None and screening_rule.intraday_kline_requirements:
            intraday_metadata = self._build_rule_intraday_metadata(
                rule=screening_rule,
                stock_basic=stock_basic,
                daily_groups=daily_groups,
                trade_date=trade_date,
                use_realtime=force_realtime,
                data_source=data_source["source"],
            )
        if rule_name == MONTHLY_BASE_BREAKOUT_RULE_NAME:
            monthly_metadata, monthly_sources = self._build_monthly_base_breakout_metadata(
                stock_basic=stock_basic,
                trade_date=trade_date,
                daily_groups=daily_groups,
                use_realtime=force_realtime,
            )

        inputs = []
        for _, row in stock_basic.iterrows():
            ts_code = str(row["ts_code"])
            stock_info = row.dropna().to_dict()
            daily_basic = basic_groups.get(ts_code, pd.DataFrame()) if require_daily_basic else pd.DataFrame()
            metadata = {
                "data_source": data_source["source"],
                "daily_basic_source": data_source.get("daily_basic_source", ""),
            }
            if rule_name == PRICE_VOLUME_MA_RULE_NAME:
                metadata[PRICE_VOLUME_MA_RULE_NAME] = price_volume_metadata.get(
                    ts_code,
                    _empty_price_volume_ma_metadata(ts_code),
                )
            metadata.update(intraday_metadata.get(ts_code, {}))
            if rule_name == MONTHLY_BASE_BREAKOUT_RULE_NAME:
                metadata["monthly_1M"] = monthly_metadata.get(ts_code, pd.DataFrame())
                metadata["monthly_1M_source"] = monthly_sources.get(ts_code, "unavailable")
            inputs.append(
                ScreeningInput(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    daily=daily_groups.get(ts_code, pd.DataFrame()),
                    daily_basic=daily_basic,
                    stock_basic=stock_info,
                    metadata=metadata,
                )
            )
        return inputs

    def load_screening_input(self, ts_code: str, trade_date: str) -> ScreeningInput:
        start_date = _start_date(trade_date, calendar_days=180)
        daily = pd.DataFrame()
        daily_basic = pd.DataFrame()
        data_source = "tushare_daily"
        if self.pro is not None:
            daily = self._call_tushare_with_retry(
                lambda: self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=trade_date),
                operation="daily",
            )
            daily_basic = self._call_tushare_with_retry(
                lambda: self.pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=trade_date),
                operation="daily_basic",
            )
        if daily.empty:
            daily = self._tickflow_provider().load_historical_daily_klines(
                [ts_code],
                start_date=start_date,
                end_date=trade_date,
            )
            data_source = "tickflow_daily"
        stock_basic = self._stock_basic(ts_code)

        return ScreeningInput(
            ts_code=ts_code,
            trade_date=trade_date,
            daily=daily,
            daily_basic=daily_basic,
            stock_basic=stock_basic,
            metadata={"data_source": data_source},
        )

    def load_indicator_inputs(
        self,
        symbols: list[str],
        trade_date: str,
        *,
        lookback_days: int = 180,
        storage: DuckDBStorage | None = None,
    ) -> list[IndicatorInput]:
        clean_symbols = [_normalize_ts_code(symbol) for symbol in symbols if str(symbol).strip()]
        if not clean_symbols:
            return []
        start_date = _start_date(trade_date, calendar_days=max(lookback_days * 2, 260))
        storage = storage or DuckDBStorage(self.settings.db_path)
        stock_basic = self._stock_basic_frame(storage=storage)
        stock_lookup = {
            str(row["ts_code"]): row.dropna().to_dict()
            for _, row in stock_basic.iterrows()
            if str(row.get("ts_code") or "").strip()
        }

        daily = self._load_indicator_daily(clean_symbols, start_date, trade_date, storage=storage)
        daily_basic = self._load_indicator_daily_basic(clean_symbols, start_date, trade_date, storage=storage)
        moneyflow = self._load_indicator_moneyflow(clean_symbols, start_date, trade_date, storage=storage)
        fundamentals = self._load_indicator_fundamentals(clean_symbols, trade_date, storage=storage)

        daily_groups = _group_by_ts_code(daily)
        basic_groups = _group_by_ts_code(daily_basic)
        moneyflow_groups = _group_by_ts_code(moneyflow)
        fundamental_groups = _group_by_ts_code(fundamentals)
        inputs: list[IndicatorInput] = []
        for ts_code in clean_symbols:
            inputs.append(
                IndicatorInput(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    daily=daily_groups.get(ts_code, pd.DataFrame()),
                    daily_basic=basic_groups.get(ts_code, pd.DataFrame()),
                    moneyflow=moneyflow_groups.get(ts_code, pd.DataFrame()),
                    fundamentals=fundamental_groups.get(ts_code, pd.DataFrame()),
                    stock_basic=stock_lookup.get(ts_code, {}),
                    data_sources={
                        "daily": str(daily.attrs.get("data_source") or "tushare_daily"),
                        "daily_basic": str(daily_basic.attrs.get("data_source") or "tushare_daily_basic"),
                        "moneyflow": str(moneyflow.attrs.get("data_source") or "tushare_moneyflow_dc"),
                        "fundamentals": str(fundamentals.attrs.get("data_source") or "tushare_fundamentals"),
                    },
                )
            )
        return inputs

    def load_index_daily(self, index_codes: list[str], *, start_date: str, end_date: str) -> pd.DataFrame:
        columns = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
        if self.pro is None:
            return pd.DataFrame(columns=columns)
        frames: list[pd.DataFrame] = []
        for ts_code in [str(code).strip().upper() for code in index_codes if str(code).strip()]:
            try:
                frame = self._call_tushare_with_retry(
                    lambda ts_code=ts_code: self.pro.index_daily(
                        ts_code=ts_code,
                        start_date=start_date,
                        end_date=end_date,
                    ),
                    operation="index_daily",
                )
            except Exception:
                continue
            frames.append(frame)
        data = _combine_daily_frames(frames)
        data.attrs["data_source"] = "tushare_index_daily"
        return data

    def load_limit_sentiment(self, trade_date: str, storage: DuckDBStorage | None = None) -> dict[str, Any]:
        counts = {"U": 0, "D": 0, "Z": 0}
        missing_fields: list[str] = []
        success = False
        if self.pro is None:
            return build_limit_sentiment_payload(
                trade_date=trade_date,
                data_source="unavailable",
                missing_fields=["limit_sentiment:tushare_unavailable"],
            )
        for limit_type, field_name in (
            ("U", "limit_up_count"),
            ("D", "limit_down_count"),
            ("Z", "broken_limit_count"),
        ):
            try:
                frame = self._call_tushare_with_retry(
                    lambda limit_type=limit_type: _call_pro(
                        self.pro,
                        "limit_list_d",
                        trade_date=str(trade_date),
                        limit_type=limit_type,
                    ),
                    operation=f"limit_list_d:{limit_type}",
                )
            except Exception as exc:
                missing_fields.append(f"{field_name}: {exc}")
                continue
            counts[limit_type] = int(len(frame)) if frame is not None else 0
            success = True
        if success and not any(counts.values()):
            missing_fields.append("limit_sentiment:tushare_empty")
        return build_limit_sentiment_payload(
            trade_date=trade_date,
            limit_up_count=counts["U"],
            limit_down_count=counts["D"],
            broken_limit_count=counts["Z"],
            data_source="tushare_limit_list_d" if success else "unavailable",
            missing_fields=missing_fields,
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
        lookback_days = _clamp_hot_sector_days(lookback_days)
        trade_dates = self._recent_trade_dates(trade_date, count=lookback_days)
        if not trade_dates:
            trade_dates = _fallback_weekday_dates(trade_date, count=lookback_days)
        context = {
            "trade_date": trade_date,
            "lookback_days": lookback_days,
            "hot_industries": [],
            "hot_concepts": [],
            "stock_hot_sectors": {},
            "missing_fields": [],
            "data_sources": {"sector_basic": "", "sector_daily": "", "sector_members": ""},
        }
        try:
            sector_basic = self._load_ths_sector_basic(storage=storage)
        except Exception as exc:
            context["missing_fields"].append(f"hot_sector_basic: {exc}")
            return context
        if sector_basic.empty:
            context["missing_fields"].append("hot_sector_basic")
            return context
        context["data_sources"]["sector_basic"] = str(sector_basic.attrs.get("data_source") or HOT_SECTOR_DATA_SOURCE)

        scored_frames: list[pd.DataFrame] = []
        for sector_type, limit in [("industry", top_industries), ("concept", top_concepts)]:
            pool = sector_basic[sector_basic["sector_type"].astype(str) == sector_type].copy()
            if pool.empty:
                context["missing_fields"].append(f"hot_sector_{sector_type}_basic")
                continue
            daily = self._load_ths_sector_daily(pool["sector_code"].astype(str).tolist(), trade_dates, storage=storage)
            if daily.empty:
                context["missing_fields"].append(f"hot_sector_{sector_type}_daily")
                continue
            context["data_sources"]["sector_daily"] = str(daily.attrs.get("data_source") or HOT_SECTOR_DATA_SOURCE)
            scored = _score_hot_sectors(pool, daily, sector_type=sector_type, limit=limit)
            if scored.empty:
                context["missing_fields"].append(f"hot_sector_{sector_type}_score")
                continue
            scored_frames.append(scored)
            key = "hot_industries" if sector_type == "industry" else "hot_concepts"
            context[key] = _sector_records(scored)

        hot = pd.concat(scored_frames, ignore_index=True, sort=False) if scored_frames else pd.DataFrame()
        if hot.empty:
            context["missing_fields"].append("hot_sector_empty")
            return context
        members = self._load_ths_sector_members(hot["sector_code"].astype(str).tolist(), storage=storage)
        if members.empty:
            context["missing_fields"].append("hot_sector_members")
            return context
        context["data_sources"]["sector_members"] = str(members.attrs.get("data_source") or HOT_SECTOR_DATA_SOURCE)
        context["stock_hot_sectors"] = _build_stock_hot_sector_map(hot, members)
        return context

    def list_tushare_stock_datasets(
        self,
        *,
        category: str | None = None,
        include_deprecated: bool = True,
    ) -> list[dict[str, Any]]:
        return list_tushare_stock_datasets(category=category, include_deprecated=include_deprecated)

    def list_tushare_datasets(
        self,
        *,
        domain: str | None = None,
        category: str | None = None,
        include_deprecated: bool = True,
        tags: list[str] | tuple[str, ...] | str | None = None,
    ) -> list[dict[str, Any]]:
        return list_tushare_datasets(
            domain=domain,
            category=category,
            include_deprecated=include_deprecated,
            tags=tags,
        )

    def fetch_dataset(
        self,
        dataset: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        spec = get_tushare_dataset(dataset)
        return self._fetch_dataset_spec(spec, params or {}, fields=fields, limit=limit)

    def fetch_stock_dataset(
        self,
        dataset: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        spec = get_tushare_stock_dataset(dataset)
        return self._fetch_dataset_spec(spec, params or {}, fields=fields, limit=limit)

    def _fetch_dataset_spec(
        self,
        spec: Any,
        params: dict[str, Any],
        *,
        fields: list[str] | str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        params = _normalize_stock_dataset_params(params, input_fields=spec.input_fields)
        field_names = _sanitize_tushare_fields(fields, allowed_fields=spec.output_fields)
        if not field_names:
            field_names = list(spec.default_fields)
        if field_names:
            params["fields"] = ",".join(field_names)
        limit = _clamp_stock_dataset_limit(limit)
        missing_fields = []
        if spec.status != "active":
            missing_fields.append(f"dataset_status:{spec.status}")
        if self.pro is None:
            missing_fields.append("tushare:unavailable")
            return _stock_dataset_payload(
                spec,
                params=params,
                frame=pd.DataFrame(),
                row_count=0,
                data_source="unavailable",
                missing_fields=missing_fields,
            )
        try:
            frame = self._call_tushare_with_retry(
                lambda: _call_pro(self.pro, spec.api, **params),
                operation=spec.api,
            )
        except Exception as exc:
            return _stock_dataset_payload(
                spec,
                params=params,
                frame=pd.DataFrame(),
                row_count=0,
                data_source="unavailable",
                missing_fields=[*missing_fields, f"{spec.api}: {exc}"],
            )
        row_count = 0 if frame is None else len(frame)
        data = frame.head(limit).copy() if frame is not None and not frame.empty else pd.DataFrame()
        return _stock_dataset_payload(
            spec,
            params=params,
            frame=data,
            row_count=row_count,
            data_source=f"tushare_{spec.api}",
            missing_fields=missing_fields,
        )

    def _load_indicator_daily(
        self,
        symbols: list[str],
        start_date: str,
        trade_date: str,
        *,
        storage: DuckDBStorage,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        source = "tushare_daily"
        if self.pro is not None:
            for ts_code in symbols:
                try:
                    frame = self._call_tushare_with_retry(
                        lambda ts_code=ts_code: self.pro.daily(
                            ts_code=ts_code,
                            start_date=start_date,
                            end_date=trade_date,
                            fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg",
                        ),
                        operation="daily",
                    )
                except Exception as exc:
                    if not _is_tushare_fallback_error(exc):
                        raise
                    frame = pd.DataFrame()
                if frame is not None and not frame.empty:
                    frames.append(frame)
        data = _combine_daily_frames(frames)
        if data.empty:
            try:
                data = self._tickflow_provider().load_historical_daily_klines(
                    symbols,
                    start_date=start_date,
                    end_date=trade_date,
                    storage=storage,
                )
                source = "tickflow_daily"
            except Exception as exc:
                self._warn_fallback(f"指标日线使用 TickFlow 备份源失败，改用 DuckDB 缓存：{exc}")
                data = storage.get_stock_daily(self._recent_trade_dates(trade_date, count=lookback_count(start_date, trade_date)))
                source = "duckdb_cache_after_provider_failure"
        else:
            storage.upsert_stock_daily(data)
        data.attrs["data_source"] = source
        return data

    def _load_indicator_daily_basic(
        self,
        symbols: list[str],
        start_date: str,
        trade_date: str,
        *,
        storage: DuckDBStorage,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        source = "tushare_daily_basic"
        if self.pro is not None:
            for ts_code in symbols:
                try:
                    frame = self._call_tushare_with_retry(
                        lambda ts_code=ts_code: self.pro.daily_basic(
                            ts_code=ts_code,
                            start_date=start_date,
                            end_date=trade_date,
                            fields="ts_code,trade_date,turnover_rate,turnover_rate_f,pe,pb,ps,circ_mv,float_share,free_share,float_mv,total_mv",
                        ),
                        operation="daily_basic",
                    )
                except Exception as exc:
                    if not _is_tushare_fallback_error(exc):
                        raise
                    frame = pd.DataFrame()
                if frame is not None and not frame.empty:
                    frames.append(frame)
        data = _combine_daily_basic_frames(frames)
        if not data.empty:
            storage.upsert_stock_daily_basic(data)
        else:
            data = storage.get_stock_daily_basic(self._recent_trade_dates(trade_date, count=lookback_count(start_date, trade_date)))
            data = data[data["ts_code"].astype(str).isin(symbols)] if not data.empty else data
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _load_indicator_moneyflow(
        self,
        symbols: list[str],
        start_date: str,
        trade_date: str,
        *,
        storage: DuckDBStorage,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        source = "tushare_moneyflow_dc"
        if self.pro is not None:
            for ts_code in symbols:
                frame = _safe_dataframe_call(
                    lambda **params: _call_pro(self.pro, "moneyflow_dc", **params),
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=trade_date,
                )
                if frame.empty:
                    frame = _safe_dataframe_call(
                        lambda **params: _call_pro(self.pro, "moneyflow", **params),
                        ts_code=ts_code,
                        start_date=start_date,
                        end_date=trade_date,
                    )
                    source = "tushare_moneyflow"
                adapted = _adapt_moneyflow_frame(frame, source=source)
                if not adapted.empty:
                    frames.append(adapted)
        data = _combine_moneyflow_frames(frames)
        if not data.empty:
            storage.upsert_stock_moneyflow(data)
        else:
            data = storage.get_stock_moneyflow(symbols, start_date=start_date, end_date=trade_date)
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _load_indicator_fundamentals(
        self,
        symbols: list[str],
        trade_date: str,
        *,
        storage: DuckDBStorage,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        if self.pro is not None:
            for ts_code in symbols:
                frame = _build_fundamental_frame(self.pro, ts_code, trade_date)
                if not frame.empty:
                    frames.append(frame)
        data = _combine_fundamental_frames(frames)
        source = "tushare_fundamentals"
        if not data.empty:
            storage.upsert_stock_fundamentals(data)
        else:
            data = storage.get_stock_fundamentals(symbols, as_of=trade_date)
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _load_ths_sector_basic(self, *, storage: DuckDBStorage) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        if self.pro is not None:
            for sector_type, tushare_type in HOT_SECTOR_TYPES.items():
                frame = _safe_dataframe_call(lambda **params: _call_pro(self.pro, "ths_index", **params), type=tushare_type)
                adapted = _adapt_ths_sector_basic(frame, sector_type=sector_type)
                if not adapted.empty:
                    frames.append(adapted)
        data = _combine_sector_basic_frames(frames)
        source = HOT_SECTOR_DATA_SOURCE
        if not data.empty:
            storage.upsert_sector_basic(data)
        else:
            data = storage.get_sector_basic(sector_types=list(HOT_SECTOR_TYPES))
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _load_ths_sector_daily(
        self,
        sector_codes: list[str],
        trade_dates: list[str],
        *,
        storage: DuckDBStorage,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        start_date = trade_dates[0] if trade_dates else ""
        end_date = trade_dates[-1] if trade_dates else ""
        if self.pro is not None:
            for sector_code in sector_codes:
                frame = _safe_dataframe_call(
                    lambda **params: _call_pro(self.pro, "ths_daily", **params),
                    ts_code=sector_code,
                    start_date=start_date,
                    end_date=end_date,
                )
                adapted = _adapt_ths_sector_daily(frame, sector_code=sector_code)
                if not adapted.empty:
                    frames.append(adapted)
        data = _combine_sector_daily_frames(frames)
        source = HOT_SECTOR_DATA_SOURCE
        if not data.empty:
            storage.upsert_sector_daily(data)
        else:
            data = storage.get_sector_daily(sector_codes, trade_dates=trade_dates)
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _load_ths_sector_members(self, sector_codes: list[str], *, storage: DuckDBStorage) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        if self.pro is not None:
            for sector_code in sector_codes:
                frame = _safe_dataframe_call(lambda **params: _call_pro(self.pro, "ths_member", **params), ts_code=sector_code)
                adapted = _adapt_ths_sector_members(frame, sector_code=sector_code)
                if not adapted.empty:
                    frames.append(adapted)
        data = _combine_sector_member_frames(frames)
        source = HOT_SECTOR_DATA_SOURCE
        if not data.empty:
            storage.upsert_sector_members(data)
        else:
            data = storage.get_sector_members(sector_codes)
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _load_sw_sector_basic(self, *, storage: DuckDBStorage) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        if self.pro is not None:
            for sector_type, level in SW_SECTOR_LEVELS.items():
                frame = _safe_dataframe_call(
                    lambda **params: _call_pro(self.pro, "index_classify", **params),
                    src=SW_SECTOR_SRC,
                    level=level,
                )
                adapted = _adapt_sw_sector_basic(frame, sector_type=sector_type)
                if not adapted.empty:
                    frames.append(adapted)
        data = _combine_sector_basic_frames(frames)
        source = SW_SECTOR_BASIC_DATA_SOURCE
        if not data.empty:
            storage.upsert_sector_basic(data)
        else:
            data = storage.get_sector_basic(sector_types=list(SW_SECTOR_LEVELS))
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _load_sw_sector_members(self, sector_codes: list[str], *, storage: DuckDBStorage) -> pd.DataFrame:
        codes = [str(code).strip().upper() for code in sector_codes if str(code).strip()]
        frames: list[pd.DataFrame] = []
        if self.pro is not None and codes:
            basic = storage.get_sector_basic(sector_types=list(SW_SECTOR_LEVELS))
            type_by_code = {
                str(row.get("sector_code") or "").strip().upper(): str(row.get("sector_type") or "").strip()
                for _, row in basic.iterrows()
            } if isinstance(basic, pd.DataFrame) and not basic.empty else {}
            param_by_type = {"sw_l1": "l1_code", "sw_l2": "l2_code", "sw_l3": "l3_code"}
            for sector_code in codes:
                param_name = param_by_type.get(type_by_code.get(sector_code, ""))
                if not param_name:
                    continue
                frame = _safe_dataframe_call(
                    lambda **params: _call_pro(self.pro, "index_member_all", **params),
                    **{param_name: sector_code},
                )
                adapted = _adapt_sw_sector_members(frame, sector_code=sector_code)
                if not adapted.empty:
                    frames.append(adapted)
        data = _combine_sector_member_frames(frames)
        source = SW_SECTOR_MEMBER_DATA_SOURCE
        if not data.empty:
            storage.upsert_sector_members(data)
        else:
            data = storage.get_sector_members(codes)
            source = "duckdb_cache_or_unavailable"
        data.attrs["data_source"] = source
        return data

    def _stock_basic_frame(self, *, storage: DuckDBStorage | None = None) -> pd.DataFrame:
        errors: list[str] = []
        if self.pro is not None:
            try:
                frame = self._call_tushare_with_retry(
                    lambda: self.pro.stock_basic(
                        exchange="",
                        list_status="L",
                        fields="ts_code,symbol,name,industry,market,exchange,list_date",
                    ),
                    operation="stock_basic",
                )
            except Exception as exc:
                if not _is_tushare_fallback_error(exc):
                    raise
                errors.append(f"Tushare stock_basic: {exc}")
            else:
                data = _normalize_stock_basic_frame(frame)
                if not data.empty:
                    self._stock_basic_cache.update({str(row["ts_code"]): row.to_dict() for _, row in data.iterrows()})
                    return data
                errors.append("Tushare stock_basic returned empty data")
        elif getattr(self, "settings", None) is None or not getattr(self.settings, "tushare_token", ""):
            errors.append("TUSHARE_TOKEN is not configured")
        elif ts is None:
            errors.append("tushare is not installed")

        try:
            data = self._tickflow_provider().load_stock_basic(storage=storage)
        except Exception as exc:
            errors.append(f"TickFlow stock_basic: {exc}")
        else:
            data = _normalize_stock_basic_frame(data)
            if not data.empty:
                self._warn_fallback("股票池使用 TickFlow 备份源。")
                self._stock_basic_cache.update({str(row["ts_code"]): row.to_dict() for _, row in data.iterrows()})
                return data

        if storage is not None:
            data = _normalize_stock_basic_frame(storage.get_stock_basic())
            if not data.empty:
                self._warn_fallback("股票池使用 DuckDB 本地缓存，可能不含最新上市、退市或名称变化。")
                self._stock_basic_cache.update({str(row["ts_code"]): row.to_dict() for _, row in data.iterrows()})
                return data
        detail = "；".join(errors) if errors else "无可用数据源"
        raise ValueError(f"stock_basic 获取失败，且本地缓存为空：{detail}")

    def _stock_basic(self, ts_code: str) -> dict[str, Any]:
        if ts_code in self._stock_basic_cache:
            return self._stock_basic_cache[ts_code]
        if self.pro is None:
            return {}
        try:
            frame = self._call_tushare_with_retry(
                lambda: self.pro.stock_basic(ts_code=ts_code, fields="ts_code,symbol,name,industry,market,exchange,list_date"),
                operation="stock_basic",
            )
        except Exception:
            return {}
        if frame is None or frame.empty:
            return {}
        return frame.iloc[0].to_dict()

    def _call_tushare_with_retry(self, func, *, operation: str) -> pd.DataFrame:
        last_error: Exception | None = None
        attempts = max(0, int(_setting(self, "tushare_max_retries", 2))) + 1
        for _ in range(attempts):
            try:
                data = func()
                return data if isinstance(data, pd.DataFrame) else pd.DataFrame()
            except Exception as exc:
                if not _is_tushare_fallback_error(exc):
                    raise ValueError(f"Tushare {operation} 业务错误：{exc}") from exc
                last_error = exc
        raise RuntimeError(f"{operation} failed after {attempts} attempts: {last_error}") from last_error

    def _tickflow_provider(self) -> TickFlowDataProvider:
        return TickFlowDataProvider(self.settings)

    def _warn_fallback(self, message: str) -> None:
        warnings.warn(message, RuntimeWarning, stacklevel=2)

    def _recent_trade_dates(self, trade_date: str, *, count: int) -> list[str]:
        start_date = _start_date(trade_date, calendar_days=count * 3)
        if self.pro is None:
            return _fallback_weekday_dates(trade_date, count=count)
        try:
            frame = self._call_tushare_with_retry(
                lambda: self.pro.trade_cal(
                    exchange="",
                    start_date=start_date,
                    end_date=trade_date,
                    is_open="1",
                    fields="cal_date,is_open",
                ),
                operation="trade_cal",
            )
        except Exception:
            return _fallback_weekday_dates(trade_date, count=count)
        if frame is None or frame.empty:
            return _fallback_weekday_dates(trade_date, count=count)
        date_col = _first_column(frame, ["cal_date", "trade_date"])
        if not date_col:
            return _fallback_weekday_dates(trade_date, count=count)
        data = frame.copy()
        if "is_open" in data.columns:
            data = data[pd.to_numeric(data["is_open"], errors="coerce") == 1]
        dates = sorted({str(value) for value in data[date_col].dropna().tolist() if str(value) <= trade_date})
        return dates[-count:]

    def _ensure_stock_daily(
        self,
        storage: DuckDBStorage,
        trade_dates: list[str],
        *,
        stock_basic: pd.DataFrame,
        defer_dates: set[str] | None = None,
    ) -> None:
        defer_dates = defer_dates or set()
        cached = storage.cached_trade_dates("stock_daily", trade_dates)
        fallback_dates: list[str] = []
        symbols = _stock_basic_symbols(stock_basic)
        for trade_date in trade_dates:
            if trade_date in cached:
                continue
            frame = pd.DataFrame()
            if self.pro is not None:
                try:
                    frame = self._call_tushare_with_retry(
                        lambda: self.pro.daily(
                            trade_date=trade_date,
                            fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg",
                        ),
                        operation="daily",
                    )
                except Exception as exc:
                    if not _is_tushare_fallback_error(exc):
                        raise
                    frame = pd.DataFrame()
                _warn_near_limit("daily", trade_date, frame)
                storage.upsert_stock_daily(frame if frame is not None else pd.DataFrame())
            if (frame is None or frame.empty) and trade_date not in defer_dates:
                fallback_dates.append(trade_date)
        if fallback_dates:
            self._fill_stock_daily_from_tickflow_or_cache(
                storage=storage,
                symbols=symbols,
                trade_dates=fallback_dates,
            )

    def _ensure_stock_daily_basic(
        self,
        storage: DuckDBStorage,
        trade_dates: list[str],
        *,
        defer_dates: set[str] | None = None,
    ) -> None:
        defer_dates = defer_dates or set()
        cached = storage.cached_trade_dates("stock_daily_basic", trade_dates)
        for trade_date in trade_dates:
            if trade_date in cached:
                continue
            frame = pd.DataFrame()
            if self.pro is not None:
                try:
                    frame = self._call_tushare_with_retry(
                        lambda: self.pro.daily_basic(
                            trade_date=trade_date,
                            fields="ts_code,trade_date,turnover_rate,turnover_rate_f,pe,pb,ps,circ_mv,float_share,free_share,float_mv,total_mv",
                        ),
                        operation="daily_basic",
                    )
                except Exception as exc:
                    if not _is_tushare_fallback_error(exc):
                        raise
                    frame = pd.DataFrame()
                _warn_near_limit("daily_basic", trade_date, frame)
                storage.upsert_stock_daily_basic(frame if frame is not None else pd.DataFrame())
            if (frame is None or frame.empty) and trade_date not in defer_dates:
                self._warn_fallback(f"{trade_date} daily_basic 使用本地缓存兜底；缓存缺失时依赖该字段的规则会失败。")

    def _ensure_current_trade_date_data(
        self,
        storage: DuckDBStorage,
        trade_date: str,
        stock_basic: pd.DataFrame,
        *,
        force_realtime: bool = False,
        require_daily_basic: bool = True,
        previous_trade_date: str | None = None,
    ) -> dict[str, Any]:
        universe_count = len(stock_basic)
        symbols = _stock_basic_symbols(stock_basic)
        if force_realtime:
            realtime_daily, source = self._fetch_realtime_daily_with_fallback(
                storage=storage,
                trade_date=trade_date,
                symbols=symbols,
                forced=True,
                previous_trade_date=previous_trade_date,
            )
            daily_missing = _is_obviously_missing(realtime_daily, universe_count)
            if daily_missing:
                raise ValueError(
                    f"{trade_date} 交易时段强制实时筛选，但 daily 实时数据不可用，"
                    "已停止筛选，未使用缓存或前一交易日数据。"
                )
            realtime_basic = None
            basic_missing = True
            if require_daily_basic:
                realtime_basic = self._build_realtime_daily_basic(storage, trade_date, realtime_daily)
                basic_missing = _is_obviously_missing(realtime_basic, universe_count)
            daily_basic_source = _daily_basic_source_from_frame(
                realtime_basic,
                default="missing",
                skipped=not require_daily_basic,
            )
            return {
                "source": source,
                "daily_basic_source": daily_basic_source,
                "realtime_daily": realtime_daily,
                "realtime_basic": None if basic_missing else realtime_basic,
            }

        today_daily = storage.get_stock_daily([trade_date])
        today_basic = storage.get_stock_daily_basic([trade_date]) if require_daily_basic else pd.DataFrame()
        source = "tushare_daily"
        daily_basic_source = (
            "tushare_daily_basic"
            if require_daily_basic and not _is_obviously_missing(today_basic, universe_count)
            else "missing"
            if require_daily_basic
            else "skipped_for_chan_third_buy"
        )

        if _is_obviously_missing(today_daily, universe_count):
            today_daily, source = self._refresh_stock_daily(
                storage,
                trade_date,
                symbols=symbols,
                allow_empty=_is_today_shanghai(trade_date),
            )
        if require_daily_basic and _is_obviously_missing(today_basic, universe_count):
            today_basic, daily_basic_source = self._refresh_stock_daily_basic(
                storage,
                trade_date,
                symbols=symbols,
                daily_frame=today_daily,
            )

        daily_missing = _is_obviously_missing(today_daily, universe_count)
        basic_missing = require_daily_basic and _is_obviously_missing(today_basic, universe_count)
        if not daily_missing:
            basic_overlay = (
                today_basic
                if daily_basic_source in {"tickflow_realtime_basic_like", "synthetic_realtime_basic"}
                else None
            )
            return {
                "source": source,
                "daily_basic_source": daily_basic_source if not basic_missing else "missing",
                "realtime_daily": None,
                "realtime_basic": basic_overlay,
            }

        if not _is_today_shanghai(trade_date):
            raise ValueError(f"{trade_date} 当日 daily 尚不可用，已停止筛选，未使用前一交易日数据。")

        realtime_daily = None
        if daily_missing:
            realtime_daily, source = self._fetch_realtime_daily_with_fallback(
                storage=storage,
                trade_date=trade_date,
                symbols=symbols,
                forced=False,
                previous_trade_date=previous_trade_date,
            )
            today_daily = realtime_daily
            daily_missing = _is_obviously_missing(today_daily, universe_count)

        realtime_basic = None
        if require_daily_basic and basic_missing:
            source_daily = realtime_daily if realtime_daily is not None else today_daily
            realtime_basic = self._build_realtime_daily_basic(storage, trade_date, source_daily)
            today_basic = realtime_basic
            basic_missing = _is_obviously_missing(today_basic, universe_count)
            daily_basic_source = _daily_basic_source_from_frame(
                realtime_basic,
                default="missing",
                skipped=not require_daily_basic,
            )

        if daily_missing:
            raise ValueError(
                f"{trade_date} 当日 daily 尚不可用，TickFlow/本地缓存均无法提供请求交易日数据，"
                "已停止筛选，未使用前一交易日数据。"
            )
        return {
            "source": source,
            "daily_basic_source": daily_basic_source if require_daily_basic and not basic_missing else "missing" if require_daily_basic else "skipped_for_chan_third_buy",
            "realtime_daily": realtime_daily,
            "realtime_basic": None if basic_missing else realtime_basic,
        }

    def _refresh_stock_daily(
        self,
        storage: DuckDBStorage,
        trade_date: str,
        *,
        symbols: list[str],
        allow_empty: bool = False,
    ) -> tuple[pd.DataFrame, str]:
        if self.pro is not None:
            try:
                frame = self._call_tushare_with_retry(
                    lambda: self.pro.daily(
                        trade_date=trade_date,
                        fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg",
                    ),
                    operation="daily",
                )
            except Exception as exc:
                if not _is_tushare_fallback_error(exc):
                    raise
                frame = pd.DataFrame()
            _warn_near_limit("daily", trade_date, frame)
            storage.upsert_stock_daily(frame if frame is not None else pd.DataFrame())
            cached = storage.get_stock_daily([trade_date])
            if not cached.empty:
                return cached, "tushare_daily"
        if allow_empty:
            return storage.get_stock_daily([trade_date]), "tushare_daily"
        source = self._fill_stock_daily_from_tickflow_or_cache(
            storage=storage,
            symbols=symbols,
            trade_dates=[trade_date],
        )
        cached = storage.get_stock_daily([trade_date])
        return cached, source

    def _refresh_stock_daily_basic(
        self,
        storage: DuckDBStorage,
        trade_date: str,
        *,
        symbols: list[str],
        daily_frame: pd.DataFrame,
    ) -> tuple[pd.DataFrame, str]:
        if self.pro is not None:
            try:
                frame = self._call_tushare_with_retry(
                    lambda: self.pro.daily_basic(
                        trade_date=trade_date,
                        fields="ts_code,trade_date,turnover_rate,turnover_rate_f,pe,pb,ps,circ_mv,float_share,free_share,float_mv,total_mv",
                    ),
                    operation="daily_basic",
                )
            except Exception as exc:
                if not _is_tushare_fallback_error(exc):
                    raise
                frame = pd.DataFrame()
            _warn_near_limit("daily_basic", trade_date, frame)
            storage.upsert_stock_daily_basic(frame if frame is not None else pd.DataFrame())
        cached = storage.get_stock_daily_basic([trade_date])
        if cached.empty:
            if _is_today_shanghai(trade_date) and daily_frame is not None and not daily_frame.empty:
                realtime_basic = self._build_realtime_daily_basic(storage, trade_date, daily_frame)
                if not realtime_basic.empty:
                    return realtime_basic, _daily_basic_source_from_frame(realtime_basic, default="synthetic_realtime_basic")
            self._warn_fallback(f"{trade_date} daily_basic 无法从 Tushare 获取，且本地同交易日缓存为空。")
            return cached, "missing"
        return cached, "tushare_daily_basic"

    def _fill_stock_daily_from_tickflow_or_cache(
        self,
        *,
        storage: DuckDBStorage,
        symbols: list[str],
        trade_dates: list[str],
    ) -> str:
        dates = sorted({str(date) for date in trade_dates if str(date).strip()})
        if not dates:
            return "duckdb_cache_after_provider_failure"
        try:
            self._tickflow_provider().load_historical_daily_klines(
                symbols,
                start_date=_days_before(dates[0], 10),
                end_date=dates[-1],
                storage=storage,
            )
        except Exception as exc:
            self._warn_fallback(f"TickFlow 日线备份源获取失败，改用本地同交易日缓存：{exc}")
        else:
            cached = storage.cached_trade_dates("stock_daily", dates)
            if all(date in cached for date in dates):
                self._warn_fallback("日线行情使用 TickFlow 备份源。")
                return "tickflow_daily"
        cached = storage.cached_trade_dates("stock_daily", dates)
        missing = [date for date in dates if date not in cached]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} stock_daily 获取失败，TickFlow 备份源和本地同交易日缓存均不可用，"
                "已停止筛选，未使用前一交易日数据。"
            )
        self._warn_fallback("日线行情使用 DuckDB 本地同交易日缓存。")
        return "duckdb_cache_after_provider_failure"

    def _fetch_realtime_daily_with_fallback(
        self,
        *,
        storage: DuckDBStorage,
        trade_date: str,
        symbols: list[str],
        forced: bool,
        previous_trade_date: str | None = None,
    ) -> tuple[pd.DataFrame, str]:
        try:
            frame = self._tickflow_provider().load_realtime_daily_quotes(symbols, trade_date=trade_date)
        except Exception as exc:
            if forced:
                raise ValueError(f"{trade_date} 交易时段当日日K批量获取失败，已停止筛选：{exc}") from exc
            self._warn_fallback(
                f"TickFlow 当日日K获取失败，尝试本地同交易日缓存：{exc}"
            )
        else:
            if not frame.empty:
                previous_daily = (
                    storage.get_stock_daily([previous_trade_date])
                    if previous_trade_date
                    else pd.DataFrame()
                )
                frame = _with_previous_close_pct_chg(frame, previous_daily)
                self._warn_fallback("实时日线使用 TickFlow 当日 1d K线。")
                return frame, "tickflow_current_1d"
        if forced:
            raise ValueError(f"{trade_date} 交易时段当日日K批量返回空数据，已停止筛选。")
        cached = storage.get_stock_daily([trade_date])
        if not cached.empty:
            self._warn_fallback("实时日线使用 DuckDB 本地同交易日缓存。")
            return cached, "duckdb_cache_after_provider_failure"
        return pd.DataFrame(), "missing"

    def _build_realtime_daily_basic(
        self,
        storage: DuckDBStorage,
        trade_date: str,
        today_daily: pd.DataFrame,
    ) -> pd.DataFrame:
        if self.pro is None:
            premarket = pd.DataFrame()
        else:
            try:
                premarket = _call_pro(
                    self.pro,
                    "stk_premarket",
                    trade_date=trade_date,
                    fields="trade_date,ts_code,float_share",
                )
            except Exception:
                premarket = pd.DataFrame()
        share_lookup = _share_lookup(premarket)
        if not share_lookup:
            historical = storage.get_stock_daily_basic(
                [date for date in self._recent_trade_dates(trade_date, count=10) if date < trade_date]
            )
            share_lookup = _latest_share_lookup(historical)
        if share_lookup:
            return _realtime_daily_basic_from_daily(
                today_daily,
                trade_date,
                share_lookup,
                source="synthetic_realtime_basic",
            )
        symbols = _stock_basic_symbols(today_daily)
        try:
            frame = self._tickflow_provider().load_realtime_daily_basic_like(
                symbols,
                trade_date=trade_date,
                daily_frame=today_daily,
            )
        except Exception as exc:
            self._warn_fallback(f"TickFlow daily_basic-like 实时替代失败：{exc}")
            return pd.DataFrame()
        return frame

    def _build_rule_intraday_metadata(
        self,
        *,
        rule: Any,
        stock_basic: pd.DataFrame,
        daily_groups: dict[str, pd.DataFrame],
        trade_date: str,
        use_realtime: bool,
        data_source: str,
    ) -> dict[str, dict[str, Any]]:
        symbols = _stock_basic_symbols(stock_basic)
        metadata = {ts_code: {} for ts_code in symbols}
        inputs: dict[str, ScreeningInput] = {}
        for _, row in stock_basic.iterrows():
            ts_code = str(row["ts_code"])
            inputs[ts_code] = ScreeningInput(
                ts_code=ts_code,
                trade_date=trade_date,
                daily=daily_groups.get(ts_code, pd.DataFrame()),
                daily_basic=pd.DataFrame(),
                stock_basic=row.dropna().to_dict(),
                metadata={"data_source": data_source},
            )

        provider = self._tickflow_provider()
        for requirement in rule.intraday_kline_requirements:
            candidates: list[str] = []
            for ts_code, item in inputs.items():
                labels = list(rule.intraday_candidate_labels(item, requirement))
                if labels:
                    candidates.append(ts_code)
                metadata[ts_code][requirement.metadata_key] = pd.DataFrame()
                metadata[ts_code][requirement.source_metadata_key] = ""
                if requirement.candidate_metadata_key:
                    metadata[ts_code][requirement.candidate_metadata_key] = labels
            if not candidates:
                continue

            history = pd.DataFrame()
            if requirement.history_calendar_days > 0:
                history = provider.load_historical_minute_klines(
                    candidates,
                    period=requirement.period,
                    start_time=_days_before(trade_date, requirement.history_calendar_days),
                    end_time=_days_before(trade_date, 1) if use_realtime else trade_date,
                )
            current = pd.DataFrame()
            source = "tickflow_history"
            if use_realtime:
                try:
                    current = provider.load_current_klines(
                        candidates,
                        period=requirement.period,
                        trade_date=trade_date,
                        count=requirement.count,
                    )
                except Exception as exc:
                    raise ValueError(
                        f"{trade_date} {rule.name} 当日 {requirement.period} K线获取失败：{exc}"
                    ) from exc
                if _current_kline_coverage_missing(current, candidates, trade_date):
                    raise ValueError(
                        f"{trade_date} {rule.name} 当日 {requirement.period} K线覆盖不足，已停止筛选。"
                    )
                source = f"tickflow_current_{requirement.period}"

            combined = _combine_minute_frames([history, current]) if use_realtime else history
            groups = _group_by_ts_code(combined)
            for ts_code in candidates:
                frame = groups.get(ts_code, pd.DataFrame())
                metadata[ts_code][requirement.metadata_key] = frame
                if not frame.empty:
                    metadata[ts_code][requirement.source_metadata_key] = source
        return metadata

    def _build_monthly_base_breakout_metadata(
        self,
        *,
        stock_basic: pd.DataFrame,
        trade_date: str,
        daily_groups: dict[str, pd.DataFrame],
        use_realtime: bool = False,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        symbols = _stock_basic_symbols(stock_basic)
        frames: dict[str, pd.DataFrame] = {symbol: pd.DataFrame() for symbol in symbols}
        sources: dict[str, str] = {symbol: "unavailable" for symbol in symbols}
        if not symbols:
            return frames, sources

        start_date = _start_date(trade_date, calendar_days=MONTHLY_BASE_BREAKOUT_DAILY_CALENDAR_DAYS)
        monthly_frame = pd.DataFrame()
        try:
            monthly_frame = self._tickflow_provider().load_klines(
                symbols,
                period=MONTHLY_BASE_BREAKOUT_PERIOD,
                start_time=start_date,
                end_time=trade_date,
                count=MONTHLY_BASE_BREAKOUT_MONTH_COUNT,
            )
        except Exception as exc:
            self._warn_fallback(f"TickFlow 月K获取失败，尝试日线聚合：{exc}")

        monthly_groups = _group_by_ts_code(monthly_frame)
        missing: list[str] = []
        for ts_code in symbols:
            monthly = monthly_groups.get(ts_code, pd.DataFrame())
            if len(monthly) >= 60:
                frames[ts_code] = monthly
                sources[ts_code] = "tickflow_1M"
            else:
                missing.append(ts_code)

        if not missing:
            return frames, sources

        daily_frame = pd.DataFrame()
        try:
            daily_frame = self._tickflow_provider().load_historical_daily_klines(
                missing,
                start_date=start_date,
                end_date=trade_date,
            )
        except Exception as exc:
            self._warn_fallback(f"TickFlow 日线聚合月K失败，尝试筛选日线窗口：{exc}")

        fallback_daily_groups = _group_by_ts_code(daily_frame)
        for ts_code in missing:
            daily = fallback_daily_groups.get(ts_code, pd.DataFrame())
            source = "daily_aggregation:tickflow_daily"
            screening_daily = daily_groups.get(ts_code, pd.DataFrame())
            if use_realtime and not screening_daily.empty:
                current = screening_daily[screening_daily["trade_date"].astype(str) == str(trade_date)]
                if not current.empty:
                    daily = _replace_trade_date_rows(daily, current, trade_date)
                    source = "daily_aggregation:tickflow_daily+current_1d"
            if daily.empty:
                daily = screening_daily
                source = "daily_aggregation:screening_daily"
            monthly = _daily_to_monthly_frame(daily, trade_date=trade_date)
            if not monthly.empty:
                frames[ts_code] = monthly
                sources[ts_code] = source
        return frames, sources

    def _build_price_volume_ma_metadata(
        self,
        *,
        stock_basic: pd.DataFrame,
        daily_frame: pd.DataFrame,
        daily_basic_frame: pd.DataFrame,
        trade_date: str,
        force_realtime: bool = False,
    ) -> dict[str, dict[str, Any]]:
        metadata = {
            str(row["ts_code"]): _empty_price_volume_ma_metadata(str(row["ts_code"]))
            for _, row in stock_basic.iterrows()
            if str(row.get("ts_code") or "").strip()
        }
        if stock_basic.empty or daily_frame.empty or daily_basic_frame.empty:
            return metadata

        stock_lookup = {
            str(row["ts_code"]): row.dropna().to_dict()
            for _, row in stock_basic.iterrows()
            if str(row.get("ts_code") or "").strip()
        }
        daily_groups = _group_by_ts_code(daily_frame)
        daily_basic_groups = _group_by_ts_code(daily_basic_frame)
        today_daily_lookup = _latest_rows_by_ts_code(daily_frame, trade_date)
        today_basic_lookup = _latest_rows_by_ts_code(daily_basic_frame, trade_date)
        trade_dates = sorted(
            {
                str(value)
                for value in daily_frame.get("trade_date", pd.Series(dtype=str)).dropna().tolist()
                if str(value) <= str(trade_date)
            }
        )
        recent_6_dates = trade_dates[-6:]
        recent_dates = trade_dates[-61:]
        min_list_date = recent_dates[0] if recent_dates else str(trade_date)

        current_selected, current_errors, current_stats = self._price_volume_current_logic(
            stock_lookup=stock_lookup,
            daily_groups=daily_groups,
            today_daily_lookup=today_daily_lookup,
            today_basic_lookup=today_basic_lookup,
            trade_date=trade_date,
            min_list_date=min_list_date,
            recent_6_dates=recent_6_dates,
            force_realtime=force_realtime,
        )
        legacy_selected = self._price_volume_legacy_logic(
            stock_lookup=stock_lookup,
            daily_groups=daily_groups,
            today_daily_lookup=today_daily_lookup,
            today_basic_lookup=today_basic_lookup,
            trade_date=trade_date,
            force_realtime=force_realtime,
        )

        for ts_code, payload in current_selected.items():
            item = metadata.setdefault(ts_code, _empty_price_volume_ma_metadata(ts_code))
            item["selected"] = True
            item["current_logic_passed"] = True
            item["raw_metrics"]["current"] = payload
        for ts_code, payload in current_errors.items():
            item = metadata.setdefault(ts_code, _empty_price_volume_ma_metadata(ts_code))
            item["raw_metrics"]["current_ma_error"] = payload
        for ts_code, payload in legacy_selected.items():
            item = metadata.setdefault(ts_code, _empty_price_volume_ma_metadata(ts_code))
            item["selected"] = True
            item["legacy_logic_passed"] = True
            item["raw_metrics"]["legacy"] = payload
        if (
            current_stats["current_ma_candidate_count"] > 0
            and current_stats["current_ma_error_count"] == current_stats["current_ma_candidate_count"]
        ):
            summary = {
                "current_ma_candidate_count": current_stats["current_ma_candidate_count"],
                "current_ma_error_count": current_stats["current_ma_error_count"],
                "message": "all current logic pro_bar MA calls failed",
            }
            for item in metadata.values():
                item["raw_metrics"]["current_scan_error_summary"] = summary
        for item in metadata.values():
            current = bool(item["current_logic_passed"])
            legacy = bool(item["legacy_logic_passed"])
            if current and legacy:
                item["selection_source"] = "current+legacy"
            elif current:
                item["selection_source"] = "current"
            elif legacy:
                item["selection_source"] = "legacy"
            else:
                item["selection_source"] = ""
        return metadata

    def _price_volume_current_logic(
        self,
        *,
        stock_lookup: dict[str, dict[str, Any]],
        daily_groups: dict[str, pd.DataFrame],
        today_daily_lookup: dict[str, dict[str, Any]],
        today_basic_lookup: dict[str, dict[str, Any]],
        trade_date: str,
        min_list_date: str,
        recent_6_dates: list[str],
        force_realtime: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
        selected: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, Any]] = {}
        stats = {"current_ma_candidate_count": 0, "current_ma_error_count": 0}
        for ts_code, stock_info in stock_lookup.items():
            if _is_st_stock_info(stock_info) or _is_bse_stock_info(ts_code, stock_info):
                continue
            list_date = str(stock_info.get("list_date") or "")
            if list_date and list_date > min_list_date:
                continue
            today_daily = today_daily_lookup.get(ts_code)
            today_basic = today_basic_lookup.get(ts_code)
            history = daily_groups.get(ts_code, pd.DataFrame())
            if today_daily is None or today_basic is None or history.empty:
                continue
            history = history[history["trade_date"].astype(str) <= str(trade_date)].sort_values("trade_date")
            recent_history = history[history["trade_date"].astype(str).isin(recent_6_dates)].sort_values("trade_date")
            if len(recent_history) < 6:
                continue

            pct_chg = _num(today_daily.get("pct_chg"))
            volume_ratio = _history_volume_ratio(recent_history)
            turnover_rate = _resolve_price_volume_turnover(today_basic)
            circ_mv = _num(today_basic.get("circ_mv"))
            if not _price_volume_pre_ma_filters_pass(pct_chg, volume_ratio, turnover_rate, circ_mv):
                continue

            stats["current_ma_candidate_count"] += 1
            if force_realtime:
                ma_metrics = _ma_stack_on_trade_date(history, trade_date)
            else:
                ma_metrics = self._price_volume_current_ma(ts_code, trade_date)
            if ma_metrics.get("error"):
                stats["current_ma_error_count"] += 1
                errors[ts_code] = {
                    "pct_chg": pct_chg,
                    "volume_ratio": volume_ratio,
                    "turnover_rate": turnover_rate,
                    "circ_mv": circ_mv,
                    "error": str(ma_metrics.get("error")),
                }
                continue
            if not ma_metrics.get("passed"):
                continue
            selected[ts_code] = {
                "pct_chg": pct_chg,
                "volume_ratio": volume_ratio,
                "turnover_rate": turnover_rate,
                "circ_mv": circ_mv,
                **ma_metrics,
            }
        return selected, errors, stats

    def _price_volume_legacy_logic(
        self,
        *,
        stock_lookup: dict[str, dict[str, Any]],
        daily_groups: dict[str, pd.DataFrame],
        today_daily_lookup: dict[str, dict[str, Any]],
        today_basic_lookup: dict[str, dict[str, Any]],
        trade_date: str,
        force_realtime: bool = False,
    ) -> dict[str, dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for ts_code, stock_info in stock_lookup.items():
            if _is_bse_stock_info(ts_code, stock_info):
                continue
            today_daily = today_daily_lookup.get(ts_code)
            today_basic = today_basic_lookup.get(ts_code)
            if today_daily is None or today_basic is None:
                continue
            pct_chg = _num(today_daily.get("pct_chg"))
            turnover_rate = _num(today_basic.get("turnover_rate"))
            circ_mv = _num(today_basic.get("circ_mv"))
            if not (
                PRICE_VOLUME_PCT_CHG_RANGE[0] <= pct_chg <= PRICE_VOLUME_PCT_CHG_RANGE[1]
                and PRICE_VOLUME_TURNOVER_RANGE[0] <= turnover_rate <= PRICE_VOLUME_TURNOVER_RANGE[1]
                and PRICE_VOLUME_CIRC_MV_RANGE[0] <= circ_mv <= PRICE_VOLUME_CIRC_MV_RANGE[1]
            ):
                continue
            if force_realtime:
                history = daily_groups.get(ts_code, pd.DataFrame())
                if not history.empty:
                    history = history[history["trade_date"].astype(str) <= str(trade_date)].sort_values("trade_date")
            else:
                history = self._price_volume_legacy_history(ts_code, trade_date)
            if history.empty:
                continue
            prev_5_avg_volume, volume_ratio = _legacy_volume_metrics(history, trade_date)
            if volume_ratio <= 1.0:
                continue
            ma_metrics = _ma_stack_on_trade_date(history, trade_date)
            if not ma_metrics.get("passed"):
                continue
            selected[ts_code] = {
                "pct_chg": pct_chg,
                "volume_ratio": volume_ratio,
                "prev_5_avg_volume": prev_5_avg_volume,
                "turnover_rate": turnover_rate,
                "circ_mv": circ_mv,
                **ma_metrics,
            }
        return selected

    def _price_volume_current_ma(self, ts_code: str, trade_date: str) -> dict[str, Any]:
        if self.pro is None:
            return {"passed": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0, "ma60": 0.0, "error": "tushare unavailable"}
        try:
            frame = _tushare_pro_bar(
                self.pro,
                ts_code=ts_code,
                start_date=_one_year_start_date(trade_date),
                end_date=trade_date,
                adj="qfq",
                ma=[5, 10, 20, 60],
            )
        except Exception as exc:
            return {"passed": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0, "ma60": 0.0, "error": str(exc)}
        if frame.empty:
            return {"passed": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0, "ma60": 0.0}
        return _ma_stack_latest(frame)

    def _price_volume_legacy_history(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        if self.pro is None:
            return pd.DataFrame()
        return _safe_dataframe_call(
            self.pro.daily,
            ts_code=ts_code,
            start_date=_days_before(trade_date, 120),
            end_date=trade_date,
        )


def _normalize_stock_basic_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return pd.DataFrame()
    data = frame.copy()
    for column in ["symbol", "name", "industry", "market", "exchange", "list_date"]:
        if column not in data.columns:
            data[column] = ""
    data["ts_code"] = data["ts_code"].astype(str).str.strip()
    data = data[data["ts_code"].map(_is_a_share_code)]
    return data.drop_duplicates(subset=["ts_code"], keep="last").sort_values("ts_code").reset_index(drop=True)


def _stock_basic_symbols(frame: pd.DataFrame) -> list[str]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return []
    return sorted({str(value).strip() for value in frame["ts_code"].dropna().tolist() if str(value).strip()})


def _setting(provider: TushareDataProvider, name: str, default: int) -> int:
    settings = getattr(provider, "settings", None)
    if settings is None:
        return default
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _is_tushare_fallback_error(exc: Exception) -> bool:
    message = str(exc).lower()
    business_markers = ["token", "permission", "权限", "denied", "invalid", "参数", "积分"]
    if any(marker in message for marker in business_markers):
        return False
    retry_markers = [
        "timeout",
        "timed out",
        "connection",
        "remote disconnected",
        "max retries",
        "temporarily",
        "429",
        "500",
        "502",
        "503",
        "504",
    ]
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(marker in message for marker in retry_markers)


def _start_date(trade_date: str, *, calendar_days: int) -> str:
    dt = datetime.strptime(trade_date, "%Y%m%d")
    return (dt - timedelta(days=calendar_days)).strftime("%Y%m%d")


def _is_a_share_code(ts_code: str) -> bool:
    return ts_code.endswith((".SH", ".SZ", ".BJ"))


def _fallback_weekday_dates(trade_date: str, *, count: int) -> list[str]:
    end = datetime.strptime(trade_date, "%Y%m%d")
    dates = []
    cursor = end
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return sorted(dates)


def _combine_minute_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    data = pd.concat(valid, ignore_index=True, sort=False)
    if {"ts_code", "period", "trade_time"}.issubset(data.columns):
        data = data.drop_duplicates(subset=["ts_code", "period", "trade_time"], keep="last")
    sort_columns = [column for column in ["ts_code", "trade_time", "trade_date"] if column in data.columns]
    return data.sort_values(sort_columns).reset_index(drop=True) if sort_columns else data.reset_index(drop=True)


def _combine_daily_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    columns = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
    if not valid:
        return pd.DataFrame(columns=columns)
    data = pd.concat(valid, ignore_index=True, sort=False)
    for column in columns:
        if column not in data.columns:
            data[column] = None
    data["ts_code"] = data["ts_code"].astype(str).map(_normalize_ts_code)
    data["trade_date"] = data["trade_date"].astype(str)
    return data[columns].drop_duplicates(subset=["ts_code", "trade_date"], keep="last").sort_values(
        ["ts_code", "trade_date"]
    ).reset_index(drop=True)


def _daily_to_monthly_frame(frame: pd.DataFrame | None, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    required = ["trade_date", "open", "high", "low", "close"]
    for column in required:
        if column not in data.columns:
            return pd.DataFrame()
    if "vol" not in data.columns:
        data["vol"] = data["volume"] if "volume" in data.columns else 0.0
    if "amount" not in data.columns:
        data["amount"] = 0.0
    data["trade_date"] = data["trade_date"].astype(str)
    data = data[data["trade_date"] <= str(trade_date)]
    for column in ["open", "high", "low", "close", "vol", "amount"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["trade_date", "open", "high", "low", "close"])
    if data.empty:
        return pd.DataFrame()
    data = data.sort_values("trade_date").reset_index(drop=True)
    month_key = pd.to_datetime(data["trade_date"], format="%Y%m%d", errors="coerce").dt.to_period("M")
    data = data[month_key.notna()].copy()
    if data.empty:
        return pd.DataFrame()
    data["_month"] = month_key[month_key.notna()].astype(str).to_numpy()
    rows = []
    for _, group in data.groupby("_month", sort=True):
        ordered = group.sort_values("trade_date")
        rows.append(
            {
                "ts_code": str(ordered.iloc[-1].get("ts_code") or ""),
                "period": MONTHLY_BASE_BREAKOUT_PERIOD,
                "trade_date": str(ordered.iloc[-1]["trade_date"]),
                "open": _num(ordered.iloc[0].get("open")),
                "high": _num(ordered["high"].max()),
                "low": _num(ordered["low"].min()),
                "close": _num(ordered.iloc[-1].get("close")),
                "vol": _num(ordered["vol"].sum()),
                "amount": _num(ordered["amount"].sum()),
            }
        )
    monthly = pd.DataFrame(rows)
    if monthly.empty:
        return monthly
    close = pd.to_numeric(monthly["close"], errors="coerce")
    monthly["pct_chg"] = close.pct_change().fillna(0.0) * 100.0
    return monthly.reset_index(drop=True)


def _combine_daily_basic_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    columns = [
        "ts_code",
        "trade_date",
        "turnover_rate",
        "turnover_rate_f",
        "circ_mv",
        "float_share",
        "free_share",
        "float_mv",
        "total_mv",
        "pe",
        "pb",
        "ps",
    ]
    if not valid:
        return pd.DataFrame(columns=columns)
    data = pd.concat(valid, ignore_index=True, sort=False)
    for column in columns:
        if column not in data.columns:
            data[column] = None
    data["ts_code"] = data["ts_code"].astype(str).map(_normalize_ts_code)
    data["trade_date"] = data["trade_date"].astype(str)
    return data[columns].drop_duplicates(subset=["ts_code", "trade_date"], keep="last").sort_values(
        ["ts_code", "trade_date"]
    ).reset_index(drop=True)


def _adapt_moneyflow_frame(frame: pd.DataFrame | None, *, source: str) -> pd.DataFrame:
    columns = ["ts_code", "trade_date", "main_net_amount", "data_source"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    code_col = _first_column(data, ["ts_code", "code", "symbol"])
    date_col = _first_column(data, ["trade_date", "date"])
    amount_col = _first_column(
        data,
        ["main_net_amount", "main_net_inflow", "net_mf_amount", "net_amount", "buy_sm_amount"],
    )
    if code_col is None or date_col is None or amount_col is None:
        return pd.DataFrame(columns=columns)
    result = pd.DataFrame(
        {
            "ts_code": data[code_col].astype(str).map(_normalize_ts_code),
            "trade_date": data[date_col].astype(str).str.replace("-", "", regex=False).str[:8],
            "main_net_amount": pd.to_numeric(data[amount_col], errors="coerce"),
            "data_source": source,
        }
    )
    return result.dropna(subset=["ts_code", "trade_date"]).drop_duplicates(
        subset=["ts_code", "trade_date"], keep="last"
    ).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _combine_moneyflow_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    columns = ["ts_code", "trade_date", "main_net_amount", "data_source"]
    if not valid:
        return pd.DataFrame(columns=columns)
    return pd.concat(valid, ignore_index=True, sort=False)[columns].drop_duplicates(
        subset=["ts_code", "trade_date"], keep="last"
    ).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _adapt_ths_sector_basic(frame: pd.DataFrame | None, *, sector_type: str) -> pd.DataFrame:
    columns = ["sector_code", "name", "sector_type", "exchange", "list_date", "data_source"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    code_col = _first_column(data, ["ts_code", "index_code", "code"])
    name_col = _first_column(data, ["name", "index_name"])
    if code_col is None:
        return pd.DataFrame(columns=columns)
    result = pd.DataFrame(
        {
            "sector_code": data[code_col].astype(str).str.strip().str.upper(),
            "name": data[name_col].astype(str).str.strip() if name_col else "",
            "sector_type": sector_type,
            "exchange": data[_first_column(data, ["exchange", "market"])].astype(str).str.strip()
            if _first_column(data, ["exchange", "market"])
            else "",
            "list_date": data[_first_column(data, ["list_date", "base_date"])].astype(str).str.strip()
            if _first_column(data, ["list_date", "base_date"])
            else "",
            "data_source": HOT_SECTOR_DATA_SOURCE,
        }
    )
    return result[result["sector_code"] != ""].drop_duplicates(subset=["sector_code"], keep="last").reset_index(drop=True)


def _adapt_sw_sector_basic(frame: pd.DataFrame | None, *, sector_type: str) -> pd.DataFrame:
    columns = ["sector_code", "name", "sector_type", "exchange", "list_date", "data_source"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    code_col = _first_column(data, ["index_code", "industry_code", "code"])
    name_col = _first_column(data, ["industry_name", "name", "index_name"])
    if code_col is None:
        return pd.DataFrame(columns=columns)
    src_col = _first_column(data, ["src", "source"])
    result = pd.DataFrame(
        {
            "sector_code": data[code_col].astype(str).str.strip().str.upper(),
            "name": data[name_col].astype(str).str.strip() if name_col else "",
            "sector_type": sector_type,
            "exchange": data[src_col].astype(str).str.strip() if src_col else SW_SECTOR_SRC,
            "list_date": "",
            "data_source": SW_SECTOR_BASIC_DATA_SOURCE,
        }
    )
    return result[result["sector_code"] != ""].drop_duplicates(subset=["sector_code"], keep="last").reset_index(drop=True)


def _combine_sector_basic_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    columns = ["sector_code", "name", "sector_type", "exchange", "list_date", "data_source"]
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame(columns=columns)
    data = pd.concat(valid, ignore_index=True, sort=False)
    for column in columns:
        if column not in data.columns:
            data[column] = ""
    return data[columns].drop_duplicates(subset=["sector_code"], keep="last").sort_values(
        ["sector_type", "sector_code"]
    ).reset_index(drop=True)


def _adapt_ths_sector_daily(frame: pd.DataFrame | None, *, sector_code: str) -> pd.DataFrame:
    columns = ["sector_code", "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount", "data_source"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    date_col = _first_column(data, ["trade_date", "date"])
    close_col = _first_column(data, ["close", "收盘"])
    if date_col is None or close_col is None:
        return pd.DataFrame(columns=columns)
    code_col = _first_column(data, ["ts_code", "index_code", "code"])
    result = pd.DataFrame(
        {
            "sector_code": data[code_col].astype(str).str.upper() if code_col else str(sector_code).upper(),
            "trade_date": data[date_col].astype(str).str.replace("-", "", regex=False).str[:8],
            "open": _numeric_column(data, ["open", "开盘"]),
            "high": _numeric_column(data, ["high", "最高"]),
            "low": _numeric_column(data, ["low", "最低"]),
            "close": pd.to_numeric(data[close_col], errors="coerce"),
            "pct_chg": _numeric_column(data, ["pct_chg", "change", "涨跌幅"]),
            "vol": _numeric_column(data, ["vol", "volume", "成交量"]),
            "amount": _numeric_column(data, ["amount", "成交额"]),
            "data_source": HOT_SECTOR_DATA_SOURCE,
        }
    )
    result["sector_code"] = result["sector_code"].astype(str).str.strip().str.upper()
    return result.dropna(subset=["close"]).drop_duplicates(
        subset=["sector_code", "trade_date"], keep="last"
    ).sort_values(["sector_code", "trade_date"]).reset_index(drop=True)


def _combine_sector_daily_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    columns = ["sector_code", "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount", "data_source"]
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame(columns=columns)
    data = pd.concat(valid, ignore_index=True, sort=False)
    for column in columns:
        if column not in data.columns:
            data[column] = None
    return data[columns].drop_duplicates(subset=["sector_code", "trade_date"], keep="last").sort_values(
        ["sector_code", "trade_date"]
    ).reset_index(drop=True)


def _adapt_ths_sector_members(frame: pd.DataFrame | None, *, sector_code: str) -> pd.DataFrame:
    columns = ["sector_code", "ts_code", "name", "weight", "in_date", "out_date", "is_new", "data_source"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    code_col = _first_column(data, ["con_code", "ts_code", "code", "symbol"])
    if code_col is None:
        return pd.DataFrame(columns=columns)
    sector_col = _first_column(data, ["ts_code", "index_code"])
    name_col = _first_column(data, ["con_name", "name"])
    result = pd.DataFrame(
        {
            "sector_code": data[sector_col].astype(str).str.upper() if sector_col and sector_col != code_col else str(sector_code).upper(),
            "ts_code": data[code_col].astype(str).map(_normalize_ts_code),
            "name": data[name_col].astype(str).str.strip() if name_col else "",
            "weight": _numeric_column(data, ["weight"]),
            "in_date": _text_column(data, ["in_date", "start_date"]),
            "out_date": _text_column(data, ["out_date", "end_date"]),
            "is_new": _bool_column(data, ["is_new"]),
            "data_source": HOT_SECTOR_DATA_SOURCE,
        }
    )
    result["sector_code"] = result["sector_code"].astype(str).str.strip().str.upper()
    return result[result["ts_code"].astype(str).str.endswith((".SH", ".SZ", ".BJ"))].drop_duplicates(
        subset=["sector_code", "ts_code"], keep="last"
    ).sort_values(["sector_code", "ts_code"]).reset_index(drop=True)


def _adapt_sw_sector_members(frame: pd.DataFrame | None, *, sector_code: str) -> pd.DataFrame:
    columns = ["sector_code", "ts_code", "name", "weight", "in_date", "out_date", "is_new", "data_source"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.copy()
    code_col = _first_column(data, ["ts_code", "con_code", "code", "symbol"])
    if code_col is None:
        return pd.DataFrame(columns=columns)
    name_col = _first_column(data, ["name", "con_name"])
    result = pd.DataFrame(
        {
            "sector_code": str(sector_code).strip().upper(),
            "ts_code": data[code_col].astype(str).map(_normalize_ts_code),
            "name": data[name_col].astype(str).str.strip() if name_col else "",
            "weight": None,
            "in_date": _text_column(data, ["in_date", "start_date"]),
            "out_date": _text_column(data, ["out_date", "end_date"]),
            "is_new": _bool_column(data, ["is_new"]),
            "data_source": SW_SECTOR_MEMBER_DATA_SOURCE,
        }
    )
    return result[result["ts_code"].astype(str).str.endswith((".SH", ".SZ", ".BJ"))].drop_duplicates(
        subset=["sector_code", "ts_code"], keep="last"
    ).sort_values(["sector_code", "ts_code"]).reset_index(drop=True)


def _combine_sector_member_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    columns = ["sector_code", "ts_code", "name", "weight", "in_date", "out_date", "is_new", "data_source"]
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame(columns=columns)
    data = pd.concat(valid, ignore_index=True, sort=False)
    for column in columns:
        if column not in data.columns:
            data[column] = None
    return data[columns].drop_duplicates(subset=["sector_code", "ts_code"], keep="last").sort_values(
        ["sector_code", "ts_code"]
    ).reset_index(drop=True)


def _score_hot_sectors(basic: pd.DataFrame, daily: pd.DataFrame, *, sector_type: str, limit: int) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    names = {
        str(row["sector_code"]): str(row.get("name") or "")
        for _, row in basic.iterrows()
    }
    rows: list[dict[str, Any]] = []
    for sector_code, group in daily.groupby("sector_code"):
        ordered = group.sort_values("trade_date").tail(5).copy()
        if len(ordered) < 3:
            continue
        closes = pd.to_numeric(ordered["close"], errors="coerce").dropna()
        if len(closes) < 3:
            continue
        five_base = closes.iloc[0]
        three_base = closes.iloc[-3]
        latest = closes.iloc[-1]
        if five_base <= 0 or three_base <= 0:
            continue
        ret_5d = (latest / five_base - 1.0) * 100.0
        ret_3d = (latest / three_base - 1.0) * 100.0
        pct_series = pd.to_numeric(ordered.get("pct_chg"), errors="coerce")
        if pct_series.isna().all():
            pct_series = closes.pct_change().fillna(0) * 100
        latest_pct = _num(pct_series.iloc[-1])
        up_days_5 = int((pct_series.tail(5) > 0).sum())
        up_days_3 = int((pct_series.tail(3) > 0).sum())
        heat = ret_5d * 0.35 + ret_3d * 0.35 + latest_pct * 0.15 + up_days_5 * 1.0 + up_days_3 * 1.5
        if latest_pct > 5 and up_days_3 <= 1:
            heat -= latest_pct * 0.4
        rows.append(
            {
                "sector_code": str(sector_code),
                "name": names.get(str(sector_code), ""),
                "sector_type": sector_type,
                "heat_score": round(float(heat), 4),
                "return_5d": round(float(ret_5d), 4),
                "return_3d": round(float(ret_3d), 4),
                "latest_pct_chg": round(float(latest_pct), 4),
                "up_days_5": up_days_5,
                "up_days_3": up_days_3,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["heat_score", "return_5d", "sector_code"], ascending=[False, False, True]).head(limit).reset_index(drop=True)


def _sector_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "sector_code": str(row.get("sector_code") or ""),
            "name": str(row.get("name") or ""),
            "sector_type": str(row.get("sector_type") or ""),
            "heat_score": _round_float(row.get("heat_score")),
            "return_5d": _round_float(row.get("return_5d")),
            "return_3d": _round_float(row.get("return_3d")),
            "latest_pct_chg": _round_float(row.get("latest_pct_chg")),
            "up_days_5": int(row.get("up_days_5") or 0),
            "up_days_3": int(row.get("up_days_3") or 0),
        }
        for _, row in frame.iterrows()
    ]


def _build_stock_hot_sector_map(hot: pd.DataFrame, members: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    hot_lookup = {str(row["sector_code"]): row.to_dict() for _, row in hot.iterrows()}
    result: dict[str, list[dict[str, Any]]] = {}
    for _, row in members.iterrows():
        sector = hot_lookup.get(str(row.get("sector_code") or ""))
        if not sector:
            continue
        item = {
            "sector_code": str(sector.get("sector_code") or ""),
            "name": str(sector.get("name") or ""),
            "sector_type": str(sector.get("sector_type") or ""),
            "heat_score": _round_float(sector.get("heat_score")),
            "return_5d": _round_float(sector.get("return_5d")),
            "return_3d": _round_float(sector.get("return_3d")),
            "latest_pct_chg": _round_float(sector.get("latest_pct_chg")),
            "up_days_5": int(sector.get("up_days_5") or 0),
            "up_days_3": int(sector.get("up_days_3") or 0),
        }
        result.setdefault(str(row.get("ts_code") or ""), []).append(item)
    for sectors in result.values():
        sectors.sort(key=lambda item: (-float(item.get("heat_score") or 0), item.get("name", "")))
    return result


def _numeric_column(data: pd.DataFrame, names: list[str]) -> pd.Series:
    column = _first_column(data, names)
    if column is None:
        return pd.Series([None] * len(data), index=data.index)
    return pd.to_numeric(data[column], errors="coerce")


def _text_column(data: pd.DataFrame, names: list[str]) -> pd.Series:
    column = _first_column(data, names)
    if column is None:
        return pd.Series([""] * len(data), index=data.index)
    return data[column].where(data[column].notna(), "").astype(str).str.strip()


def _bool_column(data: pd.DataFrame, names: list[str]) -> pd.Series:
    column = _first_column(data, names)
    if column is None:
        return pd.Series([False] * len(data), index=data.index)
    return data[column].map(lambda value: str(value).strip().lower() in {"1", "true", "t", "yes", "y"})


def _round_float(value: Any) -> float:
    number = _num(value)
    return round(float(number), 4)


def _clamp_hot_sector_days(value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 5
    return min(5, max(3, number))


def _build_fundamental_frame(pro: Any, ts_code: str, trade_date: str) -> pd.DataFrame:
    income = _latest_pit_row(
        _safe_dataframe_call(lambda **params: _call_pro(pro, "income", **params), ts_code=ts_code),
        trade_date,
    )
    fina = _latest_pit_row(
        _safe_dataframe_call(lambda **params: _call_pro(pro, "fina_indicator", **params), ts_code=ts_code),
        trade_date,
    )
    balance = _latest_pit_row(
        _safe_dataframe_call(lambda **params: _call_pro(pro, "balancesheet", **params), ts_code=ts_code),
        trade_date,
    )
    if income.empty and fina.empty and balance.empty:
        return pd.DataFrame()
    values: dict[str, Any] = {"ts_code": _normalize_ts_code(ts_code), "data_source": "tushare_fundamentals"}
    for row in [income, fina, balance]:
        if row.empty:
            continue
        record = row.iloc[-1].to_dict()
        for key in ["end_date", "ann_date", "f_ann_date"]:
            if key in record and record.get(key):
                values.setdefault("ann_date" if key == "f_ann_date" else key, str(record.get(key)))
        values.update(
            {
                "total_revenue": record.get("total_revenue", values.get("total_revenue")),
                "revenue": record.get("revenue", values.get("revenue")),
                "net_profit": record.get("n_income", record.get("net_profit", values.get("net_profit"))),
                "profit": record.get("operate_profit", record.get("profit", values.get("profit"))),
                "roe": record.get("roe", values.get("roe")),
                "debt_to_assets": record.get("debt_to_assets", values.get("debt_to_assets")),
                "total_assets": record.get("total_assets", values.get("total_assets")),
                "total_liab": record.get("total_liab", values.get("total_liab")),
            }
        )
    if not values.get("debt_to_assets"):
        assets = _num(values.get("total_assets"))
        liab = _num(values.get("total_liab"))
        if assets > 0:
            values["debt_to_assets"] = liab / assets * 100
    if not values.get("end_date"):
        values["end_date"] = str(trade_date)
    return pd.DataFrame([values])


def _latest_pit_row(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    ann_col = _first_column(data, ["f_ann_date", "ann_date"])
    if ann_col:
        ann_dates = data[ann_col].astype(str).str.replace("-", "", regex=False).str[:8]
        data = data[ann_dates <= str(trade_date)].copy()
    if data.empty:
        return pd.DataFrame()
    sort_cols = [column for column in ["end_date", "ann_date", "f_ann_date"] if column in data.columns]
    if sort_cols:
        data = data.sort_values(sort_cols)
    return data.tail(1).reset_index(drop=True)


def _combine_fundamental_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    data = pd.concat(valid, ignore_index=True, sort=False)
    data["ts_code"] = data["ts_code"].astype(str).map(_normalize_ts_code)
    return data.drop_duplicates(subset=["ts_code", "end_date"], keep="last").sort_values(
        ["ts_code", "end_date"]
    ).reset_index(drop=True)


def _filter_minute_trade_date(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    return frame[frame["trade_date"].astype(str) == str(trade_date)]


def _current_kline_coverage_missing(frame: pd.DataFrame, symbols: list[str], trade_date: str) -> bool:
    current = _filter_minute_trade_date(frame, trade_date)
    if current.empty or "ts_code" not in current.columns:
        return True
    returned = current["ts_code"].dropna().astype(str).nunique()
    return returned < max(1, int(len(symbols) * 0.5))


def _group_by_ts_code(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    sort_column = "trade_date" if "trade_date" in frame.columns else "end_date" if "end_date" in frame.columns else None
    return {
        str(ts_code): group.sort_values(sort_column).reset_index(drop=True) if sort_column else group.reset_index(drop=True)
        for ts_code, group in frame.groupby("ts_code", sort=False)
    }


def _replace_trade_date_rows(base: pd.DataFrame, replacement: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if replacement is None or replacement.empty:
        return base
    if base is None or base.empty or "trade_date" not in base.columns:
        return replacement.copy()
    kept = base[base["trade_date"].astype(str) != str(trade_date)].copy()
    return pd.concat([kept, replacement], ignore_index=True, sort=False)


def _with_previous_close_pct_chg(frame: pd.DataFrame, previous_daily: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or previous_daily is None or previous_daily.empty:
        return frame
    history = previous_daily.copy()
    history["ts_code"] = history["ts_code"].astype(str)
    history["trade_date"] = history["trade_date"].astype(str)
    history = history.sort_values(["ts_code", "trade_date"]).drop_duplicates(subset=["ts_code"], keep="last")
    previous_close = {
        str(row["ts_code"]): _num(row.get("close"))
        for _, row in history.iterrows()
    }
    result = frame.copy()
    if "pct_chg" not in result.columns:
        result["pct_chg"] = 0.0
    for index, row in result.iterrows():
        close = _num(row.get("close"))
        pre_close = previous_close.get(str(row.get("ts_code") or ""), 0.0)
        if close > 0 and pre_close > 0:
            result.at[index, "pct_chg"] = (close / pre_close - 1.0) * 100.0
    result.attrs.update(frame.attrs)
    return result


def _is_obviously_missing(frame: pd.DataFrame | None, universe_count: int) -> bool:
    if frame is None or frame.empty:
        return True
    threshold = max(1, int(universe_count * 0.5))
    return len(frame) < threshold


def _missing_label(daily_missing: bool, basic_missing: bool) -> str:
    labels = []
    if daily_missing:
        labels.append("daily")
    if basic_missing:
        labels.append("daily_basic")
    return "/".join(labels)


def _is_today_shanghai(trade_date: str) -> bool:
    return str(trade_date) == datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")


def _should_force_realtime(trade_date: str) -> bool:
    return _is_today_shanghai(trade_date) and _is_a_share_trading_time_now()


def _is_a_share_trading_time_now() -> bool:
    now = datetime.now(SHANGHAI_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    morning_start = 9 * 60 + 30
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60
    afternoon_end = 15 * 60
    return morning_start <= minutes <= morning_end or afternoon_start <= minutes <= afternoon_end


def _call_pro(pro: Any, api_name: str, **params: Any) -> pd.DataFrame:
    method = getattr(pro, api_name, None)
    if callable(method):
        data = method(**params)
    else:
        data = pro.query(api_name, **params)
    return data if isinstance(data, pd.DataFrame) else pd.DataFrame()


def _stock_dataset_payload(
    spec: Any,
    *,
    params: dict[str, Any],
    frame: pd.DataFrame,
    row_count: int,
    data_source: str,
    missing_fields: list[str],
) -> dict[str, Any]:
    columns = [str(column) for column in frame.columns] if frame is not None and not frame.empty else []
    return {
        "dataset": spec.dataset,
        "api": spec.api,
        "domain": spec.domain,
        "category": spec.category,
        "title": spec.title,
        "doc_id": spec.doc_id,
        "tags": list(getattr(spec, "tags", ())),
        "params": dict(params),
        "columns": columns,
        "rows": _dataframe_records(frame),
        "row_count": int(row_count),
        "returned_row_count": int(len(frame)) if frame is not None else 0,
        "data_source": data_source,
        "missing_fields": list(missing_fields),
    }


def _normalize_stock_dataset_params(params: dict[str, Any], *, input_fields: tuple[str, ...]) -> dict[str, Any]:
    allowed = set(input_fields)
    clean: dict[str, Any] = {}
    for key, value in params.items():
        param = str(key or "").strip()
        if param not in allowed or value is None:
            continue
        normalized = _normalize_stock_dataset_value(param, value)
        if normalized not in {None, ""}:
            clean[param] = normalized
    return clean


def _normalize_stock_dataset_value(param: str, value: Any) -> Any:
    if param == "ts_code":
        values = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
        return ",".join(_normalize_ts_code(item) for item in values if str(item).strip())
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    if param.endswith("date") or param in {
        "date",
        "period",
        "month",
        "start_month",
        "end_month",
        "enddate",
        "m",
        "start_m",
        "end_m",
        "q",
        "start_q",
        "end_q",
    }:
        clean = text.replace("-", "")
        return clean[:8] if len(clean) >= 8 else clean
    return value if isinstance(value, (int, float)) else text


def _sanitize_tushare_fields(fields: list[str] | str | None, *, allowed_fields: tuple[str, ...] = ()) -> list[str]:
    if fields is None:
        return []
    raw = fields.split(",") if isinstance(fields, str) else fields if isinstance(fields, list) else []
    allowed = set(allowed_fields)
    result: list[str] = []
    for field in raw:
        name = str(field or "").strip()
        if name and name.replace("_", "").isalnum() and (not allowed or name in allowed) and name not in result:
            result.append(name)
    return result[:30]


def _clamp_stock_dataset_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 200
    return min(1000, max(1, limit))


def _dataframe_records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    data = frame.astype(object).where(pd.notna(frame), None)
    return data.to_dict(orient="records")


def _normalize_ts_code(value: Any) -> str:
    return normalize_ts_code(value)


def _share_lookup(frame: pd.DataFrame | None) -> dict[str, float]:
    if frame is None or frame.empty:
        return {}
    code_col = _first_column(frame, ["ts_code", "symbol", "code"])
    share_col = _first_column(frame, ["float_share", "free_share"])
    if code_col is None or share_col is None:
        return {}
    result: dict[str, float] = {}
    for _, row in frame.iterrows():
        ts_code = _normalize_ts_code(row.get(code_col))
        share = _num(row.get(share_col))
        if ts_code and share > 0:
            result[ts_code] = share
    return result


def _latest_share_lookup(frame: pd.DataFrame | None) -> dict[str, float]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    data = frame.copy()
    if "trade_date" in data.columns:
        data = data.sort_values("trade_date")
    result: dict[str, float] = {}
    for _, row in data.iterrows():
        ts_code = str(row.get("ts_code") or "").strip()
        share = _num(row.get("float_share")) or _num(row.get("free_share"))
        if ts_code and share > 0:
            result[ts_code] = share
    return result


def _daily_basic_source_from_frame(
    frame: pd.DataFrame | None,
    *,
    default: str,
    skipped: bool = False,
) -> str:
    if skipped:
        return "skipped_for_chan_third_buy"
    if frame is None or frame.empty:
        return default
    return str(frame.attrs.get("daily_basic_source") or default)


def _realtime_daily_basic_from_daily(
    daily: pd.DataFrame,
    trade_date: str,
    share_lookup: dict[str, float],
    *,
    source: str = "synthetic_realtime_basic",
) -> pd.DataFrame:
    if daily is None or daily.empty or not share_lookup:
        return pd.DataFrame()
    rows = []
    for _, row in daily.iterrows():
        ts_code = str(row.get("ts_code") or "").strip()
        float_share = share_lookup.get(ts_code, 0.0)
        close = _num(row.get("close"))
        vol = _num(row.get("vol"))
        if not ts_code or float_share <= 0 or close <= 0 or vol <= 0:
            continue
        turnover_rate = vol / float_share
        circ_mv = close * float_share
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": str(trade_date),
                "turnover_rate": turnover_rate,
                "turnover_rate_f": turnover_rate,
                "circ_mv": circ_mv,
                "float_share": float_share,
                "free_share": float_share,
                "float_mv": circ_mv,
                "total_mv": None,
                "pe": None,
                "pb": None,
                "ps": None,
            }
        )
    result = pd.DataFrame(rows)
    if not result.empty:
        result.attrs["daily_basic_source"] = source
    return result


def _empty_price_volume_ma_metadata(ts_code: str) -> dict[str, Any]:
    return {
        "ts_code": ts_code,
        "selected": False,
        "selection_source": "",
        "current_logic_passed": False,
        "legacy_logic_passed": False,
        "raw_metrics": {},
    }


def _latest_rows_by_ts_code(frame: pd.DataFrame, trade_date: str) -> dict[str, dict[str, Any]]:
    if frame is None or frame.empty or "ts_code" not in frame.columns or "trade_date" not in frame.columns:
        return {}
    data = frame[frame["trade_date"].astype(str) == str(trade_date)].copy()
    if data.empty:
        return {}
    return {
        str(row["ts_code"]): row.to_dict()
        for _, row in data.drop_duplicates(subset=["ts_code"], keep="last").iterrows()
        if str(row.get("ts_code") or "").strip()
    }


def _is_st_stock_info(stock_info: dict[str, Any]) -> bool:
    return "ST" in str(stock_info.get("name") or "").upper()


def _is_bse_stock_info(ts_code: str, stock_info: dict[str, Any]) -> bool:
    raw = (ts_code or "").strip().upper()
    market = str(stock_info.get("market") or "")
    exchange = str(stock_info.get("exchange") or "").upper()
    return raw.endswith(".BJ") or exchange == "BSE" or "北交" in market


def _resolve_price_volume_turnover(row: dict[str, Any]) -> float:
    turnover_rate_f = _optional_num(row.get("turnover_rate_f"))
    if turnover_rate_f is not None:
        return turnover_rate_f
    return _num(row.get("turnover_rate"))


def _price_volume_pre_ma_filters_pass(
    pct_chg: float,
    volume_ratio: float,
    turnover_rate: float,
    circ_mv: float,
) -> bool:
    return (
        PRICE_VOLUME_PCT_CHG_RANGE[0] <= pct_chg <= PRICE_VOLUME_PCT_CHG_RANGE[1]
        and volume_ratio > 1.0
        and PRICE_VOLUME_TURNOVER_RANGE[0] <= turnover_rate <= PRICE_VOLUME_TURNOVER_RANGE[1]
        and PRICE_VOLUME_CIRC_MV_RANGE[0] <= circ_mv <= PRICE_VOLUME_CIRC_MV_RANGE[1]
    )


def _history_volume_ratio(frame: pd.DataFrame) -> float:
    if frame is None or frame.empty or "vol" not in frame.columns or len(frame) < 6:
        return 0.0
    data = frame.sort_values("trade_date").reset_index(drop=True)
    latest = _num(data.iloc[-1].get("vol"))
    base = _num(pd.to_numeric(data.iloc[-6:-1]["vol"], errors="coerce").mean())
    if base <= 0:
        return 0.0
    return latest / base


def _legacy_volume_metrics(frame: pd.DataFrame, trade_date: str) -> tuple[float, float]:
    if frame is None or frame.empty or "trade_date" not in frame.columns or "vol" not in frame.columns:
        return 0.0, 0.0
    data = frame.copy()
    trade_dates = data["trade_date"].astype(str)
    window_start = _days_before(trade_date, 10)
    recent_window = data[(trade_dates >= window_start) & (trade_dates <= str(trade_date))]
    if len(recent_window) < 6:
        return 0.0, 0.0
    recent_trade_dates = recent_window["trade_date"].astype(str)
    today_rows = recent_window[recent_trade_dates == str(trade_date)]
    if today_rows.empty:
        return 0.0, 0.0
    current_volume = _num(today_rows.iloc[0].get("vol"))
    previous_volumes = pd.to_numeric(recent_window[recent_trade_dates != str(trade_date)]["vol"], errors="coerce").tail(5)
    prev_5_avg_volume = _num(previous_volumes.mean())
    if prev_5_avg_volume <= 0:
        return 0.0, 0.0
    return prev_5_avg_volume, current_volume / prev_5_avg_volume


def _ma_stack_latest(frame: pd.DataFrame) -> dict[str, Any]:
    data = _sorted_history(frame)
    if len(data) < 61 or "close" not in data.columns:
        return {"passed": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0, "ma60": 0.0}
    ma5 = _resolve_ma_series(data, 5)
    ma10 = _resolve_ma_series(data, 10)
    ma20 = _resolve_ma_series(data, 20)
    ma60 = _resolve_ma_series(data, 60)
    latest_ma5 = _num(ma5.iloc[-1])
    latest_ma10 = _num(ma10.iloc[-1])
    latest_ma20 = _num(ma20.iloc[-1])
    latest_ma60 = _num(ma60.iloc[-1])
    return {
        "passed": latest_ma5 > latest_ma10 > latest_ma20 > latest_ma60,
        "ma5": latest_ma5,
        "ma10": latest_ma10,
        "ma20": latest_ma20,
        "ma60": latest_ma60,
    }


def _ma_stack_on_trade_date(frame: pd.DataFrame, trade_date: str) -> dict[str, Any]:
    data = _sorted_history(frame)
    if len(data) < 60 or "close" not in data.columns or "trade_date" not in data.columns:
        return {"passed": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0, "ma60": 0.0}
    closes = pd.to_numeric(data["close"], errors="coerce")
    ma5 = closes.rolling(window=5, min_periods=5).mean()
    ma10 = closes.rolling(window=10, min_periods=10).mean()
    ma20 = closes.rolling(window=20, min_periods=20).mean()
    ma60 = closes.rolling(window=60, min_periods=60).mean()
    matched = data[data["trade_date"].astype(str) == str(trade_date)]
    if matched.empty:
        return {"passed": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0, "ma60": 0.0}
    index = matched.index[-1]
    values = (ma5.iloc[index], ma10.iloc[index], ma20.iloc[index], ma60.iloc[index])
    if any(pd.isna(value) for value in values):
        return {"passed": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0, "ma60": 0.0}
    latest_ma5, latest_ma10, latest_ma20, latest_ma60 = [_num(value) for value in values]
    return {
        "passed": latest_ma5 > latest_ma10 > latest_ma20 > latest_ma60,
        "ma5": latest_ma5,
        "ma10": latest_ma10,
        "ma20": latest_ma20,
        "ma60": latest_ma60,
    }


def _resolve_ma_series(frame: pd.DataFrame, window: int) -> pd.Series:
    column = f"ma{window}"
    if column in frame.columns:
        series = pd.to_numeric(frame[column], errors="coerce")
        if not series.isna().all():
            return series
    return pd.to_numeric(frame["close"], errors="coerce").rolling(window=window, min_periods=window).mean()


def _sorted_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "trade_date" in data.columns:
        data["trade_date"] = data["trade_date"].astype(str)
        return data.sort_values("trade_date").reset_index(drop=True)
    return data.reset_index(drop=True)


def _safe_dataframe_call(func: Any, **params: Any) -> pd.DataFrame:
    try:
        data = func(**params)
    except Exception:
        return pd.DataFrame()
    return data if isinstance(data, pd.DataFrame) else pd.DataFrame()


def _tushare_pro_bar(api: Any, **params: Any) -> pd.DataFrame:
    if ts is None:  # pragma: no cover - dependency guard
        raise RuntimeError("tushare is not installed; install requirements.txt")
    data = ts.pro_bar(api=api, **params)
    return data if isinstance(data, pd.DataFrame) else pd.DataFrame()


def _one_year_start_date(trade_date: str) -> str:
    trade_dt = datetime.strptime(trade_date, "%Y%m%d")
    return trade_dt.replace(year=trade_dt.year - 1).strftime("%Y%m%d")


def _days_before(trade_date: str, days: int) -> str:
    trade_dt = datetime.strptime(trade_date, "%Y%m%d")
    return (trade_dt - timedelta(days=days)).strftime("%Y%m%d")


def lookback_count(start_date: str, trade_date: str) -> int:
    try:
        start_dt = datetime.strptime(str(start_date), "%Y%m%d")
        end_dt = datetime.strptime(str(trade_date), "%Y%m%d")
    except ValueError:
        return SCREENING_TRADE_DAYS
    return max(SCREENING_TRADE_DAYS, (end_dt - start_dt).days)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_num(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _warn_near_limit(interface: str, trade_date: str, frame: pd.DataFrame | None) -> None:
    if frame is not None and len(frame) >= TUSHARE_ROW_WARNING_LIMIT:
        warnings.warn(
            f"Tushare {interface} returned {len(frame)} rows for {trade_date}; "
            "result may be near the single-call limit.",
            RuntimeWarning,
            stacklevel=2,
        )


def _first_column(frame: pd.DataFrame, names: list[str]) -> str | None:
    for name in names:
        if name in frame.columns:
            return name
    lowered = {str(col).lower(): str(col) for col in frame.columns}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None
