from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.cli import main
from sats.data.astock_provider import AStockDataProvider
from sats.repl import CLI_COMMANDS, handle_repl_line, help_text
from sats.storage import DuckDBStorage


class PeriodChangeCliTest(unittest.TestCase):
    def test_stock_period_change_uses_nearest_trade_dates_and_requested_order(self) -> None:
        providers = []

        class FakeProvider:
            def __init__(self, settings) -> None:
                self.calls = []
                providers.append(self)

            def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
                self.calls.append((symbols, start_date, end_date))
                return pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20260421", "close": 10.0},
                        {"ts_code": "000001.SZ", "trade_date": "20260423", "close": 11.0},
                        {"ts_code": "000001.SZ", "trade_date": "20260619", "close": 12.0},
                        {"ts_code": "600519.SH", "trade_date": "20260424", "close": 100.0},
                        {"ts_code": "600519.SH", "trade_date": "20260618", "close": 90.0},
                    ]
                )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                        {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台"},
                    ]
                )
            )
            stdout = io.StringIO()
            with (
                patch("sats.cli.AStockDataProvider", FakeProvider),
                patch("sats.cli.datetime") as mocked_datetime,
                patch("sys.stdout", stdout),
            ):
                mocked_datetime.now.return_value = datetime(2026, 6, 21)
                exit_code = main(
                    [
                        "period-change",
                        "--stocks",
                        "000001,600519",
                        "--days",
                        "60",
                        "--db",
                        str(db_path),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            providers[0].calls,
            [(["000001.SZ", "600519.SH"], "20260322", "20260621")],
        )
        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(
            lines[0],
            "目标区间: 2026-04-22 至 2026-06-21（60 个自然日；使用最接近交易日的收盘价）",
        )
        self.assertEqual(
            " ".join(lines[2].split()),
            "1. 000001.SZ 平安银行 2026-04-21 10.00 2026-06-19 12.00 +2.00 +20.00%",
        )
        self.assertEqual(
            " ".join(lines[3].split()),
            "2. 600519.SH 贵州茅台 2026-04-24 100.00 2026-06-18 90.00 -10.00 -10.00%",
        )

    def test_index_period_change_accepts_common_names(self) -> None:
        providers = []

        class FakeProvider:
            def __init__(self, settings) -> None:
                self.calls = []
                providers.append(self)

            def load_index_daily(self, symbols, *, start_date, end_date):
                self.calls.append((symbols, start_date, end_date))
                return pd.DataFrame(
                    [
                        {"ts_code": "000001.SH", "trade_date": "20251222", "close": 3000.0},
                        {"ts_code": "000001.SH", "trade_date": "20260619", "close": 3300.0},
                        {"ts_code": "000300.SH", "trade_date": "20251224", "close": 4000.0},
                        {"ts_code": "000300.SH", "trade_date": "20260619", "close": 3800.0},
                    ]
                )

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch("sats.cli.AStockDataProvider", FakeProvider),
                patch("sats.cli.datetime") as mocked_datetime,
                patch("sys.stdout", stdout),
            ):
                mocked_datetime.now.return_value = datetime(2026, 6, 21)
                exit_code = main(
                    [
                        "period-change",
                        "--indices",
                        "上证指数,沪深300",
                        "--days",
                        "180",
                        "--db",
                        str(Path(tmp) / "sats.duckdb"),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            providers[0].calls,
            [(["000001.SH", "000300.SH"], "20251122", "20260621")],
        )
        output = " ".join(stdout.getvalue().split())
        self.assertIn("000001.SH 上证指数 2025-12-22 3000.00 2026-06-19 3300.00 +300.00 +10.00%", output)
        self.assertIn("000300.SH 沪深300 2025-12-24 4000.00 2026-06-19 3800.00 -200.00 -5.00%", output)

    def test_period_change_requires_positive_days(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            main(["period-change", "--stocks", "000001", "--days", "0"])

        self.assertEqual(str(exc.exception), "--days must be positive")

    def test_tushare_fallback_expands_for_long_periods(self) -> None:
        class BrokenTickFlow:
            def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
                raise RuntimeError("unavailable")

        class Tushare:
            def __init__(self) -> None:
                self.lookback_days = 0

            def load_indicator_inputs(self, symbols, trade_date, *, lookback_days, storage=None):
                self.lookback_days = lookback_days
                return []

        tushare = Tushare()
        provider = AStockDataProvider(
            SimpleNamespace(),
            tickflow_provider=BrokenTickFlow(),
            tushare_provider=tushare,
        )

        provider.load_historical_daily_klines(
            ["000001.SZ"],
            start_date="20240101",
            end_date="20260621",
        )

        self.assertGreater(tushare.lookback_days, 180)

    def test_repl_allows_period_change_command(self) -> None:
        calls: list[list[str]] = []

        keep_running = handle_repl_line(
            "/period-change --stocks 000001,600519 --days 60",
            runner=lambda argv: calls.append(argv) or 0,
            printer=lambda _: None,
        )

        self.assertTrue(keep_running)
        self.assertIn("period-change", CLI_COMMANDS)
        self.assertIn("/period-change --stocks 000001,600519 --days 60", help_text())
        self.assertEqual(
            calls,
            [["period-change", "--stocks", "000001,600519", "--days", "60"]],
        )


if __name__ == "__main__":
    unittest.main()
