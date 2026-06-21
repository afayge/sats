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

from sats.agent.models import AgentExecutionPolicy
from sats.agent.planner import build_agent_plan
from sats.agent.tools import AgentToolContext, build_default_tool_registry
from sats.cli import main
from sats.deep_analysis import DeepAnalysisRequest, DeepAnalysisResult, run_deep_analysis
from sats.indicators import IndicatorInput
from sats.repl import CLI_COMMANDS, help_text, repl_command_to_argv
from tests.fixtures import make_price_volume_daily, make_trade_dates


class FakeDeepProvider:
    def __init__(self, *, empty: bool = False, partial: bool = False) -> None:
        self.empty = empty
        self.partial = partial

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
                    "pb": [1.4],
                    "ps": [2.2],
                    "total_mv": [1_200_000.0],
                    "circ_mv": [900_000.0],
                    "turnover_rate": [2.5],
                }
            )
            dates = make_trade_dates(2, end=trade_date)
            fundamentals = pd.DataFrame(
                {
                    "ts_code": [symbol, symbol],
                    "end_date": dates,
                    "revenue": [100.0, 125.0],
                    "profit": [12.0, 16.0],
                    "roe": [16.0, 18.0],
                    "debt_to_assets": [38.0, 36.0],
                }
            )
            moneyflow = pd.DataFrame(
                {
                    "ts_code": [symbol] * 10,
                    "trade_date": make_trade_dates(10, end=trade_date),
                    "main_net_amount": [100.0] * 10,
                }
            )
            if self.partial:
                daily_basic = pd.DataFrame()
                fundamentals = pd.DataFrame()
                moneyflow = pd.DataFrame()
            result.append(
                IndicatorInput(
                    ts_code=symbol,
                    trade_date=trade_date,
                    daily=daily,
                    daily_basic=daily_basic,
                    moneyflow=moneyflow,
                    fundamentals=fundamentals,
                    stock_basic={"name": "平安银行", "industry": "银行", "list_date": "19910403"},
                    data_sources={"daily": "fake_daily", "daily_basic": "fake_daily_basic", "fundamentals": "fake_fundamentals"},
                )
            )
        return result

    def load_realtime_quote_lookup(self, symbols):
        return {symbol: {"ts_code": symbol, "name": "平安银行", "close": 12.3} for symbol in symbols}

    def load_chip_context(self, symbols):
        return {}

    def load_fundamental_context(self, symbols):
        return {symbol: {"industry": "银行"} for symbol in symbols}

    def load_hot_sector_context(self, trade_date, *, storage):
        return {"stock_hot_sectors": {"000001.SZ": [{"name": "银行", "rank": 1}]}, "data_source": "fake_hot_sector"}

    def load_market_breadth(self):
        return {"total": 100, "up_count": 65, "down_count": 30}, "fake_breadth"

    def load_limit_sentiment(self, trade_date, *, storage):
        return {"limit_up_count": 40, "limit_down_count": 5}

    def load_event_context(self, *args, **kwargs):
        return {"items": [{"title": "业绩说明会"}], "data_source": "fake_events"}

    def load_news_context(self, *args, **kwargs):
        return {"items": [{"title": "年度报告发布"}], "data_source": "fake_news"}


class FakeLLM:
    def chat(self, messages, timeout=None):
        return SimpleNamespace(content="证据显示质量较好，但同业和基金持仓仍缺口明显。")


class DeepAnalysisServiceTest(unittest.TestCase):
    def test_run_deep_analysis_builds_core_closure_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp))
            result = run_deep_analysis(
                ["000001"],
                trade_date="20260514",
                settings=settings,
                storage=SimpleNamespace(),
                astock_provider=FakeDeepProvider(),
                reports_dir=Path(tmp) / "reports",
                llm_review=False,
            )

            self.assertEqual(len(result.analyses), 1)
            analysis = result.analyses[0]
            self.assertEqual(analysis.ts_code, "000001.SZ")
            self.assertEqual(len(analysis.dimensions), 12)
            self.assertEqual(len(analysis.investor_votes), 12)
            self.assertGreater(analysis.overall_score, 0)
            self.assertIsNotNone(result.markdown_report_path)
            self.assertTrue(result.markdown_report_path.exists())
            self.assertIn("估值观察区", result.to_markdown())

    def test_run_deep_analysis_marks_missing_dimensions_without_fabricating(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), project_root=Path("."))
        result = run_deep_analysis(
            ["000001"],
            trade_date="20260514",
            settings=settings,
            storage=SimpleNamespace(),
            astock_provider=FakeDeepProvider(partial=True),
            report=False,
            llm_review=False,
        )

        analysis = result.analyses[0]
        by_key = {item.key: item for item in analysis.dimensions}
        self.assertIn(by_key["5_fund_holders"].quality, {"missing", "partial"})
        self.assertIn("fund_holders", by_key["5_fund_holders"].missing_fields)
        self.assertIn("peer_table", by_key["4_peers"].missing_fields)

    def test_run_deep_analysis_reports_no_analyzable_stock(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), project_root=Path("."))
        result = run_deep_analysis(
            ["000001"],
            trade_date="20260514",
            settings=settings,
            storage=SimpleNamespace(),
            astock_provider=FakeDeepProvider(empty=True),
            report=False,
        )

        self.assertEqual(result.message, "无可分析股票")
        self.assertEqual(result.analyses, ())

    def test_run_deep_analysis_rejects_non_common_a_share(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), project_root=Path("."))
        with self.assertRaisesRegex(ValueError, "仅支持 A 股普通股票"):
            run_deep_analysis(
                ["512400.SH"],
                trade_date="20260514",
                settings=settings,
                storage=SimpleNamespace(),
                astock_provider=FakeDeepProvider(),
                report=False,
            )

    def test_run_deep_analysis_can_attach_optional_llm_review(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), project_root=Path("."))
        result = run_deep_analysis(
            "000001,600519",
            trade_date="20260514",
            settings=settings,
            storage=SimpleNamespace(),
            astock_provider=FakeDeepProvider(),
            report=False,
            llm=FakeLLM(),
        )

        self.assertEqual(len(result.analyses), 2)
        review = result.analyses[0].synthesis["llm_review"]
        self.assertEqual(review["status"], "ok")
        self.assertIn("同业", review["comment"])
        self.assertIn("LLM 复核", result.to_markdown())


class DeepAnalysisEntrypointTest(unittest.TestCase):
    def test_cli_deep_analysis_dispatches_and_prints_json(self) -> None:
        request = DeepAnalysisRequest(symbols=("000001.SZ",), trade_date="20260514", report=False)
        fake_result = DeepAnalysisResult(request=request, message="无可分析股票")
        settings = SimpleNamespace(db_path=Path("fake.duckdb"), project_root=Path("."))
        stdout = io.StringIO()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.DuckDBStorage", return_value=SimpleNamespace()),
            patch("sats.cli.AStockDataProvider", return_value=SimpleNamespace()),
            patch("sats.cli.run_deep_analysis", return_value=fake_result) as runner,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["deep-analysis", "--stocks", "000001", "--trade-date", "20260514", "--json", "--noreport"]), 0)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["message"], "无可分析股票")
        runner.assert_called_once()

    def test_cli_deep_analysis_prints_markdown_report_by_default(self) -> None:
        request = DeepAnalysisRequest(symbols=("000001.SZ",), trade_date="20260514", report=True)
        fake_result = SimpleNamespace(
            request=request,
            message="",
            markdown_report_path=Path("reports/deep_analysis/fake.md"),
            json_artifact_path=Path("reports/deep_analysis/fake.json"),
            to_markdown=lambda: "# SATS 原生个股深研报告\n\n## 000001.SZ 平安银行\n\n- 综合评分: 80.0/100\n",
        )
        settings = SimpleNamespace(db_path=Path("fake.duckdb"), project_root=Path("."))
        stdout = io.StringIO()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.DuckDBStorage", return_value=SimpleNamespace()),
            patch("sats.cli.AStockDataProvider", return_value=SimpleNamespace()),
            patch("sats.cli.run_deep_analysis", return_value=fake_result),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["deep-analysis", "--stocks", "000001", "--trade-date", "20260514"]), 0)

        output = stdout.getvalue()
        self.assertIn("# SATS 原生个股深研报告", output)
        self.assertIn("## 000001.SZ 平安银行", output)
        self.assertIn("报告: reports/deep_analysis/fake.md", output)

    def test_repl_exposes_deep_analysis_command(self) -> None:
        self.assertIn("deep-analysis", CLI_COMMANDS)
        self.assertEqual(
            repl_command_to_argv("/deep-analysis --stocks 000001 --phase panel"),
            ["deep-analysis", "--stocks", "000001", "--phase", "panel"],
        )
        self.assertIn("/deep-analysis", help_text())

    def test_agent_registry_and_planner_route_deep_intent_only(self) -> None:
        registry = build_default_tool_registry()
        self.assertIn("research.deep_stock_analysis", registry.names())
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        deep_plan = build_agent_plan(
            "深度分析 000001 估值和投委会观点",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        deep_tools = [step.tool_name for step in deep_plan.steps if step.kind == "tool"]
        self.assertEqual(deep_tools, ["research.deep_stock_analysis"])

        light_plan = build_agent_plan(
            "分析 002436 下周走势",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        light_tools = [step.tool_name for step in light_plan.steps if step.kind == "tool"]
        self.assertNotIn("research.deep_stock_analysis", light_tools)

    def test_agent_tool_returns_payload_without_artifacts_for_empty_result(self) -> None:
        request = DeepAnalysisRequest(symbols=("000001.SZ",), trade_date="20260514", report=False)
        fake_result = DeepAnalysisResult(request=request, message="无可分析股票")
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path(".")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            session_id="s",
            turn_id="t",
            message="深度分析 000001",
        )
        registry = build_default_tool_registry()

        with patch("sats.agent.tools.research_tools.run_deep_analysis", return_value=fake_result) as runner:
            result = registry.execute(
                "research.deep_stock_analysis",
                {"symbols": ["000001"], "trade_date": "20260514", "phase": "run"},
                context,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.data_names, ("deep_stock_analysis",))
        self.assertEqual(result.artifacts, ())
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
