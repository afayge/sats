from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient
from prompt_toolkit.utils import get_cwidth

from sats.cli import build_parser, cmd_result_rules, cmd_results, cmd_screen, main
from sats.cli import cmd_analyze_chan
from sats.api.app import create_app
from sats.screening.base import ScreeningInput, ScreeningResult
from sats.screening.service import evaluate_and_store
from sats.storage.duckdb import DuckDBStorage
from tests.fixtures import (
    make_benchmark,
    make_chan_daily,
    make_chan_minute_30m,
    make_daily_basic,
    make_monthly_base_breakout,
    make_passing_daily,
    make_price_volume_daily,
    make_trade_dates,
)


def make_stock_basic(row: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame([row])


def make_rps_daily(ts_code: str, *, gain: float, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(121, end=end)
    start = 10.0
    finish = start * (1.0 + gain)
    step = (finish - start) / 120
    rows = []
    for index, trade_date in enumerate(dates):
        close = start + step * index
        previous = rows[-1]["close"] if rows else close
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "open": close - 0.05,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "vol": 1000.0,
                "amount": close * 1000.0,
                "pct_chg": (close / previous - 1.0) * 100 if previous > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)


class FakeTushareProvider:
    def __init__(self, settings) -> None:
        self.settings = settings

    def list_a_share_symbols(self) -> list[str]:
        return ["000001.SZ"]

    def load_all_screening_inputs(
        self,
        trade_date: str,
        *,
        storage: DuckDBStorage,
        trade_days: int = 80,
        rule_name: str | None = None,
    ) -> list[ScreeningInput]:
        return [self.load_screening_input("000001.SZ", trade_date, rule_name=rule_name)]

    def load_screening_input(self, ts_code: str, trade_date: str, rule_name: str | None = None) -> ScreeningInput:
        if rule_name in {"chan_third_buy", "chan_composite", "chan_signals"}:
            return ScreeningInput(
                ts_code=ts_code,
                trade_date=trade_date,
                daily=make_chan_daily(end=trade_date),
                daily_basic=make_daily_basic(end=trade_date),
                stock_basic={"name": "平安银行", "market": "主板", "exchange": "SZSE"},
                metadata={"minute_30m": make_chan_minute_30m(end=trade_date), "minute_30m_source": "tickflow_history"},
            )
        if rule_name == "monthly_base_breakout":
            return ScreeningInput(
                ts_code=ts_code,
                trade_date=trade_date,
                daily=make_price_volume_daily(end=trade_date),
                daily_basic=pd.DataFrame(),
                stock_basic={"name": "平安银行", "market": "北交所", "exchange": "BSE"},
                metadata={"monthly_1M": make_monthly_base_breakout(end=trade_date), "monthly_1M_source": "test_monthly"},
            )
        return ScreeningInput(
            ts_code=ts_code,
            trade_date=trade_date,
            daily=make_price_volume_daily(end=trade_date),
            daily_basic=make_daily_basic(end=trade_date),
            stock_basic={"name": "平安银行"},
            industry_daily=make_benchmark(end=trade_date),
        )


class FakeTickFlowProvider:
    def __init__(self, settings) -> None:
        self.settings = settings

    def load_realtime_minute_klines(self, symbols, *, period="1m", count=None):
        return _minute_k_frame(symbols[0], period=period, trade_time="2026-05-14 09:31:00")

    def load_historical_minute_klines(
        self,
        symbols,
        *,
        period="1m",
        start_time=None,
        end_time=None,
        count=None,
    ):
        return _minute_k_frame(symbols[0], period=period, trade_time="2026-05-13 10:00:00")


def _minute_k_frame(symbol: str, *, period: str, trade_time: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": [symbol],
            "period": [period],
            "trade_date": [trade_time[:10].replace("-", "")],
            "trade_time": [trade_time],
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "vol": [12.0],
            "amount": [121.2],
            "data_source": ["tickflow"],
        }
    )


def _quote_history(symbol: str, *, close: float, days: int = 260) -> list[dict[str, object]]:
    dates = pd.date_range("2025-01-01", periods=days, freq="D")
    return [
        {
            "ts_code": symbol,
            "trade_date": day.strftime("%Y%m%d"),
            "close": close,
        }
        for day in dates
    ]


class StorageAndApiTest(unittest.TestCase):
    def test_duckdb_storage_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            data = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_passing_daily(),
                daily_basic=make_daily_basic(),
                industry_daily=make_benchmark(),
            )

            results = evaluate_and_store([data], rule_name="ma-volume-relative-strength", storage=storage)
            rows = storage.list_screening_results(trade_date="20260430", passed=True)

            self.assertEqual(len(results), 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ts_code"], "000001.SZ")
            self.assertTrue(rows[0]["passed"])

    def test_stock_basic_cache_can_be_read_back_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台"},
                        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                    ]
                )
            )

            rows = storage.get_stock_basic()

            self.assertEqual(rows["ts_code"].tolist(), ["000001.SZ", "600519.SH"])
            self.assertEqual(rows.iloc[0]["name"], "平安银行")

    def test_readonly_storage_can_read_existing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(
                pd.DataFrame(
                    [{"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"}]
                )
            )

            readonly = DuckDBStorage(db_path, read_only=True)
            rows = readonly.get_stock_basic()

            self.assertTrue(readonly.read_only)
            self.assertEqual(rows.iloc[0]["ts_code"], "000001.SZ")

    def test_sector_cache_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            self.assertEqual(
                storage.upsert_sector_basic(
                    pd.DataFrame(
                        [
                            {
                                "sector_code": "885001.TI",
                                "name": "AI算力",
                                "sector_type": "concept",
                                "exchange": "THS",
                                "list_date": "20200101",
                                "data_source": "tushare_ths",
                            },
                            {
                                "sector_code": "881155.TI",
                                "name": "银行",
                                "sector_type": "industry",
                                "exchange": "THS",
                                "list_date": "20200101",
                                "data_source": "tushare_ths",
                            },
                        ]
                    )
                ),
                2,
            )
            self.assertEqual(
                storage.upsert_sector_daily(
                    pd.DataFrame(
                        [
                            {
                                "sector_code": "885001.TI",
                                "trade_date": "20260520",
                                "open": 10.0,
                                "high": 11.0,
                                "low": 9.8,
                                "close": 10.8,
                                "pct_chg": 2.5,
                                "vol": 1000.0,
                                "amount": 10000.0,
                                "data_source": "tushare_ths",
                            }
                        ]
                    )
                ),
                1,
            )
            self.assertEqual(
                storage.upsert_sector_members(
                    pd.DataFrame(
                        [
                            {
                                "sector_code": "885001.TI",
                                "ts_code": "000938.SZ",
                                "name": "紫光股份",
                                "weight": 1.0,
                                "in_date": "20200101",
                                "out_date": "",
                                "is_new": False,
                                "data_source": "tushare_ths",
                            }
                        ]
                    )
                ),
                1,
            )

            concepts = storage.get_sector_basic(sector_types=["concept"])
            daily = storage.get_sector_daily(["885001.TI"], trade_dates=["20260520"])
            members = storage.get_sector_members(["885001.TI"])

            self.assertEqual(concepts["sector_code"].tolist(), ["885001.TI"])
            self.assertEqual(concepts.iloc[0]["name"], "AI算力")
            self.assertEqual(daily.iloc[0]["close"], 10.8)
            self.assertEqual(members.iloc[0]["ts_code"], "000938.SZ")

    def test_api_screen_defaults_to_all_a_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)
            payload = {
                "trade_date": "20260430",
                "rule": "ma-volume-relative-strength",
            }

            with patch("sats.api.app.AStockDataProvider", FakeTushareProvider):
                response = client.post("/api/screen", json=payload)
            query = client.get("/api/screen/results", params={"trade_date": "20260430", "passed": True})

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total_count"], 1)
            self.assertEqual(response.json()["passed_count"], 1)
            self.assertEqual(response.json()["passed_results"][0]["ts_code"], "000001.SZ")
            self.assertEqual(query.status_code, 200, query.text)
            self.assertEqual(query.json()["count"], 1)

    def test_api_screen_accepts_price_volume_ma_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)

            with patch("sats.api.app.AStockDataProvider", FakeTushareProvider):
                response = client.post(
                    "/api/screen",
                    json={"trade_date": "20260430", "rule": "price-volume-ma"},
                )
            query = client.get(
                "/api/screen/results",
                params={"trade_date": "20260430", "rule": "price-volume-ma", "passed": True},
            )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["rule"], "price_volume_ma")
            self.assertEqual(response.json()["passed_count"], 1)
            self.assertEqual(query.status_code, 200, query.text)
            self.assertEqual(query.json()["count"], 1)

    def test_api_screen_accepts_sequoia_x_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)

            with patch("sats.api.app.AStockDataProvider", FakeTushareProvider):
                response = client.post(
                    "/api/screen",
                    json={"trade_date": "20260430", "rule": "TurtleTrade"},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["rule"], "turtle_trade")

    def test_api_screen_accepts_chan_third_buy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)

            with patch("sats.api.app.AStockDataProvider", FakeTushareProvider):
                response = client.post(
                    "/api/screen",
                    json={"trade_date": "20260430", "rule": "chan-third-buy"},
                )
            query = client.get(
                "/api/screen/results",
                params={"trade_date": "20260430", "rule": "chan-third-buy", "passed": True},
            )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["rule"], "chan_third_buy")
            self.assertEqual(response.json()["passed_count"], 1)
            self.assertEqual(query.status_code, 200, query.text)
            self.assertEqual(query.json()["count"], 1)

    def test_api_screen_accepts_chan_composite_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)

            with patch("sats.api.app.AStockDataProvider", FakeTushareProvider):
                response = client.post(
                    "/api/screen",
                    json={"trade_date": "20260430", "rule": "chan-stock-select"},
                )
            query = client.get(
                "/api/screen/results",
                params={"trade_date": "20260430", "rule": "chan-composite", "passed": True},
            )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["rule"], "chan_composite")
            self.assertEqual(response.json()["passed_count"], 1)
            self.assertEqual(response.json()["passed_results"][0]["metrics_json"]["matched_chan_rules"], ["三买"])
            self.assertEqual(query.status_code, 200, query.text)
            self.assertEqual(query.json()["count"], 1)

    def test_api_screen_accepts_chan_signals_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)

            with patch("sats.api.app.AStockDataProvider", FakeTushareProvider):
                response = client.post(
                    "/api/screen",
                    json={"trade_date": "20260430", "rule": "chan-ai-select"},
                )
            query = client.get(
                "/api/screen/results",
                params={"trade_date": "20260430", "rule": "chan-signals", "passed": True},
            )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["rule"], "chan_signals")
            self.assertEqual(response.json()["passed_count"], 1)
            metrics = response.json()["passed_results"][0]["metrics_json"]
            self.assertIn("三买", metrics["matched_chan_rules"])
            self.assertIn("chan_signals", metrics)
            self.assertEqual(query.status_code, 200, query.text)
            self.assertEqual(query.json()["count"], 1)

    def test_api_rejects_legacy_symbol_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)

            response = client.post(
                "/api/screen",
                json={"trade_date": "20260430", "symbols": ["000001.SZ"]},
            )

            self.assertEqual(response.status_code, 422, response.text)

    def test_api_minute_k_returns_tickflow_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            app = create_app(storage=storage)
            client = TestClient(app)

            with patch("sats.api.app.AStockDataProvider", FakeTickFlowProvider):
                response = client.get(
                    "/api/market/minute-k",
                    params={"symbols": "000001", "period": "5m", "mode": "history"},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["count"], 1)
            self.assertEqual(response.json()["results"][0]["ts_code"], "000001.SZ")
            self.assertEqual(response.json()["results"][0]["period"], "5m")
            with storage.connect() as con:
                tables = con.execute("SHOW TABLES").fetchdf()["name"].tolist()
            self.assertNotIn("stock_minute", tables)

    def test_cli_screen_defaults_to_all_a_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule="ma-volume-relative-strength",
                db=Path(tmp) / "sats.duckdb",
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTushareProvider), redirect_stdout(stdout):
                exit_code = cmd_screen(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行")

    def test_cli_screen_does_not_prompt_watchlist_import_when_not_tty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule="ma-volume-relative-strength",
                db=Path(tmp) / "sats.duckdb",
                select_watchlist=False,
                no_select_watchlist=False,
            )
            stdout = io.StringIO()

            with (
                patch("sats.cli.AStockDataProvider", FakeTushareProvider),
                patch("sats.cli.sys.stdin.isatty", return_value=False),
                patch("sats.cli.sys.stdout.isatty", return_value=False),
                patch("sats.cli.select_and_import_watchlist") as importer,
                redirect_stdout(stdout),
            ):
                exit_code = cmd_screen(args)

            self.assertEqual(exit_code, 0)
            importer.assert_not_called()
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行")

    def test_cli_screen_select_watchlist_forces_import_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule="ma-volume-relative-strength",
                db=Path(tmp) / "sats.duckdb",
                select_watchlist=True,
                no_select_watchlist=False,
            )
            stdout = io.StringIO()

            with (
                patch("sats.cli.AStockDataProvider", FakeTushareProvider),
                patch("sats.cli.select_and_import_watchlist") as importer,
                redirect_stdout(stdout),
            ):
                exit_code = cmd_screen(args)

            self.assertEqual(exit_code, 0)
            importer.assert_called_once()

    def test_cli_screen_no_select_watchlist_disables_import_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule="ma-volume-relative-strength",
                db=Path(tmp) / "sats.duckdb",
                select_watchlist=False,
                no_select_watchlist=True,
            )
            stdout = io.StringIO()

            with (
                patch("sats.cli.AStockDataProvider", FakeTushareProvider),
                patch("sats.cli.sys.stdin.isatty", return_value=True),
                patch("sats.cli.sys.stdout.isatty", return_value=True),
                patch("sats.cli.select_and_import_watchlist") as importer,
                redirect_stdout(stdout),
            ):
                exit_code = cmd_screen(args)

            self.assertEqual(exit_code, 0)
            importer.assert_not_called()

    def test_cli_quote_prints_realtime_table_with_headers_and_requested_order(self) -> None:
        class FakeQuoteProvider:
            def __init__(self, settings) -> None:
                self.settings = settings

            def load_realtime_quotes(self, *, symbols=None, universe_id=None):
                return pd.DataFrame(
                    [
                        {"ts_code": "600519.SH", "name": "贵州茅台", "close": 40.0, "pct_chg": -2.5},
                        {"ts_code": "000001.SZ", "name": "平安银行", "close": 30.0, "pct_chg": 5.0},
                    ]
                )

            def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
                return pd.DataFrame(
                    _quote_history("000001.SZ", close=10.0) + _quote_history("600519.SH", close=20.0)
                )

            def load_realtime_daily_quotes(self, symbols, *, trade_date):
                return pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": trade_date, "close": 30.0},
                        {"ts_code": "600519.SH", "trade_date": trade_date, "close": 40.0},
                    ]
                )

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeQuoteProvider), redirect_stdout(stdout):
                exit_code = main(["quote", "--stocks", "000001,600519", "--db", str(db)])

            self.assertEqual(exit_code, 0)
            lines = stdout.getvalue().strip().splitlines()
            self.assertEqual(
                lines[0].split(),
                ["序号", "股票代码", "股票名称", "现价", "涨跌幅", "周线", "月线", "季线", "年线"],
            )
            self.assertEqual(" ".join(lines[1].split()), "1. 000001.SZ 平安银行 30.00 +5.00% 14.00 11.00 10.33 10.08")
            self.assertEqual(" ".join(lines[2].split()), "2. 600519.SH 贵州茅台 40.00 -2.50% 24.00 21.00 20.33 20.08")

    def test_cli_quote_uses_placeholders_for_missing_quote_and_short_history(self) -> None:
        class FakeQuoteProvider:
            def __init__(self, settings) -> None:
                self.settings = settings

            def load_realtime_quotes(self, *, symbols=None, universe_id=None):
                return pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "name": "平安银行", "close": 30.0, "pct_chg": 5.0},
                    ]
                )

            def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
                return pd.DataFrame(
                    _quote_history("000001.SZ", close=10.0, days=30) + _quote_history("600519.SH", close=20.0, days=260)
                )

            def load_realtime_daily_quotes(self, symbols, *, trade_date):
                return pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": trade_date, "close": 30.0},
                        {"ts_code": "600519.SH", "trade_date": trade_date, "close": 40.0},
                    ]
                )

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeQuoteProvider), redirect_stdout(stdout):
                exit_code = main(["quote", "--stocks", "000001,600519", "--db", str(db)])

            self.assertEqual(exit_code, 0)
            lines = stdout.getvalue().strip().splitlines()
            self.assertEqual(" ".join(lines[1].split()), "1. 000001.SZ 平安银行 30.00 +5.00% 14.00 11.00 -- --")
            self.assertEqual(" ".join(lines[2].split()), "2. 600519.SH -- -- 24.00 21.00 20.33 20.08")

    def test_cli_quote_falls_back_to_stock_basic_name_when_quote_name_missing(self) -> None:
        class FakeQuoteProvider:
            def __init__(self, settings) -> None:
                self.settings = settings

            def load_realtime_quotes(self, *, symbols=None, universe_id=None):
                return pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "name": "", "close": 30.0, "pct_chg": 5.0},
                    ]
                )

            def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
                return pd.DataFrame(_quote_history("000001.SZ", close=10.0))

            def load_realtime_daily_quotes(self, symbols, *, trade_date):
                return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": trade_date, "close": 30.0}])

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db)
            storage.upsert_stock_basic(make_stock_basic({"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"}))
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeQuoteProvider), redirect_stdout(stdout):
                exit_code = main(["quote", "--stocks", "000001", "--db", str(db)])

            self.assertEqual(exit_code, 0)
            self.assertIn("1. 000001.SZ 平安银行 30.00 +5.00%", " ".join(stdout.getvalue().split()))

    def test_cli_quote_raises_when_all_realtime_quotes_missing(self) -> None:
        class FakeQuoteProvider:
            def __init__(self, settings) -> None:
                self.settings = settings

            def load_realtime_quotes(self, *, symbols=None, universe_id=None):
                return pd.DataFrame()

            def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
                return pd.DataFrame(_quote_history("000001.SZ", close=10.0))

            def load_realtime_daily_quotes(self, symbols, *, trade_date):
                return pd.DataFrame()

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sats.duckdb"

            with patch("sats.cli.AStockDataProvider", FakeQuoteProvider):
                with self.assertRaises(SystemExit) as exc:
                    main(["quote", "--stocks", "000001", "--db", str(db)])

            self.assertEqual(str(exc.exception), "未获取到实时行情")

    def test_cli_screen_accepts_price_volume_ma_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                trade_date="20260430",
                rule="price-volume-ma",
                db=db_path,
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTushareProvider), redirect_stdout(stdout):
                exit_code = cmd_screen(args)

            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date="20260430",
                rule_name="price_volume_ma",
                passed=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行")
            self.assertEqual(len(rows), 1)

    def test_cli_screen_accepts_rps_breakout_and_requests_required_trade_days(self) -> None:
        class FakeRpsProvider:
            calls: list[dict[str, object]] = []

            def __init__(self, settings) -> None:
                self.settings = settings

            def load_all_screening_inputs(
                self,
                trade_date: str,
                *,
                storage: DuckDBStorage,
                trade_days: int = 80,
                rule_name: str | None = None,
            ) -> list[ScreeningInput]:
                self.calls.append({"trade_days": trade_days, "rule_name": rule_name})
                return [
                    ScreeningInput(
                        ts_code="000001.SZ",
                        trade_date=trade_date,
                        daily=make_rps_daily("000001.SZ", gain=0.10, end=trade_date),
                        daily_basic=make_daily_basic(end=trade_date),
                        stock_basic={"name": "普通股份"},
                    ),
                    ScreeningInput(
                        ts_code="000002.SZ",
                        trade_date=trade_date,
                        daily=make_rps_daily("000002.SZ", gain=1.00, end=trade_date),
                        daily_basic=make_daily_basic(end=trade_date),
                        stock_basic={"name": "强势股份"},
                    ),
                ]

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                trade_date="20260430",
                rule="rps-breakout",
                db=db_path,
                select_watchlist=False,
                no_select_watchlist=True,
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeRpsProvider), redirect_stdout(stdout):
                exit_code = cmd_screen(args)

            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date="20260430",
                rule_name="rps_breakout",
                passed=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(FakeRpsProvider.calls[0], {"trade_days": 121, "rule_name": "rps_breakout"})
            self.assertEqual(stdout.getvalue().strip(), "1. 000002.SZ 强势股份")
            self.assertEqual(len(rows), 1)

    def test_cli_screen_accepts_chan_third_buy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                trade_date="20260430",
                rule="chan-third-buy",
                db=db_path,
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTushareProvider), redirect_stdout(stdout):
                exit_code = cmd_screen(args)

            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date="20260430",
                rule_name="chan_third_buy",
                passed=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行")
            self.assertEqual(len(rows), 1)

    def test_cli_screen_accepts_chan_composite_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                trade_date="20260430",
                rule="chan-composite",
                db=db_path,
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTushareProvider), redirect_stdout(stdout):
                exit_code = cmd_screen(args)

            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date="20260430",
                rule_name="chan_composite",
                passed=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行 三买")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["metrics"]["matched_chan_rules"], ["三买"])
            stocks = DuckDBStorage(db_path).list_screening_stocks(
                trade_date="20260430",
                rule_name="chan_composite",
                passed=True,
            )
            self.assertEqual(stocks[0]["matched_labels"], ["三买"])

    def test_cli_screen_select_watchlist_receives_same_chan_rows_as_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                trade_date="20260430",
                rule="chan-composite",
                db=db_path,
                select_watchlist=True,
                no_select_watchlist=False,
            )
            stdout = io.StringIO()

            with (
                patch("sats.cli.AStockDataProvider", FakeTushareProvider),
                patch("sats.cli.select_and_import_watchlist") as importer,
                redirect_stdout(stdout),
            ):
                exit_code = cmd_screen(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行 三买")
            rows = importer.call_args.args[1]
            self.assertEqual(rows[0]["ts_code"], "000001.SZ")
            self.assertEqual(rows[0]["name"], "平安银行")
            self.assertEqual(rows[0]["matched_labels"], ["三买"])

    def test_cli_screen_accepts_chan_signals_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                trade_date="20260430",
                rule="chan-signals",
                db=db_path,
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTushareProvider), redirect_stdout(stdout):
                exit_code = cmd_screen(args)

            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date="20260430",
                rule_name="chan_signals",
                passed=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行 三买,持股等待")
            self.assertEqual(len(rows), 1)
            self.assertIn("三买", rows[0]["metrics"]["matched_chan_rules"])

    def test_cli_screen_accepts_signal_composite_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                trade_date="20260430",
                rule="abu-signals",
                db=db_path,
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTushareProvider), redirect_stdout(stdout):
                exit_code = cmd_screen(args)

            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date="20260430",
                rule_name="signal_composite",
                passed=True,
            )
            stocks = DuckDBStorage(db_path).list_screening_stocks(
                trade_date="20260430",
                rule_name="signal_composite",
                passed=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["metrics"]["matched_signal_labels"])
            self.assertTrue(stocks[0]["matched_labels"])
            self.assertIn("000001.SZ 平安银行", stdout.getvalue())

    def test_cli_screen_accepts_monthly_base_breakout_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTushareProvider), redirect_stdout(stdout):
                exit_code = main(
                    [
                        "screen",
                        "--trade-date",
                        "20260430",
                        "--rule",
                        "monthly-base-breakout",
                        "--db",
                        str(db_path),
                    ]
                )

            rows = DuckDBStorage(db_path).list_screening_results(
                trade_date="20260430",
                rule_name="monthly_base_breakout",
                passed=True,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行")
            self.assertEqual(len(rows), 1)
            self.assertIn("early_breakout", rows[0]["metrics"]["matched_stages"])

    def test_cli_analyze_chan_prints_llm_review_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule="price-volume-ma",
                chan_rule="chan-signals",
                top=20,
                stocks=None,
                db=Path(tmp) / "sats.duckdb",
            )
            stdout = io.StringIO()
            fake_result = SimpleNamespace(
                message="",
                reviews=[{"ts_code": "000001.SZ", "name": "平安银行", "buy_point_quality": "高", "summary": "确认"}],
                report_path=Path(tmp) / "report.md",
            )
            captured = {}

            def fake_review_runner(**kwargs):
                captured["review_kwargs"] = kwargs
                return fake_result

            with patch("sats.cli.run_chan_llm_review", side_effect=fake_review_runner), redirect_stdout(stdout):
                exit_code = cmd_analyze_chan(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["review_kwargs"]["screening_rule_name"], "price_volume_ma")
            self.assertEqual(captured["review_kwargs"]["chan_rule_name"], "chan_signals")
            self.assertIn("1. 000001.SZ 平安银行 高 确认", stdout.getvalue())
            self.assertIn("报告:", stdout.getvalue())

    def test_cli_analyze_chan_stocks_runs_temporary_rule_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule=None,
                chan_rule="chan-composite",
                top=20,
                stocks="000001",
                db=Path(tmp) / "sats.duckdb",
            )
            stock_input = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_chan_daily(),
                daily_basic=make_daily_basic(),
                stock_basic={"name": "平安银行"},
                metadata={"minute_30m": make_chan_minute_30m()},
            )
            temporary_result = ScreeningResult(
                trade_date="20260430",
                ts_code="000001.SZ",
                rule_name="chan_composite",
                passed=False,
                score=0,
                matched_conditions=[],
                failed_conditions=["chan_first_buy"],
                metrics={"risk_flags": ["未形成买点"]},
            )
            fake_review = SimpleNamespace(
                message="",
                reviews=[{"ts_code": "000001.SZ", "name": "平安银行", "buy_point_quality": "低", "summary": "未确认"}],
                report_path=Path(tmp) / "report.md",
            )
            captured = {}

            class Provider:
                def __init__(self, settings) -> None:
                    pass

                def load_screening_inputs(self, symbols, trade_date, **kwargs):
                    captured["symbols"] = symbols
                    captured["trade_date"] = trade_date
                    captured["provider_kwargs"] = kwargs
                    return [stock_input]

            def fake_review_runner(**kwargs):
                captured["review_kwargs"] = kwargs
                return fake_review

            def fake_ensure(symbols, trade_date, **kwargs):
                captured["ensure_symbols"] = symbols
                captured["ensure_trade_date"] = trade_date
                captured["ensure_kwargs"] = kwargs
                return {
                    "000001.SZ": {
                        "minute_curves": {
                            "15m": {"rows": [{"trade_time": "2026-04-30 10:00:00", "close": 10.1}]},
                            "30m": {"rows": [{"trade_time": "2026-04-30 10:30:00", "close": 10.2}]},
                        },
                        "data_sources": {"minute_15m": "tickflow_history", "minute_30m": "tickflow_history"},
                        "missing_fields": [],
                    }
                }

            stdout = io.StringIO()
            with (
                patch("sats.cli.AStockDataProvider", Provider),
                patch("sats.cli.ensure_stock_analysis_data", side_effect=fake_ensure),
                patch("sats.cli.evaluate_inputs", return_value=[temporary_result]),
                patch("sats.cli.run_chan_llm_review", side_effect=fake_review_runner),
                redirect_stdout(stdout),
            ):
                exit_code = cmd_analyze_chan(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["ensure_symbols"], ["000001.SZ"])
            self.assertEqual(captured["ensure_trade_date"], "20260430")
            self.assertEqual(captured["ensure_kwargs"]["periods"], ("15m", "30m"))
            self.assertEqual(captured["symbols"], ["000001.SZ"])
            self.assertEqual(captured["trade_date"], "20260430")
            self.assertEqual(captured["provider_kwargs"]["rule_name"], "chan_composite")
            self.assertEqual(captured["review_kwargs"]["screening_results"], [temporary_result])
            self.assertEqual(captured["review_kwargs"]["screening_rule_name"], "chan_composite")
            self.assertEqual(captured["review_kwargs"]["chan_rule_name"], "chan_composite")
            self.assertEqual(captured["review_kwargs"]["names"], {"000001.SZ": "平安银行"})
            self.assertIn("minute_curves", captured["review_kwargs"]["stock_contexts"]["000001.SZ"])
            self.assertEqual(DuckDBStorage(Path(tmp) / "sats.duckdb").list_screening_results(), [])
            self.assertIn("1. 000001.SZ 平安银行 低 未确认", stdout.getvalue())

    def test_cli_analyze_chan_stocks_stops_when_data_prefetch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule=None,
                chan_rule="chan-signals",
                top=20,
                stocks="000001",
                db=Path(tmp) / "sats.duckdb",
            )
            with (
                patch("sats.cli.ensure_stock_analysis_data", side_effect=ValueError("缺少真实30m分钟K数据")),
                patch("sats.cli.AStockDataProvider") as provider,
                patch("sats.cli.run_chan_llm_review") as review,
            ):
                with self.assertRaises(SystemExit) as raised:
                    cmd_analyze_chan(args)

            self.assertEqual(str(raised.exception), "缺少真实30m分钟K数据")
            provider.return_value.load_screening_inputs.assert_not_called()
            review.assert_not_called()

    def test_cli_analyze_chan_rejects_rule_with_stocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260430",
                rule="chan-composite",
                chan_rule="chan-signals",
                top=20,
                stocks="000001",
                db=Path(tmp) / "sats.duckdb",
            )

            with self.assertRaises(SystemExit) as exc:
                cmd_analyze_chan(args)

            self.assertIn("--rule only supports saved screening results", str(exc.exception))

    def test_cli_results_prints_numbered_stock_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_basic(
                make_stock_basic({"ts_code": "000001.SZ", "name": "平安银行"})
            )
            data = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_passing_daily(),
                daily_basic=make_daily_basic(),
                industry_daily=make_benchmark(),
            )
            evaluate_and_store([data], rule_name="ma-volume-relative-strength", storage=storage)
            args = SimpleNamespace(
                trade_date="20260430",
                rule=None,
                passed=True,
                db=Path(tmp) / "sats.duckdb",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_results(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行 ma_volume_relative_strength")

    def test_cli_results_accepts_price_volume_ma_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(
                make_stock_basic({"ts_code": "000001.SZ", "name": "平安银行"})
            )
            data = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_price_volume_daily(),
                daily_basic=make_daily_basic(),
                stock_basic={"name": "平安银行"},
            )
            evaluate_and_store([data], rule_name="price-volume-ma", storage=storage)
            args = SimpleNamespace(
                trade_date="20260430",
                rule="price-volume-ma",
                passed=True,
                db=db_path,
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_results(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行 price_volume_ma")

    def test_cli_results_prints_chan_matched_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(
                make_stock_basic({"ts_code": "000001.SZ", "name": "平安银行"})
            )
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260430",
                        ts_code="000001.SZ",
                        rule_name="chan_composite",
                        passed=True,
                        score=80,
                        matched_conditions=["chan_third_buy"],
                        failed_conditions=[],
                        metrics={"matched_chan_rules": ["三买"]},
                    )
                ]
            )
            args = SimpleNamespace(
                trade_date="20260430",
                rule="chan-composite",
                passed=True,
                db=db_path,
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_results(args)

            stocks = storage.list_screening_stocks(trade_date="20260430", rule_name="chan_composite", passed=True)
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. 000001.SZ 平安银行 chan_composite 三买")
            self.assertEqual(stocks[0]["rule_name"], "chan_composite")
            self.assertEqual(stocks[0]["matched_labels"], ["三买"])

    def test_cli_results_prints_rule_name_for_multiple_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                        {"ts_code": "000002.SZ", "symbol": "000002", "name": "万科A"},
                    ]
                )
            )
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260430",
                        ts_code="000001.SZ",
                        rule_name="chan_composite",
                        passed=True,
                        score=90,
                        matched_conditions=["chan_first_buy", "chan_second_buy"],
                        failed_conditions=[],
                        metrics={"matched_chan_rules": ["一买", "二买"]},
                    ),
                    ScreeningResult(
                        trade_date="20260430",
                        ts_code="000002.SZ",
                        rule_name="price_volume_ma",
                        passed=True,
                        score=80,
                        matched_conditions=[],
                        failed_conditions=[],
                        metrics={},
                    ),
                ]
            )
            args = SimpleNamespace(
                trade_date="20260430",
                rule=None,
                passed=True,
                db=db_path,
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_results(args)

            self.assertEqual(exit_code, 0)
            lines = stdout.getvalue().strip().splitlines()
            self.assertEqual(
                "\n".join(lines),
                "1. 000001.SZ 平安银行 chan_composite  一买,二买\n"
                "2. 000002.SZ 万科A    price_volume_ma",
            )
            rule_starts = [
                get_cwidth(lines[0][: lines[0].index("chan_composite")]),
                get_cwidth(lines[1][: lines[1].index("price_volume_ma")]),
            ]
            self.assertEqual(rule_starts[0], rule_starts[1])

    def test_screening_stock_labels_empty_for_non_chan_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260430",
                        ts_code="000001.SZ",
                        rule_name="price_volume_ma",
                        passed=True,
                        score=80,
                        matched_conditions=[],
                        failed_conditions=[],
                        metrics={},
                    )
                ]
            )

            stocks = storage.list_screening_stocks(trade_date="20260430", rule_name="price_volume_ma", passed=True)

            self.assertEqual(stocks[0]["matched_labels"], [])

    def test_screening_stock_labels_use_signal_composite_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260430",
                        ts_code="000938.SZ",
                        rule_name="signal_composite",
                        passed=True,
                        score=88,
                        matched_conditions=["ma_dragon_sea_kline"],
                        failed_conditions=[],
                        metrics={"matched_signal_labels": ["蛟龙出海买入点 ✚ K线信号"]},
                    )
                ]
            )

            stocks = storage.list_screening_stocks(trade_date="20260430", rule_name="signal_composite", passed=True)

            self.assertEqual(stocks[0]["matched_labels"], ["蛟龙出海买入点 ✚ K线信号"])

    def test_storage_lists_distinct_screening_rule_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            first = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_passing_daily(),
                daily_basic=make_daily_basic(),
                industry_daily=make_benchmark(),
            )
            second = ScreeningInput(
                ts_code="000002.SZ",
                trade_date="20260430",
                daily=make_passing_daily(),
                daily_basic=make_daily_basic(),
                industry_daily=make_benchmark(),
            )

            evaluate_and_store([first], rule_name="ma-volume-relative-strength", storage=storage)
            evaluate_and_store([second], rule_name="ma-volume-relative-strength", storage=storage)

            self.assertEqual(storage.list_screening_rule_names(), ["ma_volume_relative_strength"])

    def test_cli_result_rules_prints_numbered_rule_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            data = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_passing_daily(),
                daily_basic=make_daily_basic(),
                industry_daily=make_benchmark(),
            )
            evaluate_and_store([data], rule_name="ma-volume-relative-strength", storage=storage)
            args = SimpleNamespace(db=Path(tmp) / "sats.duckdb")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_result_rules(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1. ma_volume_relative_strength")

    def test_cli_result_rules_prints_empty_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(db=Path(tmp) / "sats.duckdb")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_result_rules(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "无结果")

    def test_cli_rejects_legacy_symbols_option(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args(["screen", "--trade-date", "20260430", "--symbols", "000001.SZ"])


if __name__ == "__main__":
    unittest.main()
