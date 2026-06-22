from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.data.astock_provider import AStockDataProvider
from sats.data.akshare_datasets import AKSHARE_DATASETS, list_akshare_datasets
from sats.data.akshare_provider import AkShareDataProvider
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
        frame = pd.DataFrame(
            [
                {"ts_code": symbol, "trade_date": "20260520", "close": 10.0, "pct_chg": 0.0, "amount": 100.0}
                for symbol in symbols
            ]
        )
        frame.attrs["data_source"] = "tickflow_current_1m_quote"
        return frame

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
        self.sw_basic_calls = 0
        self.sw_member_calls: list[list[str]] = []
        self.stock_basic_calls = 0
        self.limit_sentiment_calls = 0
        self.stock_dataset_calls = 0
        self.dataset_calls = 0

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

    def _load_sw_sector_basic(self, *, storage):
        self.sw_basic_calls += 1
        frame = pd.DataFrame(
            [{"sector_code": "851911.SI", "name": "贸易Ⅲ", "sector_type": "sw_l3", "exchange": "SW2021"}]
        )
        if storage is not None:
            storage.upsert_sector_basic(frame)
        return frame

    def _load_sw_sector_members(self, sector_codes, *, storage):
        self.sw_member_calls.append(list(sector_codes))
        frame = pd.DataFrame(
            [{"sector_code": "851911.SI", "ts_code": "600153.SH", "name": "建发股份", "data_source": "fake_sw"}]
        )
        if storage is not None:
            storage.upsert_sector_members(frame)
        return frame[frame["sector_code"].isin(sector_codes)].copy()

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

    def list_tushare_stock_datasets(self, *, category=None, include_deprecated=True):
        return [
            {
                "dataset": "daily",
                "domain": "股票数据",
                "category": "行情数据",
                "status": "active",
                "default_fields": ["ts_code", "trade_date", "close"],
            }
        ]

    def list_tushare_datasets(self, *, domain=None, category=None, include_deprecated=True, tags=None):
        return [
            {
                "dataset": "index_daily",
                "domain": domain or "指数专题",
                "category": category or "指数专题",
                "status": "active",
                "tags": tags or ["index"],
                "default_fields": ["ts_code", "trade_date", "close"],
            }
        ]

    def fetch_dataset(self, dataset, params=None, *, fields=None, limit=200):
        self.dataset_calls += 1
        return {
            "dataset": dataset,
            "api": dataset,
            "domain": "指数专题",
            "params": params or {},
            "columns": fields or ["ts_code", "close"],
            "rows": [{"ts_code": "000001.SH", "close": 3100.0}],
            "row_count": 1,
            "returned_row_count": 1,
            "data_source": f"tushare_{dataset}",
            "missing_fields": [],
        }

    def fetch_stock_dataset(self, dataset, params=None, *, fields=None, limit=200):
        self.stock_dataset_calls += 1
        return {
            "dataset": dataset,
            "api": dataset,
            "domain": "股票数据",
            "params": params or {},
            "columns": fields or ["ts_code", "close"],
            "rows": [{"ts_code": "000001.SZ", "close": 10.0}],
            "row_count": 1,
            "returned_row_count": 1,
            "data_source": f"tushare_{dataset}",
            "missing_fields": [],
        }


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
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_daily(
                _daily_frame(["000001.SZ"], trade_date="20260519", close=9.5)
            )
            tickflow = _TickFlowBackend()
            akshare = _AkShareBackend()
            provider = AStockDataProvider(
                _settings(db_path),
                tickflow_provider=tickflow,
                tushare_provider=_TushareBackend(),
                akshare_provider=akshare,
            )

            frame = provider.load_realtime_quotes(symbols=["000001"])

            self.assertEqual(frame.attrs["data_source"], "tickflow_current_1m_quote")
            self.assertEqual(frame.iloc[0]["ts_code"], "000001.SZ")
            self.assertAlmostEqual(float(frame.iloc[0]["pre_close"]), 9.5)
            self.assertAlmostEqual(float(frame.iloc[0]["pct_chg"]), (10.0 / 9.5 - 1.0) * 100.0)
            self.assertEqual(tickflow.quote_calls, 1)
            self.assertEqual(akshare.quote_calls, 0)

    def test_realtime_quotes_do_not_fall_back_to_akshare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            akshare = _AkShareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_BrokenTickFlowBackend(),
                tushare_provider=_TushareBackend(),
                akshare_provider=akshare,
            )

            with self.assertRaisesRegex(RuntimeError, "tickflow unavailable"):
                provider.load_realtime_quotes(symbols=["000001"])

            self.assertEqual(akshare.quote_calls, 0)

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

    def test_sw_sector_loaders_delegate_to_tushare_and_cache_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            tushare = _TushareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=tushare,
            )

            basic = provider.load_sw_sector_basic(storage=storage)
            members = provider.load_sw_sector_members(["851911.SI"], storage=storage)

            self.assertEqual(tushare.sw_basic_calls, 1)
            self.assertEqual(tushare.sw_member_calls, [["851911.SI"]])
            self.assertEqual(basic.iloc[0]["sector_type"], "sw_l3")
            self.assertEqual(members.iloc[0]["ts_code"], "600153.SH")

            cached_provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=None,
            )
            cached_provider._tushare_failed = True

            cached_basic = cached_provider.load_sw_sector_basic(storage=storage)
            cached_members = cached_provider.load_sw_sector_members(["851911.SI"], storage=storage)

            self.assertEqual(cached_basic.iloc[0]["sector_code"], "851911.SI")
            self.assertEqual(cached_members.iloc[0]["ts_code"], "600153.SH")

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

    def test_tushare_stock_dataset_facade_delegates_to_tushare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tushare = _TushareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=tushare,
            )

            datasets = provider.list_tushare_stock_datasets(category="行情数据")
            payload = provider.fetch_tushare_stock_dataset(
                "daily",
                {"ts_code": "000001"},
                fields=["ts_code", "close"],
                limit=1,
            )

            self.assertEqual(datasets[0]["dataset"], "daily")
            self.assertEqual(payload["data_source"], "tushare_daily")
            self.assertEqual(tushare.stock_dataset_calls, 1)

    def test_tushare_general_dataset_facade_delegates_to_tushare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tushare = _TushareBackend()
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=tushare,
            )

            datasets = provider.list_tushare_datasets(domain="指数专题", tags=["index"])
            payload = provider.fetch_tushare_dataset(
                "index_daily",
                {"ts_code": "000001.SH"},
                fields=["ts_code", "close"],
                limit=1,
            )

            self.assertEqual(datasets[0]["dataset"], "index_daily")
            self.assertEqual(payload["domain"], "指数专题")
            self.assertEqual(payload["data_source"], "tushare_index_daily")
            self.assertEqual(tushare.dataset_calls, 1)

    def test_company_fundamentals_aggregate_tushare_without_daily_klines(self) -> None:
        class CompanyTickFlow(_TickFlowBackend):
            def load_stock_basic(self, *, storage=None):
                frame = pd.DataFrame(
                    [
                        {"ts_code": "688700.SH", "symbol": "688700", "name": "东威科技"},
                        {"ts_code": "688559.SH", "symbol": "688559", "name": "海目星"},
                    ]
                )
                if storage is not None:
                    storage.upsert_stock_basic(frame)
                return frame

        class CompanyTushare(_TushareBackend):
            def fetch_stock_dataset(self, dataset, params=None, *, fields=None, limit=200):
                ts_code = str((params or {}).get("ts_code") or "")
                rows = {
                    "stock_company": [{"ts_code": ts_code, "com_name": f"{ts_code}公司", "main_business": "专用设备"}],
                    "fina_mainbz": [{"ts_code": ts_code, "end_date": "20251231", "bz_item": "设备", "bz_sales": 100.0}],
                    "daily_basic": [{"ts_code": ts_code, "trade_date": "20260618", "pe": 20.0, "pb": 2.0, "ps": 3.0, "total_mv": 100000.0}],
                    "fina_indicator": [
                        {"ts_code": ts_code, "end_date": f"{year}1231", "roe": 10.0 + year - 2022, "debt_to_assets": 45.0}
                        for year in range(2022, 2026)
                    ],
                    "income": [{"ts_code": ts_code, "end_date": "20251231", "revenue": 1000.0, "n_income": 100.0}],
                    "balancesheet": [{"ts_code": ts_code, "end_date": "20251231", "total_assets": 2000.0, "total_liab": 900.0}],
                    "cashflow": [{"ts_code": ts_code, "end_date": "20251231", "n_cashflow_act": 120.0}],
                }[dataset]
                return {
                    "dataset": dataset,
                    "rows": rows[:limit],
                    "row_count": len(rows),
                    "returned_row_count": min(len(rows), limit),
                    "data_source": f"tushare_{dataset}",
                    "missing_fields": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=CompanyTickFlow(),
                tushare_provider=CompanyTushare(),
                akshare_provider=SimpleNamespace(),
            )

            payload = provider.load_company_fundamentals(
                ["688700.SH", "688559.SH"],
                trade_date="20260621",
                storage=DuckDBStorage(Path(tmp) / "sats.duckdb"),
            )

        self.assertEqual(payload["688700.SH"]["name"], "东威科技")
        self.assertEqual(payload["688559.SH"]["name"], "海目星")
        self.assertEqual(payload["688700.SH"]["main_business"], "专用设备")
        self.assertEqual(len(payload["688700.SH"]["financial_indicators"]), 4)
        self.assertEqual(payload["688700.SH"]["valuation"]["trade_date"], "20260618")
        self.assertEqual(payload["688700.SH"]["missing_fields"], [])

    def test_company_fundamentals_keeps_tushare_results_when_akshare_fallback_fails(self) -> None:
        class PartialTushare(_TushareBackend):
            def fetch_stock_dataset(self, dataset, params=None, *, fields=None, limit=200):
                rows = [] if dataset in {"stock_company", "fina_mainbz"} else [{"ts_code": "688700.SH", "end_date": "20251231", "roe": 12.0}]
                return {
                    "dataset": dataset,
                    "rows": rows,
                    "row_count": len(rows),
                    "returned_row_count": len(rows),
                    "data_source": f"tushare_{dataset}" if rows else "unavailable",
                    "missing_fields": [] if rows else [f"{dataset}:unavailable"],
                }

        class FailedAkShare:
            def fetch_akshare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                return {
                    "dataset": dataset,
                    "rows": [],
                    "data_source": "unavailable",
                    "missing_fields": ["akshare:fetch_failed"],
                }

        provider = AStockDataProvider(
            _settings(Path("sats.duckdb")),
            tickflow_provider=SimpleNamespace(
                load_stock_basic=lambda **kwargs: pd.DataFrame(
                    [{"ts_code": "688700.SH", "symbol": "688700", "name": "东威科技"}]
                )
            ),
            tushare_provider=PartialTushare(),
            akshare_provider=FailedAkShare(),
        )

        payload = provider.load_company_fundamentals(["688700.SH"], trade_date="20260621")

        company = payload["688700.SH"]
        self.assertEqual(company["name"], "东威科技")
        self.assertTrue(company["financial_indicators"])
        self.assertIn("akshare:fetch_failed", company["missing_fields"])

    def test_provider_capabilities_facade_lists_tickflow_and_tushare(self) -> None:
        provider = AStockDataProvider(
            _settings(Path("sats.duckdb")),
            tickflow_provider=_TickFlowBackend(),
            tushare_provider=_TushareBackend(),
        )

        tickflow = provider.load_provider_capabilities(provider="tickflow", realtime=True, compact=True)
        tushare = provider.load_provider_capabilities(provider="tushare", category="指数专题", compact=True)
        akshare = provider.load_provider_capabilities(provider="akshare", category="数据字典", compact=True)

        self.assertIn("tickflow.realtime_quotes", {item["capability_id"] for item in tickflow})
        self.assertIn("tushare.index_member_all", {item["capability_id"] for item in tushare})
        self.assertIn("akshare.dataset_catalog", {item["capability_id"] for item in akshare})

    def test_akshare_dataset_catalog_lists_public_data_functions(self) -> None:
        datasets = list_akshare_datasets(query="stock_zh_a_spot_em", compact=True)

        self.assertIn("stock_zh_a_spot_em", AKSHARE_DATASETS)
        self.assertNotIn("APIError", AKSHARE_DATASETS)
        self.assertEqual(datasets[0]["dataset"], "stock_zh_a_spot_em")
        self.assertIn("stock", datasets[0]["tags"])

    def test_akshare_fetcher_normalizes_dataframe_and_limits_fields(self) -> None:
        class FakeAkShare:
            def stock_zh_a_spot_em(self):
                return pd.DataFrame(
                    [
                        {"代码": "000001", "名称": "平安银行", "最新价": 10.5},
                        {"代码": "600000", "名称": "浦发银行", "最新价": 9.5},
                    ]
                )

        provider = AkShareDataProvider(ak_module=FakeAkShare())

        payload = provider.fetch_akshare_dataset(
            "stock_zh_a_spot_em",
            fields=["代码", "最新价"],
            limit=1,
        )

        self.assertEqual(payload["dataset"], "stock_zh_a_spot_em")
        self.assertEqual(payload["columns"], ["代码", "最新价"])
        self.assertEqual(payload["returned_row_count"], 1)
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["rows"][0]["代码"], "000001")
        self.assertEqual(payload["market_data_provenance"][0]["source"], "akshare")

    def test_akshare_fetcher_rejects_unknown_and_unsafe_params(self) -> None:
        provider = AkShareDataProvider(ak_module=SimpleNamespace(bond_cb_jsl=lambda cookie="": pd.DataFrame()))

        with self.assertRaises(KeyError):
            provider.fetch_akshare_dataset("not_a_dataset")
        with self.assertRaises(ValueError):
            provider.fetch_akshare_dataset("bond_cb_jsl", {"cookie": "secret"})

    def test_astock_akshare_facade_returns_structured_unavailable(self) -> None:
        provider = AStockDataProvider(
            _settings(Path("sats.duckdb")),
            tickflow_provider=_TickFlowBackend(),
            tushare_provider=_TushareBackend(),
        )
        provider._akshare_failed = True

        payload = provider.fetch_akshare_dataset("stock_zh_a_spot_em")

        self.assertEqual(payload["data_source"], "unavailable")
        self.assertEqual(payload["rows"], [])
        self.assertIn("akshare:unavailable", payload["missing_fields"])

    def test_tushare_stock_dataset_facade_returns_structured_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=None,
            )
            provider._tushare_failed = True

            payload = provider.fetch_tushare_stock_dataset("daily", {"ts_code": "000001"})

            self.assertEqual(payload["data_source"], "unavailable")
            self.assertEqual(payload["rows"], [])
            self.assertIn("tushare:unavailable", payload["missing_fields"])

    def test_tushare_general_dataset_facade_returns_structured_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = AStockDataProvider(
                _settings(Path(tmp) / "sats.duckdb"),
                tickflow_provider=_TickFlowBackend(),
                tushare_provider=None,
            )
            provider._tushare_failed = True

            payload = provider.fetch_tushare_dataset("index_daily", {"ts_code": "000001.SH"})

            self.assertEqual(payload["domain"], "指数专题")
            self.assertEqual(payload["data_source"], "unavailable")
            self.assertEqual(payload["rows"], [])
            self.assertIn("tushare:unavailable", payload["missing_fields"])

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
