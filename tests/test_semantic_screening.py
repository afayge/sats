from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.agent.models import AgentExecutionPolicy, AgentObservation
from sats.agent.command_runner import CommandRunResult
from sats.agent.planner import build_agent_plan
from sats.agent.tools import AgentToolContext, build_default_tool_registry
from sats.agent.tools.workflow_tools import infer_screening_rule
from sats.catalog import build_capability_catalog
from sats.chat_components import run_rule_generation_component
from sats.memory import ChatMemoryStore
from sats.screening.base import ScreeningInput
from sats.screening.semantic import evaluate_semantic_inputs, semantic_spec_from_message
from sats.storage.duckdb import DuckDBStorage


def _daily(*, pullback: bool = True) -> pd.DataFrame:
    rows = []
    for index, trade_date in enumerate(pd.bdate_range("2026-05-18", periods=45).strftime("%Y%m%d")):
        close = 10.0 + index * 0.08
        low = (close - 0.17 if pullback else close + 1.0) if index >= 40 else close - 0.03
        rows.append(
            {
                "trade_date": trade_date,
                "open": close - 0.02,
                "high": max(close + 0.12, low + 0.10),
                "low": low,
                "close": close,
                "vol": 800.0 if index == 44 else 1000.0,
                "pct_chg": 0.5,
            }
        )
    return pd.DataFrame(rows)


def _input(code: str, name: str, *, pullback: bool = True) -> ScreeningInput:
    daily = _daily(pullback=pullback)
    benchmark = daily.copy()
    benchmark["close"] = 10.0 + pd.Series(range(len(benchmark))) * 0.02
    return ScreeningInput(
        ts_code=code,
        trade_date=str(daily.iloc[-1]["trade_date"]),
        daily=daily,
        daily_basic=pd.DataFrame(),
        stock_basic={"name": name},
        fallback_index_daily=benchmark,
        metadata={"data_source": "fake_daily"},
    )


class SemanticScreeningTest(unittest.TestCase):
    def test_unmatched_natural_language_does_not_fall_back_to_default_rule(self) -> None:
        match = infer_screening_rule("选出趋势较强、回踩不破关键均线的个股")

        self.assertEqual(match.rule_name, "")
        self.assertEqual(match.confidence, 0.0)
        self.assertIn("回踩", match.uncovered_requirements)

        plan = build_agent_plan(
            "选出趋势较强、回踩不破关键均线的个股",
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=build_default_tool_registry(),
        )
        step = next(item for item in plan.steps if item.tool_name == "workflow.screened_stock_analysis")
        self.assertEqual(step.arguments["rule"], "")

    def test_semantic_pullback_spec_uses_hard_and_soft_conditions(self) -> None:
        spec = semantic_spec_from_message("选出趋势较强、回踩不破关键均线的个股")

        self.assertIsNotNone(spec)
        assert spec is not None
        by_id = {str(item["id"]): item for item in spec.conditions}
        self.assertTrue(by_id["recent_pullback_near_ma5_ma10"]["required"])
        self.assertTrue(by_id["recent_close_holds_ma20"]["required"])
        self.assertFalse(by_id["pullback_volume_not_expanding"]["required"])
        self.assertIn("MA20 为趋势失效线", "\n".join(spec.assumptions))

    def test_semantic_evaluator_returns_named_near_misses_without_relaxing_hard_conditions(self) -> None:
        spec = semantic_spec_from_message("选出趋势较强、回踩不破关键均线的个股")
        assert spec is not None
        passing = _input("000001.SZ", "平安银行", pullback=True)
        failing = _input("000002.SZ", "万科A", pullback=False)

        result = evaluate_semantic_inputs([passing, failing], spec, near_miss_limit=10)

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["strict_rows"][0]["name"], "平安银行")
        self.assertEqual(result["near_misses"][0]["name"], "万科A")
        self.assertGreater(result["near_misses"][0]["required_failed_count"], 0)
        self.assertTrue(result["near_misses"][0]["failed_conditions"])

    def test_workflow_runs_ephemeral_spec_without_writing_screening_results(self) -> None:
        item = _input("000001.SZ", "平安银行", pullback=True)
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("unused.duckdb"))
        storage = SimpleNamespace()
        context = AgentToolContext(
            settings=settings,
            storage=storage,
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(dry_run=True),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="选出趋势较强、回踩不破关键均线的个股",
        )
        provider = SimpleNamespace(load_all_screening_inputs=lambda *args, **kwargs: [item])

        with patch("sats.agent.tools.workflow_tools.AStockDataProvider", return_value=provider):
            result = build_default_tool_registry().execute(
                "workflow.screened_stock_analysis",
                {"message": context.message, "trade_date": item.trade_date, "analysis_mode": "batch"},
                context,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.payload["selection_strategy"], "ephemeral_spec")
        self.assertEqual(result.payload["business_status"], "matched")
        self.assertEqual(result.payload["selected_rows"][0]["name"], "平安银行")
        self.assertFalse(hasattr(storage, "upsert_screening_results"))

    def test_existing_rule_zero_result_returns_named_near_miss(self) -> None:
        storage = SimpleNamespace(
            list_screening_stocks=lambda **kwargs: [],
            list_screening_results=lambda **kwargs: [
                {
                    "trade_date": "20260714",
                    "ts_code": "000001.SZ",
                    "rule_name": "ma_volume_relative_strength",
                    "passed": False,
                    "score": 60.0,
                    "matched_conditions": ["positive_day"],
                    "failed_conditions": ["ma_bull_stack_5_10_20_60"],
                    "metrics": {"latest_daily_trade_date": "20260714", "data_source": "fake_daily"},
                }
            ],
            get_stock_basic=lambda: pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}]),
        )
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("unused.duckdb")),
            storage=storage,
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(dry_run=True),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="用 ma_volume_relative_strength 筛选并分析",
        )

        result = build_default_tool_registry().execute(
            "workflow.screened_stock_analysis",
            {"message": context.message, "rule": "ma_volume_relative_strength", "trade_date": "20260714"},
            context,
        )

        self.assertEqual(result.payload["business_status"], "zero_results")
        self.assertEqual(result.payload["near_misses"][0]["name"], "平安银行")
        self.assertEqual(result.payload["near_misses"][0]["failed_conditions"], ["ma_bull_stack_5_10_20_60"])

    def test_screen_command_error_is_not_reported_as_zero_candidates(self) -> None:
        storage = SimpleNamespace(list_screening_stocks=lambda **kwargs: [])
        settings = SimpleNamespace(
            project_root=Path("."),
            db_path=Path("unused.duckdb"),
            self_repair_mode="off",
            self_repair_max_attempts=0,
            self_repair_timeout_seconds=1,
        )
        context = AgentToolContext(
            settings=settings,
            storage=storage,
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(
                run=lambda *args, **kwargs: CommandRunResult(
                    ("screen",), 2, stderr="provider timeout", status="error"
                )
            ),
            trader=SimpleNamespace(),
            message="用 ma_volume_relative_strength 筛选并分析",
        )

        result = build_default_tool_registry().execute(
            "workflow.screened_stock_analysis",
            {"message": context.message, "rule": "ma_volume_relative_strength", "trade_date": "20260714"},
            context,
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.payload["business_status"], "execution_error")
        self.assertEqual(result.payload["screen_result"]["returncode"], 2)
        self.assertIn("provider timeout", result.content)

    def test_catalog_exposes_rule_semantics(self) -> None:
        settings = SimpleNamespace(project_root=Path("."))
        payload = build_capability_catalog(settings=settings, section="screening-rules", limit=50)
        rows = payload["data"]["screening-rules"]["items"]
        item = next(row for row in rows if row["name"] == "ma_volume_relative_strength")

        self.assertTrue(item["description"])
        self.assertTrue(item["semantic_tags"])
        self.assertTrue(item["condition_summary"])
        self.assertTrue(item["data_dependencies"])

    def test_latest_ephemeral_spec_becomes_confirmation_plan_before_file_generation(self) -> None:
        item = _input("000002.SZ", "万科A", pullback=False)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            settings = SimpleNamespace(project_root=Path(tmp), db_path=db_path)
            store = ChatMemoryStore(db_path)
            context = AgentToolContext(
                settings=settings,
                storage=DuckDBStorage(db_path),
                resolver=SimpleNamespace(),
                policy=AgentExecutionPolicy(),
                command_runner=SimpleNamespace(),
                trader=SimpleNamespace(),
                store=store,
                session_id="semantic",
                turn_id="turn_semantic",
                message="选出趋势较强、回踩不破关键均线的个股",
            )
            provider = SimpleNamespace(load_all_screening_inputs=lambda *args, **kwargs: [item])
            with patch("sats.agent.tools.workflow_tools.AStockDataProvider", return_value=provider):
                result = build_default_tool_registry().execute(
                    "workflow.screened_stock_analysis",
                    {"message": context.message, "trade_date": item.trade_date, "analysis_mode": "batch"},
                    context,
                )

            outcome = run_rule_generation_component(
                "保存这个规则",
                settings=settings,
                store=store,
                session_id="semantic",
            )

            pending = store.get_pending_action(outcome.pending_action_id or "")
            self.assertEqual(result.payload["business_status"], "zero_results")
            self.assertTrue(outcome.requires_confirmation)
            self.assertIn("确认生成规则", outcome.content)
            self.assertEqual(
                pending["payload"]["plan"]["conditions"],
                result.payload["semantic_spec"]["conditions"],
            )
            self.assertFalse(list(Path(tmp).rglob("*.py")))

    def test_candidate_python_program_requires_bounded_observation_and_output_contract(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("unused.duckdb"), self_repair_mode="off", self_repair_max_attempts=0)
        context = AgentToolContext(
            settings=settings,
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="候选股票排名",
        )
        registry = build_default_tool_registry()
        code = (
            "def run(context):\n"
            "    return {'rows': [{'ts_code': '000001.SZ', 'name': '平安银行', 'reason': '测试'}], "
            "'provenance': [{'source': 'registered_observation'}]}\n"
        )

        blocked = registry.execute("analysis.python_program", {"task": "候选股票排名", "code": code}, context)
        bounded = context.with_observations(
            (
                AgentObservation(
                    step_id="universe",
                    kind="tool",
                    status="done",
                    content="bounded",
                    payload={"tool_name": "research.theme_stock_list"},
                ),
            )
        )
        valid = registry.execute(
            "analysis.python_program",
            {
                "task": "候选股票排名",
                "code": code,
                "expected_schema": {"type": "object", "required": ["rows", "provenance"]},
            },
            bounded,
        )
        invalid_schema = registry.execute(
            "analysis.python_program",
            {
                "task": "普通计算",
                "code": "def run(context):\n    return {'value': 1}\n",
                "expected_schema": {"type": "object", "required": ["rows"]},
            },
            bounded,
        )

        self.assertEqual(blocked.status, "error")
        self.assertIn("bounded universe", blocked.content)
        self.assertEqual(valid.status, "done")
        self.assertEqual(valid.payload["python_program"]["rows"][0]["name"], "平安银行")
        self.assertEqual(invalid_schema.status, "error")
        self.assertIn("expected_schema", invalid_schema.content)


if __name__ == "__main__":
    unittest.main()
