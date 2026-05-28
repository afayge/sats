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

from sats.cli import build_parser, cmd_minute_k, cmd_minute_k_clear, cmd_result_rules, cmd_results, cmd_screen
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
    make_passing_daily,
    make_price_volume_daily,
)


def make_stock_basic(row: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame([row])


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

    def load_realtime_minute_klines(self, symbols, *, period="1m", count=None, storage=None):
        frame = _minute_k_frame(symbols[0], period=period, trade_time="2026-05-14 09:31:00")
        if storage is not None:
            storage.upsert_stock_minute(frame)
        return frame

    def load_historical_minute_klines(
        self,
        symbols,
        *,
        period="1m",
        start_time=None,
        end_time=None,
        count=None,
        storage=None,
    ):
        frame = _minute_k_frame(symbols[0], period=period, trade_time="2026-05-13 10:00:00")
        if storage is not None:
            storage.upsert_stock_minute(frame)
        return frame


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

    def test_cli_minute_k_prints_rows_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            args = SimpleNamespace(
                symbols="000001",
                period="1m",
                mode="realtime",
                count=20,
                start_date=None,
                end_date=None,
                db=db_path,
            )
            stdout = io.StringIO()

            with patch("sats.cli.AStockDataProvider", FakeTickFlowProvider), redirect_stdout(stdout):
                exit_code = cmd_minute_k(args)

            rows = DuckDBStorage(db_path).get_stock_minute(symbols=["000001.SZ"], period="1m")
            self.assertEqual(exit_code, 0)
            self.assertIn("1. 000001.SZ 1m 2026-05-14 09:31:00", stdout.getvalue())
            self.assertEqual(len(rows), 1)

    def test_storage_deletes_stock_minute_by_date_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_minute(
                pd.concat(
                    [
                        _minute_k_frame("000001.SZ", period="1m", trade_time="2026-05-11 09:31:00"),
                        _minute_k_frame("000001.SZ", period="1m", trade_time="2026-05-12 09:31:00"),
                        _minute_k_frame("000001.SZ", period="1m", trade_time="2026-05-13 09:31:00"),
                        _minute_k_frame("000001.SZ", period="1m", trade_time="2026-05-14 09:31:00"),
                        _minute_k_frame("000001.SZ", period="1m", trade_time="2026-05-15 09:31:00"),
                    ],
                    ignore_index=True,
                )
            )

            self.assertEqual(storage.delete_stock_minute(end_date="20260512"), 2)
            self.assertEqual(storage.delete_stock_minute(start_date="20260514"), 2)
            rows = storage.get_stock_minute(symbols=["000001.SZ"], period="1m")

            self.assertEqual(rows["trade_date"].tolist(), ["20260513"])
            self.assertEqual(storage.delete_stock_minute(trade_date="20260513"), 1)
            self.assertTrue(storage.get_stock_minute(symbols=["000001.SZ"], period="1m").empty)

    def test_storage_deletes_stock_minute_by_period_and_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_minute(
                pd.concat(
                    [
                        _minute_k_frame("000001.SZ", period="1m", trade_time="2026-05-14 09:31:00"),
                        _minute_k_frame("000001.SZ", period="5m", trade_time="2026-05-14 09:35:00"),
                        _minute_k_frame("600519.SH", period="1m", trade_time="2026-05-14 09:31:00"),
                    ],
                    ignore_index=True,
                )
            )

            deleted = storage.delete_stock_minute(
                start_date="20260514",
                end_date="20260514",
                period="1m",
                symbols=["000001.SZ"],
            )
            rows = storage.get_stock_minute(trade_date="20260514")

            self.assertEqual(deleted, 1)
            self.assertEqual(sorted(rows["ts_code"].tolist()), ["000001.SZ", "600519.SH"])
            self.assertEqual(sorted(rows["period"].tolist()), ["1m", "5m"])

    def test_cli_minute_k_clear_prints_deleted_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_minute(
                pd.concat(
                    [
                        _minute_k_frame("000001.SZ", period="1m", trade_time="2026-05-14 09:31:00"),
                        _minute_k_frame("600519.SH", period="1m", trade_time="2026-05-14 09:31:00"),
                    ],
                    ignore_index=True,
                )
            )
            args = SimpleNamespace(
                trade_date=None,
                start_date=None,
                end_date="20260514",
                period="1m",
                symbols="000001",
                db=db_path,
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_minute_k_clear(args)

            rows = DuckDBStorage(db_path).get_stock_minute(trade_date="20260514")
            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "已删除 1 条分钟K数据")
            self.assertEqual(rows["ts_code"].tolist(), ["600519.SH"])

    def test_cli_minute_k_clear_prints_empty_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                trade_date="20260514",
                start_date=None,
                end_date=None,
                period=None,
                symbols=None,
                db=Path(tmp) / "sats.duckdb",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = cmd_minute_k_clear(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue().strip(), "无匹配分钟K数据")

    def test_cli_minute_k_clear_rejects_unsafe_or_invalid_dates(self) -> None:
        base = {
            "trade_date": None,
            "start_date": None,
            "end_date": None,
            "period": None,
            "symbols": None,
            "db": Path("unused.duckdb"),
        }

        invalid_args = [
            base,
            {**base, "trade_date": "20260514", "start_date": "20260511"},
            {**base, "trade_date": "2026-05-14"},
            {**base, "start_date": "20260514", "end_date": "20260511"},
        ]

        for payload in invalid_args:
            with self.assertRaises(SystemExit):
                cmd_minute_k_clear(SimpleNamespace(**payload))

    def test_cli_minute_k_clear_parser_rejects_invalid_period(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args(["minute-k-clear", "--trade-date", "20260514", "--period", "10m"])

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
