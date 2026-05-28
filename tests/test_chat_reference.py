from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from sats.chat_reference import build_chat_reference_context, is_reference_question
from sats.output_saver import CapturedOutput


class ChatReferenceContextTest(unittest.TestCase):
    def test_non_reference_question_returns_none(self) -> None:
        captured = CapturedOutput(
            content="1. 000001.SZ 平安银行",
            request="/results --trade-date 20260522 --passed",
            source="/results",
        )

        context = build_chat_reference_context("分析 000001", captured, SimpleNamespace(db_path=Path("test.duckdb")))

        self.assertIsNone(context)
        self.assertFalse(is_reference_question("分析 000001"))

    def test_plain_output_reference_extracts_symbols(self) -> None:
        captured = CapturedOutput(
            content="上一条回答提到 000001.SZ 和 600519.SH",
            request="帮我总结",
            source="chat",
        )

        context = build_chat_reference_context("继续分析上面股票", captured, SimpleNamespace(db_path=Path("test.duckdb")))

        self.assertIsNotNone(context)
        self.assertEqual(context.symbols, ["000001.SZ", "600519.SH"])
        self.assertEqual(context.data_name, "上条输出")
        self.assertIn("上一条可见输出", context.system_message)

    def test_reference_question_accepts_screen_followup_phrase(self) -> None:
        self.assertTrue(is_reference_question("分析上面的股票，预测短期走势"))

    def test_results_reference_uses_structured_scores_from_storage(self) -> None:
        captured = CapturedOutput(
            content="1. 000001.SZ 平安银行 price_volume_ma\n2. 600519.SH 贵州茅台 price_volume_ma",
            request="/results --trade-date 20260522 --passed",
            source="/results",
        )

        class FakeStorage:
            def __init__(self, db_path: Path) -> None:
                self.db_path = db_path

            def list_screening_stocks(self, **kwargs):
                self.kwargs = kwargs
                return [
                    {
                        "ts_code": "600519.SH",
                        "name": "贵州茅台",
                        "rule_name": "price_volume_ma",
                        "score": 88.0,
                        "matched_labels": ["放量"],
                        "metrics": {"turnover_rate": 2.1},
                    },
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "rule_name": "price_volume_ma",
                        "score": 91.0,
                        "matched_labels": ["突破"],
                        "metrics": {"turnover_rate": 3.2},
                    },
                ]

        context = build_chat_reference_context(
            "分析上面列表，选出最高评分的股票",
            captured,
            SimpleNamespace(db_path=Path("test.duckdb")),
            storage_factory=FakeStorage,
        )

        self.assertIsNotNone(context)
        self.assertEqual(context.symbols, ["000001.SZ", "600519.SH"])
        self.assertEqual(context.trade_date, "20260522")
        self.assertEqual(context.data_name, "筛选结果")
        self.assertIn("structured_screening_results_json", context.system_message)
        self.assertIn('"score": 91.0', context.system_message)
        self.assertIn('"name": "贵州茅台"', context.system_message)
        self.assertIn("screening_results.score", context.system_message)

    def test_screen_reference_uses_structured_scores_from_storage(self) -> None:
        captured = CapturedOutput(
            content="1. 000001.SZ 平安银行 突破\n2. 600519.SH 贵州茅台 放量",
            request="/screen --trade-date 20260522 --rule price_volume_ma",
            source="/screen",
        )

        class FakeStorage:
            def __init__(self, db_path: Path) -> None:
                self.db_path = db_path

            def list_screening_stocks(self, **kwargs):
                self.kwargs = kwargs
                return [
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "rule_name": "price_volume_ma",
                        "score": 91.0,
                        "matched_labels": ["突破"],
                        "metrics": {},
                    },
                    {
                        "ts_code": "600519.SH",
                        "name": "贵州茅台",
                        "rule_name": "price_volume_ma",
                        "score": 88.0,
                        "matched_labels": ["放量"],
                        "metrics": {},
                    },
                ]

        context = build_chat_reference_context(
            "对上面股票进行分析",
            captured,
            SimpleNamespace(db_path=Path("test.duckdb")),
            storage_factory=FakeStorage,
        )

        self.assertIsNotNone(context)
        self.assertEqual(context.symbols, ["000001.SZ", "600519.SH"])
        self.assertEqual(context.source, "/screen")
        self.assertIn("structured_screening_results_json", context.system_message)
        self.assertIn('"score": 91.0', context.system_message)

    def test_results_reference_marks_688_prefix_policy(self) -> None:
        captured = CapturedOutput(
            content="1. 688001.SH 华兴源创\n2. 000001.SZ 平安银行",
            request="/results --trade-date 20260522 --passed",
            source="/results",
        )

        class FakeStorage:
            def __init__(self, db_path: Path) -> None:
                pass

            def list_screening_stocks(self, **kwargs):
                return []

        exclude_context = build_chat_reference_context(
            "分析上面列表，提出688开头股票",
            captured,
            SimpleNamespace(db_path=Path("test.duckdb")),
            storage_factory=FakeStorage,
        )
        include_context = build_chat_reference_context(
            "分析上面列表，只看688开头股票",
            captured,
            SimpleNamespace(db_path=Path("test.duckdb")),
            storage_factory=FakeStorage,
        )

        self.assertIn("prefix_policy: exclude_prefix=688", exclude_context.system_message)
        self.assertIn("prefix_policy: include_prefix=688", include_context.system_message)


if __name__ == "__main__":
    unittest.main()
