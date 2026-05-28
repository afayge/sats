from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.analysis.market_llm_context import build_market_llm_context, get_a_share_market_context, is_market_question


def _daily(symbol: str, *, end: str = "20260521") -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=90).strftime("%Y%m%d")
    rows = []
    for index, trade_date in enumerate(dates, start=1):
        close = 3000 + index * 2
        rows.append(
            {
                "ts_code": symbol,
                "trade_date": trade_date,
                "open": close - 5,
                "high": close + 8,
                "low": close - 10,
                "close": close,
                "vol": 10_000 + index,
                "amount": 20_000 + index,
                "pct_chg": 0.2,
            }
        )
    return pd.DataFrame(rows)


class _FakeTickFlowProvider:
    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        return pd.concat([_daily(symbol, end=end_date or "20260521") for symbol in symbols], ignore_index=True)

    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        if universe_id:
            return pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "close": 10.0, "pct_chg": 1.0, "amount": 100.0},
                    {"ts_code": "000002.SZ", "close": 9.0, "pct_chg": -0.5, "amount": 80.0},
                    {"ts_code": "600000.SH", "close": 8.0, "pct_chg": 10.0, "amount": 120.0},
                    {"ts_code": "600001.SH", "close": 7.0, "pct_chg": -10.0, "amount": 70.0},
                ]
            )
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "name": f"Index{symbol[:6]}",
                    "close": 3100.0,
                    "pct_chg": 0.88,
                    "amount": 99_000.0,
                    "data_source": "tickflow_quote",
                }
                for symbol in symbols
            ]
        )


class _EmptyTushareProvider:
    def load_index_daily(self, index_codes, *, start_date, end_date):
        return pd.DataFrame()

    def load_hot_sector_context(self, trade_date, *, storage, lookback_days=5, top_industries=10, top_concepts=20):
        return {
            "trade_date": trade_date,
            "lookback_days": lookback_days,
            "hot_industries": [
                {
                    "sector_code": "881155.TI",
                    "name": "电力",
                    "sector_type": "industry",
                    "heat_score": 8.0,
                    "latest_pct_chg": 1.2,
                }
            ],
            "hot_concepts": [
                {
                    "sector_code": "885001.TI",
                    "name": "AI算力",
                    "sector_type": "concept",
                    "heat_score": 15.0,
                    "latest_pct_chg": 2.0,
                }
            ],
            "stock_hot_sectors": {},
            "missing_fields": [],
            "data_sources": {"sector_basic": "fake", "sector_daily": "fake", "sector_members": "fake"},
        }


class _BrokenBreadthTickFlowProvider(_FakeTickFlowProvider):
    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        if universe_id:
            raise RuntimeError("breadth unavailable")
        return super().load_realtime_quotes(symbols=symbols, universe_id=universe_id)


class _EmptyProvider:
    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        return pd.DataFrame()

    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        return pd.DataFrame()


class _EmptyAkShareProvider:
    def load_a_share_realtime_quotes(self):
        return pd.DataFrame()


class MarketLLMContextTest(unittest.TestCase):
    def test_detects_market_question_without_stock_code(self) -> None:
        self.assertTrue(is_market_question("今天A股大盘分析，明天和下周走势预测"))
        self.assertTrue(is_market_question("上证和创业板今天怎么看"))
        self.assertFalse(is_market_question("帮我解释筛选规则"))

    def test_build_market_context_contains_indices_breadth_and_data_sources(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("/tmp/sats.duckdb"))
        context = build_market_llm_context(
            "今天A股大盘分析，明天和下周走势预测",
            settings=settings,
            trade_date="20260521",
            tickflow_provider=_FakeTickFlowProvider(),
            tushare_provider=_EmptyTushareProvider(),
            akshare_provider=_EmptyAkShareProvider(),
        )

        self.assertIsNotNone(context)
        payload = context.payload
        self.assertEqual(payload["trade_date"], "20260521")
        self.assertEqual(payload["requested_horizons"], ["today", "tomorrow", "next_week"])
        self.assertEqual(payload["requested_dimensions"], ["core_indices", "market_breadth", "limit_sentiment"])
        self.assertEqual(payload["indices"][0]["ts_code"], "000001.SH")
        self.assertIn("399330.SZ", payload["requested_indices"])
        self.assertIn("ma60", payload["indices"][0]["technical"]["ma"])
        self.assertEqual(payload["market_breadth"]["advancing_count"], 2)
        self.assertEqual(payload["market_breadth"]["declining_count"], 2)
        self.assertEqual(payload["limit_sentiment"]["limit_up_count"], 1)
        self.assertEqual(payload["limit_sentiment"]["limit_down_count"], 1)
        self.assertIn("broken_limit_count:tushare_unavailable", payload["limit_sentiment"]["missing_fields"])
        self.assertEqual(payload["data_sources"]["index_daily"], "tickflow_index_daily")
        self.assertIn("limit_sentiment", payload["data_sources"])
        self.assertEqual(payload["hot_sector_context"], {})
        self.assertEqual(payload["data_sources"]["hot_sector_context"], "not_requested")
        self.assertIn("不得编造价格", context.system_message)
        self.assertIn("limit_sentiment 来自涨停、跌停、炸板统计", context.system_message)

    def test_market_context_respects_requested_dimensions_and_day_after_tomorrow(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("/tmp/sats.duckdb"))
        payload = get_a_share_market_context(
            settings=settings,
            trade_date="20260521",
            horizons=["tomorrow", "day_after_tomorrow"],
            dimensions=["core_indices", "limit_sentiment"],
            indices=["上证指数", "深证100"],
            market_plan_source="llm+local_market_plan",
            tickflow_provider=_FakeTickFlowProvider(),
            tushare_provider=_EmptyTushareProvider(),
            akshare_provider=_EmptyAkShareProvider(),
        )

        self.assertEqual(payload["requested_horizons"], ["tomorrow", "day_after_tomorrow"])
        self.assertEqual(payload["requested_dimensions"], ["core_indices", "limit_sentiment"])
        self.assertEqual(payload["requested_indices"], ["000001.SH", "399330.SZ"])
        self.assertEqual(payload["market_plan_source"], "llm+local_market_plan")
        self.assertEqual(payload["market_breadth"], {})
        self.assertNotIn("market_breadth", payload["missing_fields"])
        self.assertEqual(payload["data_sources"]["market_breadth"], "not_requested")
        self.assertEqual([item["ts_code"] for item in payload["indices"]], ["000001.SH", "399330.SZ"])

    def test_market_context_continues_when_breadth_missing(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("/tmp/sats.duckdb"))
        payload = get_a_share_market_context(
            settings=settings,
            trade_date="20260521",
            tickflow_provider=_BrokenBreadthTickFlowProvider(),
            tushare_provider=_EmptyTushareProvider(),
            akshare_provider=_EmptyAkShareProvider(),
        )

        self.assertEqual(payload["market_breadth"], {})
        self.assertIn("market_breadth", payload["missing_fields"])
        self.assertIn("limit_sentiment", payload["missing_fields"])

    def test_market_context_stops_when_core_index_daily_missing(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("/tmp/sats.duckdb"))
        with self.assertRaisesRegex(ValueError, "核心指数真实日线数据"):
            get_a_share_market_context(
                settings=settings,
                trade_date="20260521",
                tickflow_provider=_EmptyProvider(),
                tushare_provider=_EmptyTushareProvider(),
                akshare_provider=_EmptyAkShareProvider(),
            )


if __name__ == "__main__":
    unittest.main()
