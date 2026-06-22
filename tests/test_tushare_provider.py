from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.data.tushare_provider import TushareDataProvider
from sats.data.tushare_stock_datasets import list_tushare_datasets, list_tushare_stock_datasets
from sats.screening.registry import get_rule
from sats.storage.duckdb import DuckDBStorage
from tests.fixtures import (
    make_benchmark,
    make_chan_daily,
    make_chan_minute_30m,
    make_daily_basic,
    make_monthly_base_breakout,
    make_monthly_base_breakout_daily,
    make_passing_daily,
    make_price_volume_daily,
    make_trade_dates,
)


class FakePro:
    def stock_basic(self, **kwargs):
        self.kwargs = kwargs
        return pd.DataFrame(
            {
                "ts_code": [
                    "600000.SH",
                    "000001.SZ",
                    "430047.BJ",
                    "000001.SZ",
                    "830000.OC",
                    "",
                ],
                "name": ["浦发银行", "平安银行", "北交样本", "平安银行", "三板样本", ""],
            }
        )


class FakeBatchPro:
    def __init__(self) -> None:
        self.trade_dates = make_trade_dates(70)
        self.daily_rows = make_passing_daily()
        self.basic_rows = make_daily_basic()
        self.daily_calls: list[dict] = []
        self.daily_basic_calls: list[dict] = []
        self.index_daily_calls: list[str] = []
        self.ths_index_calls = 0
        self.ths_index_types: list[str] = []
        self.ths_daily_calls: list[str] = []
        self.ths_member_calls: list[str] = []
        self.index_classify_calls: list[dict] = []
        self.index_member_all_calls: list[dict] = []
        self.limit_list_d_calls: list[str] = []

    def stock_basic(self, **kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "symbol": ["000001"],
                "name": ["平安银行"],
                "industry": ["银行"],
                "market": ["主板"],
                "exchange": ["SZSE"],
                "list_date": ["19910403"],
            }
        )

    def trade_cal(self, **kwargs):
        return pd.DataFrame({"cal_date": self.trade_dates, "is_open": [1] * len(self.trade_dates)})

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        return self.daily_rows[self.daily_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()

    def daily_basic(self, **kwargs):
        self.daily_basic_calls.append(kwargs)
        return self.basic_rows[self.basic_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()

    def index_daily(self, **kwargs):
        self.index_daily_calls.append(kwargs["ts_code"])
        data = make_benchmark()
        data["ts_code"] = kwargs["ts_code"]
        return data

    def ths_index(self, **kwargs):
        self.ths_index_calls += 1
        self.ths_index_types.append(kwargs.get("type", ""))
        if kwargs.get("type") == "N":
            return pd.DataFrame({"ts_code": ["885001.TI", "885002.TI"], "name": ["AI算力", "单日脉冲"]})
        return pd.DataFrame({"ts_code": ["881155.TI"], "name": ["银行"]})

    def ths_daily(self, **kwargs):
        self.ths_daily_calls.append(kwargs["ts_code"])
        dates = make_trade_dates(5, end="20260430")
        closes = {
            "881155.TI": [10, 10.2, 10.4, 10.6, 10.9],
            "885001.TI": [10, 10.3, 10.7, 11.0, 11.4],
            "885002.TI": [10, 9.9, 9.8, 9.7, 10.7],
        }.get(kwargs["ts_code"], [10, 10, 10, 10, 10])
        return pd.DataFrame(
            {
                "ts_code": kwargs["ts_code"],
                "trade_date": dates,
                "open": closes,
                "high": [value + 0.1 for value in closes],
                "low": [value - 0.1 for value in closes],
                "close": closes,
                "pct_chg": [0, *[(closes[index] / closes[index - 1] - 1) * 100 for index in range(1, len(closes))]],
                "vol": [1000] * len(closes),
                "amount": [10000] * len(closes),
            }
        )

    def ths_member(self, **kwargs):
        self.ths_member_calls.append(kwargs["ts_code"])
        members = {
            "881155.TI": [("000001.SZ", "平安银行")],
            "885001.TI": [("000938.SZ", "紫光股份")],
            "885002.TI": [("600519.SH", "贵州茅台")],
        }.get(kwargs["ts_code"], [])
        return pd.DataFrame(
            {
                "ts_code": [kwargs["ts_code"]] * len(members),
                "con_code": [code for code, _ in members],
                "con_name": [name for _, name in members],
                "weight": [1.0] * len(members),
                "is_new": ["N"] * len(members),
            }
        )

    def index_classify(self, **kwargs):
        self.index_classify_calls.append(dict(kwargs))
        level = kwargs.get("level", "")
        rows = {
            "L1": [{"index_code": "801780.SI", "industry_name": "银行", "level": "L1", "src": "SW2021"}],
            "L2": [{"index_code": "801783.SI", "industry_name": "股份制银行Ⅱ", "level": "L2", "src": "SW2021"}],
            "L3": [{"index_code": "851911.SI", "industry_name": "贸易Ⅲ", "level": "L3", "src": "SW2021"}],
        }.get(level, [])
        return pd.DataFrame(rows)

    def index_member_all(self, **kwargs):
        self.index_member_all_calls.append(dict(kwargs))
        if kwargs.get("l1_code") == "801780.SI":
            rows = [
                {"l1_code": "801780.SI", "l1_name": "银行", "ts_code": "000001.SZ", "name": "平安银行", "is_new": "Y"},
                {"l1_code": "801780.SI", "l1_name": "银行", "ts_code": "00700.HK", "name": "腾讯控股", "is_new": "Y"},
            ]
        elif kwargs.get("l2_code") == "801783.SI":
            rows = [
                {"l2_code": "801783.SI", "l2_name": "股份制银行Ⅱ", "ts_code": "600000.SH", "name": "浦发银行", "is_new": "N"}
            ]
        elif kwargs.get("l3_code") == "851911.SI":
            rows = [
                {"l3_code": "851911.SI", "l3_name": "贸易Ⅲ", "ts_code": "600153.SH", "name": "建发股份", "is_new": "Y"}
            ]
        else:
            rows = []
        return pd.DataFrame(rows)

    def limit_list_d(self, **kwargs):
        limit_type = kwargs.get("limit_type", "")
        self.limit_list_d_calls.append(limit_type)
        sizes = {"U": 90, "D": 2, "Z": 20}
        size = sizes.get(limit_type, 0)
        return pd.DataFrame(
            {
                "ts_code": [f"000{index:03d}.SZ" for index in range(size)],
                "trade_date": [kwargs.get("trade_date", "20260430")] * size,
            }
        )


class FakeMissingCurrentPro(FakeBatchPro):
    def __init__(self) -> None:
        super().__init__()
        self.trade_dates = ["20260513", "20260514"]
        self.daily_rows = make_passing_daily(end="20260513")
        self.basic_rows = make_daily_basic(end="20260513")

    def rt_k(self, **kwargs):
        raise AssertionError("must not call rt_k")


class FakeRefreshCurrentPro(FakeBatchPro):
    def __init__(self) -> None:
        super().__init__()
        self.trade_dates = ["20260513", "20260514"]
        self.daily_rows = make_passing_daily(end="20260514")
        self.basic_rows = make_daily_basic(end="20260514")
        self.daily_by_date_calls: dict[str, int] = {}
        self.basic_by_date_calls: dict[str, int] = {}

    def daily(self, **kwargs):
        trade_date = str(kwargs["trade_date"])
        self.daily_calls.append(kwargs)
        self.daily_by_date_calls[trade_date] = self.daily_by_date_calls.get(trade_date, 0) + 1
        if trade_date == "20260514" and self.daily_by_date_calls[trade_date] == 1:
            return pd.DataFrame()
        return self.daily_rows[self.daily_rows["trade_date"].astype(str) == trade_date].copy()

    def daily_basic(self, **kwargs):
        trade_date = str(kwargs["trade_date"])
        self.daily_basic_calls.append(kwargs)
        self.basic_by_date_calls[trade_date] = self.basic_by_date_calls.get(trade_date, 0) + 1
        if trade_date == "20260514" and self.basic_by_date_calls[trade_date] == 1:
            return pd.DataFrame()
        return self.basic_rows[self.basic_rows["trade_date"].astype(str) == trade_date].copy()


class FakeRealtimeCurrentPro(FakeBatchPro):
    def __init__(self) -> None:
        super().__init__()
        self.trade_dates = make_trade_dates(70, end="20260514")
        self.daily_rows = make_passing_daily(end="20260513")
        self.basic_rows = make_daily_basic(end="20260513")
        self.premarket_calls: list[dict] = []

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        trade_date = str(kwargs["trade_date"])
        if trade_date == "20260514":
            return pd.DataFrame()
        return self.daily_rows[self.daily_rows["trade_date"].astype(str) == trade_date].copy()

    def daily_basic(self, **kwargs):
        self.daily_basic_calls.append(kwargs)
        trade_date = str(kwargs["trade_date"])
        if trade_date == "20260514":
            return pd.DataFrame()
        return self.basic_rows[self.basic_rows["trade_date"].astype(str) == trade_date].copy()

    def rt_k(self, **kwargs):
        raise AssertionError("must not call rt_k")

    def stk_premarket(self, **kwargs):
        self.premarket_calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "trade_date": "20260514",
                    "ts_code": "000001.SZ",
                    "float_share": 15_000.0,
                }
            ]
        )


class FakeForcedRealtimeCachedPro(FakeBatchPro):
    def __init__(self) -> None:
        super().__init__()
        self.premarket_calls: list[dict] = []

    def rt_k(self, **kwargs):
        raise AssertionError("must not call rt_k")

    def stk_premarket(self, **kwargs):
        self.premarket_calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "trade_date": "20260430",
                    "ts_code": "000001.SZ",
                    "float_share": 50_000.0,
                }
            ]
        )


class FakeForcedRealtimeFailurePro(FakeForcedRealtimeCachedPro):
    pass


def _stock_daily(ts_code: str, *, latest_volume: float = 1200.0, ma_stack: bool = True) -> pd.DataFrame:
    frame = make_price_volume_daily(latest_volume=latest_volume, ma_stack=ma_stack)
    frame["ts_code"] = ts_code
    return frame


def _stock_basic_rows(codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": ts_code,
                "symbol": ts_code.split(".")[0],
                "name": f"样本{index}",
                "industry": "测试",
                "market": "主板",
                "exchange": "SZSE" if ts_code.endswith(".SZ") else "SSE",
                "list_date": "19910403",
            }
            for index, ts_code in enumerate(codes, start=1)
        ]
    )


class FakePriceVolumeUnionPro:
    def __init__(self) -> None:
        self.trade_dates = make_trade_dates(70, end="20260430")
        self.codes = ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"]
        self.daily_rows = pd.concat([_stock_daily(code) for code in self.codes], ignore_index=True)
        self.basic_rows = pd.concat(
            [
                make_daily_basic(end="20260430").assign(ts_code=code)
                for code in self.codes
            ],
            ignore_index=True,
        )
        self.current_history = {
            "000001.SZ": _stock_daily("000001.SZ", ma_stack=True),
            "000002.SZ": _stock_daily("000002.SZ", ma_stack=False),
            "000003.SZ": _stock_daily("000003.SZ", ma_stack=True),
            "000004.SZ": _stock_daily("000004.SZ", ma_stack=False),
        }
        self.legacy_history = {
            "000001.SZ": _stock_daily("000001.SZ", latest_volume=900.0, ma_stack=True),
            "000002.SZ": _stock_daily("000002.SZ", latest_volume=1200.0, ma_stack=True),
            "000003.SZ": _stock_daily("000003.SZ", latest_volume=1200.0, ma_stack=True),
            "000004.SZ": _stock_daily("000004.SZ", latest_volume=900.0, ma_stack=False),
        }
        self.daily_calls: list[dict] = []
        self.daily_basic_calls: list[dict] = []
        self.pro_bar_calls: list[dict] = []

    def stock_basic(self, **kwargs):
        return _stock_basic_rows(self.codes)

    def trade_cal(self, **kwargs):
        return pd.DataFrame({"cal_date": self.trade_dates, "is_open": [1] * len(self.trade_dates)})

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        if "trade_date" in kwargs:
            return self.daily_rows[self.daily_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()
        return self.legacy_history[str(kwargs["ts_code"])].copy()

    def daily_basic(self, **kwargs):
        self.daily_basic_calls.append(kwargs)
        return self.basic_rows[self.basic_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()

    def pro_bar(self, **kwargs):
        raise AssertionError("price_volume_ma current logic must call top-level tushare.pro_bar")


class FakePriceVolumeMissingRecentVolumePro:
    def __init__(self) -> None:
        self.trade_dates = make_trade_dates(70, end="20260430")
        self.codes = ["000010.SZ", "000011.SZ"]
        missing_recent_date = self.trade_dates[-2]
        primary_daily = _stock_daily("000010.SZ")
        primary_daily = primary_daily[primary_daily["trade_date"].astype(str) != missing_recent_date]
        filler_daily = _stock_daily("000011.SZ")
        filler_daily.loc[filler_daily["trade_date"].astype(str) == "20260430", "pct_chg"] = 0.0
        self.daily_rows = pd.concat([primary_daily, filler_daily], ignore_index=True)
        self.basic_rows = pd.concat(
            [
                make_daily_basic(end="20260430").assign(ts_code=code)
                for code in self.codes
            ],
            ignore_index=True,
        )
        self.current_history = {code: _stock_daily(code, ma_stack=True) for code in self.codes}
        self.legacy_history = {
            "000010.SZ": _stock_daily("000010.SZ", latest_volume=900.0, ma_stack=True),
            "000011.SZ": _stock_daily("000011.SZ", latest_volume=900.0, ma_stack=True),
        }
        self.daily_calls: list[dict] = []
        self.daily_basic_calls: list[dict] = []
        self.pro_bar_calls: list[dict] = []

    def stock_basic(self, **kwargs):
        return _stock_basic_rows(self.codes)

    def trade_cal(self, **kwargs):
        return pd.DataFrame({"cal_date": self.trade_dates, "is_open": [1] * len(self.trade_dates)})

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        if "trade_date" in kwargs:
            return self.daily_rows[self.daily_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()
        return self.legacy_history[str(kwargs["ts_code"])].copy()

    def daily_basic(self, **kwargs):
        self.daily_basic_calls.append(kwargs)
        return self.basic_rows[self.basic_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()

    def pro_bar(self, **kwargs):
        self.pro_bar_calls.append(kwargs)
        return self.current_history[str(kwargs["ts_code"])].copy()


class FakePriceVolumeForcedRealtimePro:
    def __init__(self) -> None:
        self.trade_dates = make_trade_dates(70, end="20260430")
        self.codes = ["000001.SZ"]
        self.daily_rows = _stock_daily("000001.SZ")
        self.basic_rows = make_daily_basic(end="20260430").assign(ts_code="000001.SZ")
        self.daily_calls: list[dict] = []
        self.daily_basic_calls: list[dict] = []
        self.pro_bar_calls: list[dict] = []
        self.premarket_calls: list[dict] = []

    def stock_basic(self, **kwargs):
        return _stock_basic_rows(self.codes)

    def trade_cal(self, **kwargs):
        return pd.DataFrame({"cal_date": self.trade_dates, "is_open": [1] * len(self.trade_dates)})

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        if "trade_date" in kwargs:
            return self.daily_rows[self.daily_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()
        raise AssertionError("forced realtime price_volume_ma must not call per-stock daily")

    def daily_basic(self, **kwargs):
        self.daily_basic_calls.append(kwargs)
        return self.basic_rows[self.basic_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()

    def pro_bar(self, **kwargs):
        self.pro_bar_calls.append(kwargs)
        raise AssertionError("forced realtime price_volume_ma must not call pro_bar")

    def rt_k(self, **kwargs):
        raise AssertionError("must not call rt_k")

    def stk_premarket(self, **kwargs):
        self.premarket_calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "trade_date": "20260430",
                    "ts_code": "000001.SZ",
                    "float_share": 50_000.0,
                }
            ]
        )


class FakeChanThirdBuyPro:
    def __init__(self) -> None:
        self.trade_dates = make_trade_dates(45, end="20260430")
        self.codes = ["000001.SZ", "000002.SZ"]
        self.daily_rows = pd.concat(
            [
                make_chan_daily(end="20260430").assign(ts_code="000001.SZ"),
                make_chan_daily(end="20260430", no_pullback=True).assign(ts_code="000002.SZ"),
            ],
            ignore_index=True,
        )
        self.basic_rows = pd.concat(
            [
                make_daily_basic(end="20260430").assign(ts_code=code)
                for code in self.codes
            ],
            ignore_index=True,
        )
        self.daily_calls: list[dict] = []
        self.daily_basic_calls: list[dict] = []

    def stock_basic(self, **kwargs):
        return _stock_basic_rows(self.codes)

    def trade_cal(self, **kwargs):
        return pd.DataFrame({"cal_date": self.trade_dates, "is_open": [1] * len(self.trade_dates)})

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        return self.daily_rows[self.daily_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()

    def daily_basic(self, **kwargs):
        self.daily_basic_calls.append(kwargs)
        return self.basic_rows[self.basic_rows["trade_date"].astype(str) == str(kwargs["trade_date"])].copy()


class FakeChanTickFlowProvider:
    calls: list[dict] = []
    fail_realtime = False
    history_end = "20260430"
    realtime_end = "20260430"

    def __init__(self, settings) -> None:
        self.settings = settings

    def load_historical_minute_klines(
        self,
        symbols,
        *,
        period="1m",
        start_time=None,
        end_time=None,
        count=None,
    ):
        self.calls.append(
            {
                "mode": "history",
                "symbols": list(symbols),
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "count": count,
            }
        )
        return pd.concat(
            [make_chan_minute_30m(end=type(self).history_end).assign(ts_code=symbol) for symbol in symbols],
            ignore_index=True,
        )

    def load_current_klines(self, symbols, *, period, trade_date, count=None):
        self.calls.append(
            {
                "mode": "current",
                "symbols": list(symbols),
                "period": period,
                "trade_date": trade_date,
                "count": count,
            }
        )
        if type(self).fail_realtime:
            raise ValueError("TickFlow 当前套餐不支持实时分钟K线")
        frame = pd.concat(
            [make_chan_minute_30m(end=type(self).realtime_end).assign(ts_code=symbol) for symbol in symbols],
            ignore_index=True,
        )
        frame.attrs["data_source"] = f"tickflow_current_{period}"
        return frame


class FakeFallbackTickFlowProvider(FakeChanTickFlowProvider):
    stock_basic_calls = 0
    daily_calls: list[dict] = []
    minute_quote_calls: list[dict] = []
    daily_basic_like_calls: list[dict] = []
    fail_daily = False
    fail_minute_quotes = False
    fail_stock_basic = False
    fail_daily_basic_like = False

    def __init__(self, settings) -> None:
        super().__init__(settings)

    def load_stock_basic(self, *, storage=None, universe_id="CN_Equity_A"):
        type(self).stock_basic_calls += 1
        if type(self).fail_stock_basic:
            raise TimeoutError("tickflow stock basic timeout")
        frame = _stock_basic_rows(["000001.SZ"])
        if storage is not None:
            storage.upsert_stock_basic(frame)
        return frame

    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        type(self).daily_calls.append(
            {"symbols": list(symbols), "start_date": start_date, "end_date": end_date}
        )
        if type(self).fail_daily:
            raise TimeoutError("tickflow daily timeout")
        frame = pd.concat(
            [make_passing_daily(end=str(end_date)).assign(ts_code=symbol) for symbol in symbols],
            ignore_index=True,
        )
        if storage is not None:
            storage.upsert_stock_daily(frame)
        return frame

    def load_realtime_daily_quotes(self, symbols, *, trade_date):
        type(self).minute_quote_calls.append(
            {"symbols": list(symbols), "trade_date": trade_date, "period": "1d", "count": 1}
        )
        if type(self).fail_minute_quotes:
            raise TimeoutError("tickflow current daily timeout")
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "trade_date": trade_date,
                    "open": 13.0,
                    "high": 13.6,
                    "low": 12.9,
                    "close": 13.5,
                    "vol": 1200.0,
                    "amount": 162000.0,
                    "pct_chg": 3.846153846,
                }
                for symbol in symbols
            ]
        )

    def load_realtime_daily_basic_like(self, symbols, *, trade_date, daily_frame=None, share_frame=None):
        type(self).daily_basic_like_calls.append(
            {"symbols": list(symbols), "trade_date": trade_date, "daily_rows": 0 if daily_frame is None else len(daily_frame)}
        )
        if type(self).fail_daily_basic_like:
            raise TimeoutError("tickflow daily_basic_like timeout")
        rows = []
        daily_lookup = {}
        if daily_frame is not None and not daily_frame.empty:
            daily_lookup = {
                str(row["ts_code"]): row.to_dict()
                for _, row in daily_frame.iterrows()
                if str(row.get("trade_date")) == str(trade_date)
            }
        for symbol in symbols:
            daily = daily_lookup.get(symbol, {})
            close = float(daily.get("close", 13.5))
            vol = float(daily.get("vol", 1200.0))
            float_share = 15_000.0
            rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": trade_date,
                    "turnover_rate": vol / float_share,
                    "turnover_rate_f": vol / float_share,
                    "circ_mv": close * float_share,
                    "float_share": float_share,
                    "free_share": float_share,
                    "float_mv": close * float_share,
                    "total_mv": close * 20_000.0,
                }
            )
        frame = pd.DataFrame(rows)
        frame.attrs["daily_basic_source"] = "tickflow_realtime_basic_like"
        return frame


class FakePriceVolumeRealtimeTickFlowProvider(FakeFallbackTickFlowProvider):
    minute_quote_calls: list[dict] = []
    fail_minute_quotes = False

    def load_realtime_daily_quotes(self, symbols, *, trade_date):
        type(self).minute_quote_calls.append(
            {"symbols": list(symbols), "trade_date": trade_date, "period": "1d", "count": 1}
        )
        if type(self).fail_minute_quotes:
            raise TimeoutError("tickflow current daily timeout")
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "trade_date": trade_date,
                    "open": 13.4,
                    "high": 14.0,
                    "low": 12.9,
                    "close": 13.936,
                    "vol": 400000.0,
                    "amount": 540000.0,
                    "pct_chg": 4.0,
                }
                for symbol in symbols
            ]
        )


class FakeMonthlyTickFlowProvider:
    monthly_calls: list[dict] = []
    daily_calls: list[dict] = []
    monthly_empty = False

    def __init__(self, settings) -> None:
        self.settings = settings

    def load_klines(self, symbols, *, period, start_time=None, end_time=None, count=None, adjust="none"):
        type(self).monthly_calls.append(
            {
                "symbols": list(symbols),
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "count": count,
            }
        )
        if type(self).monthly_empty:
            return pd.DataFrame()
        return pd.concat(
            [make_monthly_base_breakout(ts_code=symbol, end=str(end_time)) for symbol in symbols],
            ignore_index=True,
        )

    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        type(self).daily_calls.append(
            {"symbols": list(symbols), "start_date": start_date, "end_date": end_date}
        )
        return pd.concat(
            [make_monthly_base_breakout_daily(ts_code=symbol, end=str(end_date)) for symbol in symbols],
            ignore_index=True,
        )


class FakeStockBasicTimeoutPro(FakeBatchPro):
    def stock_basic(self, **kwargs):
        raise TimeoutError("stock_basic timeout")


class FakeDailyTimeoutPro(FakeBatchPro):
    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        raise TimeoutError("daily timeout")


class FakeDailyBasicTimeoutPro(FakeBatchPro):
    def daily_basic(self, **kwargs):
        self.daily_basic_calls.append(kwargs)
        raise TimeoutError("daily_basic timeout")


class FakeHotSectorUnavailablePro(FakeBatchPro):
    def ths_index(self, **kwargs):
        self.ths_index_calls += 1
        self.ths_index_types.append(kwargs.get("type", ""))
        return pd.DataFrame()


class FakeLimitListUnavailablePro(FakeBatchPro):
    def limit_list_d(self, **kwargs):
        self.limit_list_d_calls.append(kwargs.get("limit_type", ""))
        raise RuntimeError("limit_list_d denied")


class FakeStockDatasetPro:
    def __init__(self) -> None:
        self.daily_calls: list[dict] = []
        self.index_daily_calls: list[dict] = []

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        return pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20260520", "close": 10.0, "vol": 100.0},
                {"ts_code": "000001.SZ", "trade_date": "20260519", "close": 9.8, "vol": 90.0},
            ]
        )

    def index_daily(self, **kwargs):
        self.index_daily_calls.append(kwargs)
        return pd.DataFrame(
            [
                {"ts_code": "000001.SH", "trade_date": "20260520", "close": 3100.0},
                {"ts_code": "000001.SH", "trade_date": "20260519", "close": 3090.0},
            ]
        )


def _fake_top_level_pro_bar(api=None, **kwargs):
    api.pro_bar_calls.append(kwargs)
    return api.current_history[str(kwargs["ts_code"])].copy()


class TushareDataProviderTest(unittest.TestCase):
    def test_stock_dataset_registry_covers_6000_point_allowlist(self) -> None:
        datasets = list_tushare_stock_datasets()
        names = {item["dataset"] for item in datasets}

        self.assertEqual(len(datasets), 90)
        self.assertIn("daily_basic", names)
        self.assertIn("limit_list_d", names)
        self.assertIn("stk_factor_pro", names)
        self.assertIn("st", names)
        self.assertIn("stk_shock", names)
        self.assertIn("ths_hot", names)
        self.assertNotIn("rt_k", names)
        self.assertNotIn("stk_premarket", names)
        self.assertNotIn("limit_step", names)
        deprecated = {item["dataset"] for item in datasets if item["status"] == "deprecated"}
        self.assertIn("stk_account", deprecated)
        self.assertIn("slb_sec", deprecated)

    def test_general_dataset_registry_covers_common_cross_domain_allowlist(self) -> None:
        datasets = list_tushare_datasets()
        names = {item["dataset"] for item in datasets}
        domains = {item["domain"] for item in datasets}

        self.assertIn("ETF专题", domains)
        self.assertIn("指数专题", domains)
        self.assertIn("宏观经济", domains)
        self.assertIn("大模型语料专题数据", domains)
        self.assertIn("港股数据", domains)
        self.assertIn("美股数据", domains)
        self.assertIn("fund_daily", names)
        self.assertIn("index_daily", names)
        self.assertIn("cn_cpi", names)
        self.assertIn("news", names)
        self.assertIn("hk_daily", names)
        self.assertIn("us_daily", names)
        self.assertNotIn("limit_step", names)
        self.assertNotIn("fut_daily", names)

    def test_fetch_stock_dataset_normalizes_params_fields_and_limit(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = FakeStockDatasetPro()
        provider.settings = SimpleNamespace()

        payload = provider.fetch_stock_dataset(
            "daily",
            {"ts_code": "000001", "trade_date": "2026-05-20", "ignored": "drop-me"},
            fields=["ts_code", "close", "volatility", "bad-name", "close"],
            limit=1,
        )

        self.assertEqual(provider.pro.daily_calls[0]["ts_code"], "000001.SZ")
        self.assertEqual(provider.pro.daily_calls[0]["trade_date"], "20260520")
        self.assertEqual(provider.pro.daily_calls[0]["fields"], "ts_code,close")
        self.assertNotIn("ignored", provider.pro.daily_calls[0])
        self.assertNotIn("volatility", provider.pro.daily_calls[0]["fields"])
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["returned_row_count"], 1)
        self.assertEqual(payload["rows"][0]["close"], 10.0)
        self.assertEqual(payload["data_source"], "tushare_daily")

    def test_fetch_general_dataset_supports_common_cross_domain_interfaces(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = FakeStockDatasetPro()
        provider.settings = SimpleNamespace()

        payload = provider.fetch_dataset(
            "index_daily",
            {"ts_code": "000001.SH", "trade_date": "2026-05-20", "unused": "drop"},
            fields=["ts_code", "trade_date", "close", "bad_field"],
            limit=1,
        )

        self.assertEqual(provider.pro.index_daily_calls[0]["ts_code"], "000001.SH")
        self.assertEqual(provider.pro.index_daily_calls[0]["trade_date"], "20260520")
        self.assertEqual(provider.pro.index_daily_calls[0]["fields"], "ts_code,trade_date,close")
        self.assertNotIn("unused", provider.pro.index_daily_calls[0])
        self.assertEqual(payload["domain"], "指数专题")
        self.assertEqual(payload["returned_row_count"], 1)
        self.assertEqual(payload["data_source"], "tushare_index_daily")

    def test_fetch_stock_dataset_rejects_unsupported_dataset(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = FakeStockDatasetPro()
        provider.settings = SimpleNamespace()

        with self.assertRaises(ValueError):
            provider.fetch_stock_dataset("rt_k", {})

    def test_fetch_stock_dataset_returns_structured_unavailable_without_token(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = None
        provider.settings = SimpleNamespace()

        payload = provider.fetch_stock_dataset("daily", {"ts_code": "000001"})

        self.assertEqual(payload["data_source"], "unavailable")
        self.assertEqual(payload["rows"], [])
        self.assertIn("tushare:unavailable", payload["missing_fields"])

    def test_init_passes_tushare_timeout_to_sdk(self) -> None:
        settings = SimpleNamespace(
            tushare_token="token",
            tushare_timeout_seconds=45,
            tickflow_api_key="key",
            tickflow_base_url="https://api.tickflow.org",
        )

        with patch("sats.data.tushare_provider.ts.pro_api", return_value=FakePro()) as pro_api:
            provider = TushareDataProvider(settings)

        self.assertIsInstance(provider.pro, FakePro)
        pro_api.assert_called_once_with("token", timeout=45)

    def test_list_a_share_symbols_filters_active_sh_sz_bj_codes(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = FakePro()
        provider._stock_basic_cache = {}

        symbols = provider.list_a_share_symbols()

        self.assertEqual(symbols, ["000001.SZ", "430047.BJ", "600000.SH"])
        self.assertEqual(provider.pro.kwargs["list_status"], "L")
        self.assertEqual(provider._stock_basic("000001.SZ")["name"], "平安银行")

    def test_load_all_screening_inputs_fetches_daily_data_by_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeBatchPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(len(inputs), 1)
            self.assertEqual(inputs[0].ts_code, "000001.SZ")
            self.assertGreaterEqual(len(inputs[0].daily), 60)
            self.assertTrue(all("trade_date" in call for call in provider.pro.daily_calls))
            self.assertTrue(all("ts_code" not in call for call in provider.pro.daily_calls))
            self.assertEqual(len(provider.pro.daily_calls), len(provider.pro.trade_dates))
            self.assertEqual(len(provider.pro.daily_basic_calls), len(provider.pro.trade_dates))
            self.assertEqual(provider.pro.index_daily_calls, [])
            self.assertEqual(provider.pro.ths_index_calls, 0)
            self.assertEqual(provider.pro.ths_daily_calls, [])

    def test_load_index_daily_fetches_requested_indices(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = FakeBatchPro()
        provider._stock_basic_cache = {}

        frame = provider.load_index_daily(["000001.SH", "399006.SZ"], start_date="20260401", end_date="20260430")

        self.assertEqual(provider.pro.index_daily_calls, ["000001.SH", "399006.SZ"])
        self.assertEqual(set(frame["ts_code"]), {"000001.SH", "399006.SZ"})
        self.assertEqual(frame.attrs["data_source"], "tushare_index_daily")

    def test_load_hot_sector_context_fetches_scores_members_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeBatchPro()
            provider._stock_basic_cache = {}

            context = provider.load_hot_sector_context("20260430", storage=storage, lookback_days=5)

            self.assertEqual(provider.pro.ths_index_types, ["I", "N"])
            self.assertEqual(set(provider.pro.ths_daily_calls), {"881155.TI", "885001.TI", "885002.TI"})
            self.assertIn("885001.TI", provider.pro.ths_member_calls)
            self.assertEqual(context["hot_concepts"][0]["name"], "AI算力")
            self.assertGreater(
                context["hot_concepts"][0]["heat_score"],
                context["hot_concepts"][1]["heat_score"],
            )
            self.assertEqual(context["stock_hot_sectors"]["000938.SZ"][0]["name"], "AI算力")
            self.assertEqual(storage.get_sector_basic(sector_types=["concept"])["sector_code"].tolist(), ["885001.TI", "885002.TI"])
            self.assertFalse(storage.get_sector_daily(["885001.TI"], trade_dates=["20260430"]).empty)
            self.assertEqual(storage.get_sector_members(["885001.TI"]).iloc[0]["ts_code"], "000938.SZ")

            cached_provider = object.__new__(TushareDataProvider)
            cached_provider.pro = None
            cached_provider._stock_basic_cache = {}
            cached_context = cached_provider.load_hot_sector_context("20260430", storage=storage, lookback_days=5)

            self.assertEqual(cached_context["data_sources"]["sector_basic"], "duckdb_cache_or_unavailable")
            self.assertEqual(cached_context["stock_hot_sectors"]["000938.SZ"][0]["name"], "AI算力")

    def test_load_sw_sector_basic_and_members_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeBatchPro()
            provider._stock_basic_cache = {}

            basic = provider._load_sw_sector_basic(storage=storage)
            members = provider._load_sw_sector_members(["801780.SI", "801783.SI", "851911.SI"], storage=storage)

            self.assertEqual([call["level"] for call in provider.pro.index_classify_calls], ["L1", "L2", "L3"])
            self.assertTrue(all(call["src"] == "SW2021" for call in provider.pro.index_classify_calls))
            self.assertEqual(basic.attrs["data_source"], "tushare_sw_index_classify")
            self.assertEqual(
                basic[["sector_code", "name", "sector_type"]].values.tolist(),
                [
                    ["801780.SI", "银行", "sw_l1"],
                    ["801783.SI", "股份制银行Ⅱ", "sw_l2"],
                    ["851911.SI", "贸易Ⅲ", "sw_l3"],
                ],
            )
            self.assertEqual(
                provider.pro.index_member_all_calls,
                [{"l1_code": "801780.SI"}, {"l2_code": "801783.SI"}, {"l3_code": "851911.SI"}],
            )
            self.assertEqual(members.attrs["data_source"], "tushare_sw_index_member_all")
            self.assertEqual(members["ts_code"].tolist(), ["000001.SZ", "600000.SH", "600153.SH"])
            self.assertEqual(storage.get_sector_basic(sector_types=["sw_l3"]).iloc[0]["sector_code"], "851911.SI")
            self.assertEqual(storage.get_sector_members(["801780.SI"])["ts_code"].tolist(), ["000001.SZ"])

            cached_provider = object.__new__(TushareDataProvider)
            cached_provider.pro = None
            cached_provider._stock_basic_cache = {}
            cached_basic = cached_provider._load_sw_sector_basic(storage=storage)
            cached_members = cached_provider._load_sw_sector_members(["851911.SI"], storage=storage)

            self.assertEqual(cached_basic.attrs["data_source"], "duckdb_cache_or_unavailable")
            self.assertEqual(cached_members.attrs["data_source"], "duckdb_cache_or_unavailable")
            self.assertEqual(cached_members.iloc[0]["ts_code"], "600153.SH")

    def test_load_hot_sector_context_degrades_when_tushare_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeHotSectorUnavailablePro()
            provider._stock_basic_cache = {}

            context = provider.load_hot_sector_context(
                "20260430",
                storage=DuckDBStorage(Path(tmp) / "sats.duckdb"),
                lookback_days=5,
            )

            self.assertEqual(context["hot_industries"], [])
            self.assertEqual(context["hot_concepts"], [])
            self.assertIn("hot_sector_basic", context["missing_fields"])

    def test_load_limit_sentiment_counts_and_scores_limit_list_d(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = FakeBatchPro()
        provider._stock_basic_cache = {}

        payload = provider.load_limit_sentiment("20260430")

        self.assertEqual(provider.pro.limit_list_d_calls, ["U", "D", "Z"])
        self.assertEqual(payload["limit_up_count"], 90)
        self.assertEqual(payload["limit_down_count"], 2)
        self.assertEqual(payload["broken_limit_count"], 20)
        self.assertEqual(payload["market_coefficient"], 90.0)
        self.assertEqual(payload["ultra_short_sentiment"], 70.77)
        self.assertEqual(payload["loss_effect"], 60.0)
        self.assertEqual(payload["emotion_stage"], "冰点")
        self.assertEqual(payload["data_source"], "tushare_limit_list_d")

    def test_load_limit_sentiment_degrades_when_tushare_unavailable(self) -> None:
        provider = object.__new__(TushareDataProvider)
        provider.pro = FakeLimitListUnavailablePro()
        provider._stock_basic_cache = {}

        payload = provider.load_limit_sentiment("20260430")

        self.assertEqual(provider.pro.limit_list_d_calls, ["U", "D", "Z"])
        self.assertEqual(payload["data_source"], "unavailable")
        self.assertIn("limit_up_count", payload["missing_fields"][0])

    def test_load_all_screening_inputs_uses_cached_daily_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeBatchPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            provider.load_all_screening_inputs("20260430", storage=storage)
            provider.pro.daily_calls.clear()
            provider.pro.daily_basic_calls.clear()
            provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(provider.pro.daily_calls, [])
            self.assertEqual(provider.pro.daily_basic_calls, [])

    def test_missing_current_trade_date_data_raises_without_using_previous_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeMissingCurrentPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            with patch("sats.data.tushare_provider._is_today_shanghai", return_value=False):
                with self.assertRaisesRegex(ValueError, "未使用前一交易日数据"):
                    provider.load_all_screening_inputs("20260514", storage=storage)

    def test_missing_current_data_force_refreshes_daily_and_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeRefreshCurrentPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            inputs = provider.load_all_screening_inputs("20260514", storage=storage)

            self.assertEqual(inputs[0].daily["trade_date"].astype(str).max(), "20260514")
            self.assertEqual(inputs[0].daily_basic["trade_date"].astype(str).max(), "20260514")
            self.assertEqual(provider.pro.daily_by_date_calls["20260514"], 2)
            self.assertEqual(provider.pro.basic_by_date_calls["20260514"], 2)

    def test_missing_today_data_uses_realtime_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeRealtimeCurrentPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.minute_quote_calls = []
            FakeFallbackTickFlowProvider.fail_minute_quotes = False

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=True),
                patch("sats.data.tushare_provider._is_a_share_trading_time_now", return_value=False),
                patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider),
            ):
                inputs = provider.load_all_screening_inputs("20260514", storage=storage)

            latest_daily = inputs[0].daily.tail(1).iloc[0]
            latest_basic = inputs[0].daily_basic.tail(1).iloc[0]
            self.assertEqual(str(latest_daily["trade_date"]), "20260514")
            self.assertAlmostEqual(float(latest_daily["vol"]), 1200.0)
            self.assertAlmostEqual(float(latest_daily["amount"]), 162000.0)
            previous_close = float(inputs[0].daily.iloc[-2]["close"])
            self.assertAlmostEqual(float(latest_daily["pct_chg"]), (13.5 / previous_close - 1.0) * 100)
            self.assertEqual(str(latest_basic["trade_date"]), "20260514")
            self.assertAlmostEqual(float(latest_basic["turnover_rate"]), 1200.0 / 15_000.0)
            self.assertAlmostEqual(float(latest_basic["circ_mv"]), 13.5 * 15_000.0)
            self.assertEqual(inputs[0].metadata["data_source"], "tickflow_current_1d")
            self.assertEqual(
                FakeFallbackTickFlowProvider.minute_quote_calls,
                [{"symbols": ["000001.SZ"], "trade_date": "20260514", "period": "1d", "count": 1}],
            )
            self.assertEqual(len(provider.pro.premarket_calls), 1)

    def test_today_trading_hours_forces_realtime_even_with_cached_daily_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeForcedRealtimeCachedPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            provider.load_all_screening_inputs("20260430", storage=storage)
            stored_before = storage.get_stock_daily(["20260430"]).iloc[-1]["close"]
            provider.pro.daily_calls.clear()
            provider.pro.daily_basic_calls.clear()
            FakeFallbackTickFlowProvider.minute_quote_calls = []
            FakeFallbackTickFlowProvider.fail_minute_quotes = False

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=True),
                patch("sats.data.tushare_provider._is_a_share_trading_time_now", return_value=True),
                patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider),
            ):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            latest_daily = inputs[0].daily.tail(1).iloc[0]
            self.assertEqual(inputs[0].metadata["data_source"], "tickflow_current_1d")
            self.assertEqual(float(latest_daily["close"]), 13.5)
            self.assertEqual(
                FakeFallbackTickFlowProvider.minute_quote_calls,
                [{"symbols": ["000001.SZ"], "trade_date": "20260430", "period": "1d", "count": 1}],
            )
            self.assertEqual(len(provider.pro.premarket_calls), 1)
            self.assertEqual([call for call in provider.pro.daily_calls if call.get("trade_date") == "20260430"], [])
            stored_after = storage.get_stock_daily(["20260430"]).iloc[-1]["close"]
            self.assertEqual(stored_after, stored_before)

    def test_today_outside_trading_hours_uses_complete_cached_daily_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeForcedRealtimeCachedPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            provider.load_all_screening_inputs("20260430", storage=storage)

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=True),
                patch("sats.data.tushare_provider._is_a_share_trading_time_now", return_value=False),
            ):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(inputs[0].metadata["data_source"], "tushare_daily")

    def test_forced_realtime_failure_does_not_use_same_day_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeForcedRealtimeFailurePro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            provider.load_all_screening_inputs("20260430", storage=storage)
            FakeFallbackTickFlowProvider.minute_quote_calls = []
            FakeFallbackTickFlowProvider.fail_minute_quotes = True

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=True),
                patch("sats.data.tushare_provider._is_a_share_trading_time_now", return_value=True),
                patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider),
                self.assertRaisesRegex(ValueError, "当日日K批量获取失败"),
            ):
                provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(
                FakeFallbackTickFlowProvider.minute_quote_calls,
                [{"symbols": ["000001.SZ"], "trade_date": "20260430", "period": "1d", "count": 1}],
            )
            FakeFallbackTickFlowProvider.fail_minute_quotes = False

    def test_stock_basic_timeout_falls_back_to_tickflow_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeStockBasicTimeoutPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.stock_basic_calls = 0
            FakeFallbackTickFlowProvider.fail_stock_basic = False

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider):
                frame = provider._stock_basic_frame(storage=storage)

            cached = storage.get_stock_basic()
            self.assertEqual(FakeFallbackTickFlowProvider.stock_basic_calls, 1)
            self.assertEqual(frame["ts_code"].tolist(), ["000001.SZ"])
            self.assertEqual(cached["ts_code"].tolist(), ["000001.SZ"])

    def test_daily_timeout_falls_back_to_tickflow_daily(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeDailyTimeoutPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.daily_calls = []
            FakeFallbackTickFlowProvider.fail_daily = False

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(inputs[0].metadata["data_source"], "tickflow_daily")
            self.assertGreaterEqual(len(FakeFallbackTickFlowProvider.daily_calls), 1)
            self.assertFalse(storage.get_stock_daily(["20260430"]).empty)

    def test_tushare_and_tickflow_daily_fail_uses_same_day_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeDailyTimeoutPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_daily(make_passing_daily().assign(ts_code="000001.SZ"))
            storage.upsert_stock_daily_basic(make_daily_basic().assign(ts_code="000001.SZ"))
            FakeFallbackTickFlowProvider.daily_calls = []
            FakeFallbackTickFlowProvider.fail_daily = True

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(inputs[0].metadata["data_source"], "tushare_daily")
            self.assertEqual(inputs[0].daily["trade_date"].astype(str).max(), "20260430")
            FakeFallbackTickFlowProvider.fail_daily = False

    def test_tushare_and_tickflow_daily_fail_without_cache_raises_short_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeDailyTimeoutPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.fail_daily = True

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider):
                with self.assertRaisesRegex(ValueError, "本地同交易日缓存均不可用"):
                    provider.load_all_screening_inputs("20260430", storage=storage)

            FakeFallbackTickFlowProvider.fail_daily = False

    def test_daily_basic_timeout_uses_cache_for_dependent_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeDailyBasicTimeoutPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_daily_basic(make_daily_basic().assign(ts_code="000001.SZ"))

            inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(inputs[0].metadata["daily_basic_source"], "tushare_daily_basic")
            self.assertEqual(inputs[0].daily_basic["trade_date"].astype(str).max(), "20260430")

    def test_today_daily_basic_timeout_uses_tickflow_basic_like_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeDailyBasicTimeoutPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.daily_basic_like_calls = []
            FakeFallbackTickFlowProvider.fail_daily_basic_like = False

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=True),
                patch("sats.data.tushare_provider._is_a_share_trading_time_now", return_value=False),
                patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider),
            ):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(inputs[0].metadata["daily_basic_source"], "tickflow_realtime_basic_like")
            self.assertEqual(inputs[0].daily_basic["trade_date"].astype(str).max(), "20260430")
            self.assertAlmostEqual(float(inputs[0].daily_basic.tail(1).iloc[0]["turnover_rate"]), 1200.0 / 15_000.0)
            self.assertEqual(len(FakeFallbackTickFlowProvider.daily_basic_like_calls), 1)
            self.assertTrue(storage.get_stock_daily_basic(["20260430"]).empty)

    def test_historical_daily_basic_timeout_does_not_use_realtime_basic_like(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeDailyBasicTimeoutPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.daily_basic_like_calls = []

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=False),
                patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider),
            ):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(inputs[0].metadata["daily_basic_source"], "missing")
            self.assertTrue(inputs[0].daily_basic.empty)
            self.assertEqual(FakeFallbackTickFlowProvider.daily_basic_like_calls, [])

    def test_forced_realtime_tickflow_failure_without_cache_raises_short_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeForcedRealtimeFailurePro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.minute_quote_calls = []
            FakeFallbackTickFlowProvider.fail_minute_quotes = True

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=True),
                patch("sats.data.tushare_provider._is_a_share_trading_time_now", return_value=True),
                patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider),
            ):
                with self.assertRaisesRegex(ValueError, "当日日K批量获取失败"):
                    provider.load_all_screening_inputs("20260430", storage=storage)

            self.assertEqual(
                FakeFallbackTickFlowProvider.minute_quote_calls,
                [{"symbols": ["000001.SZ"], "trade_date": "20260430", "period": "1d", "count": 1}],
            )
            FakeFallbackTickFlowProvider.fail_minute_quotes = False

    def test_price_volume_ma_forced_realtime_uses_overlay_without_external_ma_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakePriceVolumeForcedRealtimePro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakePriceVolumeRealtimeTickFlowProvider.minute_quote_calls = []
            FakePriceVolumeRealtimeTickFlowProvider.fail_minute_quotes = False

            with (
                patch("sats.data.tushare_provider._is_today_shanghai", return_value=True),
                patch("sats.data.tushare_provider._is_a_share_trading_time_now", return_value=True),
                patch("sats.data.tushare_provider.ts.pro_bar", side_effect=AssertionError("must not call pro_bar")),
                patch("sats.data.tushare_provider.TickFlowDataProvider", FakePriceVolumeRealtimeTickFlowProvider),
            ):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="price_volume_ma")

            metadata = inputs[0].metadata["price_volume_ma"]
            self.assertEqual(inputs[0].metadata["data_source"], "tickflow_current_1d")
            self.assertTrue(metadata["selected"], metadata)
            self.assertEqual(metadata["selection_source"], "current+legacy")
            self.assertEqual(
                FakePriceVolumeRealtimeTickFlowProvider.minute_quote_calls,
                [{"symbols": ["000001.SZ"], "trade_date": "20260430", "period": "1d", "count": 1}],
            )
            self.assertEqual(provider.pro.pro_bar_calls, [])
            self.assertEqual([call for call in provider.pro.daily_calls if "ts_code" in call], [])

    def test_price_volume_ma_precomputes_current_and_legacy_union(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakePriceVolumeUnionPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            with patch("sats.data.tushare_provider.ts.pro_bar", side_effect=_fake_top_level_pro_bar):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="price_volume_ma")

            metadata = {item.ts_code: item.metadata["price_volume_ma"] for item in inputs}
            self.assertTrue(metadata["000001.SZ"]["selected"])
            self.assertEqual(metadata["000001.SZ"]["selection_source"], "current")
            self.assertTrue(metadata["000002.SZ"]["selected"])
            self.assertEqual(metadata["000002.SZ"]["selection_source"], "legacy")
            self.assertTrue(metadata["000003.SZ"]["selected"])
            self.assertEqual(metadata["000003.SZ"]["selection_source"], "current+legacy")
            self.assertFalse(metadata["000004.SZ"]["selected"])
            self.assertEqual(len(provider.pro.pro_bar_calls), 4)
            self.assertEqual(len([call for call in provider.pro.daily_calls if "ts_code" in call]), 4)

    def test_price_volume_ma_records_current_pro_bar_errors_without_blocking_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakePriceVolumeUnionPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            with patch("sats.data.tushare_provider.ts.pro_bar", side_effect=RuntimeError("pro_bar denied")):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="price_volume_ma")

            metadata = {item.ts_code: item.metadata["price_volume_ma"] for item in inputs}
            self.assertFalse(metadata["000001.SZ"]["selected"])
            self.assertTrue(metadata["000002.SZ"]["selected"])
            self.assertEqual(metadata["000002.SZ"]["selection_source"], "legacy")
            self.assertIn("current_ma_error", metadata["000002.SZ"]["raw_metrics"])
            self.assertIn("current_scan_error_summary", metadata["000002.SZ"]["raw_metrics"])
            self.assertEqual(metadata["000002.SZ"]["raw_metrics"]["current_ma_error"]["error"], "pro_bar denied")

    def test_price_volume_ma_current_logic_requires_recent_6_trade_day_volume_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakePriceVolumeMissingRecentVolumePro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="price_volume_ma")

            metadata = {item.ts_code: item.metadata["price_volume_ma"] for item in inputs}
            self.assertFalse(metadata["000010.SZ"]["current_logic_passed"])
            self.assertFalse(metadata["000010.SZ"]["selected"])
            self.assertEqual(provider.pro.pro_bar_calls, [])

    def test_monthly_base_breakout_prefers_tickflow_1m_and_skips_daily_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeBatchPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeMonthlyTickFlowProvider.monthly_calls = []
            FakeMonthlyTickFlowProvider.daily_calls = []
            FakeMonthlyTickFlowProvider.monthly_empty = False

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeMonthlyTickFlowProvider):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="monthly_base_breakout")

            self.assertEqual(FakeMonthlyTickFlowProvider.monthly_calls[0]["period"], "1M")
            self.assertEqual(FakeMonthlyTickFlowProvider.daily_calls, [])
            self.assertEqual(provider.pro.daily_basic_calls, [])
            self.assertEqual(inputs[0].metadata["monthly_1M_source"], "tickflow_1M")
            self.assertGreaterEqual(len(inputs[0].metadata["monthly_1M"]), 60)
            self.assertTrue(inputs[0].daily_basic.empty)

    def test_monthly_base_breakout_falls_back_to_daily_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeBatchPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeMonthlyTickFlowProvider.monthly_calls = []
            FakeMonthlyTickFlowProvider.daily_calls = []
            FakeMonthlyTickFlowProvider.monthly_empty = True

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeMonthlyTickFlowProvider):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="monthly_base_breakout")

            self.assertEqual(FakeMonthlyTickFlowProvider.monthly_calls[0]["period"], "1M")
            self.assertEqual(len(FakeMonthlyTickFlowProvider.daily_calls), 1)
            self.assertEqual(provider.pro.daily_basic_calls, [])
            self.assertEqual(inputs[0].metadata["monthly_1M_source"], "daily_aggregation:tickflow_daily")
            self.assertGreaterEqual(len(inputs[0].metadata["monthly_1M"]), 60)
            FakeMonthlyTickFlowProvider.monthly_empty = False

    def test_chan_third_buy_fetches_30m_only_for_daily_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeChanThirdBuyPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeChanTickFlowProvider.calls = []
            FakeChanTickFlowProvider.fail_realtime = False
            FakeChanTickFlowProvider.history_end = "20260430"
            FakeChanTickFlowProvider.realtime_end = "20260430"

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeChanTickFlowProvider):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="chan_third_buy")

            minute_by_code = {item.ts_code: item.metadata["minute_30m"] for item in inputs}
            self.assertEqual(len(FakeChanTickFlowProvider.calls), 1)
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["mode"], "history")
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["period"], "30m")
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["symbols"], ["000001.SZ"])
            self.assertEqual(provider.pro.daily_basic_calls, [])
            self.assertTrue(all(item.daily_basic.empty for item in inputs))
            self.assertTrue(all(item.metadata["daily_basic_source"] == "skipped_for_chan_third_buy" for item in inputs))
            self.assertFalse(minute_by_code["000001.SZ"].empty)
            self.assertTrue(minute_by_code["000002.SZ"].empty)

    def test_chan_composite_fetches_30m_only_for_daily_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeChanThirdBuyPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeChanTickFlowProvider.calls = []
            FakeChanTickFlowProvider.fail_realtime = False
            FakeChanTickFlowProvider.history_end = "20260430"
            FakeChanTickFlowProvider.realtime_end = "20260430"

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeChanTickFlowProvider):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="chan_composite")

            minute_by_code = {item.ts_code: item.metadata["minute_30m"] for item in inputs}
            candidate_by_code = {item.ts_code: item.metadata["chan_daily_candidates"] for item in inputs}
            self.assertEqual(len(FakeChanTickFlowProvider.calls), 1)
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["mode"], "history")
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["period"], "30m")
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["symbols"], ["000001.SZ"])
            self.assertEqual(provider.pro.daily_basic_calls, [])
            self.assertTrue(all(item.daily_basic.empty for item in inputs))
            self.assertTrue(all(item.metadata["daily_basic_source"] == "skipped_for_chan_third_buy" for item in inputs))
            self.assertIn("chan_third_buy", candidate_by_code["000001.SZ"])
            self.assertEqual(candidate_by_code["000002.SZ"], [])
            self.assertFalse(minute_by_code["000001.SZ"].empty)
            self.assertTrue(minute_by_code["000002.SZ"].empty)

    def test_chan_signals_fetches_30m_only_for_daily_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeChanThirdBuyPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeChanTickFlowProvider.calls = []
            FakeChanTickFlowProvider.fail_realtime = False
            FakeChanTickFlowProvider.history_end = "20260430"
            FakeChanTickFlowProvider.realtime_end = "20260430"

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeChanTickFlowProvider):
                inputs = provider.load_all_screening_inputs("20260430", storage=storage, rule_name="chan_signals")

            minute_by_code = {item.ts_code: item.metadata["minute_30m"] for item in inputs}
            candidate_by_code = {item.ts_code: item.metadata["chan_daily_candidates"] for item in inputs}
            self.assertEqual(len(FakeChanTickFlowProvider.calls), 1)
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["mode"], "history")
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["period"], "30m")
            self.assertEqual(FakeChanTickFlowProvider.calls[0]["symbols"], ["000001.SZ"])
            self.assertEqual(provider.pro.daily_basic_calls, [])
            self.assertTrue(all(item.daily_basic.empty for item in inputs))
            self.assertIn("chan_third_buy", candidate_by_code["000001.SZ"])
            self.assertEqual(candidate_by_code["000002.SZ"], [])
            self.assertFalse(minute_by_code["000001.SZ"].empty)
            self.assertTrue(minute_by_code["000002.SZ"].empty)

    def test_chan_third_buy_forced_realtime_skips_realtime_daily_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakeForcedRealtimeCachedPro()
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            FakeFallbackTickFlowProvider.minute_quote_calls = []
            FakeFallbackTickFlowProvider.fail_minute_quotes = False

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeFallbackTickFlowProvider):
                data_source = provider._ensure_current_trade_date_data(
                    storage,
                    "20260430",
                    _stock_basic_rows(["000001.SZ"]),
                    force_realtime=True,
                    require_daily_basic=False,
                )

            self.assertEqual(data_source["daily_basic_source"], "skipped_for_chan_third_buy")
            self.assertIsNone(data_source["realtime_basic"])
            self.assertEqual(provider.pro.premarket_calls, [])

    def test_chan_composite_realtime_combines_history_and_intraday_30m(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            stock_basic = _stock_basic_rows(["000001.SZ"])
            daily_groups = {"000001.SZ": make_chan_daily(end="20260430").assign(ts_code="000001.SZ")}
            FakeChanTickFlowProvider.calls = []
            FakeChanTickFlowProvider.fail_realtime = False
            FakeChanTickFlowProvider.history_end = "20260429"
            FakeChanTickFlowProvider.realtime_end = "20260430"

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeChanTickFlowProvider):
                metadata = provider._build_rule_intraday_metadata(
                    rule=get_rule("chan_composite"),
                    stock_basic=stock_basic,
                    daily_groups=daily_groups,
                    trade_date="20260430",
                    use_realtime=True,
                    data_source="tickflow_current_1d",
                )

            item = metadata["000001.SZ"]
            self.assertEqual([call["mode"] for call in FakeChanTickFlowProvider.calls], ["history", "current"])
            self.assertEqual(item["minute_30m_source"], "tickflow_current_30m")
            self.assertIn("chan_third_buy", item["chan_daily_candidates"])
            self.assertEqual(item["minute_30m"]["trade_date"].astype(str).max(), "20260430")
            FakeChanTickFlowProvider.history_end = "20260430"

    def test_chan_third_buy_realtime_combines_history_and_intraday_30m(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            stock_basic = _stock_basic_rows(["000001.SZ"])
            daily_groups = {"000001.SZ": make_chan_daily(end="20260430").assign(ts_code="000001.SZ")}
            FakeChanTickFlowProvider.calls = []
            FakeChanTickFlowProvider.fail_realtime = False
            FakeChanTickFlowProvider.history_end = "20260429"
            FakeChanTickFlowProvider.realtime_end = "20260430"

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeChanTickFlowProvider):
                metadata = provider._build_rule_intraday_metadata(
                    rule=get_rule("chan_third_buy"),
                    stock_basic=stock_basic,
                    daily_groups=daily_groups,
                    trade_date="20260430",
                    use_realtime=True,
                    data_source="tickflow_current_1d",
                )

            item = metadata["000001.SZ"]
            self.assertEqual([call["mode"] for call in FakeChanTickFlowProvider.calls], ["history", "current"])
            self.assertEqual(item["minute_30m_source"], "tickflow_current_30m")
            self.assertEqual(item["minute_30m"]["trade_date"].astype(str).max(), "20260430")
            FakeChanTickFlowProvider.history_end = "20260430"

    def test_chan_third_buy_realtime_failure_does_not_use_minute_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            stock_basic = _stock_basic_rows(["000001.SZ"])
            daily_groups = {"000001.SZ": make_chan_daily(end="20260430").assign(ts_code="000001.SZ")}
            FakeChanTickFlowProvider.calls = []
            FakeChanTickFlowProvider.fail_realtime = True
            FakeChanTickFlowProvider.history_end = "20260429"

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeChanTickFlowProvider):
                with self.assertRaisesRegex(ValueError, "当日 30m K线获取失败"):
                    provider._build_rule_intraday_metadata(
                        rule=get_rule("chan_third_buy"),
                        stock_basic=stock_basic,
                        daily_groups=daily_groups,
                        trade_date="20260430",
                        use_realtime=True,
                        data_source="tickflow_current_1d",
                    )

            FakeChanTickFlowProvider.fail_realtime = False
            FakeChanTickFlowProvider.history_end = "20260430"

    def test_chan_third_buy_realtime_failure_without_cache_raises_short_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.settings = SimpleNamespace(tickflow_api_key="key", tickflow_base_url="https://api.tickflow.org")
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            stock_basic = _stock_basic_rows(["000001.SZ"])
            daily_groups = {"000001.SZ": make_chan_daily(end="20260430").assign(ts_code="000001.SZ")}
            FakeChanTickFlowProvider.calls = []
            FakeChanTickFlowProvider.fail_realtime = True
            FakeChanTickFlowProvider.history_end = "20260429"

            with patch("sats.data.tushare_provider.TickFlowDataProvider", FakeChanTickFlowProvider):
                with self.assertRaisesRegex(ValueError, "当日 30m K线获取失败"):
                    provider._build_rule_intraday_metadata(
                        rule=get_rule("chan_third_buy"),
                        stock_basic=stock_basic,
                        daily_groups=daily_groups,
                        trade_date="20260430",
                        use_realtime=True,
                        data_source="tickflow_current_1d",
                    )

            FakeChanTickFlowProvider.fail_realtime = False
            FakeChanTickFlowProvider.history_end = "20260430"

    def test_other_rules_do_not_trigger_price_volume_extra_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = object.__new__(TushareDataProvider)
            provider.pro = FakePriceVolumeUnionPro()
            provider._stock_basic_cache = {}
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            with patch("sats.data.tushare_provider.TickFlowDataProvider", side_effect=AssertionError("must not call TickFlow")):
                provider.load_all_screening_inputs("20260430", storage=storage, rule_name="ma_volume_relative_strength")

            self.assertEqual(provider.pro.pro_bar_calls, [])
            self.assertEqual([call for call in provider.pro.daily_calls if "ts_code" in call], [])


if __name__ == "__main__":
    unittest.main()
