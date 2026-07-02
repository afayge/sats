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
from sats.indicators import IndicatorInput
from sats.repl import CLI_COMMANDS, help_text, repl_command_to_argv
from sats.screening.base import ScreeningInput
from sats.serenity import SerenityScreenRequest, SerenityScreenResult, run_serenity_screen
from sats.serenity.scoring import FACTOR_WEIGHTS, calculate_penalties, score_serenity_candidate
from sats.serenity.service import _theme_from_query
from sats.storage.duckdb import DuckDBStorage
from tests.fixtures import make_price_volume_daily, make_trade_dates


class FakeSerenityProvider:
    def load_stock_basic(self, *, storage=None):
        return pd.DataFrame(
            [
                {
                    "ts_code": "300394.SZ",
                    "symbol": "300394",
                    "name": "天孚通信",
                    "industry": "光通信",
                    "market": "创业板",
                    "exchange": "SZSE",
                },
                {
                    "ts_code": "600519.SH",
                    "symbol": "600519",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "market": "主板",
                    "exchange": "SSE",
                },
                {
                    "ts_code": "430047.BJ",
                    "symbol": "430047",
                    "name": "北交样本",
                    "industry": "软件服务",
                    "market": "北交所",
                    "exchange": "BSE",
                },
            ]
        )

    def load_screening_inputs(self, symbols, trade_date, *, storage, trade_days=60, rule_name=None):
        result = []
        for symbol in symbols:
            daily = make_price_volume_daily(end=trade_date)
            daily["ts_code"] = symbol
            daily_basic = pd.DataFrame(
                {
                    "ts_code": [symbol],
                    "trade_date": [trade_date],
                    "total_mv": [2_000_000.0 if symbol == "300394.SZ" else 18_000_000.0],
                    "circ_mv": [1_500_000.0],
                    "pe": [24.0],
                    "pb": [3.0],
                    "turnover_rate": [3.0],
                }
            )
            basic = self.load_stock_basic()
            row = basic[basic["ts_code"] == symbol].iloc[0].dropna().to_dict()
            result.append(
                ScreeningInput(
                    ts_code=symbol,
                    trade_date=trade_date,
                    daily=daily,
                    daily_basic=daily_basic,
                    stock_basic=row,
                    metadata={"data_source": "fake_screening"},
                )
            )
        return result

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        result = []
        for symbol in symbols:
            daily = make_price_volume_daily(end=trade_date)
            daily["ts_code"] = symbol
            daily_basic = pd.DataFrame(
                {
                    "ts_code": [symbol],
                    "trade_date": [trade_date],
                    "pe": [24.0],
                    "pb": [3.0],
                    "total_mv": [2_000_000.0 if symbol == "300394.SZ" else 18_000_000.0],
                }
            )
            dates = make_trade_dates(2, end=trade_date)
            fundamentals = pd.DataFrame(
                {
                    "ts_code": [symbol, symbol],
                    "end_date": dates,
                    "revenue": [100.0, 140.0 if symbol == "300394.SZ" else 108.0],
                    "profit": [10.0, 16.0 if symbol == "300394.SZ" else 11.0],
                    "roe": [12.0, 18.0],
                    "debt_to_assets": [30.0, 28.0],
                }
            )
            basic = self.load_stock_basic()
            row = basic[basic["ts_code"] == symbol].iloc[0].dropna().to_dict()
            result.append(
                IndicatorInput(
                    ts_code=symbol,
                    trade_date=trade_date,
                    daily=daily,
                    daily_basic=daily_basic,
                    fundamentals=fundamentals,
                    stock_basic=row,
                    data_sources={"daily": "fake_daily", "fundamentals": "fake_fundamentals"},
                )
            )
        return result

    def load_statement_context(self, symbols, *, trade_date, limit=8):
        return {
            symbol: {
                "items": [{"dataset": "fina_indicator", "roe": 18.0, "data_source": "tushare_fina_indicator"}],
                "data_source": "tushare_fina_indicator",
            }
            for symbol in symbols
        }

    def load_company_news_context(self, symbols, *, trade_date, lookback_days=90, limit=20):
        result = {}
        for symbol in symbols:
            if symbol == "300394.SZ":
                title = "公司公告：CPO 光模块客户定点，认证周期长，批量交付订单进入量产"
            else:
                title = "公司公告：年度股东大会"
            result[symbol] = {
                "items": [{"dataset": "anns_d", "title": title, "data_source": "tushare_anns_d"}],
                "data_source": "tushare_anns_d",
            }
        return result

    def load_holder_activity_context(self, symbols, *, trade_date, lookback_days=180, limit=20):
        return {symbol: {"items": [], "data_source": "unavailable"} for symbol in symbols}

    def load_fundamental_context(self, symbols):
        return {
            "300394.SZ": {"industry": "光通信", "description": "CPO 光器件上游核心供应商，少数厂商可供"},
            "600519.SH": {"industry": "白酒"},
        }

    def load_chip_context(self, symbols):
        return {}

    def load_realtime_quote_lookup(self, symbols):
        return {symbol: {"name": "天孚通信" if symbol == "300394.SZ" else "贵州茅台"} for symbol in symbols}

    def load_hot_sector_context(self, trade_date, *, storage):
        return {
            "stock_hot_sectors": {
                "300394.SZ": [{"name": "CPO", "heat_score": 80, "reason": "光通信需求放量"}]
            }
        }


class SerenityScoringTest(unittest.TestCase):
    def test_scorecard_weights_and_penalty_scale_are_stable(self) -> None:
        self.assertEqual(sum(FACTOR_WEIGHTS.values()), 100.0)
        penalties = calculate_penalties(
            {"indicator": {"fundamentals": {}}},
            blob="定增 解禁 高比例质押 出口管制 技术路线之争 周期 概念爆炒",
            market_cap_yi=20,
            amount=10_000,
            evidence_grade="weak",
            ai_chain_hit=True,
        )
        self.assertGreaterEqual(penalties["融资稀释"], 6.0)
        self.assertGreaterEqual(penalties["流动性风险"], 8.0)
        self.assertGreater(sum(penalties.values()), 20.0)

    def test_hard_evidence_scores_above_narrative_only(self) -> None:
        base = {
            "ts_code": "300394.SZ",
            "name": "测试公司",
            "trade_date": "20260514",
            "industry": "光通信",
            "relation_reason": "CPO 光模块 上游",
            "market_cap_yi": 200,
            "indicator": {"fundamentals": {"pe": 25.0, "pb": 3.0, "roe": 18.0}},
            "fundamentals_frame": pd.DataFrame({"end_date": ["1", "2"], "revenue": [100, 140]}),
            "fundamental_extra": {"description": "少数厂商可供，客户切换成本高"},
        }
        strong = score_serenity_candidate(
            {
                **base,
                "news": {
                    "items": [
                        {
                            "dataset": "anns_d",
                            "title": "客户定点后量产交付，认证周期长，订单进入放量",
                        }
                    ]
                },
            }
        )
        weak = score_serenity_candidate(
            {
                **base,
                "news": {"items": [{"dataset": "stock_news_em", "title": "CPO 概念题材受到关注"}]},
            }
        )
        self.assertGreater(strong.final_score, weak.final_score)
        self.assertTrue(strong.passed)
        self.assertFalse(weak.passed)

    def test_non_ai_stock_does_not_pass(self) -> None:
        result = score_serenity_candidate(
            {
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "trade_date": "20260514",
                "industry": "白酒",
                "indicator": {"fundamentals": {"pe": 20.0, "pb": 5.0, "roe": 30.0}},
                "news": {"items": [{"dataset": "anns_d", "title": "年度股东大会公告"}]},
            }
        )
        self.assertFalse(result.ai_chain_hit)
        self.assertFalse(result.passed)


class SerenityServiceTest(unittest.TestCase):
    def test_natural_language_query_extracts_clean_theme(self) -> None:
        self.assertEqual(
            _theme_from_query("请用 Serenity 筛选 AI 半导体前5只"),
            "AI 半导体",
        )

    def test_explicit_symbols_run_two_stage_screen_and_write_cli_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp))
            storage = DuckDBStorage(settings.db_path)
            result = run_serenity_screen(
                symbols=["300394", "600519"],
                trade_date="20260514",
                settings=settings,
                storage=storage,
                astock_provider=FakeSerenityProvider(),
                llm_review=False,
                reports_dir=Path(tmp) / "reports",
            )

            self.assertEqual(result.candidate_source, "explicit_symbols")
            self.assertEqual(result.universe_count, 2)
            self.assertEqual(len(result.candidates), 2)
            self.assertEqual(result.candidates[0].ts_code, "300394.SZ")
            self.assertTrue(result.candidates[0].passed)
            self.assertFalse(next(item for item in result.candidates if item.ts_code == "600519.SH").passed)
            self.assertTrue(result.markdown_report_path.exists())
            self.assertTrue(result.json_artifact_path.exists())
            self.assertIn("供应链层级优先级", result.to_markdown())
            self.assertIn("数据来源", result.to_markdown())
            self.assertIn("data_policy", result.to_dict())
            rows = storage.list_screening_results(
                trade_date="20260514",
                rule_name="serenity_bottleneck",
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(1 for row in rows if row["passed"]), 1)

    def test_accepts_listed_bse_common_stock(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), project_root=Path("."))
        result = run_serenity_screen(
            symbols=["430047"],
            trade_date="20260514",
            settings=settings,
            storage=SimpleNamespace(upsert_screening_results=lambda rows: None),
            astock_provider=FakeSerenityProvider(),
            llm_review=False,
            report=False,
        )
        self.assertEqual(result.candidates[0].ts_code, "430047.BJ")

    def test_rejects_etf(self) -> None:
        settings = SimpleNamespace(db_path=Path("missing.duckdb"), project_root=Path("."))
        with self.assertRaisesRegex(ValueError, "仅支持 A 股普通股票"):
            run_serenity_screen(
                symbols=["512400.SH"],
                trade_date="20260514",
                settings=settings,
                storage=SimpleNamespace(),
                astock_provider=FakeSerenityProvider(),
                llm_review=False,
                report=False,
            )


class SerenityEntrypointTest(unittest.TestCase):
    def test_results_accepts_serenity_rule_name(self) -> None:
        settings = SimpleNamespace(db_path=Path("fake.duckdb"))
        storage = SimpleNamespace(
            list_screening_stocks=lambda **kwargs: [
                {
                    "trade_date": "20260514",
                    "ts_code": "300394.SZ",
                    "name": "天孚通信",
                    "rule_name": "serenity_bottleneck",
                    "passed": True,
                    "score": 86.0,
                }
            ]
        )
        stdout = io.StringIO()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.DuckDBStorage", return_value=storage),
            redirect_stdout(stdout),
        ):
            self.assertEqual(
                main(
                    [
                        "results",
                        "--trade-date",
                        "20260514",
                        "--rule",
                        "serenity_bottleneck",
                        "--passed",
                    ]
                ),
                0,
            )

        self.assertIn("300394.SZ", stdout.getvalue())

    def test_cli_prints_markdown_report(self) -> None:
        request = SerenityScreenRequest(theme="AI半导体", trade_date="20260514")
        fake_result = SimpleNamespace(
            request=request,
            message="",
            markdown_report_path=Path("reports/serenity/fake.md"),
            json_artifact_path=Path("reports/serenity/fake.json"),
            to_markdown=lambda: "# SATS Serenity AI 卡位筛选\n\n## 候选排名\n",
        )
        settings = SimpleNamespace(db_path=Path("fake.duckdb"), project_root=Path("."))
        stdout = io.StringIO()

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.DuckDBStorage", return_value=SimpleNamespace()),
            patch("sats.cli.AStockDataProvider", return_value=SimpleNamespace()),
            patch("sats.cli.run_serenity_screen", return_value=fake_result) as runner,
            redirect_stdout(stdout),
        ):
            self.assertEqual(
                main(["serenity-screen", "--theme", "AI半导体", "--trade-date", "20260514"]),
                0,
            )

        self.assertIn("# SATS Serenity AI 卡位筛选", stdout.getvalue())
        runner.assert_called_once()

    def test_repl_exposes_serenity_command(self) -> None:
        self.assertIn("serenity-screen", CLI_COMMANDS)
        self.assertEqual(
            repl_command_to_argv("/serenity-screen --theme AI半导体 --top 5"),
            ["serenity-screen", "--theme", "AI半导体", "--top", "5"],
        )
        self.assertIn("/serenity-screen", help_text())

    def test_agent_registry_and_planner_use_serenity_only_for_matching_intent(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)
        self.assertIn("research.serenity_screen", registry.names())

        explicit = build_agent_plan(
            "用 Serenity 筛选 AI 半导体前5只",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        automatic = build_agent_plan(
            "推荐几只光通信卡位股",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        ordinary = build_agent_plan(
            "推荐几只未来几天可能上涨的股票",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        other_rule = build_agent_plan(
            "用 price_volume_ma 筛选 AI 股票并分析",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        self.assertEqual(
            [step.tool_name for step in explicit.steps if step.kind == "tool"],
            ["research.serenity_screen"],
        )
        self.assertEqual(
            [step.tool_name for step in automatic.steps if step.kind == "tool"],
            ["research.serenity_screen"],
        )
        self.assertEqual(
            [step.tool_name for step in ordinary.steps if step.kind == "tool"],
            ["research.market_context", "analysis.python_program"],
        )
        self.assertIn(
            "workflow.screened_stock_analysis",
            [step.tool_name for step in other_rule.steps if step.kind == "tool"],
        )

    def test_agent_tool_returns_serenity_payload(self) -> None:
        request = SerenityScreenRequest(theme="AI半导体", trade_date="20260514", report=False)
        fake_result = SerenityScreenResult(request=request, message="无可分析的 Serenity 候选股票")
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path(".")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            session_id="s",
            turn_id="t",
            message="用 Serenity 筛选 AI 半导体",
        )
        registry = build_default_tool_registry()

        with patch("sats.agent.tools.research_tools.run_serenity_screen", return_value=fake_result):
            result = registry.execute(
                "research.serenity_screen",
                {
                    "query": context.message,
                    "trade_date": "20260514",
                    "llm_review": False,
                    "report": False,
                },
                context,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.data_names, ("serenity_screen",))
        self.assertEqual(result.artifacts, ())
        payload = json.loads(result.content)
        self.assertEqual(
            payload["serenity_screen"]["message"],
            "无可分析的 Serenity 候选股票",
        )
        self.assertFalse(payload["serenity_screen"]["request"]["report"])


if __name__ == "__main__":
    unittest.main()
