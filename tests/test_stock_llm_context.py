from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.analysis.stock_llm_context import build_stock_llm_context, ensure_stock_analysis_data
from sats.indicators import IndicatorInput
from sats.storage.duckdb import DuckDBStorage


def _daily(symbol: str = "002436.SZ", *, end: str = "20260515") -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=200).strftime("%Y%m%d")
    rows = []
    for index, date in enumerate(dates, start=1):
        close = 20 + index * 0.1
        rows.append(
            {
                "ts_code": symbol,
                "trade_date": date,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "vol": 1000 + index,
                "amount": 2000 + index,
                "pct_chg": 0.5,
            }
        )
    return pd.DataFrame(rows)


def _minute(symbol: str = "002436.SZ", *, period: str = "15m") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": symbol,
                "period": period,
                "trade_date": "20260515",
                "trade_time": "2026-05-15 10:15:00",
                "open": 34.0,
                "high": 34.2,
                "low": 33.8,
                "close": 34.1,
                "vol": 100,
                "amount": 200,
                "data_source": "tickflow_history",
            },
            {
                "ts_code": symbol,
                "period": period,
                "trade_date": "20260515",
                "trade_time": "2026-05-15 10:45:00",
                "open": 35.0,
                "high": 35.2,
                "low": 34.8,
                "close": 35.1,
                "vol": 120,
                "amount": 220,
                "data_source": "tickflow_history",
            },
        ]
    )


class _FakeTickFlowProvider:
    historical_calls: list[dict] = []

    def __init__(self, settings) -> None:
        self.settings = settings

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        return [
            IndicatorInput(
                ts_code=symbol,
                trade_date=trade_date,
                daily=_daily(symbol, end=trade_date),
                daily_basic=pd.DataFrame([{"ts_code": symbol, "trade_date": trade_date, "turnover_rate": 3.2}]),
                stock_basic={"ts_code": symbol, "name": "兴森科技"},
                data_sources={"daily": "tickflow_daily", "daily_basic": "tickflow_realtime_basic_like"},
            )
            for symbol in symbols
        ]

    def load_historical_minute_klines(self, symbols, *, period="1m", start_time=None, end_time=None, count=None, storage=None):
        type(self).historical_calls.append({"period": period, "start_time": start_time, "end_time": end_time})
        frame = pd.concat([_minute(symbol, period=period) for symbol in symbols], ignore_index=True)
        if storage is not None:
            storage.upsert_stock_minute(frame)
        return frame

    def load_realtime_minute_klines(self, symbols, *, period="1m", count=None, storage=None):
        frame = pd.concat([_minute(symbol, period=period) for symbol in symbols], ignore_index=True)
        if storage is not None:
            storage.upsert_stock_minute(frame)
        return frame

    def load_realtime_quotes(self, *, symbols):
        return pd.DataFrame(
            [{"ts_code": symbol, "trade_date": "20260515", "close": 34.56, "data_source": "tickflow_quote"} for symbol in symbols]
        )


class _EmptyTushareProvider:
    def __init__(self, settings) -> None:
        self.settings = settings

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        return []


class _BrokenTickFlowProvider(_FakeTickFlowProvider):
    def load_historical_minute_klines(self, symbols, *, period="1m", start_time=None, end_time=None, count=None, storage=None):
        raise RuntimeError("tickflow minute failed")

    def load_realtime_minute_klines(self, symbols, *, period="1m", count=None, storage=None):
        raise RuntimeError("tickflow minute failed")


class StockLLMContextTest(unittest.TestCase):
    def test_context_contains_real_indicator_and_15m_30m_curves_filtered_by_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            _FakeTickFlowProvider.historical_calls = []

            with (
                patch("sats.data.astock_provider.TickFlowDataProvider", _FakeTickFlowProvider),
                patch("sats.data.astock_provider.TushareDataProvider", _EmptyTushareProvider),
            ):
                context = build_stock_llm_context(
                    "看002436 MACD 20260515 10:30",
                    settings=settings,
                    storage=DuckDBStorage(settings.db_path),
                )

            self.assertIsNotNone(context)
            payload = context.payload
            stock = payload["stocks"][0]
            self.assertEqual(payload["trade_date"], "20260515")
            self.assertEqual(payload["as_of_time"], "2026-05-15 10:30:00")
            self.assertEqual(stock["ts_code"], "002436.SZ")
            self.assertEqual(stock["name"], "兴森科技")
            self.assertIn("indicator_result", stock)
            self.assertIn("15m", stock["minute_curves"])
            self.assertIn("30m", stock["minute_curves"])
            self.assertEqual([row["trade_time"] for row in stock["minute_curves"]["15m"]["rows"]], ["2026-05-15 10:15:00"])
            self.assertTrue(all(call["end_time"] == "2026-05-15 10:30:00" for call in _FakeTickFlowProvider.historical_calls))
            self.assertIn("不得编造价格", context.system_message)

    def test_ensure_stock_analysis_data_fetches_and_caches_15m_30m(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)

            with (
                patch("sats.data.astock_provider.TickFlowDataProvider", _FakeTickFlowProvider),
                patch("sats.data.astock_provider.TushareDataProvider", _EmptyTushareProvider),
            ):
                contexts = ensure_stock_analysis_data(["002436"], "20260515", settings=settings, storage=storage)

            self.assertIn("002436.SZ", contexts)
            self.assertIn("15m", contexts["002436.SZ"]["minute_curves"])
            self.assertIn("30m", contexts["002436.SZ"]["minute_curves"])
            self.assertFalse(storage.get_stock_minute(symbols=["002436.SZ"], period="15m", trade_date="20260515").empty)
            self.assertFalse(storage.get_stock_minute(symbols=["002436.SZ"], period="30m", trade_date="20260515").empty)

    def test_context_uses_same_day_minute_cache_after_tickflow_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_stock_minute(_minute(period="15m"))
            storage.upsert_stock_minute(_minute(period="30m"))

            with (
                patch("sats.data.astock_provider.TickFlowDataProvider", _BrokenTickFlowProvider),
                patch("sats.data.astock_provider.TushareDataProvider", _EmptyTushareProvider),
            ):
                context = build_stock_llm_context("分析002436 20260515", settings=settings, storage=storage)

            stock = context.payload["stocks"][0]
            self.assertEqual(stock["minute_curves"]["15m"]["source"], "duckdb_minute_cache_after_provider_failure")
            self.assertEqual(stock["minute_curves"]["30m"]["source"], "duckdb_minute_cache_after_provider_failure")

    def test_context_failure_stops_before_llm_when_minute_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")

            with (
                patch("sats.data.astock_provider.TickFlowDataProvider", _BrokenTickFlowProvider),
                patch("sats.data.astock_provider.TushareDataProvider", _EmptyTushareProvider),
            ):
                with self.assertRaises(ValueError) as raised:
                    build_stock_llm_context("分析002436 20260515", settings=settings, storage=DuckDBStorage(settings.db_path))

            self.assertIn("真实 15m 分钟K数据", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
