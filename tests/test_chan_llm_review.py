from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.analysis.chan_llm_review import run_chan_llm_review
from sats.screening.base import ScreeningInput, ScreeningResult
from sats.screening.service import evaluate_and_store
from sats.storage.duckdb import DuckDBStorage
from tests.fixtures import make_chan_daily, make_chan_minute_30m, make_daily_basic


class FakeReviewLLM:
    calls = 0
    messages = []

    def __init__(self) -> None:
        FakeReviewLLM.calls += 1

    def chat(self, messages):
        FakeReviewLLM.messages = messages
        return SimpleNamespace(
            content=(
                '{"reviews":[{"ts_code":"000001.SZ","buy_point_quality":"高",'
                '"risk_flags":["回抽有效"],"watch_levels":{"support":10.18,"invalid":10.07},'
                '"summary":"日线突破后回抽确认，30分钟动能改善。"}]}'
            )
        )


class ChanLLMReviewTest(unittest.TestCase):
    def test_no_candidates_does_not_call_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeReviewLLM.calls = 0
            FakeReviewLLM.messages = []
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")

            result = run_chan_llm_review(
                storage=storage,
                trade_date="20260430",
                reports_dir=Path(tmp) / "reports",
                llm_factory=FakeReviewLLM,
            )

            self.assertEqual(result.message, "无缠论买卖点候选股票")
            self.assertEqual(FakeReviewLLM.calls, 0)
            self.assertIsNone(result.report_path)

    def test_fake_llm_json_generates_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeReviewLLM.calls = 0
            FakeReviewLLM.messages = []
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_basic(
                pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}])
            )
            data = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_chan_daily(),
                daily_basic=make_daily_basic(),
                stock_basic={"name": "平安银行", "market": "主板", "exchange": "SZSE"},
                metadata={"minute_30m": make_chan_minute_30m()},
            )
            evaluate_and_store([data], rule_name="chan-third-buy", storage=storage)

            result = run_chan_llm_review(
                storage=storage,
                trade_date="20260430",
                reports_dir=Path(tmp) / "reports",
                llm_factory=FakeReviewLLM,
            )

            self.assertEqual(FakeReviewLLM.calls, 1)
            self.assertEqual(result.reviewed_codes, ["000001.SZ"])
            self.assertEqual(result.reviews[0]["buy_point_quality"], "高")
            self.assertEqual(result.reviews[0]["name"], "平安银行")
            self.assertIsNotNone(result.report_path)
            self.assertIn("日线突破后回抽确认", result.report_path.read_text(encoding="utf-8"))
            self.assertIn("| 000001.SZ 平安银行 |", result.report_path.read_text(encoding="utf-8"))
            self.assertIn("不得编造价格", FakeReviewLLM.messages[0]["content"])
            self.assertIn("rag_evidence", FakeReviewLLM.messages[-1]["content"])
            self.assertIn('"trade_date": "20260430"', FakeReviewLLM.messages[-1]["content"])
            self.assertIn("data_sources", FakeReviewLLM.messages[-1]["content"])
            self.assertIn("missing_fields", FakeReviewLLM.messages[-1]["content"])

    def test_can_review_chan_signals_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeReviewLLM.calls = 0
            FakeReviewLLM.messages = []
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_basic(
                pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}])
            )
            data = ScreeningInput(
                ts_code="000001.SZ",
                trade_date="20260430",
                daily=make_chan_daily(),
                daily_basic=make_daily_basic(),
                stock_basic={"name": "平安银行", "market": "主板", "exchange": "SZSE"},
                metadata={"minute_30m": make_chan_minute_30m()},
            )
            evaluate_and_store([data], rule_name="chan-signals", storage=storage)

            result = run_chan_llm_review(
                storage=storage,
                trade_date="20260430",
                reports_dir=Path(tmp) / "reports",
                screening_rule_name="chan_signals",
                chan_rule_name="chan_signals",
                llm_factory=FakeReviewLLM,
            )

            self.assertEqual(FakeReviewLLM.calls, 1)
            self.assertEqual(result.reviewed_codes, ["000001.SZ"])
            self.assertIn("缠论买卖点", FakeReviewLLM.messages[-1]["content"])

    def test_can_review_temporary_failed_screening_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeReviewLLM.calls = 0
            FakeReviewLLM.messages = []
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            result = ScreeningResult(
                trade_date="20260430",
                ts_code="000001.SZ",
                rule_name="chan_composite",
                passed=False,
                score=0,
                matched_conditions=[],
                failed_conditions=["chan_first_buy"],
                metrics={"matched_chan_rules": [], "risk_flags": ["未形成买点"]},
            )

            review = run_chan_llm_review(
                storage=storage,
                trade_date="20260430",
                reports_dir=Path(tmp) / "reports",
                screening_rule_name="chan_composite",
                chan_rule_name="chan_composite",
                screening_results=[result],
                names={"000001.SZ": "平安银行"},
                llm_factory=FakeReviewLLM,
            )

            self.assertEqual(FakeReviewLLM.calls, 1)
            self.assertEqual(review.reviewed_codes, ["000001.SZ"])
            payload = FakeReviewLLM.messages[-1]["content"]
            self.assertIn('"passed": false', payload)
            self.assertIn("chan_first_buy", payload)
            self.assertIn("| 000001.SZ 平安银行 | 0 | 否 |", review.report_path.read_text(encoding="utf-8"))

    def test_temporary_review_includes_stock_minute_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeReviewLLM.calls = 0
            FakeReviewLLM.messages = []
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            result = ScreeningResult(
                trade_date="20260430",
                ts_code="000001.SZ",
                rule_name="chan_signals",
                passed=True,
                score=80,
                matched_conditions=["chan_third_buy"],
                failed_conditions=[],
                metrics={"matched_chan_rules": ["三买"]},
            )

            run_chan_llm_review(
                storage=storage,
                trade_date="20260430",
                reports_dir=Path(tmp) / "reports",
                screening_rule_name="chan_signals",
                chan_rule_name="chan_signals",
                screening_results=[result],
                names={"000001.SZ": "平安银行"},
                stock_contexts={
                    "000001.SZ": {
                        "daily_tail": [{"trade_date": "20260430", "close": 10.5}],
                        "minute_curves": {
                            "15m": {"source": "tickflow_history", "rows": [{"trade_time": "2026-04-30 10:00:00", "close": 10.2}]},
                            "30m": {"source": "tickflow_history", "rows": [{"trade_time": "2026-04-30 10:30:00", "close": 10.3}]},
                        },
                        "data_sources": {"minute_15m": "tickflow_history", "minute_30m": "tickflow_history"},
                        "missing_fields": [],
                    }
                },
                llm_factory=FakeReviewLLM,
            )

            payload = FakeReviewLLM.messages[-1]["content"]
            self.assertIn("minute_curves", payload)
            self.assertIn('"15m"', payload)
            self.assertIn('"30m"', payload)
            self.assertIn("tickflow_history", payload)

    def test_screening_rule_filter_and_chan_rule_context_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            FakeReviewLLM.calls = 0
            FakeReviewLLM.messages = []
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_basic(
                pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}])
            )
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260430",
                        ts_code="000001.SZ",
                        rule_name="price_volume_ma",
                        passed=True,
                        score=88,
                        matched_conditions=["price_volume_breakout"],
                        failed_conditions=[],
                        metrics={"matched_conditions": ["price_volume_breakout"]},
                    )
                ]
            )

            review = run_chan_llm_review(
                storage=storage,
                trade_date="20260430",
                reports_dir=Path(tmp) / "reports",
                screening_rule_name="price_volume_ma",
                chan_rule_name="chan_signals",
                llm_factory=FakeReviewLLM,
            )

            self.assertEqual(FakeReviewLLM.calls, 1)
            self.assertEqual(review.reviews[0]["name"], "平安银行")
            payload = FakeReviewLLM.messages[-1]["content"]
            self.assertIn('"screening_rule_name": "price_volume_ma"', payload)
            self.assertIn('"chan_rule_name": "chan_signals"', payload)
            self.assertIn("rag_evidence", payload)
            self.assertIn(
                "chan_llm_review_20260430_price_volume_ma_as_chan_signals_",
                str(review.report_path),
            )


if __name__ == "__main__":
    unittest.main()
