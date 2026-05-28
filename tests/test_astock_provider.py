from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.data.astock_provider import AStockDataProvider
from sats.data.limit_sentiment import classify_limit_sentiment_stage
from sats.indicators import IndicatorInput
from sats.storage.duckdb import DuckDBStorage


def _settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(db_path=db_path)


def _daily_frame(symbols: list[str], *, trade_date: str = "20260520", close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": symbol,
                "trade_date": trade_date,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "vol": 1000.0,
                "amount": 10000.0,
                "pct_chg": 1.2,
            }
            for symbol in symbols
        ]
    )


def _basic_frame(symbol: str = "000001.SZ", *, trade_date: str = "20260520") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": symbol,
                "trade_date": trade_date,
                "turnover_rate": 2.3,
                "pe": 12.0,
                "pb": 1.1,
                "circ_mv": 120000.0,
            }
        ]
    )


class _TickFlowBackend:
    def __init__(self) -> None:
        self.quote_calls = 0
        self.daily_calls = 0
        self.indicator_calls = 0
        self.stock_basic_calls = 0

    def load_universe_symbols(self, universe_id):
        return ["600000.SH", "000001.SZ"]

    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        self.quote_calls += 1
        if universe_id:
            return pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "close": 10.0, "pct_chg": 1.0, "amount": 100.0},
                    {"ts_code": "000002.SZ", "close": 9.0, "pct_chg": -0.5, "amount": 80.0},
                ]
            )
        return pd.DataFrame(
            [
                {"ts_code": symbol, "close": 10.0, "pct_chg": 1.0, "amount": 100.0}
                for symbol in symbols
            ]
        )

    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        self.daily_calls += 1
        frame = _daily_frame(list(symbols), trade_date=str(end_date or "20260520"), close=11.0)
        frame.attrs["data_source"] = "tickflow_daily"
        return frame

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        self.indicator_calls += 1
        return [
            IndicatorInput(
                ts_code=symbol,
                trade_date=trade_date,
                daily=_daily_frame([symbol], trade_date=trade_date, close=12.0),
                daily_basic=pd.DataFrame(),
                stock_basic={"name": "TickFlowName"},
                data_sources={"daily": "tickflow_daily"},
            )
            for symbol in symbols
        ]

    def load_stock_basic(self, *, storage=None):
        self.stock_basic_calls += 1
        frame = pd.DataFrame(
            [
                {
                    "ts_code": "000938.SZ",
                    "symbol": "000938",
                    "name": "紫光股份",
                    "industry": "计算机",
                    "market": "主板",
                    "exchange": "SZSE",
                    "list_date": "19991104",
                }
            ]
        )
        if storage is not None:
            storage.upsert_stock_basic(frame)
        return frame


class _PartialTickFlowBackend(_TickFlowBackend):
    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        self.daily_calls += 1
        frame = _daily_frame([symbols[0]], trade_date=str(end_date or "20260520"), close=11.0)
        frame.attrs["data_source"] = "tickflow_daily"
        return frame


class _BrokenTickFlowBackend(_TickFlowBackend):
    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        self.quote_calls += 1
        raise RuntimeError("tickflow unavailable")

    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        self.daily_calls += 1
        raise RuntimeError("tickflow unavailable")


class _TushareBackend:
    def __init__(self) -> None:
        self.symbol_calls = 0
        self.index_calls = 0
        self.indicator_calls = 0
        self.hot_sector_calls = 0
        self.stock_basic_calls = 0
        self.limit_sentiment_calls = 0

    def list_a_share_symbols(self):
        self.symbol_calls += 1
        return ["000001.SZ"]

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        self.indicator_calls += 1
        return [
            IndicatorInput(
                ts_code=symbol,
                trade_date=trade_date,
                daily=_daily_frame([symbol], trade_date=trade_date, close=9.0),
                daily_basic=_basic_frame(symbol, trade_date=trade_date),
                moneyflow=pd.DataFrame([{"ts_code": symbol, "trade_date": trade_date, "main_net_amount": 10.0}]),
                fundamentals=pd.DataFrame([{"ts_code": symbol, "end_date": trade_date, "roe": 8.0}]),
                stock_basic={"name": "TushareName", "industry": "银行"},
                data_sources={
                    "daily": "tushare_daily",
                    "daily_basic": "tushare_daily_basic",
                    "moneyflow": "tushare_moneyflow_dc",
                    "fundamentals": "tushare_fundamentals",
                },
            )
            for symbol in symbols
        ]

    def load_index_daily(self, index_codes, *, start_date, end_date):
        self.index_calls += 1
        frame = _daily_frame(list(index_codes), trade_date=end_date, close=3200.0)
        frame.attrs["data_source"] = "tushare_index_daily"
        return frame

    def load_hot_sector_context(self, trade_date, *, storage, lookback_days=5, top_industries=10, top_concepts=20):
        self.hot_sector_calls += 1
        return {
            "trade_date": trade_date,
            "lookback_days": lookback_days,
            "hot_industries": [{"sector_code": "881155.TI", "name": "银行"}],
            "hot_concepts": [],
            "stock_hot_sectors": {"000001.SZ": [{"name": "银行", "score": 8.0}]},
            "missing_fields": [],
            "data_sources": {"hot_sector": "tushare_ths"},
        }

    def load_limit_sentiment(self, trade_date, storage=None):
        self.limit_sentiment_calls += 1
        return {
            "trade_date": trade_date,
            "limit_up_count": 81,
            "limit_down_count": 1,
            "broken_limit_count": 2,
            "market_coefficient": 81.0,
            "ultra_short_sentiment": 62.46,
            "loss_effect": 10.0,
            "emotion_stage": "高潮",
            "stage_advice": "卖在高潮，注意兑现和仓位收缩。",
            "data_source": "tushare_limit_list_d",
            "missing_fields": [],
        }

    def load_stock_basic(self, *, storage=None):
        self.stock_basic_calls += 1
        return pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "symbol": "600519",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "market": "主板",
                    "exchange": "SSE",
                    "list_date": "20010827",
                }
            ]
        )


class _AkShareBackend:
    def __init__(self) -> None:
        self.quote_calls = 0
        self.breadth_calls = 0
        self.chip_calls = 0

    def load_realtime_quotes(self, symbols):
        self.quote_calls += 1
        return pd.DataFrame(
            [
                {"ts_code": symbol, "close": 8.0, "pct_chg": -1.0, "amount": 60.0}
                for symbol in symbols
            ]
        )

    def load_a_share_realtime_quotes(self):
        self.breadth_calls += 1
        return pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "close": 10.0, "pct_chg": 1.0, "amount": 100.0},
                {"ts_code": "000002.SZ", "close": 9.0, "pct_chg": -0.5, "amount": 80.0},
                {"ts_code": "600000.SH", "close": 8.0, "pct_chg": 10.0, "amount": 120.0},
            ]
        )

    def load_chip_context(self, symbols):
        self.chip_calls += 1
        return {symbols[0]: {"profit_ratio": 0.55, "data_source": "akshare_stock_cyq_em"}}


class _EmptyLimitSentimentTushareBackend(_TushareBackend):
    def load_limit_sentiment(self, trade_date, storage=None):
        self.limit_sentiment_calls += 1
        return {
            "trade_date": trade_date,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "broken_limit_count": 0,
            "market_coefficient": 0.0,
            "ultra_short_sentiment": 0.0,
            "loss_effect": 0.0,
            "emotion_stage": "退潮",
            "stage_advice": "退潮期控制仓位，避免追高。",
            "data_source": "tushare_limit_list_d",
            "missing_fields": ["limit_sentiment:tushare_empty"],
        }


class _LimitQuoteTickFlowBackend(_TickFlowBackend):
    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        self.quote_calls += 1
        if universe_id:
            return pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "close": 10.0, "pct_chg": 10.0, "amount": 100.0},
                    {"ts_code": "000002.SZ", "close": 9.0, "pct_chg": -10.0, "amount": 80.0},
                    {"ts_code": "600000.SH", "close": 8.0, "pct_chg": 1.0, "amount": 120.0},
                ]
            )
        return super().load_realtime_quotes(symbols=symbols, universe_id=universe_id)


class AStockDataProviderTest(unittest.TestCase):
    def test_limit_sentiment_stage_classification(self) -> None:
        self.assertEqual(classify_limit_sentiment_stage(90, 20, 10), "高潮")
        self.assertEqual(classify_limit_sentiment_stage(10, 20, 10), "退潮")
        self.assertEqual(classify_limit_sentiment_stage(65, 20, 10), "强势")
        self.assertEqual(classify_limit_sentiment_stage(40, 30, 10), "正常")
        self.assertEqual(classify_limit_sentiment_stage(90, 90, 40), "冰冰点")
        self.assertEqual(classify_limit_sentiment_stage(90, 90, 60), "冰点")

    def test_realtime_quotes_prefer_tickflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tickflow = _TickFlowBackend()
            akshare = _AkShareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=tickflow,
                tushare_provider=_TushareBackend(),
                akshare_provider=akshare,
            )

            frame = provider.load_realtime_quotes(symbols=["000001"])

            self.assertEqual(frame.attrs["data_source"], "tickflow_quote")
            self.assertEqual(frame.iloc[0]["ts_code"], "000001.SZ")
            self.assertEqual(tickflow.quote_calls, 1)
            self.assertEqual(akshare.quote_calls, 0)

    def test_realtime_quotes_fall_back_to_akshare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            akshare = _AkShareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_BrokenTickFlowBackend(),
                tushare_provider=_TushareBackend(),
                akshare_provider=akshare,
            )

            frame = provider.load_realtime_quotes(symbols=["000001"])

            self.assertEqual(frame.attrs["data_source"], "akshare_spot_em")
            self.assertEqual(frame.iloc[0]["close"], 8.0)
            self.assertEqual(akshare.quote_calls, 1)

    def test_daily_klines_prefer_tickflow_before_tushare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tickflow = _TickFlowBackend()
            tushare = _TushareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=tickflow,
                tushare_provider=tushare,
            )

            frame = provider.load_historical_daily_klines(["000001.SZ"], start_date="20260501", end_date="20260520")

            self.assertEqual(frame.attrs["data_source"], "tickflow_daily")
            self.assertEqual(float(frame.iloc[0]["close"]), 11.0)
            self.assertEqual(tickflow.daily_calls, 1)
            self.assertEqual(tushare.indicator_calls, 0)

    def test_daily_klines_fall_back_to_tushare_indicator_daily(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_BrokenTickFlowBackend(),
                tushare_provider=_TushareBackend(),
            )

            frame = provider.load_historical_daily_klines(["000001.SZ"], start_date="20260501", end_date="20260520")

            self.assertEqual(frame.attrs["data_source"], "tushare_daily")
            self.assertEqual(float(frame.iloc[0]["close"]), 9.0)

    def test_indicator_inputs_merge_tickflow_prices_with_tushare_fundamentals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=_TushareBackend(),
            )

            items = provider.load_indicator_inputs(["000001.SZ"], "20260520", storage=DuckDBStorage(Path(tmp) / "sats.duckdb"))

            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(float(item.daily.iloc[0]["close"]), 12.0)
            self.assertFalse(item.daily_basic.empty)
            self.assertFalse(item.moneyflow.empty)
            self.assertFalse(item.fundamentals.empty)
            self.assertEqual(item.stock_basic["name"], "TushareName")
            self.assertEqual(item.data_sources["daily"], "tickflow_daily")
            self.assertEqual(item.data_sources["daily_basic"], "tushare_daily_basic")

    def test_index_daily_fills_tickflow_missing_symbols_from_tushare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_PartialTickFlowBackend(),
                tushare_provider=_TushareBackend(),
            )

            frame = provider.load_index_daily(["000001.SH", "399001.SZ"], start_date="20260501", end_date="20260520")

            self.assertEqual(sorted(frame["ts_code"].unique().tolist()), ["000001.SH", "399001.SZ"])
            self.assertEqual(frame.attrs["data_source"], "tickflow_index_daily+tushare_index_daily")

    def test_market_breadth_falls_back_to_akshare_and_uses_legacy_field_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_BrokenTickFlowBackend(),
                tushare_provider=_TushareBackend(),
                akshare_provider=_AkShareBackend(),
            )

            breadth, source = provider.load_market_breadth()

            self.assertEqual(source, "akshare_spot_em")
            self.assertEqual(breadth["advancing_count"], 2)
            self.assertEqual(breadth["declining_count"], 1)
            self.assertEqual(breadth["up_count"], breadth["advancing_count"])

    def test_hot_sector_context_delegates_to_tushare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tushare = _TushareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=tushare,
            )

            payload = provider.load_hot_sector_context("20260520", storage=DuckDBStorage(Path(tmp) / "sats.duckdb"))

            self.assertEqual(payload["hot_industries"][0]["name"], "银行")
            self.assertEqual(tushare.hot_sector_calls, 1)

    def test_limit_sentiment_prefers_tushare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tushare = _TushareBackend()
            tickflow = _LimitQuoteTickFlowBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=tickflow,
                tushare_provider=tushare,
            )

            payload = provider.load_limit_sentiment("20260520")

            self.assertEqual(payload["data_source"], "tushare_limit_list_d")
            self.assertEqual(payload["emotion_stage"], "高潮")
            self.assertEqual(tushare.limit_sentiment_calls, 1)
            self.assertEqual(tickflow.quote_calls, 0)

    def test_limit_sentiment_falls_back_to_realtime_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tushare = _EmptyLimitSentimentTushareBackend()
            tickflow = _LimitQuoteTickFlowBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=tickflow,
                tushare_provider=tushare,
            )

            payload = provider.load_limit_sentiment("20260520")

            self.assertEqual(payload["limit_up_count"], 1)
            self.assertEqual(payload["limit_down_count"], 1)
            self.assertEqual(payload["broken_limit_count"], 0)
            self.assertIn("realtime_quote_fallback", payload["data_source"])
            self.assertIn("broken_limit_count:tushare_unavailable", payload["missing_fields"])

    def test_stock_basic_prefers_tickflow_and_writes_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tickflow = _TickFlowBackend()
            tushare = _TushareBackend()
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=tickflow,
                tushare_provider=tushare,
            )

            frame = provider.load_stock_basic(storage=storage)

            self.assertEqual(frame.attrs["data_source"], "tickflow_stock_basic")
            self.assertEqual(frame.iloc[0]["name"], "紫光股份")
            self.assertEqual(tickflow.stock_basic_calls, 1)
            self.assertEqual(tushare.stock_basic_calls, 0)
            self.assertEqual(storage.get_stock_basic().iloc[0]["ts_code"], "000938.SZ")

    def test_stock_basic_falls_back_to_tushare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=SimpleNamespace(load_stock_basic=lambda **kwargs: pd.DataFrame()),
                tushare_provider=_TushareBackend(),
            )

            frame = provider.load_stock_basic(storage=DuckDBStorage(Path(tmp) / "sats.duckdb"))

            self.assertEqual(frame.attrs["data_source"], "tushare_stock_basic")
            self.assertEqual(frame.iloc[0]["name"], "贵州茅台")

    def test_business_modules_do_not_import_backend_providers_directly(self) -> None:
        forbidden = (
            "from sats.data.tushare_provider import",
            "from sats.data.tickflow_provider import",
            "from sats.data.akshare_provider import",
            "TushareDataProvider",
            "TickFlowDataProvider",
            "AkShareDataProvider",
        )
        violations: list[str] = []
        for path in Path("sats").rglob("*.py"):
            if path.parts[:2] == ("sats", "data"):
                continue
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    violations.append(f"{path}:{needle}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
