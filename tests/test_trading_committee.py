from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.analysis.trading_committee import (
    TradingCommitteeRequest,
    TradingCommitteeResult,
    run_trading_committee,
)
from sats.cli import main
from sats.indicators import IndicatorInput
from sats.repl import CLI_COMMANDS, help_text, repl_command_to_argv
from tests.fixtures import make_price_volume_daily, make_trade_dates


class FakeCommitteeProvider:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        if self.empty:
            return []
        result = []
        for symbol in symbols:
            daily = make_price_volume_daily(end=trade_date)
            daily["ts_code"] = symbol
            daily_basic = pd.DataFrame(
                {
                    "ts_code": [symbol],
                    "trade_date": [trade_date],
                    "pe": [18.0],
                    "pb": [1.5],
                    "total_mv": [1_000_000.0],
                    "turnover_rate": [2.8],
                }
            )
            fundamentals = pd.DataFrame(
                {
                    "ts_code": [symbol],
                    "end_date": [trade_date],
                    "revenue": [120.0],
                    "profit": [18.0],
                    "roe": [15.0],
                    "debt_to_assets": [38.0],
                }
            )
            moneyflow = pd.DataFrame(
                {
                    "ts_code": [symbol] * 10,
                    "trade_date": make_trade_dates(10, end=trade_date),
                    "main_net_amount": [100.0] * 10,
                }
            )
            result.append(
                IndicatorInput(
                    ts_code=symbol,
                    trade_date=trade_date,
                    daily=daily,
                    daily_basic=daily_basic,
                    moneyflow=moneyflow,
                    fundamentals=fundamentals,
                    stock_basic={"name": "平安银行", "industry": "银行"},
                    data_sources={"daily": "fake_daily", "daily_basic": "fake_daily_basic", "fundamentals": "fake_fundamentals"},
                )
            )
        return result

    def load_realtime_quote_lookup(self, symbols):
        return {symbol: {"ts_code": symbol, "name": "平安银行", "close": 12.3, "data_source": "fake_quote"} for symbol in symbols}

    def load_chip_context(self, symbols):
        return {symbol: {"profit_ratio": 55.0, "avg_cost": 10.8, "data_source": "fake_chip"} for symbol in symbols}

    def load_fundamental_context(self, symbols):
        return {symbol: {"industry": "银行", "data_source": "fake_basic"} for symbol in symbols}

    def load_statement_context(self, symbols, *, trade_date):
        return {symbol: {"items": [{"dataset": "income", "revenue": 120.0}], "data_source": "fake_statement", "missing_fields": []} for symbol in symbols}

    def load_company_news_context(self, symbols, *, trade_date):
        return {symbol: {"items": [{"title": "年度报告发布"}], "data_source": "fake_news", "missing_fields": []} for symbol in symbols}

    def load_macro_news_context(self, *, trade_date):
        return {"items": [{"title": "宏观政策"}], "data_source": "fake_macro", "missing_fields": []}

    def load_holder_activity_context(self, symbols, *, trade_date):
        return {symbol: {"items": [{"title": "股东增持"}], "data_source": "fake_holder", "missing_fields": []} for symbol in symbols}

    def load_social_sentiment_context(self, symbols):
        return {symbol: {"total_hits": 2, "mentions": {}, "data_source": "fake_social", "missing_fields": []} for symbol in symbols}

    def load_event_context(self, *args, **kwargs):
        return {"items": [{"title": "业绩说明会"}], "data_source": "fake_events", "missing_fields": []}

    def load_hot_sector_context(self, trade_date, *, storage):
        return {"stock_hot_sectors": {"000001.SZ": [{"name": "银行", "rank": 1}]}, "data_source": "fake_hot_sector"}

    def load_market_breadth(self):
        return {"total": 100, "up_count": 63, "down_count": 30}, "fake_breadth"

    def load_limit_sentiment(self, trade_date, *, storage):
        return {"limit_up_count": 35, "limit_down_count": 4, "data_source": "fake_limit"}


class TradingCommitteeServiceTest(unittest.TestCase):
    def test_run_trading_committee_no_llm_prints_full_committee_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp))
            result = run_trading_committee(
                ["000001"],
                trade_date="20260514",
                settings=settings,
                storage=SimpleNamespace(),
                astock_provider=FakeCommitteeProvider(),
                llm_enabled=False,
                report=True,
                reports_dir=Path(tmp) / "reports",
            )

            self.assertEqual(len(result.reports), 1)
            report = result.reports[0]
            self.assertEqual(report.ts_code, "000001.SZ")
            self.assertIn(report.final_rating, {"Buy", "Overweight", "Hold", "Underweight", "Sell"})
            markdown = result.to_markdown()
            self.assertIn("# SATS 投资委员会报告", markdown)
            self.assertIn("### 分析师团队", markdown)
            self.assertIn("### 风控团队", markdown)
            self.assertIn("已按参数 --no-llm 跳过大模型", markdown)
            self.assertNotIn("大模型不可用，原因", markdown)
            self.assertFalse(result.llm_unavailable)
            self.assertTrue(result.llm_diagnostics.disabled)
            self.assertIsNotNone(result.markdown_report_path)
            self.assertTrue(result.markdown_report_path.exists())

    def test_run_trading_committee_records_llm_build_error(self) -> None:
        settings = SimpleNamespace(
            db_path=Path("fake.duckdb"),
            project_root=Path("."),
            llm_provider="mimo",
            llm_profile="XIAOMIMIMO",
            openai_model="mimo-v2.5-pro",
            openai_base_url="https://api.xiaomimimo.com/v1",
            llm_timeout_seconds=20,
        )

        with patch("sats.analysis.trading_committee.ChatLLM", side_effect=RuntimeError("missing provider")):
            result = run_trading_committee(
                ["000001"],
                trade_date="20260514",
                settings=settings,
                storage=SimpleNamespace(),
                astock_provider=FakeCommitteeProvider(),
                report=False,
            )

        self.assertTrue(result.llm_unavailable)
        self.assertEqual(result.llm_diagnostics.failure_stage, "build")
        self.assertEqual(result.llm_diagnostics.error_type, "RuntimeError")
        self.assertIn("missing provider", result.llm_diagnostics.error_message)
        payload = result.to_dict()
        self.assertEqual(payload["llm_diagnostics"]["model"], "mimo-v2.5-pro")
        self.assertIn("大模型不可用", result.to_markdown())
        self.assertIn("RuntimeError: missing provider", result.to_markdown())

    def test_run_trading_committee_records_llm_call_error(self) -> None:
        class FailingLLM:
            def chat(self, messages, timeout=None):
                raise TimeoutError("model timed out")

        settings = SimpleNamespace(
            db_path=Path("fake.duckdb"),
            project_root=Path("."),
            llm_provider="mimo",
            llm_profile="XIAOMIMIMO",
            openai_model="mimo-v2.5-pro",
            openai_base_url="https://api.xiaomimimo.com/v1",
            llm_timeout_seconds=20,
        )

        result = run_trading_committee(
            ["000001"],
            trade_date="20260514",
            settings=settings,
            storage=SimpleNamespace(),
            astock_provider=FakeCommitteeProvider(),
            llm=FailingLLM(),
            report=False,
        )

        self.assertTrue(result.llm_unavailable)
        self.assertEqual(result.llm_diagnostics.failure_stage, "analyst")
        self.assertEqual(result.llm_diagnostics.error_type, "TimeoutError")
        self.assertIn("model timed out", result.to_dict()["llm_diagnostics"]["error_message"])
        self.assertIn("阶段: analyst", result.to_markdown())

    def test_run_trading_committee_reports_no_analyzable_stock(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), project_root=Path("."))
        result = run_trading_committee(
            ["000001"],
            trade_date="20260514",
            settings=settings,
            storage=SimpleNamespace(),
            astock_provider=FakeCommitteeProvider(empty=True),
            report=False,
        )

        self.assertEqual(result.message, "无可分析股票")
        self.assertEqual(result.reports, ())


class TradingCommitteeEntrypointTest(unittest.TestCase):
    def test_cli_trading_committee_prints_json(self) -> None:
        request = TradingCommitteeRequest(symbols=("000001.SZ",), trade_date="20260514", report=False)
        fake_result = TradingCommitteeResult(request=request, message="无可分析股票")
        settings = SimpleNamespace(db_path=Path("fake.duckdb"), project_root=Path("."))
        stdout = io.StringIO()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.DuckDBStorage", return_value=SimpleNamespace()),
            patch("sats.cli.AStockDataProvider", return_value=SimpleNamespace()),
            patch("sats.cli.run_trading_committee", return_value=fake_result) as runner,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["trading-committee", "--stocks", "000001", "--trade-date", "20260514", "--json", "--noreport"]), 0)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["message"], "无可分析股票")
        runner.assert_called_once()

    def test_cli_trading_committee_prints_markdown_report_by_default(self) -> None:
        request = TradingCommitteeRequest(symbols=("000001.SZ",), trade_date="20260514", report=True)
        fake_result = SimpleNamespace(
            request=request,
            message="",
            markdown_report_path=Path("reports/trading_committee/fake.md"),
            json_artifact_path=Path("reports/trading_committee/fake.json"),
            to_markdown=lambda: "# SATS 投资委员会报告\n\n## 000001.SZ 平安银行\n\n- 最终评级: Hold\n",
        )
        settings = SimpleNamespace(db_path=Path("fake.duckdb"), project_root=Path("."))
        stdout = io.StringIO()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.DuckDBStorage", return_value=SimpleNamespace()),
            patch("sats.cli.AStockDataProvider", return_value=SimpleNamespace()),
            patch("sats.cli.run_trading_committee", return_value=fake_result),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["trading-committee", "--stocks", "000001", "--trade-date", "20260514"]), 0)

        output = stdout.getvalue()
        self.assertIn("# SATS 投资委员会报告", output)
        self.assertIn("报告: reports/trading_committee/fake.md", output)

    def test_cli_model_ping_success(self) -> None:
        settings = SimpleNamespace(
            llm_provider="mimo",
            llm_profile="XIAOMIMIMO",
            openai_model="mimo-v2.5-pro",
            openai_base_url="https://api.xiaomimimo.com/v1",
            llm_timeout_seconds=30,
        )

        class FakeChatLLM:
            def __init__(self, *, timeout_seconds=None):
                self.timeout_seconds = timeout_seconds

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content="OK", finish_reason="stop")

        stdout = io.StringIO()
        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.ChatLLM", FakeChatLLM),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["model", "ping", "--timeout", "5"]), 0)

        output = stdout.getvalue()
        self.assertIn("available: yes", output)
        self.assertIn("response: OK", output)
        self.assertNotIn("api_key", output)

    def test_cli_model_ping_failure(self) -> None:
        settings = SimpleNamespace(
            llm_provider="mimo",
            llm_profile="XIAOMIMIMO",
            openai_model="mimo-v2.5-pro",
            openai_base_url="https://api.xiaomimimo.com/v1",
            llm_timeout_seconds=30,
        )

        class FailingChatLLM:
            def __init__(self, *, timeout_seconds=None):
                pass

            def chat(self, messages, timeout=None):
                raise ConnectionError("DNS failed")

        stdout = io.StringIO()
        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.ChatLLM", FailingChatLLM),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["model", "ping"]), 1)

        output = stdout.getvalue()
        self.assertIn("available: no", output)
        self.assertIn("error_type: ConnectionError", output)
        self.assertIn("DNS failed", output)

    def test_cli_model_ping_json(self) -> None:
        settings = SimpleNamespace(
            llm_provider="mimo",
            llm_profile="XIAOMIMIMO",
            openai_model="mimo-v2.5-pro",
            openai_base_url="https://api.xiaomimimo.com/v1",
            llm_timeout_seconds=30,
        )

        class FakeChatLLM:
            def __init__(self, *, timeout_seconds=None):
                pass

            def chat(self, messages, timeout=None):
                return SimpleNamespace(content="OK", finish_reason="stop")

        stdout = io.StringIO()
        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.ChatLLM", FakeChatLLM),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["model", "ping", "--json"]), 0)

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["available"])
        self.assertEqual(payload["provider"], "mimo")
        self.assertEqual(payload["response"], "OK")

    def test_repl_exposes_trading_committee_command(self) -> None:
        self.assertIn("trading-committee", CLI_COMMANDS)
        self.assertEqual(
            repl_command_to_argv("/trading-committee --stocks 000001 --debate-rounds 2"),
            ["trading-committee", "--stocks", "000001", "--debate-rounds", "2"],
        )
        self.assertEqual(
            repl_command_to_argv("/model ping --timeout 20"),
            ["model", "ping", "--timeout", "20"],
        )
        self.assertIn("/trading-committee", help_text())
        self.assertIn("/model ping", help_text())


if __name__ == "__main__":
    unittest.main()
