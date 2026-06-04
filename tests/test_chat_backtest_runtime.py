from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.backtesting.service import run_strategy_backtest
from sats.backtesting.strategy_spec import strategy_spec_from_request, validate_strategy_spec
from sats.storage.duckdb import DuckDBStorage


def _daily(symbol: str = "000001.SZ", *, end: str = "20260520", count: int = 80) -> pd.DataFrame:
    cursor = datetime.strptime(end, "%Y%m%d")
    dates = []
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    dates = sorted(dates)
    rows = []
    for index, trade_date in enumerate(dates):
        close = 10 + index * 0.05
        rows.append(
            {
                "ts_code": symbol,
                "trade_date": trade_date,
                "open": close - 0.02,
                "high": close + 0.05,
                "low": close - 0.05,
                "close": close,
                "vol": 1000,
                "amount": close * 1000,
                "pct_chg": 0.0,
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs["data_source"] = "fake_daily"
    return frame


class FakeBacktestProvider:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls = []

    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        self.calls.append({"symbols": list(symbols), "start_date": start_date, "end_date": end_date, "storage": storage})
        return self.frame


class ChatBacktestRuntimeTest(unittest.TestCase):
    def test_strategy_spec_from_request_is_restricted_and_normalized(self) -> None:
        spec = strategy_spec_from_request("写一个5日和20日均线策略并回测000001")

        self.assertEqual(spec.symbols, ("000001.SZ",))
        self.assertEqual(spec.short_window, 5)
        self.assertEqual(spec.long_window, 20)
        self.assertEqual(spec.strategy_type, "moving_average")

    def test_validate_strategy_spec_rejects_unsupported_strategy_type(self) -> None:
        with self.assertRaises(ValueError):
            validate_strategy_spec(
                {
                    "name": "危险策略",
                    "strategy_type": "python",
                    "symbols": ["000001.SZ"],
                    "start_date": "20260101",
                    "end_date": "20260520",
                }
            )

    def test_light_backtest_uses_astock_provider_daily_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb", openai_model="m")
            storage = DuckDBStorage(settings.db_path)
            provider = FakeBacktestProvider(_daily())
            spec = validate_strategy_spec(
                {
                    "name": "均线研究策略",
                    "strategy_type": "moving_average",
                    "symbols": ["000001.SZ"],
                    "start_date": "20260101",
                    "end_date": "20260520",
                    "short_window": 5,
                    "long_window": 20,
                    "top_n": 1,
                }
            )

            result = run_strategy_backtest(spec, settings=settings, storage=storage, provider=provider)

            self.assertEqual(provider.calls[0]["symbols"], ["000001.SZ"])
            self.assertEqual(result.data_source, "fake_daily")
            self.assertIn("total_return", result.metrics)
            self.assertTrue(result.equity_curve)


if __name__ == "__main__":
    unittest.main()
